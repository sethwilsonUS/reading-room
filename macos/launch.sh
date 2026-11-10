#!/bin/sh
# Runs `bin/library app` for the checkout that contains this Reading Room.app.
# Installed by scripts/build_mac_app.py at Reading Room.app/Contents/Resources.
set -u
BUNDLE=$(CDPATH= cd -P -- "$(dirname -- "$0")/../.." && pwd)
CHECKOUT=$(dirname -- "$BUNDLE")
STATE="$CHECKOUT/.reading-room"
LOG="$STATE/app.log"

alert() {
  # Pass the message as an argument; never interpolate it into AppleScript.
  /usr/bin/osascript -e 'on run argv' \
    -e 'display alert "The Reading Room could not open" message (item 1 of argv) as critical' \
    -e 'end run' "$1" >/dev/null 2>&1
}

if [ ! -x "$CHECKOUT/bin/library" ]; then
  alert "Keep Reading Room.app in its reading-room folder, next to bin/library. If it is there, run ./bin/setup again."
  exit 1
fi
mkdir -p "$STATE"
if ! "$CHECKOUT/bin/library" app >"$LOG" 2>&1; then
  detail=$(tail -n 3 "$LOG")
  alert "${detail:-Unknown error.}

Full log: $LOG"
  exit 1
fi
