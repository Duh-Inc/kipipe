"""FEATURES.md must name everything that exists, so the list can't drift."""
import re
from pathlib import Path

DOC = (Path(__file__).parent.parent / "FEATURES.md").read_text()


def test_every_addon_is_listed():
    from kipipe import addons
    from kipipe.addons import core
    addons.load()
    missing = [a for a in core.REG if a not in DOC]
    assert not missing, f"add to FEATURES.md: {missing}"


def test_every_cli_command_is_listed():
    from kipipe import cli
    src = Path(cli.__file__).read_text()
    cmds = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', src))
    missing = [c for c in cmds if f"`{c}`" not in DOC]
    assert not missing, f"add to FEATURES.md: {missing}"


def test_every_pipeline_flag_is_listed():
    from kipipe import pipeline
    missing = [f for f in pipeline.ALL_FLAGS if f"`{f}`" not in DOC]
    assert not missing, f"add to FEATURES.md: {missing}"
