#!/usr/bin/env bash
# Continuously captures output from a tmux pane to a file.
#
# Usage: tmux_capture.sh <session:window.pane> <output_file> [interval_seconds]
#
# Example: tmux_capture.sh mysession:0.0 /tmp/pane.log 1

set -euo pipefail

TARGET="${1:?Usage: $0 <session:window.pane> <output_file> [interval]}"
OUTFILE="${2:?Usage: $0 <session:window.pane> <output_file> [interval]}"
INTERVAL="${3:-1}"

echo "[tmux_capture] monitoring pane '$TARGET' -> '$OUTFILE' (every ${INTERVAL}s)"
echo "[tmux_capture] PID $$. Stop with: kill $$"

# Truncate/create the output file
> "$OUTFILE"

LAST_LINES=0

while true; do
    # Capture full pane history (-S - means from the start of scrollback)
    CURRENT=$(tmux capture-pane -t "$TARGET" -p -S - 2>/dev/null) || {
        echo "[tmux_capture] pane '$TARGET' not found, exiting."
        exit 1
    }
    CURRENT_LINES=$(echo "$CURRENT" | wc -l)

    if [ "$CURRENT_LINES" -gt "$LAST_LINES" ]; then
        # Append only new lines
        echo "$CURRENT" | tail -n +"$((LAST_LINES + 1))" >> "$OUTFILE"
        LAST_LINES="$CURRENT_LINES"
    fi

    sleep "$INTERVAL"
done
