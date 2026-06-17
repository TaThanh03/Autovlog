# autovlog

Build one chronological video from a folder of mixed photos and videos, ordered by capture date, with optional crossfade transitions and per-type transition sound effects.

`autovlog.sh` is a single Bash script. It reads the oldest timestamp available for each file (filesystem times plus EXIF/QuickTime capture date), sorts chronologically, normalizes every item to one resolution/fps/codec, and concatenates into a single MP4. Photos and videos are both handled in the same pass; audio from clips is preserved, stills get a silent track so concatenation stays clean.

It exists because off-the-shelf slideshow tools sort by filename and ignore capture metadata, and because none of them mix real video clips with stills correctly. Capture order is recovered even when a transfer (e.g. LocalSend) reset the filesystem times.

## Requirements

- `ffmpeg` (with the `xfade` filter and, for `--nvenc`, an NVENC-capable build)
- `exiftool` (`libimage-exiftool-perl`)

```sh
sudo apt install ffmpeg libimage-exiftool-perl
```

HEIC decoding depends on the ffmpeg build. If HEIC fails, use a `libheif`-enabled ffmpeg or convert first with `heif-convert`.

## Install

```sh
git clone https://github.com/TaThanh03/autovlog.git
cd autovlog
chmod +x autovlog.sh
```

## Usage

```sh
./autovlog.sh -s /path/to/media -o out.mp4
```

```sh
./autovlog.sh -s ~/photos -d 3 -r 3840x2160 --fps 30 --nvenc \
  --xfade 0.5 --cache /path/to/cache -o family.mp4
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `-s DIR` | current dir | Source folder (non-recursive). Scans: jpg jpeg png heic webp mp4 mov m4v avi mkv |
| `-o FILE` | `output.mp4` | Output file |
| `-d SECONDS` | `4` | On-screen time per photo |
| `-r WxH` | `1920x1080` | Output resolution. Sources are scaled to fit and letterboxed; aspect kept |
| `--fps N` | `30` | Output frame rate |
| `--nvenc` | off | Use the GPU encoder (`h264_nvenc`). Much faster; slightly lower quality at equal size. Default encoder is `libx264` CRF 20 preset medium |
| `--fs-only` | off | Ignore EXIF; order by filesystem times (atime/mtime/ctime) only |
| `--xfade SECONDS` | `0` | Crossfade duration between every clip. `0` = hard cut. Must be shorter than the shortest clip |
| `--xfade-type NAME` | `fade` | Transition style. See list below |
| `--sfx DIR` | `<script dir>/sfx` | Folder of transition sound-effect files |
| `--sfx-gain G` | `1.0` | Gain applied to sound effects |
| `--cache DIR` | off | Persistent normalized-segment cache |
| `--no-cache` | — | Disable caching |
| `-j N` | `1` | Parallel normalize workers |
| `--tmp DIR` | `<script dir>/tmp` | Scratch location for intermediates |

Full help: `./autovlog.sh --help`

## Ordering

Each file is placed by the oldest timestamp available to it: filesystem atime, mtime, ctime, birthtime, plus EXIF `DateTimeOriginal` / `CreateDate` / `MediaCreateDate` / `CreationDate`. Capture date is normally the oldest, so true shooting order is recovered. `--fs-only` restricts to filesystem times.

## Transitions

`--xfade` / `--xfade-type` use ffmpeg's built-in `xfade` filter. No files or downloads required. Common values:

```
fade fadeblack fadewhite dissolve pixelize radial
wipeleft wiperight wipeup wipedown
slideleft slideright slideup slidedown
smoothleft smoothright circleopen circleclose zoomin
```

Full list for your build: `ffmpeg -h filter=xfade`

## Sound effects

No audio is bundled. Supply your own files in the `sfx/` folder (or a `--sfx DIR`), named so they contain `photo` or `video` (e.g. `photo_chime.wav`, `video_whoosh.wav`). The matching effect plays on each transition into a still or a clip. If the folder is empty or missing, the video is built silently with no transition sounds; nothing breaks.

## Cache

`--cache DIR` persists normalized segments keyed by source path + size + mtime + resolution/fps/encoder/crf (plus display time for photos). Reruns skip re-encoding anything unchanged, which is the main speedup when iterating on `--xfade`, `--xfade-type`, or `--sfx`.

- Put the cache on persistent storage so it survives reboots.
- Editing a source file (changed size or mtime) rebuilds only that clip.
- Changing resolution/fps/encoder/crf rebuilds everything.
- The cache grows unbounded; it does not prune stale entries. Delete the directory to force a clean rebuild.

## Performance

- 4K is slow on modest hardware. A full 4K run can take hours; 1080p renders in minutes. Use 4K only when you intend to leave the machine running.
- `-j` parallelism mostly helps when the set is many cheap stills. For video-heavy folders the gain is small, and combining `-j` with `--nvenc` risks the encoder session cap on consumer GPUs. Leave it at `1` unless the folder is mostly stills, then `-j 3` with `libx264`.
- Encoding is the bottleneck, not scratch I/O. Avoid pointing `--tmp` at a small tmpfs `/tmp`; intermediates can exceed available RAM.

## License

MIT. See [LICENSE](LICENSE).
