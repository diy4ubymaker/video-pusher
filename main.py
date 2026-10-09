# Video Pusher
# Copyright (C) 2026 Desmond Kee
#
# This program is free software; you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License (LICENSE file) for more details.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Video Pusher - pushes a looping video file, or a looping folder playlist, live via FFmpeg
(SRT as MPEG-TS, or RTMP as FLV).

Folder playlist mode keeps ONE output connection open for the whole playlist. Each file is
played by a short-lived "feeder" FFmpeg that writes MPEG-TS into the output FFmpeg's stdin, so
the receiver sees one continuous stream. While a file plays, the next one is probed and test-
decoded in the background; files that are unreadable, undecodable or incompatible are skipped.
"""

import io
import json
import os
import sys
import time
import queue
import threading
import subprocess
import platform
import re
import socket
import struct
from collections import namedtuple
from fractions import Fraction
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, colorchooser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

APP_VERSION = "2.7.0"

# Output protocols: default endpoint, accepted URL schemes
PROTOCOLS = {
    "SRT": {"default": "srt://127.0.0.1:9000?mode=caller", "schemes": ("srt://",)},
    "RTMP": {"default": "rtmp://127.0.0.1:1935/live", "schemes": ("rtmp://", "rtmps://")},
    "RIST": {"default": "rist://127.0.0.1:5000", "schemes": ("rist://",)},
    "UDP": {"default": "udp://239.1.1.1:5000", "schemes": ("udp://",)},
}
# Connection direction: the pusher either connects to the receiver ("push") or waits for the
# receiver to connect to it ("listen"). Default addresses for each protocol and direction.
CONNECTIONS = {"push": "Push (connect to the receiver)", "listen": "Listen (receiver connects to this PC)"}
DEFAULT_ENDPOINTS = {
    "SRT/push": "srt://127.0.0.1:9000?mode=caller",
    "SRT/listen": "srt://0.0.0.0:9000?mode=listener",
    "RTMP/push": "rtmp://127.0.0.1:1935/live",          # + the Stream key field
    "RTMP/listen": "rtmp://0.0.0.0:1935/live",
    "RIST/push": "rist://127.0.0.1:5000",
    "RIST/listen": "rist://@0.0.0.0:5000",
    "UDP/push": "udp://239.1.1.1:5000",                  # multicast group, or a receiver's IP
}
# UDP (MPEG-TS over UDP, multicast or unicast): no connection, so always "push"
UDP_TTL = 16                     # multicast hops (routers) the stream may cross
UDP_TTL_RANGE = (1, 255)
UDP_PKT_SIZE = 1316              # 7 TS packets per datagram: the standard for IPTV / IRDs
UDP_MUXRATE_HEADROOM = 1.15      # re-encode + bandwidth cap: CBR mux rate = cap x this (TS overhead)
UDP_PACING_HEADROOM = 1.05       # sending pace a little above the mux rate, so it never falls behind
RIST_PROFILES = {"Main": "main", "Simple": "simple"}      # Advanced is rarely supported by receivers
RIST_ENCRYPTION = {"Off": None, "AES-128": "128", "AES-256": "256"}
# SRT encryption: key length in bytes (FFmpeg -pbkeylen); passphrase length limits set by libsrt
SRT_ENCRYPTION = {"Off": None, "AES-128": "16", "AES-192": "24", "AES-256": "32"}
SRT_PASSPHRASE_LEN = (10, 79)
RTMP_IO_TIMEOUT_S = 10           # pusher: drop an RTMP connection that accepts no data for this long
RIST_BUFFER_MS = 1000                                      # librist's own default
RIST_BUFFER_RANGE = (50, 30000)
# Over RIST, very low-bitrate streams (e.g. a black picture) are dropped by receivers, so the test
# signal is sent at a constant bitrate with filler when using RIST.
RIST_SLATE_KBPS = 2000
RIST_MIN_SLATE_KBPS = 800

VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".mkv", ".ts", ".m2ts", ".mts", ".mxf",
              ".avi", ".flv", ".webm", ".mpg", ".mpeg", ".wmv")

MAX_LOG_LINES = 2000          # keep the log widget bounded on 24/7 runs
RECONNECT_DELAY_S = 3         # default reconnect wait (configurable in the UI)
RECONNECT_DELAY_RANGE = (1, 3600)
AUDIO_BITRATE_K = 128         # default AAC bitrate used when re-encoding
# Re-encode quality controls
X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]
DEFAULT_PRESET = "veryfast"
DEFAULT_CRF = 23              # x264's own default; lower = better quality, bigger stream
CRF_RANGE = (16, 35)
AUDIO_BITRATES_K = [64, 96, 128, 160, 192, 256, 320]
# Date/time overlay (re-encode only), drawn with the bundled font
OVERLAY_FORMATS = {
    "2026-10-08 14:30:00": "%Y-%m-%d %T",
    "08/10/2026 14:30:00": "%d/%m/%Y %T",
    "14:30:00": "%T",
}
OVERLAY_POSITIONS = ["Top-left", "Top-centre", "Top-right", "Middle-left", "Centre", "Middle-right",
                     "Bottom-left", "Bottom-centre", "Bottom-right", "Custom"]
OVERLAY_COLOURS = {            # standard palette (name -> RGB hex)
    "White": "#FFFFFF", "Yellow": "#FFFF00", "Black": "#000000", "Red": "#FF0000",
    "Green": "#00FF00", "Blue": "#0000FF", "Cyan": "#00FFFF", "Magenta": "#FF00FF",
    "Orange": "#FFA500", "Grey": "#808080",
}
CUSTOM_COLOUR = "Custom..."
OVERLAY_SIZE_RANGE = (2, 20)  # text height as % of the picture height
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
LOGO_SIZE_RANGE = (2, 60)     # logo width as % of the picture width
DEFAULT_LOGO_SIZE = 15
LOGO_OPACITY_RANGE = (10, 100)
DEFAULT_OVERLAY_SIZE = 5
MIN_VIDEO_BITRATE_K = 200     # floor for the video share of the cap
STOP_GRACE_S = 3              # time FFmpeg gets to quit cleanly before being killed

PROBE_TIMEOUT_S = 20          # ffprobe time limit per file
DECODE_TEST_S = 2             # seconds of each file test-decoded before it is played
DECODE_TEST_TIMEOUT_S = 30
NO_PLAYABLE_RETRY_S = 10      # wait before rescanning a folder with nothing playable
FILE_SETTLE_S = 10            # default: a file must be unchanged this long before it is used
FILE_SETTLE_RANGE = (2, 600)
FOLDER_POLL_S = 1             # how often the folder watcher re-checks file sizes
SETTLING_RETRY_S = 2          # wait before re-checking when only still-copying files are left
MAX_OUTPUT_RUN_S = 24 * 3600  # restart the output daily, clear of the 26.5 h MPEG-TS timestamp wrap
PIPE_CHUNK = 188 * 348        # ~64 KB of whole TS packets
FILE_GAP_S = 0.1              # timestamp gap between files (avoids overlap from rounding)
MAX_LEAD_S = 0.5              # how far the pushed timeline may run ahead of real time
LISTEN_QUEUE_CHUNKS = 8       # folder + listen: stream data kept while no receiver is connected
SLATE_SEGMENT_S = 4           # test signal is sent in segments; the folder is re-checked between them
# output log lines expected at every file boundary (MPEG-TS continuity reset) - not real errors
BOUNDARY_NOISE = ("Packet corrupt", "corrupt input packet")

IS_WINDOWS = platform.system() == "Windows"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


# ======================================================================
# Helpers
# ======================================================================
def app_dir():
    """Folder holding bin/ and assets/: the PyInstaller bundle if frozen, else next to this file."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


ASSETS_DIR = os.path.join(app_dir(), "assets")
SLATE_FONT = "DejaVuSansMono-Bold.ttf"        # bundled font for the test-signal caption and clock
SLATE_IMAGE = "no_transmission.png"           # pre-rendered caption, used if FFmpeg can't draw text


def find_tool(name):
    """Bundled tool: inside the PyInstaller bundle if frozen, otherwise ./bin next to this file."""
    exe = name + ".exe" if IS_WINDOWS else name
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "bin", exe)


def popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=NO_WINDOW, **kw)


def run_quiet(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)


def last_line(text, default):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else default


def fmt_duration(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def list_videos(folder):
    """Video files directly inside folder, sorted by name (case-insensitive)."""
    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith(VIDEO_EXTS) and not f.startswith(".")]
    files = [f for f in files if os.path.isfile(f)]
    return sorted(files, key=lambda p: os.path.basename(p).lower())


def add_srt_maxbw(endpoint, kbps):
    """Add SRT's maxbw (bytes/s) to the URL unless the user already set one."""
    parts = urlsplit(endpoint)
    params = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "maxbw" for k, _ in params):
        return endpoint
    params.append(("maxbw", str(kbps * 1000 // 8)))
    return urlunsplit(parts._replace(query=urlencode(params, safe=":/")))


def encode_args(settings):
    """H.264/AAC live encode settings: preset, quality (CRF) or bandwidth cap, audio bitrate."""
    audio_k = settings.get("audio_k", AUDIO_BITRATE_K)
    args = [
        "-c:v", "libx264", "-preset", settings.get("preset", DEFAULT_PRESET), "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        # keyframe every 2 s regardless of frame rate -> fast receiver join
        "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-c:a", "aac", "-b:a", f"{audio_k}k",
    ]
    bw = settings["bandwidth_k"]
    if bw > 0:
        # the cap covers video + audio, so give video what is left after audio
        vbr = max(bw - audio_k, MIN_VIDEO_BITRATE_K)
        args += ["-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{vbr}k"]
    else:
        args += ["-crf", str(settings.get("crf", DEFAULT_CRF))]
    return args


def image_width(ffprobe, path):
    """Width of the first video/image stream in a file, or None if it can't be read."""
    if not os.path.isfile(ffprobe):
        return None
    try:
        r = run_quiet([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                       "-of", "csv=p=0", path], PROBE_TIMEOUT_S)
        return int(r.stdout.strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def overlay_filter(overlay, name=""):
    """drawtext filter for the date/time overlay, or None. Needs cwd=ASSETS_DIR (bundled font).
    name: instance name, so live commands can reach it (drawtext@name)."""
    if not overlay:
        return None
    return f"drawtext{'@' + name if name else ''}=" + drawtext_options(overlay)


DEFAULT_OVERLAY = {"format": list(OVERLAY_FORMATS)[0], "position": "Top-left", "x": 0, "y": 0,
                   "size": DEFAULT_OVERLAY_SIZE, "box": True, "colour": "#FFFFFF"}


def drawtext_options(overlay, visible=True):
    """Every drawtext option for the overlay, spelled out, so that a live 'reinit' command
    (which keeps options it isn't given) fully replaces the previous look."""
    fmt = OVERLAY_FORMATS[overlay["format"]].replace(":", "\\:")
    m = "h*0.03"                                   # margin from the picture edge
    xs = {"left": m, "centre": "(w-text_w)/2", "right": f"w-text_w-{m}"}
    ys = {"top": m, "middle": "(h-text_h)/2", "bottom": f"h-text_h-{m}"}
    pos = overlay["position"]
    if pos == "Custom":
        # 0 % = left/top edge, 100 % = right/bottom edge (the text always stays inside the picture)
        x = f"(w-text_w)*{overlay['x'] / 100:.4f}"
        y = f"(h-text_h)*{overlay['y'] / 100:.4f}"
    else:
        vert, _, horiz = pos.lower().partition("-") if "-" in pos else ("middle", "", "centre")
        x, y = xs[horiz], ys[vert]
    colour = overlay.get("colour", "#FFFFFF").lstrip("#").upper()
    back = contrast_colour(colour)                 # box/outline that keeps the text readable
    f = (f"fontfile={SLATE_FONT}:text='%{{localtime\\:{fmt}}}':fontcolor=0x{colour}"
         f":fontsize=h*{overlay['size'] / 100:.4f}:x={x}:y={y}")
    if not visible:
        return f + ":alpha=0:box=0:borderw=0"
    if overlay.get("box", True):
        f += f":box=1:boxcolor={back}@0.55:boxborderw=8:borderw=0"
    else:
        f += f":box=0:borderw=2:bordercolor={back}@0.8"     # outline keeps it readable without a box
    return f + ":alpha=1"


def contrast_colour(hex_rgb):
    """'black' behind light text, 'white' behind dark text (by perceived brightness)."""
    r, g, b = (int(hex_rgb[i:i + 2], 16) for i in (0, 2, 4))
    return "black" if 0.299 * r + 0.587 * g + 0.114 * b >= 110 else "white"


# ---------------- live overlay changes (FFmpeg's zmq filter) ----------------
LIVE_TEXT = "vpclock"            # filter instance names that live commands are sent to
LIVE_LOGO = "vplogo"
LIVE_LOGO_ALPHA = "vplogoalpha"
LIVE_LOGO_FPS = 10               # a looped logo image is re-sent this often (opacity changes)
ZMQ_TIMEOUT_S = 2.0


def free_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def zmq_filter(port):
    """zmq filter for a filtergraph: receives commands on 127.0.0.1:port (escaped for the graph)."""
    return f"zmq=bind_address=tcp\\\\://127.0.0.1\\\\:{port}"


class ZmqError(Exception):
    pass


class ZmqRequester:
    """Minimal ZeroMQ REQ client (ZMTP 3.0, NULL security) - just enough to send commands to
    FFmpeg's zmq filter, without needing the pyzmq package."""

    def __init__(self, port, timeout=ZMQ_TIMEOUT_S):
        self.port = port
        self.timeout = timeout
        self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ZmqError("connection closed")
            data += chunk
        return data

    def _send_frame(self, body, more=False, command=False):
        flags = (0x01 if more else 0) | (0x04 if command else 0)
        if len(body) > 255:
            self.sock.sendall(bytes([flags | 0x02]) + struct.pack(">Q", len(body)) + body)
        else:
            self.sock.sendall(bytes([flags, len(body)]) + body)

    def _recv_frame(self):
        """(body, more, command)"""
        flags = self._recv_exact(1)[0]
        size = struct.unpack(">Q", self._recv_exact(8))[0] if flags & 0x02 else self._recv_exact(1)[0]
        return self._recv_exact(size), bool(flags & 0x01), bool(flags & 0x04)

    def connect(self):
        deadline = time.monotonic() + self.timeout
        while True:                     # the filter binds when FFmpeg starts: retry briefly
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
                break
            except OSError as e:
                if time.monotonic() > deadline:
                    raise ZmqError(f"can't connect to FFmpeg ({e})")
                time.sleep(0.1)
        try:
            greeting = (b"\xff" + b"\x00" * 8 + b"\x7f" + bytes([3, 0]) + b"NULL".ljust(20, b"\x00")
                        + b"\x00" + b"\x00" * 31)
            self.sock.sendall(greeting)
            peer = self._recv_exact(64)
            if peer[0] != 0xFF or peer[9] != 0x7F or peer[10] < 3:
                raise ZmqError("not a ZeroMQ 3 peer")
            ready = b"\x05READY" + b"\x0bSocket-Type" + struct.pack(">I", 3) + b"REQ"
            self._send_frame(ready, command=True)
            while True:
                body, _more, command = self._recv_frame()
                if command and body[:6] == b"\x05READY":
                    break
                if command and body[:6] == b"\x05ERROR":
                    raise ZmqError("handshake refused")
        except (OSError, struct.error) as e:
            raise ZmqError(str(e))

    def request(self, text):
        """Send one command; returns FFmpeg's reply, e.g. '0 Success'."""
        if self.sock is None:
            self.connect()
        try:
            self._send_frame(b"", more=True)              # REQ envelope delimiter
            self._send_frame(text.encode("utf-8"))
            parts = []
            while True:
                body, more, command = self._recv_frame()
                if not command:
                    parts.append(body)
                if not more and not command:
                    break
        except (OSError, struct.error) as e:
            self.close()
            raise ZmqError(str(e))
        return b"".join(p for p in parts if p).decode("utf-8", "replace")


def live_logo_commands(logo, width):
    """Commands that move / fade an existing logo; None if this change needs a new feeder
    (different file or size). logo=None hides it."""
    if logo is None:
        return [f"colorchannelmixer@{LIVE_LOGO_ALPHA} aa 0"]
    x, y = logo_position(logo)
    return [f"overlay@{LIVE_LOGO} x {x}", f"overlay@{LIVE_LOGO} y {y}",
            f"colorchannelmixer@{LIVE_LOGO_ALPHA} aa {logo['opacity'] / 100:.2f}"]


# ---------------- output formats ----------------
# width, height, frame rate (frames/s), interlaced, sample aspect ratio, square-pixel width
Target = namedtuple("Target", "w h fps interlaced sar wsq")

OUTPUT_FORMATS = {
    "1080p25":    Target(1920, 1080, "25", False, "1", 1920),
    "1080i50":    Target(1920, 1080, "25", True, "1", 1920),
    "1080p50":    Target(1920, 1080, "50", False, "1", 1920),
    "1080p29.97": Target(1920, 1080, "30000/1001", False, "1", 1920),
    "1080i59.94": Target(1920, 1080, "30000/1001", True, "1", 1920),
    "1080p59.94": Target(1920, 1080, "60000/1001", False, "1", 1920),
    "720p50":     Target(1280, 720, "50", False, "1", 1280),
    "720p59.94":  Target(1280, 720, "60000/1001", False, "1", 1280),
    "576i50":     Target(720, 576, "25", True, "64/45", 1024),        # PAL SD, 16:9 anamorphic
    "480i59.94":  Target(720, 480, "30000/1001", True, "32/27", 854),  # NTSC SD, 16:9 anamorphic
}
FORMAT_AUTO = "Auto (match first file)"
TEST_SIGNAL_KEYS = {"Bars & tone (NO TRANSMISSION)": "bars", "Black & silence (NO TRANSMISSION)": "black"}
TEST_SIGNALS = list(TEST_SIGNAL_KEYS)
DEFAULT_FORMAT = "1080p25"      # used when "Auto" has no file to match yet
INTERLACED_ORDERS = ("tt", "bb", "tb", "bt")


def field_rate(fps):
    """'25' -> '50', '30000/1001' -> '60000/1001'."""
    return str(Fraction(fps) * 2)


def target_from_info(info):
    """Output format matching a probed file (re-encode 'Auto', and the test signal in copy mode)."""
    w, h = info["width"] - info["width"] % 2, info["height"] - info["height"] % 2
    sar = info.get("sar") or "1"
    return Target(w, h, info["fps_str"], bool(info.get("interlaced")), sar, w)


def describe_target(t):
    rate = f"{Fraction(t.fps) * (2 if t.interlaced else 1):.2f}".rstrip("0").rstrip(".")
    return f"{t.w}x{t.h}{'i' if t.interlaced else 'p'}{rate}"


def _pre_filters(t, src_interlaced):
    """Deinterlace if needed, then fit into the target picture at square pixels."""
    parts = []
    if src_interlaced is None:
        parts.append("yadif=mode=send_field:deint=interlaced")
    elif src_interlaced:
        parts.append("yadif=mode=send_field")        # one frame per field, for clean scaling/retiming
    parts += [f"scale={t.wsq}:{t.h}:force_original_aspect_ratio=decrease",
              f"pad={t.wsq}:{t.h}:(ow-iw)/2:(oh-ih)/2"]
    return parts


def _post_filters(t):
    """Square pixels -> final pixel shape (anamorphic SD), then frame rate / interlacing."""
    parts = [f"scale={t.w}:{t.h}"] if t.wsq != t.w else []
    return parts + [f"setsar={t.sar}"] + interlace_filters(t)


def video_chain(t, src_interlaced, overlay=None):
    """Filter chain converting any video to target t. src_interlaced: True/False, or None to
    deinterlace only the frames flagged as interlaced. overlay: extra filter (e.g. the date/time),
    applied at square pixels and before frames are woven into interlaced fields."""
    return ",".join(_pre_filters(t, src_interlaced) + ([overlay] if overlay else []) + _post_filters(t))


def logo_position(logo):
    """overlay x/y expressions (W/H = picture, w/h = logo) with a margin of 3 % of the height."""
    m = "H*0.03"
    pos = logo["position"]
    if pos == "Custom":
        return f"(W-w)*{logo['x'] / 100:.4f}", f"(H-h)*{logo['y'] / 100:.4f}"
    vert, _, horiz = pos.lower().partition("-") if "-" in pos else ("middle", "", "centre")
    xs = {"left": m, "centre": "(W-w)/2", "right": f"W-w-{m}"}
    ys = {"top": m, "middle": "(H-h)/2", "bottom": f"H-h-{m}"}
    return xs[horiz], ys[vert]


def logo_width(width, logo):
    return max(2, int(width * logo["size"] / 100) // 2 * 2)


def video_graph(src, t, src_interlaced, text, logo, logo_input, width, live=None):
    """-filter_complex graph (output label [vout]) for re-encoding with a logo. src: input video
    label, e.g. "[0:v]"; t: target format or None (keep the source size); text: date/time drawtext
    or None; logo: logo settings; logo_input: the logo's input index; width: picture width the logo
    size is relative to (square pixels). live: a zmq filter, when the logo can be changed while
    streaming - the logo input is then a looped image (-loop 1), so opacity changes take effect."""
    pre = ([live] if live else []) + (_pre_filters(t, src_interlaced) if t else []) + ([text] if text else [])
    lw = logo_width(width, logo)
    x, y = logo_position(logo)
    alpha = f"colorchannelmixer@{LIVE_LOGO_ALPHA}" if live else "colorchannelmixer"
    over = (f"overlay@{LIVE_LOGO}=x={x}:y={y}:shortest=1" if live else
            # a still image is one frame: eof_action=repeat keeps it on screen for the whole stream
            f"overlay=x={x}:y={y}:eof_action=repeat")
    g = [f"{src}{','.join(pre) or 'null'}[base]",
         f"[{logo_input}:v]scale={lw}:-1,format=rgba,{alpha}=aa={logo['opacity'] / 100:.2f}[logo]",
         f"[base][logo]{over}[ov]"]
    post = _post_filters(t) if t else []
    g.append(f"[ov]{','.join(post) or 'null'}[vout]")
    return ";".join(g)


def interlace_filters(t):
    """Final frame-rate step: progressive frames, or pairs of fields woven into interlaced frames."""
    if t.interlaced:
        return [f"fps={field_rate(t.fps)}", "tinterlace=mode=interleave_top", "setfield=tff"]
    return [f"fps={t.fps}"]


def interlace_args(t, encoder):
    """Encoder flags for interlaced (top field first) output."""
    if not t.interlaced:
        return []
    if encoder == "libx264":
        return ["-flags", "+ildct+ilme", "-x264-params", "tff=1"]
    if encoder == "mpeg2video":
        return ["-flags", "+ildct+ilme", "-top", "1"]
    return []


def ffmpeg_caps(ffmpeg):
    """Filters and encoders this FFmpeg build has."""
    caps = {"filters": set(), "encoders": set()}
    for kind in caps:
        try:
            r = run_quiet([ffmpeg, "-hide_banner", "-" + kind], 15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] != "=":
                caps[kind].add(parts[1])
    return caps


# ---------------- test signal ("no transmission") ----------------
CHANNEL_LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 8: "7.1"}


def slate_levels(caps, assets_dir=None):
    """Best-first ways of drawing the caption with this FFmpeg build."""
    assets_dir = assets_dir or ASSETS_DIR
    levels = []
    if "drawtext" in caps["filters"] and os.path.isfile(os.path.join(assets_dir, SLATE_FONT)):
        levels.append("text")       # caption + live clock, bundled font
    if os.path.isfile(os.path.join(assets_dir, SLATE_IMAGE)):
        levels.append("image")      # pre-rendered caption image (any FFmpeg build)
    levels.append("plain")          # bars / black only
    return levels


def slate_codecs(settings, reference, caps):
    """(video encoder args, audio encoder args or None, sample rate, channels) for the test signal.
    Re-encode mode matches the file feeders; copy mode matches the playlist's first file."""
    enc = caps["encoders"]
    if settings["reencode"] or reference is None:
        v = [a for a in encode_args(settings)]
        vi = v.index("-c:a")
        return v[:vi], v[vi:], 48000, 2

    # copy mode: same codecs and audio layout as the files, so receivers see no format change
    bw = settings["bandwidth_k"]
    vk = min(4000, max(int(bw * 0.7) - 192, MIN_VIDEO_BITRATE_K)) if bw > 0 else 4000
    rate = ["-b:v", f"{vk}k", "-maxrate", f"{vk}k", "-bufsize", f"{vk}k"]
    pix = reference.get("pix_fmt") if reference.get("pix_fmt") in ("yuv420p", "yuv422p") else "yuv420p"
    gop = ["-force_key_frames", "expr:gte(t,n_forced*2)"]
    vcodec = reference["vcodec"]
    if vcodec == "hevc" and "libx265" in enc:
        v = (["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", pix,
              "-x265-params", "log-level=error"] + gop + rate)
    elif vcodec == "mpeg2video" and "mpeg2video" in enc:
        v = ["-c:v", "mpeg2video", "-pix_fmt", pix] + gop + rate
    else:
        v = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", pix] + gop + rate
    if not reference["has_audio"]:
        return v, None, None, None
    acodec = reference["acodec"]
    audio_enc = {"aac": ("aac", "192k"), "mp2": ("mp2", "192k"), "ac3": ("ac3", "384k"),
                 "eac3": ("eac3", "384k"), "mp3": ("libmp3lame", "192k"), "opus": ("libopus", "128k")}
    name, abr = audio_enc.get(acodec, ("aac", "192k"))
    if name not in enc:
        name, abr = "aac", "192k"
    try:
        sr = int(reference["sample_rate"])
    except (TypeError, ValueError):
        sr = 48000
    return v, ["-c:a", name, "-b:a", abr], sr, int(reference["channels"] or 2)


def constant_rate(vargs, kbps):
    """Replace the rate control in encoder args with constant bitrate plus filler, so the stream
    never drops to a trickle (needed for RIST, see RIST_SLATE_KBPS)."""
    out, skip = [], False
    for a in vargs:
        if skip:
            skip = False
            continue
        if a in ("-b:v", "-maxrate", "-minrate", "-bufsize", "-crf"):
            skip = True
            continue
        out.append(a)
    rate = f"{kbps}k"
    out += ["-b:v", rate, "-minrate", rate, "-maxrate", rate, "-bufsize", rate]
    codec = out[out.index("-c:v") + 1]
    if codec == "libx264":
        out += ["-x264-params", "nal-hrd=cbr:filler=1"]
    elif codec == "libx265" and "-x265-params" in out:
        out[out.index("-x265-params") + 1] += ":hrd=1"
    return out


def merge_x264_params(cmd):
    """FFmpeg keeps only the last -x264-params, so combine them into one."""
    params = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "-x264-params"]
    if len(params) < 2:
        return cmd
    out, skip = [], False
    for a in cmd:
        if skip:
            skip = False
            continue
        if a == "-x264-params":
            skip = True
            continue
        out.append(a)
    i = out.index("-c:v") + 2
    return out[:i] + ["-x264-params", ":".join(params)] + out[i:]


def slate_cmd(ffmpeg, kind, level, t, vargs, aargs, sample_rate, channels, seconds, ts_offset):
    """FFmpeg command producing `seconds` of test signal as MPEG-TS on stdout.
    kind: 'bars' (SMPTE bars + 1 kHz tone at -18 dBFS) or 'black' (black + silence).
    level: 'text' (caption + clock via the bundled font), 'image' (caption PNG) or 'plain'.
    Must be run with cwd=ASSETS_DIR, so the font and image are found without path escaping."""
    rate = field_rate(t.fps) if t.interlaced else t.fps
    size = f"{t.wsq}x{t.h}"
    if kind == "bars":
        vsrc = f"{'smptehdbars' if t.h >= 720 else 'smptebars'}=s={size}:r={rate}"
    else:
        vsrc = f"color=c=black:s={size}:r={rate}"
    secs = str(seconds)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-re", "-f", "lavfi", "-t", secs, "-i", vsrc]
    n = 1
    a_in = None
    if aargs:
        layout = CHANNEL_LAYOUTS.get(channels, "stereo")
        asrc = (f"sine=frequency=1000:sample_rate={sample_rate}" if kind == "bars"
                else f"anullsrc=r={sample_rate}:cl={layout}")
        cmd += ["-re", "-f", "lavfi", "-t", secs, "-i", asrc]
        a_in, n = n, n + 1
    img_in = None
    if level == "image":
        cmd += ["-loop", "1", "-t", secs, "-i", SLATE_IMAGE]
        img_in = n

    graph = []
    v = "[0:v]"
    if level == "text":
        caption = (f"drawtext=fontfile={SLATE_FONT}:text='NO TRANSMISSION':fontsize=h/10:fontcolor=white"
                   ":box=1:boxcolor=black@0.7:boxborderw=28:x=(w-text_w)/2:y=h*0.36-text_h/2")
        clock = (f"drawtext=fontfile={SLATE_FONT}:text='%{{localtime\\:%Y-%m-%d  %T}}':fontsize=h/18"
                 ":fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=16:x=(w-text_w)/2:y=h*0.52-text_h/2")
        graph.append(f"{v}{caption},{clock}[cap]")
        v = "[cap]"
    elif level == "image":
        cw = int(t.wsq * 0.6) // 2 * 2
        graph.append(f"[{img_in}:v]scale={cw}:-2[img]")
        graph.append(f"{v}[img]overlay=(W-w)/2:H*0.36-h/2:shortest=1[cap]")
        v = "[cap]"
    tail = []
    if t.wsq != t.w:
        tail.append(f"scale={t.w}:{t.h}")
    tail.append(f"setsar={t.sar}")
    if t.interlaced:
        tail += ["tinterlace=mode=interleave_top", "setfield=tff"]
    tail.append("format=yuv420p" if "-pix_fmt" not in vargs else f"format={vargs[vargs.index('-pix_fmt') + 1]}")
    graph.append(f"{v}{','.join(tail)}[vout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    cmd += vargs + interlace_args(t, vargs[vargs.index("-c:v") + 1])
    if aargs:
        layout = CHANNEL_LAYOUTS.get(channels, "stereo")
        cmd += ["-map", f"{a_in}:a", "-af", f"aformat=sample_rates={sample_rate}:channel_layouts={layout}"]
        cmd += aargs + ["-ar", str(sample_rate), "-ac", str(channels)]
    cmd += ["-output_ts_offset", f"{ts_offset:.3f}", "-f", "mpegts", "pipe:1"]
    return merge_x264_params(cmd)


def rist_args(settings):
    """RIST protocol options (placed before the output URL)."""
    args = ["-rist_profile", settings.get("rist_profile", "main"),
            "-buffer_size", str(settings.get("rist_buffer_ms", RIST_BUFFER_MS))]
    if settings.get("rist_encryption"):
        args += ["-secret", settings["rist_secret"], "-encryption", settings["rist_encryption"]]
    return args


# ---------------- secrets: SRT/RIST passphrases and RTMP stream keys ----------------
SECRET_OPTIONS = ("-secret", "-passphrase")          # FFmpeg options whose value is a passphrase
SECRET_URL_PARAMS = ("secret", "passphrase")         # the same, written into an address
MASK = "****"
_MASK_RULES = [
    (re.compile(r"(?i)\b((?:%s)=)[^&\s'\"]*" % "|".join(SECRET_URL_PARAMS)), r"\1" + MASK),
    # rtmp://host/app/KEY: the last path part is the stream key
    (re.compile(r"(?i)(rtmps?://[^/\s'\"]+/[^/\s'\"?]+/)[^\s'\"?]+"), r"\1" + MASK),
    # FFmpeg's RTMP server: "Unexpected stream KEY, expecting KEY"
    (re.compile(r"(Unexpected stream )\S*?(, expecting )\S*"), r"\1%s\2%s" % (MASK, MASK)),
]


def mask_secrets(text, secrets=()):
    """Text for the log/status bar with passphrases and stream keys replaced by ****."""
    for value in secrets:
        if value:
            text = text.replace(value, MASK)
            quoted = quote(value, safe="")
            if quoted != value:
                text = text.replace(quoted, MASK)
    for pattern, repl in _MASK_RULES:
        text = pattern.sub(repl, text)
    return text


def redact(cmd, secrets=()):
    """Command for the log, with passphrases and stream keys hidden."""
    out = list(cmd)
    for i, arg in enumerate(out[:-1]):
        if arg in SECRET_OPTIONS:
            out[i + 1] = MASK
    return [mask_secrets(a, secrets) for a in out]


def cmd_secrets(cmd):
    """Passphrase values passed on an FFmpeg command line."""
    return [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg in SECRET_OPTIONS]


def secret_values(settings):
    """Every secret in a settings dict (to mask them in FFmpeg's own output)."""
    return [v for v in (settings.get("rist_secret"), settings.get("srt_passphrase"), settings.get("rtmp_key"))
            if v]


def srt_args(settings):
    """SRT encryption options (placed before the output URL / before -i)."""
    if settings.get("srt_encryption"):
        return ["-passphrase", settings["srt_passphrase"], "-pbkeylen", settings["srt_encryption"]]
    return []


def pop_url_param(url, key):
    """(url without ?key=..., its value or None)."""
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    value = next((v for k, v in params if k == key), None)
    if value is None:
        return url, None
    params = [(k, v) for k, v in params if k != key]
    return urlunsplit(parts._replace(query=urlencode(params, safe=":/@"))), value


def split_rtmp_key(url):
    """rtmp://host/app/key -> ('rtmp://host/app', 'key'); ('rtmp://host/app', '') when there is no key."""
    parts = urlsplit(url)
    segments = [x for x in parts.path.split("/") if x]
    if len(segments) < 2 or parts.query or parts.fragment:
        return url, ""
    return urlunsplit(parts._replace(path="/" + "/".join(segments[:-1]))), unquote(segments[-1])


def rtmp_url(base, key):
    """Server/app address plus the stream key."""
    return base.rstrip("/") + "/" + key if key else base


def strip_saved_secrets(url, rtmp_key=False):
    """Address as it may be saved: no passphrase, and (rtmp_key=True) no RTMP stream key."""
    for key in SECRET_URL_PARAMS:
        url = pop_url_param(url, key)[0] if "://" in url else url
    if rtmp_key and url.lower().startswith(("rtmp://", "rtmps://")):
        url = split_rtmp_key(url)[0]
    return url


def url_scheme(endpoint):
    return endpoint.split("://", 1)[0].lower() if "://" in endpoint else ""


def ffmpeg_protocols(ffmpeg, direction):
    """Protocol names this FFmpeg (or ffplay) build can use for 'input' or 'output'
    (empty set if it can't be determined)."""
    try:
        r = run_quiet([ffmpeg, "-hide_banner", "-protocols"], 15)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names, section = set(), None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.endswith(":"):
            section = line[:-1].lower()
        elif line and section == direction:
            names.add(line)
    return names


def ffmpeg_output_protocols(ffmpeg):
    """Protocol names this FFmpeg build can write to (empty set if it can't be determined)."""
    return ffmpeg_protocols(ffmpeg, "output")


def set_url_param(url, key, value):
    """(url with ?key=value set, previous value or None)."""
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    old = next((v for k, v in params if k == key), None)
    params = [(k, v) for k, v in params if k != key] + [(key, value)]
    return urlunsplit(parts._replace(query=urlencode(params, safe=":/@"))), old


def normalize_connection(protocol, listen, endpoint):
    """Make the address match the direction. listen=True: this app waits for the other side to
    connect; listen=False: this app connects out. Returns (endpoint, [notes], error or None)."""
    notes = []
    if protocol == "SRT":
        want = "listener" if listen else "caller"
        endpoint, old = set_url_param(endpoint, "mode", want)
        if old and old != want:
            notes.append(f"SRT mode changed from '{old}' to '{want}' to match the connection setting.")
        return endpoint, notes, None
    if protocol == "RTMP":
        if listen and endpoint.lower().startswith("rtmps://"):
            return endpoint, notes, ("Can't listen for RTMPS (TLS): that needs a server certificate. "
                                     "Use rtmp://, or SRT/RIST with encryption.")
        return endpoint, notes, None
    if protocol == "UDP":
        if listen:
            return endpoint, notes, "UDP has no connection: the sender just sends. Use Push here."
        try:
            parts = urlsplit(endpoint)
            port = parts.port
        except ValueError:
            return endpoint, notes, "UDP address has an invalid port."
        if not parts.hostname or port is None:
            return endpoint, notes, "UDP address needs an IP and a port, e.g. udp://239.1.1.1:5000"
        return endpoint, notes, None
    if protocol == "RIST":
        scheme, _, rest = endpoint.partition("://")
        if listen and not rest.startswith("@"):
            endpoint = f"{scheme}://@{rest}"
            notes.append(f"Listening RIST addresses are written with '@'; using {endpoint}")
        elif not listen and rest.startswith("@"):
            endpoint = f"{scheme}://{rest[1:]}"
            notes.append(f"Connecting RIST addresses have no '@'; using {endpoint}")
        try:
            port = urlsplit(endpoint.replace("://@", "://", 1)).port
        except ValueError:
            return endpoint, notes, "RIST address has an invalid port."
        if port is None:
            return endpoint, notes, "RIST address needs a port, e.g. rist://192.168.1.50:5000"
        if port % 2:
            notes.append(f"Warning: RIST port {port} is odd. RIST uses port+1 for control traffic, so "
                         "the other side normally expects an even port.")
        return endpoint, notes, None
    return endpoint, notes, None


def is_multicast(host):
    try:
        first = int(str(host).split(".")[0])
    except ValueError:
        return False
    return 224 <= first <= 239


def udp_muxrate_k(settings):
    """Constant mux rate (kbit/s) for UDP, or 0 for a variable rate. Copy mode: the Bandwidth
    value itself (must be above the stream's bitrate); re-encode: the cap plus TS overhead."""
    bw = settings.get("bandwidth_k", 0)
    if bw <= 0:
        return 0
    return int(bw * UDP_MUXRATE_HEADROOM) if settings.get("reencode") else bw


def udp_output_url(settings):
    """UDP destination with packet size, TTL (multicast), network card and, at a constant mux
    rate, smooth pacing (hardware decoders dislike bursts)."""
    url = settings["endpoint"]
    host = urlsplit(url).hostname
    url = set_url_param(url, "pkt_size", str(UDP_PKT_SIZE))[0]
    if is_multicast(host):
        url = set_url_param(url, "ttl", str(settings.get("udp_ttl", UDP_TTL)))[0]
    if settings.get("udp_localaddr"):
        url = set_url_param(url, "localaddr", settings["udp_localaddr"])[0]
    rate = udp_muxrate_k(settings)
    if rate:
        url = set_url_param(url, "bitrate", str(int(rate * 1000 * UDP_PACING_HEADROOM)))[0]
    return url


def output_args(settings):
    """Muxer + destination for the chosen protocol."""
    endpoint = settings["endpoint"]
    if settings["protocol"] == "RIST":
        # no link-rate cap in RIST (bandwidth only applies when re-encoding)
        return rist_args(settings) + ["-f", "mpegts", endpoint]
    if settings["protocol"] == "UDP":
        rate = udp_muxrate_k(settings)
        # -flush_packets 0: always fill whole 1316-byte datagrams (7 TS packets), as receivers expect
        return ((["-muxrate", f"{rate}k"] if rate else []) +
                ["-flush_packets", "0", "-f", "mpegts", udp_output_url(settings)])
    if settings["protocol"] == "RTMP":
        # (RTMP has no link-rate cap; the app warns that the value is ignored in copy mode)
        # skip the duration/filesize header rewrite that fails on a live socket
        listen = ["-listen", "1"] if settings.get("connection") == "listen" else []
        # a receiver that vanishes without closing the TCP connection would block the push
        # forever; give up after RTMP_IO_TIMEOUT_S so auto-reconnect can take over
        return (["-f", "flv", "-flvflags", "no_duration_filesize"] + listen +
                ["-rw_timeout", str(RTMP_IO_TIMEOUT_S * 1_000_000), endpoint])
    if not settings["reencode"] and settings["bandwidth_k"] > 0:
        # copy mode can't change the bitrate; cap the SRT link instead
        endpoint = add_srt_maxbw(endpoint, settings["bandwidth_k"])
    return srt_args(settings) + ["-f", "mpegts", endpoint]


def build_single_file_cmd(ffmpeg_path, settings):
    # Loop forever, read at native frame rate so it behaves like a live feed
    # (no -nostdin: stdin is used to send "q" for a clean stop)
    cmd = [ffmpeg_path, "-hide_banner", "-stream_loop", "-1", "-re", "-i", settings["source"]]
    fmt = OUTPUT_FORMATS.get(settings.get("output_format"))
    logo = settings.get("logo") if settings["reencode"] else None
    if logo:
        ov = overlay_filter(settings.get("overlay"))
        width = fmt.wsq if fmt else settings["src_width"]
        cmd += ["-i", logo["path"],
                "-filter_complex", video_graph("[0:v:0]", fmt, None, ov, logo, 1, width),
                "-map", "[vout]", "-map", "0:a:0?"]
        cmd += encode_args(settings) + (interlace_args(fmt, "libx264") if fmt else [])
    elif settings["reencode"]:
        ov = overlay_filter(settings.get("overlay"))
        if fmt:
            cmd += ["-vf", video_chain(fmt, None, ov)]
        elif ov:
            cmd += ["-vf", ov]
        cmd += encode_args(settings) + (interlace_args(fmt, "libx264") if fmt else [])
    else:
        cmd += ["-c", "copy"]
    return cmd + output_args(settings)


def is_progress_line(line):
    return line.startswith(("frame=", "size=")) and "bitrate=" in line


# ---------------- media probing (folder mode) ----------------
class ProbeError(Exception):
    pass


def parse_rate(text):
    try:
        num, den = str(text).split("/")
        return float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_media(ffprobe, ffmpeg, path):
    """Return stream info for a playable file, or raise ProbeError explaining why it isn't."""
    try:
        r = run_quiet([ffprobe, "-v", "error",
                       "-show_entries",
                       "format=duration:stream=index,codec_type,codec_name,width,height,"
                       "avg_frame_rate,r_frame_rate,sample_rate,channels,field_order,pix_fmt,"
                       "sample_aspect_ratio:stream_disposition=attached_pic",
                       "-of", "json", path], PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe timed out")
    if r.returncode != 0:
        raise ProbeError(last_line(r.stderr, "ffprobe could not read the file"))
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise ProbeError("unreadable ffprobe output")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise ProbeError("no video stream")
    try:
        duration = float(data.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ProbeError("unknown or zero duration")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError("invalid video dimensions")

    fps_str = video.get("avg_frame_rate", "0/0")
    if not 1 <= parse_rate(fps_str) <= 120:
        fps_str = video.get("r_frame_rate", "0/0")
    fps = parse_rate(fps_str)
    if not 1 <= fps <= 120:
        fps_str, fps = "25", 25.0

    # Decode the first seconds to catch files that probe fine but can't actually be played
    try:
        r = run_quiet([ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                       "-t", str(DECODE_TEST_S), "-i", path, "-f", "null", "-"], DECODE_TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ProbeError("test decode timed out")
    if r.returncode != 0:
        raise ProbeError("test decode failed: " + last_line(r.stderr, f"exit code {r.returncode}"))

    return {
        "path": path,
        "name": os.path.basename(path),
        "duration": duration,
        "v_index": video["index"],
        "vcodec": video.get("codec_name", "?"),
        "width": width,
        "height": height,
        "fps": fps,
        "fps_str": fps_str,
        "has_audio": audio is not None,
        "a_index": audio["index"] if audio else None,
        "acodec": audio.get("codec_name") if audio else None,
        "sample_rate": audio.get("sample_rate") if audio else None,
        "channels": audio.get("channels") if audio else None,
        "interlaced": video.get("field_order") in INTERLACED_ORDERS,
        "pix_fmt": video.get("pix_fmt"),
        "sar": (video.get("sample_aspect_ratio") or "1:1").replace(":", "/")
               if video.get("sample_aspect_ratio") not in (None, "0:1", "N/A") else "1",
    }


def copy_signature(info):
    """Stream parameters that must match for files to be copied back-to-back into one stream."""
    return (info["vcodec"], info["width"], info["height"], round(info["fps"], 2),
            info["has_audio"], info["acodec"], info["sample_rate"], info["channels"])


def describe(info):
    audio = (f"{info['acodec']} {info['sample_rate']} Hz {info['channels']}ch"
             if info["has_audio"] else "no audio")
    return f"{info['vcodec']} {info['width']}x{info['height']} {info['fps']:.2f} fps, {audio}"


# ======================================================================
# Saved settings (shared by Video Pusher and Video Receiver)
# ======================================================================
def config_dir():
    """Per-user settings folder: %APPDATA%\\VideoPusher on Windows, ~/Library/Application Support/
    VideoPusher on macOS, ~/.config/VideoPusher on Linux. VIDEO_PUSHER_CONFIG_DIR overrides it."""
    override = os.environ.get("VIDEO_PUSHER_CONFIG_DIR")
    if override:
        return override
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "VideoPusher")


def settings_file(name):
    return os.path.join(config_dir(), f"{name}-settings.json")


def load_app_settings(name):
    """(settings dict, error message or None). A missing file is not an error."""
    path = settings_file(name)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return (data, None) if isinstance(data, dict) else ({}, f"ignored unreadable settings file {path}")
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, f"ignored unreadable settings file {path} ({e})"


def save_app_settings(name, data):
    """Write the settings atomically; returns an error message or None."""
    path = settings_file(name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return None
    except OSError as e:
        return f"couldn't save settings to {path} ({e})"


class PersistMixin:
    """Saves the window's settings on Start and on close, and restores them at the next launch.
    Subclasses define SETTINGS_NAME, _persist_vars(), _persist_extras() and _restore_extras().
    Passwords (the RIST passphrase) are never included."""
    SETTINGS_NAME = "app"

    def _persist_vars(self):
        """{key: (tk variable, allowed values or None)}"""
        return {}

    def _persist_extras(self):
        return {}

    def _restore_extras(self, data):
        pass

    def _save_settings(self):
        data = {"settings_version": 1, "app_version": APP_VERSION}
        for key, (var, _allowed) in self._persist_vars().items():
            try:
                data[key] = var.get()
            except (tk.TclError, ValueError):
                pass
        data.update(self._persist_extras())
        err = save_app_settings(self.SETTINGS_NAME, data)
        if err:
            self._log("Note: " + err)

    def _load_settings(self):
        data, err = load_app_settings(self.SETTINGS_NAME)
        if err:
            self._log("Note: " + err)
        if not data:
            return
        for key, (var, allowed) in self._persist_vars().items():
            if key not in data:
                continue
            value = data[key]
            if isinstance(var, tk.BooleanVar):
                if not isinstance(value, bool):
                    continue
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                value = str(value)
            else:
                continue
            if allowed is not None and value not in allowed:
                continue                       # e.g. an option removed in a newer version
            var.set(value)
        self._restore_extras(data)
        self._log(f"Settings restored from {settings_file(self.SETTINGS_NAME)}")


class SecurityMixin:
    """SRT encryption and RTMP stream key controls, shared by Video Pusher and Video Receiver.
    Passphrases and stream keys are typed into hidden fields, never logged and never saved."""

    def _build_security(self, parent, on_change):
        # SRT: encryption, passphrase, stream ID
        self.frm_srt = ttk.Frame(parent)
        ttk.Label(self.frm_srt, text="Encryption").pack(side=tk.LEFT)
        self.var_srt_enc = tk.StringVar(value="Off")
        self.cmb_srt_enc = ttk.Combobox(self.frm_srt, textvariable=self.var_srt_enc, state="readonly",
                                        width=8, values=list(SRT_ENCRYPTION))
        self.cmb_srt_enc.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_srt_enc.bind("<<ComboboxSelected>>", lambda e: on_change())
        ttk.Label(self.frm_srt, text="Passphrase").pack(side=tk.LEFT)
        self.var_srt_pass = tk.StringVar(value="")
        self.ent_srt_pass = ttk.Entry(self.frm_srt, textvariable=self.var_srt_pass, show="*", width=16)
        self.ent_srt_pass.pack(side=tk.LEFT, padx=(4, 12))
        self.lbl_srt_streamid = ttk.Label(self.frm_srt, text="Stream ID")
        self.lbl_srt_streamid.pack(side=tk.LEFT)
        self.var_srt_streamid = tk.StringVar(value="")
        self.ent_srt_streamid = ttk.Entry(self.frm_srt, textvariable=self.var_srt_streamid, width=16)
        self.ent_srt_streamid.pack(side=tk.LEFT, padx=(4, 0))
        # RTMP: stream key
        self.frm_rtmp = ttk.Frame(parent)
        ttk.Label(self.frm_rtmp, text="Stream key").pack(side=tk.LEFT)
        self.var_rtmp_key = tk.StringVar(value="")
        self.ent_rtmp_key = ttk.Entry(self.frm_rtmp, textvariable=self.var_rtmp_key, show="*", width=28)
        self.ent_rtmp_key.pack(side=tk.LEFT, padx=(4, 6))
        self.var_show_key = tk.BooleanVar(value=False)
        self.chk_show_key = ttk.Checkbutton(
            self.frm_rtmp, text="Show", variable=self.var_show_key,
            command=lambda: self.ent_rtmp_key.config(show="" if self.var_show_key.get() else "*"))
        self.chk_show_key.pack(side=tk.LEFT)
        self.lbl_rtmp_note = ttk.Label(self.frm_rtmp, foreground="gray", text="")
        self.lbl_rtmp_note.pack(side=tk.LEFT, padx=(10, 0))

    def _update_security_controls(self, locked, streamid_on, key_on, key_note=""):
        """streamid_on: this app calls out (SRT caller); key_on: the stream key is used/checked."""
        def st(on):
            return tk.NORMAL if on and not locked else tk.DISABLED
        self.cmb_srt_enc.config(state="readonly" if not locked else tk.DISABLED)
        self.ent_srt_pass.config(state=st(self.var_srt_enc.get() != "Off"))
        self.ent_srt_streamid.config(state=st(streamid_on))
        self.lbl_srt_streamid.config(foreground="" if streamid_on else "gray")
        self.ent_rtmp_key.config(state=st(key_on))
        self.chk_show_key.config(state=st(key_on))
        self.lbl_rtmp_note.config(text=key_note)

    def _read_security(self, protocol, endpoint, streamid_on, key_on):
        """Validate the SRT / RTMP security fields. A passphrase or key typed into the address is
        moved into its hidden field. Returns (settings to add, address to show/save) or None."""
        extra = {}
        if protocol == "SRT":
            endpoint, typed = pop_url_param(endpoint, "passphrase")
            endpoint, keylen = pop_url_param(endpoint, "pbkeylen")
            if typed is not None:
                self.var_srt_pass.set(typed)
                names = {v: k for k, v in SRT_ENCRYPTION.items() if v}
                if keylen in names:
                    self.var_srt_enc.set(names[keylen])
                elif self.var_srt_enc.get() == "Off":
                    self.var_srt_enc.set("AES-128")
                self._log("Note: moved the SRT passphrase from the address into the Passphrase field "
                          "(it is hidden and never saved).")
            encryption = SRT_ENCRYPTION[self.var_srt_enc.get()]
            passphrase = self.var_srt_pass.get()
            if encryption:
                lo, hi = SRT_PASSPHRASE_LEN
                if not lo <= len(passphrase) <= hi:
                    self._log(f"Error: the SRT passphrase must be {lo} to {hi} characters "
                              f"(it has {len(passphrase)}), or set Encryption to Off.")
                    return None
            cmd_endpoint = endpoint
            streamid = self.var_srt_streamid.get().strip()
            if streamid and streamid_on:
                cmd_endpoint = set_url_param(endpoint, "streamid", streamid)[0]
            extra.update(srt_encryption=encryption, srt_passphrase=passphrase if encryption else "",
                         srt_streamid=streamid if streamid_on else "", endpoint=cmd_endpoint)
        elif protocol == "RTMP" and key_on:
            base, typed = split_rtmp_key(endpoint)
            if typed:
                if self.var_rtmp_key.get() and self.var_rtmp_key.get() != typed:
                    self._log("Error: the address ends in a stream key and the Stream key field has a "
                              "different one. Remove the key from the address.")
                    return None
                self.var_rtmp_key.set(typed)
                endpoint = base
                self._log("Note: moved the stream key from the address into the Stream key field "
                          "(it is hidden and never saved).")
            key = self.var_rtmp_key.get().strip()
            if any(c in key for c in "/?# "):
                self._log("Error: the stream key can't contain '/', '?', '#' or spaces.")
                return None
            extra.update(rtmp_key=key, endpoint=rtmp_url(endpoint, key))
        else:
            extra["endpoint"] = endpoint
        return extra, endpoint


def str_dict(value, keys):
    """Only the given keys of a saved {key: string} dict."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in keys and isinstance(v, str)}


# ======================================================================
# Workers (background threads; talk to the GUI only through the event queue)
# ======================================================================
class WorkerBase(threading.Thread):
    def __init__(self, events, secrets=()):
        super().__init__(daemon=True)
        self.events = events
        self.stop_event = threading.Event()
        self.secrets = list(secrets)   # never shown: masked in everything sent to the GUI

    def emit(self, kind, payload=""):
        if isinstance(payload, str) and payload:
            payload = mask_secrets(payload, self.secrets)
        self.events.put((kind, payload))

    def log(self, msg):
        self.emit("log", f"[{time.strftime('%H:%M:%S')}] {msg}")


class SingleFileWorker(WorkerBase):
    """Loops one file with -stream_loop; relaunches FFmpeg if the stream drops."""

    def __init__(self, cmd, reconnect, reconnect_delay, events, cwd=None, listen=None, secrets=()):
        super().__init__(events, cmd_secrets(cmd) + list(secrets))
        self.cmd = cmd
        self.listen = listen      # None (push), or the protocol name when waiting for a receiver
        self.cwd = cwd            # ASSETS_DIR when the date/time overlay needs the bundled font
        self.reconnect = reconnect
        self.reconnect_delay = reconnect_delay
        self.proc = None
        self._lock = threading.Lock()

    def stop(self):
        self.stop_event.set()
        with self._lock:
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("q\n")        # ask FFmpeg to quit cleanly
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def run(self):
        reason = "stopped by user"
        self.log("Command: " + subprocess.list2cmdline(redact(self.cmd, self.secrets)))

        while not self.stop_event.is_set():
            waits = self.listen in ("SRT", "RTMP")     # these block until a receiver connects
            if waits:
                self.emit("status", f"Listening - waiting for a receiver to connect ({self.listen})")
                self.log("Waiting for a receiver to connect ...")
            elif self.listen:
                self.emit("status", "Streaming - RIST receivers can connect at any time")
            else:
                self.emit("status", "Streaming")
            try:
                proc = popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=self.cwd,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except OSError as e:
                self.log(f"Could not start FFmpeg: {e}")
                reason = "FFmpeg failed to start"
                break

            with self._lock:
                self.proc = proc
            threading.Thread(target=self._kill_if_stuck, args=(proc,), daemon=True).start()

            connected = False
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if waits and not connected and line.startswith("Output #0"):
                    connected = True
                    self.emit("status", "Streaming - receiver connected")
                    self.log("Receiver connected.")
                self.emit("stats" if is_progress_line(line) else "log", line)

            code = proc.wait()
            with self._lock:
                self.proc = None

            if self.stop_event.is_set():
                break

            if waits and connected:
                self.log("Receiver disconnected.")
            else:
                self.log(f"FFmpeg exited (code {code}).")
            if not self.reconnect:
                reason = "stream dropped, auto-reconnect off"
                break

            again = "Listening again" if self.listen else "Reconnecting"
            self.emit("status", f"{again} in {self.reconnect_delay} s...")
            self.log(f"{again} in {self.reconnect_delay} s...")
            if self.stop_event.wait(self.reconnect_delay):  # wakes immediately on Stop
                break

        self.log(f"Stopped ({reason}).")
        self.emit("done")

    def _kill_if_stuck(self, proc):
        """After Stop, give FFmpeg a grace period to exit, then force it."""
        while proc.poll() is None:
            if self.stop_event.wait(0.5):
                try:
                    proc.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return


class PlaylistWorker(WorkerBase):
    """Plays every video in a folder, in name order, in a loop, over one output connection.
    With settings["mode"] == "file" it loops a single file the same way (a playlist of one).

    Each file is encoded by a short-lived 'feeder' FFmpeg that pipes MPEG-TS into one long-lived
    output FFmpeg, so the connection stays up across files. When re-encoding, the date/time and
    logo overlays can be changed while streaming (update_overlays): the feeder is restarted at
    the same point in the file with the new settings, and the output carries on."""

    def __init__(self, settings, ffmpeg_path, ffprobe_path, events):
        super().__init__(events, secret_values(settings))
        self.settings = settings
        self.single = settings.get("mode") == "file"
        source = os.path.abspath(settings["source"])        # absolute: feeders may run in ASSETS_DIR
        self.source_file = source if self.single else None
        self.folder = os.path.dirname(source) if self.single else source
        self.live_overlays = bool(settings["reencode"])     # overlays can change while streaming
        self._overlay_gen = 0           # bumped by update_overlays()
        self._restart_requested = False # the current feeder was stopped to apply new overlays
        self._resume_at = 0.0           # where in the file to carry on after such a restart
        self._loops = 0                 # single-file mode: completed passes
        # Live commands (FFmpeg's zmq filter): change the overlays in the running feeder without
        # restarting it. Decided at Start; otherwise changes restart the feeder.
        self._zmq = False
        self._can_text = False
        self._next_live = None          # what the feeder being built can change live
        self._feeder_live = None        # ... and the one running: {"port", "gen", "text", "logo", "width"}
        self._cmd_lock = threading.Lock()
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path
        self._lock = threading.Lock()
        self.out_proc = None
        self.out_dead = threading.Event()
        self.out_start = 0.0
        self.next_offset = 0.0   # where the next file's timestamps start (s since output start)
        self.feeder = None
        self.reference = None    # first played file: copy-mode format / re-encode target
        # re-encode output format: chosen in the UI, or taken from the first file ("Auto")
        self.target = OUTPUT_FORMATS.get(settings.get("output_format"))
        # test signal while nothing is playable: None, "bars" or "black"
        self.test_signal = settings.get("test_signal")
        self._slate_levels = []
        self._slate_active = False
        self._slate_cmd_logged = False
        self._caps = None
        self._idle_logged = False
        self._warned_copy_slate = False
        # Listen mode (SRT/RTMP): the output waits for a receiver. Meanwhile the playlist keeps
        # running in real time and its data goes through a small queue that drops the oldest data
        # when full, so a receiver that connects later gets the stream live (no backlog burst).
        listen = settings.get("connection") == "listen"
        self.listen_wait = listen and settings["protocol"] in ("SRT", "RTMP")
        self.listen_rist = listen and settings["protocol"] == "RIST"
        self.receiver_connected = False
        self._out_queue = None
        self._cache = {}         # (path, size, mtime) -> (info, error)
        self._warned = set()
        self._feeder_cmd_logged = False
        self._pool = ThreadPoolExecutor(max_workers=1)   # look-ahead checker
        self._feeder_is_file = False   # the running feeder plays a file (not the test signal)
        # Folder watcher: path -> (size, mtime, monotonic time it was first seen with that size/mtime).
        # A file is only used once its size and mtime have stayed the same for settle_s seconds,
        # so files that are still being copied into the folder are never probed or played.
        # (a single source file is used at once: the user chose it)
        self.settle_s = 0 if self.single else settings.get("settle_s", FILE_SETTLE_S)
        self._seen = {}
        self._seen_lock = threading.Lock()
        self._settling_count = 0

    # ---------- live overlay changes ----------
    def update_overlays(self, overlay, logo):
        """Use new date/time and logo settings (None = off) from now on (called from the GUI).
        The running feeder gets them as live commands when possible (instant); otherwise it is
        restarted where it is (about a second). Either way the connection stays up."""
        if not self.live_overlays:
            return False
        with self._lock:
            self.settings = dict(self.settings, overlay=overlay, logo=logo)
            self._overlay_gen += 1
            self._feeder_cmd_logged = False        # log the new feeder command once
        threading.Thread(target=self._apply_overlays, daemon=True).start()
        return True

    def _apply_overlays(self):
        with self._cmd_lock:                       # one change at a time, in order
            with self._lock:
                feeder = self.feeder if self._feeder_is_file else None
                live = self._feeder_live
                gen, settings = self._overlay_gen, self.settings
            if feeder is None or feeder.poll() is not None or self.stop_event.is_set():
                return                             # the next feeder starts with the new settings
            if live is not None and live["gen"] == gen:
                return                             # already showing these settings
            why = "this FFmpeg build has no zmq filter"
            if live is not None:
                why = self._send_live(live, settings.get("overlay"), settings.get("logo"))
                if why is None:
                    live["gen"] = gen
                    self.log("Overlay settings changed - applied instantly (live commands).")
                    return
            with self._lock:
                restart = self.feeder is feeder and feeder.poll() is None
                if restart:
                    self._restart_requested = True
            if restart:
                if live is not None:
                    self.log(f"Restarting the feeder for this change ({why}).")
                feeder.terminate()

    def _send_live(self, live, overlay, logo):
        """Send the change to the running feeder. None if done, else why it can't be done live."""
        cmds = []
        if overlay and not live["text"]:
            return "the date/time wasn't in the stream yet"
        if live["text"]:
            cmds.append(f"drawtext@{LIVE_TEXT} reinit " + (drawtext_options(overlay) if overlay else
                                                            drawtext_options(DEFAULT_OVERLAY, visible=False)))
        if logo:
            if live["logo"] is None:
                return "adding a logo"
            if live["logo"] != (logo["path"], logo["size"]):
                return "a different logo file or size"
        if live["logo"] is not None:
            cmds += live_logo_commands(logo, live["width"])
        try:
            with ZmqRequester(live["port"]) as z:
                for cmd in cmds:
                    reply = z.request(cmd)
                    if not reply.startswith("0 "):
                        return f"FFmpeg replied '{reply}'"
        except ZmqError as e:
            return f"live command failed: {e}"
        return None

    def _list(self):
        """The files to play: the folder's videos, or the single source file."""
        if self.single:
            return [self.source_file] if os.path.isfile(self.source_file) else []
        return list_videos(self.folder)

    # ---------- stop ----------
    def stop(self):
        self.stop_event.set()
        with self._lock:
            feeder, out = self.feeder, self.out_proc
        if feeder and feeder.poll() is None:
            feeder.terminate()
        if out and out.poll() is None:
            try:
                out.stdin.close()                # EOF -> output FFmpeg finishes cleanly
            except (OSError, ValueError):
                pass

    # ---------- main loop ----------
    def run(self):
        reason = "stopped by user"
        if self.single:
            self.log(f"Source file (looped): {self.source_file}")
        else:
            self.log(f"Playlist folder: {self.folder}")
            self.log(f"New or changed files are used once their size has been stable for {self.settle_s} s.")
        if self.live_overlays:
            caps = ffmpeg_caps(self.ffmpeg)
            self._can_text = "drawtext" in caps["filters"] and os.path.isfile(os.path.join(ASSETS_DIR, SLATE_FONT))
            self._zmq = "zmq" in caps["filters"] and self.settings.get("live_commands", True)
            self.log("Date/time and logo can be changed while streaming (Apply to live stream): " +
                     ("instantly, with live commands (FFmpeg zmq filter)." if self._zmq else
                      "the file playing restarts where it is (this FFmpeg build has no zmq filter)."))
        if self.target:
            self.log(f"Output format: {describe_target(self.target)} (selected).")
        if self.test_signal:
            self._caps = ffmpeg_caps(self.ffmpeg)
            self._slate_levels = slate_levels(self._caps)
            what = {"text": "caption and clock (bundled font)", "image": "caption image (FFmpeg has no "
                    "text drawing)", "plain": "no caption (caption image missing)"}[self._slate_levels[0]]
            self.log(f"Test signal when no file is playable: "
                     f"{'bars & tone' if self.test_signal == 'bars' else 'black & silence'}, {what}.")
        self._scan_folder(initial=True)
        # A second look one second later catches copies in progress at Start even when the copy
        # tool preserved an old modification time (Windows Explorer does): their size changes.
        if not self.stop_event.wait(FOLDER_POLL_S):
            self._scan_folder()
        threading.Thread(target=self._watch_folder, daemon=True).start()
        lookahead = None   # Future -> next playable file (checked while the current one plays)
        pending = None     # file to replay first after an output reconnect
        current_path = None
        failed_path = None # file that just failed to play (e.g. deleted after it was checked)
        try:
            while not self.stop_event.is_set():
                if not self._start_output():
                    reason = "FFmpeg failed to start"
                    break
                lost = False

                while not self.stop_event.is_set():
                    if self.out_dead.is_set():
                        lost = True
                        break
                    if time.monotonic() - self.out_start > MAX_OUTPUT_RUN_S:
                        self.log("Scheduled daily output restart (keeps MPEG-TS timestamps clear of wrap-around).")
                        break

                    item = None
                    if pending:
                        item, pending = pending, None
                    else:
                        if lookahead is None:
                            lookahead = self._pool.submit(self._find_next, current_path)
                        if failed_path and not lookahead.done():
                            # the last file failed at once, so the look-ahead hasn't had time to
                            # check another one: play an already-checked file instead of pausing
                            item = self._quick_next(failed_path)
                            if item:
                                self.log(f"Continuing with {item[0]['name']} (already checked).")
                                lookahead = None   # discard the look-ahead still running
                        if item is None and self._slate_active and not lookahead.done():
                            # keep the test signal going while the folder is being checked
                            item = self._slate_item()
                        if item is None:
                            item = self._wait_for(lookahead)
                            lookahead = None
                        if self.stop_event.is_set():
                            break
                        if item is None:
                            if self.test_signal:
                                if not self._slate_active:
                                    self._slate_active = True
                                    self.log("No playable file - sending test signal until one is ready.")
                                item = self._slate_item()
                            elif self._settling_count:
                                # nothing ready yet, but files are still being copied in
                                self.emit("status", "Waiting for files to finish copying")
                                if self.stop_event.wait(SETTLING_RETRY_S):
                                    break
                                continue
                            else:
                                self.emit("status", "No playable files - waiting")
                                if not self._idle_logged:
                                    self._idle_logged = True
                                    self.log(("The source file can't be played (see above); checking again "
                                              if self.single else
                                              "No playable video files in the folder; checking again ")
                                             + f"every {NO_PLAYABLE_RETRY_S} s.")
                                if self.stop_event.wait(NO_PLAYABLE_RETRY_S):
                                    break
                                continue

                    info, pos, total = item
                    if info.get("slate"):
                        self._status("Test signal - no playable file")
                        result = self._play(info)
                        if result == "failed":
                            self._slate_failed()
                        elif result == "output":
                            lost = True
                            break
                        continue
                    if self._slate_active:
                        self._slate_active = False
                        self.log("Playable file ready - ending test signal.")
                    self._idle_logged = False
                    if not self._still_valid(info):
                        # deleted or replaced after it was checked: switch to a file that has
                        # already been checked, so the stream doesn't pause
                        quick = self._quick_next(info["path"])
                        if quick is None:
                            self.log(f"{info['name']} was removed or changed before it could play - "
                                     "finding another file.")
                            continue
                        self.log(f"{info['name']} was removed or changed before it could play - "
                                 f"switching to {quick[0]['name']} (already checked).")
                        info, pos, total = item = quick
                    current_path = info["path"]
                    # look ahead: probe + test-decode the following file while this one plays
                    lookahead = self._pool.submit(self._find_next, current_path)

                    if self.single:
                        self._status(f"Looping: {info['name']} ({fmt_duration(info['duration'])})" +
                                     (f" - pass {self._loops + 1}" if self._loops else ""))
                        if not self._loops:
                            self.log(f"Now playing {info['name']} ({fmt_duration(info['duration'])}), in a loop")
                    else:
                        self._status(f"Playing {pos}/{total}: {info['name']} ({fmt_duration(info['duration'])})")
                        self.log(f"Now playing {pos}/{total}: {info['name']} ({fmt_duration(info['duration'])})")
                    start_at = 0.0
                    while True:
                        result = self._play(info, start_at)
                        if result != "restart":
                            break
                        start_at = self._resume_at
                        self.log(f"Overlay settings changed - continuing {info['name']} from "
                                 f"{fmt_duration(start_at)} with the new settings.")
                    if result == "ok":
                        self._loops += 1
                    failed_path = info["path"] if result == "failed" else None
                    if result == "output":
                        pending = item          # replay this file once reconnected
                        lost = True
                        break

                self._close_output()
                if self.stop_event.is_set():
                    break
                if lost:
                    if self.listen_wait and self.receiver_connected:
                        self.log("Receiver disconnected.")
                    else:
                        self.log("Output connection lost.")
                    if not self.settings["reconnect"]:
                        reason = "stream dropped, auto-reconnect off"
                        break
                    delay = self.settings["reconnect_delay"]
                    again = "Listening again" if self.listen_wait or self.listen_rist else "Reconnecting"
                    self.emit("status", f"{again} in {delay} s...")
                    self.log(f"{again} in {delay} s...")
                    if self.stop_event.wait(delay):
                        break
        except Exception as e:          # never leave the UI stuck in "running"
            reason = f"unexpected error: {e}"
        finally:
            self._kill_feeder()
            self._close_output()
            self._pool.shutdown(wait=False, cancel_futures=True)
            self.log(f"Stopped ({reason}).")
            self.emit("done")

    def _wait_for(self, future):
        while not self.stop_event.is_set():
            try:
                return future.result(timeout=0.2)
            except FutureTimeout:
                continue
            except Exception as e:
                self.log(f"Look-ahead error: {e}")
                return None
        return None

    # ---------- output (one connection for the whole playlist) ----------
    def _start_output(self):
        cmd = [self.ffmpeg, "-hide_banner", "-f", "mpegts", "-i", "pipe:0",
               "-map", "0", "-c", "copy"] + output_args(self.settings)
        self.log("Output: " + subprocess.list2cmdline(redact(cmd, self.secrets)))
        try:
            proc = popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            self.log(f"Could not start FFmpeg: {e}")
            return False
        dead = threading.Event()
        with self._lock:
            self.out_proc, self.out_dead = proc, dead
        self.out_start = time.monotonic()
        self.next_offset = 0.0
        self.receiver_connected = False
        self._out_queue = None
        if self.listen_wait:
            self._out_queue = queue.Queue(maxsize=LISTEN_QUEUE_CHUNKS)
            threading.Thread(target=self._write_output, args=(proc, self._out_queue, dead), daemon=True).start()
            self.log("Waiting for a receiver to connect (the playlist keeps running meanwhile) ...")
        elif self.listen_rist:
            self.log("Listening for RIST receivers - they can connect at any time.")
        threading.Thread(target=self._read_output_log, args=(proc, dead), daemon=True).start()
        return True

    def _write_output(self, proc, q, dead):
        """Listen mode: write queued stream data to the output FFmpeg (blocks while it waits)."""
        while not dead.is_set():
            try:
                chunk = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                proc.stdin.write(chunk)
                proc.stdin.flush()
            except (OSError, ValueError):
                break

    def _send(self, out, chunk):
        """Pass stream data to the output: directly, or via the drop-oldest queue in listen mode."""
        q = self._out_queue
        if q is None:
            out.stdin.write(chunk)
            out.stdin.flush()
            return
        while True:
            try:
                q.put_nowait(chunk)
                return
            except queue.Full:
                try:
                    q.get_nowait()           # no receiver yet: drop the oldest data
                except queue.Empty:
                    pass

    def _status(self, text):
        if self.listen_wait and not self.receiver_connected:
            text += " - waiting for a receiver"
        self.emit("status", text)

    def _read_output_log(self, proc, dead):
        stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")  # \r -> line ends
        for raw in stream:
            line = raw.strip()
            if not line or any(n in line for n in BOUNDARY_NOISE):
                continue
            if self.listen_wait and not self.receiver_connected and line.startswith("Output #0"):
                self.receiver_connected = True
                self.log("Receiver connected.")
            self.emit("stats" if is_progress_line(line) else "log", line)
        dead.set()

    def _close_output(self):
        with self._lock:
            proc, self.out_proc = self.out_proc, None
        if not proc:
            return
        if self._out_queue is not None:
            try:
                self._out_queue.put_nowait(None)     # stop the writer thread
            except queue.Full:
                pass
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # ---------- feeder (one per file) ----------
    def _feeder_cmd(self, info, ts_offset, start_at=0.0):
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-re"]
        if start_at > 0:
            cmd += ["-ss", f"{start_at:.3f}"]          # carry on mid-file after an overlay change
        cmd += ["-i", info["path"]]
        self._next_live = None
        if self.settings["reencode"]:
            t = self.target
            if not info["has_audio"]:
                # silent track so every file has the same stream layout
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            overlay = self.settings.get("overlay")
            logo = self.settings.get("logo")
            zmq = None
            if self._zmq:
                # live commands: name the filters, and include the date/time (hidden when off)
                # so it can be switched on without a restart
                port = free_tcp_port()
                zmq = zmq_filter(port)
                if overlay:
                    text = overlay_filter(overlay, LIVE_TEXT)
                elif self._can_text:
                    text = f"drawtext@{LIVE_TEXT}=" + drawtext_options(DEFAULT_OVERLAY, visible=False)
                else:
                    text = None
                self._next_live = {"port": port, "text": bool(text), "width": t.wsq,
                                   "logo": (logo["path"], logo["size"]) if logo else None}
            else:
                text = overlay_filter(overlay)
            audio_map = f"0:{info['a_index']}" if info["has_audio"] else "1:a:0"
            if logo:
                li = 1 if info["has_audio"] else 2
                # live: the image is looped so that opacity changes reach the picture
                loop = ["-loop", "1", "-framerate", str(LIVE_LOGO_FPS)] if zmq else []
                cmd += loop + ["-i", logo["path"],
                               "-filter_complex", video_graph(f"[0:{info['v_index']}]", t,
                                                              info.get("interlaced", False), text, logo, li,
                                                              t.wsq, live=zmq),
                               "-map", "[vout]", "-map", audio_map]
            else:
                chain = video_chain(t, info.get("interlaced", False), text)
                cmd += ["-map", f"0:{info['v_index']}", "-map", audio_map,
                        "-vf", f"{zmq},{chain}" if zmq else chain]
            cmd += encode_args(self.settings) + interlace_args(t, "libx264") + ["-ar", "48000", "-ac", "2"]
            if not info["has_audio"]:
                cmd += ["-shortest"]
        else:
            cmd += ["-map", f"0:{info['v_index']}"]
            if info["has_audio"]:
                cmd += ["-map", f"0:{info['a_index']}"]
            cmd += ["-c", "copy"]
        # continue the timeline of the previous file so the output sees one stream
        cmd += ["-output_ts_offset", f"{ts_offset:.3f}", "-f", "mpegts", "pipe:1"]
        return cmd

    def _play(self, info, start_at=0.0):
        """Push one file (from start_at seconds) into the output. Returns 'ok', 'failed', 'output'
        (output lost), 'stopped', or 'restart' (overlays changed: play again from self._resume_at)."""
        # The file's timestamps continue exactly where the previous file ended (or at the current
        # time if there was a pause). FFmpeg's -re reads the first ~0.5 s of a file in a burst, so
        # wait here if the pushed timeline is too far ahead of real time - otherwise the lead
        # would grow with every file and eventually overflow the receiver's buffer.
        elapsed = time.monotonic() - self.out_start
        offset = max(elapsed, self.next_offset)
        if offset - elapsed > MAX_LEAD_S and self.stop_event.wait(offset - elapsed - MAX_LEAD_S):
            return "stopped"
        slate = bool(info.get("slate"))
        live = None
        if slate:
            cmd = self._slate_cmd(offset)
            if not self._slate_cmd_logged:
                self.log("Test signal: " + subprocess.list2cmdline(cmd))
                self._slate_cmd_logged = True
        else:
            with self._lock:
                gen = self._overlay_gen
            cmd = self._feeder_cmd(info, offset, start_at)
            live = self._next_live
            if live is not None:
                live["gen"] = gen
            if not self._feeder_cmd_logged:
                self.log("Feeder: " + subprocess.list2cmdline(cmd))
                self._feeder_cmd_logged = True
        try:
            # the test signal runs in the assets folder so the font/image are found by bare name
            feeder = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=ASSETS_DIR if slate or self.settings.get("overlay") or
                           (not slate and live and live["text"]) else None)
        except OSError as e:
            self.log(f"Could not start FFmpeg for {info['name']}: {e}")
            return "failed"
        with self._lock:
            self.feeder = feeder
            self._feeder_is_file = not slate
            self._feeder_live = None if slate else live
            out = self.out_proc
            if not slate and gen != self._overlay_gen:
                # overlays changed while this feeder was being started: restart it at once
                self._restart_requested = True
                feeder.terminate()
        errors = []
        err_thread = threading.Thread(target=self._read_feeder_errors, args=(feeder, errors, info["name"]),
                                      daemon=True)
        err_thread.start()

        started = time.monotonic()
        out_broken = False
        sent = 0
        try:
            while True:
                chunk = feeder.stdout.read1(PIPE_CHUNK)
                if not chunk:
                    break
                if self.out_dead.is_set():          # output gone (e.g. the receiver disconnected)
                    out_broken = True
                    break
                try:
                    self._send(out, chunk)
                    sent += len(chunk)
                except (OSError, ValueError):
                    out_broken = True
                    break
        finally:
            self._kill_feeder()
            err_thread.join(timeout=2)

        with self._lock:
            restart, self._restart_requested = self._restart_requested, False
        if sent:   # nothing sent (e.g. file vanished): the timeline doesn't move
            played = (max(0.0, info["duration"] - start_at) if feeder.returncode == 0 and not restart
                      else time.monotonic() - started + MAX_LEAD_S)
            self.next_offset = offset + played + FILE_GAP_S
        if self.stop_event.is_set():
            return "stopped"
        if out_broken or self.out_dead.is_set():
            return "output"
        if restart:
            # carry on from about where the receiver is (-re keeps the feeder near real time)
            self._resume_at = start_at + (time.monotonic() - started if sent else 0.0)
            if self._resume_at >= info["duration"] - 0.5:
                return "ok"                       # was about to end anyway: go to the next file
            return "restart"
        if feeder.returncode != 0 and live is not None and not sent and \
                any("zmq" in e.lower() for e in errors):
            # the live-command channel couldn't start (e.g. port taken): carry on without it
            self._zmq = False
            self.log("Live overlay commands unavailable - overlay changes will restart the feeder instead.")
            self._resume_at = start_at
            return "restart"
        if feeder.returncode != 0:
            msg = errors[-1] if errors else f"exit code {feeder.returncode}"
            if slate:
                self.log(f"Test signal failed ({msg}).")
                return "failed"
            self.log(f"{info['name']} failed after {time.monotonic() - started:.0f} s ({msg}) - "
                     "skipping it until the file changes.")
            key = info.get("key")
            if key:
                self._cache[key] = (None, f"failed during playback: {msg}")
            return "failed"
        return "ok"

    def _read_feeder_errors(self, proc, errors, name):
        stream = io.TextIOWrapper(proc.stderr, encoding="utf-8", errors="replace")
        for raw in stream:
            line = raw.strip()
            if line and not self.stop_event.is_set():   # messages caused by Stop are not errors
                errors.append(line)
                del errors[:-5]
                self.emit("log", f"[{name}] {line}")

    def _kill_feeder(self):
        with self._lock:
            feeder, self.feeder = self.feeder, None
            self._feeder_is_file = False
            self._feeder_live = None
        if not feeder:
            return
        if feeder.poll() is None:
            feeder.terminate()
        try:
            feeder.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            feeder.kill()
            feeder.wait()

    # ---------- look-ahead: choose and check the next file ----------
    def _find_next(self, after_path):
        """Next playable file after after_path (wrapping round the folder), as (info, pos, total),
        or None if nothing in the folder can be played. The folder is re-read every time, so
        files added, removed or replaced while streaming are picked up."""
        try:
            files = self._list()
        except OSError as e:
            self.log(f"Cannot read folder: {e.strerror or e}")
            return None
        if not files:
            return None

        start = 0
        if after_path:
            key = os.path.basename(after_path).lower()
            start = next((i for i, p in enumerate(files) if os.path.basename(p).lower() > key), 0)

        settling = 0
        result = None
        for path in files[start:] + files[:start]:
            if self.stop_event.is_set():
                return None
            ready, st = self._is_settled(path)
            if not ready:
                settling += 1
                if st is not None and (path, "settling", st.st_size) not in self._warned:
                    self._warned.add((path, "settling", st.st_size))
                    self.log(f"Not ready yet: {os.path.basename(path)} is new or still changing "
                             f"({st.st_size / 1e6:.1f} MB) - will use it once its size has been "
                             f"stable for {self.settle_s} s.")
                continue
            info, err = self._check(path)
            if info:
                err = self._compat_problem(info)
            if err:
                if (path, err) not in self._warned:
                    self._warned.add((path, err))
                    self.log(f"Skipping {os.path.basename(path)}: {err}")
                continue
            if self.reference is None:
                self._set_reference(info)
            result = info, files.index(path) + 1, len(files)
            break
        self._settling_count = settling
        return result

    def _still_valid(self, info):
        """True if the file is still there, unchanged since it was checked."""
        try:
            st = os.stat(info["path"])
        except OSError:
            return False
        return info.get("key") == (info["path"], st.st_size, st.st_mtime)

    def _quick_next(self, after_path):
        """Like _find_next, but only considers files already checked and unchanged since, so it
        returns at once (no probing). Used to avoid a pause when the chosen file disappears."""
        try:
            files = self._list()
        except OSError:
            return None
        if not files:
            return None
        key = os.path.basename(after_path).lower()
        start = next((i for i, p in enumerate(files) if os.path.basename(p).lower() > key), 0)
        for path in files[start:] + files[:start]:
            if path == after_path:
                continue
            ready, st = self._is_settled(path)
            if not ready:
                continue
            info, _err = self._cache.get((path, st.st_size, st.st_mtime), (None, None))
            if info is None or self._compat_problem(info):
                continue
            return info, files.index(path) + 1, len(files)
        return None

    def _scan_folder(self, initial=False):
        """Record each video file's size and mtime, noting when they last changed."""
        try:
            files = self._list()
        except OSError:
            return
        now = time.monotonic()
        current = {}
        with self._seen_lock:
            for path in files:
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                sig = (st.st_size, st.st_mtime)
                prev = self._seen.get(path)
                if prev and prev[:2] == sig:
                    current[path] = prev
                elif initial and st.st_size > 0 and time.time() - st.st_mtime >= self.settle_s:
                    # already in the folder at Start and not modified recently: ready straight away
                    current[path] = (*sig, now - self.settle_s)
                else:
                    current[path] = (*sig, now)
            self._seen = current

    def _watch_folder(self):
        while not self.stop_event.wait(FOLDER_POLL_S):
            self._scan_folder()

    def _is_settled(self, path):
        """(ready, stat): ready once size and mtime have been unchanged for settle_s seconds."""
        try:
            st = os.stat(path)
        except OSError:
            return False, None
        with self._seen_lock:
            entry = self._seen.get(path)
            if entry is None or entry[:2] != (st.st_size, st.st_mtime):
                # not seen by the watcher yet, or changed since: (re)start its clock now
                self._seen[path] = (st.st_size, st.st_mtime, time.monotonic())
                return False, st
        ready = st.st_size > 0 and time.monotonic() - entry[2] >= self.settle_s
        return ready, st

    def _check(self, path):
        try:
            st = os.stat(path)
        except OSError as e:
            return None, f"cannot read file ({e.strerror})"
        key = (path, st.st_size, st.st_mtime)
        if key not in self._cache:
            try:
                info = probe_media(self.ffprobe, self.ffmpeg, path)
                info["key"] = key
                self._cache[key] = (info, None)
            except ProbeError as e:
                self._cache[key] = (None, str(e))
        return self._cache[key]

    def _compat_problem(self, info):
        if self.settings["reencode"]:
            return None   # everything is normalised to the same format
        if self.settings["protocol"] == "RTMP" and (
                info["vcodec"] != "h264" or (info["has_audio"] and info["acodec"] != "aac")):
            return (f"{info['vcodec']}/{info['acodec'] or 'no audio'} can't be sent over RTMP without "
                    "re-encode (needs H.264/AAC)")
        if self.reference and copy_signature(info) != copy_signature(self.reference):
            return (f"format differs from the first file ({describe(info)} vs {describe(self.reference)}); "
                    "tick Re-encode to mix formats")
        return None

    # ---------- test signal ----------
    def _slate_item(self):
        return {"slate": True, "name": "test signal", "path": None, "duration": SLATE_SEGMENT_S}, 0, 0

    def _slate_target(self):
        """Format of the test signal: the stream's format, so receivers see no change."""
        if self.settings["reencode"]:
            if self.target is None:
                # "Auto" with no file to match yet: fix the stream format now so it never changes
                self.target = OUTPUT_FORMATS[DEFAULT_FORMAT]
                self.log(f"Output format: H.264/AAC {describe_target(self.target)} (Auto, but no file "
                         "to match yet - files will be converted to this format).")
            return self.target
        if self.reference is not None:  # copy mode: match the files
            return target_from_info(self.reference)
        if not self._warned_copy_slate:
            self._warned_copy_slate = True
            self.log("Copy mode: no file has played yet, so the test signal uses the selected output "
                     "format; receivers will see a format change if the files differ from it.")
        return OUTPUT_FORMATS.get(self.settings.get("output_format")) or OUTPUT_FORMATS[DEFAULT_FORMAT]

    def _slate_cmd(self, ts_offset):
        t = self._slate_target()
        reference = None if self.settings["reencode"] else self.reference
        vargs, aargs, sr, ch = slate_codecs(self.settings, reference, self._caps)
        if self.settings["protocol"] == "RIST":
            bw = self.settings["bandwidth_k"]
            kbps = RIST_SLATE_KBPS if bw <= 0 else max(RIST_MIN_SLATE_KBPS, min(RIST_SLATE_KBPS, bw - 200))
            vargs = constant_rate(vargs, kbps)
        return slate_cmd(self.ffmpeg, self.test_signal, self._slate_levels[0], t, vargs, aargs, sr, ch,
                         SLATE_SEGMENT_S, ts_offset)

    def _slate_failed(self):
        """Drop to a simpler test signal (no clock -> no caption); give up if even that fails."""
        self._slate_cmd_logged = False
        failed = self._slate_levels.pop(0)
        if self._slate_levels:
            nxt = {"image": "caption image", "plain": "no caption"}[self._slate_levels[0]]
            self.log(f"Test signal: '{failed}' style failed - switching to {nxt}.")
        else:
            self.log("Test signal could not be generated by this FFmpeg build - disabled; "
                     "the stream will pause until a file is playable.")
            self.test_signal = None
            self._slate_active = False

    def _set_reference(self, info):
        self.reference = info
        if self.settings["reencode"]:
            if self.target is None:
                self.target = target_from_info(info)
                self.log(f"Output format: H.264/AAC {describe_target(self.target)} "
                         f"(taken from {info['name']}; other files are converted to match)")
        else:
            self.log(f"Copy mode format: {describe(info)} (from {info['name']}); "
                     "files that differ will be skipped")


# ======================================================================
# GUI
# ======================================================================
LABEL_WIDTH = 16                 # characters: aligns the setting names across the panels


class CollapsiblePanel(ttk.Frame):
    """A titled group of settings. Clicking the title collapses it to one line that shows a
    summary of its settings, and expands it again."""

    def __init__(self, parent, title, expanded=True, on_toggle=None):
        super().__init__(parent)
        self.on_toggle = on_toggle
        head = ttk.Frame(self, cursor="hand2")
        head.pack(fill=tk.X)
        self.arrow = ttk.Label(head, width=2, style="PanelTitle.TLabel")
        self.arrow.pack(side=tk.LEFT)
        self.lbl_title = ttk.Label(head, text=title, style="PanelTitle.TLabel")
        self.lbl_title.pack(side=tk.LEFT)
        self.lbl_summary = ttk.Label(head, foreground="gray", text="")
        self.lbl_summary.pack(side=tk.LEFT, padx=(12, 0))
        for w in (head, self.arrow, self.lbl_title, self.lbl_summary):
            w.bind("<Button-1>", self.toggle)
        ttk.Separator(self).pack(fill=tk.X, pady=(2, 0))
        self.body = ttk.Frame(self, padding=(18, 4, 0, 6))
        self.expanded = None
        self.set_expanded(expanded)

    def set_expanded(self, expanded):
        self.expanded = bool(expanded)
        self.arrow.config(text="▼" if self.expanded else "▶")
        if self.expanded:
            self.body.pack(fill=tk.X)
            self.lbl_summary.pack_forget()
        else:
            self.body.pack_forget()
            self.lbl_summary.pack(side=tk.LEFT, padx=(12, 0))

    def toggle(self, _event=None):
        self.set_expanded(not self.expanded)
        if self.on_toggle:
            self.on_toggle()

    def set_summary(self, text):
        self.lbl_summary.config(text=text)


class VideoPusherApp(SecurityMixin, PersistMixin, tk.Tk):
    SETTINGS_NAME = "pusher"

    def __init__(self):
        super().__init__()
        self.title(f"Video Pusher v{APP_VERSION}")
        self.geometry("960x900")
        self.minsize(860, 560)
        try:
            import tkinter.font as tkfont
            bold = tkfont.nametofont("TkDefaultFont").copy()
            bold.configure(weight="bold")
            ttk.Style(self).configure("PanelTitle.TLabel", font=bold)
        except (ImportError, tk.TclError, AttributeError):
            pass

        self.ffmpeg_path = find_tool("ffmpeg")
        self.ffprobe_path = find_tool("ffprobe")
        self.events = queue.Queue()
        self.worker = None
        self._closing = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self._poll_job = self.after(100, self._poll_events)
        self._log(f"Ready. Expecting FFmpeg at: {self.ffmpeg_path}")
        self._load_settings()

    # ---------------- UI ----------------
    def _build_ui(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)
        self._locked_widgets = []
        self._panels = {}
        panels = ttk.Frame(main)
        panels.pack(fill=tk.X)

        def panel(name, title, expanded):
            p = CollapsiblePanel(panels, title, expanded=expanded, on_toggle=self._on_panel_toggle)
            p.pack(fill=tk.X, pady=(0, 4))
            p.body.columnconfigure(1, weight=1)
            self._panels[name] = p
            return p.body

        def label(parent, text, row):
            lbl = ttk.Label(parent, text=text, width=LABEL_WIDTH)
            lbl.grid(row=row, column=0, sticky=tk.W, pady=3)
            return lbl

        def line(parent, row):
            """A frame spanning the value columns of a panel row."""
            f = ttk.Frame(parent)
            f.grid(row=row, column=1, columnspan=2, sticky=tk.W, padx=(6, 0), pady=3)
            return f

        def hint(parent, row, width=720):
            lbl = ttk.Label(parent, foreground="gray", text="", wraplength=width)
            lbl.grid(row=row, column=1, columnspan=2, sticky=tk.W, padx=(6, 0), pady=(0, 3))
            return lbl

        # ---------------- Source ----------------
        src = panel("source", "Source", True)
        label(src, "Mode:", 0)
        mode = line(src, 0)
        self.var_mode = tk.StringVar(value="file")
        for value, text in (("file", "Single file (loop)"), ("folder", "Folder playlist (loop)")):
            rb = ttk.Radiobutton(mode, text=text, value=value, variable=self.var_mode, command=self._on_mode_change)
            rb.pack(side=tk.LEFT, padx=(0, 15))
            self._locked_widgets.append(rb)
        # (each mode remembers its own path)
        self._sources = {"file": "", "folder": ""}
        self._current_mode = "file"
        self.lbl_source = label(src, "Source file:", 1)
        self.var_source = tk.StringVar()
        ttk.Entry(src, textvariable=self.var_source).grid(row=1, column=1, sticky=tk.EW, padx=(6, 6), pady=3)
        ttk.Button(src, text="Browse...", command=self._browse).grid(row=1, column=2, pady=3)
        self.lbl_mode_hint = hint(src, 2)
        st = self._row_settle = line(src, 3)
        self.lbl_settle = ttk.Label(st, text="Use new or changed files once their size is stable for")
        self.lbl_settle.pack(side=tk.LEFT)
        self.var_settle = tk.StringVar(value=str(FILE_SETTLE_S))
        lo, hi = FILE_SETTLE_RANGE
        self.spin_settle = ttk.Spinbox(st, from_=lo, to=hi, increment=1, width=6, textvariable=self.var_settle)
        self.spin_settle.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(st, text="seconds").pack(side=tk.LEFT)
        ts = self._row_test = line(src, 4)
        self.var_test_signal = tk.BooleanVar(value=False)
        self.chk_test_signal = ttk.Checkbutton(ts, text="When no file is playable, send a test signal",
                                               variable=self.var_test_signal, command=self._update_folder_controls)
        self.chk_test_signal.pack(side=tk.LEFT)
        self.var_test_kind = tk.StringVar(value=TEST_SIGNALS[0])
        self.cmb_test_kind = ttk.Combobox(ts, textvariable=self.var_test_kind, state="readonly", width=34,
                                          values=TEST_SIGNALS)
        self.cmb_test_kind.pack(side=tk.LEFT, padx=(6, 0))
        self.cmb_test_kind.bind("<<ComboboxSelected>>", lambda e: self._update_summaries())

        # ---------------- Output ----------------
        out = panel("output", "Output", True)
        label(out, "Protocol:", 0)
        proto = line(out, 0)
        self.var_protocol = tk.StringVar(value="SRT")
        for name, text in (("SRT", "SRT"), ("RTMP", "RTMP / RTMPS"), ("RIST", "RIST"), ("UDP", "UDP (multicast)")):
            rb = ttk.Radiobutton(proto, text=text, value=name, variable=self.var_protocol,
                                 command=self._on_protocol_change)
            rb.pack(side=tk.LEFT, padx=(0, 15))
            self._locked_widgets.append(rb)
        label(out, "Connection:", 1)
        cf = line(out, 1)
        self.var_connection = tk.StringVar(value="push")
        self.rb_connection = {}
        for value, text in CONNECTIONS.items():
            rb = ttk.Radiobutton(cf, text=text, value=value, variable=self.var_connection,
                                 command=self._on_protocol_change)
            rb.pack(side=tk.LEFT, padx=(0, 15))
            self._locked_widgets.append(rb)
            self.rb_connection[value] = rb
        # (each protocol + direction remembers its own address)
        self._endpoints = dict(DEFAULT_ENDPOINTS)
        self._current_protocol = "SRT"
        self._current_key = "SRT/push"
        self.lbl_endpoint = label(out, "SRT Endpoint:", 2)
        self.var_endpoint = tk.StringVar(value=self._endpoints["SRT/push"])
        ttk.Entry(out, textvariable=self.var_endpoint).grid(row=2, column=1, columnspan=2, sticky=tk.EW,
                                                             padx=(6, 0), pady=3)
        # protocol options: SRT encryption, RTMP stream key, RIST settings or UDP settings
        self.lbl_rist = label(out, "SRT security:", 3)
        po = line(out, 3)
        self._build_security(po, self._update_rist_controls)
        ro = self.frm_rist = ttk.Frame(po)
        ttk.Label(ro, text="Profile").pack(side=tk.LEFT)
        self.var_rist_profile = tk.StringVar(value="Main")
        self.cmb_rist_profile = ttk.Combobox(ro, textvariable=self.var_rist_profile, state="readonly", width=7,
                                             values=list(RIST_PROFILES))
        self.cmb_rist_profile.pack(side=tk.LEFT, padx=(4, 12))
        self.cmb_rist_profile.bind("<<ComboboxSelected>>", lambda e: self._update_rist_controls())
        ttk.Label(ro, text="Buffer").pack(side=tk.LEFT)
        self.var_rist_buffer = tk.StringVar(value=str(RIST_BUFFER_MS))
        lo, hi = RIST_BUFFER_RANGE
        self.spin_rist_buffer = ttk.Spinbox(ro, from_=lo, to=hi, increment=50, width=6,
                                            textvariable=self.var_rist_buffer)
        self.spin_rist_buffer.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(ro, text="ms").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(ro, text="Encryption").pack(side=tk.LEFT)
        self.var_rist_enc = tk.StringVar(value="Off")
        self.cmb_rist_enc = ttk.Combobox(ro, textvariable=self.var_rist_enc, state="readonly", width=8,
                                         values=list(RIST_ENCRYPTION))
        self.cmb_rist_enc.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_rist_enc.bind("<<ComboboxSelected>>", lambda e: self._update_rist_controls())
        ttk.Label(ro, text="Passphrase").pack(side=tk.LEFT)
        self.var_rist_secret = tk.StringVar(value="")
        self.ent_rist_secret = ttk.Entry(ro, textvariable=self.var_rist_secret, show="*", width=16)
        self.ent_rist_secret.pack(side=tk.LEFT, padx=(4, 0))
        uo = self.frm_udp = ttk.Frame(po)
        ttk.Label(uo, text="TTL").pack(side=tk.LEFT)
        self.var_udp_ttl = tk.StringVar(value=str(UDP_TTL))
        lo, hi = UDP_TTL_RANGE
        self.spin_udp_ttl = ttk.Spinbox(uo, from_=lo, to=hi, increment=1, width=4, textvariable=self.var_udp_ttl)
        self.spin_udp_ttl.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(uo, text="Local NIC IP (optional)").pack(side=tk.LEFT)
        self.var_udp_localaddr = tk.StringVar(value="")
        self.ent_udp_localaddr = ttk.Entry(uo, textvariable=self.var_udp_localaddr, width=16)
        self.ent_udp_localaddr.pack(side=tk.LEFT, padx=(4, 0))
        label(out, "Bandwidth:", 4)
        bw = line(out, 4)
        self.var_bw = tk.StringVar(value="0")
        ttk.Entry(bw, textvariable=self.var_bw, width=10).pack(side=tk.LEFT)
        ttk.Label(bw, text=" kbps  (0 = unlimited)").pack(side=tk.LEFT, padx=5)
        self.lbl_hint = hint(out, 5)
        rc = line(out, 6)
        self.var_reconnect = tk.BooleanVar(value=True)
        self.chk_reconnect = ttk.Checkbutton(rc, text="Auto-reconnect if the stream drops (recommended) - retry after",
                                             variable=self.var_reconnect, command=self._on_reconnect_toggle)
        self.chk_reconnect.pack(side=tk.LEFT)
        self.var_reconnect_delay = tk.StringVar(value=str(RECONNECT_DELAY_S))
        lo, hi = RECONNECT_DELAY_RANGE
        self.spin_reconnect = ttk.Spinbox(rc, from_=lo, to=hi, increment=1, width=6,
                                          textvariable=self.var_reconnect_delay)
        self.spin_reconnect.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(rc, text="seconds").pack(side=tk.LEFT)

        # ---------------- Encoding ----------------
        enc = panel("encoding", "Encoding", False)
        self.var_reencode = tk.BooleanVar(value=False)
        w = ttk.Checkbutton(enc, text="Re-encode to H.264/AAC (bandwidth = target bitrate for video + audio)",
                            variable=self.var_reencode, command=self._update_hints)
        w.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=3)
        self._locked_widgets.append(w)
        label(enc, "Output format:", 1)
        of = line(enc, 1)
        self.var_format = tk.StringVar(value=FORMAT_AUTO)
        self.cmb_format = ttk.Combobox(of, textvariable=self.var_format, state="readonly", width=24,
                                       values=[FORMAT_AUTO] + list(OUTPUT_FORMATS))
        self.cmb_format.pack(side=tk.LEFT)
        self.cmb_format.bind("<<ComboboxSelected>>", lambda e: self._update_summaries())
        self.lbl_format_hint = ttk.Label(of, foreground="gray", text="")
        self.lbl_format_hint.pack(side=tk.LEFT, padx=8)
        self._enc_widgets = []
        label(enc, "Quality:", 2)
        q = line(enc, 2)
        ttk.Label(q, text="Preset").pack(side=tk.LEFT)
        self.var_preset = tk.StringVar(value=DEFAULT_PRESET)
        w = ttk.Combobox(q, textvariable=self.var_preset, state="readonly", width=10, values=X264_PRESETS)
        w.pack(side=tk.LEFT, padx=(4, 14))
        w.bind("<<ComboboxSelected>>", lambda e: self._update_summaries())
        self._enc_widgets.append((w, "readonly"))
        ttk.Label(q, text="Quality (CRF)").pack(side=tk.LEFT)
        self.var_crf = tk.StringVar(value=str(DEFAULT_CRF))
        lo, hi = CRF_RANGE
        w = ttk.Spinbox(q, from_=lo, to=hi, increment=1, width=4, textvariable=self.var_crf)
        w.pack(side=tk.LEFT, padx=(4, 14))
        self._enc_widgets.append((w, tk.NORMAL))
        ttk.Label(q, text="Audio").pack(side=tk.LEFT)
        self.var_audio_k = tk.StringVar(value=f"{AUDIO_BITRATE_K} kbps")
        w = ttk.Combobox(q, textvariable=self.var_audio_k, state="readonly", width=9,
                         values=[f"{k} kbps" for k in AUDIO_BITRATES_K])
        w.pack(side=tk.LEFT, padx=(4, 0))
        w.bind("<<ComboboxSelected>>", lambda e: self._update_summaries())
        self._enc_widgets.append((w, "readonly"))
        hint(enc, 3).config(text="Faster presets use less CPU; slower ones look better at the same bitrate. CRF: "
                                 "lower = better picture and bigger stream (18 = near-lossless look, 23 = default, "
                                 "28 = small). CRF is used when Bandwidth is 0; with a bandwidth cap, the cap sets "
                                 "the bitrate.")

        # ---------------- Overlays ----------------
        ovp = panel("overlays", "Overlays (re-encode)", False)
        self.var_overlay = tk.BooleanVar(value=False)
        self.chk_overlay = ttk.Checkbutton(ovp, text="Date/time", variable=self.var_overlay, width=LABEL_WIDTH - 3,
                                           command=self._update_encode_controls)
        self.chk_overlay.grid(row=0, column=0, sticky=tk.W, pady=3)
        ov = line(ovp, 0)
        self.var_ov_format = tk.StringVar(value=list(OVERLAY_FORMATS)[0])
        self.cmb_ov_format = ttk.Combobox(ov, textvariable=self.var_ov_format, state="readonly", width=19,
                                          values=list(OVERLAY_FORMATS))
        self.cmb_ov_format.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(ov, text="Position").pack(side=tk.LEFT)
        self.var_ov_pos = tk.StringVar(value="Top-left")
        self.cmb_ov_pos = ttk.Combobox(ov, textvariable=self.var_ov_pos, state="readonly", width=13,
                                       values=OVERLAY_POSITIONS)
        self.cmb_ov_pos.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_ov_pos.bind("<<ComboboxSelected>>", lambda e: self._update_encode_controls())
        ov2 = line(ovp, 1)
        ttk.Label(ov2, text="Colour").pack(side=tk.LEFT)
        self.var_ov_colour = tk.StringVar(value="White")
        self._ov_colour_prev = "White"
        self._ov_custom_hex = None
        self.cmb_ov_colour = ttk.Combobox(ov2, textvariable=self.var_ov_colour, state="readonly", width=10,
                                          values=list(OVERLAY_COLOURS) + [CUSTOM_COLOUR])
        self.cmb_ov_colour.pack(side=tk.LEFT, padx=(4, 4))
        self.cmb_ov_colour.bind("<<ComboboxSelected>>", lambda e: self._on_colour_change())
        self.swatch = tk.Label(ov2, width=3, relief=tk.SUNKEN, bd=1, bg=OVERLAY_COLOURS["White"])
        self.swatch.pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(ov2, text="Size").pack(side=tk.LEFT)
        self.var_ov_size = tk.StringVar(value=str(DEFAULT_OVERLAY_SIZE))
        lo, hi = OVERLAY_SIZE_RANGE
        self.spin_ov_size = ttk.Spinbox(ov2, from_=lo, to=hi, increment=1, width=4, textvariable=self.var_ov_size)
        self.spin_ov_size.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(ov2, text="% of picture height").pack(side=tk.LEFT, padx=(0, 14))
        self.var_ov_box = tk.BooleanVar(value=True)
        self.chk_ov_box = ttk.Checkbutton(ov2, text="Background box", variable=self.var_ov_box)
        self.chk_ov_box.pack(side=tk.LEFT)
        ov3 = line(ovp, 2)
        ttk.Label(ov3, text="Custom position:  X").pack(side=tk.LEFT)
        self.var_ov_x = tk.StringVar(value="50")
        self.spin_ov_x = ttk.Spinbox(ov3, from_=0, to=100, increment=5, width=4, textvariable=self.var_ov_x)
        self.spin_ov_x.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(ov3, text="%   Y").pack(side=tk.LEFT)
        self.var_ov_y = tk.StringVar(value="90")
        self.spin_ov_y = ttk.Spinbox(ov3, from_=0, to=100, increment=5, width=4, textvariable=self.var_ov_y)
        self.spin_ov_y.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(ov3, text="%").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(ov3, foreground="gray", text="(0 % = left/top, 100 % = right/bottom)").pack(side=tk.LEFT)
        ttk.Separator(ovp).grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=4)
        self.var_logo = tk.BooleanVar(value=False)
        self.chk_logo = ttk.Checkbutton(ovp, text="Logo", variable=self.var_logo, width=LABEL_WIDTH - 3,
                                        command=self._update_encode_controls)
        self.chk_logo.grid(row=4, column=0, sticky=tk.W, pady=3)
        lg = ttk.Frame(ovp)
        lg.grid(row=4, column=1, columnspan=2, sticky=tk.EW, padx=(6, 0), pady=3)
        lg.columnconfigure(0, weight=1)
        self.var_logo_path = tk.StringVar(value="")
        self.ent_logo = ttk.Entry(lg, textvariable=self.var_logo_path)
        self.ent_logo.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
        self.btn_logo = ttk.Button(lg, text="Browse...", command=self._browse_logo)
        self.btn_logo.grid(row=0, column=1)
        lg2 = line(ovp, 5)
        ttk.Label(lg2, text="Position").pack(side=tk.LEFT)
        self.var_logo_pos = tk.StringVar(value="Top-right")
        self.cmb_logo_pos = ttk.Combobox(lg2, textvariable=self.var_logo_pos, state="readonly", width=13,
                                         values=OVERLAY_POSITIONS)
        self.cmb_logo_pos.pack(side=tk.LEFT, padx=(4, 12))
        self.cmb_logo_pos.bind("<<ComboboxSelected>>", lambda e: self._update_encode_controls())
        ttk.Label(lg2, text="Size").pack(side=tk.LEFT)
        self.var_logo_size = tk.StringVar(value=str(DEFAULT_LOGO_SIZE))
        lo, hi = LOGO_SIZE_RANGE
        self.spin_logo_size = ttk.Spinbox(lg2, from_=lo, to=hi, increment=1, width=4, textvariable=self.var_logo_size)
        self.spin_logo_size.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(lg2, text="% of picture width").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(lg2, text="Opacity").pack(side=tk.LEFT)
        self.var_logo_opacity = tk.StringVar(value="100")
        lo, hi = LOGO_OPACITY_RANGE
        self.spin_logo_opacity = ttk.Spinbox(lg2, from_=lo, to=hi, increment=5, width=4,
                                             textvariable=self.var_logo_opacity)
        self.spin_logo_opacity.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(lg2, text="%").pack(side=tk.LEFT)
        lg3 = line(ovp, 6)
        ttk.Label(lg3, text="Custom position:  X").pack(side=tk.LEFT)
        self.var_logo_x = tk.StringVar(value="95")
        self.spin_logo_x = ttk.Spinbox(lg3, from_=0, to=100, increment=5, width=4, textvariable=self.var_logo_x)
        self.spin_logo_x.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(lg3, text="%   Y").pack(side=tk.LEFT)
        self.var_logo_y = tk.StringVar(value="5")
        self.spin_logo_y = ttk.Spinbox(lg3, from_=0, to=100, increment=5, width=4, textvariable=self.var_logo_y)
        self.spin_logo_y.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(lg3, text="%").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(lg3, foreground="gray", text="(a PNG with transparency works best)").pack(side=tk.LEFT)
        ttk.Separator(ovp).grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=4)
        ap = ttk.Frame(ovp)
        ap.grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=(0, 3))
        self.btn_apply = ttk.Button(ap, text="Apply to live stream", command=self._apply_overlays,
                                    state=tk.DISABLED)
        self.btn_apply.pack(side=tk.LEFT)
        ttk.Label(ap, foreground="gray", wraplength=600,
                  text="While streaming: change the date/time or logo, then Apply. The connection stays up; "
                       "the log says whether it applied instantly or restarted the file where it was.").pack(side=tk.LEFT, padx=8)

        # Buttons
        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, pady=(6, 8))
        for i in range(3):
            btns.columnconfigure(i, weight=1)
        self.btn_start = ttk.Button(btns, text="▶ START", command=self._start_push)
        self.btn_start.grid(row=0, column=0, sticky=tk.EW, padx=5)
        self.btn_stop = ttk.Button(btns, text="■ STOP", command=self._stop_push, state=tk.DISABLED)
        self.btn_stop.grid(row=0, column=1, sticky=tk.EW, padx=5)
        ttk.Button(btns, text="✕ EXIT", command=self.on_closing).grid(row=0, column=2, sticky=tk.EW, padx=5)

        # Status, stats, log
        self.var_status = tk.StringVar(value="Idle")
        ttk.Entry(main, textvariable=self.var_status, state="readonly").pack(fill=tk.X, pady=(0, 2))
        self.var_stats = tk.StringVar(value="")
        ttk.Entry(main, textvariable=self.var_stats, state="readonly").pack(fill=tk.X, pady=(0, 5))
        lh = ttk.Frame(main)
        lh.pack(fill=tk.X)
        ttk.Label(lh, text="FFmpeg log:").pack(side=tk.LEFT)
        ttk.Button(lh, text="Clear Log", command=self._clear_log).pack(side=tk.RIGHT)
        self.txt_log = scrolledtext.ScrolledText(main, wrap=tk.WORD, height=8, state=tk.DISABLED)
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # keep the collapsed panels' one-line summaries up to date
        for var in (self.var_source, self.var_endpoint, self.var_bw, self.var_crf, self.var_srt_enc,
                    self.var_rist_enc, self.var_overlay, self.var_logo, self.var_reencode, self.var_mode,
                    self.var_protocol, self.var_connection, self.var_test_signal):
            var.trace_add("write", lambda *a: self._update_summaries())
        self._update_hints()

    # ---------------- collapsible panels ----------------
    def _on_panel_toggle(self):
        """Grow the window if an expanded panel no longer fits (the log shrinks first)."""
        self.update_idletasks()
        need = self.winfo_reqheight()
        if self.winfo_height() < need:
            room = self.winfo_screenheight() - 80
            self.geometry(f"{self.winfo_width()}x{min(need, room)}")

    def _update_summaries(self):
        """One-line summaries shown on collapsed panels."""
        if not getattr(self, "_panels", None):
            return
        mode = self.var_mode.get()
        src = os.path.basename(self.var_source.get().rstrip("/\\")) or "(none chosen)"
        if mode == "folder":
            text = f"Folder playlist · {src}"
            if self.var_test_signal.get():
                text += " · test signal on"
        else:
            text = f"Single file · {src}"
        self._panels["source"].set_summary(text)

        protocol = self.var_protocol.get()
        parts = [protocol]
        if protocol != "UDP":
            parts.append(self.var_connection.get())
        parts.append(mask_secrets(self.var_endpoint.get().strip()))
        if protocol == "SRT" and self.var_srt_enc.get() != "Off":
            parts.append(self.var_srt_enc.get())
        if protocol == "RIST" and self.var_rist_enc.get() != "Off":
            parts.append(self.var_rist_enc.get())
        bw = self.var_bw.get().strip()
        if bw not in ("", "0"):
            parts.append(f"{bw} kbps")
        self._panels["output"].set_summary(" · ".join(parts))

        if self.var_reencode.get():
            fmt = self.var_format.get()
            text = (f"Re-encode H.264/AAC · {fmt.split(' (')[0]} · {self.var_preset.get()} · "
                    f"CRF {self.var_crf.get()} · audio {self.var_audio_k.get()}")
        else:
            text = "Copy (no re-encode)"
        self._panels["encoding"].set_summary(text)

        if not self.var_reencode.get():
            text = "not used in copy mode"
        else:
            on = [n for n, v in (("date/time", self.var_overlay), ("logo", self.var_logo)) if v.get()]
            text = " + ".join(on) if on else "none"
        self._panels["overlays"].set_summary(text)

    # ---------------- saved settings ----------------
    def _persist_vars(self):
        return {
            "mode": (self.var_mode, ["file", "folder"]),
            "protocol": (self.var_protocol, list(PROTOCOLS)),
            "connection": (self.var_connection, list(CONNECTIONS)),
            "bandwidth_k": (self.var_bw, None),
            "output_format": (self.var_format, [FORMAT_AUTO] + list(OUTPUT_FORMATS)),
            "reencode": (self.var_reencode, None),
            "preset": (self.var_preset, X264_PRESETS),
            "crf": (self.var_crf, None),
            "audio": (self.var_audio_k, [f"{k} kbps" for k in AUDIO_BITRATES_K]),
            "overlay": (self.var_overlay, None),
            "overlay_format": (self.var_ov_format, list(OVERLAY_FORMATS)),
            "overlay_position": (self.var_ov_pos, OVERLAY_POSITIONS),
            "overlay_x": (self.var_ov_x, None),
            "overlay_y": (self.var_ov_y, None),
            "overlay_size": (self.var_ov_size, None),
            "overlay_box": (self.var_ov_box, None),
            "overlay_colour": (self.var_ov_colour, list(OVERLAY_COLOURS) + [CUSTOM_COLOUR]),
            "logo": (self.var_logo, None),
            "logo_path": (self.var_logo_path, None),
            "logo_position": (self.var_logo_pos, OVERLAY_POSITIONS),
            "logo_size": (self.var_logo_size, None),
            "logo_opacity": (self.var_logo_opacity, None),
            "logo_x": (self.var_logo_x, None),
            "logo_y": (self.var_logo_y, None),
            "settle_s": (self.var_settle, None),
            "test_signal": (self.var_test_signal, None),
            "test_signal_kind": (self.var_test_kind, TEST_SIGNALS),
            "reconnect": (self.var_reconnect, None),
            "reconnect_delay": (self.var_reconnect_delay, None),
            "rist_profile": (self.var_rist_profile, list(RIST_PROFILES)),
            "rist_buffer_ms": (self.var_rist_buffer, None),
            "rist_encryption": (self.var_rist_enc, list(RIST_ENCRYPTION)),
            "srt_encryption": (self.var_srt_enc, list(SRT_ENCRYPTION)),
            "srt_streamid": (self.var_srt_streamid, None),
            "udp_ttl": (self.var_udp_ttl, None),
            "udp_localaddr": (self.var_udp_localaddr, None),
            # passphrases and the RTMP stream key are deliberately not saved
        }

    def _persist_extras(self):
        sources = dict(self._sources)
        sources[self._current_mode] = self.var_source.get()
        endpoints = dict(self._endpoints)
        endpoints[self._current_key] = self.var_endpoint.get()
        endpoints = {k: strip_saved_secrets(v, rtmp_key=(k == "RTMP/push")) for k, v in endpoints.items()}
        return {"sources": sources, "endpoints_by_direction": endpoints,
                "overlay_custom_colour": self._ov_custom_hex,
                "panels": {name: p.expanded for name, p in self._panels.items()}}

    def _restore_extras(self, data):
        self._sources.update(str_dict(data.get("sources"), self._sources))
        # settings saved before 2.6.0 have one address per protocol: those were push addresses
        old = str_dict(data.get("endpoints"), PROTOCOLS)
        self._endpoints.update({f"{p}/push": url for p, url in old.items()})
        self._endpoints.update(str_dict(data.get("endpoints_by_direction"), DEFAULT_ENDPOINTS))
        self._current_mode = self.var_mode.get()
        self.var_source.set(self._sources.get(self._current_mode, ""))
        self.lbl_source.config(text="Source folder:" if self._current_mode == "folder" else "Source file:")
        self._current_protocol = self.var_protocol.get()
        self._current_key = self._endpoint_key()
        self.var_endpoint.set(self._endpoints[self._current_key])
        self.lbl_endpoint.config(text=self._endpoint_label())
        custom = data.get("overlay_custom_colour")
        if isinstance(custom, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", custom):
            self._ov_custom_hex = custom.upper()
        if self.var_ov_colour.get() == CUSTOM_COLOUR and not self._ov_custom_hex:
            self.var_ov_colour.set("White")
        self._ov_colour_prev = self.var_ov_colour.get()
        self.swatch.config(bg=self._ov_colour_hex())
        panels = data.get("panels")
        if isinstance(panels, dict):
            for name, p in self._panels.items():
                if isinstance(panels.get(name), bool):
                    p.set_expanded(panels[name])
        self._update_hints()
        self._on_reconnect_toggle()

    def _on_mode_change(self):
        new = self.var_mode.get()
        if new == self._current_mode:
            return
        self._sources[self._current_mode] = self.var_source.get()
        self.var_source.set(self._sources[new])
        self._current_mode = new
        self.lbl_source.config(text="Source folder:" if new == "folder" else "Source file:")
        self._update_hints()

    def _endpoint_key(self):
        return f"{self.var_protocol.get()}/{self.var_connection.get()}"

    def _endpoint_label(self):
        p = self.var_protocol.get()
        if p == "UDP":
            return "UDP destination:"
        return f"{p} Endpoint:" if self.var_connection.get() == "push" else f"{p} Listen on:"

    def _on_protocol_change(self):
        """Protocol or connection direction changed: show that combination's remembered address."""
        if self.var_protocol.get() == "UDP":
            self.var_connection.set("push")       # UDP has no connection to wait for
        key = self._endpoint_key()
        if key != self._current_key:
            self._endpoints[self._current_key] = self.var_endpoint.get()
            self.var_endpoint.set(self._endpoints[key])
            self._current_key = key
        self._current_protocol = self.var_protocol.get()
        self.lbl_endpoint.config(text=self._endpoint_label())
        self._update_hints()

    def _update_hints(self):
        protocol = self.var_protocol.get()
        listen = self.var_connection.get() == "listen"
        if protocol == "SRT":
            hint = ("Encryption: same passphrase (10-79 characters) on both sides. Without re-encode, bandwidth "
                    "caps the SRT link (maxbw) - set it above the file's bitrate.")
            if listen:
                hint = ("SRT listen: the receiver calls this PC (srt://this-pc:9000?mode=caller). " + hint)
        elif protocol == "RIST":
            hint = ("RIST listen (Main profile): the receiver connects to rist://this-pc:5000. "
                    if listen else "RIST: the receiver listens (e.g. rist://@0.0.0.0:5000). ")
            hint += "Same profile and passphrase on both sides; use an even port. Without re-encode, bandwidth is ignored."
        elif protocol == "UDP":
            hint = ("UDP: MPEG-TS to a multicast group (239.x.x.x - for receivers and IRDs on the LAN) or to one "
                    "receiver's IP. No encryption or retransmission: for a well-behaved local network. TTL = "
                    "routers it may cross. Bandwidth = a constant mux rate with smooth pacing (0 = variable "
                    "rate); in copy mode set it above the files' bitrate.")
        else:
            hint = ("RTMP listen: this PC acts as the RTMP server - the receiver plays rtmp://this-pc:1935/live. FFmpeg's "
                    "RTMP server can't check a player's stream key: use SRT or RIST encryption to restrict access. "
                    if listen else "RTMP: address = server/app (e.g. rtmp://a.rtmp.youtube.com/live2), plus the "
                    "stream key. ")
            hint += "Without re-encode the source must be H.264/AAC, and bandwidth is ignored."
        self.lbl_hint.config(text=hint)
        self._update_rist_controls()
        if self.var_mode.get() == "folder":
            mode_hint = ("Folder: plays all videos in name order and loops; bad files are skipped. "
                         "Without re-encode, all files need the same format.")
        else:
            mode_hint = ""
        self.lbl_mode_hint.config(text=mode_hint)
        # folder-only settings are hidden in single-file mode
        for w in (self.lbl_mode_hint, self._row_settle, self._row_test):
            if self.var_mode.get() == "folder":
                w.grid()
            else:
                w.grid_remove()
        if self.var_reencode.get():
            fmt_hint = "applies to the whole stream (Auto = first file's format)"
        elif self.var_mode.get() == "folder":
            fmt_hint = "copy mode: only for a test signal before the first file"
        else:
            fmt_hint = "only used when re-encoding"
        self.lbl_format_hint.config(text=fmt_hint)
        self._update_folder_controls()
        self._update_encode_controls()
        self._update_summaries()

    def _update_rist_controls(self, locked=None):
        """Protocol options row: SRT security, RTMP stream key or RIST settings."""
        if locked is None:
            locked = self.worker is not None and self.worker.is_alive()
        protocol = self.var_protocol.get()
        push = self.var_connection.get() == "push"
        self.lbl_rist.config(text={"SRT": "SRT security:", "RTMP": "RTMP:", "RIST": "RIST options:",
                                   "UDP": "UDP options:"}[protocol])
        udp = protocol == "UDP"
        self.rb_connection["listen"].config(state=tk.DISABLED if udp or locked else tk.NORMAL)
        self.spin_udp_ttl.config(state=tk.NORMAL if udp and not locked else tk.DISABLED)
        self.ent_udp_localaddr.config(state=tk.NORMAL if udp and not locked else tk.DISABLED)
        for name, frame in (("SRT", self.frm_srt), ("RTMP", self.frm_rtmp), ("RIST", self.frm_rist),
                            ("UDP", self.frm_udp)):
            if name == protocol:
                frame.pack(side=tk.LEFT)
            else:
                frame.pack_forget()
        self._update_security_controls(
            locked, streamid_on=push, key_on=push,
            key_note="" if push else "(not used when listening)")
        on = protocol == "RIST" and not locked
        main_profile = self.var_rist_profile.get() == "Main"
        if not main_profile:
            self.var_rist_enc.set("Off")          # encryption needs the Main profile
        self.cmb_rist_profile.config(state="readonly" if on else tk.DISABLED)
        self.spin_rist_buffer.config(state=tk.NORMAL if on else tk.DISABLED)
        self.cmb_rist_enc.config(state="readonly" if on and main_profile else tk.DISABLED)
        enc_on = on and main_profile and self.var_rist_enc.get() != "Off"
        self.ent_rist_secret.config(state=tk.NORMAL if enc_on else tk.DISABLED)

    def _update_folder_controls(self, locked=None):
        if locked is None:
            locked = self.worker is not None and self.worker.is_alive()
        folder = self.var_mode.get() == "folder"
        enabled = folder and not locked
        self.spin_settle.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self.lbl_settle.config(foreground="" if enabled else "gray")
        self.chk_test_signal.config(state=tk.NORMAL if enabled else tk.DISABLED)
        kind_on = enabled and self.var_test_signal.get()
        self.cmb_test_kind.config(state="readonly" if kind_on else tk.DISABLED)
        fmt_on = not locked and (self.var_reencode.get() or folder)
        self.cmb_format.config(state="readonly" if fmt_on else tk.DISABLED)

    def _on_reconnect_toggle(self):
        self.spin_reconnect.config(state=tk.NORMAL if self.var_reconnect.get() else tk.DISABLED)

    def _set_inputs_locked(self, locked):
        for w in self._locked_widgets + [self.chk_reconnect]:
            w.config(state=tk.DISABLED if locked else tk.NORMAL)
        if locked:
            self.spin_reconnect.config(state=tk.DISABLED)
        else:
            self._on_reconnect_toggle()
        self._update_folder_controls(locked)
        self._update_rist_controls(locked)
        self._update_encode_controls(locked)

    def _update_encode_controls(self, locked=None):
        if locked is None:
            locked = self.worker is not None and self.worker.is_alive()
        on = self.var_reencode.get() and not locked
        for w, active in self._enc_widgets:
            w.config(state=active if on else tk.DISABLED)
        # date/time and logo: also editable while streaming, when the stream can take changes
        live = locked and self._live_overlays()
        self.btn_apply.config(state=tk.NORMAL if live else tk.DISABLED)
        on = on or live
        for w in (self.chk_overlay, self.chk_logo):
            w.config(state=tk.NORMAL if on else tk.DISABLED)
        ov = on and self.var_overlay.get()
        for w in (self.cmb_ov_format, self.cmb_ov_pos, self.cmb_ov_colour):
            w.config(state="readonly" if ov else tk.DISABLED)
        for w in (self.spin_ov_size, self.chk_ov_box):
            w.config(state=tk.NORMAL if ov else tk.DISABLED)
        custom = ov and self.var_ov_pos.get() == "Custom"
        for w in (self.spin_ov_x, self.spin_ov_y):
            w.config(state=tk.NORMAL if custom else tk.DISABLED)
        lg = on and self.var_logo.get()
        for w in (self.ent_logo, self.btn_logo, self.spin_logo_size, self.spin_logo_opacity):
            w.config(state=tk.NORMAL if lg else tk.DISABLED)
        self.cmb_logo_pos.config(state="readonly" if lg else tk.DISABLED)
        custom = lg and self.var_logo_pos.get() == "Custom"
        for w in (self.spin_logo_x, self.spin_logo_y):
            w.config(state=tk.NORMAL if custom else tk.DISABLED)

    def _live_overlays(self):
        """True while a stream that accepts overlay changes is running."""
        return self.worker is not None and getattr(self.worker, "live_overlays", False)

    def _apply_overlays(self):
        """Send the current date/time and logo settings to the running stream."""
        if not (self.worker and self.worker.is_alive() and self._live_overlays()):
            return
        overlay = logo = None
        if self.var_overlay.get():
            overlay = self._read_overlay()
            if overlay is None:
                return
        if self.var_logo.get():
            logo = self._read_logo()
            if logo is None:
                return
        if self.worker.update_overlays(overlay, logo):
            parts = [f"date/time {'on' if overlay else 'off'}", f"logo {'on' if logo else 'off'}"]
            self._log("Applying new overlay settings to the live stream (" + ", ".join(parts) + ").")
            self._save_settings()

    def _browse_logo(self):
        patterns = " ".join("*" + e for e in LOGO_EXTS)
        f = filedialog.askopenfilename(title="Choose logo image",
                                       filetypes=[("Images", patterns), ("All Files", "*.*")])
        if f:
            self.var_logo_path.set(f)

    def _ov_colour_hex(self):
        name = self.var_ov_colour.get()
        return self._ov_custom_hex if name == CUSTOM_COLOUR else OVERLAY_COLOURS.get(name, "#FFFFFF")

    def _on_colour_change(self):
        if self.var_ov_colour.get() == CUSTOM_COLOUR:
            start = self._ov_custom_hex or OVERLAY_COLOURS.get(self._ov_colour_prev, "#FFFFFF")
            picked = colorchooser.askcolor(color=start, title="Date/time colour", parent=self)[1]
            if picked:
                self._ov_custom_hex = picked.upper()
            elif not self._ov_custom_hex:          # cancelled with no custom colour yet
                self.var_ov_colour.set(self._ov_colour_prev)
        self._ov_colour_prev = self.var_ov_colour.get()
        self.swatch.config(bg=self._ov_colour_hex())

    def _clear_log(self):
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def _browse(self):
        if self.var_mode.get() == "folder":
            d = filedialog.askdirectory(mustexist=True)
            if d:
                self.var_source.set(d)
        else:
            patterns = " ".join("*" + e for e in VIDEO_EXTS)
            f = filedialog.askopenfilename(filetypes=[("Video files", patterns), ("All Files", "*.*")])
            if f:
                self.var_source.set(f)

    # ---------------- logging / events (GUI thread only) ----------------
    def _log(self, msg):
        self.events.put(("log", f"[{time.strftime('%H:%M:%S')}] {mask_secrets(msg)}"))

    def _append_log(self, text):
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.insert(tk.END, text + "\n")
        overflow = int(self.txt_log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if overflow > 0:
            self.txt_log.delete("1.0", f"{overflow + 1}.0")
        self.txt_log.see(tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def destroy(self):
        """Cancel the log poller first, so closing never leaves a stray Tk callback behind."""
        job = getattr(self, "_poll_job", None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        super().destroy()

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "stats":
                    self.var_stats.set(payload)
                elif kind == "done":
                    self._on_worker_done()
        except queue.Empty:
            pass
        if not self._closing:
            self._poll_job = self.after(100, self._poll_events)

    # ---------------- start / stop ----------------
    def _read_settings(self):
        """Read and validate all inputs on the GUI thread; returns dict or None."""
        mode = self.var_mode.get()
        protocol = self.var_protocol.get()
        source = self.var_source.get().strip()
        endpoint = self.var_endpoint.get().strip()
        what = "folder" if mode == "folder" else "file"
        if not source or not endpoint:
            self._log(f"Error: Source {what} and {protocol} Endpoint must be provided.")
            return None
        if mode == "folder":
            if not os.path.isdir(source):
                self._log(f"Error: folder not found: {source}")
                return None
            try:
                count = len(list_videos(source))
            except OSError as e:
                self._log(f"Error: cannot read folder: {e.strerror or e}")
                return None
            if count == 0 and not self.var_test_signal.get():
                self._log("Error: no video files in the folder (" + ", ".join(VIDEO_EXTS) + "). "
                          "Tick 'send test signal' to start with an empty folder.")
                return None
            if not os.path.isfile(self.ffprobe_path):
                self._log(f"Error: ffprobe (needed to check files) not found at {self.ffprobe_path}")
                return None
            self._log(f"Folder has {count} video file(s)." if count else
                      "Folder is empty - the test signal will be sent until files arrive.")
        elif not os.path.isfile(source):
            self._log(f"Error: source file not found: {source}")
            return None
        schemes = PROTOCOLS[protocol]["schemes"]
        if not endpoint.lower().startswith(schemes):
            self._log(f"Error: {protocol} endpoint must start with " + " or ".join(schemes))
            return None
        if not os.path.isfile(self.ffmpeg_path):
            self._log(f"Error: FFmpeg not found at {self.ffmpeg_path}")
            return None
        # does this FFmpeg build support the protocol? (skipped if the list can't be read)
        scheme = url_scheme(endpoint)
        supported = ffmpeg_output_protocols(self.ffmpeg_path)
        if supported and scheme not in supported:
            need = {"srt": "libsrt", "rist": "librist", "rtmps": "TLS (OpenSSL/GnuTLS)"}.get(scheme, scheme)
            self._log(f"Error: this FFmpeg build can't send {scheme}:// (it lacks {need}). "
                      "Use a 'full' FFmpeg build - check with: ffmpeg -protocols")
            return None
        connection = self.var_connection.get()
        endpoint, notes, err = normalize_connection(protocol, connection == "listen", endpoint)
        if err:
            self._log("Error: " + err)
            return None
        for note in notes:
            self._log(note if note.startswith("Warning") else "Note: " + note)
        push = connection == "push"
        checked = self._read_security(protocol, endpoint, streamid_on=push, key_on=push)
        if checked is None:
            return None
        security, endpoint = checked         # security["endpoint"] adds the stream ID / stream key
        self.var_endpoint.set(endpoint)
        rist = {}
        if protocol == "UDP":
            lo, hi = UDP_TTL_RANGE
            try:
                ttl = int(self.var_udp_ttl.get().strip())
                if not lo <= ttl <= hi:
                    raise ValueError
            except ValueError:
                self._log(f"Error: UDP TTL must be a whole number from {lo} to {hi}.")
                return None
            localaddr = self.var_udp_localaddr.get().strip()
            if localaddr and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", localaddr):
                self._log("Error: Local NIC IP must be an IPv4 address of this PC, e.g. 192.168.1.10 (or empty).")
                return None
            rist = {"udp_ttl": ttl, "udp_localaddr": localaddr}
        if protocol == "RIST":
            if connection == "listen" and self.var_rist_profile.get() != "Main":
                self._log("Error: RIST listen mode needs the Main profile (Simple can only connect out).")
                return None
            lo, hi = RIST_BUFFER_RANGE
            try:
                buffer_ms = int(self.var_rist_buffer.get().strip())
                if not lo <= buffer_ms <= hi:
                    raise ValueError
            except ValueError:
                self._log(f"Error: RIST buffer must be a whole number of milliseconds from {lo} to {hi}.")
                return None
            encryption = RIST_ENCRYPTION[self.var_rist_enc.get()]
            secret = self.var_rist_secret.get()
            if encryption and not secret:
                self._log("Error: enter a RIST passphrase, or set Encryption to Off.")
                return None
            rist = {"rist_profile": RIST_PROFILES[self.var_rist_profile.get()], "rist_buffer_ms": buffer_ms,
                    "rist_encryption": encryption, "rist_secret": secret if encryption else ""}
        try:
            bw = int(self.var_bw.get().strip() or 0)
            if bw < 0:
                raise ValueError
        except ValueError:
            self._log("Error: bandwidth must be a whole number of kbps (0 = unlimited).")
            return None
        lo, hi = RECONNECT_DELAY_RANGE
        try:
            reconnect_delay = int(self.var_reconnect_delay.get().strip())
            if not lo <= reconnect_delay <= hi:
                raise ValueError
        except ValueError:
            self._log(f"Error: reconnect delay must be a whole number of seconds from {lo} to {hi}.")
            return None
        settle_s = FILE_SETTLE_S
        if mode == "folder":
            lo, hi = FILE_SETTLE_RANGE
            try:
                settle_s = int(self.var_settle.get().strip())
                if not lo <= settle_s <= hi:
                    raise ValueError
            except ValueError:
                self._log(f"Error: file stable time must be a whole number of seconds from {lo} to {hi}.")
                return None
        enc = {}
        if self.var_reencode.get():
            lo, hi = CRF_RANGE
            try:
                crf = int(self.var_crf.get().strip())
                if not lo <= crf <= hi:
                    raise ValueError
            except ValueError:
                self._log(f"Error: quality (CRF) must be a whole number from {lo} to {hi}.")
                return None
            enc = {"preset": self.var_preset.get(), "crf": crf,
                   "audio_k": int(self.var_audio_k.get().split()[0]), "overlay": None}
            if self.var_overlay.get():
                overlay = self._read_overlay()
                if overlay is None:
                    return None
                enc["overlay"] = overlay
            enc["logo"] = None
            if self.var_logo.get():
                logo = self._read_logo()
                if logo is None:
                    return None
                enc["logo"] = logo
                if mode == "file" and self.var_format.get() not in OUTPUT_FORMATS:
                    # "Auto" keeps the source size: the logo size is relative to the source width
                    width = image_width(self.ffprobe_path, source)
                    if not width:
                        self._log("Error: couldn't read the source video's size (needed to size the logo). "
                                  "Pick a fixed Output format, or check that ffprobe is in bin/.")
                        return None
                    enc["src_width"] = width
        if protocol == "UDP" and bw > 0 and not self.var_reencode.get():
            self._log(f"Note: UDP copy mode - sending at a constant {bw} kbps (padded); it must be above "
                      "the files' bitrate.")
        if protocol in ("RTMP", "RIST") and bw > 0 and not self.var_reencode.get():
            self._log(f"Note: bandwidth cap is ignored for {protocol} without re-encode.")
        return {
            "mode": mode,
            "protocol": protocol,
            "source": os.path.abspath(source),
            "endpoint": endpoint,
            "connection": connection,
            "bandwidth_k": bw,
            "reencode": self.var_reencode.get(),
            "reconnect": self.var_reconnect.get(),
            "reconnect_delay": reconnect_delay,
            "settle_s": settle_s,
            "output_format": self.var_format.get(),
            "test_signal": (TEST_SIGNAL_KEYS[self.var_test_kind.get()]
                            if mode == "folder" and self.var_test_signal.get() else None),
            **security,
            **rist,
            **enc,
        }

    def _read_logo(self):
        """Validate the logo settings; None (after logging why) if they can't be used."""
        path = self.var_logo_path.get().strip()
        if not path or not os.path.isfile(path):
            self._log("Error: choose a logo image file (PNG with transparency recommended).")
            return None
        try:
            size = int(self.var_logo_size.get().strip())
            opacity = int(self.var_logo_opacity.get().strip())
            x = int(self.var_logo_x.get().strip())
            y = int(self.var_logo_y.get().strip())
            lo, hi = LOGO_SIZE_RANGE
            olo, ohi = LOGO_OPACITY_RANGE
            if not (lo <= size <= hi and olo <= opacity <= ohi and 0 <= x <= 100 and 0 <= y <= 100):
                raise ValueError
        except ValueError:
            self._log(f"Error: logo size must be {LOGO_SIZE_RANGE[0]}-{LOGO_SIZE_RANGE[1]} %, opacity "
                      f"{LOGO_OPACITY_RANGE[0]}-100 % and X/Y 0-100 %.")
            return None
        if not image_width(self.ffprobe_path, path):
            self._log(f"Error: the logo file can't be read as an image: {path}")
            return None
        return {"path": os.path.abspath(path), "position": self.var_logo_pos.get(), "x": x, "y": y,
                "size": size, "opacity": opacity}

    def _read_overlay(self):
        """Validate the date/time overlay settings; None (after logging why) if they can't be used."""
        lo, hi = OVERLAY_SIZE_RANGE
        try:
            size = int(self.var_ov_size.get().strip())
            x = int(self.var_ov_x.get().strip())
            y = int(self.var_ov_y.get().strip())
            if not lo <= size <= hi or not (0 <= x <= 100 and 0 <= y <= 100):
                raise ValueError
        except ValueError:
            self._log(f"Error: date/time size must be {lo}-{hi} % and X/Y 0-100 %.")
            return None
        if not os.path.isfile(os.path.join(ASSETS_DIR, SLATE_FONT)):
            self._log(f"Error: the date/time font is missing ({os.path.join(ASSETS_DIR, SLATE_FONT)}).")
            return None
        if "drawtext" not in ffmpeg_caps(self.ffmpeg_path)["filters"]:
            self._log("Error: this FFmpeg build can't draw text (no freetype), so the date/time overlay "
                      "can't be shown. Use a 'full' FFmpeg build, or untick 'Show date/time'.")
            return None
        return {"format": self.var_ov_format.get(), "position": self.var_ov_pos.get(), "x": x, "y": y,
                "size": size, "box": self.var_ov_box.get(), "colour": self._ov_colour_hex()}

    def _start_push(self):
        if self.worker and self.worker.is_alive():
            return  # previous run still shutting down
        settings = self._read_settings()
        if not settings:
            return
        self._save_settings()
        if settings["mode"] == "folder" or os.path.isfile(self.ffprobe_path):
            # single files run through the same pipeline as a playlist of one, so the connection
            # survives each loop and the overlays can be changed while streaming
            self.worker = PlaylistWorker(settings, self.ffmpeg_path, self.ffprobe_path, self.events)
        else:
            self._log(f"Note: ffprobe not found at {self.ffprobe_path} - using the simple single-file "
                      "player (overlays can't be changed while streaming).")
            cmd = build_single_file_cmd(self.ffmpeg_path, settings)
            self.worker = SingleFileWorker(cmd, settings["reconnect"], settings["reconnect_delay"],
                                           self.events, cwd=ASSETS_DIR if settings.get("overlay") else None,
                                           listen=settings["protocol"] if settings["connection"] == "listen" else None,
                                           secrets=secret_values(settings))
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self._set_inputs_locked(True)
        self.var_status.set(f"Starting ({settings['protocol']})...")
        self.var_stats.set("")
        self.worker.start()

    def _stop_push(self):
        if self.worker and self.worker.is_alive():
            self.btn_stop.config(state=tk.DISABLED)
            self.var_status.set("Stopping...")
            self.worker.stop()
        # START is re-enabled only when the worker reports "done"

    def _on_worker_done(self):
        self.worker = None
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self._set_inputs_locked(False)
        self.var_status.set("Idle")
        if self._closing:
            self.destroy()

    def on_closing(self):
        if self._closing:
            return
        self._save_settings()
        if self.worker and self.worker.is_alive():
            self._closing = True
            self.var_status.set("Stopping before exit...")
            self.worker.stop()
            self._wait_then_close()
        else:
            self._closing = True
            self.destroy()

    def _wait_then_close(self, waited=0.0):
        # keep draining events until the worker finishes (or give up after the grace period)
        if self.worker is None:
            return  # _on_worker_done already destroyed the window
        if not self.worker.is_alive() or waited > STOP_GRACE_S + 2:
            self.destroy()
            return
        try:
            while True:
                kind, _ = self.events.get_nowait()
                if kind == "done":
                    self.destroy()
                    return
        except queue.Empty:
            pass
        self.after(100, self._wait_then_close, waited + 0.1)


if __name__ == "__main__":
    VideoPusherApp().mainloop()
