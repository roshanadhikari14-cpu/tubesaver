"""Local YouTube downloader: a small Flask server wrapping yt-dlp."""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
import uuid
import webbrowser

from flask import Flask, jsonify, request, send_from_directory
import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_FOLDER = r"D:\YouTube" if os.path.exists("D:\\") else os.path.join(os.path.expanduser("~"), "YouTube")
YOUTUBE_RE = re.compile(r"^https?://([a-z0-9-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)

app = Flask(__name__, static_folder="static")
jobs = {}
jobs_lock = threading.Lock()


# ---------- helpers ----------

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def download_folder():
    folder = load_config().get("folder") or DEFAULT_FOLDER
    os.makedirs(folder, exist_ok=True)
    return folder


def find_ffmpeg():
    """Locate ffmpeg even if PATH was updated after this process started."""
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for pattern in patterns:
        hits = glob.glob(pattern)
        if hits:
            return os.path.dirname(hits[0])
    return None


FFMPEG_DIR = find_ffmpeg()


def valid_url(url):
    return bool(url) and bool(YOUTUBE_RE.match(url.strip()))


def base_opts():
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


def size_of(fmt):
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def build_options(info):
    """Collapse yt-dlp's raw format list into one choice per resolution."""
    formats = info.get("formats") or []
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audios, key=lambda f: f.get("abr") or 0, default=None)
    audio_size = size_of(best_audio) if best_audio else 0

    by_height = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        current = by_height.get(h)
        # prefer higher fps, then mp4/avc for compatibility, then bitrate
        score = (f.get("fps") or 0, f.get("ext") == "mp4", f.get("tbr") or 0)
        if current is None or score > current[0]:
            by_height[h] = (score, f)

    video = []
    for h in sorted(by_height, reverse=True):
        f = by_height[h][1]
        has_audio = f.get("acodec") not in (None, "none")
        video.append({
            "id": f"v{h}",
            "kind": "video",
            "height": h,
            "label": f"{h}p",
            "badge": "8K" if h >= 4320 else "4K" if h >= 2160 else "2K" if h >= 1440 else "HD" if h >= 720 else "SD",
            "fps": int(f.get("fps") or 0),
            "codec": (f.get("vcodec") or "").split(".")[0],
            "size": size_of(f) + (0 if has_audio else audio_size),
        })

    audio = []
    if best_audio:
        audio.append({"id": "mp3", "kind": "audio", "label": "MP3", "badge": "AUDIO",
                      "detail": "192 kbps", "size": audio_size})
        audio.append({"id": "m4a", "kind": "audio", "label": "M4A", "badge": "AUDIO",
                      "detail": f"{int(best_audio.get('abr') or 0)} kbps original", "size": audio_size})
    audio.append({"id": "thumb", "kind": "image", "label": "Thumbnail", "badge": "JPG",
                  "detail": "Highest quality", "size": 0})
    return video, audio


# ---------- routes ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/quick")
def quick():
    """Fast oEmbed lookup so title/channel appear before the full fetch finishes."""
    url = request.args.get("url", "")
    if not valid_url(url):
        return jsonify(error="Please paste a valid YouTube link."), 400
    try:
        endpoint = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(endpoint, timeout=8) as r:
            data = json.load(r)
        return jsonify(title=data.get("title"), channel=data.get("author_name"))
    except Exception:
        return jsonify(title=None, channel=None)


@app.get("/api/info")
def info():
    url = request.args.get("url", "")
    if not valid_url(url):
        return jsonify(error="Please paste a valid YouTube link."), 400
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        return jsonify(error=msg), 400

    video, audio = build_options(data)
    return jsonify(
        id=data.get("id"),
        title=data.get("title"),
        channel=data.get("channel") or data.get("uploader"),
        thumbnail=data.get("thumbnail"),
        duration=data.get("duration"),
        views=data.get("view_count"),
        likes=data.get("like_count"),
        upload_date=data.get("upload_date"),
        video=video,
        audio=audio,
        ffmpeg=bool(FFMPEG_DIR),
    )


def run_job(job_id, url, choice):
    job = jobs[job_id]
    folder = download_folder()
    opts = base_opts()
    opts["outtmpl"] = os.path.join(folder, "%(title).150B [%(id)s].%(ext)s")
    opts["windowsfilenames"] = True

    def on_progress(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job.update(status="downloading",
                       percent=round(done / total * 100, 1) if total else job.get("percent", 0),
                       speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            job["stage"] += 1
            job["filename"] = d.get("filename")

    def on_post(d):
        if d["status"] == "started":
            job.update(status="processing", percent=100)
        elif d["status"] == "finished" and d.get("info_dict", {}).get("filepath"):
            job["filename"] = d["info_dict"]["filepath"]

    opts["progress_hooks"] = [on_progress]
    opts["postprocessor_hooks"] = [on_post]

    if choice.startswith("v") and choice[1:].isdigit():
        h = int(choice[1:])
        if FFMPEG_DIR:
            opts["format"] = (f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/"
                              f"bestvideo[height={h}]+bestaudio/best[height={h}]")
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = f"best[height={h}][acodec!=none]/best[height<={h}][acodec!=none]"
    elif choice == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif choice == "m4a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio"
    elif choice == "thumb":
        opts.update(skip_download=True, writethumbnail=True)
        if FFMPEG_DIR:
            opts["postprocessors"] = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}]
    else:
        job.update(status="error", error="Unknown format.")
        return

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=True)
            if choice == "thumb":
                thumbs = [t.get("filepath") for t in data.get("thumbnails", []) if t.get("filepath")]
                job["filename"] = thumbs[-1] if thumbs else None
            elif data.get("requested_downloads"):
                job["filename"] = data["requested_downloads"][-1].get("filepath") or job.get("filename")
        job.update(status="done", percent=100, folder=folder)
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        job.update(status="error", error=msg)


@app.post("/api/download")
def download():
    body = request.get_json(silent=True) or {}
    url, choice = body.get("url", ""), body.get("format", "")
    if not valid_url(url):
        return jsonify(error="Please paste a valid YouTube link."), 400
    if choice in ("mp3",) and not FFMPEG_DIR:
        return jsonify(error="MP3 needs FFmpeg. Install it and restart the app."), 400
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status": "starting", "percent": 0, "stage": 0, "filename": None}
    threading.Thread(target=run_job, args=(job_id, url, choice), daemon=True).start()
    return jsonify(job=job_id)


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job."), 404
    out = dict(job)
    if out.get("filename"):
        out["name"] = os.path.basename(out["filename"])
    return jsonify(out)


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        folder = ((request.get_json(silent=True) or {}).get("folder") or "").strip()
        if not folder:
            return jsonify(error="Folder can't be empty."), 400
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            return jsonify(error=f"Can't use that folder: {e.strerror}"), 400
        cfg = load_config()
        cfg["folder"] = folder
        save_config(cfg)
    return jsonify(folder=download_folder(), ffmpeg=bool(FFMPEG_DIR))


@app.post("/api/open")
def open_folder():
    body = request.get_json(silent=True) or {}
    target = body.get("file")
    folder = download_folder()
    # only ever reveal paths inside the download folder
    if target and os.path.abspath(target).startswith(os.path.abspath(folder)) and os.path.exists(target):
        subprocess.Popen(["explorer", "/select,", os.path.abspath(target)])
    else:
        os.startfile(folder)
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    print(f"\n  YouTube Downloader running at http://127.0.0.1:{port}")
    print(f"  Saving to: {download_folder()}")
    print(f"  FFmpeg: {'found' if FFMPEG_DIR else 'NOT FOUND (HD merging and MP3 disabled)'}\n")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False)
