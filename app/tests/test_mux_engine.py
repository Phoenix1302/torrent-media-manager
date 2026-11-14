import json
import sys
from pathlib import Path

# Ensure app directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from media_inspector import inspect_file_with_mkvmerge, scan_release_tracks
from mux_builder import build_advanced_mkvmerge_cmd
from muxer import (
    MuxProgressTracker,
    get_mux_progress,
    parse_mkvmerge_progress,
    progress_tracker,
)


# --- 1. MediaInspector Tests with JSON Fixtures ---

def test_media_inspector_with_sample_json_fixtures():
    sample_mkvmerge_json = {
        "container": {
            "type": "Matroska",
            "properties": {
                "duration": 3600000000000,  # 3600 seconds in ns
                "container_type": 17,
            },
        },
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "MPEG-4p10/AVC/h.264",
                "properties": {
                    "pixel_dimensions": "1920x1080",
                    "width": 1920,
                    "height": 1080,
                    "default_track": True,
                },
            },
            {
                "id": 1,
                "type": "audio",
                "codec": "AC-3",
                "properties": {
                    "audio_channels": 6,
                    "channel_layout": "5.1",
                    "language": "eng",
                    "language_ietf": "en",
                    "track_name": "English 5.1 Surround",
                    "default_track": True,
                    "forced_track": False,
                    "tag_bps": "640000",
                },
            },
            {
                "id": 2,
                "type": "audio",
                "codec": "AAC",
                "properties": {
                    "audio_channels": 2,
                    "channel_layout": "stereo",
                    "language": "jpn",
                    "language_ietf": "ja",
                    "track_name": "Japanese Stereo",
                    "default_track": False,
                    "forced_track": False,
                    "bit_rate": 192000,
                },
            },
            {
                "id": 3,
                "type": "subtitles",
                "codec": "SubRip/SRT",
                "properties": {
                    "language": "eng",
                    "language_ietf": "en",
                    "track_name": "Full English",
                    "default_track": True,
                    "forced_track": False,
                },
            },
            {
                "id": 4,
                "type": "subtitles",
                "codec": "SubStationAlpha",
                "properties": {
                    "language": "rus",
                    "language_ietf": "ru",
                    "track_name": "Russian Forced Signs",
                    "default_track": False,
                    "forced_track": True,
                },
            },
        ],
        "attachments": [
            {
                "id": 1,
                "file_name": "arial_bold.ttf",
                "content_type": "application/x-truetype-font",
                "size": 102400,
            }
        ],
    }

    result = inspect_file_with_mkvmerge("dummy.mkv", raw_json=sample_mkvmerge_json)

    assert result["format"] == "mkv"
    assert result["duration"] == 3600.0

    # Video
    assert len(result["video_tracks"]) == 1
    v = result["video_tracks"][0]
    assert v["id"] == 0
    assert v["codec"] == "MPEG-4p10/AVC/h.264"
    assert v["properties"]["width"] == 1920
    assert v["properties"]["height"] == 1080
    assert v["properties"]["pixel_dimensions"] == "1920x1080"
    assert v["properties"]["default_track"] is True

    # Audio
    assert len(result["audio_tracks"]) == 2
    a0 = result["audio_tracks"][0]
    assert a0["id"] == 1
    assert a0["codec"] == "AC-3"
    assert a0["channels"] == 6
    assert a0["channel_layout"] == "5.1"
    assert a0["language"] == "eng"
    assert a0["language_ietf"] == "en"
    assert a0["name"] == "English 5.1 Surround"
    assert a0["default_track"] is True
    assert a0["forced_track"] is False
    assert a0["bit_rate"] == 640000

    a1 = result["audio_tracks"][1]
    assert a1["id"] == 2
    assert a1["codec"] == "AAC"
    assert a1["channels"] == 2
    assert a1["channel_layout"] == "stereo"
    assert a1["language"] == "jpn"
    assert a1["default_track"] is False
    assert a1["bit_rate"] == 192000

    # Subtitles
    assert len(result["subtitle_tracks"]) == 2
    s0 = result["subtitle_tracks"][0]
    assert s0["id"] == 3
    assert s0["codec"] == "SubRip/SRT"
    assert s0["language"] == "eng"
    assert s0["default_track"] is True
    assert s0["forced_track"] is False

    s1 = result["subtitle_tracks"][1]
    assert s1["id"] == 4
    assert s1["codec"] == "SubStationAlpha"
    assert s1["language"] == "rus"
    assert s1["default_track"] is False
    assert s1["forced_track"] is True

    # Attachments
    assert len(result["attachments"]) == 1
    att = result["attachments"][0]
    assert att["id"] == 1
    assert att["file_name"] == "arial_bold.ttf"
    assert att["content_type"] == "application/x-truetype-font"
    assert att["size"] == 102400


# --- 2. build_advanced_mkvmerge_cmd Tests ---

def test_build_advanced_cmd_drop_internal_audio_keep_subtitles():
    cmd = build_advanced_mkvmerge_cmd(
        output_file=Path("/out/movie.mkv"),
        video_file=Path("/in/movie.mkv"),
        keep_internal_audio_ids=None,
        keep_internal_subtitle_ids=[2, 3],
    )

    # Must drop all internal audio
    assert "--no-audio" in cmd
    # Must keep subtitle tracks 2,3
    assert "--subtitle-tracks" in cmd
    sub_idx = cmd.index("--subtitle-tracks")
    assert cmd[sub_idx + 1] == "2,3"

    # Destination and input file
    assert "-o" in cmd
    assert cmd[cmd.index("-o") + 1] == str(Path("/out/movie.mkv"))
    assert str(Path("/in/movie.mkv")) in cmd


def test_build_advanced_cmd_keep_specific_internal_audio_add_external_with_sync():
    cmd = build_advanced_mkvmerge_cmd(
        output_file=Path("/out/movie.mkv"),
        video_file=Path("/in/movie.mkv"),
        keep_internal_audio_ids=[1],
        keep_internal_subtitle_ids=[],
        external_audio=[
            {
                "path": Path("/tracks/rus_dub.mka"),
                "track_name": "RUS Dub (AniLibria)",
                "language": "rus",
                "is_default": True,
                "sync_ms": 250,
            }
        ],
    )

    # Internal audio 1 kept, internal subtitles dropped
    assert "--audio-tracks" in cmd
    assert cmd[cmd.index("--audio-tracks") + 1] == "1"
    assert "--no-subtitles" in cmd

    # External audio options
    assert "--language" in cmd
    assert cmd[cmd.index("--language") + 1] == "0:rus"
    assert "--track-name" in cmd
    assert cmd[cmd.index("--track-name") + 1] == "0:RUS Dub (AniLibria)"
    assert "--default-track-flag" in cmd
    assert cmd[cmd.index("--default-track-flag") + 1] == "0:1"
    assert "--sync" in cmd
    assert cmd[cmd.index("--sync") + 1] == "0:250"
    assert str(Path("/tracks/rus_dub.mka")) in cmd


def test_build_advanced_cmd_default_and_forced_flags():
    cmd = build_advanced_mkvmerge_cmd(
        output_file=Path("/out/movie.mkv"),
        video_file=Path("/in/movie.mkv"),
        keep_internal_audio_ids=[1, 2],
        keep_internal_subtitle_ids=[3],
        external_subtitles=[
            {
                "path": Path("/tracks/forced.ass"),
                "track_name": "RUS Forced Signs",
                "language": "rus",
                "is_default": False,
                "is_forced": True,
                "sync_ms": 0,  # 0 sync should not add --sync
            }
        ],
        default_audio_track_spec=(0, 2),  # Track 2 in video_file is default audio
        default_subtitle_track_spec=(1, 0),  # Track 0 in source 1 (external subtitle)
    )

    # Internal track 2 marked default
    assert "--default-track-flag" in cmd
    # Source 0 options
    video_idx = cmd.index(str(Path("/in/movie.mkv")))
    opts_before_video = cmd[:video_idx]
    assert "2:1" in opts_before_video

    # External subtitle track has forced=1 and default=1 (via default_subtitle_track_spec)
    assert "--forced-display-flag" in cmd
    assert cmd[cmd.index("--forced-display-flag") + 1] == "0:1"
    # No --sync added for 0ms
    assert "--sync" not in cmd


def test_build_advanced_cmd_track_ordering():
    cmd = build_advanced_mkvmerge_cmd(
        output_file=Path("/out/movie.mkv"),
        video_file=Path("/in/movie.mkv"),
        keep_internal_audio_ids=[1],
        track_order=["0:0", "1:0", "0:1"],
    )

    assert "--track-order" in cmd
    order_idx = cmd.index("--track-order")
    assert cmd[order_idx + 1] == "0:0,1:0,0:1"


# --- 3. Progress String Regex Parser Tests ---

def test_progress_string_regex_parser():
    assert parse_mkvmerge_progress("Progress: 73%") == 73.0
    assert parse_mkvmerge_progress("Progress: 0%") == 0.0
    assert parse_mkvmerge_progress("Progress: 100%") == 100.0
    assert parse_mkvmerge_progress("Progress: 45.5%") == 45.5

    # Multiple progress updates in streaming buffer
    buffer = "Progress: 10%Progress: 25%Progress: 73%"
    assert parse_mkvmerge_progress(buffer) == 73.0

    # No progress
    assert parse_mkvmerge_progress("The file has been opened for writing.") is None
    assert parse_mkvmerge_progress("") is None


# --- 4. MuxProgressTracker Tests ---

def test_mux_progress_tracker_lifecycle():
    tracker = MuxProgressTracker()

    state = tracker.start_job(job_id=42, total_files=2, first_file_name="ep01.mkv")
    assert state["status"] == "muxing"
    assert state["current_file_index"] == 1
    assert state["total_files"] == 2
    assert state["current_percent"] == 0.0
    assert state["overall_percent"] == 0.0
    assert state["current_file_name"] == "ep01.mkv"

    # Progress on first file: 50%
    tracker.update_progress(job_id=42, current_percent=50.0)
    current = tracker.get_progress(42)
    assert current["current_percent"] == 50.0
    assert current["overall_percent"] == 25.0  # 50% of first file out of 2 = 25%

    # Move to file 2: 0%
    tracker.update_progress(
        job_id=42,
        current_percent=0.0,
        current_file_index=2,
        current_file_name="ep02.mkv",
    )
    current = tracker.get_progress(42)
    assert current["current_file_index"] == 2
    assert current["current_file_name"] == "ep02.mkv"
    assert current["overall_percent"] == 50.0

    # File 2: 80%
    tracker.update_progress(job_id=42, current_percent=80.0)
    current = tracker.get_progress(42)
    assert current["current_percent"] == 80.0
    assert current["overall_percent"] == 90.0  # (1 + 0.8)/2 = 90%

    # Complete
    tracker.complete_job(job_id=42)
    current = tracker.get_progress(42)
    assert current["status"] == "completed"
    assert current["overall_percent"] == 100.0

    # Error job
    tracker.start_job(job_id=43, total_files=1)
    tracker.fail_job(job_id=43, error="Disk full")
    failed = tracker.get_progress(43)
    assert failed["status"] == "error"
    assert failed["error"] == "Disk full"


# --- 5. scan_release_tracks Integration Test ---

def test_scan_release_tracks_with_mocked_inspection(tmp_path: Path, monkeypatch):
    staging = tmp_path / "staging_job_1"
    staging.mkdir()

    # Create dummy video and external audio
    vid = staging / "S01E01.mkv"
    vid.write_bytes(b"dummy")

    ext_audio_dir = staging / "RUS Dub"
    ext_audio_dir.mkdir()
    ext_audio = ext_audio_dir / "01.mka"
    ext_audio.write_bytes(b"dummy")

    # Mock inspect_file_with_mkvmerge so it doesn't need external process
    mock_inspection = {
        "format": "mkv",
        "duration": 1420.0,
        "video_tracks": [{"id": 0, "codec": "AVC", "properties": {"width": 1920, "height": 1080, "pixel_dimensions": "1920x1080", "default_track": True}}],
        "audio_tracks": [{"id": 1, "codec": "AAC", "channels": 2, "channel_layout": "stereo", "language": "jpn", "language_ietf": "ja", "name": "Japanese", "default_track": True, "forced_track": False, "bit_rate": 192000}],
        "subtitle_tracks": [],
        "attachments": [],
    }
    monkeypatch.setattr("media_inspector.inspect_file_with_mkvmerge", lambda path: mock_inspection)

    result = scan_release_tracks(staging)

    assert result["video_count"] == 1
    assert result["has_external_tracks"] is True
    assert "internal_tracks" in result
    assert result["internal_tracks"]["duration"] == 1420.0
    assert len(result["internal_tracks"]["audio_tracks"]) == 1
    assert "external_groups" in result
    assert len(result["external_groups"]["audio"]) == 1
    assert result["external_groups"]["audio"][0]["name"] == "RUS Dub"


# --- 6. Real mkvmerge Subprocess Integration Tests ---

def test_real_mkvmerge_inspect_and_multiplex(tmp_path: Path):
    import subprocess

    # Check if mkvmerge is available on system
    try:
        proc = subprocess.run(["mkvmerge", "--version"], stdout=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            pytest.skip("mkvmerge binary not working")
    except FileNotFoundError:
        pytest.skip("mkvmerge binary not found")

    # Create dummy srt
    srt_file = tmp_path / "sub.srt"
    srt_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello World\n", encoding="utf-8")

    # Mux into initial mkv
    input_mkv = tmp_path / "input.mkv"
    res = subprocess.run(
        ["mkvmerge", "-o", str(input_mkv), str(srt_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert input_mkv.exists()

    # Real inspect_file_with_mkvmerge on real file
    inspection = inspect_file_with_mkvmerge(input_mkv)
    assert inspection["format"] == "mkv"
    assert inspection["duration"] > 0
    assert len(inspection["subtitle_tracks"]) >= 1

    # Now create external subtitle and multiplex using build_advanced_mkvmerge_cmd
    ext_sub = tmp_path / "ext_sub.srt"
    ext_sub.write_text("1\n00:00:00,500 --> 00:00:02,000\nExternal Sub\n", encoding="utf-8")

    output_mkv = tmp_path / "output.mkv"
    cmd = build_advanced_mkvmerge_cmd(
        output_file=output_mkv,
        video_file=input_mkv,
        keep_internal_audio_ids=None,
        keep_internal_subtitle_ids=[0],
        external_subtitles=[
            {
                "path": ext_sub,
                "track_name": "External English",
                "language": "eng",
                "is_default": True,
                "is_forced": False,
                "sync_ms": 100,
            }
        ],
        track_order=["0:0", "1:0"],
    )

    mux_res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert mux_res.returncode == 0
    assert output_mkv.exists()

    # Inspect final muxed mkv
    final_inspection = inspect_file_with_mkvmerge(output_mkv)
    assert len(final_inspection["subtitle_tracks"]) == 2
    # Second subtitle track should have name 'External English' and language 'eng'
    ext_track = final_inspection["subtitle_tracks"][1]
    assert ext_track["name"] == "External English"
    assert ext_track["language"] == "eng"
    assert ext_track["default_track"] is True


# --- 7. FastAPI API Endpoints Test ---

def test_mux_api_endpoints(tmp_path: Path, monkeypatch):
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))

    import main
    from fastapi.testclient import TestClient

    # Mock database job
    fake_job = {
        "id": 99,
        "torrent_hash": "hash99",
        "media_type": "anime",
        "title": "Test Show",
        "plex_title": "Test Show",
        "status": "needs_mux",
        "staging_path": str(tmp_path / "fake_staging"),
        "mux_result_path": "",
        "external_tracks": {
            "root_path": str(tmp_path / "fake_staging"),
            "internal_tracks": {
                "format": "mkv",
                "duration": 1200.0,
                "video_tracks": [{"id": 0, "codec": "AVC"}],
                "audio_tracks": [{"id": 1, "codec": "AAC"}],
                "subtitle_tracks": [],
                "attachments": [],
            },
            "external_groups": {"audio": [], "subtitles": []},
        },
        "selected_tracks": {},
        "error": "",
    }

    monkeypatch.setattr(main, "get_job_by_id", lambda jid: fake_job if jid == 99 else None)
    monkeypatch.setattr(database, "get_job_by_id", lambda jid: fake_job if jid == 99 else None)
    monkeypatch.setattr(main, "update_mux_selection", lambda **kw: None)

    client = TestClient(main.app)

    # 1. Test GET /api/jobs/99/tracks
    resp = client.get("/api/jobs/99/tracks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "internal_tracks" in data
    assert data["internal_tracks"]["duration"] == 1200.0

    # 2. Test GET /api/jobs/99/mux-progress when not active
    resp = client.get("/api/jobs/99/mux-progress")
    assert resp.status_code == 200
    progress = resp.json()
    assert progress["job_id"] == 99
    assert progress["status"] == "needs_mux"
    assert progress["overall_percent"] == 0.0

    # 3. Test GET /api/jobs/99/mux-progress when active in tracker
    tracker_state = progress_tracker.start_job(job_id=99, total_files=4, first_file_name="ep1.mkv")
    progress_tracker.update_progress(job_id=99, current_percent=50.0)
    resp = client.get("/api/jobs/99/mux-progress")
    assert resp.status_code == 200
    active_progress = resp.json()
    assert active_progress["status"] == "muxing"
    assert active_progress["current_percent"] == 50.0
    assert active_progress["overall_percent"] == 12.5  # 50% of 1st out of 4 = 12.5%
    progress_tracker.clear_job(99)

    # 4. Test POST /api/jobs/99/start-mux returns immediately with "started" status
    fake_staging = tmp_path / "fake_staging"
    fake_staging.mkdir(parents=True, exist_ok=True)
    fake_video = fake_staging / "ep01.mkv"
    fake_video.write_bytes(b"dummy")

    async def fake_run_mux(*args, **kwargs):
        return {}

    monkeypatch.setattr(main, "run_mux_job_async", fake_run_mux)

    resp = client.post(
        "/api/jobs/99/start-mux",
        data={"selected_tracks_json": json.dumps({"audio": []})},
    )
    assert resp.status_code == 200
    start_data = resp.json()
    assert start_data["status"] == "started"
    assert start_data["job_id"] == 99
    assert "Muxing started in background" in start_data["message"]
    progress_tracker.clear_job(99)
