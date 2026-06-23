#!/usr/bin/env python3
"""
lazyvlog_core.py — Shared engine for the lazyvlog tools.

Everything that is NOT transition-specific lives here and is reused by both
front ends:

  * lazyvlog.py      — high-quality keeper: libx264, true frame-exact crossfades.
  * lazyvlogfast.py  — fast GPU build: NVENC, dip-to-black transitions.

Shared here: logging, the subprocess runner (with clean Ctrl-C), ffprobe
helpers, chronological dating + sort, the source survey + no-upscale cap, the
TV-safe encoder flags + auto level, the cache-key scheme (incl. legacy bash
cache reuse), the SFX overlay, the mux + verify step, and the parallel helper.

The two front ends implement only their own transition stage and defaults.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# --------------------------------------------------------------------------- #
# Logging: each line shows seconds elapsed, on stderr, so progress is visible
# without polluting stdout or the output file.
# --------------------------------------------------------------------------- #
_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{int(time.time() - _T0):4d}s] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Child-process tracking, so Ctrl-C kills running ffmpeg jobs instead of
# leaving orphaned encoders behind.
# --------------------------------------------------------------------------- #
_CHILDREN: "set[subprocess.Popen]" = set()


def on_sigint(signum, frame):
    """Terminate every live ffmpeg child, then exit."""
    for p in list(_CHILDREN):
        try:
            p.terminate()
        except Exception:
            pass
    log("interrupted — terminating ffmpeg jobs")
    sys.exit(130)


def run(cmd: "list[str]") -> None:
    """Run a command; raise with captured stderr if it fails (so a broken
    stage stops the run instead of silently making a bad file)."""
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


def out(cmd: "list[str]") -> str:
    """Run a command and return its stdout, stripped (used for ffprobe)."""
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# ffprobe wrappers.
# --------------------------------------------------------------------------- #
def probe_duration(path: str) -> float:
    s = out(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    return float(s) if s else 0.0


def probe_pix_fmt(path: str) -> str:
    return out(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", path])


def probe_profile(path: str) -> str:
    return out(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=profile", "-of", "csv=p=0", path])


def has_audio(path: str) -> bool:
    return bool(out(["ffprobe", "-v", "error", "-select_streams", "a",
                     "-show_entries", "stream=index", "-of", "csv=p=0", path]))


def probe_wh_fps(path: str):
    """Return (width, height, fps_or_None) of the video stream. Photos -> None fps."""
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
# Dating: pick the OLDEST timestamp so true capture order survives transfers
# (LocalSend etc.) that reset filesystem times.
# --------------------------------------------------------------------------- #
EXIF_TAGS = ["DateTimeOriginal", "CreateDate", "MediaCreateDate", "CreationDate"]


def oldest_timestamp(path: str, fs_only: bool, have_exiftool: bool) -> float:
    st = os.stat(path)
    cands = [st.st_atime, st.st_mtime, st.st_ctime]
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
# H.264 level: lowest level that covers the resolution+fps (TV-safe).
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


def video_opts(a) -> "list[str]":
    """TV-safe / VDPAU-safe encoder flags: High profile, 8-bit yuv420p, explicit
    level, and (libx264) input-csp=i420 so it can't promote to 4:4:4."""
    level = h264_level(a.width, a.height, a.fps)
    if a.nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(a.crf), "-b:v", "0", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-level:v", level]
    return ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", level,
            "-x264-params", "input-csp=i420"]


def scale_filter(a) -> str:
    """Fit source into the target frame (letterbox/pillarbox), CFR, 4:2:0.
    Lanczos gives a sharper downscale for high-megapixel photos."""
    return (f"scale={a.width}:{a.height}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos,pad={a.width}:{a.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={a.fps},format=yuv420p")


# --------------------------------------------------------------------------- #
# Cache-key helper + media types.
# --------------------------------------------------------------------------- #
def sha(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VID_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


class Clip:
    """One input file as it moves through the pipeline. Front ends may attach
    extra attributes (e.g. body/head/tail for slow, proc for fast)."""
    def __init__(self, src, kind, date):
        self.src = src
        self.kind = kind          # "photo" or "video"
        self.date = date          # epoch used for ordering
        self.norm = None          # normalized clip path
        self.dur = 0.0
        self.frames = 0


def legacy_bash_key(c: Clip, a) -> str:
    """Reproduce the OLD bash cache filename so its already-encoded clips can be
    reused. Bash hashed: abspath|size|mtime|W|H|fps|venc|crf|extra (sha1[:16])."""
    st = os.stat(c.src)
    venc = "h264_nvenc" if a.nvenc else "libx264"
    dur_tok = str(int(a.dur)) if float(a.dur).is_integer() else str(a.dur)
    extra = f"d{dur_tok}" if c.kind == "photo" else ""
    return sha(os.path.realpath(c.src), st.st_size, int(st.st_mtime),
               a.width, a.height, a.fps, venc, a.crf, extra)


# --------------------------------------------------------------------------- #
# Parallel helper: run independent zero-arg callables in a thread pool.
# --------------------------------------------------------------------------- #
def parallel(jobs: int, tasks):
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = [ex.submit(t) for t in tasks]
        for f in as_completed(futures):
            f.result()  # re-raise the first worker error, aborting the run


# --------------------------------------------------------------------------- #
# Scan + date + sort: turn a folder into an ordered list of Clips.
# --------------------------------------------------------------------------- #
def scan_media(src: str):
    if not os.path.isdir(src):
        sys.exit(f"source folder not found: {src}")
    files = []
    for name in os.listdir(src):
        ext = os.path.splitext(name)[1].lower()
        full = os.path.join(src, name)
        if os.path.isfile(full) and (ext in IMG_EXT or ext in VID_EXT):
            files.append((full, "photo" if ext in IMG_EXT else "video"))
    if not files:
        sys.exit(f"no media found in {src}")
    log(f"SCAN: {len(files)} files found")
    return files


def date_and_sort(files, fs_only, have_exiftool):
    log("DATE: reading timestamps" + (" (fs-only)" if fs_only else ""))
    clips = []
    for i, (path, kind) in enumerate(files, 1):
        d = oldest_timestamp(path, fs_only, have_exiftool)
        log(f"DATE: [{i}/{len(files)}] {os.path.basename(path)}")
        clips.append(Clip(path, kind, d))
    clips.sort(key=lambda c: c.date)
    log("DATE: sorted chronologically")
    return clips


# --------------------------------------------------------------------------- #
# Survey + cap: report source res/fps/aspect; never let output exceed source.
# Returns the set of distinct aspect ratios present (for pad decisions).
# --------------------------------------------------------------------------- #
def survey_and_cap(clips, a):
    res_count, fps_count, aspects = {}, {}, set()
    max_px = max_w = max_h = 0
    max_fps = 0.0
    for c in clips:
        info = probe_wh_fps(c.src)
        if not info:
            continue
        w, h, fps = info
        res_count[(w, h)] = res_count.get((w, h), 0) + 1
        aspects.add(round(w / h, 3) if h else 0)
        if w * h > max_px:
            max_px, max_w, max_h = w * h, w, h
        if fps:
            rf = round(fps)
            fps_count[rf] = fps_count.get(rf, 0) + 1
            max_fps = max(max_fps, fps)

    res_str = ", ".join(
        f"{w}x{h} ({n})" for (w, h), n in
        sorted(res_count.items(), key=lambda kv: -kv[0][0] * kv[0][1]))
    fps_str = ", ".join(f"{r}fps ({n})" for r, n in sorted(fps_count.items())) \
        or "n/a (photos only)"
    log(f"SURVEY: resolutions: {res_str}")
    log(f"SURVEY: frame rates: {fps_str}")
    rec_fps = round(max_fps) if max_fps else a.fps
    if max_w:
        log(f"SURVEY: source maximum = {max_w}x{max_h}"
            + (f" @ {rec_fps}fps" if max_fps else ""))
        log(f"SURVEY: recommended:   -r {max_w}x{max_h}"
            + (f" --fps {rec_fps}" if max_fps else ""))
    if len(aspects) > 1:
        log(f"SURVEY: {len(aspects)} aspect ratios present — mismatched clips "
            f"will be letterboxed/pillarboxed")

    if max_px and a.width * a.height > max_px:
        log(f"CAP: requested {a.width}x{a.height} exceeds source max "
            f"{max_w}x{max_h}; clamping down (no upscaling).")
        a.width, a.height = max_w, max_h
    if max_fps and a.fps > rec_fps:
        log(f"CAP: requested {a.fps}fps exceeds source max {rec_fps}fps; "
            f"clamping to {rec_fps}.")
        a.fps = rec_fps
    return aspects


# --------------------------------------------------------------------------- #
# Directories: scratch on a real disk (warn on tiny tmpfs), plus the cache dir.
# --------------------------------------------------------------------------- #
def setup_dirs(a):
    os.makedirs(a.tmp, exist_ok=True)
    work = os.path.join(a.tmp, f"lazyvlog_{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    if a.cache:
        os.makedirs(a.cache, exist_ok=True)
    free_gb = shutil.disk_usage(work).free // (1024 ** 3)
    if free_gb < 20:
        log(f"WARN: scratch {work} has only {free_gb}G free")
    return work


# --------------------------------------------------------------------------- #
# Sound effects: discover pools, then overlay one per clip boundary.
# --------------------------------------------------------------------------- #
def find_sfx_pools(sfx_dir):
    photo_sfx, video_sfx = [], []
    if sfx_dir and os.path.isdir(sfx_dir):
        for f in sorted(os.listdir(sfx_dir)):
            low = f.lower()
            full = os.path.join(sfx_dir, f)
            if "photo" in low:
                photo_sfx.append(full)
            if "video" in low:
                video_sfx.append(full)
        log(f"SFX: {len(photo_sfx)} photo, {len(video_sfx)} video effects in {sfx_dir}")
    return photo_sfx, video_sfx


def overlay_sfx(audio_in, clips, start_ms, photo_sfx, video_sfx, gain, work, rng):
    """Mix one transition sound in at each clip's start time. Returns the new
    audio path, or the original if no SFX apply."""
    if not (photo_sfx or video_sfx):
        return audio_in
    import random as _r  # rng passed in for determinism if desired
    log("SFX: assigning transition effects")
    inputs = ["-i", audio_in]
    flt, amix_in, n, idx = "", "[0:a]", 0, 1
    for s, c in enumerate(clips):
        pool = photo_sfx if c.kind == "photo" else video_sfx
        if not pool:
            continue
        sfx = (rng or _r).choice(pool)
        ms = start_ms[s]
        log(f"SFX: seg {s+1} {c.kind} @{ms}ms <- {os.path.basename(sfx)}")
        inputs += ["-i", sfx]
        flt += f"[{idx}:a]adelay={ms}:all=1,volume={gain}[s{idx}];"
        amix_in += f"[s{idx}]"
        idx += 1
        n += 1
    if n == 0:
        return audio_in
    log(f"MIX: overlaying {n} effects")
    flt += f"{amix_in}amix=inputs={n+1}:normalize=0:duration=first[aout]"
    out_path = os.path.join(work, "audio_sfx.m4a")
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", flt,
         "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out_path])
    return out_path


# --------------------------------------------------------------------------- #
# Mux + verify: combine video + audio and confirm the output is TV-safe 4:2:0.
# --------------------------------------------------------------------------- #
def mux_and_verify(video, audio, out_path):
    log("MUX: combining video + audio")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", audio,
         "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest",
         "-movflags", "+faststart", out_path])
    pf, pr = probe_pix_fmt(out_path), probe_profile(out_path)
    if pf == "yuv420p":
        log(f"VERIFY: TV-safe ({pr} / {pf})")
    else:
        log(f"VERIFY: WARNING — pix_fmt '{pf}' (profile '{pr}') is NOT yuv420p; "
            f"may not play on TV/VDPAU")


def add_common_args(p, here):
    """Arguments shared by both front ends."""
    p.add_argument("-s", "--src", default=".", help="source folder (non-recursive)")
    p.add_argument("-o", "--out", default="output.mp4", help="output file")
    p.add_argument("-d", "--dur", type=float, default=4.0,
                   help="seconds per still photo (default 4)")
    p.add_argument("-r", "--res", default="3840x2160",
                   help="output resolution WxH (capped to source; no upscaling)")
    p.add_argument("--fps", type=int, default=30, help="output frame rate")
    p.add_argument("--crf", type=int, default=18,
                   help="quality: lower=better+bigger (default 18)")
    p.add_argument("--sfx", default=os.path.join(here, "sfx"),
                   help="folder of sound effects (files named *photo* / *video*)")
    p.add_argument("--sfx-gain", type=float, default=1.0, help="SFX volume")
    p.add_argument("--fs-only", action="store_true",
                   help="order by filesystem times only (ignore EXIF)")
    p.add_argument("--cache", default=os.path.join(here, "cache"),
                   help="persistent cache dir")
    p.add_argument("--no-cache", action="store_true", help="disable the cache")
    p.add_argument("--tmp", default=os.path.join(here, "tmp"),
                   help="scratch dir (avoid small tmpfs)")


def finalize_common(a):
    """Resolve shared args after parsing."""
    a.width, a.height = (int(x) for x in a.res.lower().split("x"))
    if a.no_cache:
        a.cache = ""
