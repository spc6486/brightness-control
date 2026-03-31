# Brightness Control

Display backlight brightness control for Raspberry Pi 5 using RP1 hardware PWM.

## Overview

Controls LCD backlight via a hardware PWM signal from the Pi 5's RP1 chip,
driven through a GPIO pin to a display controller board's DIM/PWM input.
Designed for portable Pi builds using third-party LCD controller boards
(TLT-IPAD3, VS-RTD2556, and similar) with a PWM-capable dimming input.

The RP1's hardware PWM runs entirely in silicon — no CPU involvement, no
jitter, no flicker — producing a clean 25 kHz signal independent of system
load.

## Features

- **System tray indicator** with vertical slider and quick presets
- **Settings dialog** for idle timeout, minimum brightness, HDMI off delay
- **Hardware PWM** at 25 kHz via sysfs (flicker-free, CPU-independent)
- **Auto-dim** on idle with configurable timeout
- **HDMI power-off** after extended idle, with full display state restore
- **Brightness persistence** across reboots
- **Boot-dark support** — backlight held OFF until Plymouth splash via
  `gpio=` firmware directive and two-stage systemd startup
- **Shared sysfs interface** — any process can set brightness by writing to
  `/sys/class/pwm/.../duty_cycle` (last write wins)
- **Configurable GPIO pin** (12, 13, 18, or 19)
- **StatusIcon fallback** for X11 sessions

## Boot Sequence

The backlight stays OFF during early boot and turns on when the Plymouth
splash screen appears:

1. **`gpio=N=op,dl`** (VideoCore firmware) — drives GPIO LOW milliseconds after power-on
2. **`brightness-pwm.service`** (early boot) — initializes hardware PWM, backlight OFF
3. **`brightness-pwm-on.service`** (after Plymouth) — restores saved brightness
4. **Tray app autostart** (after GUI login) — takes over brightness control

## Installation

```bash
# Default GPIO12 (physical pin 32):
sudo bash install.sh

# Specify a different hardware PWM pin:
sudo bash install.sh 13    # GPIO13, physical pin 33
sudo bash install.sh 18    # GPIO18, physical pin 12
sudo bash install.sh 19    # GPIO19, physical pin 35
```

## Uninstallation

```bash
sudo /opt/brightness-control/install.sh --uninstall
```

## Wiring

Connect the chosen GPIO pin to your display controller board's DIM/PWM
input through a series resistor:

```
Pi GPIO ── 1kΩ ── DIM pad on LCD controller board
Pi GND  ── GND on LCD controller board (common ground)
```

**Resistor values:**

- **1kΩ series resistor** (required) — placed between the GPIO pin and the
  DIM pad. Protects the GPIO if the DIM pin has an internal pull-up to a
  higher voltage (e.g., 5V). Limits worst-case current to ~1.7mA at the
  5V/3.3V mismatch.

- **4.7kΩ pull-down resistor** (optional) — placed between the DIM pad and
  GND. Holds the backlight OFF when the Pi is powered down or the GPIO is
  not yet initialized. Without this, the display controller's internal
  pull-up keeps the backlight on whenever the board has power. The 4.7kΩ
  value is strong enough to overcome most internal pull-ups (~10kΩ) while
  being easily overpowered by the 1kΩ drive during normal PWM operation.

```
               1kΩ
Pi GPIO ──────┤├────┬──── DIM pad
                    │
                   4.7kΩ  (optional, for boot-dark)
                    │
                   GND
```

Keep the backlight driver's EN (enable) pin held HIGH as required by
your board.

**Hardware PWM-capable GPIO pins on the Pi 5:**

| GPIO | Physical Pin | PWM Channel |
|------|-------------|-------------|
| 12   | 32          | PWM0_CHAN0  |
| 13   | 33          | PWM0_CHAN1  |
| 18   | 12          | PWM0_CHAN2  |
| 19   | 35          | PWM0_CHAN3  |

## Configuration

User settings: `~/.config/brightness-control/settings.json`

| Key                   | Default | Description                          |
|-----------------------|---------|--------------------------------------|
| gpio_pin              | 12      | Hardware PWM GPIO (12, 13, 18, 19)   |
| brightness            | 100     | Last-set brightness percentage       |
| auto_dim_enabled      | false   | Enable idle dimming                  |
| auto_dim_minutes      | 5       | Minutes of idle before auto-dim      |
| hdmi_off_delay_minutes| 2       | Minutes after dim before HDMI off    |
| min_brightness        | 10      | Minimum brightness percentage        |

## Integration

Brightness can be controlled by any process that writes to the sysfs
PWM interface. The tray app polls for external changes every 2 seconds
and updates its slider accordingly.

```bash
# Set brightness to 50% from a script:
CHIP=$(ls -d /sys/class/pwm/pwmchip* | head -1)
echo 20000 > "$CHIP/pwm0/duty_cycle"   # 20000/40000 = 50%
```

## Dependencies

- Raspberry Pi 5 (RP1 chip required for hardware PWM)
- Raspberry Pi OS Bookworm (Wayland or X11)
- python3-gi, gir1.2-gtk-3.0
- gir1.2-ayatanaappindicator3-0.1, ayatana-indicator-application
- python3-evdev (idle detection)
- wlr-randr (HDMI power management, Wayland only)

## License

MIT
