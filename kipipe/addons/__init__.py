"""Add-ons: everything that works on a design without being part of the core text -> board path.

Three kinds, one registry:

  check     reads a job and returns findings (error / warning / note). Checks marked auto run in the
            pipeline's "Design checks" stage, cost nothing, and never stop a build.
  generate  makes files from a finished job (BOM, enclosure, firmware pin map, ...).
  aid       helps before or around the design (blocks, brief check, questions about a board).

Each add-on states how finished it is: ready (tested end to end), beta (works, rough edges or heuristics),
preview (framework and first cut; output is a starting point). The UI shows that label next to the name.

Your own add-ons: drop a .py file into ~/.local/share/kipipe/addons/ with

    def register(api):
        @api.addon(id="my.check", title="My check", kind="check", summary="what it looks at")
        def run(r):
            for ref, c in r.job.comps.items():
                if ...:
                    r.finding("warning", "short title", "what and why", refs=[ref])

`r` is a Run (see core.py): r.job is the design (spec, nets, layout), r.finding() reports, r.write() saves files.
"""
from __future__ import annotations

from .core import (Addon, Finding, Job, Run, REG, PLUGIN_ERRORS, addon, available, catalog, emit, job_state,  # noqa: F401
                   load_plugins, on, run, run_checks, EVENTS, PLUGIN_DIR)

_loaded = False


def load():
    """Import the built-in add-ons and the user's plugins (once)."""
    global _loaded
    if _loaded:
        return REG
    _loaded = True
    from . import checks, gen_docs, gen_build, gen_mech, aids, notify, projects, guide, partchecks  # noqa: F401
    load_plugins()
    return REG
