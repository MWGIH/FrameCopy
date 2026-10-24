#!/usr/bin/env python3
"""
SCopy - a small web page that lets any browser on the same network
play, download and delete the Steam Frame's recordings, screenshots and videos.

Python standard library only. The SCopy dashboard tab turns it on and off:

    server = ShareServer("~/Videos/SFRecorder")
    server.start()
    print(server.urls, server.pin)   # shown in the headset
    ...
    server.stop()

It can also be run by hand from Konsole for testing:

    python3 framecopy.py --dir ~/Videos/SFRecorder

Recordings in the folder and in its subfolders one level down (such as clips/)
are listed. Files still being written end in .part and are left out until the
recorder renames them. Unless --only-dir is given, the usual SteamOS media
folders are listed too: Steam's screenshots (named by game), finished files in
Steam Game Recording's folder, ~/Videos, ~/Pictures and framecorder's folder.
Steam's own folders are read-only here; files can be deleted from the others.

Safety: it only listens while sharing is on, asks for a 4-digit PIN once per
browser, only serves the video and image files it lists, and turns itself
off after IDLE_MINUTES with no requests.
"""

import argparse
import glob
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # steam_recordings.py sits next to this file
from steam_recordings import DashMovie, UnsupportedRecording, find_sessions  # noqa: E402

DEFAULT_DIR = "~/Videos/SFRecorder"
FRAMECORDER_DIR = "~/Videos/framecorder"
DEFAULT_PORT = 8642
IDLE_MINUTES = 30
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
STEAM_SCREENSHOTS_APP = "760"   # Steam keeps screenshots under userdata/<user>/760/remote/<game>/screenshots
LIST_CACHE_SECONDS = 3.0
# A Steam recording whose chunks changed this recently is still being recorded.
STEAM_SETTLE_SECONDS = 30.0
# Images up to this size are their own thumbnail; bigger ones get a placeholder.
MAX_IMAGE_THUMB_BYTES = 8 * 1024 * 1024
# A file written to within this many seconds is treated as still recording.
SETTLE_SECONDS = 5.0
MAX_FAILED_PINS = 5
LOCKOUT_SECONDS = 30.0
COPY_CHUNK = 1024 * 1024
COOKIE_NAME = "frame_share"
# Long-lived cookie for a computer that ticked "Remember this computer".
DEVICE_COOKIE = "framecopy_device"
DEVICE_COOKIE_SECONDS = 365 * 24 * 3600
DEVICES_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                            "framecopy", "devices.json")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                         "framecopy", "thumbs")
MIME_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
              ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp"}


# Where log lines go; --json moves them to stderr so stdout carries only the JSON line.
LOG_STREAM = sys.stdout


def log(message):
    print(message, file=LOG_STREAM, flush=True)


def lan_ip():
    """The address other devices on the Wi-Fi reach us at (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def steam_dir():
    """Steam's data folder: ~/.steam/steam points at it on SteamOS."""
    for candidate in ("~/.steam/steam", "~/.local/share/Steam"):
        path = os.path.realpath(os.path.expanduser(candidate))
        if os.path.isdir(os.path.join(path, "userdata")):
            return path
    return None


def steam_game_names(steam):
    """App id -> game name, from the installed games' manifests."""
    names = {}
    for manifest in glob.glob(os.path.join(steam, "steamapps", "appmanifest_*.acf")):
        try:
            with open(manifest, errors="replace") as f:
                text = f.read(4096)
        except OSError:
            continue
        app_id = re.search(r'"appid"\s+"(\d+)"', text)
        name = re.search(r'"name"\s+"([^"]*)"', text)
        if app_id and name:
            names[app_id.group(1)] = name.group(1)
    return names


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024


class ShareServer:
    def __init__(self, recordings_dir=DEFAULT_DIR, port=DEFAULT_PORT, idle_minutes=IDLE_MINUTES,
                 on_stop=None, steamos_folders=True, devices_file=DEVICES_FILE):
        self.recordings_dir = os.path.realpath(os.path.expanduser(recordings_dir))
        # Also share Steam's screenshots and exported clips, ~/Videos and ~/Pictures.
        self.steamos_folders = steamos_folders
        self.port = port
        self.idle_seconds = int(idle_minutes * 60)
        # Called with no arguments when sharing stops by itself (idle timeout).
        self.on_stop = on_stop
        self.pin = ""
        self._sessions = set()
        # Remembered computers survive restarts; only hashes of their tokens are kept on disk.
        self.devices_file = devices_file
        self._devices_lock = threading.Lock()
        self._failed = {}       # client ip -> (failures, locked_until)
        self._last_request = 0.0
        self._httpd = None
        self._thumb_lock = threading.Lock()
        self._lock = threading.Lock()
        self._list_lock = threading.Lock()
        self._movies = {}       # Steam recording session folder -> DashMovie
        self._listing = ([], {}, 0.0)   # (files, name -> (path, file), monotonic time)

    @property
    def active(self):
        return self._httpd is not None

    @property
    def urls(self):
        """Addresses to show in the headset, the friendly name first."""
        return [f"http://{socket.gethostname()}.local:{self.port}", f"http://{lan_ip()}:{self.port}"]

    def start(self):
        with self._lock:
            if self._httpd:
                return
            os.makedirs(self.recordings_dir, exist_ok=True)
            self.pin = f"{secrets.randbelow(10000):04d}"
            self._sessions.clear()
            self._failed.clear()
            self._last_request = time.monotonic()
            handler = type("Handler", (_Handler,), {"share": self})
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
            self._httpd.daemon_threads = True
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
            threading.Thread(target=self._watch_idle, args=(self._httpd,), daemon=True).start()
        log(f"Sharing {self.recordings_dir}{' and SteamOS media folders' if self.steamos_folders else ''} at {', '.join(self.urls)} (PIN {self.pin})")

    def stop(self):
        with self._lock:
            httpd, self._httpd = self._httpd, None
            self._sessions.clear()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
            log("Sharing stopped")

    def _watch_idle(self, httpd):
        while self._httpd is httpd:
            time.sleep(10)
            if time.monotonic() - self._last_request > self.idle_seconds and self._httpd is httpd:
                log(f"No requests for {self.idle_seconds // 60} minutes")
                self.stop()
                if self.on_stop:
                    self.on_stop()
                return

    # --- files ---

    def _scan(self, found, source, label, root, depth, deletable, group=None):
        """Adds the finished videos and images under root, `depth` folder levels down, to found."""
        now = time.time()
        root = os.path.realpath(root)
        for folder, dirs, files in os.walk(root):
            rel = os.path.relpath(folder, root)
            level = 0 if rel == "." else rel.count(os.sep) + 1
            # Hidden folders, folders listed on their own and anything too deep are skipped.
            dirs[:] = [d for d in dirs if not d.startswith(".") and level < depth
                       and os.path.join(folder, d) not in self._own_folders()]
            for file in files:
                lower = file.lower()
                kind = "video" if lower.endswith(VIDEO_EXTENSIONS) else "image" if lower.endswith(IMAGE_EXTENSIONS) else None
                if not kind or file.startswith("."):
                    continue
                path = os.path.join(folder, file)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or os.path.islink(path) or now - st.st_mtime < SETTLE_SECONDS:
                    continue
                name = source + "/" + (file if rel == "." else rel.replace(os.sep, "/") + "/" + file)
                sub = group or ("" if rel == "." else rel.replace(os.sep, "/"))
                found.append((path, {"name": name, "file": file, "source": source, "label": label, "group": sub,
                                     "kind": kind, "deletable": deletable, "size": st.st_size,
                                     "size_text": human_size(st.st_size), "mtime": st.st_mtime}))

    def _own_folders(self):
        """Folders with their own entry, so a scan of ~/Videos doesn't list them twice."""
        own = {self.recordings_dir}
        if self.steamos_folders:
            own.add(os.path.realpath(os.path.expanduser(FRAMECORDER_DIR)))
        return own

    def _list(self):
        found = []
        self._scan(found, "recordings", "Recordings", self.recordings_dir, 1, True)
        if self.steamos_folders:
            steam = steam_dir()
            if steam:
                games = steam_game_names(steam)
                userdata = os.path.join(steam, "userdata")
                # userdata/<user>/760/remote/<app id>/screenshots/<file>; Steam's own thumbnails are skipped.
                pattern = os.path.join(userdata, "*", STEAM_SCREENSHOTS_APP, "remote", "*", "screenshots")
                for folder in sorted(glob.glob(pattern)):
                    app_id = os.path.basename(os.path.dirname(folder))
                    user = os.path.relpath(folder, userdata).split(os.sep)[0]
                    self._scan(found, f"steam-screenshots/{user}/{app_id}", "Steam screenshots", folder, 0, False,
                               group=games.get(app_id, f"App {app_id}"))
                self._steam_recordings(found, steam, games)
            self._scan(found, "videos", "Videos", os.path.expanduser("~/Videos"), 2, True)
            self._scan(found, "pictures", "Pictures", os.path.expanduser("~/Pictures"), 2, True)
            framecorder = os.path.realpath(os.path.expanduser(FRAMECORDER_DIR))
            if framecorder != self.recordings_dir:
                self._scan(found, "framecorder", "framecorder", framecorder, 1, True)
        found.sort(key=lambda f: f[1]["mtime"], reverse=True)
        return [f for _, f in found], {f["name"]: (path, f) for path, f in found}

    def _steam_recordings(self, found, steam, games):
        """Steam's background recordings and clips, each served as one MP4 joined from its chunks."""
        now = time.time()
        for session in find_sessions(steam):
            if now - session["mtime"] < STEAM_SETTLE_SECONDS:
                continue
            game = games.get(session["app_id"], f"App {session['app_id']}")
            d, t = session["date"], session["time"]
            when = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}.{t[2:4]}.{t[4:]}"
            part = f" part {session['part']}" if session["parts"] > 1 else ""
            file = re.sub(r'[\\/:*?"<>|]', "-", f"{game} {when}{part}.mp4")
            name = f"steam-recordings/{session['user']}/{session['folder']}/{session['part']}.mp4"
            label = "Steam clips" if session["kind"] == "clip" else "Steam recordings"
            found.append((session["session_dir"], {
                "name": name, "file": file, "source": "steam-recordings", "label": label, "group": game,
                "kind": "video", "deletable": False, "virtual": True, "size": session["size"],
                "size_text": human_size(session["size"]), "mtime": session["mtime"]}))

    def open_range(self, name):
        """(info, size, read) for a listed file, where read(start, end) yields its bytes; or None."""
        path, info = self.lookup(name)
        if not path:
            return None
        if info.get("virtual"):
            with self._list_lock:
                movie = self._movies.setdefault(path, DashMovie(path))
            try:
                size = movie.prepare()
            except (UnsupportedRecording, OSError, struct.error) as e:
                log(f"Can't join Steam recording {name}: {e}")
                return None
            return info, size, movie.read

        def read(start, end):
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(COPY_CHUNK, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)

        return info, os.path.getsize(path), read

    def _current(self):
        with self._list_lock:
            files, index, when = self._listing
            if time.monotonic() - when > LIST_CACHE_SECONDS:
                files, index = self._list()
                self._listing = (files, index, time.monotonic())
            return files, index

    def files(self):
        """Shared videos and images, newest first. A name is <source>/<path inside it>, e.g. recordings/clips/x.mp4."""
        return self._current()[0]

    recordings = files

    def lookup(self, name):
        """(path, file info) for a listed name, or (None, None). Only listed files are ever served."""
        return self._current()[1].get(name, (None, None))

    def path_for(self, name):
        return self.lookup(name)[0]

    @staticmethod
    def _thumb_prefix(name):
        return name.replace("/", "__") + "."

    def thumbnail(self, name):
        """(bytes, content type) of a small preview, or None (for a video, when ffmpeg isn't available)."""
        path, info = self.lookup(name)
        if not path:
            return None
        if info["kind"] == "image":
            # Steam saves a small copy of each screenshot next to it.
            steam_thumb = os.path.join(os.path.dirname(path), "thumbnails", info["file"])
            for candidate in (steam_thumb, path):
                if os.path.isfile(candidate) and os.path.getsize(candidate) <= MAX_IMAGE_THUMB_BYTES:
                    with open(candidate, "rb") as f:
                        return f.read(), MIME_TYPES.get(os.path.splitext(candidate)[1].lower(), "image/jpeg")
            return None
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        thumb = os.path.join(CACHE_DIR, f"{self._thumb_prefix(name)}{int(info['mtime'])}.jpg")
        with self._thumb_lock:
            if not os.path.exists(thumb):
                os.makedirs(CACHE_DIR, exist_ok=True)
                tmp = thumb + ".tmp.jpg"
                if info.get("virtual"):
                    # The first chunk of a Steam recording is enough for a still.
                    with self._list_lock:
                        movie = self._movies.setdefault(path, DashMovie(path))
                    try:
                        frames = movie.first_video_frames()
                    except (UnsupportedRecording, OSError):
                        return None
                    path = thumb + ".src.mp4"
                    with open(path, "wb") as f:
                        f.write(frames)
                # 1 s in skips a black first frame; fall back to the start for very short clips.
                for seek in ("1", "0"):
                    result = subprocess.run(
                        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-ss", seek, "-i", path,
                         "-frames:v", "1", "-vf", "scale=480:-2", tmp],
                        capture_output=True, timeout=30)
                    if result.returncode == 0 and os.path.exists(tmp):
                        os.replace(tmp, thumb)
                        break
                if info.get("virtual") and os.path.exists(path):
                    os.remove(path)
            if not os.path.exists(thumb):
                return None
        with open(thumb, "rb") as f:
            return f.read(), "image/jpeg"

    def delete(self, name):
        path, info = self.lookup(name)
        # Steam's own folders are read-only here, so Steam's records of them stay right.
        if not path or not info["deletable"]:
            return False
        os.remove(path)
        with self._list_lock:
            self._listing = ([], {}, 0.0)
        for cached in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
            if cached.startswith(self._thumb_prefix(name)):
                os.remove(os.path.join(CACHE_DIR, cached))
        log(f"Deleted {name}")
        return True

    # --- PIN ---

    def check_pin(self, ip, pin):
        """Returns a session token, or None. Locks a client out for a while after repeated misses."""
        failures, locked_until = self._failed.get(ip, (0, 0.0))
        if time.monotonic() < locked_until:
            return None
        if self.pin and hmac.compare_digest(pin.strip(), self.pin):
            self._failed.pop(ip, None)
            token = secrets.token_urlsafe(24)
            self._sessions.add(token)
            return token
        failures += 1
        if failures >= MAX_FAILED_PINS:
            self._failed[ip] = (0, time.monotonic() + LOCKOUT_SECONDS)
        else:
            self._failed[ip] = (failures, 0.0)
        return None

    def locked_out(self, ip):
        return time.monotonic() < self._failed.get(ip, (0, 0.0))[1]

    # --- remembered computers ---

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _load_devices(self):
        try:
            with open(self.devices_file) as f:
                devices = json.load(f).get("devices", [])
            return [d for d in devices if isinstance(d, dict) and isinstance(d.get("hash"), str)]
        except (OSError, ValueError, AttributeError):
            return []

    def _save_devices(self, devices):
        os.makedirs(os.path.dirname(self.devices_file), exist_ok=True)
        tmp = self.devices_file + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"devices": devices}, f, indent=1)
        os.replace(tmp, self.devices_file)

    def remember_device(self, name):
        """Returns a new long-lived token for a computer that just entered the right PIN."""
        token = secrets.token_urlsafe(32)
        with self._devices_lock:
            devices = self._load_devices()
            devices.append({"hash": self._hash(token), "name": name[:120], "added": int(time.time())})
            self._save_devices(devices)
        log(f"Remembering a computer ({name[:60]})")
        return token

    def device_known(self, token):
        if not token:
            return False
        wanted = self._hash(token)
        with self._devices_lock:
            return any(hmac.compare_digest(d["hash"], wanted) for d in self._load_devices())

    def forget_device(self, token):
        wanted = self._hash(token)
        with self._devices_lock:
            self._save_devices([d for d in self._load_devices() if d["hash"] != wanted])

    def forget_all_devices(self):
        with self._devices_lock:
            self._save_devices([])
        log("Forgot all remembered computers")

    def device_count(self):
        with self._devices_lock:
            return len(self._load_devices())


class _Handler(BaseHTTPRequestHandler):
    share = None    # set per server in ShareServer.start()
    protocol_version = "HTTP/1.1"
    server_version = "FrameShare"

    def log_message(self, fmt, *args):
        pass

    # --- plumbing ---

    def _cookie(self, name):
        # Split by hand: SimpleCookie gives up on the rest of the header at the first cookie it
        # can't parse, and other apps on the headset share this host's cookies (ports don't count).
        for part in self.headers.get("Cookie", "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name:
                return value.strip().strip('"')
        return ""

    def _authed(self):
        return (self._cookie(COOKIE_NAME) in self.share._sessions
                or self.share.device_known(self._cookie(DEVICE_COOKIE)))

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # A list of pairs allows a header twice (two Set-Cookie lines).
        for key, value in headers.items() if isinstance(headers, dict) else headers or []:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data), "application/json")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024:
            # Too big to be ours: drop the connection rather than leave the rest unread.
            self.close_connection = True
        return self.rfile.read(min(length, 64 * 1024)) if length > 0 else b""

    def _route(self):
        self.share._last_request = time.monotonic()
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        parts = parsed.path.split("/", 2)
        return parts[1] if len(parts) > 1 else "", urllib.parse.unquote(parts[2]) if len(parts) > 2 else "", query

    # --- GET / HEAD ---

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        section, name, query = self._route()
        if not self._authed():
            if section == "":
                return self._send(200, PIN_PAGE.replace("{error}", ""), "text/html; charset=utf-8")
            return self._send(401, "Enter the PIN first")
        if section == "":
            return self._send(200, MAIN_PAGE, "text/html; charset=utf-8")
        if section == "api" and name == "files":
            return self._json({"files": self.share.files()})
        if section == "files":
            return self._send_file(name, download="download" in query)
        if section == "thumb":
            thumb = self.share.thumbnail(name)
            if thumb is None:
                return self._send(404, "No thumbnail")
            return self._send(200, thumb[0], thumb[1], {"Cache-Control": "max-age=86400"})
        if section == "zip":
            return self._send_zip(query.get("name", []))
        return self._send(404, "Not found")

    def _send_file(self, name, download):
        opened = self.share.open_range(name)
        if not opened:
            return self._send(404, "Not found")
        info, size, read = opened
        start, end = 0, size - 1
        status = 200
        byte_range = self.headers.get("Range", "")
        if byte_range.startswith("bytes="):
            first, _, last = byte_range[6:].split(",")[0].strip().partition("-")
            try:
                if first:
                    start, end = int(first), int(last) if last else size - 1
                else:
                    start = max(0, size - int(last))
                end = min(end, size - 1)
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                return self._send(416, "", headers={"Content-Range": f"bytes */{size}"})
            status = 206
        disposition = "attachment" if download else "inline"
        self.send_response(status)
        self.send_header("Content-Type", MIME_TYPES.get(os.path.splitext(info["file"])[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{urllib.parse.quote(info['file'])}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            for chunk in read(start, end):
                self.wfile.write(chunk)
                self.share._last_request = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (UnsupportedRecording, OSError) as e:
            # Headers are sent, so all that's left is to drop the connection.
            log(f"Stopped sending {name}: {e}")
            self.close_connection = True

    def _send_zip(self, names):
        opened = [self.share.open_range(n) for n in names]
        if not opened or any(o is None for o in opened):
            return self._send(404, "Not found")
        # Inside the zip, files are sorted into readable folders: "Steam screenshots/Portal 2/x.jpg".
        entries, used = [], set()
        for info, size, read in opened:
            stem, ext = os.path.splitext("/".join(p for p in (info["label"], info["group"], info["file"]) if p))
            arcname, n = stem + ext, 2
            while arcname in used:
                arcname, n = f"{stem}-{n}{ext}", n + 1
            used.add(arcname)
            entries.append((arcname, info, size, read))
        # Video doesn't compress further, so files are stored as-is and streamed; the size isn't
        # known up front, so the connection closes at the end instead.
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename=\"scopy-{time.strftime('%Y%m%d-%H%M')}.zip\"")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            with zipfile.ZipFile(_TouchingWriter(self.wfile, self.share), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for arcname, info, size, read in entries:
                    zinfo = zipfile.ZipInfo(arcname, time.localtime(max(info["mtime"], 315532800))[:6])
                    zinfo.file_size = size
                    with zf.open(zinfo, "w", force_zip64=size > 0x7FFFFFFF) as out:
                        for chunk in read(0, size - 1):
                            out.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (UnsupportedRecording, OSError) as e:
            log(f"Stopped sending the zip: {e}")

    # --- POST ---

    def do_POST(self):
        section, name, _ = self._route()
        ip = self.client_address[0]
        # Read the body before any reply, so a kept-alive connection never sees it as the next request.
        body = self._read_body()
        if section == "login":
            form = urllib.parse.parse_qs(body.decode(errors="replace"))
            token = self.share.check_pin(ip, form.get("pin", [""])[0])
            if token:
                headers = [("Location", "/"),
                           ("Set-Cookie", f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict")]
                if form.get("remember"):
                    # Lax, not Strict: the browser still sends it when the page is opened from a link or bookmark.
                    device = self.share.remember_device(self.headers.get("User-Agent", "Unknown browser"))
                    headers.append(("Set-Cookie", f"{DEVICE_COOKIE}={device}; Path=/; HttpOnly; SameSite=Lax; "
                                                  f"Max-Age={DEVICE_COOKIE_SECONDS}"))
                return self._send(303, "", headers=headers)
            error = ("Too many wrong PINs. Wait 30 seconds and try again." if self.share.locked_out(ip)
                     else "Wrong PIN. Check the number shown in the headset.")
            page = PIN_PAGE.replace("{error}", f'<p class="error">{html.escape(error)}</p>')
            return self._send(403, page, "text/html; charset=utf-8")
        if not self._authed():
            return self._send(401, "Enter the PIN first")
        # Same-origin JSON only: a form on another site can't send this content type without CORS.
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, "Expected JSON")
        if section == "api" and name == "delete":
            try:
                names = json.loads(body or b"{}").get("names", [])
            except (ValueError, AttributeError):
                return self._send(400, "Bad request")
            deleted = [n for n in names if isinstance(n, str) and self.share.delete(n)]
            return self._json({"deleted": deleted})
        if section == "api" and name == "forget":
            device = self._cookie(DEVICE_COOKIE)
            if device:
                self.share.forget_device(device)
            return self._send(200, "", headers=[
                ("Set-Cookie", f"{DEVICE_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
                ("Set-Cookie", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")])
        return self._send(404, "Not found")


class _TouchingWriter:
    """Write-only stream for zipfile that keeps the idle timer fresh during long downloads."""

    def __init__(self, out, share):
        self.out, self.share = out, share

    def write(self, data):
        self.share._last_request = time.monotonic()
        return self.out.write(data)

    def flush(self):
        self.out.flush()


STYLE = """
/* Gaming On Steam colours: black bar, navy page, blue logo, cyan accent, navy-to-purple cards. */
:root { color-scheme: dark; --bar:#000000; --bg:#08111f; --card:#0b1628; --card-end:#241a4a; --raised:#1a2d4d; --line:#22385c; --text:#ffffff; --muted:#9fb3c8; --brand:#189dff; --accent:#03bfff; --on-accent:#001526; --danger:#ff4d5e; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.4 Rubik, system-ui, sans-serif; background:var(--bg); color:var(--text); accent-color:var(--accent); }
header { position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:12px 16px; background:var(--bar); }
h1 { font-size:20px; font-weight:700; letter-spacing:.5px; color:var(--brand); margin:0 auto 0 0; }
button, .button { font:inherit; font-weight:500; padding:8px 14px; border-radius:8px; border:1px solid transparent; background:var(--raised); color:var(--text); cursor:pointer; text-decoration:none; }
button:not(:disabled):hover, .button:hover { border-color:var(--brand); }
button.primary { background:var(--accent); border-color:var(--accent); color:var(--on-accent); }
button.danger { color:var(--danger); }
button:disabled { opacity:.4; cursor:default; }
main { padding:16px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); gap:14px; }
.card { background:linear-gradient(180deg, var(--card) 40%, var(--card-end)); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
.card.selected { outline:2px solid var(--accent); }
.thumb { position:relative; aspect-ratio:16/9; background:#000 center/cover no-repeat; cursor:pointer; display:flex; align-items:center; justify-content:center; color:var(--muted); }
.thumb input { position:absolute; top:8px; left:8px; width:20px; height:20px; }
.info { padding:10px 12px; }
.name { font-weight:600; word-break:break-all; }
.meta { color:var(--muted); font-size:13px; margin:2px 0 8px; }
.actions { display:flex; gap:6px; }
.actions > * { flex:1; text-align:center; padding:6px; font-size:14px; }
.empty { color:var(--muted); text-align:center; padding:60px 16px; }
dialog { padding:0; border:0; background:#000; max-width:96vw; }
dialog video, dialog img { display:block; max-width:96vw; max-height:85vh; }
dialog [hidden] { display:none; }
.filters { flex-basis:100%; display:flex; flex-wrap:wrap; gap:6px; }
.filters:empty { display:none; }
.chip { padding:5px 14px; border-radius:999px; font-size:14px; color:var(--muted); }
.chip.on { background:var(--accent); border-color:var(--accent); color:var(--on-accent); }
dialog::backdrop { background:rgba(0,0,0,.8); }
.pin { max-width:320px; margin:12vh auto; padding:0 16px; text-align:center; }
.pin h1 { margin:0; font-size:26px; }
.pin input { font-size:32px; letter-spacing:12px; width:100%; text-align:center; padding:10px; border-radius:10px; border:1px solid var(--line); background:var(--card); color:var(--text); margin:16px 0; }
.pin input:focus { outline:none; border-color:var(--accent); }
.error { color:var(--danger); }
.pin .remember { display:flex; gap:8px; align-items:center; justify-content:center; margin:0 0 16px; color:var(--muted); }
.pin .remember input { width:18px; height:18px; margin:0; }
"""

PIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>SCopy</title>
<style>""" + STYLE + """</style></head><body>
<form class="pin" method="post" action="/login">
<h1>SCopy</h1>
<p>Enter the PIN shown in the headset.</p>
{error}
<input name="pin" inputmode="numeric" autocomplete="off" maxlength="4" autofocus required>
<label class="remember"><input type="checkbox" name="remember" checked> Remember this computer</label>
<button class="primary" type="submit">Open</button>
</form></body></html>"""

MAIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>SCopy</title>
<style>""" + STYLE + """</style></head><body>
<header>
<h1>SCopy</h1>
<button id="all">Select all</button>
<button id="zip" class="primary" disabled>Download selected</button>
<button id="del" class="danger" disabled>Delete selected</button>
<button id="refresh">Refresh</button>
<button id="forget" title="Ask for the PIN again next time on this computer">Forget this computer</button>
<nav id="filters" class="filters"></nav>
</header>
<main><div id="grid" class="grid"></div><p id="empty" class="empty" hidden>Nothing here yet.</p></main>
<dialog id="player"><video id="video" controls autoplay hidden></video><img id="image" alt="" hidden></dialog>
<script>
const grid = document.getElementById("grid"), selected = new Set();
const enc = encodeURIComponent;
let files = [], filter = "All";
function esc(s) { return s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function byName(n) { return files.find(f => f.name === n); }
function deletable() { return [...selected].filter(n => byName(n)?.deletable); }
function visible() { return filter === "All" ? files : files.filter(f => f.label === filter); }
function updateButtons() {
  document.getElementById("zip").disabled = !selected.size;
  document.getElementById("del").disabled = !deletable().length;
  document.getElementById("zip").textContent = selected.size ? `Download selected (${selected.size})` : "Download selected";
}
function render() {
  const order = ["Recordings", "Steam screenshots", "Steam recordings", "Steam clips", "Videos", "Pictures", "framecorder"];
  const labels = [...new Set(files.map(f => f.label))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
  if (filter !== "All" && !labels.includes(filter)) filter = "All";
  document.getElementById("filters").innerHTML = labels.length > 1 ? ["All", ...labels].map(l => {
    const count = l === "All" ? files.length : files.filter(f => f.label === l).length;
    return `<button class="chip${l === filter ? " on" : ""}" data-filter="${esc(l)}">${esc(l)} (${count})</button>`;
  }).join("") : "";
  const shown = visible();
  document.getElementById("empty").hidden = shown.length > 0;
  grid.innerHTML = shown.map(f => `
    <div class="card${selected.has(f.name) ? " selected" : ""}" data-name="${esc(f.name)}">
      <div class="thumb" style="background-image:url('/thumb/${enc(f.name)}')">
        <input type="checkbox" ${selected.has(f.name) ? "checked" : ""} aria-label="Select">${f.kind === "video" ? "&#9654;" : ""}</div>
      <div class="info"><div class="name">${esc(f.file)}</div>
        <div class="meta">${esc(f.label)}${f.group ? " &middot; " + esc(f.group) : ""}<br>${new Date(f.mtime * 1000).toLocaleString()} &middot; ${f.size_text}</div>
        <div class="actions"><a class="button" href="/files/${enc(f.name)}?download">Download</a>
          ${f.deletable ? '<button class="danger" data-delete>Delete</button>' : ""}</div></div>
    </div>`).join("");
  updateButtons();
}
async function load() {
  const res = await fetch("/api/files");
  if (res.status === 401) return location.reload();
  files = (await res.json()).files;
  for (const n of [...selected]) if (!byName(n)) selected.delete(n);
  render();
}
async function remove(names) {
  const skipped = names.length - names.filter(n => byName(n)?.deletable).length;
  names = names.filter(n => byName(n)?.deletable);
  if (!names.length) return;
  let question = names.length === 1 ? `Delete ${byName(names[0]).file}?` : `Delete ${names.length} files?`;
  if (skipped) question += `\n${skipped} from Steam's folders will be kept: delete those in Steam.`;
  if (!confirm(question)) return;
  await fetch("/api/delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({names})});
  names.forEach(n => selected.delete(n));
  load();
}
document.getElementById("filters").addEventListener("click", e => {
  const chip = e.target.closest("[data-filter]"); if (!chip) return;
  filter = chip.dataset.filter; render();
});
grid.addEventListener("click", e => {
  const card = e.target.closest(".card"); if (!card) return;
  const name = card.dataset.name;
  if (e.target.matches("input[type=checkbox]")) {
    e.target.checked ? selected.add(name) : selected.delete(name);
    card.classList.toggle("selected", e.target.checked); updateButtons();
  } else if (e.target.matches("[data-delete]")) {
    remove([name]);
  } else if (e.target.closest(".thumb")) {
    const isVideo = byName(name).kind === "video";
    const video = document.getElementById("video"), image = document.getElementById("image");
    video.hidden = !isVideo; image.hidden = isVideo;
    (isVideo ? video : image).src = "/files/" + enc(name);
    document.getElementById("player").showModal();
  }
});
document.getElementById("player").addEventListener("click", e => {
  if (e.target.id === "player" || e.target.id === "image") { document.getElementById("player").close(); }
});
document.getElementById("player").addEventListener("close", () => {
  const video = document.getElementById("video"); video.pause(); video.removeAttribute("src"); video.load();
  document.getElementById("image").removeAttribute("src");
});
document.getElementById("all").onclick = () => {
  const shown = visible();
  const all = shown.length && shown.every(f => selected.has(f.name));
  shown.forEach(f => all ? selected.delete(f.name) : selected.add(f.name));
  render();
};
document.getElementById("zip").onclick = () => {
  if (selected.size === 1) location.href = "/files/" + enc([...selected][0]) + "?download";
  else location.href = "/zip?" + [...selected].map(n => "name=" + enc(n)).join("&");
};
document.getElementById("del").onclick = () => remove([...selected]);
document.getElementById("refresh").onclick = load;
document.getElementById("forget").onclick = async () => {
  if (!confirm("Forget this computer? You'll need the PIN from the headset next time.")) return;
  await fetch("/api/forget", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  location.reload();
};
load();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Share recordings, screenshots and videos with browsers on the same network.")
    parser.add_argument("--dir", default=DEFAULT_DIR, help=f"recordings folder (default {DEFAULT_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--only-dir", action="store_true",
                        help="share only --dir, not Steam's screenshots and clips, ~/Videos and ~/Pictures")
    parser.add_argument("--json", action="store_true",
                        help="for apps that run this as a child process: print one JSON line "
                             "({urls, pin, port}) once listening, log to stderr, exit 0 when sharing stops")
    args = parser.parse_args()
    global LOG_STREAM
    if args.json:
        LOG_STREAM = sys.stderr
    stopped = threading.Event()
    server = ShareServer(args.dir, args.port, on_stop=stopped.set, steamos_folders=not args.only_dir)
    try:
        server.start()
    except OSError as e:
        print(f"Can't share on port {args.port}: {e.strerror or e}", file=sys.stderr, flush=True)
        sys.exit(1)
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if args.json:
        print(json.dumps({"urls": server.urls, "pin": server.pin, "port": server.port}), flush=True)
    else:
        print(f"\nOpen one of these on your PC:\n  " + "\n  ".join(server.urls) + f"\nPIN: {server.pin}\n"
              "Press Ctrl+C to stop.", flush=True)
    try:
        while not stopped.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    server.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
