import os
import re
import glob
import json
import time
import uuid
import tempfile
import shutil
import threading
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import yt_dlp

app = Flask(__name__)

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Locate ffmpeg — it lives in the Nix store, not on PATH
_ffmpeg_bin = shutil.which('ffmpeg')
if not _ffmpeg_bin:
    _candidates = glob.glob('/nix/store/*ffmpeg*/bin/ffmpeg')
    _ffmpeg_bin = _candidates[0] if _candidates else None
FFMPEG_DIR = os.path.dirname(_ffmpeg_bin) if _ffmpeg_bin else None

# In-memory job store: job_id -> job dict
jobs = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_error(msg):
    msg = ANSI_ESCAPE.sub("", str(msg))
    msg = re.sub(r"^ERROR:\s*", "", msg, flags=re.IGNORECASE).strip()
    msg = re.sub(r"\[.*?\]\s*[\w\-]+:\s*", "", msg, count=1).strip()

    low = msg.lower()
    if "ffmpeg" in low and ("not installed" in low or "not found" in low):
        return "Server configuration error: media processor not found. Please try again later."
    if "login required" in low or "sign in" in low or "authentication" in low:
        return ("Login required. Instagram, Facebook, and Twitter/X block downloads "
                "without an active session. Try a public YouTube or TikTok link instead.")
    if "403" in low or "forbidden" in low:
        return ("Access denied (HTTP 403). This video is geo-restricted, login-protected, "
                "or the site is blocking automated downloads. YouTube and TikTok work best.")
    if "http error 4" in low or "unable to download video data" in low:
        return ("Could not download the video (the host refused the request). "
                "The video may be region-blocked or require a login.")
    if "rate" in low and ("limit" in low or "429" in low):
        return "Rate limit reached. Please wait a few minutes and try again."
    if "private" in low:
        return "This video is private and cannot be downloaded."
    if "not available" in low or "unavailable" in low:
        return "This video is not available (removed, region-blocked, or private)."
    if "unsupported url" in low:
        return "This URL is not supported. Please paste a direct video link from a supported platform."
    if "no video" in low or "no formats" in low:
        return "No downloadable video found at this URL. Make sure you paste a direct video link."
    if "copyright" in low:
        return "This video cannot be downloaded due to copyright restrictions."
    if "unable to extract" in low:
        return "Could not extract video from this page. The site may have changed or the URL may be invalid."
    if "please report" in low:
        return "This site is not fully supported. Try a link from YouTube, TikTok, Facebook, or Vimeo."
    if len(msg) > 260:
        msg = msg[:260] + "..."
    return msg or "Could not fetch video info. Please check the URL and try again."


def base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


def estimate_sizes(formats, heights, duration=0):
    """Return estimated byte sizes keyed by height string and 'mp3'.
    Falls back to tbr-based estimation when filesize metadata is absent."""

    def get_size(f):
        s = f.get('filesize') or f.get('filesize_approx') or 0
        if not s and duration:
            tbr = f.get('tbr') or 0
            if tbr:
                s = int(tbr * 1000 / 8 * duration)
        return s

    video_only = [f for f in formats
                  if f.get('vcodec', 'none') != 'none'
                  and f.get('acodec', 'none') == 'none']
    audio_only = [f for f in formats
                  if f.get('acodec', 'none') != 'none'
                  and f.get('vcodec', 'none') == 'none']
    combined   = [f for f in formats
                  if f.get('vcodec', 'none') != 'none'
                  and f.get('acodec', 'none') != 'none']

    best_audio_size = 0
    if audio_only:
        best_a = max(audio_only, key=lambda f: f.get('abr') or f.get('tbr') or 0)
        best_audio_size = get_size(best_a)

    sizes = {}
    for height in heights:
        h = int(height)
        total = 0
        # Try split video+audio streams first
        vf = [f for f in video_only if 0 < (f.get('height') or 0) <= h]
        if vf:
            best_v = max(vf, key=lambda f: ((f.get('height') or 0), f.get('tbr') or 0))
            v_size = get_size(best_v)
            if v_size > 0:
                total = v_size + (best_audio_size or 0)
        # Fall back to pre-muxed (combined) formats — common on TikTok/Facebook/etc.
        if total == 0:
            cf = [f for f in combined if 0 < (f.get('height') or 0) <= h]
            if cf:
                best_c = max(cf, key=lambda f: ((f.get('height') or 0), f.get('tbr') or 0))
                total = get_size(best_c)
        sizes[str(height)] = total if total > 0 else None

    # MP3 size
    mp3_size = 0
    if audio_only:
        best_a = max(audio_only, key=lambda f: f.get('abr') or f.get('tbr') or 0)
        mp3_size = get_size(best_a)
    elif combined:
        # Some platforms have no audio-only stream; estimate from best combined
        best_c = max(combined, key=lambda f: f.get('abr') or f.get('tbr') or 0)
        abr = best_c.get('abr') or 0
        if abr and duration:
            mp3_size = int(abr * 1000 / 8 * duration)
    sizes['mp3'] = mp3_size if mp3_size > 0 else None
    return sizes


def build_format_string(quality, fmt):
    if fmt == "mp3":
        return "bestaudio/best"
    try:
        h = int(quality)
        return (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}]/best"
        )
    except ValueError:
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"


# ---------------------------------------------------------------------------
# Background download worker
# ---------------------------------------------------------------------------

def run_download(job_id, url, quality, fmt):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    tmp_dir = tempfile.mkdtemp()
    output_base = os.path.join(tmp_dir, "video")

    # Track which stream yt-dlp is on (video=0, audio=1 for merged formats)
    stream_index = [0]

    def progress_hook(d):
        with jobs_lock:
            j = jobs.get(job_id)
        if not j:
            return

        status = d.get('status')

        if status == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                stream_pct = downloaded / total * 100.0
            else:
                raw = d.get('_percent_str', '0%').strip().rstrip('%')
                try:
                    stream_pct = float(raw)
                except ValueError:
                    stream_pct = 0.0

            si = stream_index[0]
            if si == 0:
                # Video stream occupies 0–65 %
                overall = stream_pct * 0.65
                phase = 'Downloading video...'
            else:
                # Audio stream occupies 65–85 %
                overall = 65.0 + stream_pct * 0.20
                phase = 'Downloading audio...'

            with jobs_lock:
                j['progress'] = round(min(overall, 84.9), 1)
                j['phase'] = phase

        elif status == 'finished':
            stream_index[0] += 1
            with jobs_lock:
                if stream_index[0] == 1:
                    j['progress'] = 65.0
                    j['phase'] = 'Downloading audio...'
                else:
                    j['progress'] = 85.0
                    j['phase'] = 'Merging streams...'

    ydl_opts = base_ydl_opts()
    ydl_opts['format'] = build_format_string(quality, fmt)
    ydl_opts['outtmpl'] = output_base + '.%(ext)s'
    ydl_opts['progress_hooks'] = [progress_hook]
    if fmt != 'mp3':
        ydl_opts['merge_output_format'] = 'mp4'
    if fmt == 'mp3':
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    with jobs_lock:
        jobs[job_id]['tmp_dir'] = tmp_dir
        jobs[job_id]['phase'] = 'Connecting...'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video') if info else 'video'

        downloaded_files = [f for f in os.listdir(tmp_dir) if f.startswith('video')]
        if not downloaded_files:
            raise RuntimeError('Download failed — no file was produced.')

        file_path = os.path.join(tmp_dir, downloaded_files[0])
        ext = downloaded_files[0].rsplit('.', 1)[-1]
        safe_title = ''.join(
            c for c in title if c.isascii() and (c.isalnum() or c in ' -_')
        )[:60].strip()
        download_name = f"{safe_title or 'video'}.{ext}"

        with jobs_lock:
            jobs[job_id].update({
                'status': 'done',
                'progress': 100.0,
                'phase': 'Ready!',
                'file': file_path,
                'filename': download_name,
                'fmt': fmt,
            })

    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with jobs_lock:
            jobs[job_id].update({'status': 'error', 'error': clean_error(str(e))})
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with jobs_lock:
            jobs[job_id].update({'status': 'error', 'error': clean_error(str(e))})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/download")
def download_page():
    url = request.args.get("url", "")
    return render_template("download.html", url=url)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/vid/info", methods=["POST"])
def video_info():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    ydl_opts = base_ydl_opts()
    ydl_opts["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", [])

        heights = set()
        for f in formats:
            h = f.get("height")
            vcodec = f.get("vcodec", "none")
            if h and h > 0 and vcodec and vcodec != "none":
                heights.add(h)

        qualities = [str(h) for h in sorted(heights)] if heights else ["360", "720"]

        has_audio = any(
            f.get("acodec") and f.get("acodec") != "none"
            for f in formats
        )

        duration = info.get("duration", 0) or 0
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        duration_str = f"{minutes}:{seconds:02d}" if duration else ""

        file_sizes = estimate_sizes(formats, qualities, duration=duration)

        return jsonify({
            "title": info.get("title", "Unknown Title"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": duration_str,
            "uploader": info.get("uploader", ""),
            "view_count": info.get("view_count", 0),
            "qualities": qualities,
            "has_audio": has_audio,
            "platform": info.get("extractor_key", "Unknown"),
            "file_sizes": file_sizes,
        })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": clean_error(str(e))}), 400
    except Exception as e:
        return jsonify({"error": clean_error(str(e))}), 400


@app.route("/vid/start", methods=["POST"])
def start_download():
    data = request.get_json()
    url     = (data or {}).get("url", "").strip()
    quality = (data or {}).get("quality", "720")
    fmt     = (data or {}).get("format", "video")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            'status':   'pending',
            'progress': 0.0,
            'phase':    'Starting...',
            'file':     None,
            'filename': None,
            'fmt':      fmt,
            'error':    None,
            'tmp_dir':  None,
        }

    t = threading.Thread(target=run_download, args=(job_id, url, quality, fmt), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/vid/progress/<job_id>")
def download_progress(job_id):
    @stream_with_context
    def generate():
        max_wait = 600   # 10 minutes timeout
        elapsed  = 0
        interval = 0.4

        while elapsed < max_wait:
            with jobs_lock:
                job = jobs.get(job_id)

            if not job:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                return

            payload = {
                'status':   job['status'],
                'progress': job['progress'],
                'phase':    job['phase'],
            }

            if job['status'] == 'error':
                payload['error'] = job['error']
                yield f"data: {json.dumps(payload)}\n\n"
                with jobs_lock:
                    jobs.pop(job_id, None)
                return

            if job['status'] == 'done':
                yield f"data: {json.dumps(payload)}\n\n"
                return  # keep job alive so /vid/file can serve it

            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(interval)
            elapsed += interval

        # Timed out
        yield f"data: {json.dumps({'status': 'error', 'error': 'Download timed out. Please try again.'})}\n\n"
        with jobs_lock:
            job = jobs.pop(job_id, None)
        if job and job.get('tmp_dir'):
            shutil.rmtree(job['tmp_dir'], ignore_errors=True)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':       'keep-alive',
        }
    )


@app.route("/vid/file/<job_id>")
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found or already downloaded"}), 404
    if job['status'] != 'done':
        return jsonify({"error": "File not ready yet"}), 202

    file_path = job['file']
    filename  = job.get('filename', 'video.mp4')
    fmt       = job.get('fmt', 'video')
    tmp_dir   = job.get('tmp_dir')

    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File missing on server"}), 500

    file_size = os.path.getsize(file_path)
    mime = "audio/mpeg" if fmt == "mp3" else "video/mp4"

    # Build a safe ASCII filename for the Content-Disposition header
    # (HTTP headers are latin-1; non-ASCII chars crash Flask's dev server)
    ascii_name = filename.encode('ascii', 'ignore').decode('ascii') or 'video.mp4'

    def stream_file():
        try:
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            with jobs_lock:
                jobs.pop(job_id, None)
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    return Response(
        stream_with_context(stream_file()),
        mimetype=mime,
        headers={
            'Content-Disposition': f'attachment; filename="{ascii_name}"',
            'Content-Length':      str(file_size),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
