"""Tests for pure-Python bencode decoder, encoder, and torrent parser."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import sys
# Ensure app directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from torrent_parser import (
    BencodeDecodeError,
    BencodeEncodeError,
    TorrentParseError,
    bdecode,
    bencode,
    decode_text,
    parse_torrent_bytes,
    parse_torrent_file,
)


class TestBencode(unittest.TestCase):
    def test_decode_integers(self):
        self.assertEqual(bdecode(b"i0e"), 0)
        self.assertEqual(bdecode(b"i42e"), 42)
        self.assertEqual(bdecode(b"i-42e"), -42)
        self.assertEqual(bdecode(b"i1234567890123456789e"), 1234567890123456789)

        # Invalid integers
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"ie")
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"i-0e")
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"i03e")
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"i-03e")
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"i123")  # unterminated

    def test_decode_strings(self):
        self.assertEqual(bdecode(b"0:"), b"")
        self.assertEqual(bdecode(b"4:spam"), b"spam")
        self.assertEqual(bdecode(b"10:1234567890"), b"1234567890")

        # Invalid strings
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"4:spa")  # truncated
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"spam")  # missing colon
        with self.assertRaises(BencodeDecodeError):
            bdecode(b"-4:spam")  # negative length

    def test_decode_lists(self):
        self.assertEqual(bdecode(b"le"), [])
        self.assertEqual(bdecode(b"l4:spami42ee"), [b"spam", 42])
        self.assertEqual(bdecode(b"lli1eeli2eee"), [[1], [2]])
        self.assertEqual(bdecode(b"li1eli2eee"), [1, [2]])

        with self.assertRaises(BencodeDecodeError):
            bdecode(b"l4:spam")  # unterminated

    def test_decode_dicts(self):
        self.assertEqual(bdecode(b"de"), {})
        self.assertEqual(bdecode(b"d3:bar4:spam3:fooi42ee"), {b"bar": b"spam", b"foo": 42})
        self.assertEqual(bdecode(b"d1:ad1:bi1eee"), {b"a": {b"b": 1}})

        with self.assertRaises(BencodeDecodeError):
            bdecode(b"d3:bare")  # missing value

    def test_encode_and_roundtrip(self):
        cases = [
            0,
            123,
            -456,
            b"hello world",
            "utf-8 string \u2603",
            [1, b"two", 3],
            {"z": 1, "a": 2, "m": [b"test", 4]},
        ]
        for item in cases:
            encoded = bencode(item)
            self.assertIsInstance(encoded, bytes)

        # Exact round-trip test on canonical bencode
        data = {b"announce": b"http://tracker.org", b"info": {b"length": 1024, b"name": b"test.mkv"}}
        encoded = bencode(data)
        decoded = bdecode(encoded)
        self.assertEqual(decoded, data)

    def test_encode_canonical_key_ordering(self):
        # Keys must be ordered lexicographically by raw bytes
        d = {"z": 1, "b": 2, "a": 3, "aa": 4}
        encoded = bencode(d)
        # Expected order: 'a', 'aa', 'b', 'z'
        self.assertEqual(encoded, b"d1:ai3e2:aai4e1:bi2e1:zi1ee")

    def test_invalid_types_raise_encode_error(self):
        with self.assertRaises(BencodeEncodeError):
            bencode(object())


class TestTorrentParser(unittest.TestCase):
    def test_single_file_torrent(self):
        info_dict = {
            b"name": b"sample_movie.mkv",
            b"length": 104857600,  # 100 MB
            b"piece length": 262144,
            b"pieces": b"A" * 20,
        }
        torrent_dict = {
            b"announce": b"http://example.com/announce",
            b"created by": b"TorrentManager 1.0",
            b"creation date": 1700000000,
            b"comment": b"Test comment",
            b"info": info_dict,
        }
        raw_bytes = bencode(torrent_dict)
        parsed = parse_torrent_bytes(raw_bytes)

        expected_hash = hashlib.sha1(bencode(info_dict)).hexdigest().lower()

        self.assertEqual(parsed["info_hash"], expected_hash)
        self.assertEqual(len(parsed["info_hash"]), 40)
        self.assertEqual(parsed["name"], "sample_movie.mkv")
        self.assertEqual(parsed["total_size"], 104857600)
        self.assertEqual(parsed["piece_length"], 262144)
        self.assertFalse(parsed["is_multi_file"])
        self.assertEqual(parsed["created_by"], "TorrentManager 1.0")
        self.assertEqual(parsed["comment"], "Test comment")
        self.assertEqual(parsed["creation_date"], 1700000000)
        self.assertEqual(len(parsed["files"]), 1)
        self.assertEqual(parsed["files"][0], {
            "index": 0,
            "path": "sample_movie.mkv",
            "size": 104857600,
        })

    def test_multi_file_torrent(self):
        files_list = [
            {b"length": 1000, b"path": [b"Season 01", b"Episode 01.mkv"]},
            {b"length": 2500, b"path": [b"Season 01", b"Episode 02.mkv"]},
            {b"length": 300, b"path": [b"Subs", b"en.srt"]},
        ]
        info_dict = {
            b"name": b"Show Series",
            b"files": files_list,
            b"piece length": 65536,
            b"pieces": b"B" * 20,
        }
        torrent_dict = {
            b"info": info_dict,
        }
        raw_bytes = bencode(torrent_dict)
        parsed = parse_torrent_bytes(raw_bytes)

        self.assertTrue(parsed["is_multi_file"])
        self.assertEqual(parsed["name"], "Show Series")
        self.assertEqual(parsed["total_size"], 1000 + 2500 + 300)
        self.assertEqual(parsed["piece_length"], 65536)
        self.assertIsNone(parsed["creation_date"])
        self.assertEqual(parsed["created_by"], "")
        self.assertEqual(parsed["comment"], "")

        self.assertEqual(len(parsed["files"]), 3)
        self.assertEqual(parsed["files"][0], {
            "index": 0,
            "path": "Season 01/Episode 01.mkv",
            "size": 1000,
        })
        self.assertEqual(parsed["files"][1], {
            "index": 1,
            "path": "Season 01/Episode 02.mkv",
            "size": 2500,
        })
        self.assertEqual(parsed["files"][2], {
            "index": 2,
            "path": "Subs/en.srt",
            "size": 300,
        })

    def test_bep0047_utf8_override(self):
        # name.utf-8 and path.utf-8 should override standard name/path
        files_list = [
            {
                b"length": 500,
                b"path": [b"old_ascii_dir", b"old_file.mkv"],
                b"path.utf-8": ["Папка".encode("utf-8"), "Серия 01.mkv".encode("utf-8")],
            }
        ]
        info_dict = {
            b"name": b"old_ascii_name",
            b"name.utf-8": "Новый Сериал".encode("utf-8"),
            b"files": files_list,
            b"piece length": 16384,
            b"pieces": b"C" * 20,
        }
        torrent_dict = {
            b"info": info_dict,
            b"comment": b"old comment",
            b"comment.utf-8": "Русский комментарий".encode("utf-8"),
        }
        parsed = parse_torrent_bytes(bencode(torrent_dict))

        self.assertEqual(parsed["name"], "Новый Сериал")
        self.assertEqual(parsed["comment"], "Русский комментарий")
        self.assertEqual(len(parsed["files"]), 1)
        self.assertEqual(parsed["files"][0]["path"], "Папка/Серия 01.mkv")
        self.assertEqual(parsed["files"][0]["size"], 500)

    def test_cp1251_fallback_encoding(self):
        # Russian trackers (Rutracker/NNMClub) historically use cp1251
        cyrillic_name = "Интерстеллар (2014) BDRip.mkv"
        name_cp1251 = cyrillic_name.encode("cp1251")

        info_dict = {
            b"name": name_cp1251,
            b"length": 5000000000,
            b"piece length": 1048576,
            b"pieces": b"D" * 20,
        }
        torrent_dict = {
            b"info": info_dict,
            b"created by": "Создатель торрента".encode("cp1251"),
        }
        raw_bytes = bencode(torrent_dict)
        parsed = parse_torrent_bytes(raw_bytes)

        self.assertEqual(parsed["name"], cyrillic_name)
        self.assertEqual(parsed["created_by"], "Создатель торрента")

    def test_parse_torrent_file(self):
        info_dict = {
            b"name": b"test_file.txt",
            b"length": 42,
            b"piece length": 16384,
            b"pieces": b"E" * 20,
        }
        torrent_dict = {
            b"info": info_dict,
        }
        raw_bytes = bencode(torrent_dict)

        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name

        try:
            parsed = parse_torrent_file(tmp_path)
            self.assertEqual(parsed["name"], "test_file.txt")
            self.assertEqual(parsed["total_size"], 42)
            self.assertEqual(len(parsed["files"]), 1)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_single_file_bep0047_utf8_override(self):
        # BEP 0047 name.utf-8 on single-file torrent
        info_dict = {
            b"name": b"legacy_name.mkv",
            b"name.utf-8": "Титаник.1997.mkv".encode("utf-8"),
            b"length": 1500000000,
            b"piece length": 262144,
            b"pieces": b"F" * 20,
        }
        torrent_dict = {b"info": info_dict}
        parsed = parse_torrent_bytes(bencode(torrent_dict))
        self.assertFalse(parsed["is_multi_file"])
        self.assertEqual(parsed["name"], "Титаник.1997.mkv")
        self.assertEqual(parsed["files"][0]["path"], "Титаник.1997.mkv")
        self.assertEqual(parsed["files"][0]["size"], 1500000000)

    def test_latin1_fallback_encoding(self):
        # Characters like 'é' in Latin-1 (0xe9)
        name_latin1 = b"Film_Fran\xe7ais_HD.mkv"
        info_dict = {
            b"name": name_latin1,
            b"length": 123456,
            b"piece length": 16384,
            b"pieces": b"G" * 20,
        }
        torrent_dict = {b"info": info_dict}
        parsed = parse_torrent_bytes(bencode(torrent_dict))
        self.assertIn("Fran", parsed["name"])

    def test_encoding_hint(self):
        cyrillic_name = "Брат 2.mkv"
        name_cp1251 = cyrillic_name.encode("cp1251")
        info_dict = {
            b"name": name_cp1251,
            b"length": 700000000,
            b"piece length": 524288,
            b"pieces": b"H" * 20,
        }
        torrent_dict = {
            b"encoding": b"windows-1251",
            b"info": info_dict,
        }
        parsed = parse_torrent_bytes(bencode(torrent_dict))
        self.assertEqual(parsed["name"], cyrillic_name)

    def test_strict_vs_non_strict_bdecode(self):
        valid = b"i42e"
        with_trailing = b"i42etrailing_garbage"
        self.assertEqual(bdecode(valid, strict=True), 42)
        self.assertEqual(bdecode(with_trailing, strict=False), 42)
        with self.assertRaises(BencodeDecodeError):
            bdecode(with_trailing, strict=True)

    def test_boolean_and_tuple_bencode(self):
        self.assertEqual(bencode(True), b"i1e")
        self.assertEqual(bencode(False), b"i0e")
        self.assertEqual(bencode((1, 2, 3)), b"li1ei2ei3ee")

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            parse_torrent_file("non_existent_file.torrent")

    def test_invalid_torrent_bytes(self):
        with self.assertRaises(TorrentParseError):
            parse_torrent_bytes(b"")
        with self.assertRaises(TorrentParseError):
            parse_torrent_bytes(b"i42e")  # integer instead of dict
        with self.assertRaises(TorrentParseError):
            parse_torrent_bytes(b"d3:bar4:spame")  # missing 'info'
        with self.assertRaises(BencodeDecodeError):
            parse_torrent_bytes(b"corrupted bencode data")


if __name__ == "__main__":
    unittest.main()
