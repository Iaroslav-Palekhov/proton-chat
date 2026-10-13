#!/usr/bin/env bash
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LOG="$ROOT/autostart.log"

if [[ "${1,,}" == "--autostart" || "${1,,}" == "/autostart" ]]; then
  {
    echo "=== $(date -Iseconds) autostart ==="
    python3 "$ROOT/start_proton_linux.py" --autostart
    ec=$?
    echo "=== exit $ec ==="
    exit $ec
  } >> "$LOG" 2>&1
fi

exec python3 "$ROOT/start_proton_linux.py" "$@"
