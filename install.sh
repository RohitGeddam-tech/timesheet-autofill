#!/bin/bash
# timesheet-autofill — macOS / Linux installer.
#   ./install.sh              set up / change settings (safe to re-run)
#   ./install.sh --uninstall  stop the daily job
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="timesheet-autofill"

# --- Python 3.9+ ---
PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    echo "Python 3.9+ is needed. Run:  xcode-select --install   (or install from python.org), then re-run this."
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    "$PY" "$DIR/timesheet.py" config set SCHEDULE=off
    user="$("$PY" "$DIR/timesheet.py" config get TS_USER)"
    if [ -n "$user" ]; then
        read -r -p "Also delete the saved site password? [y/N]: " yn
        if [[ "$yn" =~ ^[Yy] ]]; then
            "$PY" -m keyring del "$SERVICE" "$user" 2>/dev/null \
                || security delete-generic-password -s "$SERVICE" -a "$user" >/dev/null
            echo "Password deleted."
        fi
    fi
    exit 0
fi

if ! command -v gh >/dev/null; then
    echo "The GitHub CLI is needed: install it from https://cli.github.com, run 'gh auth login', then re-run this."
    exit 1
fi

# keyring keeps the password in the Keychain / Secret Service. Without it, macOS falls back to `security`.
"$PY" -m pip install --user --quiet --disable-pip-version-check keyring 2>/dev/null \
    || echo "note: could not install 'keyring'; using the macOS Keychain directly."

# `timesheet` command: a wrapper next to the script, pinned to the Python found above.
printf '#!/bin/sh\nexec "%s" "%s/timesheet.py" "$@"\n' "$PY" "$DIR" > "$DIR/timesheet"
chmod +x "$DIR/timesheet"
case ":$PATH:" in
    *":$DIR:"*) ;;
    *) echo "To run 'timesheet' from any folder, add this to your shell profile:  export PATH=\"\$PATH:$DIR\"" ;;
esac

# Settings, password, login test and the optional daily job are all `timesheet config`.
exec "$PY" "$DIR/timesheet.py" config
