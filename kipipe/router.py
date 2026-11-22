"""Freerouting management: pick a release that runs on the installed Java,
download it once, run it headless."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("KIPIPE_HOME", Path.home() / ".local/share/kipipe"))
# newest release per minimum Java version (2.2+ is compiled for Java 25)
RELEASES = [(25, "2.5.0"), (21, "2.1.0")]
URL = "https://github.com/freerouting/freerouting/releases/download/v{v}/freerouting-{v}.jar"


def java_version() -> int:
    java = shutil.which("java")
    if not java:
        return 0
    out = subprocess.run([java, "-version"], capture_output=True, text=True)
    m = re.search(r'version "(\d+)', out.stderr + out.stdout)
    return int(m.group(1)) if m else 0


def pick_release(jv: int | None = None) -> str | None:
    jv = java_version() if jv is None else jv
    for need, ver in RELEASES:
        if jv >= need:
            return ver
    return None


def jar_path(log=print) -> Path:
    override = os.environ.get("KIPIPE_FREEROUTING_JAR")
    if override:
        return Path(override)
    jv = java_version()
    ver = pick_release(jv)
    if ver is None:
        raise RuntimeError(f"Java 21+ is required for Freerouting (found {jv or 'none'}). "
                           "Install it: sudo apt install openjdk-21-jre")
    p = DATA / "freerouting" / f"freerouting-{ver}.jar"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        log(f"downloading Freerouting {ver} (Java {jv})")
        tmp = p.with_suffix(".part")
        with urllib.request.urlopen(URL.format(v=ver), timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.rename(p)
    return p


ROUTE_CACHE = Path(os.environ.get("KIPIPE_CACHE", Path.home() / ".cache/kipipe")) / "routes"


def _route_key(dsn: Path, passes: int, jar: Path) -> str:
    """Freerouting gives a different result on each run of the same input (measured: three runs of one board, three
    different session files, even single-threaded). So identical inputs reuse the first result instead."""
    import hashlib
    body = dsn.read_text().split("\n", 1)[-1]          # first line holds the job folder path
    return hashlib.sha256(f"{jar.name}:{passes}\n{body}".encode()).hexdigest()[:24]


def route(dsn: Path, ses: Path, passes: int = 30, threads: int = 1, timeout: int = 900, log=print,
          use_cache: bool = True) -> None:
    jar = jar_path(log)
    key = _route_key(dsn, passes, jar)
    hit = ROUTE_CACHE / f"{key}.ses"
    if use_cache and hit.exists():
        shutil.copy(hit, ses)
        log(f"  reusing the routing of an identical earlier board (cache {key[:8]}); --fresh routes it again")
        return
    _route(dsn, ses, passes, threads, timeout, log, jar)
    if ses.exists() and ses.stat().st_size:
        try:
            ROUTE_CACHE.mkdir(parents=True, exist_ok=True)
            shutil.copy(ses, hit)
        except OSError:
            pass


def _route(dsn: Path, ses: Path, passes: int, threads: int, timeout: int, log, jar: Path) -> None:
    # Freerouting 2.x: max_passes is not honored by 2.1.0, job_timeout is, and the
    # session is still written when time runs out. Our watchdog is the backstop.
    hh, rem = divmod(max(10, timeout), 3600)
    mm, ss = divmod(rem, 60)
    cmd = ["java", "-jar", str(jar), "-de", str(dsn), "-do", str(ses), "-mp", str(passes),
           f"--router.max_passes={passes}", f"--router.job_timeout={hh:02d}:{mm:02d}:{ss:02d}",
           "-da", "--gui.enabled=false"]
    if threads:
        cmd += ["-mt", str(threads)]
    log(" ".join(cmd[2:]))
    from .kicad_lib import clean_env
    env = clean_env()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=dsn.parent)
    timer = threading.Timer(timeout + 90, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "JAVA_TOOL_OPTIONS" in line:
                continue
            m = re.search(r"pass #(\d+).*?score of ([\d.]+)(?: \((\d+) unrouted)?", line)
            if m:
                n = int(m.group(1))
                if n <= 3 or n % 25 == 0:
                    log(f"  pass {n}: score {m.group(2)}" + (f", {m.group(3)} unrouted" if m.group(3) else ", all routed"))
            elif re.search(r"(optimiz|ERROR|timeout)", line, re.I) and "currentLocale" not in line:
                log("  " + re.sub(r"^\S+ \S+ ", "", line)[:160])
        proc.wait()
    finally:
        timed_out = not timer.is_alive() and proc.returncode not in (0, None) and proc.returncode < 0
        timer.cancel()
    if timed_out:
        raise RuntimeError(f"Freerouting timed out after {timeout}s")
    if not ses.exists():
        raise RuntimeError("Freerouting produced no session file")
