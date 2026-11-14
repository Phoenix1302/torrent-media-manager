"""Tests for version and config definitions."""

import sys
import unittest
from pathlib import Path

# Ensure app directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


class TestConfigVersion(unittest.TestCase):
    def test_app_version_is_1_1_0(self):
        self.assertTrue(hasattr(config, "APP_VERSION"))
        self.assertEqual(config.APP_VERSION, "1.1.0")

    def test_main_health_and_app_version(self):
        # Read main.py directly to verify FastAPI title/version and /api/health return value
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        content = main_py.read_text(encoding="utf-8")

        self.assertIn('FastAPI(title="Torrent Media Manager", version="1.1.0")', content)
        self.assertIn('"version": "1.1.0"', content)
        self.assertIn('"status": "ok"', content)
        self.assertIn('"app": "torrent-media-manager"', content)


if __name__ == "__main__":
    unittest.main()
