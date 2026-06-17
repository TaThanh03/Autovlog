#!/usr/bin/env bash
# automovie.sh — chronological video from mixed photos/videos, with optional
# visual crossfade transitions and per-type transition sound effects.
#
# Ordering: oldest timestamp across atime, mtime, ctime, birthtime, and
# EXIF/QuickTime capture date (capture date wins when present). --fs-only
# restricts to the three filesystem times only.
#
# Visual transitions: hard cut by default; --xfade SECONDS enables an xfade
# crossfade of the given type (--xfade-type) between every segment.
#
# Sound effects: at the start of each segment (the transition point) a sound
# effect is mixed in, chosen by segment type:
#   photo -> random file matching *photo* in the SFX repo
#   video -> random file matching *video* in the SFX repo
set -euo pipefail

# ---- defaults ----
SRC="."
OUT="output.mp4"
DUR=4                    # seconds per still image
W=3840; H=2160; FPS=30   # 4K, tuned for Samsung The Frame 2021 (override -r)
VENC="libx264"           # --nvenc -> h264_nvenc (GTX 1660 Ti)
CRF=18                   # high quality (override --crf)
PRESET="slow"            # better compression at equal size (override --preset)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SFXDIR="$SCRIPT_DIR/sfx"  # --sfx DIR ; defaults to <script dir>/sfx
SFXGAIN=1.0              # --sfx-gain
FSONLY=0                 # --fs-only
XFADE=0.5                # --xfade SECONDS ; 0 = hard cut. Auto-falls to 25% of
                         #   the shortest clip if too long, then to hard cut if
                         #   that rounds to 0.
XTYPE="random"           # --xfade-type: 'random' (per-join), or a fixed name
                         #   (fade dissolve wipeleft slideright circleopen ...)
CACHE="$SCRIPT_DIR/cache" # --cache DIR override; --no-cache to disable
JOBS=1                   # -j N ; parallel normalize workers (1 = serial)
TMPBASE="$SCRIPT_DIR/tmp" # --tmp DIR ; scratch location (avoid small tmpfs /tmp)

usage(){
cat >&2 <<'HELP'
automovie.sh — build one chronological video from a folder of mixed photos and
videos, ordered by capture date, with optional crossfade transitions and
per-type transition sound effects.

USAGE
  automovie.sh [options]

ORDERING
  Each file is placed by the OLDEST timestamp available to it: filesystem
  atime, mtime, ctime, birthtime, plus EXIF/QuickTime capture date. Capture
  date is normally the oldest, so true shooting order is recovered even when a
  transfer (e.g. LocalSend) reset the filesystem times. --fs-only restricts to
  atime/mtime/ctime if you specifically want that.

INPUT / OUTPUT
  -s DIR            Source folder (non-recursive). Default: current dir.
                    Scans: jpg jpeg png heic webp mp4 mov m4v avi mkv
  -o FILE           Output file. Default: output.mp4
  --fs-only         Ignore EXIF; order by filesystem times only.

STILLS
  -d SECONDS        On-screen time per photo. Default: 4

VIDEO FORMAT
  -r WxH            Output resolution. Default: 3840x2160 (4K, for The Frame)
                    Sources are scaled to fit and letterboxed; aspect kept.
  --fps N           Output frame rate. Default: 30

ENCODER
  (default)         libx264, CRF 18, preset slow. Always works.
  --nvenc           Use the GPU encoder (h264_nvenc). Much faster on the
                    GTX 1660 Ti. LOWER quality per bitrate than libx264 —
                    omit it for the best-looking output.
  --crf N           Quality target. Lower = better + bigger file. Default 18.
                    18 = near-transparent, 16 = near-lossless, 23 = smaller.
  --preset NAME     libx264 speed/efficiency: ultrafast..veryslow. Default
                    medium. Slower = more quality at the same file size.

TRANSITIONS (visual) — built into ffmpeg, no files needed
  --xfade SECONDS   Crossfade duration between every clip. Default: 0.5
                    (0 = hard cut). If >= the shortest clip, it is auto-reduced
                    to 25% of the shortest clip with a warning; if that rounds
                    to 0, it falls back to hard cuts. Never aborts.
  --xfade-type NAME Transition style. Default: random (a different transition
                    chosen per join from a curated set). Pass a fixed name to
                    use one style everywhere. Common values:
                    fade fadeblack fadewhite dissolve pixelize radial
                    wipeleft wiperight wipeup wipedown
                    slideleft slideright slideup slidedown
                    smoothleft smoothright circleopen circleclose zoomin
                    Full list for your build: ffmpeg -h filter=xfade

SOUND EFFECTS (audio) — you supply the files; none are bundled
  --sfx DIR         Folder of sound-effect files. Default: <script dir>/sfx
                    A sound is mixed in at the start of each clip, chosen by
                    type from filenames containing the keyword:
                      photo segment -> random file matching *photo*
                      video segment -> random file matching *video*
                    Missing/empty folder = no sound, built silently.
  --sfx-gain G      Volume multiplier for injected effects. Default: 1.0

PERFORMANCE
  --cache DIR       Persist normalized clips here and reuse them on later runs.
                    Default: <script dir>/cache (always on). Skips re-encoding
                    unchanged sources — the big win when re-rendering only to
                    tweak --xfade or --sfx. Invalidated per-file by source
                    size/mtime/path; globally by -r/--fps/encoder/crf (and -d
                    for photos). Grows unbounded; delete the folder to reset.
  --no-cache        Disable the cache; normalize into a temp dir each run.
  --tmp DIR         Scratch dir for intermediates. Default: <script dir>/tmp.
                    Do NOT use a small tmpfs /tmp for 4K — it will fill up.
  -j N              Parallel normalize workers. Default: 1. Helps only for
                    photo-heavy sets with libx264. With --nvenc, N>1 may exceed
                    the GPU's encode-session limit — leave at 1.

OTHER
  -h, --help        This help.

DEPENDENCIES
  ffmpeg, ffprobe (sudo apt install ffmpeg)
  exiftool (sudo apt install libimage-exiftool-perl) unless --fs-only

EXAMPLES
  # simplest: photos+videos in this folder, in capture order
  automovie.sh -s /mnt/Maxtor/Localsend -o trip.mp4

  # GPU encode, half-second dissolves, sounds from ./sfx
  automovie.sh -s ./media -o trip.mp4 --nvenc --xfade 0.5 --xfade-type dissolve

  # iterate fast: cache normalized clips so reruns skip re-encoding
  automovie.sh -s ./media -o v1.mp4 --nvenc --cache /mnt/data/automovie-cache
  automovie.sh -s ./media -o v2.mp4 --nvenc --cache /mnt/data/automovie-cache \
    --xfade 0.7 --xfade-type circleopen   # only re-renders the timeline

PROGRESS
  Stage logs print to stderr with elapsed seconds; redirect with 2>run.log.
HELP
exit "${1:-1}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -s) SRC="$2"; shift 2;;
    -o) OUT="$2"; shift 2;;
    -d) DUR="$2"; shift 2;;
    -r) W="${2%x*}"; H="${2#*x}"; shift 2;;
    --fps) FPS="$2"; shift 2;;
    --nvenc) VENC="h264_nvenc"; shift;;
    --sfx) SFXDIR="$2"; shift 2;;
    --sfx-gain) SFXGAIN="$2"; shift 2;;
    --fs-only) FSONLY=1; shift;;
    --xfade) XFADE="$2"; shift 2;;
    --xfade-type) XTYPE="$2"; shift 2;;
    --cache) CACHE="$2"; shift 2;;
    --no-cache) CACHE=""; shift;;
    --tmp) TMPBASE="$2"; shift 2;;
    --crf) CRF="$2"; shift 2;;
    --preset) PRESET="$2"; shift 2;;
    -j) JOBS="$2"; shift 2;;
    -h|--help) usage 0;;
    *) echo "unknown arg: $1" >&2; usage 1;;
  esac
done

[ -d "$SFXDIR" ] || SFXDIR=""   # silently disable SFX if the dir is absent
[ -n "$CACHE" ] && mkdir -p "$CACHE"
[[ "$JOBS" =~ ^[0-9]+$ ]] && [ "$JOBS" -ge 1 ] || JOBS=1

mkdir -p "$TMPBASE"
WORK="$(mktemp -d "$TMPBASE/automovie.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
# scratch space sanity: warn loudly if the chosen tmp has little room
avail_gb=$(df -BG --output=avail "$WORK" 2>/dev/null | tail -1 | tr -dc '0-9')
fstype=$(df --output=fstype "$WORK" 2>/dev/null | tail -1 | tr -d ' ')
if [ "${avail_gb:-0}" -lt 20 ]; then
  echo "warn: scratch dir $WORK has only ${avail_gb:-?}G free (${fstype})." >&2
  echo "warn: 4K renders need tens of GB. Use --tmp /path/on/a/big/disk if this fails." >&2
fi
[ "$fstype" = tmpfs ] && echo "warn: scratch is on tmpfs (RAM) — large renders may exhaust it; use --tmp" >&2

command -v ffmpeg  >/dev/null || { echo "missing: ffmpeg (sudo apt install ffmpeg)"; exit 1; }
command -v ffprobe >/dev/null || { echo "missing: ffprobe (ships with ffmpeg)"; exit 1; }
[ "$FSONLY" -eq 1 ] || command -v exiftool >/dev/null || { echo "missing: exiftool (sudo apt install libimage-exiftool-perl) — or use --fs-only"; exit 1; }

# H.264 level: lowest that covers the chosen resolution+fps (best TV compat).
pixels=$(( W * H )); fint=${FPS%%.*}
if   [ "$pixels" -le $((1280*720)) ];  then [ "$fint" -le 30 ] && LEVEL=3.1 || LEVEL=3.2
elif [ "$pixels" -le $((1920*1088)) ]; then [ "$fint" -le 30 ] && LEVEL=4.0 || LEVEL=4.2
elif [ "$pixels" -le $((2560*1440)) ]; then [ "$fint" -le 30 ] && LEVEL=5.0 || LEVEL=5.1
elif [ "$pixels" -le $((3840*2160)) ]; then [ "$fint" -le 30 ] && LEVEL=5.1 || LEVEL=5.2
else LEVEL=5.2; fi

if [ "$VENC" = "h264_nvenc" ]; then
  VOPTS=(-c:v h264_nvenc -preset p5 -rc vbr -cq "$CRF" -b:v 0 -pix_fmt yuv420p -profile:v high -level:v "$LEVEL")
else
  VOPTS=(-c:v libx264 -preset "$PRESET" -crf "$CRF" -pix_fmt yuv420p -profile:v high -level:v "$LEVEL" -x264-params "input-csp=i420")
fi
VF="scale=${W}:${H}:force_original_aspect_ratio=decrease:flags=lanczos,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=${FPS},format=yuv420p"

T0=$(date +%s)
log(){ printf '[%4ds] %s\n' "$(( $(date +%s) - T0 ))" "$*" >&2; }

# Confirm the finished file is TV-safe (8-bit 4:2:0 H.264). Warns if not.
verify_out(){
  local pf pr
  pf=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$OUT" 2>/dev/null)
  pr=$(ffprobe -v error -select_streams v:0 -show_entries stream=profile -of csv=p=0 "$OUT" 2>/dev/null)
  if [ "$pf" = yuv420p ]; then
    log "VERIFY: TV-safe (${pr:-?} / $pf)"
  else
    log "VERIFY: WARNING — pix_fmt is '$pf' (profile '${pr:-?}'), NOT yuv420p"
    log "VERIFY: this may not play on TV/VDPAU hardware decoders"
  fi
}

# ---- oldest-timestamp date key (epoch seconds) ----
get_date(){
  local f="$1" cands=() e min="" A M C B
  read -r A M C B < <(stat -c '%X %Y %Z %W' "$f")
  cands+=("$A" "$M" "$C")
  [[ "${B:-0}" =~ ^[0-9]+$ ]] && [ "${B:-0}" -gt 0 ] && cands+=("$B")
  if [ "$FSONLY" -eq 0 ]; then
    for tag in DateTimeOriginal CreateDate MediaCreateDate CreationDate; do
      e=$(exiftool -s3 -d %s "-$tag" "$f" 2>/dev/null | head -n1)
      [[ "$e" =~ ^[0-9]+$ ]] && cands+=("$e")
    done
  fi
  for e in "${cands[@]}"; do
    [[ "$e" =~ ^[0-9]+$ ]] || continue
    [ "$e" -le 0 ] && continue
    { [ -z "$min" ] || [ "$e" -lt "$min" ]; } && min="$e"
  done
  echo "${min:-0}"
}

# ---- collect + sort ----
log "SCAN: searching $SRC for media"
mapfile -d '' FILES < <(find "$SRC" -maxdepth 1 -type f \
  \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' -o -iname '*.webp' \
   -o -iname '*.mp4' -o -iname '*.mov' -o -iname '*.m4v' -o -iname '*.avi' -o -iname '*.mkv' \) -print0)
[ "${#FILES[@]}" -gt 0 ] || { echo "no media found in $SRC"; exit 1; }
log "SCAN: ${#FILES[@]} files found"

log "DATE: reading timestamps$([ "$FSONLY" -eq 1 ] && echo ' (fs-only)')"
declare -a KEYED=()
k=0
for f in "${FILES[@]}"; do
  k=$((k+1)); log "DATE: [$k/${#FILES[@]}] ${f##*/}"
  KEYED+=("$(get_date "$f")"$'\t'"$f")
done
IFS=$'\n' SORTED=($(printf '%s\n' "${KEYED[@]}" | sort -n)); unset IFS
log "DATE: sorted chronologically"

# ---- SFX pools ----
declare -a PHOTO_SFX=() VIDEO_SFX=()
if [ -n "$SFXDIR" ]; then
  shopt -s nullglob nocaseglob
  PHOTO_SFX=( "$SFXDIR"/*photo* )
  VIDEO_SFX=( "$SFXDIR"/*video* )
  shopt -u nullglob nocaseglob
  log "SFX: ${#PHOTO_SFX[@]} photo, ${#VIDEO_SFX[@]} video effects in $SFXDIR"
  [ "${#PHOTO_SFX[@]}" -eq 0 ] && echo "warn: no *photo* SFX in $SFXDIR"
  [ "${#VIDEO_SFX[@]}" -eq 0 ] && echo "warn: no *video* SFX in $SFXDIR"
fi

# ---- encode worker (one item -> uniform mp4 at $seg) ----
encode_one(){
  local f="$1" type="$2" seg="$3"
  if [ "$type" = photo ]; then
    ffmpeg -y -loglevel error -loop 1 -framerate "$FPS" -t "$DUR" -i "$f" \
      -f lavfi -t "$DUR" -i anullsrc=channel_layout=stereo:sample_rate=48000 \
      -vf "$VF" "${VOPTS[@]}" -c:a aac -b:a 128k -ar 48000 \
      -map 0:v -map 1:a -shortest -movflags +faststart "$seg" \
      || { echo "ENCODE FAIL: $f" >&2; rm -f "$seg"; return 1; }
  else
    if ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$f" | grep -q .; then
      ffmpeg -y -loglevel error -i "$f" -vf "$VF" "${VOPTS[@]}" \
        -c:a aac -b:a 128k -ar 48000 -ac 2 -movflags +faststart "$seg" \
        || { echo "ENCODE FAIL: $f" >&2; rm -f "$seg"; return 1; }
    else
      ffmpeg -y -loglevel error -i "$f" \
        -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
        -vf "$VF" "${VOPTS[@]}" -c:a aac -b:a 128k -ar 48000 \
        -map 0:v -map 1:a -shortest -movflags +faststart "$seg" \
        || { echo "ENCODE FAIL: $f" >&2; rm -f "$seg"; return 1; }
    fi
  fi
}

# ---- plan segments (resolve cache hits/misses, keep input order) ----
log "ENCODE: normalizing ${#SORTED[@]} clips to ${W}x${H}@${FPS} via $VENC (-j $JOBS$([ -n "$CACHE" ] && echo ', cache on'))"
[ "$JOBS" -gt 1 ] && [ "$VENC" = h264_nvenc ] && echo "warn: -j $JOBS with NVENC may hit the GPU session limit"
declare -a SEG=() SEGTYPE=() SEGDUR=()
declare -a TODO_f=() TODO_t=() TODO_s=()
i=0; hits=0
for line in "${SORTED[@]}"; do
  f="${line#*$'\t'}"
  ext="${f##*.}"; ext="${ext,,}"
  case "$ext" in jpg|jpeg|png|heic|webp) type=photo;; *) type=video;; esac
  if [ -n "$CACHE" ]; then
    abs="$(readlink -f "$f")"
    read -r fsz fmt < <(stat -c '%s %Y' "$f")
    extra=""; [ "$type" = photo ] && extra="d$DUR"
    sig=$(printf '%s|%s|%s|%s|%s|%s|%s|%s|%s' "$abs" "$fsz" "$fmt" "$W" "$H" "$FPS" "$VENC" "$CRF" "$extra" | sha1sum | cut -c1-16)
    seg="$CACHE/${sig}.mp4"
  else
    seg="$WORK/$(printf '%05d' "$i").mp4"
  fi
  SEG+=("$seg"); SEGTYPE+=("$type")
  if [ -s "$seg" ]; then
    hits=$((hits+1)); log "ENCODE: [$((i+1))/${#SORTED[@]}] cache hit  ${f##*/}"
  else
    TODO_f+=("$f"); TODO_t+=("$type"); TODO_s+=("$seg")
  fi
  i=$((i+1))
done
[ -n "$CACHE" ] && log "ENCODE: $hits cached, ${#TODO_f[@]} to build"

# ---- dispatch encodes with bounded parallelism ----
running=0
for k in "${!TODO_s[@]}"; do
  log "ENCODE: building ${TODO_t[$k]} ${TODO_f[$k]##*/} ($(du -h "${TODO_f[$k]}" | cut -f1))"
  encode_one "${TODO_f[$k]}" "${TODO_t[$k]}" "${TODO_s[$k]}" &
  running=$((running+1))
  if [ "$running" -ge "$JOBS" ]; then wait -n; running=$((running-1)); fi
done
wait

# ---- probe durations, build concat list in order ----
LIST="$WORK/list.txt"; : > "$LIST"
for s in "${!SEG[@]}"; do
  [ -s "${SEG[$s]}" ] || { echo "missing segment for index $s — encode failed"; exit 1; }
  SEGDUR+=("$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${SEG[$s]}")")
  echo "file '${SEG[$s]}'" >> "$LIST"
done
N=${#SEG[@]}

# ---- clamp xfade if it is too long for the shortest clip ----
if awk -v t="$XFADE" 'BEGIN{exit !(t>0)}'; then
  minD=$(printf '%s\n' "${SEGDUR[@]}" | sort -n | head -1)
  if awk -v t="$XFADE" -v m="$minD" 'BEGIN{exit !(t>=m)}'; then
    NEWX=$(awk -v m="$minD" 'BEGIN{printf "%.3f", m*0.25}')
    if awk -v x="$NEWX" 'BEGIN{exit !(x>0)}'; then
      log "WARN: --xfade $XFADE >= shortest clip ${minD}s; reducing to ${NEWX}s (25% of shortest)"
      XFADE="$NEWX"
    else
      log "WARN: shortest clip ${minD}s too short for any crossfade; using hard cuts"
      XFADE=0
    fi
  fi
fi

# ---- timeline start (ms) of each segment, accounting for xfade overlap ----
declare -a START_MS=()
acc="0"
for s in "${!SEG[@]}"; do
  if [ "$s" -eq 0 ]; then
    START_MS[0]=0
    acc="${SEGDUR[0]}"
  else
    START_MS[$s]=$(awk -v a="$acc" -v t="$XFADE" 'BEGIN{v=(a-t)*1000; if(v<0)v=0; printf "%d", v}')
    acc=$(awk -v a="$acc" -v d="${SEGDUR[$s]}" -v t="$XFADE" 'BEGIN{printf "%.6f", a+d-t}')
  fi
done

# curated pool for --xfade-type random (widely-supported xfade transitions)
XPOOL=(fade fadeblack dissolve wipeleft wiperight wipeup wipedown \
       slideleft slideright slideup slidedown smoothleft smoothright \
       smoothup smoothdown circleopen circleclose radial pixelize)

# ---- build base track ----
BASE="$WORK/base.mp4"
if awk -v t="$XFADE" 'BEGIN{exit !(t>0)}' && [ "$N" -ge 2 ]; then
  log "TRANSITION: xfade '$XTYPE' ${XFADE}s, pairwise (2 inputs/pass, low memory)"
  # Pairwise tree reduction: each pass xfades adjacent pairs, halving the count.
  # Only two clips are decoded per ffmpeg call, so memory stays flat for any N.
  cur_f=("${SEG[@]}"); cur_d=("${SEGDUR[@]}")
  level=0
  while [ "${#cur_f[@]}" -gt 1 ]; do
    next_f=(); next_d=(); k=0; pair=0
    while [ "$k" -lt "${#cur_f[@]}" ]; do
      if [ "$((k+1))" -lt "${#cur_f[@]}" ]; then
        L="${cur_f[$k]}"; Ld="${cur_d[$k]}"
        R="${cur_f[$((k+1))]}"; Rd="${cur_d[$((k+1))]}"
        off=$(awk -v a="$Ld" -v t="$XFADE" 'BEGIN{v=a-t; if(v<0)v=0; printf "%.3f", v}')
        if [ "$XTYPE" = random ]; then xt="${XPOOL[$((RANDOM % ${#XPOOL[@]}))]}"; else xt="$XTYPE"; fi
        out="$WORK/m_${level}_${pair}.mp4"
        log "TRANSITION: L${level} merge $((pair+1)) -> $xt"
        ffmpeg -y -loglevel error -i "$L" -i "$R" -filter_complex \
          "[0:v]format=yuv420p,setsar=1,fps=${FPS}[v0];[1:v]format=yuv420p,setsar=1,fps=${FPS}[v1];[v0][v1]xfade=transition=${xt}:duration=${XFADE}:offset=${off},format=yuv420p[v];[0:a][1:a]acrossfade=d=${XFADE}:c1=tri:c2=tri[a]" \
          -map "[v]" -map "[a]" "${VOPTS[@]}" -c:a aac -b:a 192k -movflags +faststart "$out"
        nd=$(awk -v a="$Ld" -v b="$Rd" -v t="$XFADE" 'BEGIN{printf "%.6f", a+b-t}')
        next_f+=("$out"); next_d+=("$nd")
        [[ "$L" == "$WORK/m_"* ]] && rm -f "$L"
        [[ "$R" == "$WORK/m_"* ]] && rm -f "$R"
        k=$((k+2)); pair=$((pair+1))
      else
        next_f+=("${cur_f[$k]}"); next_d+=("${cur_d[$k]}")  # odd one carries up
        k=$((k+1))
      fi
    done
    cur_f=("${next_f[@]}"); cur_d=("${next_d[@]}"); level=$((level+1))
  done
  mv "${cur_f[0]}" "$BASE"
else
  log "TRANSITION: hard cuts, concat $N clips"
  ffmpeg -y -loglevel error -f concat -safe 0 -i "$LIST" -c copy -movflags +faststart "$BASE"
fi

# ---- inject transition SFX, or finish ----
if [ -z "$SFXDIR" ] || { [ "${#PHOTO_SFX[@]}" -eq 0 ] && [ "${#VIDEO_SFX[@]}" -eq 0 ]; }; then
  log "SFX: none — finishing"
  cp "$BASE" "$OUT"; verify_out; log "DONE: $OUT"; echo "done: $OUT"; exit 0
fi

log "SFX: assigning transition effects"
inputs=(-i "$BASE")
filter=""; amix_in="[0:a]"; n=0; idx=1
for s in "${!SEGTYPE[@]}"; do
  if [ "${SEGTYPE[$s]}" = photo ]; then pool=("${PHOTO_SFX[@]}"); else pool=("${VIDEO_SFX[@]}"); fi
  [ "${#pool[@]}" -eq 0 ] && continue
  sfx="${pool[$((RANDOM % ${#pool[@]}))]}"
  ms="${START_MS[$s]}"
  log "SFX: seg $((s+1)) ${SEGTYPE[$s]} @${ms}ms <- ${sfx##*/}"
  inputs+=(-i "$sfx")
  filter+="[${idx}:a]adelay=${ms}:all=1,volume=${SFXGAIN}[s${idx}];"
  amix_in+="[s${idx}]"
  idx=$((idx+1)); n=$((n+1))
done

if [ "$n" -eq 0 ]; then
  cp "$BASE" "$OUT"; verify_out; log "DONE: $OUT"; echo "done: $OUT"; exit 0
fi
log "MIX: overlaying $n effects onto final track"
filter+="${amix_in}amix=inputs=$((n+1)):normalize=0:duration=first[aout]"
ffmpeg -y -loglevel error "${inputs[@]}" -filter_complex "$filter" \
  -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 192k -movflags +faststart "$OUT"
verify_out
log "DONE: $OUT"
echo "done: $OUT"
