import os

APP_VERSION = "1.1.1"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}

QBITTORRENT_HOST = os.getenv("QBITTORRENT_HOST", "http://127.0.0.1:8080")
QBITTORRENT_USER = os.getenv("QBITTORRENT_USER", "admin")
QBITTORRENT_PASS = os.getenv("QBITTORRENT_PASS", "adminadmin")

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

QBIT_INCOMPLETE_PATH = os.getenv(
    "QBIT_INCOMPLETE_PATH",
    "/mnt/dlc_data/torrents/InComplete",
)

QBIT_COMPLETE_PATH = os.getenv(
    "QBIT_COMPLETE_PATH",
    "/mnt/dlc_data/torrents/Complete",
)

WATCH_PATH = os.getenv(
    "WATCH_PATH",
    QBIT_COMPLETE_PATH,
)


DOWNLOAD_DISK_PATH = os.getenv(
    "DOWNLOAD_DISK_PATH",
    "/mnt/dlc_data/torrents",
)

MIN_FREE_DLC_SPACE_GB = float(os.getenv("MIN_FREE_DLC_SPACE_GB", "20"))
MIN_FREE_DLC_SPACE_BYTES = int(MIN_FREE_DLC_SPACE_GB * 1024 * 1024 * 1024)
QBIT_PREALLOCATE_DISK_SPACE = env_bool("QBIT_PREALLOCATE_DISK_SPACE", True)

STAGING_PATH = os.getenv(
    "STAGING_PATH",
    "/mnt/main_data/Media/InComplete",
)

MOVIES_PATH = os.getenv("MOVIES_PATH", "/mnt/main_data/Media/Video/Films")
TV_PATH = os.getenv("TV_PATH", "/mnt/main_data/Media/Video/Serials")
ANIME_PATH = os.getenv("ANIME_PATH", "/mnt/main_data/Media/Video/Anime")
CARTOONS_PATH = os.getenv("CARTOONS_PATH", "/mnt/main_data/Media/Video/Mults")

IMPORT_MODE = os.getenv("IMPORT_MODE", "copy")
DELETE_SOURCE_AFTER_IMPORT = os.getenv("DELETE_SOURCE_AFTER_IMPORT", "false").lower() == "true"

DB_PATH = os.getenv("DB_PATH", "/app/data/app.db")
AUTO_IMPORT_INTERVAL_SECONDS = int(os.getenv("AUTO_IMPORT_INTERVAL_SECONDS", "60"))

DIR_MODE = int(os.getenv("DIR_MODE", "775"), 8)
FILE_MODE = int(os.getenv("FILE_MODE", "775"), 8)
UMASK = int(os.getenv("UMASK", "002"), 8)

# User and Group IDs (Ownership)
# Default: smbwriter (1000:1000) for media library, qbittorrent-nox (1002:1003) for downloads
PUID = int(os.getenv("PUID", os.getenv("TARGET_UID", "1000")))
PGID = int(os.getenv("PGID", os.getenv("TARGET_GID", "1000")))
TARGET_UID = PUID
TARGET_GID = PGID
QBIT_UID = int(os.getenv("QBIT_UID", "1002"))
QBIT_GID = int(os.getenv("QBIT_GID", "1003"))

