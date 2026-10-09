"""Video Receiver tests.

Unit tests check the commands it builds. End-to-end tests send with Video Pusher and receive with
Video Receiver over SRT, RTMP and RIST (skipped when FFmpeg lacks a protocol). The playback test
needs ffplay and a display (DISPLAY set), so it is skipped on headless CI."""
import os
import queue
import random
import shutil
import subprocess
import time

import pytest

import main as vp
import receiver as rx
from conftest import _tool
from test_rist import make_real_clip, received_seconds


def rx_settings(**kw):
    base = dict(protocol="SRT", action="save_only", endpoint="srt://0.0.0.0:9000?mode=listener",
                win_size="original", output="out.ts", mc_ip="239.0.0.1", mc_port=5000, mc_local_ip="",
                rist_profile="main", rist_buffer_ms=1000, rist_encryption=None, rist_secret="")
    base.update(kw)
    return base


# ---------------- unit ----------------
def test_input_args_per_protocol():
    assert rx.input_args(rx_settings(protocol="SRT")) == ["-timeout", "5000000"]
    assert rx.input_args(rx_settings(protocol="RTMP")) == ["-listen", "1", "-rw_timeout", "5000000"]
    assert rx.input_args(rx_settings(protocol="RTMP", sender_timeout=12)) == ["-listen", "1", "-rw_timeout", "12000000"]
    assert rx.input_args(rx_settings(protocol="RIST", rist_encryption="128", rist_secret="pw")) == \
        ["-rist_profile", "main", "-buffer_size", "1000", "-secret", "pw", "-encryption", "128"]


def test_rist_endpoint_gets_listen_marker():
    assert rx.normalize_endpoint("RIST", "rist://0.0.0.0:5000")[0] == "rist://@0.0.0.0:5000"
    assert rx.normalize_endpoint("RIST", "rist://@0.0.0.0:5000") == ("rist://@0.0.0.0:5000", None)
    assert rx.normalize_endpoint("SRT", "srt://0.0.0.0:9000?mode=listener")[1] is None
    assert rx.normalize_endpoint("RIST", "rist://@10.0.0.5:5000", pull=True)[0] == "rist://10.0.0.5:5000"


def test_rtmp_pull_has_no_listen_flag():
    assert "-listen" not in rx.input_args(rx_settings(protocol="RTMP", connection="pull"))
    assert rx.input_args(rx_settings(protocol="RTMP", connection="listen"))[:2] == ["-listen", "1"]


def test_play_only_uses_ffplay_directly():
    play, rec = rx.build_commands(rx_settings(action="play_only", protocol="RTMP",
                                              endpoint="rtmp://0.0.0.0:1935/live/s", win_size="medium"),
                                  "ffmpeg", "ffplay")
    assert rec is None
    assert play[:2] == ["ffplay", "-hide_banner"] and "-listen" in play
    assert play[play.index("-vf") + 1] == "scale=iw/2:ih/2"


def test_play_save_uses_local_bridge():
    play, rec = rx.build_commands(rx_settings(action="play_save", protocol="RIST",
                                              endpoint="rist://@0.0.0.0:5000", output="rec.mp4"),
                                  "ffmpeg", "ffplay")
    assert play[play.index("-i") + 1] == rx.LOCAL_BRIDGE and "-rist_profile" not in play
    assert rec[rec.index("-i") - 4:rec.index("-i")] == ["-rist_profile", "main", "-buffer_size", "1000"]
    assert rx.LOCAL_BRIDGE in rec and rec[-1] == "rec.mp4" and "aac_adtstoasc" in rec


def test_multicast_url():
    s = rx_settings(action="multicast_only", mc_ip="239.1.1.1", mc_port=1234, mc_local_ip="10.0.0.2")
    _, rec = rx.build_commands(s, "ffmpeg", "ffplay")
    assert rec[-1] == "udp://239.1.1.1:1234?pkt_size=1316&localaddr=10.0.0.2"


def test_later_sessions_get_new_file_names():
    assert rx.session_path("/x/rec.ts", 1) == "/x/rec.ts"
    later = rx.session_path("/x/rec.ts", 2)
    assert later.startswith("/x/rec_") and later.endswith(".ts") and later != "/x/rec.ts"


def test_stats_lines():
    assert rx.is_stats_line("frame=  100 fps= 25 q=-1.0 size=  1024kB time=00:00:04.00 bitrate=2097.2kbits/s")
    assert rx.is_stats_line("   6.62 A-V:  0.010 fd=   0 aq=   10KB vq=  100KB sq=    0B f=0/0")
    assert rx.is_stats_line("    nan    :  0.000 fd=   0 aq=    0KB vq=    0KB sq=    0B f=0/0")
    assert not rx.is_stats_line("Input #0, mpegts, from 'srt://...'")


# ---------------- end to end: Video Pusher -> Video Receiver ----------------
@pytest.fixture(scope="module")
def tools(ffmpeg_tools, tmp_path_factory):
    ffmpeg, ffprobe = ffmpeg_tools
    clip = tmp_path_factory.mktemp("rxclip") / "clip.mp4"
    make_real_clip(ffmpeg, clip, seconds=4)
    return ffmpeg, ffprobe, str(clip)


def port():
    return random.randrange(20000, 30000) // 2 * 2


def need(ffmpeg, scheme):
    if scheme not in vp.ffmpeg_protocols(ffmpeg, "input") or scheme not in vp.ffmpeg_output_protocols(ffmpeg):
        pytest.skip(f"FFmpeg has no {scheme}")


def push(ffmpeg, clip, protocol, endpoint, seconds, **extra):
    """Run Video Pusher (single file loop) for `seconds`."""
    s = dict(mode="file", protocol=protocol, source=clip, endpoint=endpoint, bandwidth_k=0, reencode=False,
             reconnect=False, reconnect_delay=1, output_format=vp.FORMAT_AUTO, **extra)
    ev = queue.Queue()
    w = vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), False, 1, ev)
    w.start()
    time.sleep(seconds)
    w.stop()
    w.join(15)


def start_receiver(ffmpeg, settings, ffplay="ffplay"):
    ev = queue.Queue()
    w = rx.ReceiverWorker(settings, ffmpeg, ffplay, ev)
    w.start()
    time.sleep(1.5)
    return w, ev


def stop_receiver(w, ev):
    t = time.monotonic()
    w.stop()
    w.join(20)
    assert not w.is_alive(), "receiver did not stop"
    logs = []
    while not ev.empty():
        kind, payload = ev.get()
        if kind == "log":
            logs.append(payload)
    return logs, time.monotonic() - t


@pytest.mark.parametrize("protocol,listen,send", [
    ("SRT", "srt://127.0.0.1:{p}?mode=listener", "srt://127.0.0.1:{p}?mode=caller"),
    ("RTMP", "rtmp://127.0.0.1:{p}/live/stream", "rtmp://127.0.0.1:{p}/live/stream"),
    ("RIST", "rist://@127.0.0.1:{p}", "rist://127.0.0.1:{p}"),
])
def test_pusher_to_receiver_save(tools, tmp_path, protocol, listen, send):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, protocol.lower())
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol=protocol, endpoint=listen.format(p=p), output=str(out)))
    push(ffmpeg, clip, protocol, send.format(p=p), 8)
    time.sleep(2)
    logs, stop_time = stop_receiver(w, ev)
    assert any("Stream connected." in l for l in logs)
    assert received_seconds(ffprobe, out) >= 5
    assert stop_time < 10


def test_rtmp_mp4_and_new_file_per_connection(tools, tmp_path):
    """RTMP sender connects twice: the first recording is kept and the second gets its own file."""
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "rtmp")
    p = port()
    out = tmp_path / "rec.mp4"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="RTMP", endpoint=f"rtmp://127.0.0.1:{p}/live/stream",
                                               output=str(out)))
    push(ffmpeg, clip, "RTMP", f"rtmp://127.0.0.1:{p}/live/stream", 5)
    time.sleep(3)                         # receiver goes back to listening
    push(ffmpeg, clip, "RTMP", f"rtmp://127.0.0.1:{p}/live/stream", 5)
    time.sleep(2)
    logs, _ = stop_receiver(w, ev)
    files = sorted(f for f in os.listdir(tmp_path) if f.endswith(".mp4"))
    assert len(files) == 2 and "rec.mp4" in files
    for f in files:
        assert received_seconds(ffprobe, tmp_path / f) >= 3
    assert any("Listening again" in l for l in logs)


def test_rist_encrypted_receive(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "rist")
    p = port()
    out = tmp_path / "rec.ts"
    enc = dict(rist_profile="main", rist_buffer_ms=1000, rist_encryption="256", rist_secret="Sh4redSecret!")
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="RIST", endpoint=f"rist://@127.0.0.1:{p}",
                                               output=str(out), **enc))
    push(ffmpeg, clip, "RIST", f"rist://127.0.0.1:{p}", 7, **enc)
    time.sleep(2)
    logs, stop_time = stop_receiver(w, ev)
    assert received_seconds(ffprobe, out) >= 4
    assert not any("Sh4redSecret!" in l for l in logs)
    assert stop_time < 10                 # an idle RIST listener ignores 'q'; Stop must still work


def test_multicast_relay(tools, tmp_path):
    """Multicast Only re-sends the stream as MPEG-TS over UDP (a unicast address is used here, as
    containers often block multicast)."""
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "srt")
    p, udp_port = port(), port() + 1
    cap_file = tmp_path / "udp.ts"
    cap = subprocess.Popen([ffmpeg, "-v", "error", "-y", "-i", f"udp://127.0.0.1:{udp_port}", "-c", "copy",
                            "-f", "mpegts", str(cap_file)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    w, ev = start_receiver(ffmpeg, rx_settings(action="multicast_only", endpoint=f"srt://127.0.0.1:{p}?mode=listener",
                                               mc_ip="127.0.0.1", mc_port=udp_port))
    push(ffmpeg, clip, "SRT", f"srt://127.0.0.1:{p}?mode=caller", 8)
    time.sleep(2)
    stop_receiver(w, ev)
    cap.terminate()                       # a UDP reader can block; force it if needed
    try:
        cap.wait(3)
    except subprocess.TimeoutExpired:
        cap.kill()
        cap.wait()
    assert received_seconds(ffprobe, cap_file) >= 4


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs a display for ffplay")
def test_play_and_save(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    ffplay = _tool("ffplay")
    if not ffplay:
        pytest.skip("ffplay not found")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(action="play_save", endpoint=f"srt://127.0.0.1:{p}?mode=listener",
                                               output=str(out), win_size="small"), ffplay)
    import threading
    sender = threading.Thread(target=push, args=(ffmpeg, clip, "SRT", f"srt://127.0.0.1:{p}?mode=caller", 8))
    sender.start()
    player_seen = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:          # check ffplay is running while the stream plays
        with w._lock:
            if w.player is not None and w.player.poll() is None:
                player_seen = True
        time.sleep(0.2)
    sender.join()
    time.sleep(1)
    logs, _ = stop_receiver(w, ev)
    assert player_seen and any("Player: " in l and rx.LOCAL_BRIDGE in l for l in logs)
    assert received_seconds(ffprobe, out) >= 5


# ---------------- reverse direction: Video Pusher listens, Video Receiver pulls ----------------
import threading


def push_listen(ffmpeg, clip, protocol, endpoint, seconds, events=None, **extra):
    """Run Video Pusher (single file) in listen mode for `seconds`; returns its log lines."""
    s = dict(mode="file", protocol=protocol, source=clip, endpoint=endpoint, bandwidth_k=0, reencode=False,
             reconnect=True, reconnect_delay=1, output_format=vp.FORMAT_AUTO, connection="listen", **extra)
    ev = events or queue.Queue()
    w = vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), True, 1, ev, listen=protocol)
    w.start()
    time.sleep(seconds)
    w.stop()
    w.join(15)
    logs = []
    while not ev.empty():
        kind, payload = ev.get()
        if kind == "log":
            logs.append(payload)
    return logs


@pytest.mark.parametrize("protocol,listen,pull", [
    ("SRT", "srt://127.0.0.1:{p}?mode=listener", "srt://127.0.0.1:{p}?mode=caller"),
    ("RTMP", "rtmp://127.0.0.1:{p}/live/stream", "rtmp://127.0.0.1:{p}/live/stream"),
    ("RIST", "rist://@127.0.0.1:{p}", "rist://127.0.0.1:{p}"),
])
def test_pusher_listens_receiver_pulls(tools, tmp_path, protocol, listen, pull):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, protocol.lower())
    p = port()
    out = tmp_path / "rec.ts"
    result = {}
    sender = threading.Thread(target=lambda: result.update(
        logs=push_listen(ffmpeg, clip, protocol, listen.format(p=p), 11)))
    sender.start()
    time.sleep(1)
    w, ev = start_receiver(ffmpeg, rx_settings(protocol=protocol, connection="pull",
                                               endpoint=pull.format(p=p), output=str(out)))
    sender.join()
    time.sleep(2)
    logs, stop_time = stop_receiver(w, ev)
    assert any("Stream connected." in l for l in logs)
    assert received_seconds(ffprobe, out) >= 5
    if protocol in ("SRT", "RTMP"):
        assert any("Receiver connected." in l for l in result["logs"])
    assert stop_time < 10


def test_pull_retries_quietly_until_the_sender_appears(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "srt")
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="SRT", connection="pull",
                                               endpoint=f"srt://127.0.0.1:{p}?mode=caller", output=str(out)))
    time.sleep(8)                       # sender not running yet: a few failed attempts
    push_listen(ffmpeg, clip, "SRT", f"srt://127.0.0.1:{p}?mode=listener", 9)
    time.sleep(2)
    logs, _ = stop_receiver(w, ev)
    assert sum("Couldn't connect to the sender" in l for l in logs) == 1   # said once, not every retry
    assert sum(l.startswith("FFmpeg: ") for l in logs) <= 2                # command not repeated each retry
    assert any("Stream connected." in l for l in logs)
    assert received_seconds(ffprobe, out) >= 4


@pytest.mark.parametrize("protocol,listen,pull", [
    ("SRT", "srt://127.0.0.1:{p}?mode=listener", "srt://127.0.0.1:{p}?mode=caller"),
    ("RTMP", "rtmp://127.0.0.1:{p}/live/stream", "rtmp://127.0.0.1:{p}/live/stream"),
])
def test_folder_playlist_listen_mode(tools, tmp_path, protocol, listen, pull):
    """Folder mode, listen: the playlist keeps running while nobody is connected; a receiver
    that pulls gets the live stream; when it leaves, the pusher listens again for the next one."""
    ffmpeg, ffprobe, _ = tools
    need(ffmpeg, protocol.lower())
    folder = tmp_path / "pl"
    folder.mkdir()
    make_real_clip(ffmpeg, folder / "a.mp4", seconds=3)
    make_real_clip(ffmpeg, folder / "b.mp4", seconds=3, pattern="testsrc")
    from test_playlist import age_files
    age_files(folder)
    p = port()
    s = dict(mode="folder", protocol=protocol, source=str(folder), endpoint=listen.format(p=p),
             bandwidth_k=0, reencode=False, reconnect=True, reconnect_delay=1, settle_s=2,
             output_format=vp.FORMAT_AUTO, connection="listen")
    pev = queue.Queue()
    pusher = vp.PlaylistWorker(s, ffmpeg, ffprobe, pev)
    pusher.start()
    plogs = []

    def drain():
        while not pev.empty():
            kind, payload = pev.get()
            if kind == "log":
                plogs.append(payload)

    time.sleep(8)                        # no receiver yet: the playlist must keep going
    drain()
    assert sum("Now playing" in l for l in plogs) >= 2
    assert not any("Receiver connected." in l for l in plogs)

    for n in (1, 2):                     # two receivers, one after the other
        out = tmp_path / f"rec{n}.ts"
        w, ev = start_receiver(ffmpeg, rx_settings(protocol=protocol, connection="pull",
                                                   endpoint=pull.format(p=p), output=str(out)))
        time.sleep(7)
        stop_receiver(w, ev)
        time.sleep(3)                    # pusher notices and listens again
        assert received_seconds(ffprobe, out) >= 3, f"receiver {n} got too little"
    pusher.stop()
    pusher.join(15)
    drain()
    assert sum("Receiver connected." in l for l in plogs) == 2
    assert any("Receiver disconnected." in l for l in plogs)
    assert any("Listening again" in l for l in plogs)
