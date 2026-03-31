#!/usr/bin/env python3
"""
Brightness Control — Pi 5 Hardware PWM Backlight Controller

System tray app for controlling display backlight via RP1 hardware PWM.
Designed for Raspberry Pi 5 builds using third-party LCD controller
boards (TLT-IPAD3, VS-RTD2556, etc.) that accept a PWM signal on
their DIM/PWM input.

Brightness is set by writing to /sys/class/pwm sysfs files — no
external microcontroller or daemon required. Any process can write
to the same sysfs file; last write wins.

Install:    sudo /opt/brightness-control/install.sh
Uninstall:  sudo /opt/brightness-control/install.sh --uninstall
"""

import os
import sys
import json
import signal
import fcntl
import time
import threading
import subprocess
import logging
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    AppIndicator3 = None

# ── Constants ─────────────────────────────────────────────────────────

APP_ID = "brightness-control"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_SVG = os.path.join(SCRIPT_DIR, "brightness-control.svg")
ICON_NAME = "brightness-control"

CONFIG_DIR = Path.home() / ".config" / "brightness-control"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOCK_FILE = CONFIG_DIR / ".lock"
LOG_FILE = CONFIG_DIR / "log.txt"

PWM_PERIOD_NS = 40000   # 25 kHz

DEFAULT_SETTINGS = {
    "gpio_pin": 12,
    "brightness": 100,
    "auto_dim_enabled": False,
    "auto_dim_minutes": 5,
    "hdmi_off_delay_minutes": 2,
    "min_brightness": 10,
}


# ── PWM Controller ────────────────────────────────────────────────────

class PWMController:
    """Read/write RP1 hardware PWM via sysfs."""

    def __init__(self):
        self.channel_path = None
        self._find_channel()

    def _find_channel(self):
        base = Path("/sys/class/pwm")
        for chip in sorted(base.glob("pwmchip*")):
            candidate = chip / "pwm0"
            if (candidate / "duty_cycle").exists():
                self.channel_path = candidate
                return
        logging.warning("PWM channel not found — brightness control unavailable")

    def is_ready(self):
        return (self.channel_path is not None
                and (self.channel_path / "duty_cycle").exists())

    def get_brightness(self):
        if not self.is_ready():
            return 100
        try:
            duty = int((self.channel_path / "duty_cycle").read_text().strip())
            return max(0, min(100, round(duty * 100 / PWM_PERIOD_NS)))
        except (OSError, ValueError):
            return 100

    def set_brightness(self, pct):
        pct = max(0, min(100, int(pct)))
        if not self.is_ready():
            logging.error("PWM not ready, cannot set brightness")
            return False
        duty = PWM_PERIOD_NS * pct // 100
        try:
            (self.channel_path / "duty_cycle").write_text(str(duty))
            return True
        except PermissionError:
            logging.error("Permission denied writing to PWM sysfs. "
                          "Ensure user is in 'gpio' group and "
                          "brightness-pwm.service is running.")
            return False
        except OSError as e:
            logging.error("Failed to set brightness: %s", e)
            return False


# ── HDMI Manager ──────────────────────────────────────────────────────

class HDMIManager:
    """Snapshot and restore wlr-randr display state."""

    def __init__(self):
        self.saved_states = {}

    def snapshot(self):
        try:
            text = subprocess.check_output(["wlr-randr"], text=True, timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired):
            logging.error("wlr-randr failed — HDMI management unavailable")
            return
        self.saved_states = self._parse(text)

    @staticmethod
    def _parse(text):
        states = {}
        cur = None
        for line in text.splitlines():
            if not line or line[0] not in (" ", "\t"):
                parts = line.split()
                if parts:
                    cur = parts[0]
                    states[cur] = {
                        "enabled": "(enabled)" in line,
                        "mode": None, "pos": None,
                        "transform": "normal", "scale": "1",
                    }
            elif cur and cur in states:
                s = line.strip()
                if "current" in s and "@" in s:
                    p = s.split()
                    if len(p) >= 3:
                        states[cur]["mode"] = p[0]
                elif s.startswith("Position:"):
                    states[cur]["pos"] = s.split(":", 1)[1].strip()
                elif s.startswith("Transform:"):
                    states[cur]["transform"] = s.split(":", 1)[1].strip()
                elif s.startswith("Scale:"):
                    states[cur]["scale"] = s.split(":", 1)[1].strip()
        return states

    def outputs_off(self):
        self.snapshot()
        for name, st in self.saved_states.items():
            if st["enabled"]:
                try:
                    subprocess.run(["wlr-randr", "--output", name, "--off"],
                                   check=True, capture_output=True, timeout=5)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    logging.error("Failed to turn off %s", name)

    def outputs_restore(self):
        for name, st in self.saved_states.items():
            if not st["enabled"]:
                continue
            cmd = ["wlr-randr", "--output", name, "--on"]
            if st["mode"]:
                cmd += ["--mode", st["mode"]]
            if st["transform"] and st["transform"] != "normal":
                cmd += ["--transform", st["transform"]]
            if st["scale"] and st["scale"] != "1":
                cmd += ["--scale", st["scale"]]
            if st["pos"]:
                cmd += ["--pos", st["pos"]]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=5)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                logging.error("Failed to restore %s: %s", name, e)


# ── Idle Watcher ──────────────────────────────────────────────────────

class IdleWatcher(threading.Thread):
    """Monitor input devices via evdev for idle detection."""

    def __init__(self, on_idle, on_active):
        super().__init__(daemon=True, name="idle-watcher")
        self._on_idle = on_idle
        self._on_active = on_active
        self._stop = threading.Event()
        self.timeout_sec = 300
        self.last_input = time.monotonic()
        self._idle = False

    def update_timeout(self, minutes):
        self.timeout_sec = max(60, minutes * 60)

    def run(self):
        try:
            import evdev
            import select as sel
        except ImportError:
            logging.error("python3-evdev not installed — idle detection disabled")
            return

        devices = []
        for path in evdev.list_devices():
            try:
                devices.append(evdev.InputDevice(path))
            except (PermissionError, OSError):
                pass

        if not devices:
            logging.warning("No input devices accessible — is user in 'input' group?")
            return

        fds = {d.fd: d for d in devices}
        logging.info("Idle watcher monitoring %d input devices", len(fds))

        while not self._stop.is_set():
            ready, _, _ = sel.select(list(fds.keys()), [], [], 1.0)
            if ready:
                for fd in ready:
                    try:
                        for _ in fds[fd].read():
                            pass
                    except (OSError, IOError):
                        pass
                self.last_input = time.monotonic()
                if self._idle:
                    self._idle = False
                    GLib.idle_add(self._on_active)
            else:
                elapsed = time.monotonic() - self.last_input
                if not self._idle and elapsed >= self.timeout_sec:
                    self._idle = True
                    GLib.idle_add(self._on_idle)

    def stop(self):
        self._stop.set()


# ── Main Application ──────────────────────────────────────────────────

class BrightnessApp:

    def __init__(self):
        self.settings = self._load_settings()
        self.pwm = PWMController()
        self.hdmi = HDMIManager()
        self.idle_watcher = None
        self.hdmi_off_timer = None
        self.hdmi_is_off = False
        self._is_dimmed = False
        self.pre_dim_brightness = self.settings["brightness"]
        self.slider = None
        self.brightness_window = None
        self.settings_window = None
        self._save_timer = None

        if self.pwm.is_ready():
            actual = self.pwm.get_brightness()
            if actual != self.settings["brightness"]:
                self.settings["brightness"] = actual

        self._build_indicator()
        self._build_menu()

        if self.settings["auto_dim_enabled"]:
            self._start_idle_watcher()

        GLib.timeout_add_seconds(2, self._poll_external_changes)

    # ── Settings ──────────────────────────────────────────────────

    def _load_settings(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE) as f:
                    saved = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update(saved)
                return merged
            except (json.JSONDecodeError, OSError):
                logging.warning("Could not read settings, using defaults")
        return dict(DEFAULT_SETTINGS)

    def _save_settings(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, "w") as f:
                json.dump(self.settings, f, indent=2)
        except OSError as e:
            logging.error("Failed to save settings: %s", e)

    # ── Indicator / tray icon ─────────────────────────────────────

    def _build_indicator(self):
        if AppIndicator3:
            self.indicator = AppIndicator3.Indicator.new(
                APP_ID, ICON_NAME,
                AppIndicator3.IndicatorCategory.HARDWARE)
            self.indicator.set_icon_theme_path(SCRIPT_DIR)
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.status_icon = None
        else:
            self.indicator = None
            self.status_icon = Gtk.StatusIcon.new_from_file(ICON_SVG)
            self.status_icon.set_tooltip_text("Brightness Control")
            self.status_icon.connect("popup-menu", self._on_status_popup)
            self.status_icon.connect("activate", self._on_status_activate)

    def _on_status_popup(self, icon, button, time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu,
                        icon, button, time)

    def _on_status_activate(self, icon):
        self._show_brightness_window()

    # ── Menu ──────────────────────────────────────────────────────

    def _build_menu(self):
        # Touch-friendly CSS (matches volume-control styling)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            scale trough { min-width: 480px; min-height: 48px; }
            scale slider { min-width: 48px;  min-height: 48px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.menu = Gtk.Menu()

        # Brightness label (informational)
        pct = self.settings["brightness"]
        self.brightness_label = Gtk.MenuItem(
            label="Brightness: %d%%" % pct)
        self.brightness_label.set_sensitive(False)
        self.menu.append(self.brightness_label)

        # Open slider window
        adjust_item = Gtk.MenuItem(label="Adjust Brightness\u2026")
        adjust_item.connect("activate",
                            self._show_brightness_window)
        self.menu.append(adjust_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Quick presets
        for level in (25, 50, 75, 100):
            item = Gtk.MenuItem(label="%d%%" % level)
            item.connect("activate", self._on_preset, level)
            self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Auto-dim toggle
        mins = self.settings["auto_dim_minutes"]
        self.auto_dim_item = Gtk.CheckMenuItem(
            label="Auto-dim (%d min)" % mins)
        self.auto_dim_item.set_active(
            self.settings["auto_dim_enabled"])
        self.auto_dim_item.connect("toggled",
                                   self._on_auto_dim_toggled)
        self.menu.append(self.auto_dim_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        settings_item = Gtk.MenuItem(label="Settings\u2026")
        settings_item.connect("activate", self._show_settings)
        self.menu.append(settings_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()
        if self.indicator:
            self.indicator.set_menu(self.menu)

    # ── Brightness Control ────────────────────────────────────────

    def _set_brightness(self, pct):
        pct = max(self.settings["min_brightness"],
                  min(100, int(pct)))
        if self.pwm.set_brightness(pct):
            self.settings["brightness"] = pct
            self._save_settings()
            self._update_ui(pct)

    def _update_ui(self, pct):
        if self.brightness_label:
            self.brightness_label.set_label(
                "Brightness: %d%%" % pct)
        if self.slider and abs(self.slider.get_value() - pct) > 0.5:
            self.slider.handler_block_by_func(
                self._on_slider_changed)
            self.slider.set_value(pct)
            self.slider.handler_unblock_by_func(
                self._on_slider_changed)

    def _on_preset(self, widget, pct):
        self._set_brightness(pct)

    def _poll_external_changes(self):
        """Detect brightness changes made by other processes."""
        if self.pwm.is_ready() and not self.hdmi_is_off \
                and not self._is_dimmed:
            actual = self.pwm.get_brightness()
            if actual != self.settings["brightness"]:
                self.settings["brightness"] = actual
                self._save_settings()
                self._update_ui(actual)
        return True

    # ── Brightness Window ─────────────────────────────────────────

    def _show_brightness_window(self, widget=None):
        if self.brightness_window:
            self.brightness_window.present()
            return

        win = Gtk.Window(title="Brightness")
        win.set_border_width(12)
        win.set_keep_above(True)
        win.set_resizable(False)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.connect("delete-event", self._on_brightness_window_close)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      spacing=10)
        win.add(col)

        lbl = Gtk.Label(label="Brightness")
        lbl.set_xalign(0.0)
        col.pack_start(lbl, False, False, 0)

        self.slider = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            self.settings["min_brightness"], 100, 1)
        self.slider.set_value(self.settings["brightness"])
        self.slider.set_digits(0)
        self.slider.set_draw_value(True)
        self.slider.connect("value-changed",
                            self._on_slider_changed)
        col.pack_start(self.slider, False, False, 0)

        win.show_all()
        self.brightness_window = win

    def _on_brightness_window_close(self, widget, event):
        self.brightness_window.hide()
        self.brightness_window = None
        self.slider = None
        return True

    def _on_slider_changed(self, widget):
        pct = int(widget.get_value())
        self.pwm.set_brightness(pct)
        self.settings["brightness"] = pct
        self.brightness_label.set_label("Brightness: %d%%" % pct)

        # Debounce settings save
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(500,
                                            self._deferred_save)

    def _deferred_save(self):
        self._save_settings()
        self._save_timer = None
        return False

    # ── Settings Dialog ───────────────────────────────────────────

    def _show_settings(self, widget=None):
        if self.settings_window:
            self.settings_window.present()
            return

        win = Gtk.Window(title="Brightness Settings")
        win.set_default_size(320, -1)
        win.set_keep_above(True)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        win.set_resizable(False)
        win.connect("delete-event", self._on_settings_close)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(16)
        grid.set_margin_bottom(16)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        row = 0

        lbl = Gtk.Label(label="Auto-dim on idle:", xalign=0)
        grid.attach(lbl, 0, row, 1, 1)
        self._settings_auto_dim = Gtk.Switch()
        self._settings_auto_dim.set_active(self.settings["auto_dim_enabled"])
        self._settings_auto_dim.connect("notify::active", self._on_settings_auto_dim)
        box = Gtk.Box()
        box.pack_end(self._settings_auto_dim, False, False, 0)
        grid.attach(box, 1, row, 1, 1)
        row += 1

        lbl = Gtk.Label(label="Idle timeout (min):", xalign=0)
        grid.attach(lbl, 0, row, 1, 1)
        self._settings_idle_min = Gtk.SpinButton.new_with_range(1, 60, 1)
        self._settings_idle_min.set_value(self.settings["auto_dim_minutes"])
        self._settings_idle_min.connect("value-changed", self._on_settings_idle_min)
        grid.attach(self._settings_idle_min, 1, row, 1, 1)
        row += 1

        lbl = Gtk.Label(label="Minimum brightness (%):", xalign=0)
        grid.attach(lbl, 0, row, 1, 1)
        self._settings_min_bright = Gtk.SpinButton.new_with_range(1, 50, 1)
        self._settings_min_bright.set_value(self.settings["min_brightness"])
        self._settings_min_bright.connect("value-changed", self._on_settings_min_bright)
        grid.attach(self._settings_min_bright, 1, row, 1, 1)
        row += 1

        lbl = Gtk.Label(label="HDMI off after dim (min):", xalign=0)
        grid.attach(lbl, 0, row, 1, 1)
        self._settings_hdmi_delay = Gtk.SpinButton.new_with_range(
            1, 30, 1)
        self._settings_hdmi_delay.set_value(
            self.settings["hdmi_off_delay_minutes"])
        self._settings_hdmi_delay.connect(
            "value-changed", self._on_settings_hdmi_delay)
        grid.attach(self._settings_hdmi_delay, 1, row, 1, 1)
        row += 1

        lbl = Gtk.Label(label="GPIO pin:", xalign=0)
        grid.attach(lbl, 0, row, 1, 1)
        pin_label = Gtk.Label(label="GPIO%d" % self.settings["gpio_pin"], xalign=0)
        pin_label.set_opacity(0.6)
        grid.attach(pin_label, 1, row, 1, 1)

        win.add(grid)
        win.show_all()
        self.settings_window = win

    def _on_settings_close(self, widget, event):
        self.settings_window.hide()
        self.settings_window = None
        return True

    def _on_settings_auto_dim(self, switch, gparam):
        enabled = switch.get_active()
        self.settings["auto_dim_enabled"] = enabled
        self.auto_dim_item.set_active(enabled)
        self._save_settings()
        if enabled:
            self._start_idle_watcher()
        else:
            self._stop_idle_watcher()

    def _on_settings_idle_min(self, spin):
        val = int(spin.get_value())
        self.settings["auto_dim_minutes"] = val
        self.auto_dim_item.set_label("Auto-dim (%d min)" % val)
        self._save_settings()
        if self.idle_watcher:
            self.idle_watcher.update_timeout(val)

    def _on_settings_min_bright(self, spin):
        val = int(spin.get_value())
        self.settings["min_brightness"] = val
        self._save_settings()
        if self.slider:
            self.slider.get_adjustment().set_lower(val)

    def _on_settings_hdmi_delay(self, spin):
        val = int(spin.get_value())
        self.settings["hdmi_off_delay_minutes"] = val
        self._save_settings()

    # ── Auto-Dim / Idle ───────────────────────────────────────────

    def _on_auto_dim_toggled(self, widget):
        self.settings["auto_dim_enabled"] = widget.get_active()
        self._save_settings()
        if widget.get_active():
            self._start_idle_watcher()
        else:
            self._stop_idle_watcher()

    def _start_idle_watcher(self):
        self._stop_idle_watcher()
        self.idle_watcher = IdleWatcher(on_idle=self._on_idle, on_active=self._on_active)
        self.idle_watcher.update_timeout(self.settings["auto_dim_minutes"])
        self.idle_watcher.start()

    def _stop_idle_watcher(self):
        if self.idle_watcher:
            self.idle_watcher.stop()
            self.idle_watcher = None

    def _on_idle(self):
        self.pre_dim_brightness = self.settings["brightness"]
        self._is_dimmed = True
        self.pwm.set_brightness(self.settings["min_brightness"])
        self._update_ui(self.settings["min_brightness"])
        logging.info("Idle: dimmed to %d%%", self.settings["min_brightness"])
        delay = self.settings["hdmi_off_delay_minutes"] * 60
        self.hdmi_off_timer = GLib.timeout_add_seconds(
            delay, self._hdmi_power_off)

    def _on_active(self):
        if self.hdmi_off_timer:
            GLib.source_remove(self.hdmi_off_timer)
            self.hdmi_off_timer = None
        if self.hdmi_is_off:
            self.hdmi.outputs_restore()
            self.hdmi_is_off = False
            GLib.timeout_add(500, self._restore_brightness_after_hdmi)
        else:
            self._restore_brightness()

    def _restore_brightness_after_hdmi(self):
        self._restore_brightness()
        return False

    def _restore_brightness(self):
        self._is_dimmed = False
        self.pwm.set_brightness(self.pre_dim_brightness)
        self.settings["brightness"] = self.pre_dim_brightness
        self._save_settings()
        self._update_ui(self.pre_dim_brightness)
        logging.info("Active: restored to %d%%", self.pre_dim_brightness)

    def _hdmi_power_off(self):
        self.pwm.set_brightness(0)
        self.hdmi.outputs_off()
        self.hdmi_is_off = True
        self.hdmi_off_timer = None
        logging.info("Extended idle: HDMI off")
        return False

    # ── Lifecycle ─────────────────────────────────────────────────

    def _on_quit(self, widget):
        self._stop_idle_watcher()
        Gtk.main_quit()

    def run(self):
        Gtk.main()


# ── Entry Point ───────────────────────────────────────────────────────

def acquire_lock():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except IOError:
        print("Another instance is already running.", file=sys.stderr)
        sys.exit(1)


def setup_logging():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
        LOG_FILE.with_suffix(".log.old").unlink(missing_ok=True)
        LOG_FILE.rename(LOG_FILE.with_suffix(".log.old"))
    logging.basicConfig(
        filename=str(LOG_FILE), level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    setup_logging()
    lock = acquire_lock()
    logging.info("Brightness Control starting")
    try:
        app = BrightnessApp()
        app.run()
    except Exception as e:
        logging.exception("Fatal error: %s", e)
        raise
    finally:
        lock.close()
        logging.info("Brightness Control stopped")


if __name__ == "__main__":
    main()
