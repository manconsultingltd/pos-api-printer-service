import os
import sys
import tempfile
from pathlib import Path

# The service modules are top-level (`from config import Config`), so the
# service directory has to be importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing main configures logging/settings from Config at module level;
# point both at the temp dir so tests never need /var/log or /etc access.
os.environ.setdefault(
    "LOG_FILE", os.path.join(tempfile.gettempdir(), "api-printer-test.log")
)
os.environ.setdefault(
    "API_PRINTER_SETTINGS_FILE",
    os.path.join(tempfile.gettempdir(), "api-printer-test-settings.json"),
)
