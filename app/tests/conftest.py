"""Test setup: import the package from app/, and keep the store out of /data."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# inferenceinquire.api opens its store at import time; keep that out of /data under test.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "import.db"))
