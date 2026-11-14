import shutil
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from config import AUTO_IMPORT_INTERVAL_SECONDS, STAGING_PATH
from database import (
    delete_job,
    get_queued_space_jobs,
    get_waiting_jobs,
    record_import_history,
    set_job_status,
    set_mux_info,
)
from importer import import_from_torrent, import_from_path, cleanup_path
from media_inspector import scan_release_tracks
from media_scan import scan_release
from qbit import (
    get_torrent_by_hash,
    cleanup_unselected_files_for_import,
    list_torrent_files_no_pause,
    build_possible_base_paths,
    build_file_candidates,
    start_torrent,
    stop_torrent,
)
from space_manager import (
    get_job_required_download_bytes,
    has_space_for_download,
)


scheduler = BackgroundScheduler()


def log(message: str):
    print(f"[watcher] {message}", flush=True)


def is_torrent_complete(torrent: dict):
    if not torrent:
        return False

    progress = float(torrent.get("progress", 0) or 0)
    progress_raw = float(torrent.get("progress_raw", 0) or 0)
    state = str(torrent.get("state", "")).lower()

    # Completed states in qBittorrent (seeding / finished)
    completed_states = {
        "uploading",
        "stalledup",
        "pausedup",
        "stoppedup",
        "queuedup",
        "checkingup",
        "forcedup",
        "complete",
    }

    if state in completed_states:
        return True

    # Active integrity checking or disk allocation must finish first
    if state in {"checkingdl", "allocating"}:
        return False

    # When 100% of data is downloaded:
    # In selective downloads or swarms without active leechers,
    # qBittorrent often stays in 'stalledDL' or 'pausedDL' even though 100% of bytes are on disk.
    if progress >= 100 or progress_raw >= 1.0:
        return True

    return False


def resolve_torrent_path(torrent: dict):
    content_path = torrent.get("content_path")

    if content_path and Path(content_path).exists():
        return Path(content_path)

    save_path = torrent.get("save_path")
    name = torrent.get("name")

    if save_path and name:
        candidate = Path(save_path) / name

        if candidate.exists():
            return candidate

    raise RuntimeError(f"Torrent path not found for: {torrent.get('name')}")


def build_staging_path(job: dict, torrent: dict):
    job_id = job["id"]
    torrent_name = torrent.get("name") or job.get("plex_title") or job.get("title") or "release"

    safe = make_safe_folder_name(torrent_name)

    return Path(STAGING_PATH) / f"{job_id}_{safe}"


def make_safe_folder_name(value: str):
    value = value.strip()
    value = value.replace("/", "_").replace("\\", "_")
    value = value.replace(":", "_").replace("*", "_").replace("?", "_")
    value = value.replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_")
    value = "_".join(value.split())

    if not value:
        return "release"

    return value[:160]


def copy_release_to_staging(source_path: Path, staging_path: Path):
    """
    Обычный режим: копирует весь релиз в staging.

    Если source_path — папка, содержимое копируется внутрь staging_path.
    Если source_path — файл, файл копируется внутрь staging_path.
    """
    log(f"copy start: {source_path} -> {staging_path}")

    if staging_path.exists():
        log(f"remove old staging path: {staging_path}")
        shutil.rmtree(staging_path)

    staging_path.mkdir(parents=True, exist_ok=True)

    if source_path.is_file():
        shutil.copy2(source_path, staging_path / source_path.name)
        log(f"copy done single file: {staging_path}")
        return staging_path

    for item in source_path.iterdir():
        target = staging_path / item.name

        if item.is_dir():
            log(f"copy dir: {item} -> {target}")
            shutil.copytree(item, target)
        else:
            log(f"copy file: {item} -> {target}")
            shutil.copy2(item, target)

    log(f"copy done: {staging_path}")
    return staging_path


def copy_selected_files_to_staging(job: dict, torrent: dict, staging_path: Path):
    """
    Выборочный режим: копирует в staging только выбранные пользователем файлы.

    Это главная защита от проблемы, когда qBittorrent успел создать лишние .mkv
    до выбора файлов. Такие файлы физически могут остаться в папке раздачи,
    но в staging они больше не попадут.
    """
    torrent_hash = job["torrent_hash"]
    selected_file_ids = job.get("selected_file_ids") or []

    if not selected_file_ids:
        raise RuntimeError("Selective job has no selected_file_ids")

    selected_set = {int(item) for item in selected_file_ids}
    files = list_torrent_files_no_pause(torrent_hash)
    base_paths = build_possible_base_paths(torrent)

    log(
        f"copy selected files to staging: "
        f"job_id={job.get('id')} selected={sorted(selected_set)} staging={staging_path}"
    )

    if staging_path.exists():
        log(f"remove old staging path: {staging_path}")
        shutil.rmtree(staging_path)

    staging_path.mkdir(parents=True, exist_ok=True)

    copied = []
    missing = []

    for file_item in files:
        file_id = int(file_item.get("index", -1))

        if file_id not in selected_set:
            continue

        rel_name = file_item.get("name") or ""

        if not rel_name:
            continue

        source = find_existing_file_for_torrent_rel_name(base_paths, rel_name)

        if not source:
            missing.append(rel_name)
            continue

        target_rel = normalize_selected_relative_path(
            rel_name=rel_name,
            torrent_name=torrent.get("name") or "",
        )

        target = staging_path / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)

        log(f"copy selected file: {source} -> {target}")
        shutil.copy2(source, target)
        copied.append(
            {
                "source": str(source),
                "target": str(target),
                "rel_name": rel_name,
            }
        )

    if missing:
        raise RuntimeError(f"Selected files not found on disk: {missing}")

    if not copied:
        raise RuntimeError("No selected files were copied to staging")

    log(f"copy selected done: copied={len(copied)} staging={staging_path}")

    return {
        "staging_path": str(staging_path),
        "copied": copied,
        "missing": missing,
    }


def find_existing_file_for_torrent_rel_name(base_paths: list[Path], rel_name: str):
    candidates = build_file_candidates(base_paths, rel_name)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    return None


def normalize_selected_relative_path(rel_name: str, torrent_name: str):
    """
    Если qBittorrent отдаёт file.name как:
      Release/file.mkv

    а staging уже сам является папкой релиза, то убираем первый компонент.
    """
    rel_path = Path(rel_name)
    parts = rel_path.parts

    if torrent_name and len(parts) > 1 and parts[0] == torrent_name:
        return Path(*parts[1:])

    return rel_path


def should_check_external_tracks(media_type: str):
    return media_type in {"tv", "anime"}


def build_metadata(job: dict):
    title = job["plex_title"] or job["title"]

    return {
        "title": title,
        "original_title": job["title"],
        "year": job["year"] or "",
    }


def is_selective_job(job: dict):
    return bool(job.get("file_selection_required")) and bool(job.get("selected_file_ids"))


def cleanup_unselected_before_import(job: dict):
    """
    Дополнительная уборка источника.

    Даже если она не сработает из-за особенностей путей qBittorrent,
    выборочный staging всё равно скопирует только выбранные файлы.
    """
    file_selection_required = bool(job.get("file_selection_required"))
    selected_file_ids = job.get("selected_file_ids") or []
    torrent_hash = job.get("torrent_hash") or ""

    if not file_selection_required:
        return {
            "status": "skipped",
            "reason": "file_selection_required is false",
        }

    if not selected_file_ids:
        return {
            "status": "skipped",
            "reason": "selected_file_ids is empty",
        }

    if not torrent_hash:
        return {
            "status": "skipped",
            "reason": "torrent_hash is empty",
        }

    log(
        f"cleanup unselected before import: "
        f"job_id={job.get('id')} torrent_hash={torrent_hash} selected={selected_file_ids}"
    )

    result = cleanup_unselected_files_for_import(
        torrent_hash=torrent_hash,
        selected_file_ids=[int(item) for item in selected_file_ids],
    )

    log(f"cleanup unselected result: {result}")

    return result


def import_completed_job(job: dict, torrent: dict):
    metadata = build_metadata(job)

    import_from_torrent(
        torrent=torrent,
        media_type=job["media_type"],
        metadata=metadata,
        season=int(job["season"] or 1),
        episode=int(job["episode"] or 1),
        episode_title=job["episode_title"] or "",
        season_pack=bool(job["season_pack"]),
        start_episode=int(job["start_episode"] or 1),
    )


def import_completed_selective_job_from_staging(job: dict, staging_path: Path):
    metadata = build_metadata(job)

    result = import_from_path(
        source_path=str(staging_path),
        media_type=job["media_type"],
        metadata=metadata,
        season=int(job["season"] or 1),
        episode=int(job["episode"] or 1),
        episode_title=job["episode_title"] or "",
        season_pack=bool(job["season_pack"]),
        start_episode=int(job["start_episode"] or 1),
    )

    return result


def prepare_mux_job(job: dict, torrent: dict):
    torrent_hash = job["torrent_hash"]
    torrent_path = resolve_torrent_path(torrent)
    staging_path = build_staging_path(job, torrent)

    log(f"prepare mux job start: job_id={job['id']} torrent={torrent.get('name')}")
    log(f"torrent path: {torrent_path}")
    log(f"staging path: {staging_path}")

    set_mux_info(
        torrent_hash=torrent_hash,
        status="staging",
        staging_path=str(staging_path),
        mux_result_path="",
        external_tracks={},
        selected_tracks={},
        error="",
    )

    if is_selective_job(job):
        copy_selected_files_to_staging(job, torrent, staging_path)
    else:
        copy_release_to_staging(torrent_path, staging_path)

    log(f"scan staging path: {staging_path}")
    scan = scan_release_tracks(staging_path)

    log(
        "scan result: "
        f"video={scan.get('video_count')} "
        f"audio={scan.get('audio_count')} "
        f"subtitles={scan.get('subtitle_count')}"
    )

    set_mux_info(
        torrent_hash=torrent_hash,
        status="needs_mux",
        staging_path=str(staging_path),
        mux_result_path="",
        external_tracks=scan,
        selected_tracks={},
        error="",
    )

    log(f"prepare mux job done: job_id={job['id']}")


def handle_completed_selective_job_without_mux(job: dict, torrent: dict):
    """
    Выборочная задача без внешних дорожек:
    всё равно импортируем не из исходной папки, а из staging, куда скопированы
    только выбранные файлы.
    """
    torrent_hash = job["torrent_hash"]
    staging_path = build_staging_path(job, torrent)

    log(f"selective normal import via staging: job_id={job['id']} staging={staging_path}")

    set_mux_info(
        torrent_hash=torrent_hash,
        status="staging",
        staging_path=str(staging_path),
        mux_result_path="",
        external_tracks={},
        selected_tracks={},
        error="",
    )

    copy_selected_files_to_staging(job, torrent, staging_path)

    set_job_status(torrent_hash, "importing", "")

    result = import_completed_selective_job_from_staging(job, staging_path)

    cleanup = cleanup_path(str(staging_path))

    set_job_status(torrent_hash, "imported", "")

    record_import_history(job, details={"result": result, "mode": "selective_staging"})
    delete_job(int(job["id"]))

    log(
        f"selective normal import done and job deleted: "
        f"job_id={job['id']} result={result} cleanup={cleanup}"
    )


def queue_job_for_space(job: dict, torrent: dict, reason: str = ""):
    torrent_hash = job["torrent_hash"]

    try:
        stop_torrent(torrent_hash)
    except Exception as exc:
        log(f"failed to stop torrent before queue: hash={torrent_hash} error={exc}")

    message = "Ожидает свободного места"

    if reason:
        message = f"{message}: {reason}"

    set_job_status(torrent_hash, "queued_space", message)
    log(f"job queued for space: job_id={job.get('id')} torrent={torrent.get('name')} reason={reason}")


def ensure_waiting_job_has_space(job: dict, torrent: dict):
    """
    Проверяет, можно ли продолжать загрузку waiting-задачи.

    Если задача ещё не скачана и места с учётом активных загрузок не хватает,
    торрент ставится на паузу, а задача получает статус queued_space.
    """
    if is_torrent_complete(torrent):
        return True

    required = get_job_required_download_bytes(job, torrent)

    if required <= 0:
        return True

    space_check = has_space_for_download(
        required_bytes=required,
        exclude_hash=job["torrent_hash"],
    )

    if space_check.get("ok"):
        return True

    queue_job_for_space(job, torrent, "not enough download disk space")
    return False


def process_queued_space_jobs():
    """
    FIFO-очередь задач, которые ждут свободного места на диске загрузок.

    В UI показывается только статус "ожидает свободного места".
    Подробные числа не выводятся пользователю, но используются для расчёта.
    """
    jobs = get_queued_space_jobs()

    if not jobs:
        return

    log(f"space queue tick: queued jobs={len(jobs)}")

    for job in jobs:
        torrent_hash = job["torrent_hash"]

        try:
            torrent = get_torrent_by_hash(torrent_hash)

            if torrent is None:
                log(f"queued torrent not found: {torrent_hash}")
                set_job_status(torrent_hash, "error", "Torrent not found in qBittorrent")
                continue

            required = get_job_required_download_bytes(job, torrent)

            if required <= 0:
                log(f"queued job has unknown size, keep waiting: job_id={job.get('id')}")
                continue

            space_check = has_space_for_download(
                required_bytes=required,
                exclude_hash=torrent_hash,
            )

            if not space_check.get("ok"):
                log(f"queued job still waits for space: job_id={job.get('id')}")
                continue

            start_torrent(torrent_hash)
            set_job_status(torrent_hash, "waiting", "")

            log(f"queued job started: job_id={job.get('id')} torrent={torrent.get('name')}")

        except Exception as exc:
            log(f"queued job error: id={job.get('id')} error={exc}")
            set_job_status(torrent_hash, "error", str(exc))


def run_auto_import_once():
    process_queued_space_jobs()

    jobs = get_waiting_jobs()

    if not jobs:
        return

    log(f"auto import tick: waiting jobs={len(jobs)}")

    for job in jobs:
        torrent_hash = job["torrent_hash"]

        try:
            log(
                f"check job: id={job['id']} "
                f"title={job.get('plex_title') or job.get('title')} "
                f"status={job.get('status')}"
            )

            torrent = get_torrent_by_hash(torrent_hash)

            if torrent is None:
                log(f"torrent not found: {torrent_hash}")
                set_job_status(torrent_hash, "error", "Torrent not found in qBittorrent")
                continue

            if not ensure_waiting_job_has_space(job, torrent):
                continue

            if not is_torrent_complete(torrent):
                log(
                    f"torrent not complete: {torrent.get('name')} "
                    f"progress={torrent.get('progress')} state={torrent.get('state')}"
                )
                continue

            if is_selective_job(job):
                cleanup_unselected_before_import(job)

            torrent = get_torrent_by_hash(torrent_hash)

            if torrent is None:
                log(f"torrent not found after cleanup: {torrent_hash}")
                set_job_status(torrent_hash, "error", "Torrent not found in qBittorrent after cleanup")
                continue

            media_type = job["media_type"]

            if should_check_external_tracks(media_type):
                if is_selective_job(job):
                    staging_path = build_staging_path(job, torrent)

                    set_mux_info(
                        torrent_hash=torrent_hash,
                        status="staging",
                        staging_path=str(staging_path),
                        mux_result_path="",
                        external_tracks={},
                        selected_tracks={},
                        error="",
                    )

                    copy_selected_files_to_staging(job, torrent, staging_path)

                    log(f"scan selective staging path for external tracks: {staging_path}")
                    scan = scan_release_tracks(staging_path)
                else:
                    torrent_path = resolve_torrent_path(torrent)

                    log(f"scan torrent path for external tracks: {torrent_path}")
                    scan = scan_release_tracks(torrent_path)

                log(
                    "torrent scan result: "
                    f"video={scan.get('video_count')} "
                    f"audio={scan.get('audio_count')} "
                    f"subtitles={scan.get('subtitle_count')} "
                    f"has_external={scan.get('has_external_tracks')}"
                )

                if scan.get("has_external_tracks"):
                    if is_selective_job(job):
                        staging_path = build_staging_path(job, torrent)

                        set_mux_info(
                            torrent_hash=torrent_hash,
                            status="needs_mux",
                            staging_path=str(staging_path),
                            mux_result_path="",
                            external_tracks=scan,
                            selected_tracks={},
                            error="",
                        )

                        log(f"selective mux job ready: job_id={job['id']}")
                    else:
                        prepare_mux_job(job, torrent)

                    continue

            if is_selective_job(job):
                handle_completed_selective_job_without_mux(job, torrent)
                continue

            log(f"start normal import: job_id={job['id']}")
            set_job_status(torrent_hash, "importing", "")

            import_completed_job(job, torrent)

            set_job_status(torrent_hash, "imported", "")

            record_import_history(job, details={"mode": "normal_auto_import"})
            delete_job(int(job["id"]))

            log(f"normal import done and job deleted: job_id={job['id']}")

        except Exception as exc:
            log(f"job error: id={job.get('id')} error={exc}")
            set_job_status(torrent_hash, "error", str(exc))


def start_watcher():
    if scheduler.running:
        return

    scheduler.add_job(
        run_auto_import_once,
        "interval",
        seconds=AUTO_IMPORT_INTERVAL_SECONDS,
        id="auto_import_watcher",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    log(f"watcher started: interval={AUTO_IMPORT_INTERVAL_SECONDS}s")
