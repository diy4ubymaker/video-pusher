# Video Receiver (part of Video Pusher)
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

"""Video Receiver - the companion to Video Pusher.

Listens for an incoming SRT, RTMP or RIST stream and plays it (ffplay), saves it to a file,
and/or re-sends it as UDP multicast, via FFmpeg. When the sender disconnects, it goes back to
listening automatically.
"""

import os
import re
import time
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

import main as vp   # shared helpers: tool paths, RIST options, protocol checks, log redaction

APP_VERSION = "2.7.0"

# What the receiver listens on, per protocol
RX_PROTOCOLS = {
    "SRT": {"default": "srt://0.0.0.0:9000?mode=listener", "schemes": ("srt://",)},
    "RTMP": {"default": "rtmp://0.0.0.0:1935/live", "schemes": ("rtmp://", "rtmps://")},
    "RIST": {"default": "rist://@0.0.0.0:5000", "schemes": ("rist://",)},
    "UDP": {"default": "udp://239.1.1.1:5000", "schemes": ("udp://",)},
}
ACTIONS = {
    "play_only": "Live Playback Only",
    "play_save": "Live Play + Save to File",
    "play_multicast": "Live Play + Multicast",
    "save_only": "Save to File Only",
    "multicast_only": "Multicast Only",
}
LOCAL_BRIDGE = "udp://127.0.0.1:29999"   # FFmpeg -> ffplay when playing and saving/multicasting
RELISTEN_DELAY_S = 1                     # wait before listening again after a stream ends
PULL_RETRY_S = 3                         # pull mode: wait before connecting to the sender again
# Connection direction: wait for the sender to connect ("listen"), or connect to a sender that is
# listening ("pull"). Default addresses for each protocol and direction.
RX_CONNECTIONS = {"listen": "Listen (sender connects here)", "pull": "Pull (connect to the sender)"}
RX_DEFAULT_ENDPOINTS = {
    "SRT/listen": "srt://0.0.0.0:9000?mode=listener",
    "SRT/pull": "srt://127.0.0.1:9000?mode=caller",
    "RTMP/listen": "rtmp://0.0.0.0:1935/live",         # + the Stream key field
    "RTMP/pull": "rtmp://127.0.0.1:1935/live",
    "RIST/listen": "rist://@0.0.0.0:5000",
    "RIST/pull": "rist://127.0.0.1:5000",
    "UDP/listen": "udp://239.1.1.1:5000",               # multicast group, or udp://0.0.0.0:port for unicast
}
# UDP input: a large receive buffer (in 188-byte packets, ~9 MB) and carry on if it overflows,
# so a CPU spike doesn't stop the stream; reuse lets other programs on this PC watch the same group
UDP_FIFO_PACKETS = 50000
# A sender that stops sending without closing the connection (network drop, crash, frozen PC)
# would otherwise hold the connection open: SRT notices after ~5 s, RTMP never. Until then the
# receiver can't accept the sender's new connection. After this many seconds with no data the
# connection is dropped and the receiver listens (or pulls) again. With RIST, a sender that
# comes back within this time carries on in the same session (and recording).
# FFmpeg's own timeouts do this for SRT and RTMP; a watchdog in the receiver (no data, by the
# stats) is the backstop for every protocol, including RIST and builds that behave differently.
SENDER_TIMEOUT_S = 5
SENDER_TIMEOUT_RANGE = (2, 120)
STOP_GRACE_S = 2                         # FFmpeg gets this long to finish a file after 'q'
# FFmpeg's RTMP server logs this when a sender publishes under another stream key
RTMP_WRONG_KEY = "Unexpected stream "
FFMPEG_TIME = re.compile(r"time=\s*(-?)(\d+):(\d+):(\d+(?:\.\d+)?)")
FFPLAY_STATS = re.compile(r"^\s*(nan|-?\d+\.\d+)\s+(A-V|M-V|M-A|V-A|:)")


# ======================================================================
# Command building (module level, so it can be tested)
# ======================================================================
def normalize_endpoint(protocol, endpoint, pull=False):
    """(endpoint, note or None) - see main.normalize_connection."""
    fixed, notes, _err = vp.normalize_connection(protocol, not pull, endpoint)
    return fixed, (" ".join(notes) or None)


def udp_input_url(endpoint, localaddr="", source=""):
    """UDP / multicast address with receive options (network card, source-specific multicast)."""
    url = endpoint
    for key, value in (("fifo_size", str(UDP_FIFO_PACKETS)), ("overrun_nonfatal", "1"), ("reuse", "1")):
        url = vp.set_url_param(url, key, value)[0]
    if localaddr:
        url = vp.set_url_param(url, "localaddr", localaddr)[0]
    if source:
        url = vp.set_url_param(url, "sources", source)[0]
    return url


def timeout_args(settings):
    """Drop a connection that delivers no data for sender_timeout seconds (SRT / RTMP)."""
    us = str(int(settings.get("sender_timeout", SENDER_TIMEOUT_S) * 1_000_000))
    # (SRT's -timeout and the generic -rw_timeout only cover reading: waiting for a sender to
    # connect is not affected)
    return {"SRT": ["-timeout", us], "RTMP": ["-rw_timeout", us]}.get(settings["protocol"], [])


def input_args(settings):
    """Protocol options placed before -i."""
    if settings["protocol"] == "RTMP":
        # listen: act as the RTMP server the pusher connects to; pull: play from a server/pusher
        listen = ["-listen", "1"] if settings.get("connection", "listen") == "listen" else []
        return listen + timeout_args(settings)
    if settings["protocol"] == "RIST":
        return vp.rist_args(settings)
    return vp.srt_args(settings) + timeout_args(settings)


def ffplay_cmd(ffplay, url, settings, direct=True):
    """ffplay command. direct=False when playing the local bridge instead of the network stream."""
    # -autoexit closes ffplay (and frees the port) when the sender drops
    cmd = [ffplay, "-hide_banner", "-autoexit", "-fflags", "nobuffer", "-flags", "low_delay"]
    if direct:
        cmd += input_args(settings)
    cmd += ["-i", url, "-window_title", f"Video Receiver - {settings['protocol']}"]
    size = settings.get("win_size", "original")
    if size == "medium":
        cmd += ["-vf", "scale=iw/2:ih/2"]
    elif size == "small":
        cmd += ["-vf", "scale=iw/4:ih/4"]
    return cmd


def multicast_url(settings):
    url = f"udp://{settings['mc_ip']}:{settings['mc_port']}?pkt_size=1316"
    if settings.get("mc_local_ip"):
        url += f"&localaddr={settings['mc_local_ip']}"
    return url


def ffmpeg_cmd(ffmpeg, endpoint, settings, save_path=None, multicast=False, bridge=None):
    cmd = [ffmpeg, "-hide_banner", "-y", "-fflags", "+genpts"] + input_args(settings) + ["-i", endpoint]
    if bridge:
        cmd += ["-map", "0", "-c", "copy", "-f", "mpegts", bridge]
    if save_path:
        cmd += ["-map", "0", "-c", "copy"]
        if save_path.lower().endswith(".mp4"):
            # fragmented MP4: stays playable even if recording stops abruptly
            cmd += ["-f", "mp4", "-bsf:a", "aac_adtstoasc",
                    "-movflags", "empty_moov+default_base_moof+frag_keyframe"]
        else:
            cmd += ["-f", "mpegts"]
        cmd.append(save_path)
    if multicast:
        cmd += ["-map", "0", "-c", "copy", "-flush_packets", "0", "-f", "mpegts", multicast_url(settings)]
    return cmd


# RIST and UDP never report that the sender has gone, so the receiver watches FFmpeg's progress
# to notice; for playback, FFmpeg receives and feeds ffplay through the local bridge.
# (SRT and RTMP playback use ffplay directly, with FFmpeg's own timeouts.)
BRIDGED_PLAYBACK = ("RIST", "UDP")


def session_path(path, session):
    """First connection writes to the chosen file; later ones get a timestamp, so a sender
    reconnecting never overwrites an earlier recording."""
    if session <= 1:
        return path
    base, ext = os.path.splitext(path)
    return f"{base}_{time.strftime('%Y%m%d-%H%M%S')}{ext}"


def build_commands(settings, ffmpeg, ffplay, session=1):
    """(ffplay command or None, ffmpeg command or None) for the chosen action."""
    action, endpoint = settings["action"], settings["endpoint"]
    save = session_path(settings["output"], session) if "save" in action else None
    multicast = "multicast" in action
    if action == "play_only" and settings["protocol"] not in BRIDGED_PLAYBACK:
        return ffplay_cmd(ffplay, endpoint, settings), None
    if action in ("save_only", "multicast_only"):
        return None, ffmpeg_cmd(ffmpeg, endpoint, settings, save, multicast)
    # play + save / play + multicast: FFmpeg receives, and feeds ffplay through a local UDP bridge
    return (ffplay_cmd(ffplay, LOCAL_BRIDGE, settings, direct=False),
            ffmpeg_cmd(ffmpeg, endpoint, settings, save, multicast, bridge=LOCAL_BRIDGE))


def stats_progress(line):
    """FFmpeg's output time (seconds) from a stats line, or None."""
    m = FFMPEG_TIME.search(line)
    if not m:
        return None
    sign, h, mi, sec = m.groups()
    return (-1 if sign else 1) * (int(h) * 3600 + int(mi) * 60 + float(sec))


def is_stats_line(line):
    return vp.is_progress_line(line) or bool(FFPLAY_STATS.match(line))


# ======================================================================
# Worker (background thread; talks to the GUI only through the event queue)
# ======================================================================
class ReceiverWorker(vp.WorkerBase):
    def __init__(self, settings, ffmpeg_path, ffplay_path, events):
        super().__init__(events, vp.secret_values(settings))
        self.settings = settings
        self.ffmpeg = ffmpeg_path
        self.ffplay = ffplay_path
        self._lock = threading.Lock()
        self.proc = None        # the process whose output is read (FFmpeg, or ffplay when only playing)
        self.player = None      # ffplay running alongside FFmpeg (play + save / multicast)

    def stop(self):
        self.stop_event.set()
        with self._lock:
            proc, player = self.proc, self.player
        if player and player.poll() is None:
            player.terminate()
        if proc and proc.poll() is None:
            threading.Thread(target=self._shut_down, args=(proc,), daemon=True).start()

    def _watchdog(self, proc, player, watch):
        """Backstop for a sender that stops sending without closing the connection: once data
        has stopped for the silent-sender time, close FFmpeg / ffplay so the receiver can take
        the sender's next connection. (For SRT and RTMP FFmpeg's own timeout normally acts
        first; this one waits a second longer.)"""
        s = self.settings
        limit = s.get("sender_timeout", SENDER_TIMEOUT_S) + (1 if s["protocol"] in ("SRT", "RTMP") else 0)
        # (UDP: no FFmpeg timeout - it would also fire while waiting for the first packet)
        while proc.poll() is None and not self.stop_event.wait(0.25):
            last = watch["last"]
            if last is not None and time.monotonic() - last > limit:
                watch["fired"] = True
                self.log(f"No data from the sender for {s.get('sender_timeout', SENDER_TIMEOUT_S)} s - "
                         "closing the connection.")
                for p in (player, proc):
                    if p and p.poll() is None:
                        p.terminate()
                try:
                    proc.wait(timeout=1.5)          # nothing more is coming: no need to wait long
                except subprocess.TimeoutExpired:
                    proc.kill()
                return

    @staticmethod
    def _close_player(player):
        """Close ffplay and wait until it has gone, so the next one can open the local bridge
        port (otherwise a quick reconnect would get a recording but no picture)."""
        if not player:
            return
        if player.poll() is None:
            player.terminate()
        try:
            player.wait(timeout=3)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait()

    @staticmethod
    def _drop(proc, player):
        """Cut off a sender at once (no clean finish: nothing was recorded from it yet)."""
        for p in (player, proc):
            if p and p.poll() is None:
                p.kill()

    def _shut_down(self, proc):
        """'q' lets FFmpeg finish the file cleanly. A RIST or idle listener may not react to it,
        so escalate to terminate, then kill."""
        if proc.args[0] == self.ffmpeg:
            try:
                proc.stdin.write("q\n")
                proc.stdin.flush()
                proc.wait(timeout=STOP_GRACE_S)
                return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        proc.terminate()
        try:
            proc.wait(timeout=vp.STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()

    def run(self):
        s = self.settings
        reason = "stopped by user"
        pull = s.get("connection", "listen") == "pull"
        # RTMP server with a stream key: FFmpeg only warns about a wrong key, so reject it here
        check_key = s["protocol"] == "RTMP" and not pull and bool(s.get("rtmp_key"))
        session = 0          # connections that delivered a stream (numbers the recordings)
        attempt = 0
        failures = 0         # pull mode: consecutive attempts that never connected
        while not self.stop_event.is_set():
            attempt += 1
            play_cmd, rec_cmd = build_commands(s, self.ffmpeg, self.ffplay, session + 1)
            if pull:
                self.emit("status", f"Connecting to {s['protocol']} sender at {s['endpoint']} ...")
            else:
                self.emit("status", f"Listening for {s['protocol']} on {s['endpoint']} ...")
            quiet = failures > 0                     # don't repeat the same errors on every retry
            if not quiet:
                verb = "Pulling from" if pull else "Listening on"
                self.log(f"{verb} {s['endpoint']} ({ACTIONS[s['action']]})")
                for label, cmd in (("Player", play_cmd), ("FFmpeg", rec_cmd)):
                    if cmd:
                        self.log(f"{label}: " + subprocess.list2cmdline(vp.redact(cmd, self.secrets)))
            try:
                player = None
                if play_cmd and rec_cmd:
                    player = vp.popen(play_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
                main_cmd = rec_cmd or play_cmd
                proc = vp.popen(main_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except OSError as e:
                self.log(f"Could not start {os.path.basename(e.filename or 'FFmpeg')}: {e}")
                reason = "failed to start"
                break
            with self._lock:
                self.proc, self.player = proc, player
            if self.stop_event.is_set():      # Stop pressed while starting
                self.stop()

            receiving = rejected = timed_out = False
            watch = {"last": None, "best": None, "fired": False}
            # the watchdog follows FFmpeg's output time (ffplay's clock keeps running without data)
            watched = main_cmd is rec_cmd
            threading.Thread(target=self._watchdog, args=(proc, player, watch), daemon=True).start()
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if receiving and watched:
                    progress = stats_progress(line)
                    if isinstance(progress, float) and (watch["best"] is None or progress > watch["best"]):
                        watch["best"] = progress
                        watch["last"] = time.monotonic()          # data is still arriving
                if check_key and not receiving and RTMP_WRONG_KEY in line:
                    rejected = True
                    self.log("Rejected a sender that used the wrong stream key.")
                    self._drop(proc, player)
                    break
                if is_stats_line(line):
                    self.emit("stats", line)
                    continue
                if not receiving and line.startswith("Input #0"):
                    receiving = True
                    if watched:
                        watch["last"] = time.monotonic()
                    quiet = False
                    self.emit("status", f"Receiving {s['protocol']} stream {'from' if pull else 'on'} {s['endpoint']}")
                    self.log("Stream connected.")
                if receiving and "timed out" in line.lower():
                    timed_out = True
                if not self.stop_event.is_set() and not quiet:
                    self.emit("log", line)
            if rejected:
                for _ in proc.stdout:        # let the closed process finish its output
                    pass
            proc.wait()
            timed_out = timed_out or watch["fired"]
            self._close_player(player)
            with self._lock:
                self.proc = self.player = None

            if self.stop_event.is_set():
                break
            if receiving:
                session += 1
                failures = 0
            delay = PULL_RETRY_S if pull else RELISTEN_DELAY_S
            if rejected:
                self.log(f"Listening again in {delay} s ...")
            elif receiving:
                again = "Connecting again" if pull else "Listening again"
                why = (f"no data from the sender for {s.get('sender_timeout', SENDER_TIMEOUT_S)} s"
                       if timed_out else f"exit code {proc.returncode}")
                self.log(f"Stream ended ({why}). {again} in {delay} s ...")
            elif pull:
                failures += 1
                if failures == 1:
                    self.log(f"Couldn't connect to the sender - retrying every {delay} s "
                             "(is it running, and set to listen?)")
                elif failures % 10 == 0:
                    self.log(f"Still trying to connect (attempt {failures}) ...")
            else:
                self.log(f"Listener ended (exit code {proc.returncode}). Listening again in {delay} s ...")
            if self.stop_event.wait(delay):
                break
        self.log(f"Stopped ({reason}).")
        self.emit("done")


# ======================================================================
# GUI
# ======================================================================
class ReceiverApp(vp.SecurityMixin, vp.PersistMixin, tk.Tk):
    SETTINGS_NAME = "receiver"

    def __init__(self):
        super().__init__()
        self.title(f"Video Receiver v{APP_VERSION}")
        self.geometry("900x800")
        self.minsize(820, 680)

        self.ffmpeg_path = vp.find_tool("ffmpeg")
        self.ffplay_path = vp.find_tool("ffplay")
        self.events = queue.Queue()
        self.worker = None
        self._closing = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self._poll_job = self.after(100, self._poll_events)
        self._log(f"Receiver ready. Expecting FFmpeg at: {self.ffmpeg_path}")
        self._load_settings()

    # ---------------- UI ----------------
    def _build_ui(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)
        form = ttk.Frame(main)
        form.pack(fill=tk.X, pady=(0, 10))
        form.columnconfigure(1, weight=1)
        self._locked = []

        # Protocol
        ttk.Label(form, text="Protocol:").grid(row=0, column=0, sticky=tk.W, pady=5)
        pf = ttk.Frame(form)
        pf.grid(row=0, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        self.var_protocol = tk.StringVar(value="SRT")
        for name, label in (("SRT", "SRT"), ("RTMP", "RTMP"), ("RIST", "RIST"), ("UDP", "UDP (multicast)")):
            rb = ttk.Radiobutton(pf, text=label, value=name, variable=self.var_protocol,
                                 command=self._on_protocol_change)
            rb.pack(side=tk.LEFT, padx=(0, 15))
            self._locked.append(rb)

        # Connection direction
        ttk.Label(form, text="Connection:").grid(row=1, column=0, sticky=tk.W, pady=5)
        cf = ttk.Frame(form)
        cf.grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        self.var_connection = tk.StringVar(value="listen")
        self.rb_connection = {}
        for value, label in RX_CONNECTIONS.items():
            rb = ttk.Radiobutton(cf, text=label, value=value, variable=self.var_connection,
                                 command=self._on_protocol_change)
            rb.pack(side=tk.LEFT, padx=(0, 15))
            self._locked.append(rb)
            self.rb_connection[value] = rb
        self.lbl_timeout = ttk.Label(cf, text="Drop a silent sender after")
        self.lbl_timeout.pack(side=tk.LEFT, padx=(10, 0))
        self.var_timeout = tk.StringVar(value=str(SENDER_TIMEOUT_S))
        lo, hi = SENDER_TIMEOUT_RANGE
        self.spin_timeout = ttk.Spinbox(cf, from_=lo, to=hi, increment=1, width=4, textvariable=self.var_timeout)
        self.spin_timeout.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(cf, text="s").pack(side=tk.LEFT)

        # Address (each protocol + direction remembers its own)
        self._endpoints = dict(RX_DEFAULT_ENDPOINTS)
        self._current_protocol = "SRT"
        self._current_key = "SRT/listen"
        self.lbl_endpoint = ttk.Label(form, text="Listen on:")
        self.lbl_endpoint.grid(row=2, column=0, sticky=tk.W, pady=5)
        self.var_endpoint = tk.StringVar(value=self._endpoints["SRT/listen"])
        self.ent_endpoint = ttk.Entry(form, textvariable=self.var_endpoint)
        self.ent_endpoint.grid(row=2, column=1, columnspan=2, sticky=tk.EW, padx=10, pady=5)
        self._locked.append(self.ent_endpoint)

        # Protocol options: SRT encryption, RTMP stream key or RIST settings, with a hint below
        self.lbl_options = ttk.Label(form, text="SRT security:")
        self.lbl_options.grid(row=3, column=0, sticky=tk.NW, pady=5)
        po = ttk.Frame(form)
        po.grid(row=3, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        self._opts_row = ttk.Frame(po)
        self._opts_row.pack(side=tk.TOP, anchor=tk.W)
        self._build_security(self._opts_row, self._update_states)
        self.frm_udp = ttk.Frame(self._opts_row)
        ttk.Label(self.frm_udp, text="Local NIC IP (optional)").pack(side=tk.LEFT)
        self.var_udp_localaddr = tk.StringVar(value="")
        self.ent_udp_localaddr = ttk.Entry(self.frm_udp, textvariable=self.var_udp_localaddr, width=16)
        self.ent_udp_localaddr.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(self.frm_udp, text="Only from source IP (optional)").pack(side=tk.LEFT)
        self.var_udp_source = tk.StringVar(value="")
        self.ent_udp_source = ttk.Entry(self.frm_udp, textvariable=self.var_udp_source, width=16)
        self.ent_udp_source.pack(side=tk.LEFT, padx=(4, 0))
        self.frm_rist = ttk.Frame(self._opts_row)
        ttk.Label(self.frm_rist, text="Profile").pack(side=tk.LEFT)
        self.var_rist_profile = tk.StringVar(value="Main")
        self.cmb_rist_profile = ttk.Combobox(self.frm_rist, textvariable=self.var_rist_profile,
                                             state="readonly", width=7, values=list(vp.RIST_PROFILES))
        self.cmb_rist_profile.pack(side=tk.LEFT, padx=(4, 12))
        self.cmb_rist_profile.bind("<<ComboboxSelected>>", lambda e: self._update_states())
        ttk.Label(self.frm_rist, text="Buffer").pack(side=tk.LEFT)
        self.var_rist_buffer = tk.StringVar(value=str(vp.RIST_BUFFER_MS))
        lo, hi = vp.RIST_BUFFER_RANGE
        self.spin_rist_buffer = ttk.Spinbox(self.frm_rist, from_=lo, to=hi, increment=50, width=6,
                                            textvariable=self.var_rist_buffer)
        self.spin_rist_buffer.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(self.frm_rist, text="ms").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(self.frm_rist, text="Encryption").pack(side=tk.LEFT)
        self.var_rist_enc = tk.StringVar(value="Off")
        self.cmb_rist_enc = ttk.Combobox(self.frm_rist, textvariable=self.var_rist_enc, state="readonly",
                                         width=8, values=list(vp.RIST_ENCRYPTION))
        self.cmb_rist_enc.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_rist_enc.bind("<<ComboboxSelected>>", lambda e: self._update_states())
        ttk.Label(self.frm_rist, text="Passphrase").pack(side=tk.LEFT)
        self.var_rist_secret = tk.StringVar(value="")
        self.ent_rist_secret = ttk.Entry(self.frm_rist, textvariable=self.var_rist_secret, show="*", width=16)
        self.ent_rist_secret.pack(side=tk.LEFT, padx=(4, 0))
        self.lbl_proto_hint = ttk.Label(po, foreground="gray", text="", wraplength=700)

        # Action
        ttk.Label(form, text="Action:").grid(row=4, column=0, sticky=tk.NW, pady=5)
        self.var_mode = tk.StringVar(value="play_only")
        mf = ttk.Frame(form)
        mf.grid(row=4, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        layout = [("play_only", 0, 0), ("play_save", 0, 1), ("play_multicast", 0, 2),
                  ("save_only", 1, 0), ("multicast_only", 1, 1)]
        for value, r, c in layout:
            rb = ttk.Radiobutton(mf, text=ACTIONS[value], variable=self.var_mode, value=value,
                                 command=self._update_states)
            rb.grid(row=r, column=c, sticky=tk.W, padx=(0, 15), pady=2)
            self._locked.append(rb)

        # Window size (playback)
        ttk.Label(form, text="Window Size (ffplay):").grid(row=5, column=0, sticky=tk.W, pady=5)
        self.var_win_size = tk.StringVar(value="original")
        sf = ttk.Frame(form)
        sf.grid(row=5, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        self.rb_sizes = []
        for value, label in (("original", "Original (100%)"), ("medium", "Medium (50%)"), ("small", "Small (25%)")):
            rb = ttk.Radiobutton(sf, text=label, variable=self.var_win_size, value=value)
            rb.pack(side=tk.LEFT, padx=(0, 15))
            self.rb_sizes.append(rb)

        # Save format + output file
        ttk.Label(form, text="Save Format:").grid(row=6, column=0, sticky=tk.W, pady=5)
        self.var_format = tk.StringVar(value=".ts")
        ff = ttk.Frame(form)
        ff.grid(row=6, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        self.rb_ts = ttk.Radiobutton(ff, text="Transport Stream (.ts)", variable=self.var_format, value=".ts",
                                     command=self._update_extension)
        self.rb_ts.pack(side=tk.LEFT, padx=(0, 15))
        self.rb_mp4 = ttk.Radiobutton(ff, text="MP4 File (.mp4)", variable=self.var_format, value=".mp4",
                                      command=self._update_extension)
        self.rb_mp4.pack(side=tk.LEFT)
        ttk.Label(form, text="Output File:").grid(row=7, column=0, sticky=tk.W, pady=5)
        self.var_output = tk.StringVar(value="received_stream.ts")
        self.entry_output = ttk.Entry(form, textvariable=self.var_output)
        self.entry_output.grid(row=7, column=1, sticky=tk.EW, padx=10, pady=5)
        self.btn_browse = ttk.Button(form, text="Browse...", command=self._browse_file)
        self.btn_browse.grid(row=7, column=2, sticky=tk.E, pady=5)

        # Multicast target
        ttk.Label(form, text="Multicast Target:").grid(row=8, column=0, sticky=tk.W, pady=5)
        mc = ttk.Frame(form)
        mc.grid(row=8, column=1, columnspan=2, sticky=tk.W, padx=10, pady=5)
        ttk.Label(mc, text="IP:").pack(side=tk.LEFT)
        self.var_mc_ip = tk.StringVar(value="239.0.0.1")
        self.entry_mc_ip = ttk.Entry(mc, textvariable=self.var_mc_ip, width=15)
        self.entry_mc_ip.pack(side=tk.LEFT, padx=(5, 15))
        ttk.Label(mc, text="Port:").pack(side=tk.LEFT)
        self.var_mc_port = tk.StringVar(value="5000")
        self.entry_mc_port = ttk.Entry(mc, textvariable=self.var_mc_port, width=8)
        self.entry_mc_port.pack(side=tk.LEFT, padx=(5, 15))
        ttk.Label(mc, text="Local NIC IP (Optional):").pack(side=tk.LEFT)
        self.var_mc_local_ip = tk.StringVar(value="")
        self.entry_mc_local_ip = ttk.Entry(mc, textvariable=self.var_mc_local_ip, width=15)
        self.entry_mc_local_ip.pack(side=tk.LEFT, padx=5)

        # Buttons
        bf = ttk.Frame(main)
        bf.pack(fill=tk.X, pady=10)
        for i in range(3):
            bf.columnconfigure(i, weight=1)
        self.btn_start = ttk.Button(bf, text="▶ START LISTENING", command=self._start_receive)
        self.btn_start.grid(row=0, column=0, sticky=tk.EW, padx=5)
        self.btn_stop = ttk.Button(bf, text="■ STOP", command=self._stop_receive, state=tk.DISABLED)
        self.btn_stop.grid(row=0, column=1, sticky=tk.EW, padx=5)
        ttk.Button(bf, text="✕ EXIT", command=self.on_closing).grid(row=0, column=2, sticky=tk.EW, padx=5)

        # Status, stats, log
        self.var_status = tk.StringVar(value="Idle")
        ttk.Entry(main, textvariable=self.var_status, state="readonly").pack(fill=tk.X, pady=(0, 2))
        self.var_stats = tk.StringVar(value="")
        ttk.Entry(main, textvariable=self.var_stats, state="readonly").pack(fill=tk.X, pady=(0, 5))
        lh = ttk.Frame(main)
        lh.pack(fill=tk.X, pady=(5, 0))
        ttk.Label(lh, text="Log:").pack(side=tk.LEFT)
        ttk.Button(lh, text="Clear Log", command=self._clear_log).pack(side=tk.RIGHT)
        self.txt_log = scrolledtext.ScrolledText(main, wrap=tk.WORD, height=12, state=tk.DISABLED)
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        self._update_states()

    # ---------------- saved settings ----------------
    def _persist_vars(self):
        return {
            "protocol": (self.var_protocol, list(RX_PROTOCOLS)),
            "connection": (self.var_connection, list(RX_CONNECTIONS)),
            "action": (self.var_mode, list(ACTIONS)),
            "window_size": (self.var_win_size, ["original", "medium", "small"]),
            "save_format": (self.var_format, [".ts", ".mp4"]),
            "output_file": (self.var_output, None),
            "multicast_ip": (self.var_mc_ip, None),
            "multicast_port": (self.var_mc_port, None),
            "multicast_local_ip": (self.var_mc_local_ip, None),
            "udp_localaddr": (self.var_udp_localaddr, None),
            "udp_source": (self.var_udp_source, None),
            "rist_profile": (self.var_rist_profile, list(vp.RIST_PROFILES)),
            "rist_buffer_ms": (self.var_rist_buffer, None),
            "rist_encryption": (self.var_rist_enc, list(vp.RIST_ENCRYPTION)),
            "sender_timeout": (self.var_timeout, None),
            "srt_encryption": (self.var_srt_enc, list(vp.SRT_ENCRYPTION)),
            "srt_streamid": (self.var_srt_streamid, None),
            # passphrases and the RTMP stream key are deliberately not saved
        }

    def _persist_extras(self):
        endpoints = dict(self._endpoints)
        endpoints[self._current_key] = self.var_endpoint.get()
        endpoints = {k: vp.strip_saved_secrets(v, rtmp_key=k.startswith("RTMP/")) for k, v in endpoints.items()}
        return {"endpoints_by_direction": endpoints}

    def _restore_extras(self, data):
        # settings saved before 2.6.0 have one address per protocol: those were listen addresses
        old = vp.str_dict(data.get("endpoints"), RX_PROTOCOLS)
        self._endpoints.update({f"{p}/listen": url for p, url in old.items()})
        self._endpoints.update(vp.str_dict(data.get("endpoints_by_direction"), RX_DEFAULT_ENDPOINTS))
        self._current_protocol = self.var_protocol.get()
        self._current_key = self._endpoint_key()
        self.var_endpoint.set(self._endpoints[self._current_key])
        self._update_states()

    def _endpoint_key(self):
        return f"{self.var_protocol.get()}/{self.var_connection.get()}"

    def _on_protocol_change(self):
        """Protocol or direction changed: show that combination's remembered address."""
        if self.var_protocol.get() == "UDP":
            self.var_connection.set("listen")     # UDP: the sender just sends - nothing to pull from
        key = self._endpoint_key()
        if key != self._current_key:
            self._endpoints[self._current_key] = self.var_endpoint.get()
            self.var_endpoint.set(self._endpoints[key])
            self._current_key = key
        self._current_protocol = self.var_protocol.get()
        self._update_states()

    def _update_states(self, locked=None):
        if locked is None:
            locked = self.worker is not None and self.worker.is_alive()
        protocol, mode = self.var_protocol.get(), self.var_mode.get()

        # protocol options row
        pull = self.var_connection.get() == "pull"
        self.lbl_endpoint.config(text="Pull from:" if pull else "Listen on:")
        hints = {
            "SRT": ("Pull: calls a pusher that is listening (pusher Connection = Listen)." if pull else
                    "Listen: the pusher calls this PC (srt://this-pc:9000?mode=caller).")
                   + " Encryption: same passphrase (10-79 characters) as the sender.",
            "RTMP": ("Pull: plays from an RTMP server or a pusher set to Listen (address rtmp://host:1935/app "
                     "plus the stream key, if the server needs one)."
                     if pull else "Listen: this PC acts as the RTMP server - point the pusher at "
                     "rtmp://this-pc:1935/live with the same stream key. Senders using another key are "
                     "rejected (no key = accept any). One sender at a time."),
            "RIST": "Same profile and passphrase as the sender; use an even port.",
            "UDP": ("MPEG-TS over UDP: a multicast group (e.g. udp://239.1.1.1:5000) is joined; for a stream "
                    "sent to this PC's own IP use udp://0.0.0.0:port. Local NIC IP picks the network card on a "
                    "PC with several. No encryption or retransmission - for the local network."),
        }
        self.lbl_options.config(text={"SRT": "SRT security:", "RTMP": "RTMP:", "RIST": "RIST options:",
                                      "UDP": "UDP options:"}[protocol])
        for name, frame in (("SRT", self.frm_srt), ("RTMP", self.frm_rtmp), ("RIST", self.frm_rist),
                            ("UDP", self.frm_udp)):
            if name == protocol:
                frame.pack(side=tk.LEFT)
            else:
                frame.pack_forget()
        self.lbl_proto_hint.config(text=hints[protocol])
        self.lbl_proto_hint.pack(side=tk.TOP, anchor=tk.W, pady=(4, 0))
        self._update_security_controls(locked, streamid_on=pull, key_on=True)
        self.spin_timeout.config(state=tk.NORMAL if not locked else tk.DISABLED)
        self.btn_start.config(text="▶ START PULLING" if pull else "▶ START LISTENING")
        rist_on = protocol == "RIST" and not locked
        main_profile = self.var_rist_profile.get() == "Main"
        if not main_profile:
            self.var_rist_enc.set("Off")        # encryption needs the Main profile
        self.cmb_rist_profile.config(state="readonly" if rist_on else tk.DISABLED)
        self.spin_rist_buffer.config(state=tk.NORMAL if rist_on else tk.DISABLED)
        self.cmb_rist_enc.config(state="readonly" if rist_on and main_profile else tk.DISABLED)
        enc = rist_on and main_profile and self.var_rist_enc.get() != "Off"
        self.ent_rist_secret.config(state=tk.NORMAL if enc else tk.DISABLED)

        # action-dependent sections
        def state(on):
            return tk.NORMAL if on and not locked else tk.DISABLED
        for rb in self.rb_sizes:
            rb.config(state=state(mode.startswith("play")))
        save = "save" in mode
        for w in (self.rb_ts, self.rb_mp4, self.entry_output, self.btn_browse):
            w.config(state=state(save))
        mc = "multicast" in mode
        for w in (self.entry_mc_ip, self.entry_mc_port, self.entry_mc_local_ip):
            w.config(state=state(mc))
        for w in self._locked:
            w.config(state=tk.DISABLED if locked else tk.NORMAL)
        udp = protocol == "UDP"
        if udp:
            self.rb_connection["pull"].config(state=tk.DISABLED)
        for w in (self.ent_udp_localaddr, self.ent_udp_source):
            w.config(state=tk.NORMAL if udp and not locked else tk.DISABLED)

    def _update_extension(self):
        current = self.var_output.get()
        if current:
            self.var_output.set(os.path.splitext(current)[0] + self.var_format.get())

    def _browse_file(self):
        ext = self.var_format.get()
        types = [("Transport Stream", "*.ts")] if ext == ".ts" else [("MP4 File", "*.mp4")]
        f = filedialog.asksaveasfilename(defaultextension=ext, filetypes=types + [("All Files", "*.*")])
        if f:
            self.var_output.set(f)
            if f.lower().endswith(".mp4"):
                self.var_format.set(".mp4")
            elif f.lower().endswith(".ts"):
                self.var_format.set(".ts")

    # ---------------- log / events (GUI thread only) ----------------
    def _log(self, msg):
        self.events.put(("log", f"[{time.strftime('%H:%M:%S')}] {vp.mask_secrets(msg)}"))

    def _clear_log(self):
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def _append_log(self, text):
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.insert(tk.END, text + "\n")
        overflow = int(self.txt_log.index("end-1c").split(".")[0]) - vp.MAX_LOG_LINES
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
        """Read and validate the inputs on the GUI thread; returns a settings dict or None."""
        protocol, action = self.var_protocol.get(), self.var_mode.get()
        endpoint = self.var_endpoint.get().strip()
        if not endpoint:
            self._log("Error: enter the address to listen on or pull from.")
            return None
        schemes = RX_PROTOCOLS[protocol]["schemes"]
        if not endpoint.lower().startswith(schemes):
            self._log(f"Error: {protocol} address must start with " + " or ".join(schemes))
            return None
        connection = self.var_connection.get()
        # (UDP: only the address is checked - there is no direction)
        endpoint, notes, err = vp.normalize_connection(protocol, connection == "listen" and protocol != "UDP",
                                                       endpoint)
        if err:
            self._log("Error: " + err)
            return None
        for note in notes:
            self._log(note if note.startswith("Warning") else "Note: " + note)
        checked = self._read_security(protocol, endpoint, streamid_on=connection == "pull", key_on=True)
        if checked is None:
            return None
        security, endpoint = checked         # security["endpoint"] adds the stream ID / stream key
        self.var_endpoint.set(endpoint)
        if protocol == "RIST" and connection == "pull" and self.var_rist_profile.get() != "Main":
            self._log("Error: pulling RIST needs the Main profile (with Simple, the sender must connect to you).")
            return None

        needs_player = action.startswith("play")
        needs_ffmpeg = action != "play_only"
        if needs_player and not os.path.isfile(self.ffplay_path):
            self._log(f"Error: ffplay not found at {self.ffplay_path}")
            return None
        if needs_ffmpeg and not os.path.isfile(self.ffmpeg_path):
            self._log(f"Error: FFmpeg not found at {self.ffmpeg_path}")
            return None
        tool = self.ffmpeg_path if needs_ffmpeg else self.ffplay_path
        scheme = vp.url_scheme(endpoint)
        supported = vp.ffmpeg_protocols(tool, "input")
        if supported and scheme not in supported:
            need = {"srt": "libsrt", "rist": "librist"}.get(scheme, scheme)
            self._log(f"Error: this {os.path.basename(tool)} build can't receive {scheme}:// (it lacks {need}). "
                      "Use a 'full' FFmpeg build - check with: ffmpeg -protocols")
            return None

        lo, hi = SENDER_TIMEOUT_RANGE
        try:
            sender_timeout = int(self.var_timeout.get().strip())
            if not lo <= sender_timeout <= hi:
                raise ValueError
        except ValueError:
            self._log(f"Error: the silent-sender time must be a whole number of seconds from {lo} to {hi}.")
            return None
        settings = {"protocol": protocol, "action": action, "endpoint": endpoint, "connection": connection,
                    "win_size": self.var_win_size.get(), "sender_timeout": sender_timeout, **security}
        if protocol == "RTMP" and connection == "listen" and not security["rtmp_key"]:
            self._log("Note: no stream key set - any sender can publish to this address.")
        if protocol == "RIST":
            lo, hi = vp.RIST_BUFFER_RANGE
            try:
                buffer_ms = int(self.var_rist_buffer.get().strip())
                if not lo <= buffer_ms <= hi:
                    raise ValueError
            except ValueError:
                self._log(f"Error: RIST buffer must be a whole number of milliseconds from {lo} to {hi}.")
                return None
            encryption = vp.RIST_ENCRYPTION[self.var_rist_enc.get()]
            secret = self.var_rist_secret.get()
            if encryption and not secret:
                self._log("Error: enter the RIST passphrase, or set Encryption to Off.")
                return None
            settings.update(rist_profile=vp.RIST_PROFILES[self.var_rist_profile.get()],
                            rist_buffer_ms=buffer_ms, rist_encryption=encryption,
                            rist_secret=secret if encryption else "")
        if "save" in action:
            out = self.var_output.get().strip()
            if not out:
                self._log("Error: choose an output file.")
                return None
            folder = os.path.dirname(os.path.abspath(out))
            if not os.path.isdir(folder):
                self._log(f"Error: folder does not exist: {folder}")
                return None
            settings["output"] = out
        if "multicast" in action:
            ip = self.var_mc_ip.get().strip()
            try:
                port = int(self.var_mc_port.get().strip())
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                self._log("Error: multicast port must be a number from 1 to 65535.")
                return None
            if not ip:
                self._log("Error: enter a multicast IP address.")
                return None
            settings.update(mc_ip=ip, mc_port=port, mc_local_ip=self.var_mc_local_ip.get().strip())
        if protocol == "UDP":
            parts = vp.urlsplit(endpoint)
            ip4 = r"\d{1,3}(\.\d{1,3}){3}"
            localaddr, source = self.var_udp_localaddr.get().strip(), self.var_udp_source.get().strip()
            for name, value in (("Local NIC IP", localaddr), ("Source IP", source)):
                if value and not re.fullmatch(ip4, value):
                    self._log(f"Error: {name} must be an IPv4 address (or empty).")
                    return None
            if source and not vp.is_multicast(parts.hostname):
                self._log("Error: 'Only from source IP' works with a multicast group address (224-239.x.x.x).")
                return None
            if "multicast" in action and (parts.hostname, parts.port) == (settings["mc_ip"], settings["mc_port"]):
                self._log("Error: the multicast relay target is the group being received - that would loop.")
                return None
            if parts.port == int(LOCAL_BRIDGE.rsplit(":", 1)[1]) and action.startswith("play"):
                self._log(f"Error: port {parts.port} is used internally for playback while saving; pick another.")
                return None
            settings["endpoint"] = udp_input_url(endpoint, localaddr, source)
        return settings

    def _start_receive(self):
        if self.worker and self.worker.is_alive():
            return  # previous run still shutting down
        settings = self._read_settings()
        if not settings:
            return
        self._save_settings()
        self.worker = ReceiverWorker(settings, self.ffmpeg_path, self.ffplay_path, self.events)
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.var_stats.set("")
        self.worker.start()
        self._update_states(locked=True)

    def _stop_receive(self):
        if self.worker and self.worker.is_alive():
            self.btn_stop.config(state=tk.DISABLED)
            self.var_status.set("Stopping...")
            self.worker.stop()
        # START is re-enabled when the worker reports "done"

    def _on_worker_done(self):
        self.worker = None
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.var_status.set("Idle")
        self._update_states(locked=False)
        if self._closing:
            self.destroy()

    def on_closing(self):
        if self._closing:
            return
        self._save_settings()
        self._closing = True
        if self.worker and self.worker.is_alive():
            self.var_status.set("Stopping before exit...")
            self.worker.stop()
            self._wait_then_close()
        else:
            self.destroy()

    def _wait_then_close(self, waited=0.0):
        if self.worker is None:
            return
        if not self.worker.is_alive() or waited > STOP_GRACE_S + vp.STOP_GRACE_S + 2:
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
    ReceiverApp().mainloop()
