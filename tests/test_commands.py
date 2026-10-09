"""Unit tests for command building and helpers (no FFmpeg or display needed)."""
import os

import main as vp


def settings(**kw):
    base = dict(mode="file", protocol="SRT", source="in.mp4",
                endpoint="srt://10.0.0.1:9000?mode=caller", bandwidth_k=0,
                reencode=False, reconnect=True, reconnect_delay=3, settle_s=10)
    base.update(kw)
    return base


def test_single_file_srt_copy():
    cmd = vp.build_single_file_cmd("ffmpeg", settings())
    assert cmd[:7] == ["ffmpeg", "-hide_banner", "-stream_loop", "-1", "-re", "-i", "in.mp4"]
    assert cmd[7:9] == ["-c", "copy"]
    assert cmd[-3:] == ["-f", "mpegts", "srt://10.0.0.1:9000?mode=caller"]


def test_srt_copy_bandwidth_adds_maxbw():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(bandwidth_k=4000))
    assert cmd[-1] == "srt://10.0.0.1:9000?mode=caller&maxbw=500000"   # 4000 kbps = 500000 bytes/s


def test_maxbw_not_overridden_if_user_set_it():
    url = "srt://h:1?mode=caller&maxbw=123"
    assert vp.add_srt_maxbw(url, 4000) == url


def test_reencode_bandwidth_split_between_video_and_audio():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(reencode=True, bandwidth_k=4000))
    assert "libx264" in cmd and "aac" in cmd
    assert cmd[cmd.index("-b:v") + 1] == f"{4000 - vp.AUDIO_BITRATE_K}k"
    assert cmd[cmd.index("-b:a") + 1] == f"{vp.AUDIO_BITRATE_K}k"
    assert "maxbw" not in cmd[-1]               # encoder handles the cap, not the link


def test_reencode_video_bitrate_has_floor():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(reencode=True, bandwidth_k=100))
    assert cmd[cmd.index("-b:v") + 1] == f"{vp.MIN_VIDEO_BITRATE_K}k"


def test_rtmp_output_is_flv():
    s = settings(protocol="RTMP", endpoint="rtmp://live.example.com/app/key", bandwidth_k=4000)
    cmd = vp.build_single_file_cmd("ffmpeg", s)
    assert cmd[-7:] == ["-f", "flv", "-flvflags", "no_duration_filesize", "-rw_timeout", "10000000",
                        "rtmp://live.example.com/app/key"]   # no maxbw for RTMP


def test_progress_line_detection():
    assert vp.is_progress_line("frame=  100 fps= 25 q=-1.0 size=  1024kB time=00:00:04.00 "
                               "bitrate=2097.2kbits/s speed=1x")
    assert vp.is_progress_line("size=     512kB time=00:00:02.00 bitrate=2000.0kbits/s speed=1x")
    assert not vp.is_progress_line("Stream #0:0: Video: h264")


def test_parse_rate():
    assert vp.parse_rate("30000/1001") == 30000 / 1001
    assert vp.parse_rate("25/1") == 25.0
    assert vp.parse_rate("0/0") == 0.0
    assert vp.parse_rate("junk") == 0.0


def test_fmt_duration():
    assert vp.fmt_duration(65) == "1:05"
    assert vp.fmt_duration(3725) == "1:02:05"


def test_list_videos_filters_and_sorts(tmp_path):
    for name in ["b.MP4", "a.mkv", "C.mov", "notes.txt", ".hidden.mp4"]:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub.mp4").mkdir()           # a folder named like a video is ignored
    names = [os.path.basename(p) for p in vp.list_videos(str(tmp_path))]
    assert names == ["a.mkv", "b.MP4", "C.mov"]


def test_copy_signature_detects_differences():
    a = dict(vcodec="h264", width=1920, height=1080, fps=25.0, has_audio=True,
             acodec="aac", sample_rate="48000", channels=2)
    assert vp.copy_signature(a) == vp.copy_signature(dict(a))
    assert vp.copy_signature(a) != vp.copy_signature(dict(a, width=1280))
    assert vp.copy_signature(a) != vp.copy_signature(dict(a, has_audio=False, acodec=None))


# ---------------- output formats / test signal ----------------
def test_field_rate():
    assert vp.field_rate("25") == "50"
    assert vp.field_rate("30000/1001") == "60000/1001"


def test_video_chain_interlaced_hd():
    chain = vp.video_chain(vp.OUTPUT_FORMATS["1080i50"], True)
    assert chain.startswith("yadif=mode=send_field,")
    assert chain.endswith("fps=50,tinterlace=mode=interleave_top,setfield=tff")


def test_video_chain_anamorphic_sd():
    chain = vp.video_chain(vp.OUTPUT_FORMATS["576i50"], False)
    assert "scale=1024:576:force_original_aspect_ratio=decrease" in chain
    assert "scale=720:576,setsar=64/45" in chain
    assert "yadif" not in chain


def test_single_file_reencode_uses_selected_format():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(reencode=True, output_format="1080i59.94"))
    vf = cmd[cmd.index("-vf") + 1]
    assert "fps=60000/1001,tinterlace" in vf and "deint=interlaced" in vf
    assert "+ildct+ilme" in cmd


def test_single_file_auto_format_adds_no_filter():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(reencode=True, output_format=vp.FORMAT_AUTO))
    assert "-vf" not in cmd


def test_slate_levels_depend_on_ffmpeg_and_assets(tmp_path):
    full = {"filters": {"drawtext"}, "encoders": set()}
    no_text = {"filters": set(), "encoders": set()}
    assert vp.slate_levels(full) == ["text", "image", "plain"]
    assert vp.slate_levels(no_text) == ["image", "plain"]
    assert vp.slate_levels(full, assets_dir=str(tmp_path)) == ["plain"]     # assets missing


def test_slate_codecs_match_copy_mode_reference():
    caps = {"filters": set(), "encoders": {"libx264", "mpeg2video", "mp2", "aac"}}
    ref = dict(vcodec="mpeg2video", has_audio=True, acodec="mp2", sample_rate="48000", channels=2,
               pix_fmt="yuv422p")
    v, a, sr, ch = vp.slate_codecs(settings(reencode=False), ref, caps)
    assert v[:2] == ["-c:v", "mpeg2video"] and "yuv422p" in v
    assert a[:2] == ["-c:a", "mp2"] and (sr, ch) == (48000, 2)


def test_slate_cmd_bars_text_interlaced():
    t = vp.OUTPUT_FORMATS["1080i50"]
    v, a, sr, ch = vp.slate_codecs(settings(reencode=True), None, {"filters": set(), "encoders": set()})
    cmd = vp.slate_cmd("ffmpeg", "bars", "text", t, v, a, sr, ch, 4, 12.5)
    joined = " ".join(cmd)
    assert "smptehdbars=s=1920x1080:r=50" in joined
    assert "sine=frequency=1000:sample_rate=48000" in joined
    assert "fontfile=DejaVuSansMono-Bold.ttf" in joined and "localtime" in joined
    assert "tinterlace=mode=interleave_top" in joined and "+ildct+ilme" in cmd
    assert cmd[-5:] == ["-output_ts_offset", "12.500", "-f", "mpegts", "pipe:1"]


# ---------------- quality controls + date/time overlay ----------------
def test_encode_args_quality_defaults_and_custom():
    a = vp.encode_args(settings(reencode=True))
    assert a[a.index("-preset") + 1] == "veryfast" and a[a.index("-crf") + 1] == "23"
    assert a[a.index("-b:a") + 1] == "128k"
    a = vp.encode_args(settings(reencode=True, preset="medium", crf=19, audio_k=192))
    assert a[a.index("-preset") + 1] == "medium" and a[a.index("-crf") + 1] == "19"
    assert a[a.index("-b:a") + 1] == "192k"


def test_bandwidth_cap_replaces_crf_and_uses_audio_setting():
    a = vp.encode_args(settings(reencode=True, bandwidth_k=3000, audio_k=192, crf=19))
    assert "-crf" not in a
    assert a[a.index("-b:v") + 1] == "2808k"


def overlay(**kw):
    base = dict(format="2026-10-08 14:30:00", position="Top-left", x=50, y=90, size=5, box=True)
    base.update(kw)
    return base


def test_overlay_filter_positions_size_and_box():
    f = vp.overlay_filter(overlay())
    assert "fontfile=DejaVuSansMono-Bold.ttf" in f and "localtime\\:%Y-%m-%d %T" in f
    assert ":x=h*0.03:y=h*0.03" in f and "fontsize=h*0.0500" in f and "box=1" in f
    f = vp.overlay_filter(overlay(position="Bottom-right", box=False, size=8, format="14:30:00"))
    assert ":x=w-text_w-h*0.03:y=h-text_h-h*0.03" in f and "box=1" not in f and "borderw=2" in f
    assert "localtime\\:%T" in f and "fontsize=h*0.0800" in f
    f = vp.overlay_filter(overlay(position="Centre"))
    assert ":x=(w-text_w)/2:y=(h-text_h)/2" in f
    f = vp.overlay_filter(overlay(position="Custom", x=25, y=80))
    assert ":x=(w-text_w)*0.2500:y=(h-text_h)*0.8000" in f
    assert vp.overlay_filter(None) is None


def test_overlay_goes_before_interlacing():
    chain = vp.video_chain(vp.OUTPUT_FORMATS["1080i50"], False, vp.overlay_filter(overlay()))
    assert chain.index("drawtext") < chain.index("tinterlace")


def test_single_file_overlay_without_fixed_format():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(reencode=True, overlay=overlay()))
    assert cmd[cmd.index("-vf") + 1].startswith("drawtext=")


def test_constant_rate_drops_crf():
    v = vp.constant_rate(["-c:v", "libx264", "-crf", "23"], 2000)
    assert "-crf" not in v and v[v.index("-b:v") + 1] == "2000k"


def test_overlay_colour_and_automatic_contrast():
    f = vp.overlay_filter(overlay(colour="#FFFF00"))
    assert "fontcolor=0xFFFF00" in f and "boxcolor=black@" in f        # light text -> dark box
    f = vp.overlay_filter(overlay(colour="#000000"))
    assert "fontcolor=0x000000" in f and "boxcolor=white@" in f        # dark text -> light box
    f = vp.overlay_filter(overlay(colour="#0000FF", box=False))
    assert "bordercolor=white@" in f                                     # dark outline -> light
    assert "fontcolor=0xFFFFFF" in vp.overlay_filter(overlay())          # default white
    assert set(vp.OVERLAY_COLOURS) >= {"White", "Yellow", "Black", "Red", "Green", "Blue"}


# ---------------- logo overlay ----------------
def logo(**kw):
    base = dict(path="/x/logo.png", position="Top-right", x=95, y=5, size=15, opacity=100)
    base.update(kw)
    return base


def test_logo_positions():
    assert vp.logo_position(logo()) == ("W-w-H*0.03", "H*0.03")
    assert vp.logo_position(logo(position="Bottom-left")) == ("H*0.03", "H-h-H*0.03")
    assert vp.logo_position(logo(position="Centre")) == ("(W-w)/2", "(H-h)/2")
    assert vp.logo_position(logo(position="Custom", x=25, y=50)) == ("(W-w)*0.2500", "(H-h)*0.5000")


def test_logo_graph_sizes_from_square_pixel_width_and_interlaces_last():
    t = vp.OUTPUT_FORMATS["576i50"]
    g = vp.video_graph("[0:v]", t, False, vp.overlay_filter(overlay()), logo(size=20, opacity=60), 1, t.wsq)
    assert "[1:v]scale=204:-1,format=rgba,colorchannelmixer=aa=0.60[logo]" in g     # 20 % of 1024
    assert "overlay=x=W-w-H*0.03:y=H*0.03:eof_action=repeat" in g
    assert g.index("drawtext") < g.index("[logo]")          # text drawn first, logo on top
    assert g.index("overlay=") < g.index("scale=720:576") < g.index("tinterlace")
    assert g.endswith("[vout]")


def test_single_file_logo_command():
    s = settings(reencode=True, logo=logo(), src_width=1280)
    cmd = vp.build_single_file_cmd("ffmpeg", s)
    assert cmd[cmd.index("-i", cmd.index("in.mp4")) + 1] == "/x/logo.png"
    assert "[1:v]scale=192:-1" in cmd[cmd.index("-filter_complex") + 1]           # 15 % of 1280
    assert cmd[cmd.index("-map") + 1] == "[vout]" and "0:a:0?" in cmd and "-vf" not in cmd


def test_logo_ignored_without_reencode():
    cmd = vp.build_single_file_cmd("ffmpeg", settings(reencode=False, logo=logo(), src_width=1280))
    assert "/x/logo.png" not in cmd
