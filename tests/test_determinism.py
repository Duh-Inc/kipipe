"""Same input, same output: the design cache, the schematic writer and the whole pipeline."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


class _Fake:
    def __init__(self, replies):
        self.replies, self.model, self.asked = list(replies), "fake", 0

    def ask(self, prompt, system=None, **kw):
        self.asked += 1
        return self.replies.pop(0)


def test_same_request_reuses_the_design(tmp_path, monkeypatch):
    from kipipe import llm
    from kipipe.pipeline import Options
    good = json.loads((FIX / "specs" / "stack_base.json").read_text())
    fake = _Fake([json.dumps(good)])

    class Pool:
        def __init__(self, *a):
            pass

        def get(self, role):
            return fake

        def fallback(self, role):
            return fake
    monkeypatch.setattr(llm, "Pool", Pool)
    monkeypatch.setattr(llm, "SPEC_CACHE", tmp_path / "cache")
    o = Options(prompt="a stack base board")
    (tmp_path / "job1").mkdir()
    (tmp_path / "job2").mkdir()
    a = llm.generate_spec(o, tmp_path / "job1", lambda m: None)
    b = llm.generate_spec(o, tmp_path / "job2", lambda m: None)       # the fake has no replies left: must hit the cache
    assert fake.asked == 1 and a == b
    o.fresh = True
    fake.replies.append(json.dumps(good))
    (tmp_path / "job3").mkdir()
    llm.generate_spec(o, tmp_path / "job3", lambda m: None)
    assert fake.asked == 2


def test_schematic_is_byte_identical():
    from kipipe import schwire, spec as sm
    s = sm.normalize(json.loads((FIX / "specs" / "datasheet_good.json").read_text()))
    assert schwire.generate(s, "t")[0] == schwire.generate(s, "t")[0]


def _jar():
    import os
    if os.environ.get("KIPIPE_FREEROUTING_JAR"):
        return os.environ["KIPIPE_FREEROUTING_JAR"]
    jars = sorted((Path.home() / ".local/share/kipipe/freerouting").glob("*.jar"))
    return str(jars[-1]) if jars else None


@pytest.mark.skipif(not shutil.which("java") or not _jar(), reason="needs Java and the Freerouting jar")
def test_same_spec_gives_the_same_board(tmp_path, monkeypatch):
    """The MagicON test: run one spec twice, compare the board files byte for byte."""
    monkeypatch.setenv("KIPIPE_FREEROUTING_JAR", _jar())
    spec = FIX / "specs" / "stack_base.json"
    outs = []
    for i in range(2):
        r = subprocess.run([sys.executable, "-m", "kipipe.cli", "run", "--spec", str(spec), "--flags", "ipc,pour",
                            "--out", str(tmp_path / f"run{i}")], capture_output=True, text=True, timeout=900,
                           cwd=Path(__file__).parent.parent)
        assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
        outs.append(next((tmp_path / f"run{i}").rglob("*.kicad_pcb")))
    assert outs[0].read_bytes() == outs[1].read_bytes()
    sch = [p.with_suffix(".kicad_sch") for p in outs]
    assert sch[0].read_bytes() == sch[1].read_bytes()
