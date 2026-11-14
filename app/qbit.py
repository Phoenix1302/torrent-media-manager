import math
from pathlib import Path
import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import QBITTORRENT_HOST, QBITTORRENT_USER, QBITTORRENT_PASS

_SESSION_LOCK = threading.Lock()
_SHARED_SESSION: requests.Session | None = None
_IS_LOGGED_IN: bool = False


def _create_pooled_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=Retry(total=2, backoff_factor=0.2, status_forcelist=[502, 503, 504]),
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _do_login(session: requests.Session) -> None:
    login_url = f"{QBITTORRENT_HOST}/api/v2/auth/login"

    response = session.post(
        login_url,
        data={
            "username": QBITTORRENT_USER,
            "password": QBITTORRENT_PASS,
        },
        timeout=15,
    )

    response_text = response.text.strip().lower()

    if response.status_code not in {200, 204}:
        raise RuntimeError(
            f"qBittorrent login failed: HTTP {response.status_code} {response.text}"
        )

    if response_text == "fails.":
        raise RuntimeError(
            f"qBittorrent login failed: HTTP {response.status_code} {response.text}"
        )


def get_session(force_relogin: bool = False) -> requests.Session:
    """Returns a persistent requests.Session with connection pooling and re-login on demand."""
    global _SHARED_SESSION, _IS_LOGGED_IN
    with _SESSION_LOCK:
        if _SHARED_SESSION is None:
            _SHARED_SESSION = _create_pooled_session()
            _IS_LOGGED_IN = False

        if not _IS_LOGGED_IN or force_relogin:
            _do_login(_SHARED_SESSION)
            _IS_LOGGED_IN = True

        return _SHARED_SESSION


def reset_session() -> None:
    """Closes and resets the shared session. Useful for tests and reconnecting."""
    global _SHARED_SESSION, _IS_LOGGED_IN
    with _SESSION_LOCK:
        if _SHARED_SESSION is not None:
            try:
                _SHARED_SESSION.close()
            except Exception:
                pass
            _SHARED_SESSION = None
            _IS_LOGGED_IN = False


def qbit_post(path: str, data: dict | None = None, files: Any = None):
    session = get_session()
    url = f"{QBITTORRENT_HOST}{path}"

    response = session.post(
        url,
        data=data or {},
        files=files,
        timeout=30,
    )

    if response.status_code in {401, 403}:
        session = get_session(force_relogin=True)
        response = session.post(
            url,
            data=data or {},
            files=files,
            timeout=30,
        )

    if response.status_code not in {200, 204}:
        raise RuntimeError(
            f"qBittorrent POST failed: {path} HTTP {response.status_code} {response.text}"
        )

    return response


def qbit_get(path: str, params: dict | None = None):
    session = get_session()
    url = f"{QBITTORRENT_HOST}{path}"

    response = session.get(
        url,
        params=params or {},
        timeout=30,
    )

    if response.status_code in {401, 403}:
        session = get_session(force_relogin=True)
        response = session.get(
            url,
            params=params or {},
            timeout=30,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"qBittorrent GET failed: {path} HTTP {response.status_code} {response.text}"
        )

    return response


def format_eta(seconds: int | float | None):
    if seconds is None:
        return ""

    try:
        seconds = int(seconds)
    except Exception:
        return ""

    if seconds < 0 or seconds >= 8640000:
        return "∞"

    if seconds == 0:
        return ""

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours > 0:
        return f"{hours}ч {minutes}м"

    return f"{minutes}м"


def normalize_torrent(item: dict[str, Any]):
    progress_float = float(item.get("progress", 0) or 0)
    progress_percent = round(progress_float * 100, 1)
    eta = item.get("eta")

    return {
        "hash": item.get("hash", ""),
        "name": item.get("name", ""),
        "state": item.get("state", ""),
        "progress": progress_percent,
        "progress_raw": progress_float,
        "size": item.get("size", 0),
        "total_size": item.get("total_size", item.get("size", 0)),
        "downloaded": item.get("downloaded", 0),
        "uploaded": item.get("uploaded", 0),
        "dlspeed": item.get("dlspeed", 0),
        "upspeed": item.get("upspeed", 0),
        "eta": eta,
        "eta_text": format_eta(eta),
        "category": item.get("category", ""),
        "save_path": item.get("save_path", ""),
        "content_path": item.get("content_path", ""),
        "added_on": item.get("added_on", 0),
        "completion_on": item.get("completion_on", 0),
    }


def list_torrents():
    response = qbit_get("/api/v2/torrents/info")
    items = response.json()
    return [normalize_torrent(item) for item in items]


def get_torrent_by_hash(torrent_hash: str):
    items = list_torrents()

    for item in items:
        if item.get("hash") == torrent_hash:
            return item

    return None


def add_magnet(magnet: str, category: str = ""):
    data = {
        "urls": magnet,
        "category": category,
    }

    response = qbit_post("/api/v2/torrents/add", data=data)

    return {
        "status": "ok",
        "message": response.text,
    }


def add_magnet_stopped(magnet: str, category: str = ""):
    """
    Режим выборочной загрузки.

    Magnet добавляется активным, потому что qBittorrent не всегда получает
    metadata у stopped/paused torrent. После получения списка файлов
    list_torrent_files() ставит torrent на паузу.
    """
    data = {
        "urls": magnet,
        "category": category,
    }

    response = qbit_post("/api/v2/torrents/add", data=data)

    return {
        "status": "ok",
        "message": response.text,
        "mode": "active_until_metadata_then_pause",
    }


def add_torrent_file(
    file_bytes: bytes,
    filename: str = "upload.torrent",
    category: str = "",
    stopped: bool = False,
) -> dict:
    """
    Добавляет .torrent файл в qBittorrent через multipart/form-data.
    """
    files = {
        "torrents": (filename, file_bytes, "application/x-bittorrent"),
    }
    data = {
        "category": category,
        "paused": "true" if stopped else "false",
        "autoTMM": "false",
    }
    if stopped:
        data["stop_condition"] = "None"

    response = qbit_post("/api/v2/torrents/add", data=data, files=files)

    return {
        "status": "ok",
        "message": response.text,
        "filename": filename,
        "stopped": stopped,
    }


def add_magnet_and_find_hash(
    magnet: str,
    category: str = "",
    stopped: bool = False,
    before_hashes: set[str] | None = None,
):
    before_hashes = before_hashes or {item["hash"] for item in list_torrents()}

    if stopped:
        add_result = add_magnet_stopped(magnet, category)
    else:
        add_result = add_magnet(magnet, category)

    torrent_hash = ""

    for _ in range(10):
        items = list_torrents()
        new_items = [item for item in items if item["hash"] not in before_hashes]

        if new_items:
            new_items = sorted(
                new_items,
                key=lambda item: item.get("added_on", 0),
                reverse=True,
            )
            torrent_hash = new_items[0]["hash"]
            break

    return {
        "status": "ok",
        "qbit": add_result,
        "torrent_hash": torrent_hash,
    }


def delete_torrent(torrent_hash: str, delete_files: bool = False):
    data = {
        "hashes": torrent_hash,
        "deleteFiles": "true" if delete_files else "false",
    }

    response = qbit_post("/api/v2/torrents/delete", data=data)

    return {
        "status": "ok",
        "message": response.text,
        "torrent_hash": torrent_hash,
        "delete_files": delete_files,
    }


def stop_torrent(torrent_hash: str):
    data = {
        "hashes": torrent_hash,
    }

    try:
        response = qbit_post("/api/v2/torrents/stop", data=data)

        return {
            "status": "ok",
            "method": "stop",
            "message": response.text,
        }
    except Exception:
        response = qbit_post("/api/v2/torrents/pause", data=data)

        return {
            "status": "ok",
            "method": "pause",
            "message": response.text,
        }


def start_torrent(torrent_hash: str):
    data = {
        "hashes": torrent_hash,
    }

    try:
        response = qbit_post("/api/v2/torrents/start", data=data)

        return {
            "status": "ok",
            "method": "start",
            "message": response.text,
        }
    except Exception:
        response = qbit_post("/api/v2/torrents/resume", data=data)

        return {
            "status": "ok",
            "method": "resume",
            "message": response.text,
        }


def normalize_torrent_file(item: dict[str, Any], index: int):
    size = int(item.get("size", 0) or 0)
    progress = float(item.get("progress", 0) or 0)
    priority = int(item.get("priority", 0) or 0)

    return {
        "index": index,
        "name": item.get("name", ""),
        "size": size,
        "size_text": format_size(size),
        "progress": round(progress * 100, 1),
        "progress_raw": progress,
        "priority": priority,
        "selected": priority > 0,
        "is_downloaded": progress >= 1,
    }


def list_torrent_files(torrent_hash: str):
    """
    Возвращает список файлов торрента.
    Если metadata уже получены, torrent ставится на паузу, чтобы он не продолжал
    качать всё подряд до выбора файлов.
    """
    response = qbit_get(
        "/api/v2/torrents/files",
        params={
            "hash": torrent_hash,
        },
    )

    items = response.json()

    normalized = [
        normalize_torrent_file(item, index)
        for index, item in enumerate(items)
    ]

    if normalized:
        try:
            stop_torrent(torrent_hash)
        except Exception:
            pass

    return normalized


def list_torrent_files_no_pause(torrent_hash: str):
    """
    То же самое, что list_torrent_files(), но без постановки на паузу.
    Используется watcher-ом перед импортом.
    """
    response = qbit_get(
        "/api/v2/torrents/files",
        params={
            "hash": torrent_hash,
        },
    )

    items = response.json()

    return [
        normalize_torrent_file(item, index)
        for index, item in enumerate(items)
    ]


def set_file_priority(torrent_hash: str, file_ids: list[int], priority: int):
    if not file_ids:
        return {
            "status": "ok",
            "message": "No file ids provided",
        }

    data = {
        "hash": torrent_hash,
        "id": "|".join(str(file_id) for file_id in file_ids),
        "priority": str(priority),
    }

    response = qbit_post("/api/v2/torrents/filePrio", data=data)

    return {
        "status": "ok",
        "message": response.text,
        "torrent_hash": torrent_hash,
        "file_ids": file_ids,
        "priority": priority,
    }


def select_torrent_files(
    torrent_hash: str,
    selected_file_ids: list[int],
    start_after_selection: bool = True,
):
    """
    Отключает все файлы, кроме выбранных.

    start_after_selection=True:
      обычный режим — после выбора файлов торрент запускается.

    start_after_selection=False:
      очередь по месту — priority выставлены, лишние файлы очищены,
      но торрент остаётся на паузе до появления свободного места.
    """
    files = list_torrent_files(torrent_hash)
    all_ids = [item["index"] for item in files]

    selected_set = {int(file_id) for file_id in selected_file_ids}
    valid_selected_ids = [
        file_id
        for file_id in all_ids
        if file_id in selected_set
    ]

    if not valid_selected_ids:
        raise RuntimeError("No valid selected files")

    selected_size = sum(
        int(item.get("size") or 0)
        for item in files
        if int(item.get("index", -1)) in set(valid_selected_ids)
    )

    stop_result = stop_torrent(torrent_hash)

    set_all_result = set_file_priority(
        torrent_hash=torrent_hash,
        file_ids=all_ids,
        priority=0,
    )

    set_selected_result = set_file_priority(
        torrent_hash=torrent_hash,
        file_ids=valid_selected_ids,
        priority=1,
    )

    cleanup_result = cleanup_unselected_files(
        torrent_hash=torrent_hash,
        files=files,
        selected_file_ids=valid_selected_ids,
        delete_complete_unselected=True,
    )

    if start_after_selection:
        start_result = start_torrent(torrent_hash)
    else:
        start_result = {
            "status": "skipped",
            "reason": "queued_space",
            "message": "Torrent left stopped until enough free space is available",
        }

    return {
        "status": "ok",
        "torrent_hash": torrent_hash,
        "selected_file_ids": valid_selected_ids,
        "selected_size": selected_size,
        "total_files": len(all_ids),
        "stop": stop_result,
        "set_all": set_all_result,
        "set_selected": set_selected_result,
        "cleanup": cleanup_result,
        "start": start_result,
    }

def cleanup_unselected_files_for_import(
    torrent_hash: str,
    selected_file_ids: list[int],
):
    """
    Финальная защита перед импортом.

    Удаляет все реальные файлы из раздачи, которые НЕ были выбраны пользователем.
    Это нужно, потому что qBittorrent мог успеть создать .mkv до выбора файлов,
    а importer потом увидит их как обычные видеофайлы.
    """
    files = list_torrent_files_no_pause(torrent_hash)

    return cleanup_unselected_files(
        torrent_hash=torrent_hash,
        files=files,
        selected_file_ids=selected_file_ids,
        delete_complete_unselected=True,
    )


def cleanup_unselected_files(
    torrent_hash: str,
    files: list[dict],
    selected_file_ids: list[int],
    delete_complete_unselected: bool = True,
):
    torrent = get_torrent_by_hash(torrent_hash)

    if not torrent:
        return {
            "status": "skipped",
            "reason": "torrent not found",
            "deleted": [],
            "errors": [],
            "debug": {},
        }

    selected_set = {int(item) for item in selected_file_ids}
    base_paths = build_possible_base_paths(torrent)

    deleted = []
    errors = []
    attempted = []

    for item in files:
        file_id = int(item.get("index", -1))

        if file_id in selected_set:
            continue

        progress_raw = float(item.get("progress_raw", 0) or 0)

        if progress_raw >= 1 and not delete_complete_unselected:
            continue

        rel_name = item.get("name") or ""

        if not rel_name:
            continue

        candidates = build_file_candidates(base_paths, rel_name)

        for candidate in candidates:
            attempted.append(str(candidate))

            try:
                if candidate.exists() and candidate.is_file():
                    candidate.unlink()
                    deleted.append(str(candidate))
            except Exception as exc:
                errors.append(
                    {
                        "path": str(candidate),
                        "error": str(exc),
                    }
                )

    for base_path in base_paths:
        cleanup_empty_dirs(base_path)

    return {
        "status": "ok",
        "deleted": unique_strings(deleted),
        "errors": errors,
        "debug": {
            "torrent_name": torrent.get("name", ""),
            "save_path": torrent.get("save_path", ""),
            "content_path": torrent.get("content_path", ""),
            "base_paths": [str(item) for item in base_paths],
            "attempted_count": len(attempted),
            "attempted_sample": unique_strings(attempted)[:30],
            "selected_file_ids": sorted(list(selected_set)),
        },
    }


def build_possible_base_paths(torrent: dict):
    """
    qBittorrent по-разному отдаёт save_path/content_path в зависимости от режима
    и структуры раздачи. Поэтому для удаления пробуем несколько баз.

    Возможные случаи:
    1. save_path = /InComplete
       file.name = Release/file.mkv

    2. save_path = /InComplete/Release
       file.name = file.mkv

    3. content_path = /InComplete/Release
       file.name = file.mkv

    4. content_path = /InComplete/Release
       file.name = Release/file.mkv
    """
    result = []

    save_path = torrent.get("save_path") or ""
    content_path = torrent.get("content_path") or ""
    name = torrent.get("name") or ""

    if save_path:
        result.append(Path(save_path))

        if name:
            result.append(Path(save_path) / name)

    if content_path:
        content = Path(content_path)

        if content.is_dir():
            result.append(content)
            result.append(content.parent)
        else:
            result.append(content.parent)

    unique = []
    seen = set()

    for item in result:
        normalized = str(item)

        if normalized in seen:
            continue

        seen.add(normalized)
        unique.append(item)

    return unique


def build_file_candidates(base_paths: list[Path], rel_name: str):
    rel_path = Path(rel_name)
    candidates = []

    for base_path in base_paths:
        direct = base_path / rel_path
        candidates.append(direct)
        candidates.append(Path(str(direct) + ".!qB"))

        # Если rel_name содержит корневую папку раздачи, а base_path уже указывает
        # внутрь этой папки, пробуем также вариант без первого компонента.
        parts = rel_path.parts

        if len(parts) > 1:
            without_root = Path(*parts[1:])
            direct_without_root = base_path / without_root
            candidates.append(direct_without_root)
            candidates.append(Path(str(direct_without_root) + ".!qB"))

    unique = []
    seen = set()

    for item in candidates:
        normalized = str(item)

        if normalized in seen:
            continue

        seen.add(normalized)
        unique.append(item)

    return unique


def cleanup_empty_dirs(base_path: Path):
    if not base_path.exists() or not base_path.is_dir():
        return

    dirs = [
        path
        for path in base_path.rglob("*")
        if path.is_dir()
    ]

    dirs.sort(key=lambda path: len(str(path)), reverse=True)

    for path in dirs:
        if path == base_path:
            continue

        try:
            path.rmdir()
        except Exception:
            pass


def unique_strings(items: list[str]):
    result = []
    seen = set()

    for item in items:
        if item in seen:
            continue

        seen.add(item)
        result.append(item)

    return result


def format_size(size: int | float | None):
    if size is None:
        return ""

    try:
        size = float(size)
    except Exception:
        return ""

    if size <= 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB"]
    power = min(int(math.log(size, 1024)), len(units) - 1)
    value = size / (1024 ** power)

    if power == 0:
        return f"{int(value)} {units[power]}"

    return f"{value:.2f} {units[power]}"
