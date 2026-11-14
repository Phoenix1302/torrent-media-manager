"""Unit test for is_torrent_complete in watcher."""

import unittest
from pathlib import Path
import sys

# Ensure app directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import is_torrent_complete


class TestWatcherCompletion(unittest.TestCase):
    def test_stalleddl_at_100_percent_is_complete(self):
        # As observed in real qBittorrent: 100% downloaded torrent in stalledDL state
        torrent = {
            "name": "Ghost in the Shell [10 из 10]",
            "progress": 100.0,
            "progress_raw": 1.0,
            "state": "stalledDL",
            "size": 18576000000,
        }
        self.assertTrue(is_torrent_complete(torrent))

    def test_uploading_states_are_complete(self):
        for state in ["uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP"]:
            torrent = {
                "name": "Movie.mkv",
                "progress": 100.0,
                "progress_raw": 1.0,
                "state": state,
            }
            self.assertTrue(is_torrent_complete(torrent))

    def test_downloading_under_100_percent_is_not_complete(self):
        torrent = {
            "name": "Parasyte The Maxim",
            "progress": 65.2,
            "progress_raw": 0.652,
            "state": "downloading",
        }
        self.assertFalse(is_torrent_complete(torrent))

    def test_checking_dl_is_not_complete_even_at_100(self):
        torrent = {
            "name": "Checking torrent",
            "progress": 100.0,
            "progress_raw": 1.0,
            "state": "checkingDL",
        }
        self.assertFalse(is_torrent_complete(torrent))


if __name__ == "__main__":
    unittest.main()
