"""Repro: /api/update/install must never 500 — every failure is a 4xx with detail."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, "api-printer-service")

from fastapi.testclient import TestClient

import updater


class FakeResp:
    status = 200

    def read(self, n=-1):
        return b"" if n else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestInstallNever500(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        fd, self.settings = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.u = updater.Updater("1.1.26", self.settings, "windows")
        self.u._publish({
            "status": "available",
            "available_version": "1.1.28",
            "release_url": "https://github.com/manconsultingltd/"
                           "pos-api-printer-service/releases/tag/v1.1.28",
            "asset_name": "api-printer-setup-windows.exe",
        })

    def tearDown(self):
        os.unlink(self.settings)

    def test_popen_oserror_is_not_500(self):
        with patch.object(self.main, "get_updater", return_value=self.u), \
             patch.object(updater.urllib.request, "urlopen", return_value=FakeResp()), \
             patch.object(updater.subprocess, "Popen",
                          side_effect=OSError(22, "Invalid argument")):
            client = TestClient(self.main.app, raise_server_exceptions=False)
            r = client.post("/api/update/install")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("detail", r.json())

    def test_download_error_is_400_with_detail(self):
        with patch.object(self.main, "get_updater", return_value=self.u), \
             patch.object(updater.urllib.request, "urlopen",
                          side_effect=Exception("connection reset")):
            client = TestClient(self.main.app, raise_server_exceptions=False)
            r = client.post("/api/update/install")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("connection reset", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
