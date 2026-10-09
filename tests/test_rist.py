"""RIST tests.

Unit tests check the commands and helpers. The end-to-end tests send through a real local RIST
receiver (an FFmpeg listener writing to a file) and are skipped when FFmpeg has no librist."""
import json
import os
import queue
import random
import subprocess
import time

import pytest

import main as vp
from test_playlist import age_files, check_timeline, played_names


def rist_settings(**kw):
    base = dict(mode="folder", protocol="RIST", source="in.mp4", endpoint="rist://127.0.0.1:5000",
                bandwidth_k=0, reencode=False, reconnect=True, reconnect_delay=1, settle_s=2,
                output_format=vp.FORMAT_AUTO, test_signal=None, rist_profile="main",
                rist_buffer_ms=1000, rist_encryption=None, rist_secret="")
    base.update(kw)
    return base


# ---------------- unit ----------------
def test_rist_output_args_plain():
    args = vp.output_args(rist_settings(bandwidth_k=4000))
    assert args == ["-rist_profile", "main", "-buffer_size", "1000", "-f", "mpegts", "rist://127.0.0.1:5000"]


def test_rist_output_args_encrypted_and_redacted():
    s = rist_settings(rist_encryption="256", rist_secret="TopSecret99", rist_profile="main")
    args = vp.output_args(s)
    assert args[args.index("-secret") + 1] == "TopSecret99"
    assert args[args.index("-encryption") + 1] == "256"
    logged = vp.redact(["ffmpeg"] + args)
    assert "TopSecret99" not in " ".join(logged) and "****" in logged


def test_redact_hides_secret_in_url():
    assert vp.redact(["rist://h:5000?secret=abc&buffer=100"]) == ["rist://h:5000?secret=****&buffer=100"]


def test_connection_address_rules():
    n = vp.normalize_connection
    assert n("RIST", True, "rist://0.0.0.0:5000")[0] == "rist://@0.0.0.0:5000"      # listen adds '@'
    assert n("RIST", False, "rist://@10.0.0.5:5000")[0] == "rist://10.0.0.5:5000"  # connect drops it
    assert n("RIST", False, "rist://10.0.0.5")[2]                                  # port required
    assert any(x.startswith("Warning") for x in n("RIST", False, "rist://10.0.0.5:5001")[1])  # odd port
    assert n("RIST", True, "rist://@0.0.0.0:5000") == ("rist://@0.0.0.0:5000", [], None)
    assert n("SRT", True, "srt://0.0.0.0:9000?mode=caller&latency=200")[0] == \
        "srt://0.0.0.0:9000?latency=200&mode=listener"
    assert n("SRT", False, "srt://10.0.0.5:9000")[0] == "srt://10.0.0.5:9000?mode=caller"
    assert n("RTMP", True, "rtmps://0.0.0.0/live/s")[2]                            # can't listen for TLS
    assert n("RTMP", False, "rtmps://a.rtmp.youtube.com/live2/key")[2] is None


def test_rtmp_listen_output_args():
    s = rist_settings(protocol="RTMP", endpoint="rtmp://0.0.0.0:1935/live/s", connection="listen")
    assert vp.output_args(s) == ["-f", "flv", "-flvflags", "no_duration_filesize", "-listen", "1",
                                 "-rw_timeout", "10000000", "rtmp://0.0.0.0:1935/live/s"]
    s["connection"] = "push"
    assert "-listen" not in vp.output_args(s)


def test_constant_rate_and_param_merge():
    v = vp.constant_rate(["-c:v", "libx264", "-preset", "veryfast", "-b:v", "4000k", "-maxrate", "4000k",
                          "-bufsize", "4000k"], 2000)
    assert v.count("-b:v") == 1 and v[v.index("-minrate") + 1] == "2000k"
    cmd = vp.merge_x264_params(["ffmpeg"] + v + ["-x264-params", "tff=1"])
    assert cmd.count("-x264-params") == 1
    assert cmd[cmd.index("-x264-params") + 1] == "nal-hrd=cbr:filler=1:tff=1"


def test_output_protocol_parsing(monkeypatch):
    class R:
        stdout = "Supported file protocols:\nInput:\n  file\n  rist\nOutput:\n  file\n  srt\n  rtmp\n"
    monkeypatch.setattr(vp, "run_quiet", lambda cmd, timeout: R())
    assert vp.ffmpeg_output_protocols("ffmpeg") == {"file", "srt", "rtmp"}


# ---------------- end to end through a real RIST receiver ----------------
@pytest.fixture(scope="module")
def rist_ffmpeg(ffmpeg_tools):
    ffmpeg, ffprobe = ffmpeg_tools
    if "rist" not in vp.ffmpeg_output_protocols(ffmpeg):
        pytest.skip("this FFmpeg build has no librist")
    return ffmpeg, ffprobe


def free_even_port():
    return random.randrange(20000, 30000) // 2 * 2


class Receiver:
    """FFmpeg RIST listener that writes what it receives to a file."""

    def __init__(self, ffmpeg, port, out, extra=()):
        self.out = out
        self.proc = subprocess.Popen([ffmpeg, "-hide_banner", "-v", "error", "-rist_profile", "main", *extra,
                                      "-i", f"rist://@127.0.0.1:{port}", "-c", "copy", "-f", "mpegts",
                                      "-y", str(out)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

    def stop(self):
        time.sleep(2)
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def received_seconds(ffprobe, path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0.0
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def make_real_clip(ffmpeg, path, seconds=3, pattern="testsrc2"):
    """Clip at a realistic, steady bitrate (like real programme content)."""
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"{pattern}=s=1280x720:r=25:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=d={seconds}", "-c:v", "libx264", "-preset", "veryfast",
                    "-b:v", "2M", "-minrate", "2M", "-maxrate", "2M", "-bufsize", "2M",
                    "-x264-params", "nal-hrd=cbr:filler=1", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", str(path)], check=True)


def run_worker(worker, events, seconds, on_log=None):
    logs = []
    worker.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            kind, payload = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if kind == "log":
            logs.append(payload)
            if on_log:
                on_log(payload)
    worker.stop()
    worker.join(15)
    assert not worker.is_alive()
    return logs


def test_rist_playlist_end_to_end(rist_ffmpeg, tmp_path):
    ffmpeg, ffprobe = rist_ffmpeg
    folder = tmp_path / "clips"
    folder.mkdir()
    make_real_clip(ffmpeg, folder / "a.mp4")
    make_real_clip(ffmpeg, folder / "b.mp4", pattern="testsrc")
    age_files(folder)
    port = free_even_port()
    rx = Receiver(ffmpeg, port, tmp_path / "rx.ts")
    events = queue.Queue()
    s = rist_settings(source=str(folder), endpoint=f"rist://127.0.0.1:{port}")
    logs = run_worker(vp.PlaylistWorker(s, ffmpeg, ffprobe, events), events, 11)
    rx.stop()
    assert played_names(logs)[:3] == ["a.mp4", "b.mp4", "a.mp4"]
    assert received_seconds(ffprobe, tmp_path / "rx.ts") >= 7
    check_timeline(ffprobe, tmp_path / "rx.ts")


def test_rist_black_test_signal_gets_through(rist_ffmpeg, tmp_path):
    """A black test signal is tiny; over RIST it is sent at constant bitrate so receivers keep it."""
    ffmpeg, ffprobe = rist_ffmpeg
    folder = tmp_path / "empty"
    folder.mkdir()
    port = free_even_port()
    rx = Receiver(ffmpeg, port, tmp_path / "rx.ts")
    events = queue.Queue()
    s = rist_settings(source=str(folder), endpoint=f"rist://127.0.0.1:{port}", test_signal="black",
                      reencode=True)

    def drop_file_in(line):
        if "Test signal: " in line:
            assert "nal-hrd=cbr:filler=1" in line
    logs = run_worker(vp.PlaylistWorker(s, ffmpeg, ffprobe, events), events, 10, drop_file_in)
    rx.stop()
    assert any("No playable file - sending test signal" in l for l in logs)
    assert received_seconds(ffprobe, tmp_path / "rx.ts") >= 6
    check_timeline(ffprobe, tmp_path / "rx.ts")


def test_rist_encrypted_single_file(rist_ffmpeg, tmp_path):
    ffmpeg, ffprobe = rist_ffmpeg
    make_real_clip(ffmpeg, tmp_path / "one.mp4", seconds=4)
    port = free_even_port()
    rx = Receiver(ffmpeg, port, tmp_path / "rx.ts", extra=("-secret", "Sh4redSecret!", "-encryption", "256"))
    s = rist_settings(mode="file", source=str(tmp_path / "one.mp4"), endpoint=f"rist://127.0.0.1:{port}",
                      rist_encryption="256", rist_secret="Sh4redSecret!")
    events = queue.Queue()
    worker = vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), True, 1, events)
    logs = run_worker(worker, events, 8)
    rx.stop()
    assert received_seconds(ffprobe, tmp_path / "rx.ts") >= 4
    assert not any("Sh4redSecret!" in l for l in logs)        # passphrase never logged
    assert any("-secret ****" in l for l in logs)


def test_rist_wrong_passphrase_receives_nothing(rist_ffmpeg, tmp_path):
    ffmpeg, ffprobe = rist_ffmpeg
    make_real_clip(ffmpeg, tmp_path / "one.mp4", seconds=4)
    port = free_even_port()
    rx = Receiver(ffmpeg, port, tmp_path / "rx.ts", extra=("-secret", "RightSecret1", "-encryption", "128"))
    s = rist_settings(mode="file", source=str(tmp_path / "one.mp4"), endpoint=f"rist://127.0.0.1:{port}",
                      rist_encryption="128", rist_secret="WrongSecret2")
    events = queue.Queue()
    run_worker(vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), True, 1, events), events, 6)
    rx.stop()
    assert received_seconds(ffprobe, tmp_path / "rx.ts") == 0
