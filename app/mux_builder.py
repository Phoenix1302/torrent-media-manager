import os
from pathlib import Path
from typing import Any


def build_advanced_mkvmerge_cmd(
    output_file: Path | str,
    video_file: Path | str,
    keep_internal_audio_ids: list[int] | None = None,
    keep_internal_subtitle_ids: list[int] | None = None,
    external_audio: list[dict[str, Any]] | None = None,
    external_subtitles: list[dict[str, Any]] | None = None,
    default_audio_track_spec: tuple[int, int] | None = None,
    default_subtitle_track_spec: tuple[int, int] | None = None,
    track_order: list[str] | str | None = None,
) -> list[str]:
    """
    Constructs an advanced mkvmerge command with fine-grained track customization:
    - keep/drop internal tracks (via --audio-tracks / --no-audio, --subtitle-tracks / --no-subtitles)
    - add external audio & subtitle files with track names, language, default/forced flags, audio delay (--sync)
    - track ordering (--track-order)
    - default track designation for audio and subtitles
    """
    cmd: list[str] = ["mkvmerge", "-o", str(output_file)]

    # Global options: track-order
    if track_order:
        if isinstance(track_order, (list, tuple)):
            order_str = ",".join(str(item) for item in track_order)
        else:
            order_str = str(track_order).strip()
        if order_str:
            cmd.extend(["--track-order", order_str])

    # --- Source 0: Video file options ---
    # Internal audio
    if not keep_internal_audio_ids:
        cmd.append("--no-audio")
    else:
        audio_ids_str = ",".join(str(tid) for tid in keep_internal_audio_ids)
        cmd.extend(["--audio-tracks", audio_ids_str])

    # Internal subtitles
    if not keep_internal_subtitle_ids:
        cmd.append("--no-subtitles")
    else:
        sub_ids_str = ",".join(str(tid) for tid in keep_internal_subtitle_ids)
        cmd.extend(["--subtitle-tracks", sub_ids_str])

    # Default flags on internal tracks (source 0)
    if default_audio_track_spec and default_audio_track_spec[0] == 0:
        cmd.extend(["--default-track-flag", f"{default_audio_track_spec[1]}:1"])

    if default_subtitle_track_spec and default_subtitle_track_spec[0] == 0:
        cmd.extend(["--default-track-flag", f"{default_subtitle_track_spec[1]}:1"])

    # Append primary video container
    cmd.append(str(video_file))

    # --- Source 1..N: External audio and subtitle files ---
    current_source_idx = 1

    # External audio files
    for item in external_audio or []:
        track_id = item.get("track_id", 0)
        language = item.get("language") or "und"
        track_name = item.get("track_name") or ""
        sync_ms = item.get("sync_ms")

        # Determine default flag
        if default_audio_track_spec is not None:
            is_default = (default_audio_track_spec == (current_source_idx, track_id))
        else:
            is_default = bool(item.get("is_default", False))

        is_forced = bool(item.get("is_forced", False))

        # Language and Track Name
        if language:
            cmd.extend(["--language", f"{track_id}:{language}"])
        if track_name:
            cmd.extend(["--track-name", f"{track_id}:{track_name}"])

        # Default & Forced flags
        cmd.extend(["--default-track-flag", f"{track_id}:{1 if is_default else 0}"])
        if "is_forced" in item or is_forced:
            cmd.extend(["--forced-display-flag", f"{track_id}:{1 if is_forced else 0}"])

        # Delay / Sync
        if sync_ms is not None and sync_ms != 0:
            cmd.extend(["--sync", f"{track_id}:{sync_ms}"])

        cmd.append(str(item["path"]))
        current_source_idx += 1

    # External subtitle files
    for item in external_subtitles or []:
        track_id = item.get("track_id", 0)
        language = item.get("language") or "und"
        track_name = item.get("track_name") or ""
        sync_ms = item.get("sync_ms")

        # Determine default flag
        if default_subtitle_track_spec is not None:
            is_default = (default_subtitle_track_spec == (current_source_idx, track_id))
        else:
            is_default = bool(item.get("is_default", False))

        is_forced = bool(item.get("is_forced", False))

        if language:
            cmd.extend(["--language", f"{track_id}:{language}"])
        if track_name:
            cmd.extend(["--track-name", f"{track_id}:{track_name}"])

        cmd.extend(["--default-track-flag", f"{track_id}:{1 if is_default else 0}"])
        cmd.extend(["--forced-display-flag", f"{track_id}:{1 if is_forced else 0}"])

        if sync_ms is not None and sync_ms != 0:
            cmd.extend(["--sync", f"{track_id}:{sync_ms}"])

        cmd.append(str(item["path"]))
        current_source_idx += 1

    return cmd
