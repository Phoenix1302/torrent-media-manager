import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def add_column_if_missing(conn, table_name: str, column_name: str, column_sql: str):
    if not column_exists(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def init_db():
    conn = get_conn()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torrent_hash TEXT UNIQUE NOT NULL,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            plex_title TEXT NOT NULL,
            year TEXT DEFAULT '',
            season INTEGER DEFAULT 1,
            episode INTEGER DEFAULT 1,
            episode_title TEXT DEFAULT '',
            season_pack INTEGER DEFAULT 0,
            start_episode INTEGER DEFAULT 1,
            status TEXT DEFAULT 'waiting',
            error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            imported_at TEXT DEFAULT '',
            staging_path TEXT DEFAULT '',
            mux_result_path TEXT DEFAULT '',
            external_tracks_json TEXT DEFAULT '',
            selected_tracks_json TEXT DEFAULT '',
            file_selection_required INTEGER DEFAULT 0,
            file_selection_done INTEGER DEFAULT 0,
            selected_file_ids_json TEXT DEFAULT ''
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torrent_hash TEXT NOT NULL,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            plex_title TEXT NOT NULL,
            year TEXT DEFAULT '',
            season INTEGER DEFAULT 1,
            episode INTEGER DEFAULT 1,
            imported_at TEXT NOT NULL,
            details_json TEXT DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_history_imported_at ON import_history(imported_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_history_hash ON import_history(torrent_hash);")

    # Миграции для уже существующей базы app.db.
    add_column_if_missing(conn, "import_jobs", "staging_path", "staging_path TEXT DEFAULT ''")
    add_column_if_missing(conn, "import_jobs", "mux_result_path", "mux_result_path TEXT DEFAULT ''")
    add_column_if_missing(conn, "import_jobs", "external_tracks_json", "external_tracks_json TEXT DEFAULT ''")
    add_column_if_missing(conn, "import_jobs", "selected_tracks_json", "selected_tracks_json TEXT DEFAULT ''")
    add_column_if_missing(conn, "import_jobs", "file_selection_required", "file_selection_required INTEGER DEFAULT 0")
    add_column_if_missing(conn, "import_jobs", "file_selection_done", "file_selection_done INTEGER DEFAULT 0")
    add_column_if_missing(conn, "import_jobs", "selected_file_ids_json", "selected_file_ids_json TEXT DEFAULT ''")

    conn.commit()
    conn.close()


def upsert_job(
    torrent_hash: str,
    media_type: str,
    title: str,
    plex_title: str,
    year: str = "",
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    season_pack: bool = False,
    start_episode: int = 1,
    file_selection_required: bool = False,
):
    now = datetime.now(timezone.utc).isoformat()
    initial_status = "waiting_files" if file_selection_required else "waiting"

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO import_jobs (
            torrent_hash,
            media_type,
            title,
            plex_title,
            year,
            season,
            episode,
            episode_title,
            season_pack,
            start_episode,
            status,
            error,
            created_at,
            updated_at,
            staging_path,
            mux_result_path,
            external_tracks_json,
            selected_tracks_json,
            file_selection_required,
            file_selection_done,
            selected_file_ids_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', '', '', '', ?, 0, '')
        ON CONFLICT(torrent_hash) DO UPDATE SET
            media_type=excluded.media_type,
            title=excluded.title,
            plex_title=excluded.plex_title,
            year=excluded.year,
            season=excluded.season,
            episode=excluded.episode,
            episode_title=excluded.episode_title,
            season_pack=excluded.season_pack,
            start_episode=excluded.start_episode,
            status=excluded.status,
            error='',
            updated_at=excluded.updated_at,
            staging_path='',
            mux_result_path='',
            external_tracks_json='',
            selected_tracks_json='',
            file_selection_required=excluded.file_selection_required,
            file_selection_done=0,
            selected_file_ids_json=''
        """,
        (
            torrent_hash,
            media_type,
            title,
            plex_title,
            year,
            season,
            episode,
            episode_title,
            1 if season_pack else 0,
            start_episode,
            initial_status,
            now,
            now,
            1 if file_selection_required else 0,
        ),
    )
    conn.commit()
    conn.close()


def list_jobs(status: str | None = None):
    conn = get_conn()

    if status:
        rows = conn.execute(
            "SELECT * FROM import_jobs WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM import_jobs ORDER BY created_at DESC"
        ).fetchall()

    conn.close()
    return [row_to_dict(row) for row in rows]


def get_job_by_id(job_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM import_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    return row_to_dict(row)


def get_job_by_hash(torrent_hash: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM import_jobs WHERE torrent_hash = ?",
        (torrent_hash,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    return row_to_dict(row)


def get_waiting_jobs():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM import_jobs
        WHERE status IN ('waiting', 'error')
        ORDER BY created_at ASC
        """
    ).fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


def get_queued_space_jobs():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM import_jobs
        WHERE status = 'queued_space'
        ORDER BY updated_at ASC, created_at ASC
        """
    ).fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


def set_job_status(torrent_hash: str, status: str, error: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    imported_at = now if status == "imported" else ""

    conn = get_conn()
    conn.execute(
        """
        UPDATE import_jobs
        SET status = ?,
            error = ?,
            updated_at = ?,
            imported_at = COALESCE(NULLIF(?, ''), imported_at)
        WHERE torrent_hash = ?
        """,
        (status, error, now, imported_at, torrent_hash),
    )
    conn.commit()
    conn.close()


def set_job_status_by_id(job_id: int, status: str, error: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    imported_at = now if status == "imported" else ""

    conn = get_conn()
    conn.execute(
        """
        UPDATE import_jobs
        SET status = ?,
            error = ?,
            updated_at = ?,
            imported_at = COALESCE(NULLIF(?, ''), imported_at)
        WHERE id = ?
        """,
        (status, error, now, imported_at, job_id),
    )
    conn.commit()
    conn.close()


def set_mux_info(
    torrent_hash: str,
    status: str,
    staging_path: str,
    mux_result_path: str = "",
    external_tracks: dict | None = None,
    selected_tracks: dict | None = None,
    error: str = "",
):
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    conn.execute(
        """
        UPDATE import_jobs
        SET status = ?,
            staging_path = ?,
            mux_result_path = ?,
            external_tracks_json = ?,
            selected_tracks_json = ?,
            error = ?,
            updated_at = ?
        WHERE torrent_hash = ?
        """,
        (
            status,
            staging_path,
            mux_result_path,
            json.dumps(external_tracks or {}, ensure_ascii=False),
            json.dumps(selected_tracks or {}, ensure_ascii=False),
            error,
            now,
            torrent_hash,
        ),
    )
    conn.commit()
    conn.close()


def update_mux_selection(
    job_id: int,
    status: str,
    mux_result_path: str = "",
    selected_tracks: dict | None = None,
    error: str = "",
):
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    conn.execute(
        """
        UPDATE import_jobs
        SET status = ?,
            mux_result_path = COALESCE(NULLIF(?, ''), mux_result_path),
            selected_tracks_json = ?,
            error = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            mux_result_path,
            json.dumps(selected_tracks or {}, ensure_ascii=False),
            error,
            now,
            job_id,
        ),
    )
    conn.commit()
    conn.close()


def set_file_selection_result(
    job_id: int,
    selected_file_ids: list[int],
    status: str = "waiting",
):
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    conn.execute(
        """
        UPDATE import_jobs
        SET status = ?,
            file_selection_done = 1,
            selected_file_ids_json = ?,
            error = '',
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps([int(item) for item in selected_file_ids], ensure_ascii=False),
            now,
            job_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_job(job_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM import_jobs WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    conn.close()


def record_import_history(job: dict, details: dict | None = None) -> dict:
    """Records an imported job into the persistent import_history table."""
    now = datetime.now(timezone.utc).isoformat()
    torrent_hash = job.get("torrent_hash") or ""
    media_type = job.get("media_type") or ""
    title = job.get("title") or ""
    plex_title = job.get("plex_title") or title
    year = str(job.get("year") or "")
    season = int(job.get("season", 1) or 1)
    episode = int(job.get("episode", 1) or 1)
    imported_at = job.get("imported_at") or now
    details_json = json.dumps(details or {}, ensure_ascii=False)

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO import_history (
            torrent_hash,
            media_type,
            title,
            plex_title,
            year,
            season,
            episode,
            imported_at,
            details_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            torrent_hash,
            media_type,
            title,
            plex_title,
            year,
            season,
            episode,
            imported_at,
            details_json,
        ),
    )
    history_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": history_id,
        "torrent_hash": torrent_hash,
        "media_type": media_type,
        "title": title,
        "plex_title": plex_title,
        "year": year,
        "season": season,
        "episode": episode,
        "imported_at": imported_at,
        "details": details or {},
    }


def list_history(limit: int = 50) -> list[dict]:
    """Retrieves the most recent imported items from import_history."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM import_history
        ORDER BY imported_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        item = dict(row)
        item["details"] = parse_json_field(item.get("details_json"))
        result.append(item)
    return result


def row_to_dict(row):
    data = dict(row)

    data["season_pack"] = bool(data.get("season_pack"))
    data["file_selection_required"] = bool(data.get("file_selection_required"))
    data["file_selection_done"] = bool(data.get("file_selection_done"))

    data["external_tracks"] = parse_json_field(data.get("external_tracks_json"))
    data["selected_tracks"] = parse_json_field(data.get("selected_tracks_json"))
    data["selected_file_ids"] = parse_json_list_field(data.get("selected_file_ids_json"))

    return data


def parse_json_field(value):
    if not value:
        return {}

    try:
        return json.loads(value)
    except Exception:
        return {}


def parse_json_list_field(value):
    if not value:
        return []

    try:
        parsed = json.loads(value)

        if isinstance(parsed, list):
            return parsed

        return []
    except Exception:
        return []
