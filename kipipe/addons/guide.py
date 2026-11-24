"""Understanding a design: where each choice came from, what to look at first, a plain-language walkthrough,
and a comparison of approaches before anything is designed."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .core import HOME, Job, addon, run_checks
from .draw import md_table, natkey

LEVELS = ("guided", "standard", "expert")
SOURCES = {"block": "verified block", "datasheet": "datasheet record", "library": "your own library part",
           "variant": "datasheet of a close variant", "memory": "the model's memory"}


def provenance(j: Job) -> dict:
    """{ref: source} for every part that carries design risk (ICs, modules, transistors, crystals, connectors)."""
    have, variant = {}, {}
    f = j.dir / "out/parts.json"
    if f.exists() and not j.bare:
        try:
            d = json.loads(f.read_text())
            have, variant = d.get("have") or {}, d.get("variant") or {}
        except Exception:
            pass
    out = {}
    for ref, c in j.comps.items():
        k = j.kind(ref)
        if k not in ("U", "Q", "Y", "K") and not (k in ("D", "L") and c.get("mpn")):
            continue
        if c.get("block"):
            out[ref] = "block"
        elif ref in have:
            out[ref] = "datasheet"
        elif ref in variant:
            out[ref] = "variant"
        elif str(c.get("symbol", "")).startswith("kipipe_parts:"):
            out[ref] = "library"
        else:
            out[ref] = "memory"
    return out


@addon(id="check.provenance", title="Where each choice came from", kind="check", auto=True, group="Design checks",
       summary="For every IC, module and transistor: whether its wiring rests on a datasheet record, a verified block, your own library, or only the model's memory.")
def check_provenance(r):
    j = r.job
    prov = provenance(j)
    if not prov:
        r.summary = "no ICs or active parts on this board"
        return
    groups = {}
    for ref, src in prov.items():
        groups.setdefault(src, []).append(ref)
    mem = sorted(groups.get("memory", []), key=natkey)
    if mem:
        r.finding("warning" if len(mem) > len(prov) / 2 else "note",
                  f"{len(mem)} of {len(prov)} active part(s) rest on the model's memory: " + ", ".join(f"{x} ({j.comps[x].get('value', '')})" for x in mem),
                  "Pin numbers, support parts and limits for these were recalled, not read. That is where wrong pinouts come from.",
                  refs=mem, fix="Add their datasheets under Datasheets and revise, or check each pin against the datasheet by hand.")
    for src in ("variant", "datasheet", "block", "library"):
        if groups.get(src):
            refs = sorted(groups[src], key=natkey)
            r.finding("note", f"From {SOURCES[src]}: {', '.join(refs)}",
                      "A variant's record matches by pin names; package pin numbers can differ." if src == "variant" else "", refs=refs)
    r.data["provenance"] = prov
    r.summary = f"{len(prov) - len(mem)} of {len(prov)} active part(s) backed by a datasheet, block or library part"


# ---------------------------------------------------------------------------
def review_items(j: Job, checks: dict | None = None) -> list:
    """What to look at, most important first: [{level: must|should|glance, title, why, how, refs}]."""
    items = []
    res = j.result
    for s in res.get("stages") or []:
        if s["status"] == "fail":
            items.append({"level": "must", "title": f"The {s['name']} step failed", "why": s.get("detail", ""),
                          "how": "The board is not finished. Read the log for this step; Revise or rerun once the cause is clear.", "refs": []})
    chk = checks or run_checks(j)
    for sev, level in (("error", "must"), ("warning", "should")):
        for c in chk["checks"]:
            for f in c["findings"]:
                if f["severity"] == sev:
                    items.append({"level": level, "title": f["title"], "why": f["detail"], "how": f.get("fix") or "Look at these parts in the schematic and decide whether the rule applies here.",
                                  "refs": f.get("refs", []), "source": c["title"]})
    prov = provenance(j)
    for ref in sorted((x for x, s in prov.items() if s in ("memory", "variant")), key=natkey):
        c = j.comps[ref]
        pins = ", ".join(f"{n}={j.pin_name(ref, n) or '?'}→{net}" for n, net in sorted(j.nets_of(ref).items(), key=lambda x: natkey(x[0])))[:400]
        if not any(i["title"].startswith(ref + " pinout") for i in items):
            items.append({"level": "should", "title": f"{ref} pinout ({c.get('value', '')})",
                          "why": "This part was wired from " + SOURCES[prov[ref]] + ". A swapped pin here is the most common way a generated board fails.",
                          "how": f"Open its datasheet at the pin table for the package ({c.get('footprint', '').split(':')[-1]}) and compare: {pins}", "refs": [ref]})
    for a in j.spec.get("assumptions") or []:
        items.append({"level": "should", "title": "Confirm: " + str(a), "why": "The request did not say, so the model chose.", "how": "If it is wrong, say so in Revise.", "refs": []})
    conns = sorted(j.of_kind("J"), key=natkey)
    if conns:
        items.append({"level": "should", "title": "Connector pinouts against whatever plugs in",
                      "why": "Nothing in this tool knows the cable or the other board, unless they are in the same project.",
                      "how": "; ".join(f"{x}: " + ", ".join(f"{n}={net}" for n, net in sorted(j.nets_of(x).items(), key=lambda q: natkey(q[0]))) for x in conns)[:600], "refs": conns})
    drc = (res.get("metrics") or {}).get("drc") or {}
    if drc.get("warnings"):
        items.append({"level": "glance", "title": f"{drc['warnings']} DRC warning(s)", "why": "Usually silkscreen over pads or text near the edge; harmless, but worth one look.",
                      "how": "Open the board in KiCad and run Inspect > Design Rules Checker.", "refs": []})
    if any(s["name"] == "ERC" and s["status"] == "skip" for s in res.get("stages") or []):
        items.append({"level": "glance", "title": "The electrical rules check did not run", "why": "This KiCad version has no command-line ERC.", "how": "Open the schematic in KiCad and run Inspect > Electrical Rules Checker.", "refs": []})
    for c in chk["checks"]:
        for f in c["findings"]:
            if f["severity"] == "note" and c["id"] in ("check.leds", "check.handsolder", "check.mech"):
                items.append({"level": "glance", "title": f["title"], "why": f["detail"], "how": "", "refs": f.get("refs", []), "source": c["title"]})
    order = {"must": 0, "should": 1, "glance": 2}
    items.sort(key=lambda i: order[i["level"]])
    return items


@addon(id="gen.guide", title="Review guide", kind="generate", group="Understand",
       summary="What to check on this board, most important first, with the reason and how to check it. Built from the checks, the pipeline result and where each part's wiring came from.")
def gen_guide(r):
    j = r.job
    items = review_items(j)
    n = {k: sum(i["level"] == k for i in items) for k in ("must", "should", "glance")}
    md = [f"# Review guide: {j.name}", "", f"{n['must']} to fix, {n['should']} to check, {n['glance']} to glance at.", ""]
    for lvl, head in (("must", "Fix before building"), ("should", "Check before ordering"), ("glance", "Worth a glance")):
        rows = [i for i in items if i["level"] == lvl]
        if rows:
            md += [f"## {head}", ""]
            for i in rows:
                md += [f"- [ ] **{i['title']}**" + (f" ({', '.join(i['refs'][:6])})" if i["refs"] else ""), f"  {i['why']}" if i["why"] else "", f"  How: {i['how']}" if i["how"] else "", ""]
    r.write(f"{j.name}-review.md", "\n".join(x for x in md if x is not None), "Review guide")
    (j.dir / "out").mkdir(exist_ok=True)
    (j.dir / "out/guide.json").write_text(json.dumps({"items": items, "counts": n}, indent=1))
    r.data.update(items=items, counts=n)
    r.summary = f"{n['must']} to fix, {n['should']} to check, {n['glance']} to glance at"


# ---------------------------------------------------------------------------
EXPLAIN_SYSTEM = """You explain one finished PCB design to its owner. Everything you know about it is in the message.
Reply with one JSON object and nothing else:
{"summary": "two or three sentences: what the board does and how, start to finish",
 "sections": [{"title": "functional block, e.g. Power input", "parts": ["J1", "D1"],
               "what": "what this group of parts does", "why": "why it is built this way, with the numbers that matter",
               "check": "the one thing worth verifying here, or empty"}],
 "glossary": [{"term": "a term used above", "meaning": "one plain sentence"}]}
Rules: every part on the board belongs to exactly one section. Follow the signal or power path in order. Use only
facts in the data; where the designer's notes give a number, use it, and do not invent datasheet figures.
"""
EXPLAIN_DEPTH = {
    "guided": "The reader is new to electronics. Use plain words, say what each kind of part is for the first time it appears "
              "(what a regulator, a bypass capacitor, a pull-up does), and put every technical term you use in the glossary.",
    "standard": "The reader knows basic electronics. Be concise, focus on why each choice was made. Glossary only for unusual terms; it may be empty.",
    "expert": "The reader is an experienced engineer. One or two terse lines per section, design decisions and margins only. Leave the glossary empty.",
}


def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


@addon(id="aid.explain", title="Walk me through this board", kind="aid", group="Understand", status="beta", needs=("spec", "model"),
       options=[{"key": "level", "label": "Explain for", "type": "select", "choices": list(LEVELS), "default": ""}],
       summary="A plain-language tour of the board, block by block: what each group of parts does, why, and what to verify. Written by the small model from this board's own data.")
def explain(r):
    from .. import llm
    from ..pipeline import load_options
    from .aids import ask_context
    j = r.job
    o = r.opts.get("_options") or load_options(j.dir)
    level = r.opts.get("level") if r.opts.get("level") in LEVELS else (getattr(o, "level", None) or "standard")
    be = llm.Pool(o, j.dir, r.log).get("ask")
    if hasattr(be, "session"):
        be.session = None
    kw = {"tools": []} if isinstance(be, llm.ClaudeCode) else {}
    out = be.ask(ask_context(j), EXPLAIN_SYSTEM + EXPLAIN_DEPTH[level], **kw)
    try:
        d = _json_from(out)
    except ValueError:
        d = {}
    if not d.get("sections"):
        raise RuntimeError("the model did not return a usable walkthrough")
    known = set(j.comps)
    placed = set()
    for s in d["sections"]:
        s["parts"] = [p for p in s.get("parts") or [] if p in known]
        placed |= set(s["parts"])
    left = sorted(known - placed, key=natkey)
    if left:
        r.finding("note", f"Not covered by the walkthrough: {', '.join(left)}", "The model left these parts out of every section.", refs=left)
    d.update(level=level, model=llm._who(be))
    r.write("walkthrough.json", json.dumps(d, indent=1, ensure_ascii=False), "Walkthrough (data)")
    md = [f"# {j.name}: walkthrough", "", d.get("summary", ""), ""]
    for s in d["sections"]:
        md += [f"## {s.get('title', '')}", "", f"Parts: {', '.join(s['parts'])}", "", s.get("what", ""), "", s.get("why", ""), "", ("Check: " + s["check"]) if s.get("check") else "", ""]
    if d.get("glossary"):
        md += ["## Terms", "", md_table(["Term", "Meaning"], [(g.get("term", ""), g.get("meaning", "")) for g in d["glossary"]]), ""]
    r.write(f"{j.name}-walkthrough.md", "\n".join(md), "Walkthrough")
    r.data["walkthrough"] = d
    r.summary = f"{len(d['sections'])} section(s), written for {level} by {d['model']}"


# ---------------------------------------------------------------------------
APPROACH_SYSTEM = """A user is about to have a PCB designed from the request below. Before any design work, lay out the
realistically different ways to build it, so they can choose. Reply with one JSON object and nothing else:
{"approaches": [{"name": "short name", "summary": "one or two sentences: the key parts and how it works",
                 "good": "main advantage", "bad": "main cost or risk", "fits": "when to pick it",
                 "line": "one sentence to add to the request that selects this approach"}],
 "open": ["a question whose answer would change the choice"]}
Rules: two or three approaches that differ in architecture (regulator type, controller family, integration level,
connector strategy), not in resistor values. If the request already fixes the architecture, return one approach and
say so in its summary. No part numbers you are not sure exist. Keep every field short."""


def approaches(prompt: str, o, context: str = "", log=None) -> dict:
    """One small model call: ways to build what the request asks for. No design is produced."""
    from .. import llm
    work = Path(tempfile.mkdtemp(prefix="approach-", dir=str(_workdir())))
    be = llm.Pool(o, work, log or (lambda m: None)).get("ask")
    kw = {"tools": []} if isinstance(be, llm.ClaudeCode) else {}
    out = be.ask("REQUEST\n" + prompt.strip() + (("\n\n" + context) if context else ""), APPROACH_SYSTEM, **kw)
    d = _json_from(out)
    d["model"] = llm._who(be)
    return d


def _workdir() -> Path:
    d = HOME / ".work"
    d.mkdir(parents=True, exist_ok=True)
    return d
