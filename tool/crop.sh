#!/usr/bin/env bash
# crop.sh - stream-copy crop preserving codec, resolution, fps, and metadata
#
# Usage:
#   crop.sh <file> <start> <duration>      keep <duration> starting at <start>
#   crop.sh -e <file> <start> <end>        keep from <start> to <end>
#
# <start>, <duration>, <end> accept HH:MM:SS, MM:SS, or seconds (fractions ok).
# Output: <name>_crop.<ext> alongside the input. Refuses to overwrite.

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <file> <start> <duration>" >&2
    echo "       $(basename "$0") -e <file> <start> <end>" >&2
    exit 1
}

# HH:MM:SS / MM:SS / SS -> seconds (float)
to_seconds() {
    local t="$1" h=0 m=0 s=0
    case "$t" in
        *:*:*) IFS=: read -r h m s <<< "$t" ;;
        *:*)   IFS=: read -r m s <<< "$t" ;;
        *)     s="$t" ;;
    esac
    awk -v h="$h" -v m="$m" -v s="$s" 'BEGIN{printf "%.6f", h*3600 + m*60 + s}'
}

mode=dur
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -e|--end) mode=end; shift ;;
        -h|--help) usage ;;
        --) shift; while [[ $# -gt 0 ]]; do args+=("$1"); shift; done ;;
        -*) echo "error: unknown option: $1" >&2; usage ;;
        *) args+=("$1"); shift ;;
    esac
done

[[ ${#args[@]} -eq 3 ]] || usage

in="${args[0]}"
start="${args[1]}"
third="${args[2]}"

[[ -f "$in" ]] || { echo "error: file not found: $in" >&2; exit 1; }

if [[ "$mode" == "end" ]]; then
    ss=$(to_seconds "$start")
    es=$(to_seconds "$third")
    dur=$(awk -v a="$ss" -v b="$es" 'BEGIN{printf "%.6f", b - a}')
    if awk -v d="$dur" 'BEGIN{exit !(d <= 0)}'; then
        echo "error: end ($third) must be after start ($start)" >&2
        exit 1
    fi
else
    dur="$third"
fi

dir=$(dirname "$in")
base=$(basename "$in")
name="${base%.*}"
ext="${base##*.}"
[[ "$name" == "$base" ]] && ext=""

if [[ -n "$ext" ]]; then
    out="$dir/${name}_crop.${ext}"
else
    out="$dir/${name}_crop"
fi

[[ -e "$out" ]] && { echo "error: output exists: $out" >&2; exit 1; }

ffmpeg -hide_banner -ss "$start" -i "$in" -t "$dur" \
    -c copy -map_metadata 0 -movflags use_metadata_tags+faststart \
    "$out"

echo "$out"
