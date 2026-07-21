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
#       "video_url": "https://...",
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


def drive_share_link_to_direct(url: str) -> str:
    """
    Convert a Google Drive share link into a direct-playable video URL.
    Handles formats like:
      https://drive.google.com/file/d/FILE_ID/view?usp=sharing
      https://drive.google.com/open?id=FILE_ID
      https://drive.google.com/uc?id=FILE_ID
    Falls back to returning the original URL untouched if it isn't a
    recognizable Drive link (so plain direct video URLs still work).
    """
    if not url:
        return url

    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)

    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    return url


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
        "video_url": "",
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
        "video_url": rooms[room_code]["video_url"],
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

    direct_url = drive_share_link_to_direct(url)
    rooms[room_code]["video_url"] = direct_url
    rooms[room_code]["state"] = "paused"
    rooms[room_code]["time"] = 0.0
    rooms[room_code]["updated_at"] = time.time()

    emit("video_set", {"video_url": direct_url}, room=room_code)


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


@socketio.on("chat")
def on_chat(data):
    room_code = data.get("room", "").upper()
    name = data.get("name", "Guest")
    msg = data.get("msg", "").strip()
    if not msg or room_code not in rooms:
        return
    emit("chat", {"name": name, "msg": msg}, room=room_code)


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
