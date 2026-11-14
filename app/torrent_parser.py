"""Bencode decoder/encoder and .torrent metadata parser.

This module provides pure-Python bencode encoding and decoding with zero external
dependencies, along with high-level functions to parse .torrent files and raw bytes.
It supports standard single-file and multi-file torrents, BEP 0047 (name.utf-8,
path.utf-8), and fallback character encodings (e.g. cp1251 for Russian trackers,
latin-1).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, BinaryIO, Union


class BencodeError(Exception):
    """Base exception for bencode encoding/decoding errors."""


class BencodeDecodeError(BencodeError):
    """Raised when decoding malformed bencode data fails."""


class BencodeEncodeError(BencodeError):
    """Raised when encoding an object to bencode fails."""


class TorrentParseError(Exception):
    """Raised when parsing .torrent metadata fails."""


def decode_text(
    value: Any,
    encoding: str = "utf-8",
    fallback_encodings: tuple[str, ...] = ("cp1251", "latin-1"),
) -> str:
    """Safely decode bytes or strings into a text string.

    Tries the primary encoding (default utf-8). If decoding fails, tries each
    fallback encoding in order. If all fail, falls back to latin-1 with error replacement.

    Args:
        value: Bytes, bytearray, string, or number to decode.
        encoding: Primary character encoding to try.
        fallback_encodings: Sequence of fallback encodings (e.g. cp1251, latin-1).

    Returns:
        Decoded string representation.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return str(value)

    raw = bytes(value)

    # 1. Try primary encoding
    if encoding:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass

    # 2. Try fallbacks
    for enc in fallback_encodings:
        if not enc or enc.lower() == (encoding or "").lower():
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # 3. Guaranteed fallback: latin-1 maps all bytes 0-255
    return raw.decode("latin-1", errors="replace")


def _decode_bencode_item(
    data: bytes,
    pos: int,
    capture_info: bool = False,
) -> tuple[Any, int, bytes | None]:
    """Decode a single bencode element starting at pos.

    Returns:
        tuple of (decoded_value, next_pos, raw_info_bytes_if_captured)
    """
    if pos >= len(data):
        raise BencodeDecodeError(f"Unexpected end of bencode data at offset {pos}")

    lead = data[pos : pos + 1]
    raw_info: bytes | None = None

    if lead == b"i":
        # Integer: i<int>e
        pos += 1
        end = data.find(b"e", pos)
        if end == -1:
            raise BencodeDecodeError(f"Unterminated integer starting at offset {pos - 1}")
        num_bytes = data[pos:end]
        if not num_bytes:
            raise BencodeDecodeError(f"Empty integer at offset {pos - 1}")
        if num_bytes == b"-0":
            raise BencodeDecodeError("Negative zero '-0' is not valid in bencode")
        if len(num_bytes) > 1 and num_bytes.startswith(b"0"):
            raise BencodeDecodeError(f"Leading zeros not allowed in integer at offset {pos - 1}")
        if len(num_bytes) > 2 and num_bytes.startswith(b"-0"):
            raise BencodeDecodeError(f"Invalid negative integer at offset {pos - 1}")
        try:
            return int(num_bytes), end + 1, None
        except ValueError as err:
            raise BencodeDecodeError(f"Invalid integer {num_bytes!r}: {err}") from err

    elif lead == b"l":
        # List: l<items>e
        pos += 1
        items = []
        while pos < len(data) and data[pos : pos + 1] != b"e":
            item, pos, child_info = _decode_bencode_item(data, pos, capture_info=capture_info)
            if child_info is not None and raw_info is None:
                raw_info = child_info
            items.append(item)
        if pos >= len(data) or data[pos : pos + 1] != b"e":
            raise BencodeDecodeError("Unterminated list in bencode")
        return items, pos + 1, raw_info

    elif lead == b"d":
        # Dictionary: d<key1><val1>...e
        pos += 1
        result: dict[bytes, Any] = {}
        while pos < len(data) and data[pos : pos + 1] != b"e":
            key, pos, _ = _decode_bencode_item(data, pos, capture_info=False)
            if not isinstance(key, bytes):
                raise BencodeDecodeError(
                    f"Dictionary key must be bytes, got {type(key).__name__} at offset {pos}"
                )
            val_start = pos
            val, pos, child_info = _decode_bencode_item(data, pos, capture_info=capture_info)
            val_end = pos

            if capture_info and key == b"info" and raw_info is None:
                raw_info = data[val_start:val_end]
            elif child_info is not None and raw_info is None:
                raw_info = child_info

            result[key] = val

        if pos >= len(data) or data[pos : pos + 1] != b"e":
            raise BencodeDecodeError("Unterminated dictionary in bencode")
        return result, pos + 1, raw_info

    elif lead.isdigit():
        # Byte string: <length>:<bytes>
        colon = data.find(b":", pos)
        if colon == -1:
            raise BencodeDecodeError(f"Missing colon in string length at offset {pos}")
        try:
            length = int(data[pos:colon])
        except ValueError as err:
            raise BencodeDecodeError(f"Invalid string length at offset {pos}: {err}") from err
        if length < 0:
            raise BencodeDecodeError(f"Negative string length ({length}) at offset {pos}")
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeDecodeError(
                f"Byte string truncated: expected {length} bytes, available {len(data) - start}"
            )
        return data[start:end], end, None

    else:
        raise BencodeDecodeError(f"Unexpected character {lead!r} at offset {pos}")


def bdecode(data: bytes | bytearray | memoryview, strict: bool = False) -> Any:
    """Decode bencoded bytes into standard Python types.

    Types mapping:
        - Integers -> int
        - Byte strings -> bytes
        - Lists -> list
        - Dictionaries -> dict[bytes, Any]

    Args:
        data: Bencoded byte sequence.
        strict: If True, raises BencodeDecodeError when trailing data exists.

    Returns:
        Parsed Python structure.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"bdecode expects bytes-like object, got {type(data).__name__}")

    raw_bytes = bytes(data)
    if not raw_bytes:
        raise BencodeDecodeError("Cannot decode empty bytes")

    val, end, _ = _decode_bencode_item(raw_bytes, 0, capture_info=False)
    if strict and end < len(raw_bytes):
        raise BencodeDecodeError(f"Trailing data after bencode element at offset {end}")
    return val


def bencode(data: Any) -> bytes:
    """Encode Python objects into bencode bytes with canonical dictionary ordering.

    Supports:
        - int -> i<int>e
        - bytes, bytearray, memoryview -> <len>:<bytes>
        - str -> <len>:<utf-8 bytes>
        - list, tuple -> l<encoded_items>e
        - dict -> d<sorted_key_val_pairs>e (sorted lexicographically by raw bytes)

    Args:
        data: Object to encode.

    Returns:
        Canonical bencoded bytes.
    """
    # Note: bool is a subclass of int in Python, treat bool as int 1 or 0
    if isinstance(data, bool):
        return b"i1e" if data else b"i0e"

    if isinstance(data, int):
        return f"i{data}e".encode("ascii")

    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        return f"{len(raw)}:".encode("ascii") + raw

    if isinstance(data, str):
        raw = data.encode("utf-8")
        return f"{len(raw)}:".encode("ascii") + raw

    if isinstance(data, (list, tuple)):
        chunks = [b"l"]
        for item in data:
            chunks.append(bencode(item))
        chunks.append(b"e")
        return b"".join(chunks)

    if isinstance(data, dict):
        chunks = [b"d"]

        def _raw_key(k: Any) -> bytes:
            if isinstance(k, (bytes, bytearray, memoryview)):
                return bytes(k)
            if isinstance(k, str):
                return k.encode("utf-8")
            raise BencodeEncodeError(
                f"Dict key must be str or bytes, got {type(k).__name__}: {k!r}"
            )

        # In BitTorrent bencode, dict keys MUST be sorted lexicographically by raw bytes
        sorted_pairs = sorted(data.items(), key=lambda kv: _raw_key(kv[0]))
        for k, v in sorted_pairs:
            raw_k = _raw_key(k)
            chunks.append(f"{len(raw_k)}:".encode("ascii") + raw_k)
            chunks.append(bencode(v))
        chunks.append(b"e")
        return b"".join(chunks)

    raise BencodeEncodeError(f"Unsupported data type for bencode: {type(data).__name__} ({data!r})")


def _get_field(
    d: dict[Any, Any],
    *keys: str | bytes,
    default: Any = None,
) -> Any:
    """Retrieve field from dictionary checking both str and bytes versions of keys."""
    for k in keys:
        if k in d:
            return d[k]
        if isinstance(k, str):
            k_bytes = k.encode("utf-8")
            if k_bytes in d:
                return d[k_bytes]
        elif isinstance(k, (bytes, bytearray)):
            try:
                k_str = bytes(k).decode("utf-8")
                if k_str in d:
                    return d[k_str]
            except UnicodeDecodeError:
                pass
    return default


def parse_torrent_bytes(
    raw_bytes: bytes,
    fallback_encodings: tuple[str, ...] = ("cp1251", "latin-1"),
) -> dict[str, Any]:
    """Parse raw bytes of a .torrent file into a structured dictionary.

    Extracts release name, total size, piece length, info-hash (SHA-1), files list,
    creation date, creator, and comments. Handles single-file and multi-file torrents,
    UTF-8 BEP 0047 extensions (.utf-8 fields), and encoding fallbacks (e.g. cp1251).

    Args:
        raw_bytes: The complete binary content of the .torrent file.
        fallback_encodings: Encoding candidates if UTF-8 fails (default: cp1251, latin-1).

    Returns:
        Dict matching the Torrent Media Manager specification:
        {
            "info_hash": str,       # 40-character lowercase hex SHA-1 of 'info' dict
            "name": str,            # release name
            "total_size": int,      # total bytes (single file or sum of files)
            "piece_length": int,    # piece length in bytes
            "files": list of {"index": int, "path": str, "size": int},
            "is_multi_file": bool,  # whether release is multi-file
            "created_by": str,      # creator client or script
            "comment": str,         # torrent comment
            "creation_date": int | None # Unix timestamp or None
        }

    Raises:
        TorrentParseError: If data is not a valid dictionary or missing 'info'.
        BencodeDecodeError: If data is not valid bencode.
    """
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise TypeError(f"raw_bytes must be bytes-like, got {type(raw_bytes).__name__}")

    raw_data = bytes(raw_bytes)
    if not raw_data:
        raise TorrentParseError("Empty torrent data")

    # Decode with capture_info=True to obtain exact raw bytes of the 'info' dict
    parsed, _, raw_info = _decode_bencode_item(raw_data, 0, capture_info=True)

    if not isinstance(parsed, dict):
        raise TorrentParseError(f"Root torrent structure must be a dictionary, got {type(parsed).__name__}")

    info = _get_field(parsed, b"info", "info")
    if not isinstance(info, dict):
        raise TorrentParseError("Torrent missing 'info' dictionary")

    # Determine primary and fallback encodings
    encoding_hint_raw = _get_field(parsed, b"encoding", "encoding")
    primary_encoding = "utf-8"
    active_fallbacks = list(fallback_encodings)
    if encoding_hint_raw:
        hint_str = decode_text(encoding_hint_raw, encoding="utf-8").strip().lower()
        if hint_str and hint_str not in ("utf-8", "utf8"):
            # Put hint at the front of fallback list
            active_fallbacks.insert(0, hint_str)

    # 1. Info Hash (SHA-1 of the bencoded 'info' dict)
    if raw_info is not None:
        info_hash = hashlib.sha1(raw_info).hexdigest().lower()
    else:
        # Fallback to re-encoding the info dict canonically
        info_hash = hashlib.sha1(bencode(info)).hexdigest().lower()

    # 2. Release Name
    # BEP 0047: 'name.utf-8' overrides 'name'
    name_raw = _get_field(info, b"name.utf-8", "name.utf-8", b"name", "name", default=b"")
    name = decode_text(name_raw, encoding=primary_encoding, fallback_encodings=tuple(active_fallbacks))

    # 3. Piece length
    piece_length_raw = _get_field(info, b"piece length", "piece length", default=0)
    try:
        piece_length = int(piece_length_raw or 0)
    except (ValueError, TypeError):
        piece_length = 0

    # 4. Files & Multi-file check
    raw_files = _get_field(info, b"files", "files")
    files: list[dict[str, Any]] = []

    if isinstance(raw_files, list):
        # Multi-file torrent
        is_multi_file = True
        total_size = 0
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                continue
            length_raw = _get_field(item, b"length", "length", default=0)
            try:
                file_size = int(length_raw or 0)
            except (ValueError, TypeError):
                file_size = 0

            # BEP 0047: 'path.utf-8' overrides 'path'
            path_parts_raw = _get_field(item, b"path.utf-8", "path.utf-8", b"path", "path")

            parts: list[str] = []
            if isinstance(path_parts_raw, list):
                for part in path_parts_raw:
                    decoded_part = decode_text(
                        part,
                        encoding=primary_encoding,
                        fallback_encodings=tuple(active_fallbacks),
                    ).strip()
                    if decoded_part:
                        parts.append(decoded_part)
            elif path_parts_raw is not None:
                decoded_part = decode_text(
                    path_parts_raw,
                    encoding=primary_encoding,
                    fallback_encodings=tuple(active_fallbacks),
                ).strip()
                if decoded_part:
                    parts.append(decoded_part)

            file_path = "/".join(parts) if parts else f"file_{index}"
            files.append({
                "index": index,
                "path": file_path,
                "size": file_size,
            })
            total_size += file_size

    else:
        # Single-file torrent
        is_multi_file = False
        length_raw = _get_field(info, b"length", "length", default=0)
        try:
            total_size = int(length_raw or 0)
        except (ValueError, TypeError):
            total_size = 0

        file_path = name if name else "file_0"
        files.append({
            "index": 0,
            "path": file_path,
            "size": total_size,
        })

    # 5. Metadata fields (created by, comment, creation date)
    created_by_raw = _get_field(
        parsed,
        b"created by.utf-8",
        "created by.utf-8",
        b"created by",
        "created by",
        default=b"",
    )
    created_by = decode_text(
        created_by_raw,
        encoding=primary_encoding,
        fallback_encodings=tuple(active_fallbacks),
    )

    comment_raw = _get_field(
        parsed,
        b"comment.utf-8",
        "comment.utf-8",
        b"comment",
        "comment",
        default=b"",
    )
    comment = decode_text(
        comment_raw,
        encoding=primary_encoding,
        fallback_encodings=tuple(active_fallbacks),
    )

    creation_date_raw = _get_field(parsed, b"creation date", "creation date", default=None)
    creation_date: int | None = None
    if creation_date_raw is not None:
        try:
            creation_date = int(creation_date_raw)
        except (ValueError, TypeError):
            creation_date = None

    return {
        "info_hash": info_hash,
        "name": name,
        "total_size": total_size,
        "piece_length": piece_length,
        "files": files,
        "is_multi_file": is_multi_file,
        "created_by": created_by,
        "comment": comment,
        "creation_date": creation_date,
    }


def parse_torrent_file(
    file_path: str | Path,
    fallback_encodings: tuple[str, ...] = ("cp1251", "latin-1"),
) -> dict[str, Any]:
    """Parse a .torrent file from filesystem path.

    Args:
        file_path: Path to the .torrent file.
        fallback_encodings: Sequence of fallback character encodings if UTF-8 fails.

    Returns:
        Structured torrent metadata dictionary.

    Raises:
        FileNotFoundError: If the specified file does not exist.
        TorrentParseError: If the file is not a valid torrent.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Torrent file not found: {path}")

    with open(path, "rb") as f:
        raw_bytes = f.read()

    return parse_torrent_bytes(raw_bytes, fallback_encodings=fallback_encodings)
