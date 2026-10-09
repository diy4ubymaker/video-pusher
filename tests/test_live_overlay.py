"""Single-file mode through the playlist pipeline, and overlay changes while streaming."""
import json
import queue
import subprocess
import time

import main as vp
from test_playlist import check_timeline, make_logo


def make_long_clip(ffmpeg, path, seconds=14, pattern="testsrc=s=640x360:r=25"):
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"{pattern}:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def start(ffmpeg_tools, out, monkeypatch, **kw):
    ffmpeg, ffprobe = ffmpeg_tools
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    s = dict(mode="file", protocol="SRT", endpoint="srt://unused", bandwidth_k=0, reencode=True,
             reconnect=True, reconnect_delay=1, output_format="720p50", preset="ultrafast", crf=28,
             audio_k=96, overlay=None, logo=None)
    s.update(kw)
    events = queue.Queue()
    w = vp.PlaylistWorker(s, ffmpeg, ffprobe, events)
    w.start()
    return w, events


def drain(events, logs, statuses, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            kind, payload = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if kind == "log":
            logs.append(payload)
        elif kind == "status":
            statuses.append(payload)


def finish(w, events, logs, statuses):
    w.stop()
    w.join(15)
    assert not w.is_alive()
    drain(events, logs, statuses, 0.2)


def duration(ffprobe, path):
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    return float(json.loads(r.stdout)["format"]["duration"])


def timeline(ffprobe, out_file):
    """(timestamps going backwards, largest gap in seconds) for the video stream."""
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v", "-show_entries", "packet=dts_time",
                        "-of", "json", str(out_file)], capture_output=True, text=True, check=True)
    times = [float(p["dts_time"]) for p in json.loads(r.stdout)["packets"] if "dts_time" in p]
    backwards = sum(b < a for a, b in zip(times, times[1:]))
    return backwards, max(b - a for a, b in zip(times, times[1:]))


def crop_pixels(ffmpeg, out, second, crop, test):
    """Pixels of the 720p frame at `second`, inside crop=w:h:x:y, for which test(r, g, b) holds."""
    frame = subprocess.run([ffmpeg, "-v", "error", "-ss", str(second), "-i", str(out), "-vf",
                            f"crop={crop}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True).stdout
    return sum(1 for i in range(0, len(frame) - 2, 3) if test(frame[i], frame[i + 1], frame[i + 2]))


def is_orange(r, g, b):
    return r > 180 and 80 < g < 170 and b < 90


def orange_pixels(ffmpeg, out, second, crop="192:96:1060:22"):
    """Orange pixels of the test logo (top-right corner of the 720p frame by default)."""
    return crop_pixels(ffmpeg, out, second, crop, is_orange)


def test_single_file_loops_seamlessly_in_copy_mode(ffmpeg_tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    clip = tmp_path / "clip.mp4"
    make_long_clip(ffmpeg, clip, seconds=3)
    out = tmp_path / "out.ts"
    w, ev = start(ffmpeg_tools, out, monkeypatch, source=str(clip), reencode=False)
    assert w.update_overlays(None, None) is False        # nothing to change in copy mode
    logs, statuses = [], []
    drain(ev, logs, statuses, 11)
    finish(w, ev, logs, statuses)
    assert sum("Now playing clip.mp4" in l for l in logs) == 1     # not repeated on every pass
    assert any(s.startswith("Looping: clip.mp4 (0:03) - pass 3") for s in statuses)   # duration shown
    assert duration(ffprobe, out) >= 8
    check_timeline(ffprobe, out)


def test_logo_and_text_changed_while_streaming(ffmpeg_tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    clip = tmp_path / "long.mp4"
    make_long_clip(ffmpeg, clip)
    logo = tmp_path / "logo.png"
    make_logo(ffmpeg, logo)
    out = tmp_path / "out.ts"
    w, ev = start(ffmpeg_tools, out, monkeypatch, source=str(clip), live_commands=False)
    logs, statuses = [], []
    drain(ev, logs, statuses, 5)
    t_change = time.monotonic()
    assert w.update_overlays(dict(format="14:30:00", position="Bottom-left", x=0, y=0, size=5, box=True),
                             dict(path=str(logo), position="Top-right", x=0, y=0, size=15, opacity=100))
    drain(ev, logs, statuses, 6)
    finish(w, ev, logs, statuses)

    changed = [l for l in logs if "Overlay settings changed - continuing long.mp4 from" in l]
    assert len(changed) == 1
    resumed = [l for l in logs if "Feeder:" in l][-1]
    assert "drawtext=" in resumed and str(logo) in resumed and " -ss " in resumed
    assert not any("failed after" in l for l in logs)
    assert sum("Now playing" in l for l in logs) == 1         # same file carried on, not restarted
    # no logo before the change, logo after it
    assert orange_pixels(ffmpeg, out, 2) < 200
    assert orange_pixels(ffmpeg, out, duration(ffprobe, out) - 2) > 2000
    # one continuous stream: never backwards, and only a brief stall at the change
    backwards, gap = timeline(ffprobe, out)
    assert backwards == 0 and gap < 1.0, gap
    assert duration(ffprobe, out) >= 9


def test_overlay_removed_while_streaming_in_folder_mode(ffmpeg_tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    folder = tmp_path / "pl"
    folder.mkdir()
    make_long_clip(ffmpeg, folder / "a.mp4", seconds=8)
    from test_playlist import age_files
    age_files(folder)
    logo = tmp_path / "logo.png"
    make_logo(ffmpeg, logo)
    out = tmp_path / "out.ts"
    w, ev = start(ffmpeg_tools, out, monkeypatch, mode="folder", source=str(folder), settle_s=2, live_commands=False,
                  logo=dict(path=str(logo), position="Top-right", x=0, y=0, size=15, opacity=100))
    logs, statuses = [], []
    drain(ev, logs, statuses, 5)
    w.update_overlays(None, None)
    drain(ev, logs, statuses, 6)
    finish(w, ev, logs, statuses)
    assert any("Overlay settings changed" in l for l in logs)
    assert "Playing 1/1: a.mp4 (0:08)" in statuses                  # file duration in the status
    assert orange_pixels(ffmpeg, out, 1.5) > 2000
    assert orange_pixels(ffmpeg, out, duration(ffprobe, out) - 1.5) < 200
    backwards, gap = timeline(ffprobe, out)
    assert backwards == 0 and gap < 1.0, gap


from test_settings import needs_gui  # noqa: E402


@needs_gui
def test_window_apply_button(ffmpeg_tools, tmp_path, monkeypatch):
    """The real window: overlay controls stay usable while streaming and Apply changes the stream."""
    ffmpeg, ffprobe = ffmpeg_tools
    monkeypatch.setenv("VIDEO_PUSHER_CONFIG_DIR", str(tmp_path / "cfg"))
    clip = tmp_path / "long.mp4"
    make_long_clip(ffmpeg, clip)
    logo = tmp_path / "logo.png"
    make_logo(ffmpeg, logo)
    out = tmp_path / "out.ts"
    monkeypatch.setattr(vp, "output_args", lambda s: ["-y", "-f", "mpegts", str(out)])
    monkeypatch.setattr(vp, "ffmpeg_output_protocols", lambda f: set())
    app = vp.VideoPusherApp()

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.update()
            time.sleep(0.05)
    try:
        app.ffmpeg_path, app.ffprobe_path = ffmpeg, ffprobe
        app.var_source.set(str(clip))
        app.var_reencode.set(True); app._update_hints()
        app.var_format.set("720p50"); app.var_preset.set("ultrafast")
        assert str(app.btn_apply["state"]) == "disabled"
        app._start_push()
        pump(4)
        assert isinstance(app.worker, vp.PlaylistWorker)
        assert str(app.btn_apply["state"]) == "normal"
        assert str(app.chk_logo["state"]) == "normal"
        assert str(app.cmb_ov_pos["state"]) == "disabled"        # date/time not ticked yet
        app.var_logo.set(True); app._update_encode_controls()
        assert str(app.ent_logo["state"]) == "normal"
        app.var_logo_path.set(str(logo))
        app.btn_apply.invoke()
        pump(5)
        log = app.txt_log.get("1.0", "end")
        assert "Applying new overlay settings to the live stream (date/time off, logo on)" in log
        assert "Overlay settings changed" in log
        app._stop_push()
        pump(3)
        assert app.worker is None and str(app.btn_apply["state"]) == "disabled"
    finally:
        app.destroy()
    assert orange_pixels(ffmpeg, out, duration(ffprobe, out) - 1.5) > 2000


# ---------------- live commands (FFmpeg zmq filter): no restart at all ----------------
def need_zmq(ffmpeg):
    import pytest
    if "zmq" not in vp.ffmpeg_caps(ffmpeg)["filters"]:
        pytest.skip("this FFmpeg build has no zmq filter")


LOGO_TR = "192:96:1060:22"          # where a 15 % logo sits: top-right ...
LOGO_BL = "192:96:22:602"           # ... and bottom-left


def test_live_commands_move_and_fade_logo_and_restyle_text(ffmpeg_tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    need_zmq(ffmpeg)
    clip = tmp_path / "long.mp4"
    make_long_clip(ffmpeg, clip, pattern="color=c=0x202060:s=640x360:r=25")
    logo = tmp_path / "logo.png"
    make_logo(ffmpeg, logo)
    out = tmp_path / "out.ts"
    lg = dict(path=str(logo), position="Top-right", x=0, y=0, size=15, opacity=100)
    w, ev = start(ffmpeg_tools, out, monkeypatch, source=str(clip), logo=lg,
                  overlay=dict(format="14:30:00", position="Top-left", x=0, y=0, size=6, box=True,
                               colour="#FFFFFF"))
    logs, statuses = [], []
    drain(ev, logs, statuses, 5)
    assert w.update_overlays(dict(format="2026-10-08 14:30:00", position="Bottom-right", x=0, y=0, size=8,
                                  box=False, colour="#FFFF00"),
                             dict(lg, position="Bottom-left", opacity=50))
    drain(ev, logs, statuses, 5)
    finish(w, ev, logs, statuses)

    assert any("instantly, with live commands" in l for l in logs)
    assert any("applied instantly (live commands)" in l for l in logs)
    assert not any("continuing long.mp4" in l or "Restarting the feeder" in l for l in logs)
    assert not any("failed after" in l for l in logs)
    end = duration(ffprobe, out) - 1.5
    assert orange_pixels(ffmpeg, out, 2, LOGO_TR) > 2000 and orange_pixels(ffmpeg, out, 2, LOGO_BL) == 0
    assert orange_pixels(ffmpeg, out, end, LOGO_TR) == 0
    # at 50 % opacity the logo is a mix of orange and the dark background
    faded = crop_pixels(ffmpeg, out, end, LOGO_BL, lambda r, g, b: 110 < r < 190 and b < 110 and r > g > b)
    assert faded > 2000
    yellow = lambda r, g, b: r > 200 and g > 200 and b < 80
    assert crop_pixels(ffmpeg, out, 2, "640:120:640:600", yellow) == 0
    assert crop_pixels(ffmpeg, out, end, "640:120:640:600", yellow) > 300      # yellow clock, bottom-right
    backwards, gap = timeline(ffprobe, out)
    assert backwards == 0 and gap < 0.1, gap                                   # no stall at all


def test_live_commands_switch_text_on_and_logo_off(ffmpeg_tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    need_zmq(ffmpeg)
    clip = tmp_path / "long.mp4"
    make_long_clip(ffmpeg, clip, pattern="color=c=0x202060:s=640x360:r=25")
    logo = tmp_path / "logo.png"
    make_logo(ffmpeg, logo)
    out = tmp_path / "out.ts"
    lg = dict(path=str(logo), position="Top-right", x=0, y=0, size=15, opacity=100)
    w, ev = start(ffmpeg_tools, out, monkeypatch, source=str(clip), logo=lg)      # no date/time at Start
    logs, statuses = [], []
    drain(ev, logs, statuses, 5)
    w.update_overlays(dict(format="14:30:00", position="Top-left", x=0, y=0, size=8, box=True,
                           colour="#FFFF00"), None)
    drain(ev, logs, statuses, 4)
    w.update_overlays(None, lg)                                                 # and back again
    drain(ev, logs, statuses, 4)
    finish(w, ev, logs, statuses)
    assert sum("applied instantly" in l for l in logs) == 2
    yellow = lambda r, g, b: r > 200 and g > 200 and b < 80
    d = duration(ffprobe, out)
    assert crop_pixels(ffmpeg, out, 2, "400:120:0:0", yellow) == 0
    assert crop_pixels(ffmpeg, out, 7.5, "400:120:0:0", yellow) > 300
    assert orange_pixels(ffmpeg, out, 7.5) == 0
    assert crop_pixels(ffmpeg, out, d - 1, "400:120:0:0", yellow) == 0
    assert orange_pixels(ffmpeg, out, d - 1) > 2000
    backwards, gap = timeline(ffprobe, out)
    assert backwards == 0 and gap < 0.1, gap


def test_new_logo_file_falls_back_to_restart(ffmpeg_tools, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_tools
    need_zmq(ffmpeg)
    clip = tmp_path / "long.mp4"
    make_long_clip(ffmpeg, clip)
    logo = tmp_path / "logo.png"
    make_logo(ffmpeg, logo)
    out = tmp_path / "out.ts"
    w, ev = start(ffmpeg_tools, out, monkeypatch, source=str(clip))            # no logo at Start
    logs, statuses = [], []
    drain(ev, logs, statuses, 5)
    w.update_overlays(None, dict(path=str(logo), position="Top-right", x=0, y=0, size=15, opacity=100))
    drain(ev, logs, statuses, 5)
    finish(w, ev, logs, statuses)
    assert any("Restarting the feeder for this change (adding a logo)" in l for l in logs)
    assert any("continuing long.mp4" in l for l in logs)
    assert orange_pixels(ffmpeg, out, duration(ffprobe, out) - 1.5) > 2000
    backwards, gap = timeline(ffprobe, out)
    assert backwards == 0 and gap < 1.0, gap
