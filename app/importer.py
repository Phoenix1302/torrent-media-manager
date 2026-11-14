import os
import re
import shutil
import time
from pathlib import Path

from config import (
    WATCH_PATH,
    MOVIES_PATH,
    TV_PATH,
    ANIME_PATH,
    CARTOONS_PATH,
    IMPORT_MODE,
    DELETE_SOURCE_AFTER_IMPORT,
    DIR_MODE,
    FILE_MODE,
    TARGET_UID,
    TARGET_GID,
)


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".ts",
    ".webm",
}


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def natural_sort_key(value: str):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]


def chmod_path(path: Path, mode: int):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def chown_path(path: Path, uid: int | None = None, gid: int | None = None):
    try:
        u = uid if uid is not None else TARGET_UID
        g = gid if gid is not None else TARGET_GID
        os.chown(path, u, g)
    except OSError:
        pass


def is_library_root(path: Path) -> bool:
    library_roots = {
        str(Path(MOVIES_PATH)),
        str(Path(TV_PATH)),
        str(Path(ANIME_PATH)),
        str(Path(CARTOONS_PATH)),
    }

    return str(path) in library_roots


def ensure_parent_dir(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)

    current = path.parent

    while True:
        chmod_path(current, DIR_MODE)
        chown_path(current, TARGET_UID, TARGET_GID)

        if is_library_root(current):
            break

        parent = current.parent

        if parent == current:
            break

        current = parent


def apply_import_permissions(target: Path):
    chmod_path(target, FILE_MODE)
    chown_path(target, TARGET_UID, TARGET_GID)

    current = target.parent

    while True:
        chmod_path(current, DIR_MODE)
        chown_path(current, TARGET_UID, TARGET_GID)

        if is_library_root(current):
            break

        parent = current.parent

        if parent == current:
            break

        current = parent


def resolve_download_path(torrent: dict):
    content_path = torrent.get("content_path")

    if content_path and Path(content_path).exists():
        return Path(content_path)

    name = torrent.get("name")

    if not name:
        raise RuntimeError("Torrent name is empty")

    fallback = Path(WATCH_PATH) / name

    if fallback.exists():
        return fallback

    raise RuntimeError(f"Download path not found: {fallback}")


def find_video_files(folder: str | Path):
    folder_path = Path(folder)

    if folder_path.is_file():
        if folder_path.suffix.lower() in VIDEO_EXTENSIONS:
            return [folder_path]
        return []

    candidates = []

    for path in folder_path.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        lower_name = path.name.lower()

        if "sample" in lower_name:
            continue

        try:
            if path.stat().st_size < 50 * 1024 * 1024:
                continue
        except OSError:
            continue

        candidates.append(path)

    return sorted(candidates, key=lambda p: natural_sort_key(str(p.relative_to(folder_path))))


def find_largest_video_file(folder: str | Path):
    candidates = find_video_files(folder)

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_size)


def parse_episode_number_from_filename(path: Path):
    stem = path.stem
    text = stem.lower()

    patterns = [
        r"\bs\d{1,2}\s*e(?P<ep>\d{1,3})\b",
        r"\b\d{1,2}x(?P<ep>\d{1,3})\b",
        r"\be(?P<ep>\d{1,3})\b",
        r"\bep\s*(?P<ep>\d{1,3})\b",
        r"\bepisode\s*(?P<ep>\d{1,3})\b",
        r"\bсерия\s*(?P<ep>\d{1,3})\b",
        r"^\s*(?P<ep>\d{1,3})(?:\s|\.|-|_)",
        r"^\s*(?P<ep>\d{1,3})$",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            episode = int(match.group("ep"))

            if 1 <= episode <= 300:
                return episode

    return None


def build_movie_target(metadata: dict, source_file: Path, root_path: str):
    title = safe_name(metadata["title"])
    year = metadata.get("year", "").strip()

    if year:
        file_name = f"{title} ({year}){source_file.suffix}"
    else:
        file_name = f"{title}{source_file.suffix}"

    return Path(root_path) / file_name


def build_show_target(
    metadata: dict,
    source_file: Path,
    root_path: str,
    season: int,
    episode: int,
    episode_title: str = "",
):
    title = safe_name(metadata["title"])
    year = metadata.get("year", "").strip()

    show_folder = f"{title} ({year})" if year else title
    season_folder = f"Season {season:02d}"

    if episode_title:
        clean_episode_title = safe_name(episode_title)
        file_name = (
            f"{show_folder} - S{season:02d}E{episode:02d} - "
            f"{clean_episode_title}{source_file.suffix}"
        )
    else:
        file_name = f"{show_folder} - S{season:02d}E{episode:02d}{source_file.suffix}"

    return Path(root_path) / show_folder / season_folder / file_name


def import_file(source: Path, target: Path, import_mode_override: str | None = None):
    ensure_parent_dir(target)

    if target.exists():
        try:
            target_size = target.stat().st_size
            source_size = source.stat().st_size
            if target_size == source_size and target_size > 0:
                apply_import_permissions(target)
                print(f"[importer] Target already exists with matching size ({round(target_size / 1048576, 1)} MB), skipping: {target.name}", flush=True)
                return {
                    "status": "exists",
                    "source": str(source),
                    "target": str(target),
                }
            else:
                print(f"[importer] Target exists but size mismatch (partial file: {round(target_size / 1048576, 1)} MB != {round(source_size / 1048576, 1)} MB), overwriting: {target.name}", flush=True)
                target.unlink(missing_ok=True)
        except OSError:
            pass

    effective_mode = import_mode_override or IMPORT_MODE
    size_mb = 0
    try:
        size_mb = round(source.stat().st_size / (1024 * 1024), 1)
    except OSError:
        pass

    print(f"[importer] Transferring ({effective_mode}, {size_mb} MB): {source.name} -> {target.name}", flush=True)
    t0 = time.monotonic()

    if effective_mode == "move":
        shutil.move(str(source), str(target))
        method = "move"

    elif effective_mode == "hardlink":
        try:
            os.link(source, target)
            method = "hardlink"
        except OSError:
            shutil.copy2(source, target)
            method = "copy_fallback"

    else:
        shutil.copy2(source, target)
        method = "copy"

    duration = max(round(time.monotonic() - t0, 2), 0.01)
    speed_mb = round(size_mb / duration, 1) if size_mb > 0 else 0
    print(f"[importer] Done ({method}) in {duration}s (~{speed_mb} MB/s): {target.name}", flush=True)

    apply_import_permissions(target)

    if DELETE_SOURCE_AFTER_IMPORT and effective_mode == "copy":
        source.unlink()

    return {
        "status": "imported",
        "method": method,
        "source": str(source),
        "target": str(target),
    }


def preview_movie_or_cartoon_from_path(source_path: str | Path, media_type: str, metadata: dict):
    source_root = Path(source_path)
    source = find_largest_video_file(source_root)

    if source is None:
        raise RuntimeError(f"Video file not found in {source_root}")

    if media_type == "movie":
        target = build_movie_target(metadata, source, MOVIES_PATH)
    elif media_type == "cartoon":
        target = build_movie_target(metadata, source, CARTOONS_PATH)
    else:
        raise RuntimeError(f"Invalid movie/cartoon media_type: {media_type}")

    return {
        "status": "ok",
        "media_type": media_type,
        "source": str(source),
        "target": str(target),
        "import_mode": IMPORT_MODE,
        "mode": "single_video",
    }


def preview_series_or_anime_from_path(
    source_path: str | Path,
    media_type: str,
    metadata: dict,
    season: int = 1,
    start_episode: int = 1,
):
    source_root = Path(source_path)
    files = find_video_files(source_root)

    if not files:
        raise RuntimeError(f"Video files not found in {source_root}")

    root_path = TV_PATH if media_type == "tv" else ANIME_PATH

    items = []
    used_episodes = set()
    fallback_episode = start_episode

    for source in files:
        parsed_episode = parse_episode_number_from_filename(source)

        if parsed_episode is not None and parsed_episode not in used_episodes:
            episode = parsed_episode
        else:
            while fallback_episode in used_episodes:
                fallback_episode += 1

            episode = fallback_episode
            fallback_episode += 1

        used_episodes.add(episode)

        target = build_show_target(
            metadata=metadata,
            source_file=source,
            root_path=root_path,
            season=season,
            episode=episode,
            episode_title="",
        )

        items.append(
            {
                "source": str(source),
                "target": str(target),
                "season": season,
                "episode": episode,
                "parsed_episode": parsed_episode,
            }
        )

    return {
        "status": "ok",
        "media_type": media_type,
        "source_root": str(source_root),
        "items": items,
        "count": len(items),
        "import_mode": IMPORT_MODE,
        "mode": "folder_episodes",
    }


def preview_import_from_path(
    source_path: str | Path,
    media_type: str,
    metadata: dict,
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    season_pack: bool = False,
    start_episode: int = 1,
):
    if media_type in {"movie", "cartoon"}:
        return preview_movie_or_cartoon_from_path(
            source_path=source_path,
            media_type=media_type,
            metadata=metadata,
        )

    if media_type in {"tv", "anime"}:
        return preview_series_or_anime_from_path(
            source_path=source_path,
            media_type=media_type,
            metadata=metadata,
            season=season,
            start_episode=start_episode or episode or 1,
        )

    raise RuntimeError(f"Unknown media_type: {media_type}")


def import_from_path(
    source_path: str | Path,
    media_type: str,
    metadata: dict,
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    season_pack: bool = False,
    start_episode: int = 1,
    import_mode_override: str | None = None,
):
    preview = preview_import_from_path(
        source_path=source_path,
        media_type=media_type,
        metadata=metadata,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_pack=season_pack,
        start_episode=start_episode,
    )

    if preview.get("mode") == "folder_episodes":
        results = []
        total_items = len(preview["items"])
        print(f"[importer] Starting batch import of {total_items} items...", flush=True)

        for idx, item in enumerate(preview["items"], 1):
            print(f"[importer] [{idx}/{total_items}] Importing: {Path(item['source']).name}", flush=True)
            result = import_file(
                Path(item["source"]),
                Path(item["target"]),
                import_mode_override=import_mode_override,
            )
            results.append(result)

        print(f"[importer] Batch import completed ({total_items} items)", flush=True)
        return {
            "status": "ok",
            "preview": preview,
            "imported_items": results,
        }

    print(f"[importer] Importing single media file: {Path(preview['source']).name}", flush=True)
    result = import_file(
        Path(preview["source"]),
        Path(preview["target"]),
        import_mode_override=import_mode_override,
    )

    return {
        "status": "ok",
        "preview": preview,
        "import": result,
    }


def preview_import(
    torrent: dict,
    media_type: str,
    metadata: dict,
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    season_pack: bool = False,
    start_episode: int = 1,
):
    download_path = resolve_download_path(torrent)

    return preview_import_from_path(
        source_path=download_path,
        media_type=media_type,
        metadata=metadata,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_pack=season_pack,
        start_episode=start_episode,
    )


def import_from_torrent(
    torrent: dict,
    media_type: str,
    metadata: dict,
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    season_pack: bool = False,
    start_episode: int = 1,
):
    download_path = resolve_download_path(torrent)

    return import_from_path(
        source_path=download_path,
        media_type=media_type,
        metadata=metadata,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_pack=season_pack,
        start_episode=start_episode,
    )


def import_from_staging_result(
    staging_path: str | Path,
    media_type: str,
    metadata: dict,
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    season_pack: bool = False,
    start_episode: int = 1,
):
    result_path = Path(staging_path) / "Result"

    if not result_path.exists():
        raise RuntimeError(f"Result directory not found: {result_path}")

    # Result — временные файлы после mkvmerge. Их не нужно сидировать,
    # поэтому перемещаем в библиотеку, а не копируем.
    return import_from_path(
        source_path=result_path,
        media_type=media_type,
        metadata=metadata,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_pack=season_pack,
        start_episode=start_episode,
        import_mode_override="move",
    )


def cleanup_path(path: str | Path):
    target = Path(path)

    if not target.exists():
        return {
            "status": "not_found",
            "path": str(target),
        }

    if target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)

    return {
        "status": "deleted",
        "path": str(target),
    }
