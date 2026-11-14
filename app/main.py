import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from guessit import guessit

from config import APP_VERSION, UMASK, PUID, PGID
from database import (
    delete_job,
    get_job_by_id,
    init_db,
    list_history,
    list_jobs,
    record_import_history,
    set_file_selection_result,
    set_job_status_by_id,
    update_mux_selection,
    upsert_job,
)
from importer import cleanup_path, import_from_path, import_from_staging_result
from media_scan import scan_release
from muxer import get_mux_progress, mux_release, progress_tracker, run_mux_job_async
from qbit import (
    add_magnet,
    add_magnet_and_find_hash,
    add_torrent_file,
    delete_torrent,
    list_torrent_files,
    list_torrents,
    select_torrent_files,
)
from space_manager import (
    calculate_selected_files_size,
    has_space_for_download,
)
from tmdb import search_tmdb
from title_cleaner import (
    clean_release_title,
    extract_title_candidates,
    recommend_media_type,
)
from torrent_parser import parse_torrent_bytes
from watcher import start_watcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        os.umask(UMASK)
    except Exception:
        pass
    init_db()
    start_watcher()
    yield


app = FastAPI(title="Torrent Media Manager", version="1.1.1", lifespan=lifespan)
# FastAPI(title="Torrent Media Manager", version="1.1.1")

app.mount("/static", StaticFiles(directory="static"), name="static")


def extract_info_hash_from_magnet(magnet: str) -> str:
    if not magnet:
        return ""
    match = re.search(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet)
    if match:
        return match.group(1).lower()
    return ""


def extract_name_from_magnet(magnet: str) -> str:
    try:
        parsed = urlparse(magnet)
        params = parse_qs(parsed.query)
        dn = params.get("dn", [""])[0]

        if dn:
            value = dn

            for _ in range(3):
                decoded = unquote_plus(value)
                if decoded == value:
                    break
                value = decoded

            return value.strip()
    except Exception:
        pass

    return magnet.strip()




def parse_season_episode(raw_name: str) -> dict:
    text = raw_name.strip()
    low = text.lower()

    result = {
        "season": None,
        "episode": None,
        "start_episode": None,
        "end_episode": None,
        "season_pack": False,
    }

    match = re.search(
        r"\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})(?:\s*[-–]\s*(?:E)?(?P<end_episode>\d{1,3}))?",
        text,
        re.IGNORECASE,
    )
    if match:
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        end_episode = match.group("end_episode")

        result["season"] = season
        result["episode"] = episode
        result["start_episode"] = episode

        if end_episode:
            result["end_episode"] = int(end_episode)
            result["season_pack"] = True

        return result

    match = re.search(r"\bS(?P<season>\d{1,2})\b", text, re.IGNORECASE)
    if match:
        result["season"] = int(match.group("season"))
        result["season_pack"] = True
        result["start_episode"] = 1
        return result

    match = re.search(r"\bseason\s*(?P<season>\d{1,2})\b", low, re.IGNORECASE)
    if match:
        result["season"] = int(match.group("season"))
        result["season_pack"] = True
        result["start_episode"] = 1
        return result

    match = re.search(r"\bсезон\s*:?\s*(?P<season>\d{1,2})\b", low, re.IGNORECASE)
    if match:
        result["season"] = int(match.group("season"))
        result["season_pack"] = True
        result["start_episode"] = 1
        return result

    match = re.search(r"\b(?P<season>\d{1,2})\s*сезон\b", low, re.IGNORECASE)
    if match:
        result["season"] = int(match.group("season"))
        result["season_pack"] = True
        result["start_episode"] = 1
        return result

    match = re.search(
        r"(?:серии|серия|episodes?)\s*:?\s*(?P<start>\d{1,3})\s*[-–]\s*(?P<end>\d{1,3})",
        low,
        re.IGNORECASE,
    )
    if match:
        result["episode"] = int(match.group("start"))
        result["start_episode"] = int(match.group("start"))
        result["end_episode"] = int(match.group("end"))
        result["season_pack"] = True

    match = re.search(
        r"\b(?P<start>\d{1,3})\s*[-–]\s*(?P<end>\d{1,3})\s*(?:серии|серия|episodes?)\b",
        low,
        re.IGNORECASE,
    )
    if match:
        result["episode"] = int(match.group("start"))
        result["start_episode"] = int(match.group("start"))
        result["end_episode"] = int(match.group("end"))
        result["season_pack"] = True

    return result


def get_tmdb_query(raw_name: str, manual_query: str = "") -> str:
    if manual_query.strip():
        return manual_query.strip()

    cleaned = clean_release_title(raw_name)

    try:
        parsed = guessit(raw_name)
        guessed = parsed.get("title")

        if guessed:
            guessed_str = str(guessed).strip()

            bad_fragments = [
                "боевик",
                "комедия",
                "драма",
                "приключения",
                "ужасы",
                "триллер",
                "сша",
                "великобритания",
                "франция",
                "германия",
            ]

            if len(guessed_str) >= 3 and not any(x in guessed_str.lower() for x in bad_fragments):
                if cleaned and len(cleaned) >= 3:
                    return cleaned

                return guessed_str
    except Exception:
        pass

    return cleaned


def normalize_media_type(media_type: str) -> str:
    category_to_type = {
        "films": "movie",
        "serials": "tv",
        "anime": "anime",
        "mults": "cartoon",
    }
    if media_type in category_to_type:
        return category_to_type[media_type]
    allowed = {"movie", "tv", "anime", "cartoon"}
    return media_type if media_type in allowed else "movie"


def build_job_metadata(job: dict):
    title = job.get("plex_title") or job.get("title")

    return {
        "title": title,
        "original_title": job.get("title") or title,
        "year": job.get("year") or "",
    }


def parse_selected_tracks_json(value: str):
    if not value.strip():
        return {
            "audio": [],
            "subtitles": [],
        }

    data = json.loads(value)

    if "audio" not in data:
        data["audio"] = []

    if "subtitles" not in data:
        data["subtitles"] = []

    return data


def parse_selected_file_ids(value: str):
    if not value.strip():
        return []

    try:
        parsed = json.loads(value)
    except Exception:
        parsed = [
            item.strip()
            for item in value.split(",")
            if item.strip()
        ]

    if not isinstance(parsed, list):
        return []

    result = []

    for item in parsed:
        try:
            result.append(int(item))
        except Exception:
            pass

    return result


def get_category_for_media_type(media_type: str):
    category_map = {
        "movie": "films",
        "tv": "serials",
        "anime": "anime",
        "cartoon": "mults",
    }

    return category_map.get(media_type, "media")


def find_new_torrent_hash(before_hashes: set[str], raw_name: str = ""):
    torrent_hash = ""

    for _ in range(10):
        after_torrents = list_torrents()
        new_torrents = [
            item for item in after_torrents
            if item["hash"] not in before_hashes
        ]

        if new_torrents:
            new_torrents = sorted(
                new_torrents,
                key=lambda item: item.get("added_on", 0),
                reverse=True,
            )
            torrent_hash = new_torrents[0]["hash"]
            break

        if raw_name:
            for item in after_torrents:
                item_name = item.get("name") or ""

                if item_name == raw_name or raw_name in item_name or item_name in raw_name:
                    torrent_hash = item["hash"]
                    break

        if torrent_hash:
            break

        time.sleep(1)

    return torrent_hash


def format_bytes(value: int | float):
    try:
        value = float(value)
    except Exception:
        return ""

    if value < 0:
        value = 0

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0

    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1

    if index == 0:
        return f"{int(value)} {units[index]}"

    return f"{value:.2f} {units[index]}"


def get_main_data_disk_path():
    explicit_path = (
        os.environ.get("MAIN_DATA_DISK_PATH")
        or os.environ.get("MAIN_DATA_PATH")
        or ""
    ).strip()

    if explicit_path:
        return explicit_path

    staging_path = os.environ.get("STAGING_PATH", "/mnt/main_data/Media/InComplete").strip()

    if staging_path:
        path = Path(staging_path)

        if path.name.lower() == "incomplete":
            return str(path.parent)

        return str(path)

    return "/mnt/main_data/Media"


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": "torrent-media-manager",
        "version": "1.1.1",
    }


@app.get("/api/disk")
def disk():
    path = get_main_data_disk_path()

    try:
        usage = shutil.disk_usage(path)
        total = int(usage.total)
        used = int(usage.used)
        free = int(usage.free)
        used_percent = round((used / total) * 100, 1) if total else 0
        free_percent = round((free / total) * 100, 1) if total else 0

        return {
            "status": "ok",
            "path": path,
            "total": total,
            "used": used,
            "free": free,
            "total_text": format_bytes(total),
            "used_text": format_bytes(used),
            "free_text": format_bytes(free),
            "used_percent": used_percent,
            "free_percent": free_percent,
        }

    except Exception as exc:
        return {
            "status": "error",
            "path": path,
            "message": str(exc),
        }




@app.post("/api/analyze")
async def analyze(
    magnet: str = Form(None),
    torrent_file: UploadFile = File(None),
    media_type: str = Form(...),
    manual_query: str = Form(""),
):
    has_file = torrent_file is not None and bool(getattr(torrent_file, "filename", None))
    has_magnet = bool(magnet and magnet.strip())

    if (has_file and has_magnet) or (not has_file and not has_magnet):
        return {
            "status": "error",
            "message": "Either magnet or torrent_file must be provided, but not both",
        }

    media_type = normalize_media_type(media_type)

    if has_file:
        file_bytes = await torrent_file.read()
        if not file_bytes:
            return {
                "status": "error",
                "message": "Uploaded .torrent file is empty",
            }
        try:
            parsed_torrent = parse_torrent_bytes(file_bytes)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to parse .torrent file: {exc}",
            }
        raw_name = parsed_torrent.get("name") or ""
        info_hash = parsed_torrent.get("info_hash") or ""
    else:
        raw_name = extract_name_from_magnet(magnet)
        info_hash = extract_info_hash_from_magnet(magnet)

    try:
        parsed = guessit(raw_name) if raw_name else {}
    except Exception:
        parsed = {}

    candidates = extract_title_candidates(raw_name)
    rec = recommend_media_type(raw_name, candidates)
    recommended_media_type = rec.category
    rec_reason = rec.reason

    try:
        query = get_tmdb_query(raw_name, manual_query)
    except Exception:
        query = manual_query or (candidates.primary if candidates else raw_name) or ""

    try:
        season_info = parse_season_episode(raw_name) if raw_name else {}
    except Exception:
        season_info = {}

    if media_type in {"movie", "cartoon"}:
        tmdb_type = "movie"
    else:
        tmdb_type = "tv"

    results = []
    tmdb_warning = ""

    try:
        try:
            results = await search_tmdb(str(query), tmdb_type, candidates=candidates)
        except TypeError:
            results = await search_tmdb(str(query), tmdb_type)

        if not results:
            from config import TMDB_API_KEY
            if not TMDB_API_KEY:
                tmdb_warning = "TMDB_API_KEY не задан в .env. Релиз можно добавить с ручным указанием названия."
            elif query:
                tmdb_warning = f"В TMDB ничего не найдено по запросу: '{query}'"
    except Exception as exc:
        tmdb_warning = f"Ошибка связи с TMDB: {exc}"

    return {
        "status": "ok",
        "raw_name": raw_name,
        "cleaned_query": query,
        "parsed": parsed,
        "season_info": season_info,
        "query": query,
        "media_type": media_type,
        "tmdb_type": tmdb_type,
        "results": results,
        "info_hash": info_hash,
        "tmdb_warning": tmdb_warning,
        "recommended_media_type": recommended_media_type,
        "recommendation_reason": rec_reason,
        "title_candidates": candidates,
    }


@app.post("/api/add")
async def add(
    magnet: str = Form(None),
    torrent_file: UploadFile = File(None),
    media_type: str = Form(...),
    title: str = Form(""),
    plex_title: str = Form(""),
    year: str = Form(""),
    season: int = Form(1),
    episode: int = Form(1),
    episode_title: str = Form(""),
    season_pack: bool = Form(False),
    start_episode: int = Form(1),
):
    has_file = torrent_file is not None and bool(getattr(torrent_file, "filename", None))
    has_magnet = bool(magnet and magnet.strip())

    if (has_file and has_magnet) or (not has_file and not has_magnet):
        return {
            "status": "error",
            "message": "Either magnet or torrent_file must be provided, but not both",
        }

    media_type = normalize_media_type(media_type)
    category = get_category_for_media_type(media_type)

    if has_file:
        file_bytes = await torrent_file.read()
        if not file_bytes:
            return {
                "status": "error",
                "message": "Uploaded .torrent file is empty",
            }
        try:
            parsed_torrent = parse_torrent_bytes(file_bytes)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to parse .torrent file: {exc}",
            }

        torrent_hash = parsed_torrent.get("info_hash") or ""
        if not torrent_hash:
            return {
                "status": "error",
                "message": "Unable to extract info_hash from .torrent file",
            }

        raw_name = parsed_torrent.get("name") or ""
        add_result = add_torrent_file(
            file_bytes=file_bytes,
            filename=torrent_file.filename or "upload.torrent",
            category=category,
            stopped=False,
        )
    else:
        before_torrents = list_torrents()
        before_hashes = {item["hash"] for item in before_torrents}

        add_result = add_magnet(magnet, category)

        raw_name = extract_name_from_magnet(magnet)
        torrent_hash = find_new_torrent_hash(before_hashes, raw_name)

    final_title = plex_title.strip() or title.strip() or clean_release_title(raw_name)

    if not torrent_hash:
        return {
            "status": "added_but_job_not_created",
            "message": "Torrent added, but hash was not found yet. Refresh torrents and create import job manually.",
            "qbit": add_result,
        }

    upsert_job(
        torrent_hash=torrent_hash,
        media_type=media_type,
        title=title.strip() or final_title,
        plex_title=final_title,
        year=year.strip(),
        season=season,
        episode=episode,
        episode_title=episode_title.strip(),
        season_pack=season_pack,
        start_episode=start_episode,
        file_selection_required=False,
    )

    return {
        "status": "added",
        "qbit": add_result,
        "job": {
            "torrent_hash": torrent_hash,
            "media_type": media_type,
            "plex_title": final_title,
            "year": year,
            "season": season,
            "episode": episode,
            "season_pack": season_pack,
            "start_episode": start_episode,
        },
    }


@app.post("/api/add-selective")
async def add_selective(
    magnet: str = Form(None),
    torrent_file: UploadFile = File(None),
    media_type: str = Form(...),
    title: str = Form(""),
    plex_title: str = Form(""),
    year: str = Form(""),
    season: int = Form(1),
    episode: int = Form(1),
    episode_title: str = Form(""),
    season_pack: bool = Form(False),
    start_episode: int = Form(1),
):
    has_file = torrent_file is not None and bool(getattr(torrent_file, "filename", None))
    has_magnet = bool(magnet and magnet.strip())

    if (has_file and has_magnet) or (not has_file and not has_magnet):
        return {
            "status": "error",
            "message": "Either magnet or torrent_file must be provided, but not both",
        }

    media_type = normalize_media_type(media_type)
    category = get_category_for_media_type(media_type)

    if has_file:
        file_bytes = await torrent_file.read()
        if not file_bytes:
            return {
                "status": "error",
                "message": "Uploaded .torrent file is empty",
            }
        try:
            parsed_torrent = parse_torrent_bytes(file_bytes)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to parse .torrent file: {exc}",
            }

        torrent_hash = parsed_torrent.get("info_hash") or ""
        if not torrent_hash:
            return {
                "status": "error",
                "message": "Unable to extract info_hash from .torrent file",
            }

        raw_name = parsed_torrent.get("name") or ""
        result = add_torrent_file(
            file_bytes=file_bytes,
            filename=torrent_file.filename or "upload.torrent",
            category=category,
            stopped=True,
        )
    else:
        before_torrents = list_torrents()
        before_hashes = {item["hash"] for item in before_torrents}

        result = add_magnet_and_find_hash(
            magnet=magnet,
            category=category,
            stopped=True,
            before_hashes=before_hashes,
        )

        torrent_hash = result.get("torrent_hash") or ""
        raw_name = extract_name_from_magnet(magnet)

    final_title = plex_title.strip() or title.strip() or clean_release_title(raw_name)

    if not torrent_hash:
        return {
            "status": "added_but_job_not_created",
            "message": "Torrent added in stopped mode, but hash was not found yet. Refresh torrents and create import job manually.",
            "qbit": result,
        }

    upsert_job(
        torrent_hash=torrent_hash,
        media_type=media_type,
        title=title.strip() or final_title,
        plex_title=final_title,
        year=year.strip(),
        season=season,
        episode=episode,
        episode_title=episode_title.strip(),
        season_pack=season_pack,
        start_episode=start_episode,
        file_selection_required=True,
    )

    return {
        "status": "waiting_files",
        "message": "Torrent added in stopped mode. Wait for metadata, then select files.",
        "qbit": result,
        "job": {
            "torrent_hash": torrent_hash,
            "media_type": media_type,
            "plex_title": final_title,
            "year": year,
            "season": season,
            "episode": episode,
            "season_pack": season_pack,
            "start_episode": start_episode,
            "file_selection_required": True,
        },
    }


@app.get("/api/torrents")
def torrents():
    return {
        "status": "ok",
        "items": list_torrents(),
    }


@app.get("/api/torrents/{torrent_hash}/files")
def torrent_files(torrent_hash: str):
    try:
        files = list_torrent_files(torrent_hash)

        return {
            "status": "ok",
            "torrent_hash": torrent_hash,
            "files": files,
            "count": len(files),
            "metadata_ready": len(files) > 0,
        }

    except Exception as exc:
        return {
            "status": "error",
            "torrent_hash": torrent_hash,
            "message": str(exc),
            "files": [],
            "count": 0,
            "metadata_ready": False,
        }


@app.delete("/api/torrents/{torrent_hash}")
def delete_qbit_torrent(
    torrent_hash: str,
    delete_files: bool = False,
):
    result = delete_torrent(
        torrent_hash=torrent_hash,
        delete_files=delete_files,
    )

    return {
        "status": "ok",
        "result": result,
    }


@app.get("/api/jobs")
def jobs():
    return {
        "status": "ok",
        "items": list_jobs(),
    }


@app.post("/api/jobs/trigger-check")
def trigger_jobs_check(background_tasks: BackgroundTasks):
    from watcher import run_auto_import_once
    background_tasks.add_task(run_auto_import_once)
    return {
        "status": "ok",
        "message": "Auto-import check triggered in background",
    }


@app.get("/api/history")
def history(limit: int = 50):
    items = list_history(limit=limit)
    return {
        "status": "ok",
        "items": items,
        "count": len(items),
    }


@app.get("/api/events")
async def sse_events(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break

            try:
                try:
                    torrent_list = list_torrents()
                except Exception:
                    torrent_list = []

                try:
                    job_list = list_jobs()
                except Exception:
                    job_list = []

                try:
                    active_mux = progress_tracker.get_all_active()
                except Exception:
                    active_mux = {}

                try:
                    disk_stat = disk()
                except Exception:
                    disk_stat = {}

                payload = {
                    "type": "tick",
                    "torrents": torrent_list,
                    "jobs": job_list,
                    "active_mux": active_mux,
                    "disk": disk_stat,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception:
                pass

            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs")
def create_job_for_existing_torrent(
    torrent_hash: str = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
    plex_title: str = Form(""),
    year: str = Form(""),
    season: int = Form(1),
    episode: int = Form(1),
    episode_title: str = Form(""),
    season_pack: bool = Form(False),
    start_episode: int = Form(1),
):
    media_type = normalize_media_type(media_type)
    final_title = plex_title.strip() or title.strip()

    upsert_job(
        torrent_hash=torrent_hash,
        media_type=media_type,
        title=title.strip(),
        plex_title=final_title,
        year=year.strip(),
        season=season,
        episode=episode,
        episode_title=episode_title.strip(),
        season_pack=season_pack,
        start_episode=start_episode,
        file_selection_required=False,
    )

    return {
        "status": "ok",
        "message": "Import job saved",
        "torrent_hash": torrent_hash,
    }


@app.delete("/api/jobs/{job_id}")
def delete_import_job(job_id: int):
    delete_job(job_id)

    return {
        "status": "ok",
        "message": "Import job deleted",
        "job_id": job_id,
    }


@app.post("/api/jobs/{job_id}/select-files")
def select_files_for_job(
    job_id: int,
    selected_file_ids_json: str = Form(...),
):
    job = get_job_by_id(job_id)

    if not job:
        return {
            "status": "error",
            "message": "Job not found",
            "job_id": job_id,
        }

    torrent_hash = job.get("torrent_hash") or ""

    if not torrent_hash:
        return {
            "status": "error",
            "message": "Job has no torrent_hash",
            "job_id": job_id,
        }

    selected_file_ids = parse_selected_file_ids(selected_file_ids_json)

    if not selected_file_ids:
        return {
            "status": "error",
            "message": "No selected files",
            "job_id": job_id,
        }

    try:
        selected_size_info = calculate_selected_files_size(
            torrent_hash=torrent_hash,
            selected_file_ids=selected_file_ids,
        )

        selected_size = int(selected_size_info.get("size") or 0)
        space_check = has_space_for_download(
            required_bytes=selected_size,
            exclude_hash=torrent_hash,
        )

        should_start_now = bool(space_check.get("ok"))

        result = select_torrent_files(
            torrent_hash=torrent_hash,
            selected_file_ids=selected_file_ids,
            start_after_selection=should_start_now,
        )

        next_status = "waiting" if should_start_now else "queued_space"

        set_file_selection_result(
            job_id=job_id,
            selected_file_ids=selected_file_ids,
            status=next_status,
        )

        if should_start_now:
            return {
                "status": "ok",
                "message": "Selected files were enabled and torrent was started",
                "job_id": job_id,
                "torrent_hash": torrent_hash,
                "selected_file_ids": selected_file_ids,
                "result": result,
                "space": space_check,
            }

        return {
            "status": "queued_space",
            "message": "Ожидает свободного места",
            "job_id": job_id,
            "torrent_hash": torrent_hash,
            "selected_file_ids": selected_file_ids,
            "result": result,
            "space": space_check,
        }

    except Exception as exc:
        set_job_status_by_id(job_id, "error", str(exc))

        return {
            "status": "error",
            "message": str(exc),
            "job_id": job_id,
        }

@app.get("/api/jobs/{job_id}/tracks")
def get_job_tracks(job_id: int):
    job = get_job_by_id(job_id)

    if not job:
        return {
            "status": "error",
            "message": "Job not found",
            "job_id": job_id,
        }

    ext_tracks = job.get("external_tracks") or {}

    return {
        "status": "ok",
        "job": job,
        "external_tracks": ext_tracks,
        "internal_tracks": ext_tracks.get("internal_tracks") or {},
        "selected_tracks": job.get("selected_tracks") or {},
    }


@app.post("/api/jobs/{job_id}/skip-mux")
def skip_mux_and_import(job_id: int):
    job = get_job_by_id(job_id)

    if not job:
        return {
            "status": "error",
            "message": "Job not found",
            "job_id": job_id,
        }

    staging_path = job.get("staging_path") or ""

    if not staging_path:
        return {
            "status": "error",
            "message": "Job has no staging_path",
            "job_id": job_id,
        }

    try:
        set_job_status_by_id(job_id, "importing", "")

        result = import_from_path(
            source_path=staging_path,
            media_type=job["media_type"],
            metadata=build_job_metadata(job),
            season=int(job["season"] or 1),
            episode=int(job["episode"] or 1),
            episode_title=job["episode_title"] or "",
            season_pack=bool(job["season_pack"]),
            start_episode=int(job["start_episode"] or 1),
        )

        cleanup = cleanup_path(staging_path)

        record_import_history(job, details={"result": result, "cleanup": cleanup, "mode": "skip_mux"})

        delete_job(job_id)

        return {
            "status": "ok",
            "message": "Imported without mux",
            "result": result,
            "cleanup": cleanup,
        }

    except Exception as exc:
        set_job_status_by_id(job_id, "error", str(exc))

        return {
            "status": "error",
            "message": str(exc),
            "job_id": job_id,
        }


@app.post("/api/jobs/{job_id}/start-mux")
async def start_mux(
    job_id: int,
    selected_tracks_json: str = Form(...),
):
    job = get_job_by_id(job_id)

    if not job:
        return {
            "status": "error",
            "message": "Job not found",
            "job_id": job_id,
        }

    staging_path = job.get("staging_path") or ""

    if not staging_path:
        return {
            "status": "error",
            "message": "Job has no staging_path",
            "job_id": job_id,
        }

    try:
        selected_tracks = parse_selected_tracks_json(selected_tracks_json)

        update_mux_selection(
            job_id=job_id,
            status="muxing",
            mux_result_path="",
            selected_tracks=selected_tracks,
            error="",
        )

        # Initialize progress tracker with file count
        scan = scan_release(staging_path)
        total_files = scan.get("video_count", 0)
        first_video_name = scan["videos"][0]["name"] if scan.get("videos") else ""
        progress_tracker.start_job(job_id=job_id, total_files=total_files, first_file_name=first_video_name)

        # Spawn asynchronous background task
        asyncio.create_task(
            run_mux_job_async(
                job_id=job_id,
                staging_path=staging_path,
                selected_tracks=selected_tracks,
            )
        )

        return {
            "status": "started",
            "message": "Muxing started in background",
            "job_id": job_id,
        }

    except Exception as exc:
        update_mux_selection(
            job_id=job_id,
            status="error",
            mux_result_path="",
            selected_tracks={},
            error=str(exc),
        )

        return {
            "status": "error",
            "message": str(exc),
            "job_id": job_id,
        }


@app.get("/api/jobs/{job_id}/mux-progress")
def get_job_mux_progress(job_id: int):
    progress = get_mux_progress(job_id)
    if progress is None:
        job = get_job_by_id(job_id)
        if not job:
            return {
                "status": "error",
                "message": "Job not found",
                "job_id": job_id,
            }
        status = job.get("status") or ""
        is_done = status in {"mux_review", "imported"}
        return {
            "job_id": job_id,
            "status": status,
            "current_file_index": 0,
            "total_files": 0,
            "current_percent": 100.0 if is_done else 0.0,
            "overall_percent": 100.0 if is_done else 0.0,
            "current_file_name": "",
            "log": [],
            "error": job.get("error") or None,
        }
    return progress


@app.post("/api/jobs/{job_id}/confirm-mux")
def confirm_mux(job_id: int):
    job = get_job_by_id(job_id)

    if not job:
        return {
            "status": "error",
            "message": "Job not found",
            "job_id": job_id,
        }

    staging_path = job.get("staging_path") or ""

    if not staging_path:
        return {
            "status": "error",
            "message": "Job has no staging_path",
            "job_id": job_id,
        }

    try:
        set_job_status_by_id(job_id, "importing", "")

        result = import_from_staging_result(
            staging_path=staging_path,
            media_type=job["media_type"],
            metadata=build_job_metadata(job),
            season=int(job["season"] or 1),
            episode=int(job["episode"] or 1),
            episode_title=job["episode_title"] or "",
            season_pack=bool(job["season_pack"]),
            start_episode=int(job["start_episode"] or 1),
        )

        cleanup = cleanup_path(staging_path)

        record_import_history(job, details={"result": result, "cleanup": cleanup, "mode": "confirm_mux"})

        delete_job(job_id)

        return {
            "status": "ok",
            "message": "Mux result imported",
            "result": result,
            "cleanup": cleanup,
        }

    except Exception as exc:
        set_job_status_by_id(job_id, "error", str(exc))

        return {
            "status": "error",
            "message": str(exc),
            "job_id": job_id,
        }
