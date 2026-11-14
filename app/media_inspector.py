import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from media_scan import scan_release

logger = logging.getLogger(__name__)


def inspect_file_with_mkvmerge(
    file_path: Path | str,
    raw_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Runs `mkvmerge -J <file_path>` via subprocess (or reads mock/cached JSON if provided in raw_json)
    and parses the output into a normalized schema:

    {
        "format": "mkv",
        "duration": float,
        "video_tracks": [
            {
                "id": int,
                "codec": str,
                "properties": {
                    "width": int | None,
                    "height": int | None,
                    "pixel_dimensions": str,
                    "default_track": bool,
                }
            }
        ],
        "audio_tracks": [
            {
                "id": int,
                "codec": str,
                "channels": int,
                "channel_layout": str,
                "language": str,
                "language_ietf": str,
                "name": str,
                "default_track": bool,
                "forced_track": bool,
                "bit_rate": int,
            }
        ],
        "subtitle_tracks": [
            {
                "id": int,
                "codec": str,
                "language": str,
                "language_ietf": str,
                "name": str,
                "default_track": bool,
                "forced_track": bool,
            }
        ],
        "attachments": [
            {
                "id": int,
                "file_name": str,
                "content_type": str,
                "size": int,
            }
        ],
    }
    """
    path = Path(file_path)

    if raw_json is not None:
        data = raw_json
    else:
        if not path.exists():
            raise FileNotFoundError(f"Media file does not exist: {path}")

        try:
            result = subprocess.run(
                ["mkvmerge", "-J", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "mkvmerge binary not found. Please install mkvtoolnix."
            ) from exc

        if result.returncode != 0 and not result.stdout.strip():
            raise RuntimeError(
                f"mkvmerge -J failed for {path} with code {result.returncode}:\n{result.stderr}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse mkvmerge -J JSON output for {path}: {result.stdout[:500]}"
            ) from exc

    # Parse container info
    container = data.get("container", {})
    container_props = container.get("properties", {})
    container_type = str(container.get("type", "Matroska")).lower()

    if "matroska" in container_type or "webm" in container_type:
        fmt = "mkv"
    elif "quicktime" in container_type or "mp4" in container_type:
        fmt = "mp4"
    else:
        fmt = path.suffix.lstrip(".").lower() or "mkv"

    # Duration parsing: mkvmerge typically provides duration in nanoseconds
    duration_raw = container_props.get("duration")
    if duration_raw is not None:
        try:
            dur_float = float(duration_raw)
            # If greater than 1 million, assume nanoseconds
            duration = dur_float / 1_000_000_000.0 if dur_float > 1_000_000 else dur_float
        except (ValueError, TypeError):
            duration = 0.0
    else:
        duration = 0.0

    raw_tracks = data.get("tracks", [])

    video_tracks: list[dict[str, Any]] = []
    audio_tracks: list[dict[str, Any]] = []
    subtitle_tracks: list[dict[str, Any]] = []

    for track in raw_tracks:
        track_type = track.get("type")
        props = track.get("properties", {})
        track_id = int(track.get("id", 0))
        codec = str(track.get("codec", ""))

        if track_type == "video":
            pixel_dims = str(props.get("pixel_dimensions", ""))
            width = props.get("pixel_width") or props.get("width")
            height = props.get("pixel_height") or props.get("height")

            if (width is None or height is None) and pixel_dims and "x" in pixel_dims:
                parts = pixel_dims.split("x")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    width = int(parts[0])
                    height = int(parts[1])

            if not pixel_dims and width is not None and height is not None:
                pixel_dims = f"{width}x{height}"

            default_track = bool(props.get("default_track", True))

            video_tracks.append(
                {
                    "id": track_id,
                    "codec": codec,
                    "properties": {
                        "width": width,
                        "height": height,
                        "pixel_dimensions": pixel_dims,
                        "default_track": default_track,
                    },
                }
            )

        elif track_type == "audio":
            channels = int(props.get("audio_channels") or props.get("channels", 2))
            channel_layout = str(props.get("channel_layout") or props.get("audio_channel_layout") or "")
            language = str(props.get("language") or "und")
            language_ietf = str(props.get("language_ietf") or props.get("language") or "und")
            name = str(props.get("track_name") or props.get("name") or "")
            default_track = bool(props.get("default_track", False))
            forced_track = bool(props.get("forced_track", False))

            bit_rate_raw = props.get("tag_bps") or props.get("bit_rate") or 0
            try:
                bit_rate = int(bit_rate_raw)
            except (ValueError, TypeError):
                bit_rate = 0

            audio_tracks.append(
                {
                    "id": track_id,
                    "codec": codec,
                    "channels": channels,
                    "channel_layout": channel_layout,
                    "language": language,
                    "language_ietf": language_ietf,
                    "name": name,
                    "default_track": default_track,
                    "forced_track": forced_track,
                    "bit_rate": bit_rate,
                }
            )

        elif track_type == "subtitles":
            language = str(props.get("language") or "und")
            language_ietf = str(props.get("language_ietf") or props.get("language") or "und")
            name = str(props.get("track_name") or props.get("name") or "")
            default_track = bool(props.get("default_track", False))
            forced_track = bool(props.get("forced_track", False))

            subtitle_tracks.append(
                {
                    "id": track_id,
                    "codec": codec,
                    "language": language,
                    "language_ietf": language_ietf,
                    "name": name,
                    "default_track": default_track,
                    "forced_track": forced_track,
                }
            )

    attachments: list[dict[str, Any]] = []
    for att in data.get("attachments", []):
        size_raw = att.get("size", 0)
        try:
            size_val = int(size_raw)
        except (ValueError, TypeError):
            size_val = 0

        attachments.append(
            {
                "id": int(att.get("id", 0)),
                "file_name": str(att.get("file_name", "")),
                "content_type": str(att.get("content_type", "")),
                "size": size_val,
            }
        )

    return {
        "format": fmt,
        "duration": duration,
        "video_tracks": video_tracks,
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
        "attachments": attachments,
    }


def scan_release_tracks(staging_path: Path | str) -> dict[str, Any]:
    """
    Combines external files scanning (from `media_scan.scan_release`) with
    `inspect_file_with_mkvmerge` on the first/sample video file.

    Produces a unified track schema containing:
    - all fields from scan_release (root_path, videos, audio_groups, subtitle_groups, has_external_tracks, ...)
    - internal_tracks: structured tracks from inside the primary video container
    - external_groups: grouped external audio and subtitle files for UI selection
    """
    scan = scan_release(staging_path)

    videos = scan.get("videos", [])
    internal_tracks: dict[str, Any] = {
        "format": "mkv",
        "duration": 0.0,
        "video_tracks": [],
        "audio_tracks": [],
        "subtitle_tracks": [],
        "attachments": [],
    }

    if videos:
        first_video_path = Path(videos[0]["path"])
        if first_video_path.exists():
            try:
                internal_tracks = inspect_file_with_mkvmerge(first_video_path)
            except Exception as exc:
                logger.warning(
                    "Failed to inspect first video %s with mkvmerge: %s",
                    first_video_path,
                    exc,
                )

    result = dict(scan)
    result["internal_tracks"] = internal_tracks
    result["external_groups"] = {
        "audio": scan.get("audio_groups", []),
        "subtitles": scan.get("subtitle_groups", []),
    }

    return result
