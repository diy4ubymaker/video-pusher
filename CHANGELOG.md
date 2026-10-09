# Changelog

## 2.7.0

**New protocols and security**
- **UDP / multicast** in both apps. The pusher sends MPEG-TS over UDP to a multicast group or one receiver, with TTL, a choice of network card, whole 1316-byte datagrams, and (with a bandwidth set) a constant, smoothly paced rate. The receiver joins a group (or receives unicast), with a choice of network card, source-specific multicast and a large receive buffer. It refuses a multicast relay that would loop back into the group it receives.
- **SRT encryption** in both apps: AES-128, AES-192 or AES-256 with a passphrase (10–79 characters, checked at Start), in push, listen and pull modes. A wrong or missing passphrase is refused. Optional **SRT stream ID** for servers that use one.
- **RTMP stream key field** (hidden, with Show) in both apps. The address is now `server/app`, and a key pasted at the end of a URL is moved into the field. **Video Receiver, acting as the RTMP server, rejects senders with the wrong stream key**; with no key set it accepts any sender.
- **Secrets are masked everywhere:** passphrases and stream keys show as `****` in logged commands, in FFmpeg's own messages and in the status bar. They are never saved, even when typed into an address.

**Video Pusher**
- **Change the date/time and logo while streaming** with **Apply to live stream**. When FFmpeg has the `zmq` filter, the change appears on the next frame with no pause (date/time on/off, format, position, colour, size, box; logo on/off, position, opacity). Otherwise, and for a new logo file or size, the file playing restarts from the same point (about 0.3 s). The receiver stays connected. No extra Python package is needed.
- **The window is reorganised into collapsible panels** (Source, Output, Encoding, Overlays), with one-line summaries when collapsed. Folder-only settings show only in folder mode, the window grows when a panel needs room, and the panel state is saved.
- **Single-file mode uses the playlist pipeline** (a playlist of one): each loop continues the same connection and timeline, and in listen mode a receiver joins the stream live. The old one-process player remains as a fallback when ffprobe is missing.
- The status line shows the playing file's duration, e.g. `Playing 1/5: show.mp4 (22:14)`.
- RTMP: a receiver that stops accepting data without closing the connection is dropped after 10 s, so auto-reconnect takes over instead of the push hanging.
- Default RTMP addresses are now `rtmp://…:1935/live`, with the stream key entered separately.

**Video Receiver**
- **Resilience:** a sender that stops or vanishes without closing the connection (network drop, crash, frozen PC) no longer blocks its own reconnection. After **Drop a silent sender after** seconds without data (default 5, all protocols), the receiver drops the dead connection, says so in the log, and listens or pulls again. Before, RTMP stayed stuck for good, SRT for about 5 s, and RIST waited silently. Waiting for a sender is not affected.
- Live playback of RIST and UDP goes through FFmpeg, so a stopped sender is noticed there too.
- After a quick reconnect, the player is fully closed before the next one opens, so the picture always comes back.
- The Start button reads **START PULLING** in pull mode.
- The multicast relay sends whole 1316-byte datagrams.

**Project**
- New **[Architecture](docs/ARCHITECTURE.md)** document with diagrams of both apps (Graphviz and Mermaid) and design notes.
- **Build scripts** (`build.bat`, `build.sh`) make two single-file executables, each with ffmpeg, ffprobe and ffplay bundled inside. All three tools are now required to build.
- `.gitignore` excludes old working copies (`main_v1.py`–`main_v11.py`, `receiver_v1.py`–`receiver_v7.py`).
- New **[User Guide](docs/USER_GUIDE.md)** with every setting. The README is rewritten with screenshots and step-by-step instructions.
- The tests (117) cover all four protocols end to end, encryption, stream keys, live overlay changes, multicast and receiver recovery. CI also runs the window tests under a virtual display.

## 2.6.0
- **Push or listen, for every protocol.** Video Pusher can push to a receiver (as before) or listen for the receiver to connect (SRT listener, RTMP server, RIST with '@'). Video Receiver can listen (as before) or pull from a sender.
- Each app remembers a separate address for each protocol and direction, and corrects SRT `mode=` and RIST `@` to match the Connection setting.
- Pusher listen mode: the status shows when a receiver connects; when it leaves, the pusher listens again. In folder mode the playlist keeps running while nobody is connected, so receivers join live instead of getting a backlog.
- Receiver pull mode: retries every 3 s while the sender isn't up, without repeating the same errors in the log.
- Settings saved by earlier versions are carried over.
- Closing a window no longer leaves a stray Tk callback behind.

## 2.5.0
- Both apps **remember their settings**: saved on Start and on close, restored at the next launch (per user: `%APPDATA%\VideoPusher` on Windows, `~/Library/Application Support/VideoPusher` on macOS, `~/.config/VideoPusher` on Linux). The RIST passphrase is never saved. Invalid or outdated saved values are ignored.

## 2.4.0
- Pusher, re-encode: **logo overlay** - image chosen in the app (PNG transparency kept), nine positions plus custom X/Y, size as % of picture width, opacity. Works in single-file and folder modes and all output formats.
- The date/time overlay is now drawn before the anamorphic squeeze on SD formats, so its text keeps the right proportions.

## 2.3.0
- Pusher: **date/time colour** - standard palette (white, yellow, black, red, green, blue, cyan, magenta, orange, grey) or a custom colour from the colour picker. The background box or outline switches between black and white automatically to keep the text readable.

## 2.2.0
- Pusher: **Clear Log** button.
- Pusher, re-encode: **quality controls** - x264 preset, quality (CRF, used when there is no bandwidth cap) and audio bitrate.
- Pusher, re-encode: **date/time overlay** - three formats, nine positions plus custom X/Y, size as % of picture height, optional background box; drawn with the bundled font, at the output size and before interlacing.

## 2.1.0
- New **Video Receiver** (`receiver.py`), based on the SRT receiver v1.8.0, with **RTMP** (acts as the RTMP server) and **RIST** (profile, buffer, AES-128/256 encryption) added alongside SRT. Keeps all five actions: playback, play + save, play + multicast, save only, multicast only.
- Receiver: each new connection records to a new timestamped file instead of overwriting the previous recording; Stop escalates when a RIST listener is idle; settings are read safely before the worker starts, and Start waits until the previous session has fully stopped; the log is bounded and the passphrase is never logged.
- Build scripts produce both executables (the receiver needs `bin/ffplay`).

## 2.0.0
- **RIST output** (MPEG-TS), alongside SRT and RTMP: Simple or Main profile, adjustable buffer, optional AES-128/256 encryption with a passphrase (never logged). Works in both single-file and folder modes, with the test signal and output formats.
- Over RIST the test signal is sent at a constant bitrate with padding, because receivers drop very low-bitrate streams.
- At Start, the app checks that the FFmpeg build supports the chosen protocol (libsrt / librist / TLS) and says what is missing.
- RIST address checks: sender listen mode is rejected with an explanation, and odd ports produce a warning.

## 1.9.0
- Folder mode: optional **test signal** when no file is playable (empty folder, files still copying, or all broken): bars & tone (1 kHz, −18 dBFS) or black & silence, with a "NO TRANSMISSION" caption and live clock. The stream stays up and switches back as soon as a file is ready. Start is allowed on an empty folder when it is enabled.
- Bundled DejaVu Sans Mono font and a pre-rendered caption image, so the caption shows with any FFmpeg build (image fallback when FFmpeg can't draw text).
- Selectable **output format** for re-encoding (1080p25 to 480i59.94), including true interlaced output; interlaced sources are deinterlaced before scaling.
- "No playable files" is logged once instead of every 10 seconds; stop-time FFmpeg messages are no longer shown as errors.

## 1.8.0
- Folder mode: if the file lined up next is deleted or replaced before it plays, or fails as soon as it opens, playback switches straight to a file that has already been checked, so the stream doesn't pause.

## 1.7.0
- Folder mode: new files are only used once their size has been stable for a set time (configurable in the UI, default 10 s), so files still being copied in are never probed or played half-finished.
- Licensed under GPL-2.0-or-later (same terms as FFmpeg's GPL builds).

## 1.6.0
- The auto-reconnect delay can be set in the UI (1–3600 s, default 3 s).

## 1.5.0
- New **Folder playlist (loop)** mode: plays every video in a folder in name order, in a loop, over one continuous output connection.
- The next file is probed and test-decoded while the current one plays; unplayable files are skipped.
- Re-encode mode mixes files of different formats by scaling and padding them to match the first file.
- Supports more video file types (mov, mkv, ts, mxf, avi and others).

## 1.4.0
- **RTMP / RTMPS** output (FLV), selectable on the UI alongside SRT.

## 1.3.0
- Renamed the app to **Video Pusher** (formerly SRT Video Pusher).
- Fixed a race in which pressing Stop then Start quickly could run two FFmpeg processes at once.
- The bandwidth cap works in copy mode (SRT `maxbw`) and covers audio when re-encoding.
- Live-tuned x264 settings (`veryfast`, `zerolatency`, keyframe every 2 s).
- Separate stats bar, a log bounded for 24/7 runs, a clean FFmpeg shutdown, and input validation.

## 1.2.0
- Original version: loops one MP4 to an SRT endpoint, with optional re-encoding and auto-reconnect.
