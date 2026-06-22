#!/usr/bin/env bash
# crop.sh - stream-copy crop preserving codec, resolution, fps, and metadata
# Usage: crop.sh <file> <start> <duration>
#   <file>      input video path
#   <start>     start offset (e.g. 00:01:45 or 105)
#   <duration>  length to keep (e.g. 40 or 00:00:40)
# Output: <name>_crop.<ext> alongside the input.

set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $(basename "$0") <file> <start> <duration>" >&2
    exit 1
fi

in="$1"
start="$2"
dur="$3"

if [[ ! -f "$in" ]]; then
    echo "error: file not found: $in" >&2
    exit 1
fi

dir=$(dirname "$in")
base=$(basename "$in")
name="${base%.*}"
ext="${base##*.}"
[[ "$name" == "$base" ]] && ext=""   # no extension

if [[ -n "$ext" ]]; then
    out="$dir/${name}_crop.${ext}"
else
    out="$dir/${name}_crop"
fi

if [[ -e "$out" ]]; then
    echo "error: output exists: $out" >&2
    exit 1
fi

ffmpeg -hide_banner -ss "$start" -i "$in" -t "$dur" \
    -c copy -map_metadata 0 -movflags use_metadata_tags+faststart \
    "$out"

echo "$out"
