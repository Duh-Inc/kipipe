"""Model backends that turn text/images into a validated design spec.

claude-code    `claude -p` headless, logged in with your Claude subscription
               (Pro/Max). No API key; usage counts against your plan. It can
               read the local KiCad libraries to look up exact symbols, pin
               numbers and footprints.
anthropic-api  Messages API with ANTHROPIC_API_KEY (billed per token, separate
               from a Pro subscription).
ollama         Local model (e.g. on a home server). Free, weaker.

Every backend runs the same loop: generate -> validate against the installed
libraries -> send errors back -> repeat (max_fix_rounds).
"""
from __future__ import annotations

import base64
import copy
import functools
import json
import mimetypes
import re
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import queue
import threading
import time
from pathlib import Path

from . import kicad_lib as kl
from . import spec as specmod

DEFAULT_MODELS = {"claude-code": "opus", "anthropic-api": "claude-opus-5-5", "ollama": "qwen3.5:9b"}
LOCAL_DEFAULT = "qwen3.5:9b"

# Which model does which job. Only these steps use a model at all: schematic layout,
# PCB placement and routing are deterministic code (schwire, placer, Freerouting).
ROLES = {
    "spec":    ("opus",   "design the circuit: parts, nets, floorplan (the step that decides the board)"),
    "fix":     ("sonnet", "fix validation errors in the spec (wrong pin numbers, missing nets); mechanical"),
    "escalate": ("opus",  "fix rounds after the first two fail"),
    "review":  ("sonnet", "check the placement render against the request, revise the floorplan"),
    "inspect_detail": ("haiku",  "first inspection pass: many small zoomed tiles, flag candidate problems"),
    "inspect_blocks": ("sonnet", "second pass: larger tiles, confirm or drop pass-1 findings"),
    "inspect_final":  ("opus",   "whole board: verdict, summary, suggested part moves"),
    "extract":  ("haiku",  "read a datasheet into the part database (first read)"),
    "extract_check": ("haiku", "second, independent read of the same datasheet; only disagreements go further"),
    "extract_resolve": ("sonnet", "settle the fields where the two datasheet reads disagree"),
    "triage":   ("haiku",  "decide whether an unusual passive or discrete needs its datasheet read"),
    "conventions": ("haiku", "read a datasheet's conventions (sign, table-wide conditions, package) before the other reads"),
    "ask":      ("haiku",  "answer a question about a finished design from its own data (Toolbox)"),
    "preflight": ("haiku", "before the design: ask what the request leaves open and list the ICs it will need"),
}
# datasheet reading follows its own ladder (cheap reads, a mid model only for disagreements) in every plan;
# set a role explicitly ("extract=sonnet") to change it
_DATASHEET_ROLES = {r: ROLES[r][0] for r in ("extract", "extract_check", "extract_resolve", "triage", "conventions", "ask", "preflight")}
PLANS = {
    "mixed": {r: m for r, (m, _) in ROLES.items()},
    "opus":   {**{r: "opus" for r in ROLES}, **_DATASHEET_ROLES},
    "sonnet": {**{r: "sonnet" for r in ROLES}, **_DATASHEET_ROLES},
    "cheap":  {**{r: "sonnet" for r in ROLES}, "inspect_detail": "haiku", "fix": "haiku", **_DATASHEET_ROLES},
    # Fable (the tier above Opus) where judgement matters most: the circuit design and the final verdict on the board.
    # Billed per token on most plans, so it is never a default
    "fable":  {**{r: m for r, (m, _) in ROLES.items()}, "spec": "fable", "inspect_final": "fable"},
    # Opus does the thinking; a local model (Ollama) does the mechanical fixes and the first look at the tiles
    "igor":   {**{r: m for r, (m, _) in ROLES.items()}, "fix": "local", "inspect_detail": "local",
               "extract": "local", "triage": "local", "conventions": "local", "ask": "local", "preflight": "local"},
}


def parse_models(text) -> dict:
    """'mixed' | 'opus' | 'review=haiku,spec=opus' | 'cheap,spec=opus' -> {role: model}."""
    out = dict(PLANS["mixed"])
    if not text:
        return out
    if isinstance(text, dict):
        text = ",".join(f"{k}={v}" for k, v in text.items())
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip()
            if k == "inspect":
                for r in ("inspect_detail", "inspect_blocks", "inspect_final"):
                    out[r] = v.strip()
                continue
            if k not in ROLES:
                raise ValueError(f"unknown model role {k!r}; roles: {', '.join(ROLES)}")
            out[k] = v.strip()
        elif part in PLANS:
            out.update(PLANS[part])
        else:
            raise ValueError(f"unknown model plan {part!r}; plans: {', '.join(PLANS)} (or role=model)")
    return out


TIERS = {"opus": "big", "fable": "big", "sonnet": "mid", "haiku": "small"}
OPENAI_BACKENDS = ("codex", "openai-api")


def route(o, role: str) -> tuple:
    """(backend, model) for a step. --model forces one model everywhere; 'local' or 'ollama:<tag>' in the plan
    sends that step to Ollama while the rest stays on the main backend."""
    if o.backend == "ollama":
        return "ollama", o.model or DEFAULT_MODELS["ollama"]
    if o.model:
        return o.backend, normalize_model(o.model, o.backend)
    m = parse_models(getattr(o, "models", None)).get(role) or ROLES[role][0]
    if m == "local":
        m = "ollama:" + (getattr(o, "local_model", None) or LOCAL_DEFAULT)
    if m.startswith("ollama:"):
        return "ollama", m[len("ollama:"):]
    if o.backend in OPENAI_BACKENDS:
        # plan names are Claude tiers; on OpenAI they mean big / mid / small, resolved by the backend
        return o.backend, ("tier:" + TIERS[m.lower()]) if m.lower() in TIERS else m
    return o.backend, normalize_model(m, o.backend)


def model_for(o, role: str) -> str:
    kind, m = route(o, role)
    if isinstance(m, str) and m.startswith("tier:"):
        m = {"codex": "Codex default", "openai-api": f"newest {m[5:]} model"}.get(kind, m)
    return m if kind == o.backend else f"{m} (local)"


class Pool:
    """One backend instance per kind for a task, so a conversation continues where the same backend is reused."""
    def __init__(self, o, workdir: Path, log):
        self.o, self.workdir, self.log = o, workdir, log
        self.bes = {}
        self.dead = False         # set when the local model failed; its steps then go to the main backend

    def fallback(self, role):
        """The main backend with the model that step would use without a local helper."""
        self.dead = True
        kind = self.o.backend
        base = ROLES[role][0]
        model = ("tier:" + TIERS[base]) if kind in OPENAI_BACKENDS else normalize_model(base, kind)
        be = self.bes.get(kind) or BACKENDS[kind](self.o, self.workdir, self.log)
        self.bes[kind] = be
        be.model = model
        be.role = role
        be.workdir = self.workdir
        return be

    def get(self, role):
        kind, model = route(self.o, role)
        if kind == "ollama" and self.dead and self.o.backend != "ollama":
            return self.fallback(role)
        be = self.bes.get(kind)
        if be is None:
            if kind not in BACKENDS:
                raise RuntimeError(f"unknown backend {kind}")
            be = self.bes[kind] = BACKENDS[kind](self.o, self.workdir, self.log)
        be.model = model
        be.role = role
        be.workdir = self.workdir
        return be


def use(be, o, role, log=None):
    kind, m = route(o, role)
    if m and m != getattr(be, "model", None):
        be.model = m
    be.role = role
    return be.model


def normalize_model(name, backend):
    """Accept what people type ("Opus 5.5", "opus", "claude-opus-5-5", "Sonnet") and
    turn it into something the backend understands."""
    if not name or backend in ("ollama", "codex", "openai-api"):
        return name
    n = name.strip()
    low = n.lower().replace("claude", "").strip(" -_")
    m = re.match(r"^(opus|sonnet|haiku|fable)\s*[-_ ]?\s*(\d+)(?:[.\-_](\d+))?$", low)
    if m:
        fam, major, minor = m.group(1), m.group(2), m.group(3)
        return f"claude-{fam}-{major}" + (f"-{minor}" if minor else "")
    if low in ("opus", "sonnet", "haiku", "fable"):
        return low if backend == "claude-code" else {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                                                     "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-2"}[low]
    return n


def _calc_doc() -> str:
    from .calc import PROMPT_DOC
    return PROMPT_DOC


def system_prompt(lib_hint: str) -> str:
    env = kl.detect()
    return f"""You are an electronics design engineer producing a circuit for an automated KiCad {env.major} pipeline.
You do NOT write KiCad files. You output ONE JSON object (no prose, no code fences) in exactly this format:

{specmod.SPEC_DOC}

{_calc_doc()}

Rules:
- "symbol" and "footprint" must be exact identifiers from the standard KiCad {env.major} libraries ("Lib:Name").
  Prefer generic symbols: {_generic_symbols()}, Connector_Generic:Conn_01xNN.
  Common footprints: Resistor_SMD:R_0805_2012Metric, Capacitor_SMD:C_0805_2012Metric, LED_SMD:LED_0805_2012Metric,
  Connector_PinHeader_2.54mm:PinHeader_1xNN_P2.54mm_Vertical, Package_TO_SOT_SMD:SOT-23, Package_SO:SOIC-8_3.9x4.9mm_P1.27mm.
- Net pins use the symbol's pin NUMBER ("U1.8"), never the pin name. Symbol pin numbers must exist as pads in the footprint.
- Every pin is in exactly one net or listed in "no_connect".
- Include decoupling capacitors (100n at each IC supply pin, bulk cap per rail) and anything else a careful engineer would.
- Lay the board out the way a layout engineer would, through "edge" and "floorplan":
  * connectors, switches, user-facing LEDs and buttons on edges ("edge"); things the user said go somewhere go there;
  * functional blocks (power supply, MCU + crystal + decoupling, analog front end, driver stage) as "regions",
    keeping noisy/high-current parts away from sensitive ones;
  * anything that should look regular (LED matrix, keypad, LED bar, row of buttons) as "grids";
  * "near" for parts that must hug a pin (crystal + load caps to the MCU, gate resistors, bootstrap caps);
    decoupling caps are handled automatically, don't list them;
  * modules with antennas (ESP32, nRF, LoRa) are pushed to an edge with the antenna overhanging automatically;
    pick that edge with "edge" if it matters and keep other parts out of the antenna region.
- Declare power nets in a net class with a realistic "current_a" so traces get sized correctly.
- High voltage: a net class clearance applies between its nets and EVERY other net, including the two pads of one
  part. Size clearance from the voltage that actually appears between neighbouring copper (IPC-2221B external,
  uncoated: up to 50 V 0.6 mm, 150 V 0.6, 300 V 1.25, 500 V 2.5, above 500 V 2.5 mm + 0.005 mm/V). In a voltage
  multiplier each diode and capacitor sees one stage, so the stage nodes need only the per-stage clearance; the
  full clearance matters between the high-voltage output and low-voltage/ground copper and across parts that see
  the full voltage (output capacitor, bleeder, divider top). Every part must have its own pads at least as far apart
  as its nets' clearance: e.g. SMA diode pads are 1.5 mm apart, SMB 1.8, SMC 4.3, DO-41 8 mm; 1206 1.8, 2512 4.7,
  1812 capacitors 2.7. Keep the high-voltage section in its own region away from inputs and logic.
- Use "rf" ONLY for true RF signal paths (antenna feeds, RF amplifier/filter/mixer paths, roughly >50 MHz analog).
  Never for USB, I2C, SPI, UART, Ethernet or other digital lines; differential pairs are not supported, leave them out.
- Net names: short, uppercase, A-Z 0-9 _ + - only (e.g. +3V3, GND, SDA, RF_OUT).
- Fill "rationale" for the user: "design" explains how the circuit works with the actual calculations; "parts" gives
  every non-trivial part (ICs, connectors, anything with a computed value or a rating that matters) one or two
  sentences on why that part and value; "placement" explains the floorplan choices. Be specific and brief.
- If an image is provided it may be a hand sketch, photo or block diagram: transcribe its circuit faithfully, then complete it.
{lib_hint}"""


def user_prompt(o, image_paths: list[Path], can_read_files: bool) -> str:
    parts = [o.prompt.strip() or "Design the circuit shown in the image."]
    if image_paths:
        if can_read_files:
            parts.append("Reference image(s) (read them first): " + ", ".join(str(p) for p in image_paths))
        else:
            parts.append("Reference image(s) attached.")
    b = []
    if o.layers:
        b.append(f"{o.layers} layers")
    if o.fab:
        b.append(f"fab {o.fab}")
    if o.width and o.height:
        b.append(f"board {o.width} x {o.height} mm")
    if o.holes:
        b.append(f"mounting holes {o.holes}")
    if getattr(o, "shape", None):
        b.append(f"board shape {o.shape}")
    if b:
        parts.append("Board constraints: " + ", ".join(b) + ".")
    try:                                    # the run's context, its project and the user's house rules
        from .addons import projects as _projects
        ctx = _projects.prompt_block(o, getattr(o, "_workdir", None) or ".")
        if ctx:
            parts.append(ctx)
    except Exception:
        pass
    parts.append("Output only the JSON object.")
    return "\n\n".join(parts)


def fix_prompt(report: specmod.Report, err: str | None = None) -> str:
    if err:
        return f"Your output was not valid JSON ({err}). Output ONLY the JSON object."
    return ("The pipeline validated your spec against the installed KiCad libraries and found problems. "
            "Fix ALL errors (warnings are optional but fix them if they are real mistakes) and output the "
            "complete corrected JSON object only.\n\n" + report.text())


# ---------------------------------------------------------------------------

class ClaudeCode:
    """Headless Claude Code using the logged-in subscription."""

    def __init__(self, o, workdir: Path, log):
        self.exe = shutil.which("claude")
        if not self.exe:
            raise RuntimeError("Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code, then run `claude` once to log in with your Pro account.")
        self.model = normalize_model(o.model, "claude-code") or DEFAULT_MODELS["claude-code"]
        self.dir = workdir
        self.log = log
        self.session = None
        self.lib_dirs = [str(p) for p in kl.detect().symbol_dirs + kl.detect().footprint_dirs]

    def ask(self, text: str, system: str, images=None, max_turns: int = 40, timeout: int = 1500, tools=None) -> str:
        """Runs claude -p with streaming output so progress shows up in the log while it works.
        tools: None = library lookups (Read, Grep, Glob); a list = exactly those ([] = none, for questions that
        carry everything they need, so the model doesn't spend turns poking around)."""
        if images:
            text = text + "\n\nImages (read every one before answering):\n" + "\n".join(str(i) for i in images)
        allowed = ["Read", "Grep", "Glob"] if tools is None else list(tools)
        cmd = [self.exe, "-p", text, "--output-format", "stream-json", "--verbose", "--model", self.model,
               "--max-turns", str(max_turns), "--tools", ",".join(allowed), "--permission-mode", "default"]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        for d in self.lib_dirs:
            cmd += ["--add-dir", d]
        if self.session:
            cmd += ["--resume", self.session]
        else:
            cmd += ["--append-system-prompt", system]
        keep = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
        env = {k: v for k, v in kl.clean_env().items()
               if not (k.startswith("CLAUDE") and k not in keep)}   # don't inherit a parent Claude session
        env.pop("ANTHROPIC_API_KEY", None)     # force subscription auth, not API billing
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir, env=env, bufsize=1)
        q: "queue.Queue[str | None]" = queue.Queue()

        def pump(stream):
            for line in stream:
                q.put(line)
            q.put(None)
        threading.Thread(target=pump, args=(proc.stdout,), daemon=True).start()
        err_lines: list = []
        threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True).start()
        t0 = time.time()
        last_beat = t0
        result, turns, tools = None, 0, 0
        while True:
            try:
                line = q.get(timeout=5)
            except queue.Empty:
                line = ""
            now = time.time()
            if line is None:
                break
            if now - t0 > timeout:
                proc.kill()
                raise RuntimeError(f"claude -p ran longer than {timeout // 60} min ({turns} turns, {tools} lookups) and was stopped")
            if now - last_beat >= 30:
                self.log(f"  claude-code ({self.model}): still working, {int(now - t0)} s, {turns} turns, {tools} library lookups")
                last_beat = now
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                self.log(f"  claude-code ({ev.get('model') or self.model}): started")
            elif kind == "assistant":
                turns += 1
                for blk in (ev.get("message") or {}).get("content") or []:
                    if blk.get("type") == "tool_use":
                        tools += 1
                        self.log("    " + _describe_tool(blk.get("name", ""), blk.get("input") or {}))
                        last_beat = now
            elif kind == "result":
                result = ev
        proc.wait(timeout=30)
        if result is None:
            msg = "".join(err_lines).strip()[-500:]
            raise RuntimeError(f"claude -p ended without a result (exit {proc.returncode}): {msg or 'no output'}")
        if result.get("is_error") or result.get("subtype") not in (None, "success"):
            msg = str(result.get("result") or result.get("subtype") or result)
            if result.get("subtype") == "error_max_turns":
                raise RuntimeError(f"claude used all {max_turns} turns without finishing (it was still looking things up)")
            if re.search(r"issue with the selected model|may not exist|not have access", msg, re.I) and self.model not in ("sonnet", "haiku"):
                down = "opus" if "fable" in str(self.model) else "sonnet"
                self.log(f"  claude-code: model '{self.model}' unavailable ({msg[:90]}); falling back to {down}")
                self.model = down
                return self.ask(text, system, images, max_turns, timeout)
            raise RuntimeError(f"claude error: {msg[:500]}")
        self.session = result.get("session_id") or self.session
        cost = result.get("total_cost_usd")
        self.last_usage = {"cost": cost}
        self.log(f"  claude-code: done in {int(time.time() - t0)} s, {result.get('num_turns', turns)} turns, {tools} lookups"
                 + (f", ~${cost:.3f} equivalent" if cost else ""))
        return result.get("result", "")


def _describe_tool(name: str, inp: dict) -> str:
    """One readable log line per lookup Claude makes."""
    short = lambda p: "/".join(str(p).split("/")[-2:]) if p else ""
    if name == "Grep":
        where = short(inp.get("path") or inp.get("glob") or "")
        return f"searching for '{str(inp.get('pattern', ''))[:60]}'" + (f" in {where}" if where else "")
    if name == "Glob":
        return f"listing {str(inp.get('pattern', ''))[:70]}"
    if name == "Read":
        return f"reading {short(inp.get('file_path', ''))}"
    return f"{name} {json.dumps(inp)[:80]}"


class AnthropicAPI:
    def __init__(self, o, workdir: Path, log):
        self.key = o.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set (Claude Pro does not include API access; use the claude-code backend for that)")
        self.model = normalize_model(o.model, "anthropic-api") or DEFAULT_MODELS["anthropic-api"]
        self.messages: list = []
        self.log = log
        self.images: list = []

    def ask(self, text: str, system: str, images=None) -> str:
        content = []
        imgs = images if images is not None else (self.images if not self.messages else [])
        if imgs:
            for img in imgs:
                mt = mimetypes.guess_type(str(img))[0] or "image/png"
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                            "data": base64.b64encode(Path(img).read_bytes()).decode()}})
        content.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": content})
        body = json.dumps({"model": self.model, "max_tokens": 16000, "system": system, "messages": self.messages}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read())
        out = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        self.messages.append({"role": "assistant", "content": out})
        u = data.get("usage", {})
        self.last_usage = {"in": u.get("input_tokens"), "out": u.get("output_tokens")}
        self.log(f"  anthropic-api: {u.get('input_tokens', '?')} in / {u.get('output_tokens', '?')} out tokens")
        return out


class Ollama:
    def __init__(self, o, workdir: Path, log):
        self.host = o.ollama_host.rstrip("/")
        self.model = o.model or DEFAULT_MODELS["ollama"]
        self.messages: list = []
        self.log = log
        self.images: list = []

    def ask(self, text: str, system: str, images=None) -> str:
        imgs = images if images is not None else (self.images if not self.messages else [])
        if not self.messages:
            self.messages.append({"role": "system", "content": system})
        msg = {"role": "user", "content": text}
        if imgs:
            msg["images"] = [base64.b64encode(Path(i).read_bytes()).decode() for i in imgs]
        self.messages.append(msg)
        has_img = any("images" in m for m in self.messages)
        body = json.dumps({"model": self.model, "messages": self.messages, "stream": False, "format": "json",
                           "think": False, "options": {"num_ctx": getattr(self, "num_ctx", None) or (32768 if has_img else 16384)}}).encode()
        req = urllib.request.Request(self.host + "/api/chat", data=body, headers={"content-type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=1200) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Ollama ({self.model}) error {e.code}: {e.read().decode(errors='replace')[:300]}")
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"Ollama not reachable at {self.host}: {e}")
        out = data.get("message", {}).get("content", "")
        self.messages.append({"role": "assistant", "content": out})
        n = data.get("eval_count") or 0
        dt = time.time() - t0
        self.last_usage = {"in": data.get("prompt_eval_count"), "out": n}
        self.log(f"  ollama {self.model} (local): {n} tokens in {dt:.0f} s" + (f", {n / dt:.0f} tok/s" if n and dt else ""))
        return out


class Codex:
    """OpenAI Codex CLI, headless (codex exec), on the user's ChatGPT Plus/Pro login. Each call is its own run
    (stateless): kipipe always sends the full context, so nothing depends on resuming sessions."""
    stateless = True

    def __init__(self, o, workdir: Path, log):
        self.exe = shutil.which("codex")
        if not self.exe:
            raise RuntimeError("Codex CLI not found. Install: npm install -g @openai/codex, then run `codex login` "
                               "and sign in with your ChatGPT account (or `codex login --device-auth` without a browser).")
        self.model = o.model or None
        self.dir = workdir
        self.log = log
        self.images: list = []
        self.calls = 0

    def ask(self, text: str, system: str, images=None, timeout: int = 1500) -> str:
        imgs = list(images if images is not None else (self.images if self.calls == 0 else []))
        self.calls += 1
        out = self.dir / "input" / f"codex_out_{self.calls}.txt"
        out.parent.mkdir(exist_ok=True)
        model = None if (not self.model or str(self.model).startswith("tier:")) else self.model
        cmd = [self.exe, "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-C", str(self.dir), "-o", str(out)]
        if model:
            cmd += ["-m", model]
        for i in imgs:
            cmd += ["-i", str(i)]
        cmd += ["--", f"{system}\n\n---\n\n{text}"]
        env = dict(kl.clean_env())
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):      # use the ChatGPT login, never API billing
            env.pop(k, None)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir, env=env, bufsize=1)
        q: "queue.Queue[str | None]" = queue.Queue()
        threading.Thread(target=lambda: ([q.put(line) for line in proc.stdout], q.put(None)), daemon=True).start()
        err: list = []
        threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True).start()
        t0 = last_beat = time.time()
        steps, last_msg = 0, ""
        self.log(f"  codex ({model or 'default model'}): started")
        while True:
            try:
                line = q.get(timeout=5)
            except queue.Empty:
                line = ""
            now = time.time()
            if line is None:
                break
            if now - t0 > timeout:
                proc.kill()
                raise RuntimeError(f"codex exec ran longer than {timeout // 60} min and was stopped")
            if now - last_beat >= 30:
                self.log(f"  codex: still working, {int(now - t0)} s, {steps} commands")
                last_beat = now
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = ev.get("item") or {}
            kind = item.get("type") or item.get("item_type") or ""
            if ev.get("type", "").endswith("started") and "command" in kind:
                steps += 1
                self.log("    running: " + str(item.get("command", ""))[:110])
                last_beat = now
            if "message" in kind and item.get("text"):
                last_msg = item["text"]
            if ev.get("type") in ("error", "turn.failed"):
                msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:300]
                raise RuntimeError(f"codex error: {msg}")
        proc.wait(timeout=30)
        result = out.read_text() if out.exists() and out.stat().st_size else last_msg
        if not result:
            msg = "".join(err).strip()[-500:]
            if re.search(r"log ?in|auth|unauthori", msg, re.I):
                msg += "  (run `codex login` and sign in with your ChatGPT account)"
            raise RuntimeError(f"codex exec gave no answer (exit {proc.returncode}): {msg or 'no output'}")
        self.log(f"  codex: done in {int(time.time() - t0)} s, {steps} commands")
        return result


class OpenAIAPI:
    """OpenAI Responses API with an API key (pay per token, or the API credits ChatGPT Pro includes).
    Plan tiers resolve to the account's newest models, so no model name is hard-coded."""

    def __init__(self, o, workdir: Path, log):
        self.key = o.api_key or os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise RuntimeError("OPENAI_API_KEY not set (a ChatGPT subscription signs in through the Codex backend instead; "
                               "ChatGPT Pro's monthly API credits work with a key from the linked API organization)")
        self.model = o.model or "tier:big"
        self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")   # or any compatible server
        self.log = log
        self.images: list = []
        self.prev = None
        self._models = None

    def _resolve(self, m):
        if not str(m).startswith("tier:"):
            return m
        if self._models is None:
            req = urllib.request.Request(self.base + "/models", headers={"authorization": f"Bearer {self.key}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read()).get("data", [])
            skip = re.compile(r"audio|realtime|image|tts|whisper|embed|moderation|search|transcribe|codex|dall|sora|computer|instruct", re.I)
            self._models = sorted([d for d in data if str(d.get("id", "")).startswith(("gpt-", "o")) and not skip.search(d["id"])],
                                  key=lambda d: d.get("created", 0), reverse=True)
        small = re.compile(r"mini|nano|luna|lite", re.I)
        tier = m[5:]
        pool = [d["id"] for d in self._models if bool(small.search(d["id"])) == (tier == "small") and not d["id"].endswith("-pro")]
        if not pool:
            pool = [d["id"] for d in self._models]
        if not pool:
            raise RuntimeError("no usable chat models found for this API key")
        pick = pool[0]
        if pick != getattr(self, "_said", {}).get(tier):
            self._said = {**getattr(self, "_said", {}), tier: pick}
            self.log(f"  openai-api: using {pick} for {tier} jobs")
        return pick

    def ask(self, text: str, system: str, images=None) -> str:
        imgs = images if images is not None else (self.images if self.prev is None else [])
        content = [{"type": "input_text", "text": text}]
        for img in imgs or []:
            mt = mimetypes.guess_type(str(img))[0] or "image/png"
            content.append({"type": "input_image", "image_url": f"data:{mt};base64," + base64.b64encode(Path(img).read_bytes()).decode()})
        body = {"model": self._resolve(self.model), "instructions": system,
                "input": [{"role": "user", "content": content}], "text": {"format": {"type": "json_object"}}}
        if self.prev:
            body["previous_response_id"] = self.prev
        req = urllib.request.Request(self.base + "/responses", data=json.dumps(body).encode(), headers={
            "authorization": f"Bearer {self.key}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"OpenAI API error {e.code}: {e.read().decode(errors='replace')[:400]}")
        self.prev = data.get("id") or self.prev
        out = "".join(c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                      for c in item.get("content", []) if c.get("type") == "output_text")
        u = data.get("usage", {})
        self.last_usage = {"in": u.get("input_tokens"), "out": u.get("output_tokens")}
        self.log(f"  openai-api ({body['model']}): {u.get('input_tokens', '?')} in / {u.get('output_tokens', '?')} out tokens")
        return out


BACKENDS = {"claude-code": ClaudeCode, "anthropic-api": AnthropicAPI, "ollama": Ollama,
            "codex": Codex, "openai-api": OpenAIAPI}

try:                                    # usage ledger: timing, reported cost and the optional budget, for every backend
    from .addons import usage as _usage
    _usage.instrument(BACKENDS)
except Exception:                       # the ledger is a convenience; never let it stop a design
    pass


def generate_spec(o, workdir: Path, log) -> dict:
    if o.backend not in BACKENDS:
        raise RuntimeError(f"unknown backend {o.backend}")
    inp = workdir / "input"
    inp.mkdir(exist_ok=True)
    (inp / "prompt.txt").write_text(o.prompt or "")
    images = []
    for i, src in enumerate(o.images):
        dst = inp / f"image{i}{Path(src).suffix.lower() or '.png'}"
        if Path(src).resolve() != dst.resolve():
            shutil.copy(src, dst)
        images.append(dst)
    o._workdir = workdir
    pool = Pool(o, workdir, log)
    be = pool.get("spec")
    can_read = isinstance(be, ClaudeCode)
    if not can_read:
        be.images = images
    system = system_prompt(_lib_hint(be, o.prompt, o=o))
    prompt = user_prompt(o, images, can_read)
    key = spec_key(be, system, prompt, images, workdir)
    hit = cached_spec(key, o, log)
    if hit:
        return hit
    log(f"asking {_who(be)} to design the circuit")
    text = be.ask(prompt, system)
    spec = _spec_loop(pool, be, o, workdir, text, log)
    store_spec(key, spec)
    return spec


# ---- spec cache: the same request to the same model gives the same design, every time

SPEC_CACHE = Path(os.environ.get("KIPIPE_CACHE", Path.home() / ".cache/kipipe")) / "specs"


def spec_key(be, system, prompt, images, workdir) -> str:
    """Hash of everything the model sees for the first design call: model, system prompt (which carries the
    library hints, part records, blocks, project and house rules), the request text and image bytes. Job folder
    paths are blanked so two jobs with the same request share the key."""
    import hashlib
    h = hashlib.sha256()
    kind = next((k for k, v in BACKENDS.items() if isinstance(be, v)), "?")
    for part in (kind, str(be.model), system, prompt.replace(str(workdir), "<job>")):
        h.update(part.encode())
        h.update(b"\0")
    for im in images:
        h.update(Path(im).read_bytes())
    return h.hexdigest()[:24]


def cached_spec(key, o, log):
    p = SPEC_CACHE / f"{key}.json"
    if getattr(o, "fresh", False) or not p.exists():
        return None
    try:
        spec = json.loads(p.read_text())
    except Exception:
        return None
    rep = specmod.validate(copy.deepcopy(spec))
    if not rep.ok:                           # libraries changed since: design it again
        log("  cached design no longer validates against your libraries; asking the model again")
        return None
    log(f"reusing the design from an identical earlier request (cache {key[:8]}); --fresh asks the model again")
    return spec


def store_spec(key, spec):
    try:
        SPEC_CACHE.mkdir(parents=True, exist_ok=True)
        (SPEC_CACHE / f"{key}.json").write_text(json.dumps(spec, indent=1))
    except OSError:
        pass


def _lib_hint(be, prompt: str = "", spec: dict | None = None, o=None) -> str:
    from . import libs, partdb
    blocks_hint = ""
    if o is not None and "blocks" in getattr(o, "flags", ()):
        from .addons import blocks as _blocks
        blocks_hint = _blocks.prompt_hint()
    mine = "\n\n".join(x for x in (libs.prompt_hint(), partdb.prompt_hint(prompt, spec), blocks_hint) if x)
    mine = ("\n\n" + mine) if mine else ""
    sd, fd = list(map(str, kl.detect().symbol_dirs)), list(map(str, kl.detect().footprint_dirs))
    if libs.SYM.exists():
        sd.append(str(libs.SYM))
        fd.append(str(libs.FP))
    dirs = f"symbols in {', '.join(sd)}, footprints in {', '.join(fd)}"
    if isinstance(be, ClaudeCode):
        return f"You may search the local KiCad libraries with Grep/Glob/Read to confirm symbol names, pin numbers and footprints: {dirs}. Keep lookups targeted." + mine
    if isinstance(be, Codex):
        return (f"You may search the local KiCad libraries with read-only shell commands (rg, grep, head) to confirm "
                f"symbol names, pin numbers and footprints: {dirs}. Keep lookups targeted; never modify files." + mine)
    return mine.strip()


def _who(be) -> str:
    kind = next((k for k, v in BACKENDS.items() if isinstance(be, v)), "?")
    m = be.model
    if not m or str(m).startswith("tier:"):
        m = "default model" if kind == "codex" else f"newest {str(m)[5:]} model" if m else "default"
    return f"{kind} ({m}{', local' if kind == 'ollama' else ''})"


REVISE_PROMPT = """This is an existing design that has already been laid out:

{spec}

Change it as follows:
{request}

Return the COMPLETE updated spec as one JSON object. Rules for a revision:
- Change only what the request needs. Every part that stays keeps its ref, symbol, footprint and value exactly,
  so it can stay where it is on the board.
- New parts get new refs; never reuse the ref of a removed part for something else.
- Keep net names whose meaning is unchanged; update netclasses, rf and floorplan to match the new circuit.
- Update "rationale" for anything you changed, and add "rationale.changes": a short list of what changed and why.
Output only the JSON object."""


def revise_spec(o, workdir: Path, spec: dict, request: str, log) -> dict:
    """Ask the model to modify an existing spec; same validation loop as a new design."""
    pool = Pool(o, workdir, log)
    be = pool.get("spec")
    system = system_prompt(_lib_hint(be, request, spec, o=o))
    log(f"asking {_who(be)} to revise the design")
    (workdir / "input").mkdir(exist_ok=True)
    (workdir / "input" / "revision.txt").write_text(request)
    try:
        from .addons import projects as _projects
        ctx = _projects.prompt_block(o, workdir)
        if ctx:
            request = request.strip() + "\n\nBackground that still applies (do not treat it as a change request):\n" + ctx
    except Exception:
        pass
    text = be.ask(REVISE_PROMPT.format(spec=json.dumps(spec, indent=1), request=request.strip()), system)
    new = _spec_loop(pool, be, o, workdir, text, log)
    new["name"] = spec.get("name", new.get("name"))
    return new


FIX_FULL = """This circuit design spec failed validation against the installed KiCad libraries.

SPEC:
{spec}

PROBLEMS:
{problems}

Fix every ERROR (warnings only if they are real mistakes). Use the suggestions and pin lists given in the problems.
Change nothing else. Output the complete corrected spec as one JSON object and nothing else."""


@functools.lru_cache(maxsize=1)
def _generic_symbols() -> str:
    """The generic symbols worth naming to the designer, checked against the installed libraries (names moved
    between KiCad versions: Device:Q_NMOS_GSD is Transistor_FET:Q_NMOS_GSD from KiCad 9 on)."""
    want = ["Device:R", "Device:C", "Device:C_Polarized", "Device:L", "Device:LED", "Device:D", "Device:D_Schottky",
            "Device:Q_NPN_BEC", "Device:Q_NMOS_GSD", "Device:Q_PMOS_GSD", "Connector:TestPoint", "Switch:SW_Push"]
    out = []
    for w in want:
        try:
            kl.get_symbol(w)
            out.append(w)
        except Exception:
            moved = specmod._relocated(w)
            if moved:
                out.append(moved)
    return ", ".join(out)


def _spec_loop(pool, be, o, workdir, text, log) -> dict:
    """Validate the model's spec against the libraries; send errors back until it is valid.
    A fix round may go to a different backend (e.g. a local model); then it gets the whole spec, since it
    wasn't part of the conversation that wrote it.
    A correction has one job: remove the listed errors. If what comes back is no longer the same design (parts or
    connections gone, or not even JSON), it is thrown away, the previous version is kept, and the stronger model
    takes over. Without this, a small model asked to fix three errors can hand back a different, smaller board."""
    last = be                 # who wrote the current text
    good = None               # (spec, report): the latest version that is still the designer's design
    base_refs, base_pins = None, 0
    strong = False            # once a fix has been thrown away, do not ask the light model again
    for rnd in range(o.max_fix_rounds + 1):
        (workdir / f"input/response{rnd}.txt").write_text(text)
        err, rep = None, None
        try:
            spec = specmod.normalize(specmod.load(text))
        except Exception as e:
            err = str(e)
            spec = None
        thrown = None
        if spec is None and good is not None:
            thrown = "it was not valid JSON"
        elif spec is not None and base_refs:
            refs = {c.get("ref") for c in spec.get("components", [])}
            pins = sum(len(v) for v in (spec.get("nets") or {}).values())
            if len(refs & base_refs) < 0.85 * len(base_refs) or pins < 0.7 * base_pins:
                thrown = f"it kept {len(refs & base_refs)} of {len(base_refs)} parts and {pins} of {base_pins} connections"
        if thrown:
            log(f"  {_who(last)}'s correction was thrown away: {thrown}. A correction must not redesign the board; "
                "keeping the previous version and handing the errors to the stronger model")
            spec, rep = good
            err, strong, last = None, True, None
        elif spec is not None:
            for k, v in (("layers", o.layers), ("fab", o.fab), ("width_mm", o.width), ("height_mm", o.height),
                         ("mounting_holes", o.holes), ("shape", getattr(o, "shape", None))):
                if v not in (None, ""):
                    spec["board"][k] = v
            rep = specmod.validate(spec)
            if rep.ok:
                log(f"  spec valid after {rnd} correction round(s)" + (f", {len(rep.warnings)} warnings" if rep.warnings else ""))
                return spec
            if base_refs is None:
                base_refs = {c.get("ref") for c in spec.get("components", [])}
                base_pins = sum(len(v) for v in (spec.get("nets") or {}).values())
            good = (spec, rep)
        if rnd == o.max_fix_rounds:
            if spec is None:
                raise RuntimeError(f"model did not return valid JSON: {err}")
            (workdir / "spec_invalid.json").write_text(json.dumps(spec, indent=2))
            raise RuntimeError("model could not produce a valid spec:\n" + rep.text())
        role = "escalate" if (rnd >= 2 or strong) else "fix"
        fb = pool.get(role)
        if strong and isinstance(fb, Ollama):
            fb = pool.fallback(role)
        if err:
            log(f"  round {rnd + 1}: invalid JSON, asking {_who(fb)} for a fix")
        else:
            log(f"  round {rnd + 1}: {len(rep.errors)} errors, sending to {_who(fb)}")
            for e in rep.errors[:8]:
                log("    " + e)
        if fb is last and not getattr(fb, "stateless", False):
            prompt = fix_prompt(rep, err)
        else:
            if isinstance(fb, Ollama):
                fb.messages = []          # fresh, short conversation for the helper
            body = json.dumps(spec, indent=1) if spec is not None else text[:20000]
            prompt = FIX_FULL.format(spec=body, problems=(f"Output was not valid JSON: {err}" if err else rep.text()))
        try:
            text = fb.ask(prompt, system_prompt(_lib_hint(fb, o=o)))
            last = fb
        except Exception as e:
            if fb is last or not isinstance(fb, Ollama):
                raise
            main = pool.fallback(role)
            log(f"  local model failed ({str(e)[:140]}); asking {_who(main)} instead, and for the rest of this run")
            body = json.dumps(spec, indent=1) if spec is not None else text[:20000]
            text = main.ask(FIX_FULL.format(spec=body, problems=(f"Output was not valid JSON: {err}" if err else rep.text()))
                            if main is not last else fix_prompt(rep, err), system_prompt(_lib_hint(main, o=o)))
            last = main
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Placement review
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = """You review PCB placements made by an automatic placer that follows a floorplan.
Judge the image like a layout engineer: does it match what the user asked for and the obvious conventions?
Check: connectors/switches/user LEDs reachable on sensible edges; antenna modules at an edge with the
antenna off the board and nothing beside it; functional blocks grouped (power, MCU+crystal, analog);
noisy/high-current parts away from sensitive ones; arrays (matrices, button rows) regular; anything absurd.
The placer obeys only these controls, so express every fix through them:
  edges: {"REF": "left"|"right"|"top"|"bottom"|null}
  floorplan: {"grids": [...], "regions": [...], "near": [...], "keepouts": [...]} (same format as given)
Revise only for real problems, not taste or tiny wirelength gains.
Reply with ONE JSON object and nothing else:
{"verdict": "ok" | "revise", "issues": ["short description", ...],
 "edges": {...only parts whose edge changes...}, "floorplan": {...complete floorplan, or omit if unchanged...}}"""


def review_placement(o, workdir: Path, png: Path, context: str, log) -> dict:
    be = Pool(o, workdir, log).get("review")
    if isinstance(be, ClaudeCode):
        text = context + f"\n\nPlacement image (read it first): {png}"
    else:
        be.images = [png]
        text = context + "\n\nPlacement image attached."
    log(f"  asking {_who(be)} to review the placement")
    out = be.ask(text, REVIEW_SYSTEM)
    (workdir / "input").mkdir(exist_ok=True)
    (workdir / "input" / f"review_{len(list((workdir / 'input').glob('review_*')))}.txt").write_text(out)
    return specmod.load(out)


# ---------------------------------------------------------------------------
# Routed-board inspection (coarse-to-fine)
# ---------------------------------------------------------------------------

INSPECT_SYSTEM = """You are a senior PCB layout engineer inspecting a finished, autorouted 2- or 4-layer board.
You will see it in passes: small detailed tiles first, then larger tiles, then the whole board.
Images are drawn from the board data: red = top copper, blue = bottom copper, orange = pads (blue-ish = bottom-side
pads), grey rings = vias, grey boxes = courtyards, white = reference designators, grid labels in mm from the
board's top-left corner. Ground is poured on all copper layers but the pour is NOT drawn.

Look for real problems a reviewer would flag, for example: traces squeezed between pads or passing very close
to other copper, acute angles, unnecessary vias or vias in pads, long detours, decoupling caps far from the pin
they serve, crystal/oscillator traces long or crossing other signals, power traces visibly thinner than they
should be, connectors facing into the board, parts crowding mounting holes or edges, high-speed/USB pair
routing that is badly asymmetric, things that will be hard to solder or probe, layout that contradicts the request.
Don't invent problems; if a region is fine, say nothing about it. Use mm coordinates (x, y) for locations.

Every reply is ONE JSON object, nothing else:
{"findings": [{"severity": "error"|"warning"|"note", "x": mm, "y": mm, "refs": ["U1", ...],
               "issue": "what is wrong", "fix": "what to change"}],
 "dismissed": {"F3": "why that earlier finding is wrong"}}   // later passes only; {} if none
Earlier findings are listed with ids (F1, F2, ...). If what you see now shows one is wrong (misread copper,
a short that isn't there), dismiss it by id instead of writing a correction as a new finding.
On the final whole-board pass add:
 "verdict": "ok" | "issues",
 "summary": "two or three sentences",
 "moves": {"REF": {"x": mm, "y": mm, "rot": degrees}}   // only for placement fixes you are confident in;
                                                         // board coordinates of the part origin, or {} """


def inspect_board(o, workdir: Path, passes: list, context: str, log) -> list:
    """Run the passes in one conversation. Returns [{pass, findings, raw}, ...]."""
    pool = Pool(o, workdir, log)
    results = []
    all_findings = []
    briefed = set()           # backends that have seen the request/board context
    for i, ps in enumerate(passes):
        last = i == len(passes) - 1
        tiles = "\n".join(f"  tile {t['id']}: x {t['crop'][0]:.0f}-{t['crop'][2]:.0f} mm, y {t['crop'][1]:.0f}-{t['crop'][3]:.0f} mm"
                          for t in ps["tiles"])
        role = "inspect_final" if last else ("inspect_detail" if i == 0 else "inspect_blocks")
        be = pool.get(role)
        intro = (context + "\n\n") if id(be) not in briefed or getattr(be, "stateless", False) else ""
        briefed.add(id(be))
        text = (f"{intro}{ps['name'].upper()} ({len(ps['tiles'])} image(s)):\n{tiles}\n\n"
                + ("Report local problems you can see at this zoom. " if i == 0 else
                   "Building on your earlier findings, report problems visible at this scale (don't repeat earlier ones). ")
                + ("This is the final pass: also give verdict, summary and moves." if last else ""))
        if i > 0 and all_findings:
            text += "\n\nEarlier findings:\n" + "\n".join(
                f"  {f['id']} [{f.get('severity')}] {', '.join(f.get('refs') or [])}: {str(f.get('issue'))[:220]}" for f in all_findings)
        if i > 0 and route(o, "inspect_detail") != route(o, role):
            text += (" Earlier findings may come from a faster, less careful model: re-check them against what you see "
                     "and drop any that are wrong rather than repeating them.")
        log(f"  inspect: {ps['name']} ({len(ps['tiles'])} image(s)) with {_who(be)}")
        try:
            raw = be.ask(text, INSPECT_SYSTEM, images=[t["file"] for t in ps["tiles"]])
        except Exception as e:
            if not isinstance(be, Ollama):
                raise
            pool.dead = True
            log(f"  local model failed ({str(e)[:140]}); this pass is skipped and the next pass covers it")
            results.append({"pass": ps["name"], "data": {"findings": []}, "model": f"{be.model} (failed)"})
            continue
        try:
            data = specmod.load(raw)
        except Exception:
            data = {"findings": [], "parse_error": raw[:500]}
        for f in data.get("findings") or []:
            if isinstance(f, dict):
                f["id"] = f"F{len(all_findings) + 1}"
                all_findings.append(f)
        results.append({"pass": ps["name"], "data": data, "model": be.model + (" (local)" if isinstance(be, Ollama) else "")})
    return results
