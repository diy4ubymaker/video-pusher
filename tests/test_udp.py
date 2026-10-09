"""UDP / multicast: Video Pusher sends MPEG-TS over UDP, Video Receiver joins the group."""
import queue
import socket
import struct
import threading
import time

import pytest

import main as vp
import receiver as rx
from test_receiver import need, rx_settings, stop_receiver, tools  # noqa: F401
from test_rist import received_seconds
from test_settings import needs_gui


def group():
    """A random multicast group/port, so test runs don't hear each other."""
    import random
    return f"239.{random.randrange(1, 250)}.{random.randrange(1, 250)}.{random.randrange(1, 250)}", \
        random.randrange(20000, 30000)


def pusher_settings(**kw):
    s = dict(mode="file", protocol="UDP", source="in.mp4", endpoint="udp://239.1.1.1:5000", bandwidth_k=0,
             reencode=False, reconnect=True, reconnect_delay=1, output_format=vp.FORMAT_AUTO,
             connection="push", udp_ttl=1, udp_localaddr="", test_signal=None)
    s.update(kw)
    return s


# ---------------- unit ----------------
def test_udp_output_args():
    s = pusher_settings(udp_ttl=8, udp_localaddr="192.168.1.10")
    assert vp.output_args(s) == ["-flush_packets", "0", "-f", "mpegts",
                                 "udp://239.1.1.1:5000?pkt_size=1316&ttl=8&localaddr=192.168.1.10"]
    s = pusher_settings(endpoint="udp://10.0.0.7:6000", bandwidth_k=3000, reencode=True)
    args = vp.output_args(s)
    assert args[:2] == ["-muxrate", "3449k"]                    # cap + TS overhead, constant rate
    assert "ttl=" not in args[-1] and "bitrate=3621450" in args[-1]   # unicast: no TTL; paced sending
    assert vp.output_args(pusher_settings(bandwidth_k=2500))[:2] == ["-muxrate", "2500k"]   # copy: as given


def test_udp_address_rules():
    assert vp.normalize_connection("UDP", False, "udp://239.1.1.1:5000")[2] is None
    assert vp.normalize_connection("UDP", False, "udp://239.1.1.1")[2]           # port needed
    assert vp.normalize_connection("UDP", True, "udp://239.1.1.1:5000")[2]       # nothing to listen for
    assert vp.is_multicast("239.1.1.1") and vp.is_multicast("224.0.1.1") and not vp.is_multicast("10.0.0.1")


def test_udp_input_url():
    url = rx.udp_input_url("udp://239.1.1.1:5000", "192.168.1.20", "192.168.1.5")
    assert url == ("udp://239.1.1.1:5000?fifo_size=50000&overrun_nonfatal=1&reuse=1"
                   "&localaddr=192.168.1.20&sources=192.168.1.5")
    assert rx.input_args(rx_settings(protocol="UDP")) == []    # the watchdog notices a silent sender


# ---------------- end to end ----------------
def send_rates(grp, port, seconds, out):
    """Received kbit/s in 100 ms slots (to check smooth, paced sending)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(grp),
                                                                           socket.inet_aton("0.0.0.0")))
    s.settimeout(1)
    bins, t0, end = {}, None, time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            d = s.recv(2048)
        except socket.timeout:
            continue
        t = time.monotonic()
        t0 = t0 or t
        out.setdefault("sizes", set()).add(len(d))
        b = int((t - t0) * 10)
        bins[b] = bins.get(b, 0) + len(d)
    s.close()
    out["kbps"] = [bins.get(i, 0) * 80 // 1000 for i in range(20, max(bins) - 5)] if bins else []


def run_pusher(ffmpeg, ffprobe, seconds, **kw):
    ev = queue.Queue()
    w = vp.PlaylistWorker(pusher_settings(**kw), ffmpeg, ffprobe, ev)
    w.start()
    time.sleep(seconds)
    w.stop()
    w.join(15)
    assert not w.is_alive()
    return [p for k, p in iter(lambda: ev.get() if not ev.empty() else (None, None), (None, None)) if k == "log"]


def test_constant_rate_and_paced(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "udp")
    grp, port = group()
    rates = {}
    t = threading.Thread(target=send_rates, args=(grp, port, 9, rates))
    t.start()
    time.sleep(0.3)
    run_pusher(ffmpeg, ffprobe, 9, source=clip, endpoint=f"udp://{grp}:{port}", bandwidth_k=4000)
    t.join()
    assert rates["sizes"] == {1316}                             # 7 TS packets per datagram
    kbps = rates["kbps"]
    assert kbps and 3500 < sum(kbps) / len(kbps) < 4600         # constant ~4000 kbit/s (padded)
    assert max(kbps) < 6000                                     # smooth: no big bursts


def test_multicast_pusher_to_receiver_and_sender_stop(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "udp")
    grp, port = group()
    out = tmp_path / "rec.ts"
    ev = queue.Queue()
    s = rx_settings(protocol="UDP", connection="listen", endpoint=rx.udp_input_url(f"udp://{grp}:{port}"),
                    output=str(out), sender_timeout=3)
    w = rx.ReceiverWorker(s, ffmpeg, "ffplay", ev)
    w.start()
    time.sleep(3)                                               # idle: nothing sent yet, keeps waiting
    run_pusher(ffmpeg, ffprobe, 10, source=clip, endpoint=f"udp://{grp}:{port}", reencode=True,
               output_format="720p50", preset="ultrafast", crf=28, audio_k=96)
    seen, end = [], time.monotonic() + 25                       # the receiver notices the silence
    while time.monotonic() < end and not any("Stream ended" in l for l in seen):
        try:
            kind, payload = ev.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "log":
            seen.append(payload)
    time.sleep(2)
    logs, _ = stop_receiver(w, ev)
    logs = seen + logs
    assert sum("Stream connected." in l for l in logs) == 1
    assert any("No data from the sender for 3 s - closing the connection." in l for l in logs)
    assert any("Stream ended (no data from the sender for 3 s). Listening again" in l for l in logs)
    assert received_seconds(ffprobe, out) >= 6


@needs_gui
def test_windows_udp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PUSHER_CONFIG_DIR", str(tmp_path / "cfg"))
    app = vp.VideoPusherApp()
    try:
        app.var_connection.set("listen")
        app.var_protocol.set("UDP"); app._on_protocol_change()
        assert app.var_connection.get() == "push"                  # UDP only sends
        assert str(app.rb_connection["listen"]["state"]) == "disabled"
        assert app.var_endpoint.get() == "udp://239.1.1.1:5000"
        assert app.lbl_endpoint["text"] == "UDP destination:"
        app.var_udp_ttl.set("4"); app.var_udp_localaddr.set("10.1.1.2")
        app.on_closing()
    except Exception:
        app.destroy()
        raise
    app = vp.VideoPusherApp()
    try:
        assert app.var_protocol.get() == "UDP" and app.var_udp_ttl.get() == "4"
        assert app.var_udp_localaddr.get() == "10.1.1.2"
    finally:
        app.destroy()
    r = rx.ReceiverApp()
    try:
        r.var_connection.set("pull")
        r.var_protocol.set("UDP"); r._on_protocol_change()
        assert r.var_connection.get() == "listen" and str(r.rb_connection["pull"]["state"]) == "disabled"
        assert r.var_endpoint.get() == "udp://239.1.1.1:5000"
        r.ffmpeg_path = r.ffplay_path = __import__("shutil").which("ffmpeg")
        r.var_mode.set("save_only"); r.var_output.set(str(tmp_path / "x.ts"))
        r.var_udp_source.set("10.0.0.9")
        s = r._read_settings()
        assert s["endpoint"].endswith("&sources=10.0.0.9") and "fifo_size=" in s["endpoint"]
        assert r.var_endpoint.get() == "udp://239.1.1.1:5000"      # options aren't written into the field
    finally:
        r.destroy()
