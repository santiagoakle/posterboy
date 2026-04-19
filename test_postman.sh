#!/usr/bin/env bash
# End-to-end test for sandbox_postman in --claude-pane mode.
# Creates a local tmux session running a bash shell, starts the postman,
# sends a message via ntfy, and checks the response comes back.
#
# Usage: ./test_postman.sh <ntfy-topic>

set -euo pipefail

TOPIC="${1:?Usage: $0 <ntfy-topic>}"
SESSION="pbtest_$$"
LOGFILE="/tmp/postman_test_$$.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
    echo "[test] cleaning up..."
    kill "$POSTMAN_PID" 2>/dev/null || true
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    rm -f "$LOGFILE"
}
trap cleanup EXIT

# 1. Start tmux session with a bash shell
echo "[test] starting tmux session '$SESSION'..."
tmux new-session -d -s "$SESSION" -x 220 -y 50
sleep 0.5

# 2. Start postman in background
echo "[test] starting sandbox_postman..."
python3 "$SCRIPT_DIR/sandbox_postman.py" \
    --topic "$TOPIC" \
    --claude-pane "${SESSION}:0" \
    --stable-secs 1.5 \
    > "$LOGFILE" 2>&1 &
POSTMAN_PID=$!
echo "[test] postman PID: $POSTMAN_PID"
sleep 2  # let it connect to ntfy stream

# 3. Send a test message via ntfy
MSG="echo POSTMAN_TEST_$(date +%s)"
echo "[test] sending message: $MSG"
RESPONSE=$(curl -s -d "$MSG" "ntfy.sh/$TOPIC")
echo "[test] ntfy publish response: $RESPONSE"
sleep 0.5

# 4. Wait for postman to send it to tmux and capture the response
echo "[test] waiting for response (up to 15s)..."
DEADLINE=$((SECONDS + 15))
while [ $SECONDS -lt $DEADLINE ]; do
    if grep -q "response published" "$LOGFILE" 2>/dev/null; then
        echo "[test] PASS: response was published back to ntfy"
        echo "[test] postman log:"
        cat "$LOGFILE"
        exit 0
    fi
    sleep 1
done

echo "[test] FAIL: timed out waiting for response"
echo "[test] postman log:"
cat "$LOGFILE"
exit 1
