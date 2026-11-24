"""Tests run against a throwaway data folder so they never touch the user's parts, projects or settings.
KIPIPE_HOME must be set before kipipe is imported, which is why it happens at module import here."""
import os
import shutil
import tempfile
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
HOME = Path(tempfile.mkdtemp(prefix="kipipe-test-"))
shutil.copytree(FIX / "partdb", HOME / "partdb")
os.environ["KIPIPE_HOME"] = str(HOME)
os.environ["KIPIPE_CACHE"] = str(HOME / "cache")

import pytest  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(HOME, ignore_errors=True)


@pytest.fixture(scope="session")
def k10_lib():
    """A hand-made symbol with a KiCad 10 stacked pin number, imported into the test user library."""
    from kipipe import libs
    libs.import_archive(str(FIX / "libs" / "k10test.kicad_sym"), log=lambda m: None)
    return "kipipe_parts:K10MOD"


def spec(name):
    import json
    return json.loads((FIX / "specs" / name).read_text())
