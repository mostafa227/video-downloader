import os
import re
import tempfile
from flask import Flask, render_template, request, jsonify, Response
import yt_dlp

app = Flask(__name__)

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def clean_error(msg):
    msg = ANSI_ESCAPE.sub("", str(msg))
    msg = re.sub(r"^ERROR:\s*", "", msg, flags=re.IGNORECASE).strip()
    msg = re.sub(r"\[.*?\]\s*[\w\-]+:\s*", "", msg, count=1).strip()

    low = msg.lower()
    if "login required" in low or "sign in" in low or "authentication" in low:
        return "This video requires login. Instagram, Facebook, and Twitter/X often restrict private or age-gated content. Try a public video URL."
    if "rate" in low and ("limit" in low or "429" in low):
        return "Rate limit reached for this platform. Please wait a few minutes and try again."
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
    if "unable to extract" in low or "please report" in low:
        return "This site is not fully supported. Try updating yt-dlp or use a link from YouTube, TikTok, Facebook, or Vimeo."
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return msg or "Could not fetch video info. Please check the URL and try again."


def estimate_sizes(formats):
    def get_size(f):
        return f.get('filesize') or f.get('filesize_approx') or 0

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
    for quality, height in [('360', 360), ('720', 720), ('1080', 1080)]:
        total = 0
        vf = [f for f in video_only if 0 < (f.get('height') or 0) <= height]
        if vf:
            best_v = max(vf, key=lambda f: ((f.get('height') or 0), f.get('tbr') or 0))
            v_size = get_size(best_v)
            if v_size > 0:
                total = v_size + best_audio_size
        if total == 0:
            cf = [f for f in combined if 0 < (f.get('height') or 0) <= height]
            if cf:
                best_c = max(cf, key=lambda f: ((f.get('height') or 0), f.get('tbr') or 0))
                total = get_size(best_c)
        sizes[quality] = total if total > 0 else None

    mp3_size = 0
    if audio_only:
        best_a = max(audio_only, key=lambda f: f.get('abr') or f.get('tbr') or 0)
        mp3_size = get_size(best_a)
    sizes['mp3'] = mp3_size if mp3_size > 0 else None
    return sizes


def get_ydl_opts(quality="best", fmt="video", output_path=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
    }
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        if output_path:
            opts["outtmpl"] = output_path + ".%(ext)s"
    else:
        if quality == "360":
            opts["format"] = "bestvideo[height<=360]+bestaudio/best[height<=360]/best"
        elif quality == "720":
            opts["format"] = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        elif quality == "1080":
            opts["format"] = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        else:
            opts["format"] = "bestvideo+bestaudio/best"
        if output_path:
            opts["outtmpl"] = output_path + ".%(ext)s"
        opts["merge_output_format"] = "mp4"
    return opts


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

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", [])
        available_qualities = set()
        for f in formats:
            h = f.get("height")
            if h:
                if h >= 1080:
                    available_qualities.add("1080")
                if h >= 720:
                    available_qualities.add("720")
                available_qualities.add("360")

        if not available_qualities:
            available_qualities = {"360", "720"}

        has_audio = any(
            f.get("acodec") and f.get("acodec") != "none"
            for f in formats
        )

        duration = info.get("duration", 0) or 0
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        duration_str = f"{minutes}:{seconds:02d}" if duration else ""

        file_sizes = estimate_sizes(formats)

        return jsonify({
            "title": info.get("title", "Unknown Title"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": duration_str,
            "uploader": info.get("uploader", ""),
            "view_count": info.get("view_count", 0),
            "qualities": sorted(list(available_qualities), key=int),
            "has_audio": has_audio,
            "platform": info.get("extractor_key", "Unknown"),
            "file_sizes": file_sizes,
        })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": clean_error(str(e))}), 400
    except Exception as e:
        return jsonify({"error": clean_error(str(e))}), 400


@app.route("/vid/download")
def download_video():
    url = request.args.get("url", "").strip()
    quality = request.args.get("quality", "720")
    fmt = request.args.get("format", "video")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    tmp_dir = tempfile.mkdtemp()
    output_base = os.path.join(tmp_dir, "video")
    ydl_opts = get_ydl_opts(quality=quality, fmt=fmt, output_path=output_base)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")

        downloaded_files = [f for f in os.listdir(tmp_dir) if f.startswith("video")]
        if not downloaded_files:
            return jsonify({"error": "Download failed — no file was produced."}), 500

        file_path = os.path.join(tmp_dir, downloaded_files[0])
        ext = downloaded_files[0].rsplit(".", 1)[-1]
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_")[:60].strip()
        download_name = f"{safe_title or 'video'}.{ext}"

        with open(file_path, "rb") as f:
            content = f.read()

        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

        mime = "audio/mpeg" if fmt == "mp3" else "video/mp4"
        return Response(
            content,
            mimetype=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Content-Length": str(len(content)),
            }
        )
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": clean_error(str(e))}), 400
    except Exception as e:
        return jsonify({"error": clean_error(str(e))}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
