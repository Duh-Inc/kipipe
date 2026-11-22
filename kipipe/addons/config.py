"""Small settings file for add-ons: ~/.local/share/kipipe/config.json. Dotted keys: get("notify.webhook")."""
from __future__ import annotations

import json

from .core import HOME

FILE = HOME / "config.json"
KNOWN = {
    "notify.desktop": "true/false: desktop notification when a run finishes",
    "notify.webhook": "Discord (or compatible) webhook URL to post to when a run finishes",
    "notify.ntfy": "ntfy.sh topic name to push to when a run finishes",
    "budget.usd_month": "stop model calls once this month's reported cost reaches this many dollars (0 = off)",
    "budget.calls_day": "stop model calls after this many in one day (0 = off)",
    "checks.skip": "comma-separated check ids to leave out of the automatic Design checks stage",
}


def load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return {}


def get(key: str, default=None):
    d = load()
    for part in key.split("."):
        if not isinstance(d, dict) or part not in d:
            return default
        d = d[part]
    return d


def set(key: str, value):       # noqa: A001
    d = load()
    cur = d
    parts = key.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    if value in (None, ""):
        cur.pop(parts[-1], None)
    else:
        if isinstance(value, str):
            low = value.lower()
            value = True if low == "true" else False if low == "false" else value
            try:
                value = float(value) if isinstance(value, str) and value.replace(".", "", 1).isdigit() else value
            except ValueError:
                pass
        cur[parts[-1]] = value
    HOME.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, indent=1))
    return d
