#!/usr/bin/env python3
"""
sandbox_postman -- bridge between ntfy and a tmux session inside a Docker container.

Usage:
  sandbox_postman --container <id> --tmux <session:pane> --path <dir> [--topic <topic>]

  --container   Docker container ID or name
  --tmux        tmux target, e.g. "mysession:0.0" or "0:0"
  --path        directory that contains (or will contain) notifications/outbox.txt
  --topic       ntfy topic
"""

import argparse
import os
import threading
import time
import json
import urllib.request
from collections import deque

NTFY_BASE = "https://ntfy.sh"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_published_echo: deque = deque()   # texts we published; suppress before tmux
_echo_lock = threading.Lock()


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
# ntfy subscriber
# ---------------------------------------------------------------------------
def ntfy_listener(topic: str, container: str, target: str) -> None:
    print(f"[ntfy] subscribing to topic: {topic}")
    while True:
        try:
            req = urllib.request.Request(f"{NTFY_BASE}/{topic}/json")
            with urllib.request.urlopen(req) as resp:
                buf = b""
                while True:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    if chunk == b"\n":
                        line = buf.decode().strip()
                        buf = b""
                        if not line:
                            continue
                        print(f"[ntfy] raw: {line}")
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("event") != "message":
                            continue
                        body = data.get("message", "")
                        if not body:
                            continue
                        with _echo_lock:
                            if body in _published_echo:
                                _published_echo.remove(body)
                                print(f"[ntfy] filtered echo: {body!r}")
                                continue
                        tmux_send(container, target, f"N:{body}")
                    else:
                        buf += chunk
        except Exception as e:
            print(f"[ntfy] stream error: {e}, reconnecting in 2s")
            time.sleep(2)


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
                    print(content, flush=True)
                    print(f"[outbox] publishing: {content!r}")
                    with _echo_lock:
                        _published_echo.append(content)
                    try:
                        req = urllib.request.Request(
                            f"{NTFY_BASE}/{topic}",
                            data=content.encode(),
                            method="POST",
                        )
                        urllib.request.urlopen(req)
                    except Exception as e:
                        print(f"[outbox] publish error: {e}")
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
    parser.add_argument("--topic", required=True,
                        help="ntfy topic")
    args = parser.parse_args()

    # Ensure outbox directory exists
    os.makedirs(os.path.join(args.watch_path, "notifications"), exist_ok=True)

    # Start ntfy subscriber thread
    listener = threading.Thread(
        target=ntfy_listener,
        args=(args.topic, args.container, args.tmux_target),
        daemon=True,
    )
    listener.start()

    # Start outbox monitor thread
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
