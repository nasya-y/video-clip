from pathlib import Path

from vcm.settings import DEFAULT_WATERMARK_TEXT, Settings, load_settings, save_settings


def test_defaults_match_specification():
    s = Settings()
    assert s.speed == 1.3
    assert s.clip_duration == 3.0
    assert s.clips_per_compilation == 10
    assert s.compilation_duration == 30.0
    assert abs(s.source_clip_duration - 3.9) < 1e-9
    assert s.watermark_text == "onlyfans.com/nyraishtara" == DEFAULT_WATERMARK_TEXT
    assert s.watermark_color_mode == "auto"
    assert s.quality == "high"
    d = s.processing_dict()
    assert d["video_codec"].startswith("H.264")
    assert d["audio_codec"] == "AAC"


def test_roundtrip(tmp_path: Path):
    p = tmp_path / "settings.json"
    s = Settings(speed=1.5, clip_duration=2.5, clips_per_compilation=8, output_dir="C:/out ü",
                 input_paths=["a.mp4", "b é.mov"], watermark_color_mode="custom", watermark_custom_color="#00FF00")
    save_settings(s, p)
    loaded = load_settings(p)
    assert loaded == s.validated()


def test_invalid_values_are_sanitised(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text('{"speed": "fast", "clip_duration": -4, "clips_per_compilation": 5000, "quality": "x",'
                 ' "watermark_custom_color": "green", "unknown_key": 1}', encoding="utf-8")
    s = load_settings(p)
    assert s.speed == 1.3
    assert s.clip_duration == 0.5
    assert s.clips_per_compilation == 100
    assert s.quality == "high"
    assert s.watermark_custom_color == "#39FF14"


def test_corrupt_file_gives_defaults(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_settings(p) == Settings()


def test_empty_watermark_is_rejected():
    assert Settings(watermark_text="   ").validated().problems()
    assert not Settings().problems()
