import subprocess
import threading
import time
import json
from collections import deque
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

TOPIC = "H4CvjgWTb3Pqctmn"
NTFY_BASE = "https://ntfy.sh"

messages = []
_sent_echo = deque()   # texts we published, pending echo suppression
_sent_lock = threading.Lock()

_last_id: str = ""     # last received message ID for reconnect
_id_lock = threading.Lock()


def ntfy_publish(text: str) -> None:
    subprocess.Popen(
        ["curl", "-s", "-d", text, f"{NTFY_BASE}/{TOPIC}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def process_event(data: dict) -> None:
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

    with _sent_lock:
        if body in _sent_echo:
            _sent_echo.remove(body)
            return   # echo from our own publish, skip it

    messages.append({"text": body, "raw": data, "sender": "them"})


def ntfy_listener():
    while True:
        # --- poll for any messages missed while disconnected ---
        with _id_lock:
            since_id = _last_id

        poll_url = f"{NTFY_BASE}/{TOPIC}/json?poll=1"
        if since_id:
            poll_url += f"&since={since_id}"

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
            print("ntfy poll:", line)
            process_event(data)

        # --- open streaming connection ---
        with _id_lock:
            since_id = _last_id

        stream_url = f"{NTFY_BASE}/{TOPIC}/json"
        if since_id:
            stream_url += f"?since={since_id}"

        print(f"ntfy streaming: {stream_url}")
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
            print("ntfy raw:", line)
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            process_event(data)

        proc.wait()
        print("ntfy stream disconnected, reconnecting...")
        time.sleep(1)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/messages", methods=["GET"])
def get_messages():
    return jsonify(messages)


@app.route("/send", methods=["POST"])
def send_message():
    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False})
    messages.append({"text": text, "sender": "me"})
    with _sent_lock:
        _sent_echo.append(text)
    ntfy_publish(text)
    return jsonify({"ok": True})


if __name__ == "__main__":
    t = threading.Thread(target=ntfy_listener, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080, debug=False)
