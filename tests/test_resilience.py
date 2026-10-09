"""Receiver resilience: a sender that vanishes without closing the connection (network drop,
crash, frozen PC) must not keep the receiver busy - its new connection has to be accepted."""
import queue
import signal
import subprocess
import sys
import time

import pytest

import main as vp
import receiver as rx
from test_receiver import need, port, rx_settings, stop_receiver, tools  # noqa: F401
from test_rist import received_seconds
from test_settings import needs_gui

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="freezes the sender with SIGSTOP")


def sender(ffmpeg, clip, protocol, url):
    fmt = "flv" if protocol == "RTMP" else "mpegts"
    return subprocess.Popen([ffmpeg, "-v", "error", "-re", "-stream_loop", "-1", "-i", clip, "-c", "copy",
                             "-f", fmt, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def keep_sending(ffmpeg, clip, protocol, url, seconds):
    """Like a pusher with auto-reconnect: retry every second until connected, then send."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        p = sender(ffmpeg, clip, protocol, url)
        while p.poll() is None and time.monotonic() < end:
            time.sleep(0.2)
        if p.poll() is None:
            p.kill()
            p.wait()
            return
        time.sleep(1)


@pytest.mark.parametrize("protocol", ["SRT", "RTMP"])
def test_frozen_sender_is_dropped_and_new_connection_accepted(tools, tmp_path, protocol):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, protocol.lower())
    p = port()
    lis, snd = {"SRT": (f"srt://127.0.0.1:{p}?mode=listener", f"srt://127.0.0.1:{p}?mode=caller"),
                "RTMP": (f"rtmp://127.0.0.1:{p}/live", f"rtmp://127.0.0.1:{p}/live/k")}[protocol]
    out = tmp_path / "rec.ts"
    ev = queue.Queue()
    w = rx.ReceiverWorker(rx_settings(protocol=protocol, endpoint=lis, output=str(out), sender_timeout=3,
                                      rtmp_key=""), ffmpeg, "ffplay", ev)
    w.start()
    time.sleep(1.5)
    first = sender(ffmpeg, clip, protocol, snd)
    time.sleep(8)
    first.send_signal(signal.SIGSTOP)              # the sender goes silent, connection left open
    try:
        keep_sending(ffmpeg, clip, protocol, snd, 22)  # ... and a new one keeps trying to connect
    finally:
        first.kill()
        first.wait()
    logs, _ = stop_receiver(w, ev)
    assert sum("Stream connected." in l for l in logs) >= 2
    assert any("no data from the sender for 3 s" in l for l in logs) or protocol == "RTMP"
    second = sorted(tmp_path.glob("rec_*.ts"))
    assert second and received_seconds(ffprobe, second[0]) >= 4


def test_play_and_save_bridge_reopens_after_quick_reconnect(tools, tmp_path, monkeypatch):
    """The local ffplay is fully closed before the next one starts (it would fail to open the
    bridge port otherwise)."""
    closed = []

    class FakePlayer:
        def __init__(self):
            self.alive = True
        def poll(self):
            return None if self.alive else 0
        def terminate(self):
            pass                                    # slow to close: still alive after terminate
        def wait(self, timeout=None):
            if timeout is not None and not closed:
                closed.append("waited")
                raise subprocess.TimeoutExpired("ffplay", timeout)
            self.alive = False
            return 0
        def kill(self):
            closed.append("killed")
    fp = FakePlayer()
    rx.ReceiverWorker._close_player(fp)
    assert closed == ["waited", "killed"] and fp.poll() == 0


def test_stats_progress_parsing():
    assert rx.stats_progress("frame=  100 fps= 25 q=-1.0 size=  1024kB time=00:01:04.50 bitrate=2097.2kbits/s") == 64.5
    assert rx.stats_progress("   6.62 A-V:  0.010 fd=   0 aq=   10KB vq=  100KB sq=    0B f=0/0") is None
    assert rx.stats_progress("size=       0kB time=N/A bitrate=N/A") is None


def test_rist_and_udp_playback_go_through_ffmpeg():
    for protocol in ("RIST", "UDP"):
        play, rec = rx.build_commands(rx_settings(protocol=protocol, action="play_only"), "ffmpeg", "ffplay")
        assert rec and rx.LOCAL_BRIDGE in rec and play[play.index("-i") + 1] == rx.LOCAL_BRIDGE
    play, rec = rx.build_commands(rx_settings(protocol="SRT", action="play_only"), "ffmpeg", "ffplay")
    assert rec is None


@pytest.mark.parametrize("action", ["save_only", "play_only"])
def test_rist_sender_that_stops_is_noticed_and_next_one_recorded(tools, tmp_path, action):
    """RIST never reports a sender leaving; the receiver's watchdog notices the silence, says so,
    and starts over for the next sender."""
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "rist")
    if action == "play_only" and not __import__("shutil").which("ffplay"):
        pytest.skip("no ffplay")
    p = port()
    out = tmp_path / "rec.ts"
    ev = queue.Queue()
    w = rx.ReceiverWorker(rx_settings(protocol="RIST", action=action, endpoint=f"rist://@127.0.0.1:{p}",
                                      output=str(out), sender_timeout=3), ffmpeg, "ffplay", ev)
    w.start()
    time.sleep(1.5)
    first = sender(ffmpeg, clip, "RIST", f"rist://127.0.0.1:{p}")
    time.sleep(10)
    first.terminate()
    first.wait()
    time.sleep(9)                                   # silence: noticed after 3 s, then listening again
    second = sender(ffmpeg, clip, "RIST", f"rist://127.0.0.1:{p}")
    time.sleep(12)
    second.kill()
    second.wait()
    logs, _ = stop_receiver(w, ev)
    assert any("No data from the sender for 3 s - closing the connection." in l for l in logs)
    assert any("Stream ended (no data from the sender for 3 s)" in l for l in logs)
    assert sum("Stream connected." in l for l in logs) >= 2
    if action == "save_only":
        assert received_seconds(ffprobe, sorted(tmp_path.glob("rec_*.ts"))[0]) >= 3


@pytest.mark.parametrize("action", ["save_only", "play_only"])
def test_steady_stream_is_not_dropped(tools, tmp_path, action):
    """The watchdog must leave a healthy stream alone (here: 20 s with a 2 s limit)."""
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "srt")
    if action == "play_only" and not __import__("shutil").which("ffplay"):
        pytest.skip("no ffplay")
    p = port()
    out = tmp_path / "rec.ts"
    ev = queue.Queue()
    w = rx.ReceiverWorker(rx_settings(protocol="SRT", action=action, endpoint=f"srt://127.0.0.1:{p}?mode=listener",
                                      output=str(out), sender_timeout=2), ffmpeg, "ffplay", ev)
    w.start()
    time.sleep(1.5)
    snd = sender(ffmpeg, clip, "SRT", f"srt://127.0.0.1:{p}?mode=caller")
    time.sleep(22)
    snd.kill()
    snd.wait()
    logs, _ = stop_receiver(w, ev)
    assert sum("Stream connected." in l for l in logs) == 1
    assert not any("No data from the sender" in l for l in logs)
    if action == "save_only":
        assert received_seconds(ffprobe, out) >= 15


@needs_gui
def test_start_button_names_the_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PUSHER_CONFIG_DIR", str(tmp_path / "cfg"))
    app = rx.ReceiverApp()
    try:
        assert app.btn_start["text"] == "▶ START LISTENING"
        app.var_connection.set("pull"); app._on_protocol_change()
        assert app.btn_start["text"] == "▶ START PULLING"
        app.var_protocol.set("RIST"); app._on_protocol_change()
        assert str(app.spin_timeout["state"]) == "normal"          # the silent-sender time applies to RIST too
    finally:
        app.destroy()
