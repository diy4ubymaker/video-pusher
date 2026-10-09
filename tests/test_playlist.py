"""End-to-end playlist tests: real FFmpeg, output written to a local .ts file instead of SRT/RTMP.
Skipped automatically when ffmpeg/ffprobe are not available."""
import json
import os
import queue
import subprocess
import threading
import time

import pytest

import main as vp


def make_clip(ffmpeg, path, size="640x360", rate=25, seconds=3, audio=True, pattern="testsrc"):
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"{pattern}=s={size}:r={rate}:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=d={seconds}", "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


@pytest.fixture(scope="module")
def folder(tmp_path_factory, ffmpeg_tools):
    ffmpeg, _ = ffmpeg_tools
    d = tmp_path_factory.mktemp("clips")
    make_clip(ffmpeg, d / "a_good.mp4", seconds=4)
    make_clip(ffmpeg, d / "b_good.mp4", pattern="testsrc2")
    (d / "c_corrupt.mp4").write_bytes(b"\x00garbage" * 20000)
    make_clip(ffmpeg, d / "d_720p.mp4", size="1280x720", rate=30)
    make_clip(ffmpeg, d / "e_noaudio.mp4", audio=False, pattern="testsrc2")
    age_files(d)
    return d


def age_files(folder, seconds=3600):
    """Back-date files so they count as 'already settled' when the playlist starts."""
    t = time.time() - seconds
    for f in os.listdir(folder):
        os.utime(os.path.join(folder, f), (t, t))


def run_playlist(ffmpeg_tools, folder, out_file, reencode, seconds, monkeypatch, settle_s=10):
    ffmpeg, ffprobe = ffmpeg_tools
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out_file)])
    s = dict(mode="folder", protocol="SRT", source=str(folder), endpoint="srt://unused",
             bandwidth_k=0, reencode=reencode, reconnect=True, reconnect_delay=1, settle_s=settle_s)
    events = queue.Queue()
    worker = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    worker.start()
    time.sleep(seconds)
    worker.stop()
    worker.join(15)
    assert not worker.is_alive(), "worker did not stop"
    logs = []
    while not events.empty():
        kind, payload = events.get()
        if kind == "log":
            logs.append(payload)
    return logs


def check_timeline(ffprobe, out_file):
    """Timestamps must keep moving forward with no gaps across file joins."""
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "packet=stream_index,dts_time",
                        "-of", "json", str(out_file)], capture_output=True, text=True, check=True)
    last, backwards, gaps = {}, 0, []
    for p in json.loads(r.stdout)["packets"]:
        if "dts_time" not in p:
            continue
        s, t = p["stream_index"], float(p["dts_time"])
        if s in last:
            backwards += t < last[s]
            if t - last[s] > 0.3:
                gaps.append(round(t - last[s], 2))
        last[s] = t
    assert backwards == 0
    assert gaps == []


def played(logs):
    return [l.split("Now playing ")[1].split(":")[1].split(" (")[0].strip()
            for l in logs if "Now playing" in l]


def test_copy_mode_skips_bad_and_mismatched_files(ffmpeg_tools, folder, tmp_path, monkeypatch):
    out = tmp_path / "copy.ts"
    logs = run_playlist(ffmpeg_tools, folder, out, reencode=False, seconds=12, monkeypatch=monkeypatch)
    names = played(logs)
    assert names[:3] == ["a_good.mp4", "b_good.mp4", "a_good.mp4"]      # looped
    assert set(names) == {"a_good.mp4", "b_good.mp4"}
    skipped = " ".join(l for l in logs if "Skipping" in l)
    for name in ("c_corrupt.mp4", "d_720p.mp4", "e_noaudio.mp4"):
        assert name in skipped
    check_timeline(ffmpeg_tools[1], out)


def test_reencode_mode_mixes_formats(ffmpeg_tools, folder, tmp_path, monkeypatch):
    out = tmp_path / "reencode.ts"
    logs = run_playlist(ffmpeg_tools, folder, out, reencode=True, seconds=16, monkeypatch=monkeypatch)
    names = played(logs)
    assert names[:4] == ["a_good.mp4", "b_good.mp4", "d_720p.mp4", "e_noaudio.mp4"]
    assert "c_corrupt.mp4" not in names
    assert any("Skipping c_corrupt.mp4" in l for l in logs)
    check_timeline(ffmpeg_tools[1], out)


def test_file_still_being_copied_is_not_used_until_stable(ffmpeg_tools, tmp_path, monkeypatch):
    """A file copied into the folder slowly must not be probed or played until its size has
    been stable for settle_s; after that it joins the playlist."""
    ffmpeg, ffprobe = ffmpeg_tools
    folder = tmp_path / "live"
    staging = tmp_path / "staging"
    folder.mkdir()
    staging.mkdir()
    make_clip(ffmpeg, folder / "a_first.mp4", seconds=3)
    age_files(folder)
    make_clip(ffmpeg, staging / "clip.mp4", seconds=3, pattern="testsrc2")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(staging / "clip.mp4"), "-c", "copy",
                    str(staging / "b_new.ts")], check=True)
    data = (staging / "b_new.ts").read_bytes()

    settle_s = 3
    copy_done = {}

    def slow_copy():
        time.sleep(0.5)
        target = folder / "b_new.ts"
        step = len(data) // 8 + 1
        with open(target, "wb") as f:
            for i in range(0, len(data), step):
                f.write(data[i:i + step])
                f.flush()
                time.sleep(0.5)
        copy_done["at"] = time.monotonic()

    out = tmp_path / "out.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="folder", protocol="SRT", source=str(folder), endpoint="srt://unused",
             bandwidth_k=0, reencode=False, reconnect=True, reconnect_delay=1, settle_s=settle_s)
    events = queue.Queue()
    worker = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    probed = []
    real_probe = vp.probe_media
    monkeypatch.setattr(vp, "probe_media",
                        lambda fp, fm, path: probed.append((os.path.basename(path), time.monotonic()))
                        or real_probe(fp, fm, path))

    copier = threading.Thread(target=slow_copy)
    worker.start()
    copier.start()
    played_at, logs = [], []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            kind, payload = events.get(timeout=0.1)
        except queue.Empty:
            continue
        if kind == "log":
            logs.append(payload)
            if "Now playing" in payload:
                played_at.append((payload, time.monotonic()))
    worker.stop()
    worker.join(15)
    copier.join()
    assert not worker.is_alive()

    assert any("Not ready yet: b_new.ts" in l for l in logs)
    new_plays = [t for l, t in played_at if "b_new.ts" in l]
    assert new_plays, "new file never joined the playlist"
    assert min(new_plays) >= copy_done["at"] + settle_s - 0.5
    new_probes = [t for name, t in probed if name == "b_new.ts"]
    assert new_probes and min(new_probes) >= copy_done["at"] + settle_s - 0.5   # never probed mid-copy
    assert not any("Skipping b_new.ts" in l for l in logs)


def _run_and_delete_next(ffmpeg_tools, tmp_path, monkeypatch, patch_validity):
    """Play a, b, c in a loop; on the second pass, delete b while a is playing (after b has been
    chosen as next). The stream must continue with c with no pause in the timeline."""
    ffmpeg, ffprobe = ffmpeg_tools
    folder = tmp_path / "pl"
    folder.mkdir()
    make_clip(ffmpeg, folder / "a.mp4", seconds=3)
    make_clip(ffmpeg, folder / "b.mp4", seconds=3, pattern="testsrc2")
    make_clip(ffmpeg, folder / "c.mp4", seconds=3)
    age_files(folder)
    if patch_validity:
        # pretend the deletion happened after the last-moment check, so FFmpeg fails to open b
        monkeypatch.setattr(vp.PlaylistWorker, "_still_valid", lambda self, info: True)

    out = tmp_path / "out.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="folder", protocol="SRT", source=str(folder), endpoint="srt://unused",
             bandwidth_k=0, reencode=False, reconnect=True, reconnect_delay=1, settle_s=2)
    events = queue.Queue()
    worker = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    worker.start()
    logs, a_plays, deleted = [], 0, False
    deadline = time.monotonic() + 18
    delete_at = None
    while time.monotonic() < deadline:
        if delete_at and not deleted and time.monotonic() >= delete_at:
            os.remove(folder / "b.mp4")
            deleted = True
        try:
            kind, payload = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if kind == "log":
            logs.append(payload)
            if "Now playing" in payload and ": a.mp4" in payload:
                a_plays += 1
                if a_plays == 2:
                    delete_at = time.monotonic() + 1.5   # look-ahead has chosen b by then
    worker.stop()
    worker.join(15)
    assert not worker.is_alive()
    assert deleted
    return logs, out


def played_names(logs):
    return [l.split("Now playing ")[1].split(": ")[1].split(" (")[0] for l in logs if "Now playing" in l]


def test_deleted_next_file_switches_without_pause(ffmpeg_tools, tmp_path, monkeypatch):
    logs, out = _run_and_delete_next(ffmpeg_tools, tmp_path, monkeypatch, patch_validity=False)
    names = played_names(logs)
    assert names[:5] == ["a.mp4", "b.mp4", "c.mp4", "a.mp4", "c.mp4"]
    assert any("b.mp4 was removed or changed before it could play - switching to c.mp4" in l for l in logs)
    check_timeline(ffmpeg_tools[1], out)


def test_file_failing_at_open_switches_without_pause(ffmpeg_tools, tmp_path, monkeypatch):
    logs, out = _run_and_delete_next(ffmpeg_tools, tmp_path, monkeypatch, patch_validity=True)
    names = played_names(logs)
    i = [n for n in range(len(names)) if names[n] == "a.mp4"][1]
    assert "c.mp4" in names[i + 1:i + 3]          # b fails at once, c follows
    assert any("b.mp4 failed after 0 s" in l for l in logs)
    check_timeline(ffmpeg_tools[1], out)


# ---------------- test signal ----------------
def _drain(events, logs, seconds, on_log=None):
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


def _start(ffmpeg_tools, folder, out, monkeypatch, **extra):
    ffmpeg, ffprobe = ffmpeg_tools
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="folder", protocol="SRT", source=str(folder), endpoint="srt://unused", bandwidth_k=0,
             reencode=False, reconnect=True, reconnect_delay=1, settle_s=2,
             output_format=vp.FORMAT_AUTO, test_signal="bars")
    s.update(extra)
    events = queue.Queue()
    worker = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    worker.start()
    return worker, events


def _stop(worker):
    worker.stop()
    worker.join(15)
    assert not worker.is_alive()


def stream_info(ffprobe, out_file):
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v", "-show_entries",
                        "stream=width,height,field_order,avg_frame_rate", "-of", "json", str(out_file)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0]


def test_empty_folder_sends_test_signal_then_plays_new_file(ffmpeg_tools, tmp_path, monkeypatch):
    """Re-encode to 1080i50 from an empty folder: test signal first, then a file dropped in takes
    over, all in one continuous interlaced stream."""
    ffmpeg, ffprobe = ffmpeg_tools
    folder = tmp_path / "empty"
    folder.mkdir()
    out = tmp_path / "out.ts"
    worker, events = _start(ffmpeg_tools, folder, out, monkeypatch, reencode=True, output_format="1080i50")
    logs = []
    _drain(events, logs, 6)
    make_clip(ffmpeg, folder / "late.mp4", seconds=3)
    _drain(events, logs, 14)
    _stop(worker)
    assert any("No playable file - sending test signal" in l for l in logs)
    assert any("Test signal: " in l and "smptehdbars=s=1920x1080:r=50" in l for l in logs)
    assert any("Playable file ready - ending test signal" in l for l in logs)
    assert "late.mp4" in played_names(logs)
    info = stream_info(ffprobe, out)
    assert (info["width"], info["height"], info["field_order"], info["avg_frame_rate"]) == (1920, 1080, "tt", "25/1")
    check_timeline(ffprobe, out)


def test_copy_mode_test_signal_matches_files_after_deletion(ffmpeg_tools, tmp_path, monkeypatch):
    """Copy mode: once the only file is deleted, the test signal uses the file's format."""
    ffmpeg, ffprobe = ffmpeg_tools
    folder = tmp_path / "one"
    folder.mkdir()
    make_clip(ffmpeg, folder / "only.mp4", seconds=3)
    age_files(folder)
    out = tmp_path / "out.ts"
    worker, events = _start(ffmpeg_tools, folder, out, monkeypatch, test_signal="black")
    logs = []

    def on_log(line):
        if "Now playing" in line and (folder / "only.mp4").exists():
            os.remove(folder / "only.mp4")
    _drain(events, logs, 12, on_log)
    _stop(worker)
    slate_cmds = [l for l in logs if "Test signal: " in l]
    assert slate_cmds and "color=c=black:s=640x360:r=25" in slate_cmds[0]
    assert "anullsrc=r=48000:cl=stereo" in slate_cmds[0]
    info = stream_info(ffprobe, out)
    assert (info["width"], info["height"]) == (640, 360)
    check_timeline(ffprobe, out)


def test_test_signal_falls_back_when_text_fails(ffmpeg_tools, tmp_path, monkeypatch):
    """If drawing text fails (simulated with an invalid drawtext option), the caption image is used."""
    folder = tmp_path / "none"
    folder.mkdir()
    out = tmp_path / "out.ts"
    monkeypatch.setattr(vp, "SLATE_FONT", "DejaVuSansMono-Bold.ttf:no_such_option=1")
    monkeypatch.setattr(vp, "slate_levels", lambda caps, assets_dir=None: ["text", "image", "plain"])
    worker, events = _start(ffmpeg_tools, folder, out, monkeypatch)
    logs = []
    _drain(events, logs, 10)
    _stop(worker)
    assert any("'text' style failed - switching to caption image" in l for l in logs)
    assert any("Test signal: " in l and "no_transmission.png" in l for l in logs)
    check_timeline(ffmpeg_tools[1], out)


def test_reencode_with_overlay_and_quality_settings(ffmpeg_tools, folder, tmp_path, monkeypatch):
    """Folder mode, re-encoding to 1080i50 with the date/time overlay and custom quality settings:
    every file plays, the output is interlaced, and the timeline stays continuous."""
    ffmpeg, ffprobe = ffmpeg_tools
    out = tmp_path / "ov.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="folder", protocol="SRT", source=str(folder), endpoint="srt://unused", bandwidth_k=0,
             reencode=True, reconnect=True, reconnect_delay=1, settle_s=2, output_format="1080i50",
             preset="ultrafast", crf=26, audio_k=96,
             overlay=dict(format="14:30:00", position="Bottom-right", x=0, y=0, size=6, box=True))
    events = queue.Queue()
    worker = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    worker.start()
    time.sleep(12)
    worker.stop()
    worker.join(15)
    logs = []
    while not events.empty():
        kind, payload = events.get()
        if kind == "log":
            logs.append(payload)
    assert played(logs)[:3] == ["a_good.mp4", "b_good.mp4", "d_720p.mp4"]
    assert not any("failed after" in l for l in logs)
    assert any("drawtext" in l and "Feeder:" in l for l in logs)
    info = stream_info(ffprobe, out)
    assert (info["width"], info["height"], info["field_order"]) == (1920, 1080, "tt")
    check_timeline(ffprobe, out)


def test_single_file_overlay_end_to_end(ffmpeg_tools, folder, tmp_path, monkeypatch):
    """Single-file mode with the overlay runs FFmpeg from the assets folder (bundled font)."""
    ffmpeg, ffprobe = ffmpeg_tools
    out = tmp_path / "single.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="file", protocol="SRT", source=str(folder / "a_good.mp4"), endpoint="srt://unused",
             bandwidth_k=0, reencode=True, output_format=vp.FORMAT_AUTO, preset="veryfast", crf=23, audio_k=128,
             overlay=dict(format="2026-10-08 14:30:00", position="Top-left", x=0, y=0, size=5, box=True))
    events = queue.Queue()
    w = vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), False, 1, events, cwd=vp.ASSETS_DIR)
    w.start()
    time.sleep(5)
    w.stop()
    w.join(15)
    assert not w.is_alive()
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(out)],
                       capture_output=True, text=True)
    assert float(json.loads(r.stdout)["format"]["duration"]) >= 3


def make_logo(ffmpeg, path):
    """Small two-colour logo with a transparent background."""
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0xFF8800:s=200x100",
                    "-f", "lavfi", "-i", "color=c=black:s=200x100", "-filter_complex",
                    "[1]drawbox=x=0:y=0:w=120:h=100:color=white:t=fill,format=gray[a];[0][a]alphamerge,format=rgba",
                    "-frames:v", "1", str(path)], check=True)


def test_folder_reencode_with_logo_and_text(ffmpeg_tools, folder, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    logo_path = tmp_path / "logo.png"
    make_logo(ffmpeg, logo_path)
    out = tmp_path / "logo.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="folder", protocol="SRT", source=str(folder), endpoint="srt://unused", bandwidth_k=0,
             reencode=True, reconnect=True, reconnect_delay=1, settle_s=2, output_format="720p50",
             overlay=dict(format="14:30:00", position="Bottom-left", x=0, y=0, size=5, box=True, colour="#FFFF00"),
             logo=dict(path=str(logo_path), position="Top-right", x=0, y=0, size=15, opacity=80))
    events = queue.Queue()
    worker = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    worker.start()
    time.sleep(13)
    worker.stop()
    worker.join(15)
    logs = []
    while not events.empty():
        kind, payload = events.get()
        if kind == "log":
            logs.append(payload)
    assert played(logs)[:3] == ["a_good.mp4", "b_good.mp4", "d_720p.mp4"]
    assert not any("failed after" in l for l in logs)
    # a still logo: shown for the whole file (looped when live overlay commands are used)
    assert any("Feeder:" in l and ("eof_action=repeat" in l or "-loop 1" in l) for l in logs)
    info = stream_info(ffprobe, out)
    assert (info["width"], info["height"]) == (1280, 720)
    check_timeline(ffprobe, out)
    # the logo is really there: top-right corner differs from the same frame without a logo
    frame = subprocess.run([ffmpeg, "-v", "error", "-i", str(out), "-vf", "select='gte(n\\,30)',crop=192:96:1060:22",
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    orange = sum(1 for i in range(0, len(frame) - 2, 3) if frame[i] > 180 and 80 < frame[i + 1] < 170 and frame[i + 2] < 90)
    assert orange > 2000


def test_single_file_with_logo_no_audio_source(ffmpeg_tools, folder, tmp_path, monkeypatch):
    """Logo in single-file mode, Auto size, on a file without audio (the 0:a:0? map is optional)."""
    ffmpeg, ffprobe = ffmpeg_tools
    logo_path = tmp_path / "logo.png"
    make_logo(ffmpeg, logo_path)
    out = tmp_path / "single_logo.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    src = folder / "e_noaudio.mp4"
    s = dict(mode="file", protocol="SRT", source=str(src), endpoint="srt://unused", bandwidth_k=0, reencode=True,
             output_format=vp.FORMAT_AUTO, src_width=vp.image_width(ffprobe, str(src)),
             logo=dict(path=str(logo_path), position="Centre", x=0, y=0, size=30, opacity=100))
    events = queue.Queue()
    w = vp.SingleFileWorker(vp.build_single_file_cmd(ffmpeg, s), False, 1, events)
    w.start()
    time.sleep(5)
    w.stop()
    w.join(15)
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(out)],
                       capture_output=True, text=True)
    assert float(json.loads(r.stdout)["format"]["duration"]) >= 3
