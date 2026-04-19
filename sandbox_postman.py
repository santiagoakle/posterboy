#!/usr/bin/env python3
"""
sandbox_postman -- bridge between ntfy and a tmux pane (local or inside Docker).

Usage:
  sandbox_postman --topic <topic> [--container <id>] [--claude-pane <target>]
                  [--tmux <target>] [--path <dir>] [--stable-secs <n>]

  --topic         ntfy topic (required)
  --container     Docker container ID/name; omit for local tmux
  --claude-pane   tmux target running Claude Code; inbound ntfy messages are
                  sent as input, responses captured and published back to ntfy
  --tmux          tmux target for legacy N:<msg> forwarding (used when
                  --claude-pane is not set)
  --path          directory containing notifications/outbox.txt to watch
  --stable-secs   seconds of no output change before considering Claude idle
                  (default: 2.0)
"""

import argparse
import os
import subprocess
import threading
import time
import json
import urllib.request
from collections import deque

NTFY_BASE = "https://ntfy.sh"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_published_echo: deque = deque()
_echo_lock = threading.Lock()


# ---------------------------------------------------------------------------
# tmux / Docker helpers
# ---------------------------------------------------------------------------
def tmux_send(target: str, text: str, container: str | None = None) -> None:
    """Send keys to a tmux pane, optionally via docker exec."""
    if container:
        cmd = ["docker", "exec", container, "tmux", "send-keys", "-t", target, text, "Enter"]
    else:
        cmd = ["tmux", "send-keys", "-t", target, text, "Enter"]
    print(f"[tmux] -> {text!r}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[tmux] error: {result.stderr.strip()}")


def capture_pane(target: str, container: str | None = None) -> str:
    """Return the current visible content of a tmux pane."""
    cmd = ["tmux", "capture-pane", "-t", target, "-p"]
    if container:
        cmd = ["docker", "exec", container] + cmd
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def wait_for_idle(target: str, container: str | None = None,
                  stable_secs: float = 2.0, poll: float = 0.3,
                  timeout: float = 120.0) -> str:
    """Poll pane until output stops changing for stable_secs; return final content."""
    last = capture_pane(target, container)
    stable_since = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        current = capture_pane(target, container)
        if current != last:
            last = current
            stable_since = time.time()
        elif time.time() - stable_since >= stable_secs:
            return current
    return capture_pane(target, container)


# ---------------------------------------------------------------------------
# Claude pane handler
# ---------------------------------------------------------------------------
def handle_claude_input(text: str, topic: str, pane: str,
                        container: str | None, stable_secs: float) -> None:
    """Send text to the Claude pane, wait for response, publish it back to ntfy."""
    before = capture_pane(pane, container)
    before_lines = set(before.splitlines())

    time.sleep(0.2)
    tmux_send(pane, text, container)
    time.sleep(0.5)  # let Claude start before polling

    after = wait_for_idle(pane, container, stable_secs=stable_secs)
    new_lines = [l for l in after.splitlines()
                 if l.rstrip() and l.rstrip() not in before_lines]
    response = "\n".join(new_lines).strip()

    if not response:
        print("[claude] no new output captured")
        return

    print(f"[claude] response ({len(response)} chars):\n{response[:200]}{'...' if len(response) > 200 else ''}")

    with _echo_lock:
        _published_echo.append(response)
    try:
        req = urllib.request.Request(
            f"{NTFY_BASE}/{topic}",
            data=response.encode(),
            method="POST",
            headers={"Title": "claude"},
        )
        urllib.request.urlopen(req)
        print("[claude] response published")
    except Exception as e:
        print(f"[claude] publish error: {e}")


# ---------------------------------------------------------------------------
# ntfy subscriber
# ---------------------------------------------------------------------------
def ntfy_listener(topic: str, container: str | None, target: str | None,
                  claude_pane: str | None, stable_secs: float) -> None:
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
                        # Skip responses we published ourselves
                        if data.get("title") == "claude":
                            continue
                        body = data.get("message", "")
                        if not body:
                            continue
                        with _echo_lock:
                            if body in _published_echo:
                                _published_echo.remove(body)
                                print(f"[ntfy] filtered echo: {body!r}")
                                continue
                        if claude_pane:
                            threading.Thread(
                                target=handle_claude_input,
                                args=(body, topic, claude_pane, container, stable_secs),
                                daemon=True,
                            ).start()
                        elif target:
                            tmux_send(target, f"N:{body}", container)
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
        description="Bridge ntfy <-> tmux (local or Docker)."
    )
    parser.add_argument("--topic", required=True, help="ntfy topic")
    parser.add_argument("--container", default=None,
                        help="Docker container ID/name (omit for local tmux)")
    parser.add_argument("--claude-pane", default=None, dest="claude_pane",
                        help="tmux target running Claude Code")
    parser.add_argument("--tmux", default=None, dest="tmux_target",
                        help="tmux target for N:<msg> forwarding")
    parser.add_argument("--path", default=None, dest="watch_path",
                        help="Base directory containing notifications/outbox.txt")
    parser.add_argument("--stable-secs", type=float, default=2.0, dest="stable_secs",
                        help="Seconds of idle output before capturing Claude response (default: 2.0)")
    args = parser.parse_args()

    if not args.claude_pane and not args.tmux_target:
        parser.error("at least one of --claude-pane or --tmux is required")

    if args.watch_path:
        os.makedirs(os.path.join(args.watch_path, "notifications"), exist_ok=True)

    listener = threading.Thread(
        target=ntfy_listener,
        args=(args.topic, args.container, args.tmux_target, args.claude_pane, args.stable_secs),
        daemon=True,
    )
    listener.start()

    if args.watch_path:
        monitor = threading.Thread(
            target=outbox_monitor,
            args=(args.watch_path, args.topic),
            daemon=True,
        )
        monitor.start()

    mode = f"claude-pane={args.claude_pane}" if args.claude_pane else f"tmux={args.tmux_target}"
    container_info = f" container={args.container}" if args.container else " (local)"
    print(f"[main] running. {mode}{container_info} topic={args.topic}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[main] shutting down.")


if __name__ == "__main__":
    main()
