import re
from pathlib import Path


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".ts",
    ".webm",
}

AUDIO_EXTENSIONS = {
    ".mka",
    ".ac3",
    ".eac3",
    ".dts",
    ".aac",
    ".flac",
    ".wav",
    ".ogg",
    ".opus",
}

SUBTITLE_EXTENSIONS = {
    ".ass",
    ".ssa",
    ".srt",
    ".sub",
    ".vtt",
}


def natural_sort_key(value: str):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]


def is_sample_file(path: Path) -> bool:
    return "sample" in path.name.lower()


def is_valid_media_file(path: Path) -> bool:
    if not path.is_file():
        return False

    if is_sample_file(path):
        return False

    return True


def parse_episode_number_from_filename(path: Path):
    """
    Пытается достать номер серии из имени файла.

    Поддерживает:
    - S01E03
    - s1e3
    - 1x03
    - E03
    - Episode 03
    - Ep 03
    - Серия 03
    - 03.mka
    - 03 - title.ass
    """
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

            # Защита от 1080p, 2160p и других случайных чисел.
            if 1 <= episode <= 300:
                return episode

    return None


def get_relative_parent(root_path: Path, file_path: Path) -> str:
    """
    Возвращает относительную папку файла внутри релиза.

    Пример:
    root: /release
    file: /release/RUS Dub/AniLibria/01.mka
    result: RUS Dub/AniLibria
    """
    try:
        relative_parent = file_path.parent.relative_to(root_path)
    except ValueError:
        return file_path.parent.name or "External"

    if str(relative_parent) == ".":
        return "External"

    return str(relative_parent)


def get_group_name(root_path: Path, file_path: Path, fallback: str) -> str:
    """
    Имя группы берём из папки, где лежат дорожки.

    Если файл лежит в корне релиза, используем fallback:
    - External Audio
    - External Subtitles
    """
    relative_parent = get_relative_parent(root_path, file_path)

    if relative_parent == "External":
        return fallback

    return relative_parent


def collect_files(root_path: str | Path):
    root = Path(root_path)

    video_files = []
    audio_files = []
    subtitle_files = []

    for path in root.rglob("*"):
        if not is_valid_media_file(path):
            continue

        suffix = path.suffix.lower()

        if suffix in VIDEO_EXTENSIONS:
            video_files.append(path)
        elif suffix in AUDIO_EXTENSIONS:
            audio_files.append(path)
        elif suffix in SUBTITLE_EXTENSIONS:
            subtitle_files.append(path)

    video_files = sorted(video_files, key=lambda p: natural_sort_key(str(p.relative_to(root))))
    audio_files = sorted(audio_files, key=lambda p: natural_sort_key(str(p.relative_to(root))))
    subtitle_files = sorted(subtitle_files, key=lambda p: natural_sort_key(str(p.relative_to(root))))

    return {
        "root": root,
        "video_files": video_files,
        "audio_files": audio_files,
        "subtitle_files": subtitle_files,
    }


def group_external_files(root_path: Path, files: list[Path], kind: str):
    """
    Группирует внешние дорожки по папкам.

    kind:
    - audio
    - subtitles

    Возвращает компактный список групп для UI.
    """
    fallback = "External Audio" if kind == "audio" else "External Subtitles"

    groups = {}

    for path in files:
        group_name = get_group_name(root_path, path, fallback)

        if group_name not in groups:
            groups[group_name] = {
                "id": make_group_id(kind, group_name),
                "kind": kind,
                "name": group_name,
                "sample_file": str(path),
                "sample_file_name": path.name,
                "files": [],
                "file_count": 0,
            }

        episode = parse_episode_number_from_filename(path)

        groups[group_name]["files"].append(
            {
                "path": str(path),
                "name": path.name,
                "episode": episode,
                "relative_path": str(path.relative_to(root_path)),
            }
        )

    result = []

    for group in groups.values():
        group["files"] = sorted(
            group["files"],
            key=lambda item: natural_sort_key(item["relative_path"]),
        )
        group["file_count"] = len(group["files"])
        result.append(group)

    return sorted(result, key=lambda item: natural_sort_key(item["name"]))


def make_group_id(kind: str, name: str) -> str:
    value = f"{kind}:{name}"
    value = value.lower()
    value = re.sub(r"[^a-z0-9а-яё]+", "_", value, flags=re.IGNORECASE)
    value = value.strip("_")
    return value or kind


def build_episode_map(files: list[Path], root_path: Path):
    """
    Строит карту:
    episode_number -> file

    Если у нескольких файлов один episode_number, первый остаётся главным.
    """
    episode_map = {}

    for path in sorted(files, key=lambda p: natural_sort_key(str(p.relative_to(root_path)))):
        episode = parse_episode_number_from_filename(path)

        if episode is None:
            continue

        if episode not in episode_map:
            episode_map[episode] = path

    return episode_map


def scan_release(root_path: str | Path):
    """
    Главная функция сканирования релиза.

    Возвращает структуру:
    {
      root_path,
      has_external_tracks,
      video_count,
      audio_count,
      subtitle_count,
      videos,
      audio_groups,
      subtitle_groups
    }
    """
    root = Path(root_path)

    collected = collect_files(root)

    video_files = collected["video_files"]
    audio_files = collected["audio_files"]
    subtitle_files = collected["subtitle_files"]

    videos = []

    for path in video_files:
        videos.append(
            {
                "path": str(path),
                "name": path.name,
                "episode": parse_episode_number_from_filename(path),
                "relative_path": str(path.relative_to(root)),
            }
        )

    audio_groups = group_external_files(root, audio_files, "audio")
    subtitle_groups = group_external_files(root, subtitle_files, "subtitles")

    return {
        "root_path": str(root),
        "has_external_tracks": bool(audio_files or subtitle_files),
        "video_count": len(video_files),
        "audio_count": len(audio_files),
        "subtitle_count": len(subtitle_files),
        "videos": videos,
        "audio_groups": audio_groups,
        "subtitle_groups": subtitle_groups,
    }


def selected_groups_to_files(
    root_path: str | Path,
    selected_group_ids: list[str],
    groups: list[dict],
):
    selected = {}

    for group in groups:
        group_id = group.get("id")

        if group_id not in selected_group_ids:
            continue

        files = []

        for item in group.get("files", []):
            files.append(Path(item["path"]))

        selected[group_id] = {
            "group": group,
            "files": files,
            "episode_map": build_episode_map(files, Path(root_path)),
        }

    return selected
