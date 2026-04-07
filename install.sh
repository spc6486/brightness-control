#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────
# Brightness Control — Installer
#
# Installs to /opt/brightness-control with:
#   • System tray indicator (GTK3 + AppIndicator)
#   • Panel autostart on login
#   • CLI access via 'brightness-control'
#   • Hardware PWM backlight dimming (Pi 5 RP1)
#   • Auto-dim and HDMI power-off on idle
#   • Boot-time PWM setup via systemd
#
# Uninstall:  sudo /opt/brightness-control/install.sh --uninstall
# ────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/brightness-control"
LAUNCHER="/usr/local/bin/brightness-control"
DESKTOP_FILE="/usr/share/applications/brightness-control.desktop"
AUTOSTART_SYS="/etc/xdg/autostart/brightness-control.desktop"
ICON_DIR="/usr/share/icons/hicolor/scalable/apps"
ICON_NAME="brightness-control.svg"
SERVICE_NAME="brightness-pwm"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_FILE_ON="/etc/systemd/system/${SERVICE_NAME}-on.service"
CONFIG_TXT="/boot/firmware/config.txt"
MARKER_BEGIN="# BEGIN brightness-control"
MARKER_END="# END brightness-control"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_REAL="${SUDO_USER:-$USER}"
USER_HOME=$(eval echo "~$USER_REAL")
USER_CONFIG="$USER_HOME/.config/brightness-control"

GPIO_PIN="${2:-12}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${BLUE}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
err()   { echo -e "${RED}✗${NC} $*"; }

# ── Uninstall ────────────────────────────────────────────

if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "remove" ]; then
    echo ""
    echo "Removing Brightness Control..."
    echo ""

    pkill -f "brightness-control.py" 2>/dev/null || true
    sleep 1

    for svc in "$SERVICE_NAME.service" "${SERVICE_NAME}-on.service"; do
        if systemctl is-enabled "$svc" &>/dev/null; then
            info "Disabling $svc..."
            sudo systemctl disable "$svc" 2>/dev/null || true
        fi
        sudo systemctl stop "$svc" 2>/dev/null || true
    done
    sudo rm -f "$SERVICE_FILE" "$SERVICE_FILE_ON"
    sudo systemctl daemon-reload 2>/dev/null || true
    ok "Removed systemd services"

    CHIP=$(ls -d /sys/class/pwm/pwmchip* 2>/dev/null | head -1)
    if [ -n "$CHIP" ] && [ -d "$CHIP/pwm0" ]; then
        echo 0 > "$CHIP/pwm0/duty_cycle" 2>/dev/null || true
        echo 0 > "$CHIP/pwm0/enable" 2>/dev/null || true
        echo 0 > "$CHIP/unexport" 2>/dev/null || true
    fi

    if grep -q "$MARKER_BEGIN" "$CONFIG_TXT" 2>/dev/null; then
        sudo cp "$CONFIG_TXT" "${CONFIG_TXT}.pre-uninstall.bak"
        sudo sed -i "/$MARKER_BEGIN/,/$MARKER_END/d" "$CONFIG_TXT"
        ok "Removed PWM overlay from config.txt"
    fi

    sudo rm -rf "$INSTALL_DIR"
    sudo rm -f  "$LAUNCHER"
    sudo rm -f  "$DESKTOP_FILE"
    sudo rm -f  "$AUTOSTART_SYS"
    sudo rm -f  "$ICON_DIR/$ICON_NAME"

    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        sudo gtk-update-icon-cache -f /usr/share/icons/hicolor/ 2>/dev/null || true
    fi
    ok "Removed application files"

    if [ -d "$USER_CONFIG" ] && [ -t 0 ]; then
        echo -n "Remove user settings ($USER_CONFIG)? [y/N] "
        read -r ans
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            rm -rf "$USER_CONFIG"
            ok "Removed user settings"
        else
            info "Kept user settings at $USER_CONFIG"
        fi
    fi

    echo ""
    ok "Brightness Control uninstalled."
    info "Reboot to fully remove the PWM overlay."
    info "config.txt backup: ${CONFIG_TXT}.pre-uninstall.bak"
    echo ""
    exit 0
fi

# ── Install ──────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    err "Run with sudo:  sudo bash $0 [gpio_pin]"
    exit 1
fi

case "$GPIO_PIN" in
    12|13|18|19) ;;
    *) err "GPIO $GPIO_PIN is not hardware PWM capable. Use 12, 13, 18, or 19."; exit 1 ;;
esac

echo ""
info "Installing Brightness Control (GPIO${GPIO_PIN})..."
echo ""

# ── Packages ─────────────────────────────────────────────

info "Checking dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3-gi gir1.2-gtk-3.0 \
    gir1.2-ayatanaappindicator3-0.1 \
    ayatana-indicator-application \
    python3-evdev wlr-randr >/dev/null
ok "Dependencies installed"

# ── User groups ──────────────────────────────────────────

for grp in input gpio; do
    if ! id -nG "$USER_REAL" | grep -qw "$grp"; then
        usermod -aG "$grp" "$USER_REAL"
        warn "Added $USER_REAL to '$grp' group (log out/in to take effect)"
    fi
done

# ── Install app files ────────────────────────────────────

info "Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

for f in brightness-control.py brightness-control.svg \
         pwm-setup.sh install.sh VERSION README.md LICENSE; do
    if [ -f "$SRC/$f" ]; then
        cp "$SRC/$f" "$INSTALL_DIR/"
    fi
done

chmod +x "$INSTALL_DIR/brightness-control.py"
chmod +x "$INSTALL_DIR/pwm-setup.sh"
chmod +x "$INSTALL_DIR/install.sh"
ok "Application files installed"

# ── CLI launcher ─────────────────────────────────────────

cat > "$LAUNCHER" <<'LAUNCH'
#!/usr/bin/env bash
exec python3 /opt/brightness-control/brightness-control.py "$@"
LAUNCH
chmod +x "$LAUNCHER"
ok "CLI launcher: $LAUNCHER"

# ── Install icon ─────────────────────────────────────────

sudo mkdir -p "$ICON_DIR"
sudo cp "$SRC/brightness-control.svg" "$ICON_DIR/$ICON_NAME"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    sudo gtk-update-icon-cache -f /usr/share/icons/hicolor/ 2>/dev/null || true
fi
ok "Icon installed"

# ── Desktop entry ────────────────────────────────────────

cat > "$DESKTOP_FILE" <<'DESK'
[Desktop Entry]
Type=Application
Name=Brightness Control
Comment=Display backlight brightness control
Exec=brightness-control
Icon=brightness-control
Categories=Settings;HardwareSettings;
Terminal=false
StartupNotify=false

[Desktop Action Uninstall]
Name=Uninstall Brightness Control
Exec=bash -c 'pkexec /opt/brightness-control/install.sh --uninstall'
DESK
ok "Desktop entry installed"

# ── Autostart ────────────────────────────────────────────

mkdir -p "$(dirname "$AUTOSTART_SYS")"
cat > "$AUTOSTART_SYS" <<'AUTO'
[Desktop Entry]
Type=Application
Name=Brightness Control
Comment=Display backlight brightness control
Exec=bash -c 'sleep 3 && exec brightness-control'
Icon=brightness-control
X-GNOME-Autostart-enabled=true
NoDisplay=true
AUTO
ok "Autostart entry installed"

# ── User config ──────────────────────────────────────────

mkdir -p "$USER_CONFIG"
if [ ! -f "$USER_CONFIG/settings.json" ]; then
    cat > "$USER_CONFIG/settings.json" <<CONF
{
  "gpio_pin": $GPIO_PIN,
  "brightness": 100,
  "pwm_frequency": 25000,
  "auto_dim_enabled": false,
  "auto_dim_minutes": 5,
  "hdmi_off_delay_minutes": 2,
  "min_brightness": 10
}
CONF
    ok "Created default settings"
else
    python3 -c "
import json
f = '$USER_CONFIG/settings.json'
cfg = json.load(open(f))
cfg['gpio_pin'] = $GPIO_PIN
json.dump(cfg, open(f, 'w'), indent=2)
"
    ok "Updated GPIO pin to $GPIO_PIN in existing settings"
fi
chown -R "$USER_REAL:$USER_REAL" "$USER_CONFIG"

# ── Remove swayidle idle blanking ─────────────────────────
# Brightness Control handles idle dimming and HDMI power
# management. Remove any swayidle lines from labwc autostart
# to avoid conflicts (blue screen on idle, double blanking).

LABWC_AUTO="$USER_HOME/.config/labwc/autostart"
if [ -f "$LABWC_AUTO" ] && grep -q "swayidle" "$LABWC_AUTO"; then
    sed -i '/swayidle/d' "$LABWC_AUTO"
    pkill swayidle 2>/dev/null || true
    ok "Removed swayidle from labwc autostart (brightness-control handles idle)"
fi

# ── config.txt overlay ───────────────────────────────────

OVERLAY_LINE="dtoverlay=pwm,pin=${GPIO_PIN},func=4"
GPIO_LINE="gpio=${GPIO_PIN}=op,dl"

cp "$CONFIG_TXT" "${CONFIG_TXT}.pre-brightness-control.bak"

if grep -q "$MARKER_BEGIN" "$CONFIG_TXT"; then
    sed -i "/$MARKER_BEGIN/,/$MARKER_END/d" "$CONFIG_TXT"
fi

cat >> "$CONFIG_TXT" <<BLOCK

$MARKER_BEGIN
$GPIO_LINE
$OVERLAY_LINE
$MARKER_END
BLOCK
ok "Added PWM overlay and boot GPIO to config.txt"

# ── Systemd services ─────────────────────────────────────

cat > "$SERVICE_FILE" <<'UNIT'
[Unit]
Description=Brightness Control PWM Init (backlight off)
DefaultDependencies=no
After=sysinit.target local-fs.target
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/opt/brightness-control/pwm-setup.sh init
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

cat > "$SERVICE_FILE_ON" <<'UNIT'
[Unit]
Description=Brightness Control PWM Restore (backlight on)
After=brightness-pwm.service plymouth-start.service
Wants=brightness-pwm.service

[Service]
Type=oneshot
ExecStart=/opt/brightness-control/pwm-setup.sh restore
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl enable "${SERVICE_NAME}-on.service"
ok "Systemd services enabled (init + restore after splash)"

# ── Runtime activation ───────────────────────────────────

info "Activating PWM now..."
dtoverlay pwm pin="$GPIO_PIN" func=4 2>/dev/null || true
"$INSTALL_DIR/pwm-setup.sh" both \
    || warn "PWM setup returned non-zero (may need reboot)"

# ── Summary ──────────────────────────────────────────────

PIN_PHYS=$(case $GPIO_PIN in
    12) echo 32 ;; 13) echo 33 ;; 18) echo 12 ;; 19) echo 35 ;;
esac)

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
ok "Brightness Control installed successfully"
echo ""
info "GPIO:       $GPIO_PIN (physical pin $PIN_PHYS)"
info "App:        $INSTALL_DIR/"
info "CLI:        brightness-control"
info "Config:     $USER_CONFIG/settings.json"
info "Services:   $SERVICE_NAME.service (init) + ${SERVICE_NAME}-on.service (restore)"
info "Boot:       Backlight held OFF until Plymouth splash"
info "Uninstall:  sudo $INSTALL_DIR/install.sh --uninstall"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
info "The tray app will autostart on next GUI login."
info "To start now: brightness-control &"
echo ""
