#!/bin/bash
# Brightness Control — PWM Setup (runs as root at boot)
#
# Usage:
#   pwm-setup.sh init     — Early boot: load overlay, start with backlight OFF
#   pwm-setup.sh restore  — After splash: set saved brightness + permissions
#   pwm-setup.sh          — Both in one (used during install for immediate activation)
set -euo pipefail

LOG_TAG="brightness-pwm"
log() { logger -t "$LOG_TAG" "$1"; echo "$1"; }

MODE="${1:-both}"

# Determine config home for the user who installed the app
# Check common home directories in order
for candidate in /home/*/; do
    u="${candidate%/}"
    u="${u##*/}"
    if [ -f "$u/.config/brightness-control/settings.json" ] 2>/dev/null; then
        CONFIG_HOME="$u/.config/brightness-control"
        break
    fi
    if [ -f "/home/$u/.config/brightness-control/settings.json" ]; then
        CONFIG_HOME="/home/$u/.config/brightness-control"
        break
    fi
done
CONFIG_HOME="${CONFIG_HOME:-/home/pi/.config/brightness-control}"
SETTINGS="$CONFIG_HOME/settings.json"

DEFAULT_PIN=12
DEFAULT_BRIGHTNESS=100
DEFAULT_FREQUENCY=25000

PIN=$DEFAULT_PIN
BRIGHTNESS=$DEFAULT_BRIGHTNESS
FREQUENCY=$DEFAULT_FREQUENCY

if [ -f "$SETTINGS" ]; then
    PIN=$(python3 -c "
import json
try:
    print(json.load(open('$SETTINGS')).get('gpio_pin', $DEFAULT_PIN))
except Exception:
    print($DEFAULT_PIN)
" 2>/dev/null || echo $DEFAULT_PIN)

    BRIGHTNESS=$(python3 -c "
import json
try:
    print(json.load(open('$SETTINGS')).get('brightness', $DEFAULT_BRIGHTNESS))
except Exception:
    print($DEFAULT_BRIGHTNESS)
" 2>/dev/null || echo $DEFAULT_BRIGHTNESS)

    FREQUENCY=$(python3 -c "
import json
try:
    print(json.load(open('$SETTINGS')).get('pwm_frequency', $DEFAULT_FREQUENCY))
except Exception:
    print($DEFAULT_FREQUENCY)
" 2>/dev/null || echo $DEFAULT_FREQUENCY)
fi

PERIOD=$((1000000000 / FREQUENCY))

# ── Find or set up PWM channel ────────────────────────────────────────

setup_channel() {
    dtoverlay pwm pin="$PIN" func=4 2>/dev/null || true

    CHIP=$(ls -d /sys/class/pwm/pwmchip* 2>/dev/null | head -1)
    if [ -z "$CHIP" ]; then
        log "ERROR: No PWM chip found"
        exit 1
    fi

    echo 0 > "$CHIP/export" 2>/dev/null || true
    sleep 0.2

    CHAN="$CHIP/pwm0"
    if [ ! -d "$CHAN" ]; then
        log "ERROR: PWM channel not available at $CHAN"
        exit 1
    fi
}

find_channel() {
    CHIP=$(ls -d /sys/class/pwm/pwmchip* 2>/dev/null | head -1)
    CHAN="$CHIP/pwm0"
    if [ -z "$CHIP" ] || [ ! -d "$CHAN" ]; then
        log "ERROR: PWM channel not available — was init run?"
        exit 1
    fi
}

# ── Init: early boot, backlight OFF ───────────────────────────────────

do_init() {
    log "Init: PWM on GPIO${PIN} at ${FREQUENCY}Hz, backlight OFF"
    setup_channel
    echo $PERIOD > "$CHAN/period"
    echo 0      > "$CHAN/duty_cycle"
    echo 1      > "$CHAN/enable"
    log "Init complete: backlight held OFF"
}

# ── Restore: set saved brightness + permissions ───────────────────────

do_restore() {
    log "Restore: setting brightness to ${BRIGHTNESS}% at ${FREQUENCY}Hz"
    find_channel
    DUTY=$((PERIOD * BRIGHTNESS / 100))
    echo "$DUTY" > "$CHAN/duty_cycle"

    chgrp -R gpio "$CHAN/" 2>/dev/null || true
    chmod g+w "$CHAN/duty_cycle" "$CHAN/enable" 2>/dev/null || true

    log "PWM ready: GPIO${PIN}, ${BRIGHTNESS}%, ${FREQUENCY}Hz, chip=$CHIP"
}

# ── Dispatch ──────────────────────────────────────────────────────────

case "$MODE" in
    init)    do_init ;;
    restore) do_restore ;;
    both)    do_init; do_restore ;;
    *)       log "Usage: $0 {init|restore|both}"; exit 1 ;;
esac
