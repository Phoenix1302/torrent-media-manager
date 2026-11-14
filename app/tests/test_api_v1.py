import asyncio
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# Ensure app directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database
import main
import qbit
from torrent_parser import bencode


# --- Helper to synthesize a valid .torrent file in memory ---
def create_synthesized_torrent(
    name: str = "Test.Movie.2024.1080p.WEBRip.x264.mkv",
    length: int = 1048576,
    piece_length: int = 262144,
) -> tuple[bytes, str]:
    """Generates valid bencoded .torrent bytes and returns (raw_bytes, info_hash_hex)."""
    piece_count = (length + piece_length - 1) // piece_length
    pieces = b"".join(hashlib.sha1(f"piece_{i}".encode()).digest() for i in range(piece_count))

    info_dict = {
        b"name": name.encode("utf-8"),
        b"length": length,
        b"piece length": piece_length,
        b"pieces": pieces,
    }
    torrent_dict = {
        b"announce": b"http://tracker.example.com/announce",
        b"created by": b"Torrent Media Manager Test",
        b"info": info_dict,
    }

    raw_info = bencode(info_dict)
    info_hash = hashlib.sha1(raw_info).hexdigest()
    raw_torrent = bencode(torrent_dict)
    return raw_torrent, info_hash


# --- 1. SQLite WAL Mode and Import History Tests ---

def test_sqlite_wal_mode_and_pragmas(tmp_path, monkeypatch):
    test_db = tmp_path / "test_wal.db"
    monkeypatch.setattr(database, "DB_PATH", str(test_db))

    database.init_db()

    conn = database.get_conn()
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
    conn.close()

    assert str(journal_mode).lower() == "wal"
    assert int(synchronous) == 1  # 1 == NORMAL in SQLite


def test_import_history_crud(tmp_path, monkeypatch):
    test_db = tmp_path / "test_history.db"
    monkeypatch.setattr(database, "DB_PATH", str(test_db))

    database.init_db()

    job1 = {
        "torrent_hash": "aabbccddeeff00112233",
        "media_type": "movie",
        "title": "Movie 1",
        "plex_title": "Movie 1 (2023)",
        "year": "2023",
        "season": 1,
        "episode": 1,
    }
    rec1 = database.record_import_history(job1, details={"mode": "normal", "imported_files": 1})
    assert rec1["id"] is not None
    assert rec1["torrent_hash"] == "aabbccddeeff00112233"
    assert rec1["plex_title"] == "Movie 1 (2023)"

    job2 = {
        "torrent_hash": "11223344556677889900",
        "media_type": "tv",
        "title": "Show S01E05",
        "plex_title": "Show",
        "year": "2022",
        "season": 1,
        "episode": 5,
    }
    rec2 = database.record_import_history(job2, details={"mode": "muxed", "tracks": ["ru", "en"]})
    assert rec2["id"] is not None

    history = database.list_history(limit=10)
    assert len(history) == 2
    assert history[0]["torrent_hash"] == "11223344556677889900"
    assert history[0]["details"]["mode"] == "muxed"
    assert history[1]["torrent_hash"] == "aabbccddeeff00112233"
    assert history[1]["details"]["mode"] == "normal"

    # Test limit
    limited = database.list_history(limit=1)
    assert len(limited) == 1
    assert limited[0]["torrent_hash"] == "11223344556677889900"


def test_get_history_api(tmp_path, monkeypatch):
    test_db = tmp_path / "test_api_hist.db"
    monkeypatch.setattr(database, "DB_PATH", str(test_db))
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "start_watcher", lambda: None)

    database.init_db()
    database.record_import_history(
        {
            "torrent_hash": "hash123",
            "media_type": "movie",
            "title": "Historical Movie",
            "plex_title": "Historical Movie",
            "year": "2021",
            "season": 1,
            "episode": 1,
        },
        details={"result": "ok"},
    )

    client = TestClient(main.app)
    resp = client.get("/api/history?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["count"] == 1
    assert data["items"][0]["torrent_hash"] == "hash123"
    assert data["items"][0]["title"] == "Historical Movie"


# --- 2. Persistent qBittorrent Session & Connection Pooling Tests ---

def test_qbit_session_reuse_and_reset(monkeypatch):
    qbit.reset_session()

    fake_login_resp = MagicMock()
    fake_login_resp.status_code = 200
    fake_login_resp.text = "Ok."

    mock_post = MagicMock(return_value=fake_login_resp)

    with patch("requests.Session.post", mock_post):
        s1 = qbit.get_session()
        assert s1 is not None
        assert mock_post.call_count == 1  # Login performed once

        s2 = qbit.get_session()
        assert s1 is s2  # Exact same pooled session instance reused!
        assert mock_post.call_count == 1  # No duplicate login!

        # Force relogin
        s3 = qbit.get_session(force_relogin=True)
        assert s3 is s1
        assert mock_post.call_count == 2

    qbit.reset_session()
    assert qbit._SHARED_SESSION is None
    assert qbit._IS_LOGGED_IN is False


def test_qbit_add_torrent_file(monkeypatch):
    mock_post = MagicMock(return_value=MagicMock(status_code=200, text="Ok."))
    monkeypatch.setattr(qbit, "qbit_post", mock_post)

    file_bytes = b"fake torrent content"
    res = qbit.add_torrent_file(
        file_bytes=file_bytes,
        filename="custom.torrent",
        category="films",
        stopped=True,
    )

    assert res["status"] == "ok"
    assert res["stopped"] is True
    assert res["filename"] == "custom.torrent"

    mock_post.assert_called_once()
    path, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert path == "/api/v2/torrents/add"
    assert kwargs["data"]["category"] == "films"
    assert kwargs["data"]["paused"] == "true"
    assert kwargs["data"]["autoTMM"] == "false"
    assert kwargs["files"]["torrents"][0] == "custom.torrent"
    assert kwargs["files"]["torrents"][1] == file_bytes


def test_qbit_post_retry_on_expired_session(monkeypatch):
    qbit.reset_session()

    # Session mock: first call returns 403 Forbidden, second returns 200 Ok
    resp_forbidden = MagicMock(status_code=403, text="Forbidden")
    resp_ok = MagicMock(status_code=200, text="Ok.")

    calls = []

    def mock_post_impl(url, *args, **kwargs):
        if "login" in url:
            return MagicMock(status_code=200, text="Ok.")
        calls.append(url)
        if len(calls) == 1:
            return resp_forbidden
        return resp_ok

    session_mock = MagicMock()
    session_mock.post = MagicMock(side_effect=mock_post_impl)
    monkeypatch.setattr(qbit, "_SHARED_SESSION", session_mock)
    monkeypatch.setattr(qbit, "_IS_LOGGED_IN", True)

    with patch.object(qbit, "get_session", wraps=qbit.get_session) as spy_get_session:
        res = qbit.qbit_post("/api/v2/torrents/action", data={"test": 1})
        assert res.status_code == 200

    qbit.reset_session()


# --- 3. /api/analyze with .torrent File & Validations ---

@pytest.mark.asyncio
async def test_api_analyze_with_torrent_file(monkeypatch):
    raw_torrent, expected_hash = create_synthesized_torrent(
        name="The.Matrix.1999.1080p.BluRay.x264.mkv"
    )

    async def fake_search(query, tmdb_type):
        return [{"id": 603, "title": "The Matrix", "release_date": "1999-03-30"}]

    monkeypatch.setattr(main, "search_tmdb", fake_search)

    client = TestClient(main.app)
    files = {
        "torrent_file": ("matrix.torrent", io.BytesIO(raw_torrent), "application/x-bittorrent"),
    }
    data = {
        "media_type": "movie",
    }

    resp = client.post("/api/analyze", data=data, files=files)
    assert resp.status_code == 200
    res = resp.json()

    assert res["status"] == "ok"
    assert res["info_hash"] == expected_hash
    assert res["raw_name"] == "The.Matrix.1999.1080p.BluRay.x264.mkv"
    assert res["media_type"] == "movie"
    assert len(res["results"]) == 1
    assert res["results"][0]["id"] == 603


def test_api_analyze_validation_errors():
    client = TestClient(main.app)

    # 1. Neither magnet nor file
    resp = client.post("/api/analyze", data={"media_type": "movie"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert "Either magnet or torrent_file" in resp.json()["message"]

    # 2. Both magnet and file
    files = {
        "torrent_file": ("test.torrent", io.BytesIO(b"dummy"), "application/x-bittorrent"),
    }
    resp = client.post(
        "/api/analyze",
        data={"magnet": "magnet:?xt=urn:btih:12345", "media_type": "movie"},
        files=files,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert "not both" in resp.json()["message"]

    # 3. Empty/malformed torrent file
    files_empty = {
        "torrent_file": ("test.torrent", io.BytesIO(b""), "application/x-bittorrent"),
    }
    resp = client.post("/api/analyze", data={"media_type": "movie"}, files=files_empty)
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert "empty" in resp.json()["message"].lower()

    files_bad = {
        "torrent_file": ("bad.torrent", io.BytesIO(b"invalid bencode data"), "application/x-bittorrent"),
    }
    resp = client.post("/api/analyze", data={"media_type": "movie"}, files=files_bad)
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert "Failed to parse .torrent file" in resp.json()["message"]


# --- 4. /api/add and /api/add-selective with .torrent File ---

def test_api_add_with_torrent_file(monkeypatch):
    raw_torrent, expected_hash = create_synthesized_torrent(
        name="Interstellar.2014.1080p.mkv"
    )

    mock_add_qbit = MagicMock(return_value={"status": "ok", "message": "Success"})
    mock_upsert = MagicMock()

    monkeypatch.setattr(main, "add_torrent_file", mock_add_qbit)
    monkeypatch.setattr(main, "upsert_job", mock_upsert)

    client = TestClient(main.app)
    files = {
        "torrent_file": ("interstellar.torrent", io.BytesIO(raw_torrent), "application/x-bittorrent"),
    }
    data = {
        "media_type": "movie",
        "plex_title": "Interstellar",
        "year": "2014",
    }

    resp = client.post("/api/add", data=data, files=files)
    assert resp.status_code == 200
    res = resp.json()

    assert res["status"] == "added"
    assert res["job"]["torrent_hash"] == expected_hash
    assert res["job"]["plex_title"] == "Interstellar"

    mock_add_qbit.assert_called_once()
    assert mock_add_qbit.call_args[1]["stopped"] is False
    assert mock_add_qbit.call_args[1]["category"] == "films"

    mock_upsert.assert_called_once()
    assert mock_upsert.call_args[1]["torrent_hash"] == expected_hash
    assert mock_upsert.call_args[1]["file_selection_required"] is False


def test_api_add_selective_with_torrent_file(monkeypatch):
    raw_torrent, expected_hash = create_synthesized_torrent(
        name="Breaking.Bad.S01.720p.BluRay.mkv"
    )

    mock_add_qbit = MagicMock(return_value={"status": "ok", "message": "Success", "stopped": True})
    mock_upsert = MagicMock()

    monkeypatch.setattr(main, "add_torrent_file", mock_add_qbit)
    monkeypatch.setattr(main, "upsert_job", mock_upsert)

    client = TestClient(main.app)
    files = {
        "torrent_file": ("breaking_bad.torrent", io.BytesIO(raw_torrent), "application/x-bittorrent"),
    }
    data = {
        "media_type": "tv",
        "plex_title": "Breaking Bad",
        "season": 1,
        "season_pack": "true",
    }

    resp = client.post("/api/add-selective", data=data, files=files)
    assert resp.status_code == 200
    res = resp.json()

    assert res["status"] == "waiting_files"
    assert res["job"]["torrent_hash"] == expected_hash
    assert res["job"]["file_selection_required"] is True

    mock_add_qbit.assert_called_once()
    assert mock_add_qbit.call_args[1]["stopped"] is True
    assert mock_add_qbit.call_args[1]["category"] == "serials"

    mock_upsert.assert_called_once()
    assert mock_upsert.call_args[1]["torrent_hash"] == expected_hash
    assert mock_upsert.call_args[1]["file_selection_required"] is True


def test_api_add_validation_errors():
    client = TestClient(main.app)

    # 1. Neither magnet nor file
    resp = client.post("/api/add", data={"media_type": "movie"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"

    # 2. Both magnet and file
    files = {
        "torrent_file": ("test.torrent", io.BytesIO(b"dummy"), "application/x-bittorrent"),
    }
    resp = client.post(
        "/api/add",
        data={"magnet": "magnet:?xt=urn:btih:12345", "media_type": "movie"},
        files=files,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


# --- 5. Server-Sent Events (SSE) /api/events Tests ---

def test_sse_events_snapshot(monkeypatch):
    monkeypatch.setattr(main, "list_torrents", lambda: [{"hash": "abc", "name": "Tor 1", "progress": 85.0}])
    monkeypatch.setattr(main, "list_jobs", lambda: [{"id": 1, "title": "Job 1", "status": "waiting"}])
    monkeypatch.setattr(main.progress_tracker, "get_all_active", lambda: {1: {"job_id": 1, "overall_percent": 42.0}})
    monkeypatch.setattr(main, "disk", lambda: {"status": "ok", "total": 1000, "used": 400, "free": 600})

    client = TestClient(main.app)
    with client.stream("GET", "/api/events") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Read first SSE event
        lines = []
        for line in response.iter_lines():
            if line:
                lines.append(line)
                break

        assert len(lines) == 1
        assert lines[0].startswith("data: ")

        raw_json = lines[0][len("data: ") :]
        data = json.loads(raw_json)

        assert data["type"] == "tick"
        assert len(data["torrents"]) == 1
        assert data["torrents"][0]["name"] == "Tor 1"
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["id"] == 1
        assert data["active_mux"]["1"]["overall_percent"] == 42.0
        assert data["disk"]["status"] == "ok"


# --- 6. Import History Recorded on Successful Import ---

def test_history_recorded_on_skip_mux(monkeypatch, tmp_path):
    test_db = tmp_path / "test_skip_mux.db"
    monkeypatch.setattr(database, "DB_PATH", str(test_db))
    database.init_db()

    fake_job = {
        "id": 10,
        "torrent_hash": "skip_mux_hash_123",
        "media_type": "movie",
        "title": "Skip Mux Movie",
        "plex_title": "Skip Mux Movie",
        "year": "2023",
        "season": 1,
        "episode": 1,
        "episode_title": "",
        "season_pack": False,
        "start_episode": 1,
        "staging_path": str(tmp_path / "staging_10"),
    }

    monkeypatch.setattr(main, "get_job_by_id", lambda jid: fake_job if jid == 10 else None)
    monkeypatch.setattr(main, "import_from_path", lambda **kw: {"imported": 1})
    monkeypatch.setattr(main, "cleanup_path", lambda p: {"cleaned": True})

    client = TestClient(main.app)
    resp = client.post("/api/jobs/10/skip-mux")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    history = database.list_history()
    assert len(history) == 1
    assert history[0]["torrent_hash"] == "skip_mux_hash_123"
    assert history[0]["details"]["mode"] == "skip_mux"


def test_history_recorded_on_confirm_mux(monkeypatch, tmp_path):
    test_db = tmp_path / "test_confirm_mux.db"
    monkeypatch.setattr(database, "DB_PATH", str(test_db))
    database.init_db()

    fake_job = {
        "id": 20,
        "torrent_hash": "confirm_mux_hash_456",
        "media_type": "tv",
        "title": "Muxed TV",
        "plex_title": "Muxed TV Show",
        "year": "2024",
        "season": 1,
        "episode": 2,
        "episode_title": "Ep 2",
        "season_pack": False,
        "start_episode": 1,
        "staging_path": str(tmp_path / "staging_20"),
    }

    monkeypatch.setattr(main, "get_job_by_id", lambda jid: fake_job if jid == 20 else None)
    monkeypatch.setattr(main, "import_from_staging_result", lambda **kw: {"imported": 1})
    monkeypatch.setattr(main, "cleanup_path", lambda p: {"cleaned": True})

    client = TestClient(main.app)
    resp = client.post("/api/jobs/20/confirm-mux")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    history = database.list_history()
    assert len(history) == 1
    assert history[0]["torrent_hash"] == "confirm_mux_hash_456"
    assert history[0]["details"]["mode"] == "confirm_mux"
