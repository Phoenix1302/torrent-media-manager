import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from database import update_mux_selection
from media_scan import (
    AUDIO_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    build_episode_map,
    natural_sort_key,
    parse_episode_number_from_filename,
    scan_release,
)
from mux_builder import build_advanced_mkvmerge_cmd

logger = logging.getLogger(__name__)

PROGRESS_REGEX = re.compile(r"Progress:\s*(\d+(?:\.\d+)?)%")


def parse_mkvmerge_progress(text: str) -> float | None:
    """
    Parses 'Progress: XX%' from text. Returns the last percentage found as a float, or None.
    Example: 'Progress: 73%' -> 73.0
    """
    matches = PROGRESS_REGEX.findall(text)
    if matches:
        try:
            return float(matches[-1])
        except (ValueError, TypeError):
            return None
    return None


class MuxProgressTracker:
    """
    Maintains in-memory state of active muxing jobs for real-time progress tracking.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}

    def start_job(self, job_id: int, total_files: int, first_file_name: str = "") -> dict[str, Any]:
        with self._lock:
            state = {
                "job_id": job_id,
                "status": "muxing",
                "current_file_index": 1 if total_files > 0 else 0,
                "total_files": total_files,
                "current_percent": 0.0,
                "overall_percent": 0.0,
                "current_file_name": first_file_name,
                "log": [],
                "error": None,
            }
            self._jobs[job_id] = state
            return dict(state)

    def update_progress(
        self,
        job_id: int,
        current_percent: float,
        current_file_index: int | None = None,
        current_file_name: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return

            if current_file_index is not None:
                job["current_file_index"] = current_file_index
            if current_file_name is not None:
                job["current_file_name"] = current_file_name

            clamped_current = max(0.0, min(100.0, float(current_percent)))
            job["current_percent"] = round(clamped_current, 1)

            total = max(1, job["total_files"])
            file_idx = max(1, job["current_file_index"])
            # overall: ((completed files + progress on current file) / total) * 100
            overall = ((file_idx - 1 + (clamped_current / 100.0)) / total) * 100.0
            job["overall_percent"] = round(max(0.0, min(100.0, overall)), 1)

    def add_log(self, job_id: int, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["log"].append(message)
                if len(job["log"]) > 200:
                    job["log"] = job["log"][-200:]

    def complete_job(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "completed"
                job["current_percent"] = 100.0
                job["overall_percent"] = 100.0

    def fail_job(self, job_id: int, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = error

    def get_progress(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return dict(job)

    def get_all_active(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            return {jid: dict(data) for jid, data in self._jobs.items()}

    def clear_job(self, job_id: int) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


# Global tracker instance
progress_tracker = MuxProgressTracker()


def get_mux_progress(job_id: int) -> dict[str, Any] | None:
    return progress_tracker.get_progress(job_id)


def get_all_active_mux() -> dict[int, dict[str, Any]]:
    return progress_tracker.get_all_active()


def ensure_clean_result_dir(staging_path: str | Path) -> Path:
    staging = Path(staging_path)
    result_dir = staging / "Result"

    if result_dir.exists():
        shutil.rmtree(result_dir)

    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir


def find_group_by_id(groups: list[dict], group_id: str):
    for group in groups:
        if group.get("id") == group_id:
            return group
    return None


def group_files_to_paths(group: dict) -> list[Path]:
    files = []
    for item in group.get("files", []):
        path = Path(item["path"])
        if path.exists():
            files.append(path)
    return sorted(files, key=lambda p: natural_sort_key(str(p)))


def build_order_map(files: list[Path]) -> dict[int, Path]:
    return {
        index: path
        for index, path in enumerate(sorted(files, key=lambda p: natural_sort_key(str(p))))
    }


def match_external_file(
    video_file: Path,
    video_index: int,
    selected_files: list[Path],
    selected_episode_map: dict[int, Path],
    selected_order_map: dict[int, Path],
) -> Path | None:
    """
    Attempts to match an external track to a video by episode number first;
    falls back to index ordering.
    """
    video_episode = parse_episode_number_from_filename(video_file)

    if video_episode is not None and video_episode in selected_episode_map:
        return selected_episode_map[video_episode]

    return selected_order_map.get(video_index)


def make_output_name(video_file: Path) -> str:
    return f"{video_file.stem}.muxed.mkv"


def build_mkvmerge_command(
    output_file: Path,
    video_file: Path,
    audio_files: list[tuple[Path, str]],
    subtitle_files: list[tuple[Path, str]],
) -> list[str]:
    """
    Legacy command builder kept for backward compatibility.
    """
    cmd = [
        "mkvmerge",
        "-o",
        str(output_file),
        str(video_file),
    ]

    for audio_file, track_name in audio_files:
        cmd.extend(
            [
                "--language",
                "0:rus",
                "--track-name",
                f"0:{track_name}",
                str(audio_file),
            ]
        )

    for subtitle_file, track_name in subtitle_files:
        cmd.extend(
            [
                "--language",
                "0:rus",
                "--track-name",
                f"0:{track_name}",
                str(subtitle_file),
            ]
        )

    return cmd


def run_command(cmd: list[str]) -> dict[str, Any]:
    """
    Synchronous subprocess runner with real-time stdout capture.
    """
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return {
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


async def run_cmd_async_with_progress(
    cmd: list[str],
    job_id: int | None = None,
    tracker: MuxProgressTracker | None = None,
) -> dict[str, Any]:
    """
    Runs an mkvmerge command asynchronously while parsing progress and updating tracker.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def read_stdout():
        buffer = ""
        while True:
            chunk = await process.stdout.read(256)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            stdout_chunks.append(text)
            buffer += text
            p = parse_mkvmerge_progress(buffer)
            if p is not None and tracker and job_id is not None:
                tracker.update_progress(job_id, current_percent=p)
            if len(buffer) > 512:
                buffer = buffer[-256:]

    async def read_stderr():
        while True:
            chunk = await process.stderr.read(256)
            if not chunk:
                break
            stderr_chunks.append(chunk.decode("utf-8", errors="replace"))

    await asyncio.gather(read_stdout(), read_stderr(), process.wait())

    return {
        "returncode": process.returncode,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
    }


def _prepare_groups_for_matching(staging: Path, scan: dict, selected_tracks: dict):
    """
    Extracts audio and subtitle group matchers from selected_tracks.
    Supports both legacy {audio: [{group_id, track_name}], subtitles: [...]}
    and advanced custom track options.
    """
    selected_audio = selected_tracks.get("audio") or selected_tracks.get("external_audio") or []
    selected_subtitles = selected_tracks.get("subtitles") or selected_tracks.get("external_subtitles") or []

    audio_group_data = []
    for sel in selected_audio:
        group_id = sel.get("group_id")
        track_name = sel.get("track_name") or "RUS Dub"
        language = sel.get("language") or "rus"
        is_default = bool(sel.get("is_default", False))
        is_forced = bool(sel.get("is_forced", False))
        sync_ms = sel.get("sync_ms")

        group = find_group_by_id(scan.get("audio_groups", []), group_id) if group_id else None
        files = group_files_to_paths(group) if group else []

        # If direct path given instead of group
        direct_path = sel.get("path")
        if not files and direct_path and Path(direct_path).exists():
            files = [Path(direct_path)]

        audio_group_data.append(
            {
                "group": group,
                "track_name": track_name,
                "language": language,
                "is_default": is_default,
                "is_forced": is_forced,
                "sync_ms": sync_ms,
                "files": files,
                "episode_map": build_episode_map(files, staging),
                "order_map": build_order_map(files),
            }
        )

    subtitle_group_data = []
    for sel in selected_subtitles:
        group_id = sel.get("group_id")
        track_name = sel.get("track_name") or "RUS Sub"
        language = sel.get("language") or "rus"
        is_default = bool(sel.get("is_default", False))
        is_forced = bool(sel.get("is_forced", False))
        sync_ms = sel.get("sync_ms")

        group = find_group_by_id(scan.get("subtitle_groups", []), group_id) if group_id else None
        files = group_files_to_paths(group) if group else []

        direct_path = sel.get("path")
        if not files and direct_path and Path(direct_path).exists():
            files = [Path(direct_path)]

        subtitle_group_data.append(
            {
                "group": group,
                "track_name": track_name,
                "language": language,
                "is_default": is_default,
                "is_forced": is_forced,
                "sync_ms": sync_ms,
                "files": files,
                "episode_map": build_episode_map(files, staging),
                "order_map": build_order_map(files),
            }
        )

    return audio_group_data, subtitle_group_data


def _build_command_for_video(
    staging: Path,
    video_file: Path,
    index: int,
    output_file: Path,
    audio_group_data: list[dict],
    subtitle_group_data: list[dict],
    selected_tracks: dict,
):
    matched_audio: list[dict[str, Any]] = []
    for gdata in audio_group_data:
        matched = match_external_file(
            video_file=video_file,
            video_index=index,
            selected_files=gdata["files"],
            selected_episode_map=gdata["episode_map"],
            selected_order_map=gdata["order_map"],
        )
        if matched:
            matched_audio.append(
                {
                    "path": matched,
                    "track_name": gdata["track_name"],
                    "language": gdata["language"],
                    "is_default": gdata["is_default"],
                    "is_forced": gdata["is_forced"],
                    "sync_ms": gdata["sync_ms"],
                }
            )

    matched_subtitles: list[dict[str, Any]] = []
    for gdata in subtitle_group_data:
        matched = match_external_file(
            video_file=video_file,
            video_index=index,
            selected_files=gdata["files"],
            selected_episode_map=gdata["episode_map"],
            selected_order_map=gdata["order_map"],
        )
        if matched:
            matched_subtitles.append(
                {
                    "path": matched,
                    "track_name": gdata["track_name"],
                    "language": gdata["language"],
                    "is_default": gdata["is_default"],
                    "is_forced": gdata["is_forced"],
                    "sync_ms": gdata["sync_ms"],
                }
            )

    # Check whether internal track settings are explicitly provided
    has_internal_audio_spec = "keep_internal_audio_ids" in selected_tracks
    has_internal_sub_spec = "keep_internal_subtitle_ids" in selected_tracks

    if not has_internal_audio_spec and not has_internal_sub_spec:
        # Legacy behavior: if neither keep_internal_* is defined, use legacy command
        # which retains all original tracks without flags
        legacy_audio = [(m["path"], m["track_name"]) for m in matched_audio]
        legacy_subs = [(m["path"], m["track_name"]) for m in matched_subtitles]
        cmd = build_mkvmerge_command(
            output_file=output_file,
            video_file=video_file,
            audio_files=legacy_audio,
            subtitle_files=legacy_subs,
        )
    else:
        cmd = build_advanced_mkvmerge_cmd(
            output_file=output_file,
            video_file=video_file,
            keep_internal_audio_ids=selected_tracks.get("keep_internal_audio_ids"),
            keep_internal_subtitle_ids=selected_tracks.get("keep_internal_subtitle_ids"),
            external_audio=matched_audio,
            external_subtitles=matched_subtitles,
            default_audio_track_spec=selected_tracks.get("default_audio_track_spec"),
            default_subtitle_track_spec=selected_tracks.get("default_subtitle_track_spec"),
            track_order=selected_tracks.get("track_order"),
        )

    return cmd, matched_audio, matched_subtitles


async def mux_release_async(
    staging_path: str | Path,
    selected_tracks: dict,
    job_id: int | None = None,
    tracker: MuxProgressTracker | None = None,
) -> dict[str, Any]:
    """
    Asynchronously executes mkvmerge for all video files in staging_path with real-time progress.
    """
    staging = Path(staging_path)
    scan = scan_release(staging)

    video_items = scan.get("videos", [])
    video_files = [
        Path(item["path"])
        for item in video_items
        if Path(item["path"]).exists()
    ]
    video_files = sorted(video_files, key=lambda p: natural_sort_key(str(p.relative_to(staging))))

    if not video_files:
        raise RuntimeError(f"No video files found in staging path: {staging}")

    total_files = len(video_files)
    if tracker and job_id is not None:
        tracker.start_job(job_id=job_id, total_files=total_files, first_file_name=video_files[0].name)

    result_dir = ensure_clean_result_dir(staging)
    audio_group_data, subtitle_group_data = _prepare_groups_for_matching(staging, scan, selected_tracks)

    items = []

    for index, video_file in enumerate(video_files):
        output_file = result_dir / make_output_name(video_file)

        if tracker and job_id is not None:
            tracker.update_progress(
                job_id=job_id,
                current_percent=0.0,
                current_file_index=index + 1,
                current_file_name=video_file.name,
            )

        cmd, matched_audio, matched_subtitles = _build_command_for_video(
            staging=staging,
            video_file=video_file,
            index=index,
            output_file=output_file,
            audio_group_data=audio_group_data,
            subtitle_group_data=subtitle_group_data,
            selected_tracks=selected_tracks,
        )

        res = await run_cmd_async_with_progress(cmd, job_id=job_id, tracker=tracker)

        if res["returncode"] != 0:
            raise RuntimeError(
                f"mkvmerge failed for {video_file}\nSTDOUT:\n{res['stdout']}\nSTDERR:\n{res['stderr']}"
            )

        if tracker and job_id is not None:
            tracker.update_progress(
                job_id=job_id,
                current_percent=100.0,
                current_file_index=index + 1,
                current_file_name=video_file.name,
            )

        items.append(
            {
                "video": str(video_file),
                "output": str(output_file),
                "audio": [
                    {
                        "path": str(m["path"]),
                        "track_name": m["track_name"],
                    }
                    for m in matched_audio
                ],
                "subtitles": [
                    {
                        "path": str(m["path"]),
                        "track_name": m["track_name"],
                    }
                    for m in matched_subtitles
                ],
                "command": cmd,
                "stdout": res["stdout"],
                "stderr": res["stderr"],
            }
        )

    summary = {
        "status": "ok",
        "staging_path": str(staging),
        "result_dir": str(result_dir),
        "selected_tracks": selected_tracks,
        "items": items,
    }

    summary_path = result_dir / "mux_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return summary


def mux_release(
    staging_path: str | Path,
    selected_tracks: dict,
    job_id: int | None = None,
) -> dict[str, Any]:
    """
    Synchronous entry point for muxing (for compatibility and synchronous contexts).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                mux_release_async(staging_path, selected_tracks, job_id=job_id, tracker=progress_tracker),
            ).result()
    else:
        return asyncio.run(
            mux_release_async(staging_path, selected_tracks, job_id=job_id, tracker=progress_tracker)
        )


async def run_mux_job_async(
    job_id: int,
    staging_path: str | Path,
    selected_tracks: dict,
) -> dict[str, Any]:
    """
    Complete background task spawned when muxing is initiated.
    Executes muxing, updates database status, and updates progress tracker.
    """
    try:
        mux_result = await mux_release_async(
            staging_path=staging_path,
            selected_tracks=selected_tracks,
            job_id=job_id,
            tracker=progress_tracker,
        )

        update_mux_selection(
            job_id=job_id,
            status="mux_review",
            mux_result_path=mux_result.get("result_dir", ""),
            selected_tracks=selected_tracks,
            error="",
        )

        progress_tracker.complete_job(job_id)
        return mux_result

    except Exception as exc:
        logger.exception("Background mux job %s failed: %s", job_id, exc)
        update_mux_selection(
            job_id=job_id,
            status="error",
            mux_result_path="",
            selected_tracks=selected_tracks,
            error=str(exc),
        )
        progress_tracker.fail_job(job_id, str(exc))
        raise


def has_result_files(staging_path: str | Path) -> bool:
    result_dir = Path(staging_path) / "Result"
    if not result_dir.exists():
        return False
    for path in result_dir.iterdir():
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            return True
    return False


def list_result_video_files(staging_path: str | Path) -> list[Path]:
    result_dir = Path(staging_path) / "Result"
    if not result_dir.exists():
        return []
    return sorted(
        [
            path
            for path in result_dir.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ],
        key=lambda p: natural_sort_key(p.name),
    )
