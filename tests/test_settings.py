"""Saved settings: helper functions (always run) and a real-window round trip for both apps
(needs Tkinter and a display, so it is skipped on headless machines)."""
import json
import os

import pytest

import main as vp


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_PUSHER_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_save_and_load_round_trip(cfg):
    assert vp.load_app_settings("pusher") == ({}, None)          # nothing saved yet: not an error
    assert vp.save_app_settings("pusher", {"a": 1, "b": "x"}) is None
    assert vp.load_app_settings("pusher") == ({"a": 1, "b": "x"}, None)
    assert not any(p.name.endswith(".tmp") for p in cfg.iterdir())  # written atomically


def test_corrupt_file_is_ignored_with_a_note(cfg):
    cfg.mkdir()
    (cfg / "pusher-settings.json").write_text("{not json", encoding="utf-8")
    data, err = vp.load_app_settings("pusher")
    assert data == {} and "ignored unreadable settings file" in err


def test_default_location_is_per_user(monkeypatch):
    monkeypatch.delenv("VIDEO_PUSHER_CONFIG_DIR", raising=False)
    assert vp.config_dir().endswith("VideoPusher")


def test_str_dict_keeps_known_string_entries():
    assert vp.str_dict({"SRT": "srt://x", "BAD": "y", "RIST": 5}, ["SRT", "RIST"]) == {"SRT": "srt://x"}
    assert vp.str_dict("nope", ["SRT"]) == {}


# ---------------- real windows ----------------
def _gui_available():
    try:
        import tkinter
        return isinstance(tkinter.Tk, type) and tkinter.Tk is not object and bool(os.environ.get("DISPLAY"))
    except ImportError:
        return False


needs_gui = pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")


@needs_gui
def test_pusher_window_round_trip(cfg):
    app = vp.VideoPusherApp()
    app.var_mode.set("folder"); app._on_mode_change()
    app.var_source.set("/videos/playlist")
    app.var_protocol.set("RIST"); app._on_protocol_change()
    app.var_endpoint.set("rist://10.1.1.5:6000")
    app.var_rist_enc.set("AES-256"); app.var_rist_secret.set("TopSecret99")
    app.var_reencode.set(True); app.var_preset.set("fast"); app.var_crf.set("20")
    app.var_overlay.set(True); app.var_ov_pos.set("Bottom-right"); app.var_ov_colour.set("Yellow")
    app.var_logo.set(True); app.var_logo_path.set("/logos/station.png"); app.var_logo_opacity.set("70")
    app.var_format.set("1080i50")
    app.on_closing()                                   # saves, then closes

    saved = (cfg / "pusher-settings.json").read_text(encoding="utf-8")
    assert "TopSecret99" not in saved                  # passphrase never written
    assert json.loads(saved)["endpoints_by_direction"]["RIST/push"] == "rist://10.1.1.5:6000"

    app = vp.VideoPusherApp()
    try:
        assert app.var_mode.get() == "folder" and app.var_source.get() == "/videos/playlist"
        assert app.lbl_source.cget("text") == "Source folder:"
        assert app.var_protocol.get() == "RIST" and app.var_endpoint.get() == "rist://10.1.1.5:6000"
        assert app.lbl_endpoint.cget("text") == "RIST Endpoint:"
        assert app.var_rist_enc.get() == "AES-256" and app.var_rist_secret.get() == ""
        assert app.var_reencode.get() and app.var_preset.get() == "fast" and app.var_crf.get() == "20"
        assert app.var_ov_pos.get() == "Bottom-right" and app.var_ov_colour.get() == "Yellow"
        assert app.var_logo.get() and app.var_logo_path.get() == "/logos/station.png"
        assert app.var_format.get() == "1080i50"
        # switching protocol still remembers the other protocols' addresses
        app.var_protocol.set("SRT"); app._on_protocol_change()
        assert app.var_endpoint.get() == vp.DEFAULT_ENDPOINTS["SRT/push"]
        # and the listen direction has its own address
        app.var_connection.set("listen"); app._on_protocol_change()
        assert app.var_endpoint.get() == vp.DEFAULT_ENDPOINTS["SRT/listen"]
        assert app.lbl_endpoint.cget("text") == "SRT Listen on:"
    finally:
        app.destroy()


@needs_gui
def test_pusher_ignores_values_it_no_longer_offers(cfg):
    cfg.mkdir()
    (cfg / "pusher-settings.json").write_text(json.dumps(
        {"preset": "placebo", "protocol": "HTTP", "reencode": "yes", "crf": 21}), encoding="utf-8")
    app = vp.VideoPusherApp()
    try:
        assert app.var_preset.get() == vp.DEFAULT_PRESET        # unknown preset ignored
        assert app.var_protocol.get() == "SRT"                    # unknown protocol ignored
        assert app.var_reencode.get() is False                    # wrong type ignored
        assert app.var_crf.get() == "21"
    finally:
        app.destroy()


@needs_gui
def test_receiver_window_round_trip(cfg):
    import receiver as rx
    app = rx.ReceiverApp()
    app.var_protocol.set("RTMP"); app._on_protocol_change()
    app.var_endpoint.set("rtmp://0.0.0.0:1940/live/x")
    app.var_mode.set("play_save"); app.var_output.set("/rec/show.mp4"); app.var_format.set(".mp4")
    app.var_mc_ip.set("239.9.9.9")
    app.var_rist_secret.set("Hidden1")
    app.on_closing()
    assert "Hidden1" not in (cfg / "receiver-settings.json").read_text(encoding="utf-8")

    app = rx.ReceiverApp()
    try:
        # the stream key ("x") typed into the address is not saved
        assert app.var_protocol.get() == "RTMP" and app.var_endpoint.get() == "rtmp://0.0.0.0:1940/live"
        assert app.var_mode.get() == "play_save" and app.var_output.get() == "/rec/show.mp4"
        assert app.var_format.get() == ".mp4" and app.var_mc_ip.get() == "239.9.9.9"
        assert app.var_rist_secret.get() == ""
    finally:
        app.destroy()


@needs_gui
def test_old_settings_files_are_migrated(cfg):
    """Settings saved before 2.6.0 had one address per protocol (push for the pusher, listen for
    the receiver)."""
    import receiver as rx
    cfg.mkdir()
    (cfg / "pusher-settings.json").write_text(json.dumps(
        {"protocol": "SRT", "endpoints": {"SRT": "srt://10.9.9.9:9000?mode=caller"}}), encoding="utf-8")
    (cfg / "receiver-settings.json").write_text(json.dumps(
        {"protocol": "RIST", "endpoints": {"RIST": "rist://@0.0.0.0:6000"}}), encoding="utf-8")
    app = vp.VideoPusherApp()
    try:
        assert app.var_connection.get() == "push" and app.var_endpoint.get() == "srt://10.9.9.9:9000?mode=caller"
    finally:
        app.destroy()
    app = rx.ReceiverApp()
    try:
        assert app.var_connection.get() == "listen" and app.var_endpoint.get() == "rist://@0.0.0.0:6000"
    finally:
        app.destroy()
