#!/usr/bin/env python3
"""
lazyvlog.py — HIGH-QUALITY keeper build.

Chronological vlog from a media folder, with TRUE frame-exact crossfades
(libx264 by default). Bodies are stream-copied; only the short crossfade
windows are re-encoded. Output is TV-safe (H.264 High / yuv420p / auto level).

This is the slow, best-looking tool. For a fast GPU draft use lazyvlogfast.py.
Shared machinery lives in lazyvlog_core.py.
"""

import argparse
import os
import random
import shutil
import signal
import sys

import lazyvlog_core as C

# One fixed transition or a random pick from this pool (libx264/CPU xfade).
XPOOL = ["fade", "fadeblack", "dissolve", "wipeleft", "wiperight", "wipeup",
         "wipedown", "slideleft", "slideright", "slideup", "slidedown",
         "smoothleft", "smoothright", "smoothup", "smoothdown",
         "circleopen", "circleclose", "radial", "pixelize"]


# --------------------------------------------------------------------------- #
# Normalize one clip to the uniform format (cached; reuses legacy bash cache).
# --------------------------------------------------------------------------- #
def normalize_one(c, a, vopts, cache_dir, work_dir):
    st = os.stat(c.src)
    extra = f"d{a.dur}" if c.kind == "photo" else ""
    key = C.sha(os.path.abspath(c.src), st.st_size, int(st.st_mtime),
                a.width, a.height, a.fps, "nvenc" if a.nvenc else "x264",
                a.crf, extra)
    dest_dir = cache_dir if cache_dir else work_dir
    c.norm = os.path.join(dest_dir, f"norm_{key}.mp4")

    if os.path.exists(c.norm) and os.path.getsize(c.norm) > 0:
        C.log(f"NORMALIZE: cache hit  {os.path.basename(c.src)}")
        return
    if cache_dir:  # adopt an old bash-cache clip if it's genuinely 4:2:0
        legacy = os.path.join(cache_dir, f"{C.legacy_bash_key(c, a)}.mp4")
        if os.path.exists(legacy) and os.path.getsize(legacy) > 0:
            if C.probe_pix_fmt(legacy) in ("yuv420p", "yuvj420p"):
                try:
                    os.link(legacy, c.norm)
                except OSError:
                    shutil.copyfile(legacy, c.norm)
                C.log(f"NORMALIZE: reused legacy cache  {os.path.basename(c.src)}")
                return
            C.log(f"NORMALIZE: legacy entry for {os.path.basename(c.src)} "
                  f"not 4:2:0 — re-encoding")

    sz = f"{st.st_size // (1024*1024)}M"
    C.log(f"NORMALIZE: building {c.kind} {os.path.basename(c.src)} ({sz})")
    vf = C.scale_filter(a)
    if c.kind == "photo":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
               "-framerate", str(a.fps), "-t", str(a.dur), "-i", c.src,
               "-f", "lavfi", "-t", str(a.dur),
               "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-movflags", "+faststart", c.norm]
    elif C.has_audio(c.src):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", c.src, "-vf", vf,
               *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
               "-movflags", "+faststart", c.norm]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", c.src, "-f", "lavfi",
               "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-movflags", "+faststart", c.norm]
    C.run(cmd)


# --------------------------------------------------------------------------- #
# Trim one frame-exact piece (body/head/tail). Bodies are cached.
# --------------------------------------------------------------------------- #
def trim_piece(c, role, sframe, eframe, a, vopts, cache_dir, work_dir):
    if eframe is not None:
        expr, endtag = f"trim=start_frame={sframe}:end_frame={eframe}", eframe
    else:
        expr, endtag = f"trim=start_frame={sframe}", "end"
    if role == "body" and cache_dir:
        key = C.sha(os.path.basename(c.norm), role, sframe, endtag)
        path = os.path.join(cache_dir, f"piece_{key}.mp4")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    else:
        path = os.path.join(work_dir, f"{role}_{C.sha(c.norm, sframe, endtag)}.mp4")
    flt = f"[0:v]{expr},setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v]"
    C.run(["ffmpeg", "-y", "-loglevel", "error", "-i", c.norm,
           "-filter_complex", flt, "-map", "[v]", "-an", *vopts,
           "-movflags", "+faststart", path])
    return path


# --------------------------------------------------------------------------- #
# One transition window: crossfade tail[i] into head[i+1] (re-encode).
# --------------------------------------------------------------------------- #
def make_window(tail, head, xtype, a, vopts, work_dir, idx, rng):
    xt = rng.choice(XPOOL) if xtype == "random" else xtype
    win = os.path.join(work_dir, f"win_{idx}.mp4")
    flt = (f"[0:v]format=yuv420p,setsar=1,fps={a.fps}[x];"
           f"[1:v]format=yuv420p,setsar=1,fps={a.fps}[y];"
           f"[x][y]xfade=transition={xt}:duration={a.xfade}:offset=0,"
           f"format=yuv420p[v]")
    C.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tail, "-i", head,
           "-filter_complex", flt, "-map", "[v]", "-an", *vopts,
           "-movflags", "+faststart", win])
    return win, xt


def parse_args(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="High-quality vlog with true crossfades.")
    C.add_common_args(p, here)
    p.add_argument("--nvenc", action="store_true",
                   help="use GPU encoder (faster, lower quality)")
    p.add_argument("--preset", default="slow", help="libx264 preset (default slow)")
    p.add_argument("--xfade", type=float, default=0.5,
                   help="crossfade seconds (0 = hard cut)")
    p.add_argument("--xfade-type", default="random",
                   help="'random' or a fixed name (fade, dissolve, ...)")
    p.add_argument("--seed", type=int, default=None,
                   help="seed for reproducible random transitions/SFX")
    p.add_argument("-j", "--jobs", type=int, default=0,
                   help="parallel jobs (default 1 for nvenc, 3 for libx264)")
    a = p.parse_args(argv)
    C.finalize_common(a)
    if a.jobs <= 0:
        a.jobs = 1 if a.nvenc else 3
    return a


def main(argv):
    signal.signal(signal.SIGINT, C.on_sigint)
    a = parse_args(argv)
    rng = random.Random(a.seed)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"missing: {tool} (sudo apt install ffmpeg)")
    have_exiftool = bool(shutil.which("exiftool"))
    if not a.fs_only and not have_exiftool:
        C.log("WARN: exiftool not found — ordering by filesystem times only")

    work = C.setup_dirs(a)
    files = C.scan_media(a.src)                                   # STEP 1
    clips = C.date_and_sort(files, a.fs_only, have_exiftool)      # STEP 2
    C.survey_and_cap(clips, a)                                    # STEP 1.5
    vopts = C.video_opts(a)
    photo_sfx, video_sfx = C.find_sfx_pools(a.sfx)
    N = len(clips)

    # STEP 3: normalize (parallel).
    C.log(f"NORMALIZE: {N} clips to {a.width}x{a.height}@{a.fps} "
          f"via {'nvenc' if a.nvenc else 'libx264'} (-j {a.jobs}"
          f"{', cache on' if a.cache else ''})")
    if a.nvenc and a.jobs > 1:
        C.log(f"WARN: -j {a.jobs} with NVENC may exceed the GPU session limit")
    C.parallel(a.jobs, [lambda c=c: normalize_one(c, a, vopts, a.cache, work)
                        for c in clips])
    for c in clips:
        c.dur = C.probe_duration(c.norm)
        c.frames = round(c.dur * a.fps)

    # Clamp xfade < half the shortest clip (interior clips fade at both ends).
    if a.xfade > 0:
        min_d = min(c.dur for c in clips)
        if 2 * a.xfade >= min_d:
            new_x = round(min_d * 0.25, 3)
            if new_x > 0:
                C.log(f"WARN: --xfade {a.xfade} too long for shortest clip "
                      f"{min_d:.3f}s; reducing to {new_x}s")
                a.xfade = new_x
            else:
                C.log("WARN: shortest clip too short for crossfade; hard cuts")
                a.xfade = 0.0

    # Per-clip on-screen start time (ms) for SFX placement.
    start_ms, acc = [], 0.0
    for i, c in enumerate(clips):
        if i == 0:
            start_ms.append(0); acc = c.dur
        else:
            start_ms.append(max(0, int((acc - a.xfade) * 1000)))
            acc += c.dur - a.xfade

    video = os.path.join(work, "video.mp4")
    audio = os.path.join(work, "audio.m4a")
    XF = round(a.xfade * a.fps)

    if a.xfade > 0 and N >= 2:
        C.log(f"TRANSITION: windowed xfade '{a.xfade_type}' {a.xfade}s "
              f"(re-encode only {XF}-frame windows)")

        def make_pieces(i):
            c = clips[i]
            if i == 0:
                c.body = trim_piece(c, "body", 0, c.frames - XF, a, vopts, a.cache, work)
                c.tail = trim_piece(c, "tail", c.frames - XF, None, a, vopts, a.cache, work)
            elif i == N - 1:
                c.head = trim_piece(c, "head", 0, XF, a, vopts, a.cache, work)
                c.body = trim_piece(c, "body", XF, None, a, vopts, a.cache, work)
            else:
                c.head = trim_piece(c, "head", 0, XF, a, vopts, a.cache, work)
                c.body = trim_piece(c, "body", XF, c.frames - XF, a, vopts, a.cache, work)
                c.tail = trim_piece(c, "tail", c.frames - XF, None, a, vopts, a.cache, work)
            C.log(f"TRIM: [{i+1}/{N}] {clips[i].kind} pieces ready")

        C.parallel(a.jobs, [lambda i=i: make_pieces(i) for i in range(N)])

        windows = [None] * (N - 1)
        # Pre-pick transition types with the seeded rng (thread-safe ordering).
        types = [(a.xfade_type if a.xfade_type != "random" else rng.choice(XPOOL))
                 for _ in range(N - 1)]

        def build_window(i):
            win, _ = make_window(clips[i].tail, clips[i + 1].head, types[i],
                                 a, vopts, work, i, rng)
            windows[i] = win
            C.log(f"TRANSITION: window {i+1}/{N-1} -> {types[i]}")

        C.parallel(a.jobs, [lambda i=i: build_window(i) for i in range(N - 1)])

        vlist = os.path.join(work, "vlist.txt")
        with open(vlist, "w") as fh:
            for i, c in enumerate(clips):
                fh.write(f"file '{c.body}'\n")
                if i < N - 1:
                    fh.write(f"file '{windows[i]}'\n")
        C.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", vlist, "-an", "-c", "copy", "-movflags", "+faststart", video])

        C.log(f"AUDIO: single-pass acrossfade over {N} clips")
        ains, af, pa = [], "", "[0:a]"
        for c in clips:
            ains += ["-i", c.norm]
        for k in range(1, N):
            af += f"{pa}[{k}:a]acrossfade=d={a.xfade}:c1=tri:c2=tri[ax{k}];"
            pa = f"[ax{k}]"
        af = af.rstrip(";")
        C.run(["ffmpeg", "-y", "-loglevel", "error", *ains, "-filter_complex", af,
               "-map", pa, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", audio])
    else:
        C.log(f"TRANSITION: hard cuts, concat {N} clips")
        vlist = os.path.join(work, "vlist.txt")
        with open(vlist, "w") as fh:
            for c in clips:
                fh.write(f"file '{c.norm}'\n")
        C.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", vlist, "-an", "-c", "copy", "-movflags", "+faststart", video])
        ains, ac = [], ""
        for i, c in enumerate(clips):
            ains += ["-i", c.norm]; ac += f"[{i}:a]"
        C.run(["ffmpeg", "-y", "-loglevel", "error", *ains, "-filter_complex",
               f"{ac}concat=n={N}:v=0:a=1[a]", "-map", "[a]", "-c:a", "aac",
               "-b:a", "192k", "-ar", "48000", audio])

    final_audio = C.overlay_sfx(audio, clips, start_ms, photo_sfx, video_sfx,
                                a.sfx_gain, work, rng)
    C.mux_and_verify(video, final_audio, a.out)
    shutil.rmtree(work, ignore_errors=True)
    C.log(f"DONE: {a.out}")
    print(f"done: {a.out}")


if __name__ == "__main__":
    main(sys.argv[1:])
