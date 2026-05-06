"""Pytest bootstrap — pin env vars BEFORE any app module is imported.

The ``app.config.Settings`` singleton is built at import time, so the
first test module to do ``from app.main import app`` wins all subsequent
DB / admin choices. To avoid each test module fighting that singleton
(and causing 401s when admin creds drift between modules) we set a
single shared bootstrap here.
"""
from __future__ import annotations

import os
from pathlib import Path

# Single shared test DB. Individual tests are responsible for nuking
# their own tables in autouse fixtures.
_DB = Path("/tmp/reckora-test.db")
if _DB.exists():
    _DB.unlink()
os.environ["RECKORA_DB_PATH"] = str(_DB)
os.environ["RECKORA_ADMIN_USERNAME"] = "rootadmin"
os.environ["RECKORA_ADMIN_PASSWORD"] = "rootpass1"
os.environ.setdefault("RECKORA_LOGIN_RATE_LIMIT", "1000/minute")
os.environ.setdefault("RECKORA_SEARCH_RATE_LIMIT", "1000/minute")
