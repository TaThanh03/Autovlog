#!/usr/bin/env python3
"""
lazyvlog.py — Build one chronological slideshow/vlog video from a folder of mixed
photos and videos, ordered by capture date, with random crossfade transitions
and per-type transition sound effects. Output is tuned to play on a Samsung
The Frame (and most TVs): H.264 High, 8-bit yuv420p, auto-selected level, AAC.

HOW IT WORKS (high level)
  1. SCAN     find media files in the source folder.
  2. DATE     read each file's oldest timestamp and sort chronologically.
  3. NORMALIZE re-encode every photo/clip to ONE uniform format (4K/30/H264).
  4. TRIM     cut each clip into frame-exact pieces (body + head/tail).
  5. WINDOW   re-encode ONLY the short crossfade between adjacent clips.
  6. CONCAT   stitch bodies + windows by stream copy (no re-encode).
  7. AUDIO    build one continuous audio track (crossfaded) in a single pass.
  8. SFX      mix a sound effect in at each transition point.
  9. MUX      combine the video and audio into the final file, then verify it.

Independent work (normalizing clips, trimming pieces, building windows) runs in
parallel via a thread pool. The heavy ffmpeg work happens in child processes, so
threads — not extra Python processes — are the right tool: the GIL is released
while we wait on ffmpeg.

Caching: normalized clips AND trimmed body pieces are cached on disk. Because the
cache key for a body depends on the clip and the crossfade DURATION (but not the
transition STYLE or the sound effects), changing --xfade-type or --sfx reuses all
the expensive encodes and only rebuilds the tiny windows + audio.
"""

import argparse
import hashlib
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# --------------------------------------------------------------------------- #
# Logging: every line is prefixed with seconds elapsed since start, on stderr,
# so progress is visible without polluting the output file or stdout.
# --------------------------------------------------------------------------- #
_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{int(time.time() - _T0):4d}s] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Child-process tracking. ffmpeg jobs run as subprocesses; if the user presses
# Ctrl-C we want to kill them instead of leaving orphaned encoders running.
# --------------------------------------------------------------------------- #
_CHILDREN: set[subprocess.Popen] = set()
_INTERRUPTED = False


def _on_sigint(signum, frame):
    """On Ctrl-C: flag the run as interrupted and terminate every live ffmpeg."""
    global _INTERRUPTED
    _INTERRUPTED = True
    for p in list(_CHILDREN):
        try:
            p.terminate()
        except Exception:
            pass
    log("interrupted — terminating ffmpeg jobs")
    sys.exit(130)


def run(cmd: list[str]) -> None:
    """
    Run one ffmpeg/ffprobe command. Raises RuntimeError with the captured
    stderr if it fails, so a broken stage stops the whole run cleanly instead
    of silently producing a bad file.
    """
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _CHILDREN.add(p)
    try:
        _, err = p.communicate()
    finally:
        _CHILDREN.discard(p)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(cmd[:6])} ...\n"
            f"{err.decode(errors='replace')[-800:]}"
        )


def out(cmd: list[str]) -> str:
    """Run a command and return its stdout as a stripped string (for ffprobe)."""
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# Small ffprobe wrappers.
# --------------------------------------------------------------------------- #
def probe_duration(path: str) -> float:
    """Return a media file's duration in seconds."""
    s = out(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    return float(s) if s else 0.0


def probe_pix_fmt(path: str) -> str:
    """Return the video stream's pixel format (e.g. yuv420p)."""
    return out(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", path])


def probe_profile(path: str) -> str:
    """Return the video stream's H.264 profile (e.g. High)."""
    return out(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=profile", "-of", "csv=p=0", path])


def has_audio(path: str) -> bool:
    """True if the file contains at least one audio stream."""
    return bool(out(["ffprobe", "-v", "error", "-select_streams", "a",
                     "-show_entries", "stream=index", "-of", "csv=p=0", path]))


def probe_wh_fps(path: str):
    """Return (width, height, fps_or_None) of a media file's video stream.
    Photos have no real frame rate, so fps is None for them."""
    s = out(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate",
             "-of", "csv=p=0", path])
    parts = s.split(",")
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    w, h = int(parts[0]), int(parts[1])
    fps = None
    if len(parts) >= 3 and "/" in parts[2]:
        num, den = parts[2].split("/")
        try:
            num, den = float(num), float(den)
            if den > 0 and num > 0:
                fps = num / den
        except ValueError:
            pass
    return w, h, fps


# --------------------------------------------------------------------------- #
# Date extraction: pick the OLDEST timestamp available so true capture order
# survives even when a transfer (LocalSend, etc.) reset the filesystem times.
# Candidates: filesystem atime/mtime/ctime/birthtime + EXIF/QuickTime dates.
# --------------------------------------------------------------------------- #
EXIF_TAGS = ["DateTimeOriginal", "CreateDate", "MediaCreateDate", "CreationDate"]


def oldest_timestamp(path: str, fs_only: bool, have_exiftool: bool) -> float:
    """Return the oldest plausible epoch timestamp for ordering this file."""
    st = os.stat(path)
    cands = [st.st_atime, st.st_mtime, st.st_ctime]
    # st_birthtime exists on some platforms; ignore if absent.
    bt = getattr(st, "st_birthtime", None)
    if bt:
        cands.append(bt)
    if not fs_only and have_exiftool:
        for tag in EXIF_TAGS:
            s = out(["exiftool", "-s3", "-d", "%s", f"-{tag}", path])
            if s.isdigit():
                cands.append(float(s))
    cands = [c for c in cands if c and c > 0]
    return min(cands) if cands else st.st_mtime


# --------------------------------------------------------------------------- #
# H.264 level: pick the LOWEST level that still covers the chosen resolution and
# frame rate. The level is a "decoder workload ceiling" stamped in the file; a
# value too low makes the stream non-compliant (and TVs reject it), needlessly
# high wastes nothing but isn't required. Auto-selecting keeps it always valid.
# --------------------------------------------------------------------------- #
def h264_level(w: int, h: int, fps: int) -> str:
    px = w * h
    if px <= 1280 * 720:
        return "3.1" if fps <= 30 else "3.2"
    if px <= 1920 * 1088:
        return "4.0" if fps <= 30 else "4.2"
    if px <= 2560 * 1440:
        return "5.0" if fps <= 30 else "5.1"
    if px <= 3840 * 2160:
        return "5.1" if fps <= 30 else "5.2"
    return "5.2"


# --------------------------------------------------------------------------- #
# Encoder options. These are the TV-safe, VDPAU-safe flags validated earlier:
# High profile, 8-bit yuv420p, explicit level, and (for libx264) input-csp=i420
# so the encoder can't silently promote to 4:4:4.
# --------------------------------------------------------------------------- #
def video_opts(a) -> list[str]:
    level = h264_level(a.width, a.height, a.fps)
    if a.nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(a.crf), "-b:v", "0", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-level:v", level]
    return ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", level,
            "-x264-params", "input-csp=i420"]


def scale_filter(a) -> str:
    """Fit each source into the target frame (letterbox/pillarbox), CFR, 4:2:0.
    Lanczos gives a sharper downscale for high-megapixel photos."""
    return (f"scale={a.width}:{a.height}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos,pad={a.width}:{a.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={a.fps},format=yuv420p")


# --------------------------------------------------------------------------- #
# Cache-key helper. A short hash of all inputs that affect the encoded result,
# so a file is reused only when nothing relevant changed.
# --------------------------------------------------------------------------- #
def sha(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VID_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
XPOOL = ["fade", "fadeblack", "dissolve", "wipeleft", "wiperight", "wipeup",
         "wipedown", "slideleft", "slideright", "slideup", "slidedown",
         "smoothleft", "smoothright", "smoothup", "smoothdown",
         "circleopen", "circleclose", "radial", "pixelize"]


class Clip:
    """Everything we track about one input file as it moves through the pipeline."""
    def __init__(self, src, kind, date):
        self.src = src              # original file path
        self.kind = kind            # "photo" or "video"
        self.date = date            # epoch used for ordering
        self.norm = None            # path to normalized 4K clip
        self.dur = 0.0              # normalized duration (seconds)
        self.frames = 0             # normalized duration (frames)
        self.body = None            # path to body piece
        self.head = None            # path to head piece (None for first clip)
        self.tail = None            # path to tail piece (None for last clip)


# --------------------------------------------------------------------------- #
# STEP 3 — normalize one clip to the uniform format. Cached on disk.
# --------------------------------------------------------------------------- #
def legacy_bash_key(c: Clip, a) -> str:
    """
    Reproduce EXACTLY the cache filename the old bash script produced, so its
    already-encoded clips (different naming, same content) can be reused.
    Bash hashed: abspath|size|mtime|W|H|fps|venc|crf|extra  (sha1, first 16 hex)
    where venc was the full ffmpeg name and extra was 'd<DUR>' for photos.
    """
    st = os.stat(c.src)
    venc = "h264_nvenc" if a.nvenc else "libx264"
    # bash used the integer -d value (default 4), e.g. 'd4' not 'd4.0'
    dur_tok = str(int(a.dur)) if float(a.dur).is_integer() else str(a.dur)
    extra = f"d{dur_tok}" if c.kind == "photo" else ""
    return sha(os.path.realpath(c.src), st.st_size, int(st.st_mtime),
              a.width, a.height, a.fps, venc, a.crf, extra)


def normalize_one(c: Clip, a, vopts, cache_dir, work_dir):
    """Re-encode one photo/clip into the shared 4K/30/H264 format used by all."""
    st = os.stat(c.src)
    # Photos also depend on display duration (-d); videos don't.
    extra = f"d{a.dur}" if c.kind == "photo" else ""
    key = sha(os.path.abspath(c.src), st.st_size, int(st.st_mtime),
              a.width, a.height, a.fps, "nvenc" if a.nvenc else "x264",
              a.crf, extra)
    dest_dir = cache_dir if cache_dir else work_dir
    c.norm = os.path.join(dest_dir, f"norm_{key}.mp4")

    # 1) Native Python cache hit.
    if os.path.exists(c.norm) and os.path.getsize(c.norm) > 0:
        log(f"NORMALIZE: cache hit  {os.path.basename(c.src)}")
        return

    # 2) Legacy bash cache hit: same content under the old filename. Adopt it,
    #    but only if it is genuinely 4:2:0 (reject stale 4:4:4 leftovers).
    if cache_dir:
        legacy = os.path.join(cache_dir, f"{legacy_bash_key(c, a)}.mp4")
        if os.path.exists(legacy) and os.path.getsize(legacy) > 0:
            if probe_pix_fmt(legacy) in ("yuv420p", "yuvj420p"):
                try:
                    os.link(legacy, c.norm)        # hardlink: instant, no extra space
                except OSError:
                    shutil.copyfile(legacy, c.norm)  # fallback across filesystems
                log(f"NORMALIZE: reused legacy cache  {os.path.basename(c.src)}")
                return
            else:
                log(f"NORMALIZE: legacy entry for {os.path.basename(c.src)} is not "
                    f"4:2:0 — re-encoding")

    sz = f"{st.st_size // (1024*1024)}M"
    log(f"NORMALIZE: building {c.kind} {os.path.basename(c.src)} ({sz})")

    vf = scale_filter(a)
    if c.kind == "photo":
        # A still image looped for `dur` seconds, with a matching silent track
        # so it can be concatenated/crossfaded alongside real video+audio.
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-loop", "1", "-framerate", str(a.fps), "-t", str(a.dur), "-i", c.src,
               "-f", "lavfi", "-t", str(a.dur),
               "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-movflags", "+faststart", c.norm]
    elif has_audio(c.src):
        # Video that already has sound: keep it, just conform to our format.
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", c.src, "-vf", vf,
               *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
               "-movflags", "+faststart", c.norm]
    else:
        # Silent video: add a silent stereo track so audio mixing stays uniform.
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", c.src,
               "-f", "lavfi",
               "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-movflags", "+faststart", c.norm]
    run(cmd)


# --------------------------------------------------------------------------- #
# STEP 4 — trim one frame-exact piece from a normalized clip. Bodies are cached.
# The trim filter cuts on exact frame numbers (unlike stream-copy, which can
# only cut on keyframes and drifts), so there is no audio/video desync.
# --------------------------------------------------------------------------- #
def trim_piece(c: Clip, role: str, sframe: int, eframe, a, vopts, cache_dir, work_dir):
    """Produce one piece (body/head/tail) as a video-only file; cache bodies."""
    if eframe is not None:
        expr = f"trim=start_frame={sframe}:end_frame={eframe}"
        endtag = eframe
    else:
        expr = f"trim=start_frame={sframe}"
        endtag = "end"
    # Body pieces are the expensive bulk -> cache them. Head/tail are tiny ->
    # build in scratch each run (they feed the cheap windows anyway).
    if role == "body" and cache_dir:
        key = sha(os.path.basename(c.norm), role, sframe, endtag)
        path = os.path.join(cache_dir, f"piece_{key}.mp4")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path  # cache hit: skip re-encoding this body
    else:
        path = os.path.join(work_dir, f"{role}_{sha(c.norm, sframe, endtag)}.mp4")

    flt = f"[0:v]{expr},setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v]"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", c.norm,
         "-filter_complex", flt, "-map", "[v]", "-an", *vopts,
         "-movflags", "+faststart", path])
    return path


# --------------------------------------------------------------------------- #
# STEP 5 — build one transition window: crossfade a clip's tail into the next
# clip's head. This is the ONLY footage that gets blended/re-encoded per join.
# --------------------------------------------------------------------------- #
def make_window(tail: str, head: str, xtype: str, a, vopts, work_dir, idx: int):
    xt = random.choice(XPOOL) if xtype == "random" else xtype
    win = os.path.join(work_dir, f"win_{idx}.mp4")
    flt = (f"[0:v]format=yuv420p,setsar=1,fps={a.fps}[x];"
           f"[1:v]format=yuv420p,setsar=1,fps={a.fps}[y];"
           f"[x][y]xfade=transition={xt}:duration={a.xfade}:offset=0,"
           f"format=yuv420p[v]")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", tail, "-i", head,
         "-filter_complex", flt, "-map", "[v]", "-an", *vopts,
         "-movflags", "+faststart", win])
    return win, xt


# --------------------------------------------------------------------------- #
# Run a batch of independent jobs in parallel, capped at `jobs` at once.
# Used for the normalize and trim stages (each clip is independent).
# --------------------------------------------------------------------------- #
def parallel(jobs: int, tasks):
    """tasks = list of zero-arg callables. Runs them with a thread pool, raising
    the first error encountered."""
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = [ex.submit(t) for t in tasks]
        for f in as_completed(futures):
            f.result()  # re-raise any worker exception, aborting the run


def survey_and_cap(clips, a):
    """
    STEP 1.5 — inspect every clip's resolution and frame rate, print a summary
    to help the user pick settings, and CAP the requested output so it can never
    be larger (more pixels) or faster (higher fps) than the source actually is.
    Upscaling/interpolating beyond the source adds no real detail, only size and
    encode time.
    """
    res_count, fps_count = {}, {}
    max_px, max_w, max_h = 0, 0, 0
    max_fps = 0.0
    for c in clips:
        info = probe_wh_fps(c.src)
        if not info:
            continue
        w, h, fps = info
        res_count[(w, h)] = res_count.get((w, h), 0) + 1
        if w * h > max_px:
            max_px, max_w, max_h = w * h, w, h
        if fps:  # only videos contribute a frame rate
            rf = round(fps)
            fps_count[rf] = fps_count.get(rf, 0) + 1
            max_fps = max(max_fps, fps)

    # Print the distribution.
    res_str = ", ".join(f"{w}x{h} ({n})" for (w, h), n in
                        sorted(res_count.items(), key=lambda kv: -kv[0][0] * kv[0][1]))
    fps_str = ", ".join(f"{r}fps ({n})" for r, n in sorted(fps_count.items())) or "n/a (photos only)"
    log(f"SURVEY: resolutions: {res_str}")
    log(f"SURVEY: frame rates: {fps_str}")
    rec_fps = round(max_fps) if max_fps else a.fps
    if max_w:
        log(f"SURVEY: source maximum = {max_w}x{max_h}"
            + (f" @ {rec_fps}fps" if max_fps else ""))
        log(f"SURVEY: recommended:   -r {max_w}x{max_h}"
            + (f" --fps {rec_fps}" if max_fps else ""))

    # CAP resolution: never exceed the largest source frame (by pixel count).
    if max_px and a.width * a.height > max_px:
        log(f"CAP: requested {a.width}x{a.height} exceeds source max "
            f"{max_w}x{max_h}; clamping down (no upscaling).")
        a.width, a.height = max_w, max_h

    # CAP frame rate: never exceed the fastest source clip.
    if max_fps and a.fps > rec_fps:
        log(f"CAP: requested {a.fps}fps exceeds source max {rec_fps}fps; "
            f"clamping to {rec_fps}.")
        a.fps = rec_fps


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Chronological slideshow video with crossfades + transition SFX.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    p.add_argument("-s", "--src", default=".", help="source folder (non-recursive)")
    p.add_argument("-o", "--out", default="output.mp4", help="output file")
    p.add_argument("-d", "--dur", type=float, default=4.0,
                   help="seconds per still photo (default 4)")
    p.add_argument("-r", "--res", default="3840x2160",
                   help="output resolution WxH (default 3840x2160, for The Frame)")
    p.add_argument("--fps", type=int, default=30, help="output frame rate")
    p.add_argument("--nvenc", action="store_true",
                   help="use GPU encoder (faster, lower quality; cap --jobs low)")
    p.add_argument("--crf", type=int, default=18,
                   help="quality: lower=better+bigger (default 18; 16 near-lossless)")
    p.add_argument("--preset", default="slow",
                   help="libx264 speed/efficiency (default slow)")
    p.add_argument("--xfade", type=float, default=0.5,
                   help="crossfade seconds (0 = hard cut; auto-reduced if too big)")
    p.add_argument("--xfade-type", default="random",
                   help="'random' (per-join) or a fixed name (fade, dissolve, ...)")
    p.add_argument("--sfx", default=os.path.join(here, "sfx"),
                   help="folder of sound effects (files named *photo* / *video*)")
    p.add_argument("--sfx-gain", type=float, default=1.0, help="SFX volume multiplier")
    p.add_argument("--fs-only", action="store_true",
                   help="order by filesystem times only (ignore EXIF)")
    p.add_argument("--cache", default=os.path.join(here, "cache"),
                   help="persistent cache dir (default <script dir>/cache)")
    p.add_argument("--no-cache", action="store_true", help="disable the cache")
    p.add_argument("--tmp", default=os.path.join(here, "tmp"),
                   help="scratch dir (default <script dir>/tmp; avoid small tmpfs)")
    p.add_argument("-j", "--jobs", type=int, default=0,
                   help="parallel encode jobs (default: 1 for nvenc, 3 for libx264)")
    a = p.parse_args(argv)
    a.width, a.height = (int(x) for x in a.res.lower().split("x"))
    if a.no_cache:
        a.cache = ""
    # Encoder-aware default concurrency: NVENC has few GPU sessions; libx264 on
    # one 4K stream doesn't saturate many cores, so 3 fills the CPU.
    if a.jobs <= 0:
        a.jobs = 1 if a.nvenc else 3
    return a


def main(argv):
    signal.signal(signal.SIGINT, _on_sigint)
    a = parse_args(argv)

    # Tooling checks.
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"missing: {tool} (sudo apt install ffmpeg)")
    have_exiftool = bool(shutil.which("exiftool"))
    if not a.fs_only and not have_exiftool:
        log("WARN: exiftool not found — ordering by filesystem times only")

    # Scratch + cache dirs. Warn if scratch is on a tiny RAM disk (tmpfs).
    os.makedirs(a.tmp, exist_ok=True)
    work = os.path.join(a.tmp, f"automovie_{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    if a.cache:
        os.makedirs(a.cache, exist_ok=True)
    free_gb = shutil.disk_usage(work).free // (1024 ** 3)
    if free_gb < 20:
        log(f"WARN: scratch {work} has only {free_gb}G free — 4K needs tens of GB")

    # ---- STEP 1: SCAN ---- find supported media in the source folder.
    if not os.path.isdir(a.src):
        sys.exit(f"source folder not found: {a.src}")
    files = []
    for name in os.listdir(a.src):
        ext = os.path.splitext(name)[1].lower()
        full = os.path.join(a.src, name)
        if os.path.isfile(full) and (ext in IMG_EXT or ext in VID_EXT):
            files.append((full, "photo" if ext in IMG_EXT else "video"))
    if not files:
        sys.exit(f"no media found in {a.src}")
    log(f"SCAN: {len(files)} files found")

    # ---- STEP 2: DATE + SORT ---- order chronologically by oldest timestamp.
    log("DATE: reading timestamps" + (" (fs-only)" if a.fs_only else ""))
    clips = []
    for i, (path, kind) in enumerate(files, 1):
        d = oldest_timestamp(path, a.fs_only, have_exiftool)
        log(f"DATE: [{i}/{len(files)}] {os.path.basename(path)}")
        clips.append(Clip(path, kind, d))
    clips.sort(key=lambda c: c.date)
    log("DATE: sorted chronologically")
    N = len(clips)

    # ---- STEP 1.5: SURVEY + CAP ---- report source res/fps and prevent upscaling.
    survey_and_cap(clips, a)

    # Encoder options depend on the (possibly capped) resolution/fps, so build
    # them now, after the cap.
    vopts = video_opts(a)

    # Sound-effect pools (optional). Files containing 'photo' / 'video' in name.
    photo_sfx, video_sfx = [], []
    if a.sfx and os.path.isdir(a.sfx):
        for f in sorted(os.listdir(a.sfx)):
            low = f.lower()
            full = os.path.join(a.sfx, f)
            if "photo" in low:
                photo_sfx.append(full)
            if "video" in low:
                video_sfx.append(full)
        log(f"SFX: {len(photo_sfx)} photo, {len(video_sfx)} video effects in {a.sfx}")

    # ---- STEP 3: NORMALIZE ---- re-encode all clips to one format, in parallel.
    log(f"NORMALIZE: {N} clips to {a.width}x{a.height}@{a.fps} "
        f"via {'nvenc' if a.nvenc else 'libx264'} (-j {a.jobs}"
        f"{', cache on' if a.cache else ''})")
    if a.nvenc and a.jobs > 1:
        log(f"WARN: -j {a.jobs} with NVENC may exceed the GPU session limit")
    parallel(a.jobs, [lambda c=c: normalize_one(c, a, vopts, a.cache, work)
                      for c in clips])

    # Measure each normalized clip: exact duration and frame count.
    for c in clips:
        c.dur = probe_duration(c.norm)
        c.frames = round(c.dur * a.fps)

    # ---- Clamp the crossfade ---- it must be shorter than half the shortest
    # clip, because interior clips give up a crossfade window at BOTH ends.
    if a.xfade > 0:
        min_d = min(c.dur for c in clips)
        if 2 * a.xfade >= min_d:
            new_x = round(min_d * 0.25, 3)
            if new_x > 0:
                log(f"WARN: --xfade {a.xfade} too long for shortest clip "
                    f"{min_d:.3f}s; reducing to {new_x}s")
                a.xfade = new_x
            else:
                log(f"WARN: shortest clip {min_d:.3f}s too short for crossfade; "
                    f"using hard cuts")
                a.xfade = 0.0

    # ---- Timeline ---- the on-screen start time (ms) of each clip, accounting
    # for the crossfade overlap. Used to place each transition sound effect.
    start_ms, acc = [], 0.0
    for i, c in enumerate(clips):
        if i == 0:
            start_ms.append(0)
            acc = c.dur
        else:
            start_ms.append(max(0, int((acc - a.xfade) * 1000)))
            acc += c.dur - a.xfade

    video = os.path.join(work, "video.mp4")
    audio = os.path.join(work, "audio.m4a")
    XF = round(a.xfade * a.fps)  # crossfade length in frames

    if a.xfade > 0 and N >= 2:
        # ---- STEP 4: TRIM ---- cut every clip into frame-exact pieces.
        log(f"TRANSITION: windowed xfade '{a.xfade_type}' {a.xfade}s "
            f"(re-encode only {XF}-frame windows)")

        def make_pieces(i):
            c = clips[i]
            if i == 0:                         # first clip: body + tail only
                c.body = trim_piece(c, "body", 0, c.frames - XF, a, vopts, a.cache, work)
                c.tail = trim_piece(c, "tail", c.frames - XF, None, a, vopts, a.cache, work)
            elif i == N - 1:                   # last clip: head + body only
                c.head = trim_piece(c, "head", 0, XF, a, vopts, a.cache, work)
                c.body = trim_piece(c, "body", XF, None, a, vopts, a.cache, work)
            else:                              # interior: head + body + tail
                c.head = trim_piece(c, "head", 0, XF, a, vopts, a.cache, work)
                c.body = trim_piece(c, "body", XF, c.frames - XF, a, vopts, a.cache, work)
                c.tail = trim_piece(c, "tail", c.frames - XF, None, a, vopts, a.cache, work)
            log(f"TRIM: [{i+1}/{N}] {clips[i].kind} pieces ready")

        parallel(a.jobs, [lambda i=i: make_pieces(i) for i in range(N)])

        # ---- STEP 5: WINDOWS ---- crossfade tail[i] into head[i+1], in parallel.
        windows = [None] * (N - 1)

        def build_window(i):
            win, xt = make_window(clips[i].tail, clips[i + 1].head,
                                  a.xfade_type, a, vopts, work, i)
            windows[i] = win
            log(f"TRANSITION: window {i+1}/{N-1} -> {xt}")

        parallel(a.jobs, [lambda i=i: build_window(i) for i in range(N - 1)])

        # ---- STEP 6: CONCAT ---- bodies + windows, by stream copy (no re-encode).
        vlist = os.path.join(work, "vlist.txt")
        with open(vlist, "w") as fh:
            for i, c in enumerate(clips):
                fh.write(f"file '{c.body}'\n")
                if i < N - 1:
                    fh.write(f"file '{windows[i]}'\n")
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", vlist, "-an", "-c", "copy", "-movflags", "+faststart", video])

        # ---- STEP 7: AUDIO ---- one continuous pass crossfading all clip audio.
        # Audio is cheap, so a single graph (no drift) is fine here.
        log(f"AUDIO: single-pass acrossfade over {N} clips")
        ains, af, pa = [], "", "[0:a]"
        for c in clips:
            ains += ["-i", c.norm]
        for k in range(1, N):
            af += f"{pa}[{k}:a]acrossfade=d={a.xfade}:c1=tri:c2=tri[ax{k}];"
            pa = f"[ax{k}]"
        af = af.rstrip(";")
        run(["ffmpeg", "-y", "-loglevel", "error", *ains,
             "-filter_complex", af, "-map", pa,
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", audio])
    else:
        # ---- Hard-cut path ---- no crossfades: concat whole clips (copy) and
        # concatenate their audio.
        log(f"TRANSITION: hard cuts, concat {N} clips")
        vlist = os.path.join(work, "vlist.txt")
        with open(vlist, "w") as fh:
            for c in clips:
                fh.write(f"file '{c.norm}'\n")
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", vlist, "-an", "-c", "copy", "-movflags", "+faststart", video])
        ains, ac = [], ""
        for i, c in enumerate(clips):
            ains += ["-i", c.norm]
            ac += f"[{i}:a]"
        run(["ffmpeg", "-y", "-loglevel", "error", *ains,
             "-filter_complex", f"{ac}concat=n={N}:v=0:a=1[a]", "-map", "[a]",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", audio])

    # ---- STEP 8: SFX ---- mix one sound effect in at each clip's start time.
    final_audio = audio
    if photo_sfx or video_sfx:
        log("SFX: assigning transition effects")
        inputs = ["-i", audio]
        flt, amix_in, n, idx = "", "[0:a]", 0, 1
        for s, c in enumerate(clips):
            pool = photo_sfx if c.kind == "photo" else video_sfx
            if not pool:
                continue
            sfx = random.choice(pool)
            ms = start_ms[s]
            log(f"SFX: seg {s+1} {c.kind} @{ms}ms <- {os.path.basename(sfx)}")
            inputs += ["-i", sfx]
            flt += f"[{idx}:a]adelay={ms}:all=1,volume={a.sfx_gain}[s{idx}];"
            amix_in += f"[s{idx}]"
            idx += 1
            n += 1
        if n > 0:
            log(f"MIX: overlaying {n} effects")
            flt += f"{amix_in}amix=inputs={n+1}:normalize=0:duration=first[aout]"
            final_audio = os.path.join(work, "audio_sfx.m4a")
            run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
                 "-filter_complex", flt, "-map", "[aout]",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", final_audio])

    # ---- STEP 9: MUX + VERIFY ---- combine video + audio, confirm TV-safe.
    log("MUX: combining video + audio")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", final_audio,
         "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest",
         "-movflags", "+faststart", a.out])

    pf, pr = probe_pix_fmt(a.out), probe_profile(a.out)
    if pf == "yuv420p":
        log(f"VERIFY: TV-safe ({pr} / {pf})")
    else:
        log(f"VERIFY: WARNING — pix_fmt '{pf}' (profile '{pr}') is NOT yuv420p; "
            f"may not play on TV/VDPAU")

    # Clean up scratch (cache is kept).
    shutil.rmtree(work, ignore_errors=True)
    log(f"DONE: {a.out}")
    print(f"done: {a.out}")


if __name__ == "__main__":
    main(sys.argv[1:])