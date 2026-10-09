# FFmpeg binaries

Put the FFmpeg executables here. They are not stored in Git (see `.gitignore`).

| Platform | Files |
|---|---|
| Windows | `ffmpeg.exe`, `ffprobe.exe`, `ffplay.exe` |
| macOS / Linux | `ffmpeg`, `ffprobe`, `ffplay` (make them executable: `chmod +x ffmpeg ffprobe ffplay`) |

To run from source, ffplay is only needed by Video Receiver (for live playback). To build the executables (`build.bat` / `build.sh`), all three are needed: they are bundled into each single-file executable.

Use a "full" build. It includes everything the apps can use:

| Library | Used for |
|---|---|
| **libsrt** | SRT |
| **librist** | RIST |
| **libx264** | re-encoding to H.264 |
| **freetype** | the date/time overlay and the test signal's clock |
| **libzmq** | instant overlay changes while streaming (without it, changes cause a short pause) |

UDP multicast and RTMP work with any build (RTMPS needs TLS support).

Where to get one:

- Windows: the "full" builds from https://www.gyan.dev/ffmpeg/builds/ or the GPL builds from https://github.com/BtbN/FFmpeg-Builds/releases
- macOS: https://evermeet.cx/ffmpeg/ or `brew install ffmpeg` (then copy or symlink the binaries here)
- Linux: the static builds from https://johnvansickle.com/ffmpeg/, or the BtbN builds

Check what your build supports:

```bash
ffmpeg -hide_banner -protocols | grep -E "srt|rist"      # SRT and RIST
ffmpeg -hide_banner -filters | grep -E " (zmq|drawtext) " # live overlay commands, text drawing
```

On Windows, use `findstr` instead of `grep`, e.g. `ffmpeg -hide_banner -protocols | findstr "srt rist"`.
