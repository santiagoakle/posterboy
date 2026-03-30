#!/usr/bin/env python3
"""
sandbox_postman -- bridge between ntfy and a tmux session inside a Docker container.

Usage:
  sandbox_postman --container <id> --tmux <session:pane> --path <dir> [--topic <topic>]

  --container   Docker container ID or name
  --tmux        tmux target, e.g. "mysession:0.0" or "0:0"
  --path        directory that contains (or will contain) notifications/outbox.txt
  --topic       ntfy topic  (default: H4CvjgWTb3Pqctmn)
"""

import argparse
import os
import subprocess
import threading
import time
import json
from collections import deque

NTFY_BASE = "https://ntfy.sh"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_published_echo: deque = deque()   # texts we published; suppress before tmux
_echo_lock = threading.Lock()

_last_id: str = ""                 # last received message ID for reconnect
_id_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Docker / tmux helpers
# ---------------------------------------------------------------------------
def tmux_send(container: str, target: str, text: str) -> None:
    """Push text as key-strokes into a tmux pane inside a Docker container."""
    cmd = [
        "docker", "exec", container,
        "tmux", "send-keys", "-t", target, text, "Enter"
    ]
    print(f"[tmux] -> {text!r}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[tmux] error: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# ntfy HTTP helpers
# ---------------------------------------------------------------------------
def ntfy_publish(topic: str, text: str) -> None:
    result = subprocess.run(
        ["curl", "-s", "-d", text, f"{NTFY_BASE}/{topic}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[outbox] publish error: {result.stderr.strip()}")


def process_event(data: dict, container: str, target: str) -> None:
    global _last_id
    msg_id = data.get("id", "")
    if msg_id:
        with _id_lock:
            _last_id = msg_id

    if data.get("event") != "message":
        return
    body = data.get("message", "")
    if not body:
        return

    with _echo_lock:
        if body in _published_echo:
            _published_echo.remove(body)
            print(f"[ntfy] filtered echo: {body!r}")
            return

    tmux_send(container, target, body)


# ---------------------------------------------------------------------------
# ntfy subscriber (curl-based, with poll-on-reconnect)
# ---------------------------------------------------------------------------
def ntfy_listener(topic: str, container: str, target: str) -> None:
    while True:
        # --- poll for any messages missed while disconnected ---
        with _id_lock:
            since_id = _last_id

        poll_url = f"{NTFY_BASE}/{topic}/json?poll=1"
        if since_id:
            poll_url += f"&since={since_id}"

        print(f"[ntfy] polling: {poll_url}")
        poll = subprocess.run(
            ["curl", "-s", poll_url],
            capture_output=True, text=True,
        )
        for line in poll.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            print(f"[ntfy] poll: {line}")
            process_event(data, container, target)

        # --- open streaming connection ---
        with _id_lock:
            since_id = _last_id

        stream_url = f"{NTFY_BASE}/{topic}/json"
        if since_id:
            stream_url += f"?since={since_id}"

        print(f"[ntfy] streaming: {stream_url}")
        proc = subprocess.Popen(
            ["curl", "-s", "-N", stream_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            print(f"[ntfy] raw: {line}")
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            process_event(data, container, target)

        proc.wait()
        print("[ntfy] stream disconnected, reconnecting...")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Outbox monitor
# ---------------------------------------------------------------------------
def outbox_monitor(watch_path: str, topic: str) -> None:
    outbox = os.path.join(watch_path, "notifications", "outbox.txt")
    print(f"[outbox] watching: {outbox}")
    while True:
        if os.path.exists(outbox):
            try:
                with open(outbox, "r") as f:
                    content = f.read().strip()
                os.remove(outbox)
                if content:
                    print(f"[outbox] publishing: {content!r}")
                    with _echo_lock:
                        _published_echo.append(content)
                    ntfy_publish(topic, content)
            except Exception as e:
                print(f"[outbox] error: {e}")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="sandbox_postman",
        description="Bridge ntfy <-> tmux in a Docker container via outbox file."
    )
    parser.add_argument("--container", required=True, help="Docker container ID or name")
    parser.add_argument("--tmux", required=True, dest="tmux_target",
                        help="tmux target, e.g. 'session:window.pane' or '0:0'")
    parser.add_argument("--path", required=True, dest="watch_path",
                        help="Base directory containing notifications/outbox.txt")
    parser.add_argument("--topic", default="H4CvjgWTb3Pqctmn",
                        help="ntfy topic (default: H4CvjgWTb3Pqctmn)")
    args = parser.parse_args()

    os.makedirs(os.path.join(args.watch_path, "notifications"), exist_ok=True)

    listener = threading.Thread(
        target=ntfy_listener,
        args=(args.topic, args.container, args.tmux_target),
        daemon=True,
    )
    listener.start()

    monitor = threading.Thread(
        target=outbox_monitor,
        args=(args.watch_path, args.topic),
        daemon=True,
    )
    monitor.start()

    print(f"[main] sandbox_postman running. container={args.container} tmux={args.tmux_target} path={args.watch_path}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[main] shutting down.")


if __name__ == "__main__":
    main()
