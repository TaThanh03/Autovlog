#!/usr/bin/env python3
"""
lazyvlogfast.py — FAST GPU draft build.

Chronological vlog from a media folder, assembled fast using the GPU:
HARDWARE decode (NVDEC) + HARDWARE encode (NVENC), with simple DIP-TO-BLACK
transitions instead of crossfades. Each clip independently fades up from black
at its start and down to black at its end; clips are then concatenated by
stream copy (black-to-black joins are invisible). No xfade filter, no chunking,
no cross-clip blending -> no VRAM ceiling, no OOM.

This trades the crossfade look and libx264 quality for speed. For the
high-quality keeper with true crossfades, use lazyvlog.py.

  --cpu   run the identical pipeline on libx264 (no GPU) — useful for testing
          or on machines without NVENC.

Shared machinery lives in lazyvlog_core.py.
"""

import argparse
import os
import shutil
import signal
import sys

import lazyvlog_core as C


def process_clip(c, a, vopts, cache_dir, work_dir):
    """
    Produce one faded clip: scaled/padded to the target, with a fade-in from
    black at the start and a fade-out to black at the end (the dip-to-black).
    Hardware-decoded (NVDEC) and hardware-encoded (NVENC) unless --cpu.
    Cached on disk.
    """
    st = os.stat(c.src)
    mode = "cpu" if a.cpu else "nvenc"
    extra = f"d{a.dur}" if c.kind == "photo" else ""
    key = C.sha("fast", os.path.abspath(c.src), st.st_size, int(st.st_mtime),
                a.width, a.height, a.fps, mode, a.crf, a.fade, extra)
    dest_dir = cache_dir if cache_dir else work_dir
    c.proc = os.path.join(dest_dir, f"fast_{key}.mp4")
    if os.path.exists(c.proc) and os.path.getsize(c.proc) > 0:
        C.log(f"PROCESS: cache hit  {os.path.basename(c.src)}")
        c.dur = C.probe_duration(c.proc)
        return

    # Determine output duration so the fade-out can start at (dur - fade).
    out_dur = a.dur if c.kind == "photo" else C.probe_duration(c.src)
    fade = min(a.fade, max(0.0, out_dur / 2 - 0.05))  # never overlap the fades

    # Scale/pad/fps/pixfmt, then fade in and out (dip to black).
    vf = C.scale_filter(a)
    if fade > 0:
        vf += (f",fade=t=in:st=0:d={fade}"
               f",fade=t=out:st={max(0, out_dur - fade):.3f}:d={fade}")

    sz = f"{st.st_size // (1024*1024)}M"
    C.log(f"PROCESS: building {c.kind} {os.path.basename(c.src)} ({sz})")

    # Hardware decode for real videos (NVDEC). Photos are single images, decoded
    # on CPU (cheap) regardless.
    hwdec = [] if (a.cpu or c.kind == "photo") else ["-hwaccel", "cuda"]

    if c.kind == "photo":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
               "-framerate", str(a.fps), "-t", str(a.dur), "-i", c.src,
               "-f", "lavfi", "-t", str(a.dur),
               "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-movflags", "+faststart", c.proc]
    elif C.has_audio(c.src):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *hwdec, "-i", c.src,
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-ac", "2", "-movflags", "+faststart", c.proc]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *hwdec, "-i", c.src,
               "-f", "lavfi",
               "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", vf, *vopts, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-movflags", "+faststart", c.proc]
    C.run(cmd)
    c.dur = C.probe_duration(c.proc)


def parse_args(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(
        description="Fast GPU vlog with dip-to-black transitions (NVENC).")
    C.add_common_args(p, here)
    p.add_argument("--fade", type=float, default=0.5,
                   help="dip-to-black seconds at each clip end (default 0.5)")
    p.add_argument("--cpu", action="store_true",
                   help="run on libx264 instead of GPU (testing / no-NVENC machines)")
    p.add_argument("-j", "--jobs", type=int, default=0,
                   help="parallel jobs (default 2 for nvenc, 3 for --cpu)")
    a = p.parse_args(argv)
    C.finalize_common(a)
    # NVENC unless --cpu. (lazyvlog_core.video_opts switches on a.nvenc.)
    a.nvenc = not a.cpu
    a.preset = "medium"  # only used if --cpu
    if a.jobs <= 0:
        a.jobs = 3 if a.cpu else 2
    return a


def main(argv):
    signal.signal(signal.SIGINT, C.on_sigint)
    a = parse_args(argv)
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

    # STEP 3: process every clip (scale + dip-to-black + encode), in parallel.
    C.log(f"PROCESS: {N} clips to {a.width}x{a.height}@{a.fps} "
          f"via {'libx264' if a.cpu else 'nvenc'} (-j {a.jobs}"
          f"{', cache on' if a.cache else ''}); dip {a.fade}s")
    if not a.cpu and a.jobs > 2:
        C.log(f"WARN: -j {a.jobs} with NVENC may exceed the GPU session limit")
    C.parallel(a.jobs, [lambda c=c: process_clip(c, a, vopts, a.cache, work)
                        for c in clips])

    # Timeline: clips play back-to-back (no overlap), so each clip's start time
    # is the running sum of durations. SFX fire at these boundaries (the dips).
    start_ms, acc = [], 0.0
    for c in clips:
        start_ms.append(int(acc * 1000))
        acc += c.dur

    # STEP 4: concat video (copy) — black-to-black joins are seamless.
    video = os.path.join(work, "video.mp4")
    audio = os.path.join(work, "audio.m4a")
    C.log(f"CONCAT: joining {N} dip-to-black clips")
    vlist = os.path.join(work, "vlist.txt")
    with open(vlist, "w") as fh:
        for c in clips:
            fh.write(f"file '{c.proc}'\n")
    C.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", vlist, "-an", "-c", "copy", "-movflags", "+faststart", video])

    # Audio: concatenate clip audio (cuts at the black dips, which reads fine).
    ains, ac = [], ""
    for i, c in enumerate(clips):
        ains += ["-i", c.proc]; ac += f"[{i}:a]"
    C.run(["ffmpeg", "-y", "-loglevel", "error", *ains, "-filter_complex",
           f"{ac}concat=n={N}:v=0:a=1[a]", "-map", "[a]", "-c:a", "aac",
           "-b:a", "192k", "-ar", "48000", audio])

    # STEP 5: SFX, mux, verify.
    import random
    final_audio = C.overlay_sfx(audio, clips, start_ms, photo_sfx, video_sfx,
                                a.sfx_gain, work, random.Random())
    C.mux_and_verify(video, final_audio, a.out)
    shutil.rmtree(work, ignore_errors=True)
    C.log(f"DONE: {a.out}")
    print(f"done: {a.out}")


if __name__ == "__main__":
    main(sys.argv[1:])
