import shutil

from config import (
    DOWNLOAD_DISK_PATH,
    MIN_FREE_DLC_SPACE_BYTES,
    QBIT_PREALLOCATE_DISK_SPACE,
)
from qbit import list_torrents, list_torrent_files_no_pause


ACTIVE_DOWNLOAD_STATES = {
    "downloading",
    "forceddl",
    "stalleddl",
    "metadl",
    "allocating",
    "checkingdl",
    "queuedl",
    "queueddl",
}


def get_download_disk_usage():
    usage = shutil.disk_usage(DOWNLOAD_DISK_PATH)

    return {
        "path": DOWNLOAD_DISK_PATH,
        "total": int(usage.total),
        "used": int(usage.used),
        "free": int(usage.free),
        "reserve": int(MIN_FREE_DLC_SPACE_BYTES),
        "preallocate": bool(QBIT_PREALLOCATE_DISK_SPACE),
    }


def get_torrent_remaining_bytes(torrent: dict):
    total = int(torrent.get("total_size") or torrent.get("size") or 0)
    downloaded = int(torrent.get("downloaded") or 0)

    if total <= 0:
        return 0

    return max(total - downloaded, 0)


def is_active_download(torrent: dict):
    state = str(torrent.get("state") or "").lower()
    progress = float(torrent.get("progress_raw") or 0)

    if progress >= 1:
        return False

    return state in ACTIVE_DOWNLOAD_STATES


def get_active_reserved_bytes(exclude_hash: str = ""):
    """
    Возвращает условно зарезервированный активными загрузками объём.

    Если в qBittorrent включён preallocation, это значение НЕ нужно вычитать
    из свободного места: диск уже уменьшился на заранее выделенные файлы.
    Но детали оставляем для debug/API.
    """
    total_reserved = 0
    details = []

    for torrent in list_torrents():
        torrent_hash = torrent.get("hash") or ""

        if exclude_hash and torrent_hash == exclude_hash:
            continue

        if not is_active_download(torrent):
            continue

        remaining = get_torrent_remaining_bytes(torrent)

        if remaining <= 0:
            continue

        total_reserved += remaining
        details.append(
            {
                "hash": torrent_hash,
                "name": torrent.get("name") or "",
                "state": torrent.get("state") or "",
                "remaining": remaining,
            }
        )

    return {
        "reserved": total_reserved,
        "items": details,
    }


def get_available_download_bytes(exclude_hash: str = ""):
    """
    Считает доступное место для запуска следующей загрузки.

    QBIT_PREALLOCATE_DISK_SPACE=true:
      qBittorrent уже заранее занял место под активные загрузки,
      поэтому считаем только free - reserve.

    QBIT_PREALLOCATE_DISK_SPACE=false:
      активные загрузки ещё не заняли весь будущий объём,
      поэтому дополнительно вычитаем remaining активных торрентов.
    """
    usage = get_download_disk_usage()
    active = get_active_reserved_bytes(exclude_hash=exclude_hash)

    if QBIT_PREALLOCATE_DISK_SPACE:
        active_reserved_used_in_formula = 0
    else:
        active_reserved_used_in_formula = active["reserved"]

    available = usage["free"] - usage["reserve"] - active_reserved_used_in_formula

    return {
        "path": usage["path"],
        "total": usage["total"],
        "used": usage["used"],
        "free": usage["free"],
        "reserve": usage["reserve"],
        "preallocate": bool(QBIT_PREALLOCATE_DISK_SPACE),
        "active_reserved": active["reserved"],
        "active_reserved_used_in_formula": active_reserved_used_in_formula,
        "available": max(available, 0),
        "active_items": active["items"],
    }


def calculate_selected_files_size(torrent_hash: str, selected_file_ids: list[int]):
    selected_set = {int(item) for item in selected_file_ids}
    files = list_torrent_files_no_pause(torrent_hash)

    total_size = 0
    matched_ids = []

    for item in files:
        file_id = int(item.get("index", -1))

        if file_id not in selected_set:
            continue

        total_size += int(item.get("size") or 0)
        matched_ids.append(file_id)

    return {
        "size": total_size,
        "matched_ids": matched_ids,
        "total_files": len(files),
    }


def get_job_required_download_bytes(job: dict, torrent: dict | None = None):
    selected_file_ids = job.get("selected_file_ids") or []
    torrent_hash = job.get("torrent_hash") or ""

    if selected_file_ids and torrent_hash:
        selected = calculate_selected_files_size(
            torrent_hash=torrent_hash,
            selected_file_ids=[int(item) for item in selected_file_ids],
        )

        if selected["size"] > 0:
            return selected["size"]

    if torrent:
        return get_torrent_remaining_bytes(torrent)

    return 0


def has_space_for_download(required_bytes: int, exclude_hash: str = ""):
    available = get_available_download_bytes(exclude_hash=exclude_hash)

    return {
        "ok": int(required_bytes) <= int(available["available"]),
        "required": int(required_bytes),
        "available": int(available["available"]),
        "free": int(available["free"]),
        "reserve": int(available["reserve"]),
        "preallocate": bool(available["preallocate"]),
        "active_reserved": int(available["active_reserved"]),
        "active_reserved_used_in_formula": int(available["active_reserved_used_in_formula"]),
        "path": available["path"],
    }
