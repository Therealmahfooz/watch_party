import os
import re
import random
import string
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
from authlib.integrations.flask_client import OAuth

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-to-something-random-in-production")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

FREE_TRIAL_MINUTES = 10
RENAME_COOLDOWN_HOURS = 24

oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---------------- Database (Postgres / Supabase) ----------------
# Uses the free Supabase Postgres database via DATABASE_URL so user data
# survives restarts/redeploys, unlike storing it on Render's local disk.


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    if not DATABASE_URL:
        # Not configured yet — video/room features still work fine,
        # login just won't until DATABASE_URL is set.
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            google_id TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login TEXT NOT NULL,
            last_renamed_at TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


init_db()


def get_user_by_id(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def upsert_user(google_id, email, name):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE users SET last_login = %s WHERE google_id = %s", (now, google_id))
        conn.commit()
        user_id = existing["id"]
    else:
        cur.execute(
            "INSERT INTO users (google_id, email, name, created_at, last_login) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (google_id, email, name, now, now),
        )
        user_id = cur.fetchone()["id"]
        conn.commit()
    cur.close()
    conn.close()
    return get_user_by_id(user_id)



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


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)


def trial_expired():
    """True if a logged-out visitor has used up their 10 free minutes."""
    if current_user():
        return False  # logged-in users have unlimited access
    started = session.get("guest_started_at")
    if not started:
        session["guest_started_at"] = time.time()
        return False
    return (time.time() - started) > FREE_TRIAL_MINUTES * 60


def trial_seconds_left():
    started = session.get("guest_started_at")
    if not started:
        return FREE_TRIAL_MINUTES * 60
    remaining = FREE_TRIAL_MINUTES * 60 - (time.time() - started)
    return max(0, int(remaining))


@app.route("/", methods=["GET"])
def index():
    user = current_user()
    if trial_expired():
        return render_template("index.html", trial_expired=True, user=user)
    return render_template("index.html", user=user, trial_seconds_left=trial_seconds_left())


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google_oauth.authorize_access_token()
    userinfo = token.get("userinfo") or google_oauth.parse_id_token(token)
    google_id = userinfo["sub"]
    email = userinfo.get("email", "")
    name = userinfo.get("name") or email.split("@")[0]

    user = upsert_user(google_id, email, name)
    session["user_id"] = user["id"]
    session.pop("guest_started_at", None)
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))


@app.route("/account/rename", methods=["POST"])
def account_rename():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    new_name = request.form.get("name", "").strip()[:40]
    if not new_name:
        return jsonify({"ok": False, "error": "Name can't be empty"}), 400

    if user["last_renamed_at"]:
        last = datetime.fromisoformat(user["last_renamed_at"])
        if datetime.now(timezone.utc) - last < timedelta(hours=RENAME_COOLDOWN_HOURS):
            wait = timedelta(hours=RENAME_COOLDOWN_HOURS) - (datetime.now(timezone.utc) - last)
            hours_left = int(wait.total_seconds() // 3600) + 1
            return jsonify({"ok": False, "error": f"You can rename again in about {hours_left}h"}), 429

    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET name = %s, last_renamed_at = %s WHERE id = %s", (new_name, now, user["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "name": new_name})


@app.route("/admin")
def admin_dashboard():
    if not ADMIN_PASSWORD:
        return "Admin dashboard isn't set up — add an ADMIN_PASSWORD environment variable.", 503
    if request.args.get("password") != ADMIN_PASSWORD:
        return """
            <body style="background:#000;color:#fff;font-family:sans-serif;display:flex;
            align-items:center;justify-content:center;height:100vh;">
            <form method="get" style="text-align:center;">
              <input type="password" name="password" placeholder="Admin password"
                     style="padding:10px;border-radius:6px;border:1px solid #444;background:#111;color:#fff;">
              <button style="padding:10px 16px;border-radius:6px;border:none;background:#6C9BCF;
                      color:#000;font-weight:bold;cursor:pointer;">Enter</button>
            </form></body>
        """, 401

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY created_at DESC")
    users = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return render_template("admin.html", users=users, total=len(users))


@app.route("/create", methods=["POST"])
def create_room():
    if trial_expired():
        return redirect(url_for("index"))
    user = current_user()
    name = (user["name"] if user else request.form.get("name", "").strip()) or "Guest"
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
    if trial_expired():
        return redirect(url_for("index"))
    user = current_user()
    name = (user["name"] if user else request.form.get("name", "").strip()) or "Guest"
    code = request.form.get("code", "").strip().upper()
    if code not in rooms:
        return render_template("index.html", error="Room not found. Check the code and try again.", user=user)
    return redirect(url_for("room", code=code, name=name))


@app.route("/room/<code>", methods=["GET"])
def room(code):
    code = code.upper()
    if code not in rooms:
        return redirect(url_for("index"))
    name = request.args.get("name", "Guest")
    return render_template("room.html", code=code, name=name)


@app.route("/api/youtube_search", methods=["GET"])
def youtube_search():
    query = request.args.get("q", "").strip()
    if not query or not YOUTUBE_API_KEY:
        return jsonify({"results": []})

    params = urllib.parse.urlencode({
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 6,
        "key": YOUTUBE_API_KEY,
    })
    url = f"https://www.googleapis.com/youtube/v3/search?{params}"

    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.load(resp)
    except Exception:
        return jsonify({"results": []})

    results = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        if not video_id:
            continue
        results.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "channel": snippet.get("channelTitle", ""),
            "thumbnail": (snippet.get("thumbnails", {}).get("default", {}) or {}).get("url", ""),
        })

    return jsonify({"results": results})


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


@socketio.on("voice_join")
def on_voice_join(data):
    """Announce this user is ready for voice chat, so existing
    voice-enabled peers in the room can start a WebRTC connection to them."""
    room_code = data.get("room", "").upper()
    if room_code not in rooms:
        return
    emit("voice_peer_joined", {"sid": request.sid}, room=room_code, include_self=False)


@socketio.on("voice_offer")
def on_voice_offer(data):
    target = data.get("target")
    if not target:
        return
    emit("voice_offer", {"sdp": data.get("sdp"), "sender": request.sid}, room=target)


@socketio.on("voice_answer")
def on_voice_answer(data):
    target = data.get("target")
    if not target:
        return
    emit("voice_answer", {"sdp": data.get("sdp"), "sender": request.sid}, room=target)


@socketio.on("voice_ice_candidate")
def on_voice_ice_candidate(data):
    target = data.get("target")
    if not target:
        return
    emit("voice_ice_candidate", {"candidate": data.get("candidate"), "sender": request.sid}, room=target)


@socketio.on("disconnect")
def on_disconnect():
    for room_code, r in list(rooms.items()):
        if request.sid in r["users"]:
            name = r["users"].pop(request.sid)
            emit("system_message", {"text": f"{name} left the room"}, room=room_code)
            emit("user_list", {"users": list(r["users"].values())}, room=room_code)
            emit("voice_peer_left", {"sid": request.sid}, room=room_code)


if __name__ == "__main__":
    # Render (and most hosts) provide the port to bind to via the PORT
    # env var. Falls back to 5000 for local development.
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)