"""Usage ledger: every model call, what it was for, how long it took and what it cost where the backend says.

One line per call in ~/.local/share/kipipe/usage.jsonl. Costs are only what a backend reports itself (Claude Code
reports an API-equivalent figure; a subscription is not billed that way). Nothing is estimated from price tables.
"""
from __future__ import annotations

import functools
import json
import time
from pathlib import Path

from . import config
from .core import HOME

FILE = HOME / "usage.jsonl"


class BudgetReached(RuntimeError):
    pass


def record(backend, model, role, seconds, cost=None, tokens_in=None, tokens_out=None, job=None, ok=True):
    row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "backend": backend, "model": str(model or ""), "role": role or "",
           "seconds": round(seconds, 1), "ok": ok}
    for k, v in (("cost", cost), ("in", tokens_in), ("out", tokens_out), ("job", job)):
        if v is not None:
            row[k] = v
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def rows(since: str | None = None, job: str | None = None) -> list:
    out = []
    try:
        for line in FILE.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (since is None or r.get("t", "") >= since) and (job is None or r.get("job") == job):
                out.append(r)
    except OSError:
        pass
    return out


def summarise(rs: list) -> dict:
    def add(d, key, r):
        x = d.setdefault(key, {"calls": 0, "seconds": 0.0, "cost": 0.0, "in": 0, "out": 0})
        x["calls"] += 1
        x["seconds"] = round(x["seconds"] + r.get("seconds", 0), 1)
        x["cost"] = round(x["cost"] + (r.get("cost") or 0), 4)
        x["in"] += r.get("in") or 0
        x["out"] += r.get("out") or 0
    by_role, by_model, total = {}, {}, {}
    for r in rs:
        add(by_role, r.get("role") or "(other)", r)
        add(by_model, f"{r.get('model')} ({r.get('backend')})", r)
        add(total, "all", r)
    return {"total": total.get("all", {"calls": 0, "seconds": 0, "cost": 0, "in": 0, "out": 0}), "by_role": by_role, "by_model": by_model,
            "cost_known_for": sum(1 for r in rs if r.get("cost") is not None)}


def summary(days: int = 30, job: str | None = None) -> dict:
    since = None if job else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    s = summarise(rows(since, job))
    s["days"] = days
    s["budget"] = {"usd_month": config.get("budget.usd_month", 0) or 0, "calls_day": config.get("budget.calls_day", 0) or 0}
    return s


def check_budget():
    """Raise BudgetReached when a limit the user set is used up. Local models are never counted."""
    cap = float(config.get("budget.usd_month", 0) or 0)
    calls = float(config.get("budget.calls_day", 0) or 0)
    if cap:
        spent = sum(r.get("cost") or 0 for r in rows(time.strftime("%Y-%m-01T00:00:00")))
        if spent >= cap:
            raise BudgetReached(f"monthly budget reached: ${spent:.2f} of ${cap:.2f} (kipipe config set budget.usd_month)")
    if calls:
        n = sum(1 for r in rows(time.strftime("%Y-%m-%dT00:00:00")) if r.get("backend") != "ollama")
        if n >= calls:
            raise BudgetReached(f"daily call limit reached: {n} of {calls:g} (kipipe config set budget.calls_day)")


def instrument(backends: dict):
    """Wrap each backend's ask() so calls are timed, recorded and held to the budget. Called once from llm."""
    for kind, cls in backends.items():
        if getattr(cls.ask, "_kipipe_usage", False):
            continue
        orig = cls.ask

        def make(orig, kind):
            @functools.wraps(orig)
            def ask(self, *a, **kw):
                if getattr(self, "_in_ask", False):          # a retry inside ask(): count the outer call only
                    return orig(self, *a, **kw)
                if kind != "ollama":
                    check_budget()
                self._in_ask, self.last_usage = True, {}
                t0, ok = time.time(), False
                try:
                    out = orig(self, *a, **kw)
                    ok = True
                    return out
                finally:
                    self._in_ask = False
                    u = getattr(self, "last_usage", None) or {}
                    wd = getattr(self, "dir", None) or getattr(self, "workdir", None)
                    record(kind, getattr(self, "model", ""), getattr(self, "role", ""), time.time() - t0, u.get("cost"), u.get("in"), u.get("out"),
                           Path(wd).name if wd else None, ok)
            ask._kipipe_usage = True
            return ask
        cls.ask = make(orig, kind)
