import os
import re
import random
import string
import time

from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-to-something-random-in-production"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# In-memory room storage.
# rooms = {
#   "ABC123": {
#       "video": { "type": "youtube"|"drive"|"direct", ... } | None,
#       "state": "paused" | "playing",
#       "time": 0.0,          # last known playback position (seconds)
#       "updated_at": ts,     # time.time() when state/time last changed
#       "users": {sid: name}
#   }
# }
rooms = {}


def make_room_code(length=6):
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choice(alphabet) for _ in range(length))
        if code not in rooms:
            return code


def parse_video_source(url: str):
    """
    Figure out what kind of video link was pasted and return a dict the
    frontend can use to decide which player to show:
      { "type": "youtube", "video_id": "..." }
      { "type": "drive", "video_url": "...", "preview_url": "..." }
      { "type": "direct", "video_url": "..." }
    """
    if not url:
        return {"type": "direct", "video_url": ""}

    # --- YouTube ---
    yt_match = re.search(
        r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/))([a-zA-Z0-9_-]{11})",
        url,
    )
    if yt_match:
        return {"type": "youtube", "video_id": yt_match.group(1)}

    # --- Google Drive ---
    drive_match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if not drive_match:
        drive_match = re.search(r"drive\.google\.com.*[?&]id=([a-zA-Z0-9_-]+)", url)
    if drive_match:
        file_id = drive_match.group(1)
        return {
            "type": "drive",
            "video_url": f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
            "preview_url": f"https://drive.google.com/file/d/{file_id}/preview",
        }

    # --- Anything else: treat as a plain direct video URL ---
    return {"type": "direct", "video_url": url}


def current_playback_time(room):
    """Estimate where playback should be right now, accounting for time
    elapsed since the last known state change (only advances if playing)."""
    r = rooms.get(room)
    if not r:
        return 0.0
    if r["state"] == "playing":
        elapsed = time.time() - r["updated_at"]
        return r["time"] + elapsed
    return r["time"]


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/create", methods=["POST"])
def create_room():
    name = request.form.get("name", "").strip() or "Guest"
    code = make_room_code()
    rooms[code] = {
        "video": None,
        "state": "paused",
        "time": 0.0,
        "updated_at": time.time(),
        "users": {},
    }
    return redirect(url_for("room", code=code, name=name))


@app.route("/join", methods=["POST"])
def join_room_route():
    name = request.form.get("name", "").strip() or "Guest"
    code = request.form.get("code", "").strip().upper()
    if code not in rooms:
        return render_template("index.html", error="Room not found. Check the code and try again.")
    return redirect(url_for("room", code=code, name=name))


@app.route("/room/<code>", methods=["GET"])
def room(code):
    code = code.upper()
    if code not in rooms:
        return redirect(url_for("index"))
    name = request.args.get("name", "Guest")
    return render_template("room.html", code=code, name=name)


# ---------------- Socket.IO events ----------------

@socketio.on("join")
def on_join(data):
    room_code = data.get("room", "").upper()
    name = data.get("name", "Guest")
    if room_code not in rooms:
        return

    join_room(room_code)
    rooms[room_code]["users"][request.sid] = name

    # Send the newcomer the current state of the room so they sync up.
    emit("sync_state", {
        "video": rooms[room_code]["video"],
        "state": rooms[room_code]["state"],
        "time": current_playback_time(room_code),
    })

    emit("system_message", {"text": f"{name} joined the room"}, room=room_code)
    emit("user_list", {"users": list(rooms[room_code]["users"].values())}, room=room_code)


@socketio.on("set_video")
def on_set_video(data):
    room_code = data.get("room", "").upper()
    url = data.get("url", "").strip()
    if room_code not in rooms:
        return

    video = parse_video_source(url)
    rooms[room_code]["video"] = video
    rooms[room_code]["state"] = "paused"
    rooms[room_code]["time"] = 0.0
    rooms[room_code]["updated_at"] = time.time()

    emit("video_set", {"video": video}, room=room_code)


@socketio.on("play")
def on_play(data):
    room_code = data.get("room", "").upper()
    t = data.get("time", 0.0)
    if room_code not in rooms:
        return
    rooms[room_code]["state"] = "playing"
    rooms[room_code]["time"] = t
    rooms[room_code]["updated_at"] = time.time()
    emit("play", {"time": t}, room=room_code, include_self=False)


@socketio.on("pause")
def on_pause(data):
    room_code = data.get("room", "").upper()
    t = data.get("time", 0.0)
    if room_code not in rooms:
        return
    rooms[room_code]["state"] = "paused"
    rooms[room_code]["time"] = t
    rooms[room_code]["updated_at"] = time.time()
    emit("pause", {"time": t}, room=room_code, include_self=False)


@socketio.on("seek")
def on_seek(data):
    room_code = data.get("room", "").upper()
    t = data.get("time", 0.0)
    if room_code not in rooms:
        return
    rooms[room_code]["time"] = t
    rooms[room_code]["updated_at"] = time.time()
    emit("seek", {"time": t}, room=room_code, include_self=False)


@socketio.on("resync")
def on_resync(data):
    """Manual 'sync everyone to my playback position' — most reliable way
    to keep YouTube in sync since play/pause/seek detection via the
    YouTube API is less precise than a plain <video> element."""
    room_code = data.get("room", "").upper()
    t = data.get("time", 0.0)
    playing = data.get("playing", False)
    if room_code not in rooms:
        return
    rooms[room_code]["time"] = t
    rooms[room_code]["state"] = "playing" if playing else "paused"
    rooms[room_code]["updated_at"] = time.time()
    emit("resync", {"time": t, "playing": playing}, room=room_code, include_self=False)


@socketio.on("chat")
def on_chat(data):
    room_code = data.get("room", "").upper()
    name = data.get("name", "Guest")
    msg = data.get("msg", "").strip()
    reply_to = data.get("reply_to")  # optional: {"name": "...", "text": "..."}
    if not msg or room_code not in rooms:
        return
    payload = {"name": name, "msg": msg}
    if isinstance(reply_to, dict) and reply_to.get("text"):
        payload["reply_to"] = {
            "name": str(reply_to.get("name", ""))[:40],
            "text": str(reply_to.get("text", ""))[:200],
        }
    emit("chat", payload, room=room_code)


@socketio.on("disconnect")
def on_disconnect():
    for room_code, r in list(rooms.items()):
        if request.sid in r["users"]:
            name = r["users"].pop(request.sid)
            emit("system_message", {"text": f"{name} left the room"}, room=room_code)
            emit("user_list", {"users": list(r["users"].values())}, room=room_code)


if __name__ == "__main__":
    # Render (and most hosts) provide the port to bind to via the PORT
    # env var. Falls back to 5000 for local development.
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)