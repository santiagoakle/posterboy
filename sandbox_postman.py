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

# Keys accepted as permission responses
PERMISSION_KEYS = {"y", "n", "a", "d", "s", "yes", "no", "1", "2", "3"}

# Patterns that indicate an actual interactive permission dialog (not just the mode status bar)
PERMISSION_PATTERNS = [
    "Do you want to proceed",
    "Esc to cancel",
    "Tab to amend",
    "(y/n",
    "❯ 1.",
]

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_published_echo: deque = deque()
_echo_lock = threading.Lock()
_permission_active = threading.Event()


# ---------------------------------------------------------------------------
# tmux / Docker helpers
# ---------------------------------------------------------------------------
def tmux_send(target: str, text: str, container: str | None = None) -> None:
    """Send text + Enter to a tmux pane."""
    if container:
        cmd = ["docker", "exec", container, "tmux", "send-keys", "-t", target, text, "Enter"]
    else:
        cmd = ["tmux", "send-keys", "-t", target, text, "Enter"]
    print(f"[tmux] -> {text!r}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[tmux] error: {result.stderr.strip()}")


def tmux_send_key(target: str, key: str, container: str | None = None) -> None:
    """Send a single keypress (no Enter) to a tmux pane."""
    if container:
        cmd = ["docker", "exec", container, "tmux", "send-keys", "-t", target, key]
    else:
        cmd = ["tmux", "send-keys", "-t", target, key]
    print(f"[tmux] key -> {key!r}")
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


_SPINNER_CHARS = frozenset("·✶✻✢✸✹✺")

def is_busy(content: str) -> bool:
    """Return True if Claude is still thinking or running a command."""
    for line in content.splitlines():
        s = line.strip()
        if s and s[0] in _SPINNER_CHARS:
            return True
        if "Running…" in line or "Running..." in line:
            return True
    return False


def wait_for_idle(target: str, container: str | None = None,
                  stable_secs: float = 2.0, poll: float = 0.3,
                  timeout: float = 120.0) -> str:
    """Poll pane until output stops changing for stable_secs and no spinner is active."""
    last = capture_pane(target, container)
    stable_since = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        current = capture_pane(target, container)
        if current != last:
            last = current
            stable_since = time.time()
        elif time.time() - stable_since >= stable_secs and not is_busy(current):
            return current
    return capture_pane(target, container)


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def is_permission_prompt(content: str) -> bool:
    return any(p in content for p in PERMISSION_PATTERNS)


def extract_permission_dialog(content: str) -> str:
    """Return only the stable dialog lines, ignoring spinners and status bar."""
    keep = {"Do you want to proceed", "Esc to cancel", "Tab to amend",
            "(y/n", "❯ 1.", "❯ 2.", "❯ 3.", "Allow", "Deny", "ctrl+e"}
    lines = [l for l in content.splitlines()
             if any(k in l for k in keep)]
    return "\n".join(lines)


def publish_ntfy(topic: str, message: str, title: str = "claude") -> None:
    with _echo_lock:
        _published_echo.append(message)
    try:
        req = urllib.request.Request(
            f"{NTFY_BASE}/{topic}",
            data=message.encode(),
            method="POST",
            headers={"Title": title},
        )
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"[ntfy] publish error: {e}")


def permission_monitor(pane: str, container: str | None, topic: str, poll: float = 1.0) -> None:
    """Watch the Claude pane for permission prompts; publish and signal when detected."""
    last_hash = None
    print(f"[perm] monitoring {pane} for permission prompts")
    while True:
        try:
            content = capture_pane(pane, container)
            if is_permission_prompt(content):
                dialog = extract_permission_dialog(content)
                h = hash(dialog)
                _permission_active.set()
                if h != last_hash:
                    last_hash = h
                    relevant = [l for l in content.splitlines() if l.strip()]
                    prompt_text = "\n".join(relevant[-15:])
                    print(f"[perm] permission prompt detected, publishing to ntfy")
                    publish_ntfy(topic, prompt_text, title="permission")
            else:
                if _permission_active.is_set():
                    print(f"[perm] permission cleared")
                _permission_active.clear()
                last_hash = None
        except Exception as e:
            print(f"[perm] error: {e}")
        time.sleep(poll)


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
    time.sleep(0.5)

    after = wait_for_idle(pane, container, stable_secs=stable_secs)
    new_lines = [l for l in after.splitlines()
                 if l.rstrip()
                 and l.rstrip() not in before_lines
                 and not l.lstrip().startswith("❯")]
    response = "\n".join(new_lines).strip()

    if not response:
        print("[claude] no new output captured")
        return

    print(f"[claude] response ({len(response)} chars):\n{response[:200]}{'...' if len(response) > 200 else ''}")
    publish_ntfy(topic, response, title="claude")
    print("[claude] response published")


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
                        title = data.get("title", "")
                        if title in ("claude", "permission"):
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
                            if _permission_active.is_set() and body.strip().lower() in PERMISSION_KEYS:
                                print(f"[perm] forwarding permission response: {body.strip()!r}")
                                tmux_send_key(claude_pane, body.strip(), container)
                            else:
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
                    publish_ntfy(topic, content, title="outbox")
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

    if args.claude_pane:
        threading.Thread(
            target=permission_monitor,
            args=(args.claude_pane, args.container, args.topic),
            daemon=True,
        ).start()

    threading.Thread(
        target=ntfy_listener,
        args=(args.topic, args.container, args.tmux_target, args.claude_pane, args.stable_secs),
        daemon=True,
    ).start()

    if args.watch_path:
        threading.Thread(
            target=outbox_monitor,
            args=(args.watch_path, args.topic),
            daemon=True,
        ).start()

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
