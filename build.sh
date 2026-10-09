#!/usr/bin/env bash
# Builds two single-file executables, dist/VideoPusher and dist/VideoReceiver.
# Each one has ffmpeg, ffprobe and ffplay bundled inside, plus the assets folder,
# so it runs on its own (no Python or FFmpeg install needed on the target PC).
#
# Requires: pip install -r requirements-dev.txt, and bin/ffmpeg, bin/ffprobe, bin/ffplay
# (static builds are best, so the executables don't depend on system libraries).
set -euo pipefail
cd "$(dirname "$0")"

for tool in ffmpeg ffprobe ffplay; do
    if [ ! -x "bin/$tool" ]; then
        echo "ERROR: bin/$tool not found or not executable. See bin/README.md" >&2
        exit 1
    fi
done

build() {   # build <name> <script>
    pyinstaller --noconfirm --clean --onefile --windowed --name "$1" \
        --add-binary "bin/ffmpeg:bin" \
        --add-binary "bin/ffprobe:bin" \
        --add-binary "bin/ffplay:bin" \
        --add-data "assets:assets" \
        "$2"
}

build VideoPusher main.py
build VideoReceiver receiver.py

echo
echo "Built single-file executables (ffmpeg, ffprobe and ffplay inside):"
echo "  dist/VideoPusher"
echo "  dist/VideoReceiver"
