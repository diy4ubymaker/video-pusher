"""SRT encryption, RTMP stream keys and secret masking.

Unit tests check the commands and helpers; end-to-end tests send from Video Pusher to Video
Receiver through real FFmpeg (skipped when FFmpeg lacks SRT or RTMP). Every test also checks that
no passphrase or stream key shows up in the logs."""
import json
import queue
import threading
import time

import pytest

import main as vp
from conftest import _tool
import receiver as rx
from test_receiver import need, port, rx_settings, start_receiver, stop_receiver, tools  # noqa: F401
from test_rist import received_seconds
from test_settings import needs_gui

PASS = "Corr3ct-Horse!42"
OTHER = "Wr0ng-Battery?77"
KEY = "Kx9-live-7731"


def pusher_settings(**kw):
    base = dict(mode="file", protocol="SRT", source="in.mp4", endpoint="srt://127.0.0.1:9000?mode=caller",
                bandwidth_k=0, reencode=False, reconnect=True, reconnect_delay=1,
                output_format=vp.FORMAT_AUTO, connection="push")
    base.update(kw)
    return base


def run_pusher(ffmpeg, seconds, **kw):
    """Video Pusher, single file, for `seconds`; returns its log lines (and status/stats text)."""
    s = pusher_settings(**kw)
    ev = queue.Queue()
    listen = s["protocol"] if s["connection"] == "listen" else None
    w = vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), True, 1, ev, listen=listen,
                            secrets=vp.secret_values(s))
    w.start()
    time.sleep(seconds)
    w.stop()
    w.join(15)
    assert not w.is_alive()
    lines = []
    while not ev.empty():
        lines.append(str(ev.get()[1]))
    return lines


def assert_hidden(lines, *secrets):
    text = "\n".join(lines)
    for secret in secrets:
        assert secret not in text, f"secret {secret!r} leaked into the log"


# ---------------- unit ----------------
def test_srt_encryption_args():
    s = pusher_settings(srt_encryption="32", srt_passphrase=PASS)
    args = vp.output_args(s)
    assert args[:4] == ["-passphrase", PASS, "-pbkeylen", "32"] and args[-1].startswith("srt://")
    assert vp.output_args(pusher_settings(srt_encryption=None, srt_passphrase="")) == \
        ["-f", "mpegts", "srt://127.0.0.1:9000?mode=caller"]
    r = rx_settings(protocol="SRT", srt_encryption="16", srt_passphrase=PASS)
    assert rx.input_args(r)[:4] == ["-passphrase", PASS, "-pbkeylen", "16"]
    play, _ = rx.build_commands(dict(r, action="play_only"), "ffmpeg", "ffplay")
    assert play.index("-passphrase") < play.index("-i")


def test_redact_and_mask():
    cmd = ["ffmpeg", "-passphrase", PASS, "-pbkeylen", "24", "-secret", "abc123456789",
           "srt://h:1?passphrase=" + PASS, "rtmp://a.rtmp.youtube.com/live2/" + KEY]
    logged = " ".join(vp.redact(cmd))
    assert PASS not in logged and "abc123456789" not in logged and KEY not in logged
    assert "-passphrase ****" in logged and "live2/****" in logged
    assert vp.mask_secrets("[rtmp @ 0x1] Unexpected stream BAD, expecting " + KEY) == \
        "[rtmp @ 0x1] Unexpected stream ****, expecting ****"
    # known values are masked anywhere, also URL-encoded
    assert vp.mask_secrets("x " + vp.quote(PASS, safe="") + " y", [PASS]) == "x **** y"
    assert vp.mask_secrets("rtmp://0.0.0.0:1935/live") == "rtmp://0.0.0.0:1935/live"


def test_address_helpers():
    assert vp.split_rtmp_key("rtmp://a.rtmp.youtube.com/live2/" + KEY) == ("rtmp://a.rtmp.youtube.com/live2", KEY)
    assert vp.split_rtmp_key("rtmp://h:1935/live") == ("rtmp://h:1935/live", "")
    assert vp.rtmp_url("rtmp://h/live/", KEY) == "rtmp://h/live/" + KEY
    assert vp.rtmp_url("rtmp://h/live", "") == "rtmp://h/live"
    assert vp.strip_saved_secrets("srt://h:9000?mode=caller&passphrase=x1234567890&latency=200") == \
        "srt://h:9000?mode=caller&latency=200"
    assert vp.strip_saved_secrets("rtmp://h/live/" + KEY, rtmp_key=True) == "rtmp://h/live"
    assert vp.strip_saved_secrets("rtmp://h/live/" + KEY) == "rtmp://h/live/" + KEY
    assert vp.pop_url_param("srt://h:1?a=1&passphrase=p", "passphrase") == ("srt://h:1?a=1", "p")


# ---------------- end to end: SRT encryption ----------------
@pytest.mark.parametrize("pusher_listens", [False, True], ids=["push-listen", "listen-pull"])
def test_encrypted_srt_both_directions(tools, tmp_path, pusher_listens):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "srt")
    p = port()
    out = tmp_path / "rec.ts"
    enc = dict(srt_encryption="32", srt_passphrase=PASS)
    result = {}
    if pusher_listens:
        sender = threading.Thread(target=lambda: result.update(logs=run_pusher(
            ffmpeg, 10, source=clip, endpoint=f"srt://127.0.0.1:{p}?mode=listener", connection="listen", **enc)))
        sender.start()
        time.sleep(1)
        w, ev = start_receiver(ffmpeg, rx_settings(protocol="SRT", connection="pull", output=str(out),
                                                   endpoint=f"srt://127.0.0.1:{p}?mode=caller", **enc))
        sender.join()
    else:
        w, ev = start_receiver(ffmpeg, rx_settings(protocol="SRT", connection="listen", output=str(out),
                                                   endpoint=f"srt://127.0.0.1:{p}?mode=listener", **enc))
        result["logs"] = run_pusher(ffmpeg, 8, source=clip, endpoint=f"srt://127.0.0.1:{p}?mode=caller", **enc)
    time.sleep(2)
    rlogs, _ = stop_receiver(w, ev)
    assert any("Stream connected." in l for l in rlogs)
    assert received_seconds(ffprobe, out) >= 5
    assert any("-passphrase ****" in l for l in result["logs"])
    assert_hidden(result["logs"] + rlogs, PASS)


@pytest.mark.parametrize("sender", [dict(srt_encryption="32", srt_passphrase=OTHER),
                                    dict(srt_encryption=None, srt_passphrase="")],
                         ids=["wrong-passphrase", "no-passphrase"])
def test_srt_wrong_passphrase_receives_nothing(tools, tmp_path, sender):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "srt")
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="SRT", connection="listen", output=str(out),
                                               endpoint=f"srt://127.0.0.1:{p}?mode=listener",
                                               srt_encryption="32", srt_passphrase=PASS))
    plogs = run_pusher(ffmpeg, 6, source=clip, endpoint=f"srt://127.0.0.1:{p}?mode=caller", **sender)
    rlogs, _ = stop_receiver(w, ev)
    assert not any("Stream connected." in l for l in rlogs)
    assert received_seconds(ffprobe, out) == 0
    assert_hidden(plogs + rlogs, PASS, OTHER)


def test_srt_stream_id_is_sent_in_the_address(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "srt")
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="SRT", connection="listen", output=str(out),
                                               endpoint=f"srt://127.0.0.1:{p}?mode=listener"))
    endpoint = vp.set_url_param(f"srt://127.0.0.1:{p}?mode=caller", "streamid", "#!::r=live/cam1,m=publish")[0]
    plogs = run_pusher(ffmpeg, 6, source=clip, endpoint=endpoint)
    stop_receiver(w, ev)
    assert any("streamid=" in l for l in plogs)
    assert received_seconds(ffprobe, out) >= 3


# ---------------- end to end: RTMP stream key ----------------
def test_rtmp_receiver_rejects_wrong_stream_key(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "rtmp")
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="RTMP", connection="listen", output=str(out),
                                               endpoint=f"rtmp://127.0.0.1:{p}/live/{KEY}", rtmp_key=KEY))
    bad = run_pusher(ffmpeg, 5, protocol="RTMP", source=clip, endpoint=f"rtmp://127.0.0.1:{p}/live/Bad-Key-55",
                     rtmp_key="Bad-Key-55")
    time.sleep(2)
    assert received_seconds(ffprobe, out) == 0           # nothing recorded from the wrong sender
    good = run_pusher(ffmpeg, 7, protocol="RTMP", source=clip, endpoint=f"rtmp://127.0.0.1:{p}/live/{KEY}",
                      rtmp_key=KEY)
    time.sleep(2)
    rlogs, _ = stop_receiver(w, ev)
    assert any("Rejected a sender that used the wrong stream key." in l for l in rlogs)
    assert any("Stream connected." in l for l in rlogs)
    assert received_seconds(ffprobe, out) >= 4
    assert_hidden(bad + good + rlogs, KEY, "Bad-Key-55")


def test_rtmp_receiver_without_key_accepts_any(tools, tmp_path):
    ffmpeg, ffprobe, clip = tools
    need(ffmpeg, "rtmp")
    p = port()
    out = tmp_path / "rec.ts"
    w, ev = start_receiver(ffmpeg, rx_settings(protocol="RTMP", connection="listen", output=str(out),
                                               endpoint=f"rtmp://127.0.0.1:{p}/live", rtmp_key=""))
    plogs = run_pusher(ffmpeg, 6, protocol="RTMP", source=clip, endpoint=f"rtmp://127.0.0.1:{p}/live/{KEY}",
                       rtmp_key=KEY)
    time.sleep(2)
    rlogs, _ = stop_receiver(w, ev)
    assert received_seconds(ffprobe, out) >= 3
    assert_hidden(plogs + rlogs, KEY)


# ---------------- windows: fields, validation, nothing secret saved ----------------
@needs_gui
def test_pusher_secrets_are_moved_validated_and_never_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PUSHER_CONFIG_DIR", str(tmp_path / "cfg"))
    clip = tmp_path / "in.mp4"
    clip.write_bytes(b"x")
    app = vp.VideoPusherApp()
    try:
        app.ffmpeg_path = _tool("ffmpeg")
        app.var_source.set(str(clip))
        # SRT: passphrase typed into the address -> moved to the hidden field
        app.var_endpoint.set(f"srt://10.0.0.5:9000?mode=caller&passphrase={PASS}&pbkeylen=24")
        app.var_srt_streamid.set("cam1")
        s = app._read_settings()
        assert s["srt_passphrase"] == PASS and s["srt_encryption"] == "24" and app.var_srt_enc.get() == "AES-192"
        assert "passphrase" not in app.var_endpoint.get() and "streamid=cam1" in s["endpoint"]
        assert "streamid" not in app.var_endpoint.get()
        app.var_srt_pass.set("short")                       # 10-79 characters
        assert app._read_settings() is None
        app.var_srt_pass.set(PASS)
        # RTMP: key typed into the address -> moved to the Stream key field
        app.var_protocol.set("RTMP"); app._on_protocol_change()
        app.var_endpoint.set(f"rtmp://a.rtmp.youtube.com/live2/{KEY}")
        s = app._read_settings()
        assert s["endpoint"] == f"rtmp://a.rtmp.youtube.com/live2/{KEY}" and s["rtmp_key"] == KEY
        assert app.var_endpoint.get() == "rtmp://a.rtmp.youtube.com/live2" and app.var_rtmp_key.get() == KEY
        app._poll_events()
        log = app.txt_log.get("1.0", "end")
        assert "moved the stream key" in log and "moved the SRT passphrase" in log
        app.on_closing()
    except Exception:
        app.destroy()
        raise
    saved = (tmp_path / "cfg" / "pusher-settings.json").read_text(encoding="utf-8")
    assert PASS not in saved and KEY not in saved
    data = json.loads(saved)
    assert data["srt_encryption"] == "AES-192" and data["srt_streamid"] == "cam1"
    assert_hidden([log], PASS, KEY)
    app = vp.VideoPusherApp()
    try:
        assert app.var_srt_pass.get() == "" and app.var_rtmp_key.get() == ""
        assert app.var_srt_enc.get() == "AES-192"
    finally:
        app.destroy()


@needs_gui
def test_receiver_secrets_never_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PUSHER_CONFIG_DIR", str(tmp_path / "cfg"))
    app = rx.ReceiverApp()
    try:
        app.ffmpeg_path, app.ffplay_path = _tool("ffmpeg"), _tool("ffplay")
        app.var_endpoint.set(f"srt://0.0.0.0:9000?mode=listener&passphrase={PASS}")
        s = app._read_settings()
        assert s["srt_passphrase"] == PASS and s["srt_encryption"] == "16" and s["srt_streamid"] == ""
        app.var_protocol.set("RTMP"); app._on_protocol_change()
        app.var_endpoint.set(f"rtmp://0.0.0.0:1935/live/{KEY}")
        s = app._read_settings()
        assert s["rtmp_key"] == KEY and s["endpoint"].endswith("/" + KEY)
        app.on_closing()
    except Exception:
        app.destroy()
        raise
    saved = (tmp_path / "cfg" / "receiver-settings.json").read_text(encoding="utf-8")
    assert PASS not in saved and KEY not in saved
