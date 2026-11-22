"""Tell the user when a run finishes: desktop notification, a Discord-style webhook, or an ntfy topic.

Off until configured:  kipipe config set notify.desktop true
                       kipipe config set notify.webhook https://discord.com/api/webhooks/...
                       kipipe config set notify.ntfy my-topic
Only the project name, the outcome and a one-line summary are sent. No files, no prompt text.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request

from . import config
from .core import Job, addon, on


def message(job: Job, result: dict | None = None) -> tuple:
    res = result or job.result or {}
    ok = res.get("ok")
    m = res.get("metrics") or {}
    b, rt, d = m.get("board") or {}, m.get("routing") or {}, m.get("drc") or {}
    bits = []
    if b.get("width"):
        bits.append(f"{float(b['width']):.0f} x {float(b['height']):.0f} mm")
    if rt:
        bits.append(f"{rt.get('unrouted', '?')} unrouted")
    if d:
        bits.append(f"{d.get('errors', '?')} DRC errors")
    if res.get("seconds"):
        bits.append(f"{float(res['seconds']):.0f} s")
    title = f"kipipe: {job.name} {'finished' if ok else 'failed' if ok is False else 'has no result yet'}"
    body = ", ".join(bits) if ok or not res.get("error") else str(res.get("error"))[:200]
    return title, body


def send(title: str, body: str) -> list:
    """Send through every configured channel. Returns [(channel, ok, detail)]."""
    out = []
    if config.get("notify.desktop"):
        exe = shutil.which("notify-send")
        if exe:
            p = subprocess.run([exe, "-a", "kipipe", title, body], capture_output=True, text=True)
            out.append(("desktop", p.returncode == 0, p.stderr.strip()[:120]))
        else:
            out.append(("desktop", False, "notify-send not found (sudo apt install libnotify-bin)"))
    hook = config.get("notify.webhook")
    if hook:
        out.append(("webhook",) + _post(str(hook), json.dumps({"content": f"**{title}**\n{body}"}).encode(), {"Content-Type": "application/json"}))
    topic = config.get("notify.ntfy")
    if topic:
        url = str(topic) if str(topic).startswith("http") else f"https://ntfy.sh/{topic}"
        out.append(("ntfy",) + _post(url, body.encode(), {"Title": title.encode("ascii", "replace").decode()}))
    return out


def _post(url, data, headers) -> tuple:
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "kipipe", **headers}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return (200 <= r.status < 300, f"HTTP {r.status}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}"[:160])


def _job_done(job=None, result=None, log=None, **_):
    if job is None or not (config.get("notify.desktop") or config.get("notify.webhook") or config.get("notify.ntfy")):
        return
    for ch, ok, detail in send(*message(job, result)):
        if log and not ok:
            log(f"  notification via {ch} failed: {detail}")


on("job_done", _job_done)


@addon(id="aid.notify", title="Notify when runs finish", kind="aid", group="Design aids", status="preview",
       summary="Desktop pop-up, Discord webhook or phone push (ntfy) when a run finishes. This button sends a test message through whatever is configured.")
def notify(r):
    sent = send(*message(r.job))
    if not sent:
        r.summary = "nothing configured yet"
        r.finding("note", "No channel is set up", "In a terminal: kipipe config set notify.desktop true   or   kipipe config set notify.webhook <Discord webhook URL>   "
                  "or   kipipe config set notify.ntfy <topic>. After that every finished run sends a message.")
        return
    for ch, ok, detail in sent:
        r.finding("note" if ok else "warning", f"{ch}: {'sent' if ok else 'failed'}", detail)
    r.summary = ", ".join(f"{ch} {'ok' if ok else 'failed'}" for ch, ok, _ in sent)
