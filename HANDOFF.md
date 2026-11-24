# kipipe: handoff notes

Feature inventory with status: FEATURES.md (tests/test_features.py keeps it complete).

Written 2026-11-14 for whoever continues this (Ben, or a model working with him). It records what exists, what
has only been tested in one place, what is known to be broken, and what to build next, with the design decisions
already made so they are not re-litigated. Read section 1 and 2 before changing anything.

## 0. KiCad 10 testing

Original situation (2026-11-14): the dev workspace could not install KiCad 10 (no PPA, Flathub or Docker Hub
access; Ubuntu 24.04 ships 7.0). Two routes were planned:
1. `.github/workflows/tests.yml` runs the suite in `kicad/kicad:10.0` and `9.0` on every push once the repo is on GitHub. If Ben adds the repo to the session, results can be read with `gh run list` / `gh run view --log-failed`. Still not run: the repo is not on GitHub yet.
2. Link the session to Ben's computer (desktop app) and run commands there through the device shell.

**As of 2026-11-22/23 this is moot for direct testing**: whoever is reading this is very likely already running
*on* Ben's machine (Linux Mint, KiCad 10.0.8 Flatpak, Claude Code CLI) - check with `kicad-cli version` and
`git log` before assuming otherwise. `git init` plus the first commit happened 2026-11-22 (there was no git
history before that); `doctor` and `pytest tests -q` are green on real KiCad 10 as of this writing. Route 1 (CI
on GitHub) is still worth setting up for *regression* coverage once the repo has a remote, but is no longer the
only way to get a real KiCad 10 result.

## 1. Ground rules

**How Ben wants to work**
- Blunt, concise, formal-ish. Push back when he is wrong. Never use em dashes in anything he reads.
- Terminal commands in plain code blocks.
- When brainstorming, give directions and questions, not finished ideas, unless he asks for specifics.
- He is a novice at PCB design and says so. The tool's job is to make its output checkable by someone like him.
- Ask before building when a decision is his (data sharing, cost, anything that changes the designer prompt).

**Honesty rules that have mattered**
- Say what was tested and where. This workspace has had KiCad 7 only; Ben runs KiCad 10.0.8 (Flatpak, Linux Mint).
  Every KiCad 10 failure so far came from something validated on 7 only. Label untested paths as untested.
- Add-ons carry a maturity label (ready / beta / preview). Keep it accurate.
- A check finding is "this rule fired", never "the circuit is right". Do not let wording drift.

**Hard constraints**
- Never bill the API through the claude-code backend: `ANTHROPIC_API_KEY` is stripped from its environment. Same
  for `OPENAI_API_KEY` with codex. Keep `CLAUDE_CODE_OAUTH_TOKEN` private.
- Do not automate SnapEDA login or scraping. Do not work around sites that refuse scripted downloads.
- The level setting (guided / standard / expert) must never change the design, only what is shown and asked.
- A correction round must never redesign the board (see `llm._spec_loop`; there is a test).
- Model calls cost Ben money. Prefer code over a model call wherever code can decide.

## 2. Map of the code

```
kipipe/
  pipeline.py    stages in order; Options; Paused (expert gate, preflight); FABS rules
  llm.py         backends, roles, plans (mixed/igor/opus/sonnet/cheap/fable), prompts, _spec_loop (correction guard)
  spec.py        SPEC_DOC (the format the designer writes), normalize, validate
  preflight.py   before the design: questions + planned parts; symbol_index (fast, cached)
  derive.py      outline and stacked connectors taken from another board of the project
  partdb.py      part database: read_datasheet, records, coverage, safe equation evaluator
  docparse.py    Docling path for datasheets (tables, pins, equations), GPU detection
  dsdraw.py      datasheet-style drawing of an application circuit
  schwire.py     wired schematic writer;  schgen.py  label-only fallback and netlist/topology check
  pcbgen.py      runs inside KiCad's Python: footprints, placement call, presets, DSN/SES, cleanup, DRC, layout dump
  placer.py      placement (top side only; locked parts are obstacles), compaction
  libs.py, legacy.py   user library import (vendor zips)
  app.py, cli.py, static/index.html   web app, terminal, single-page UI
  addons/
    core.py        registry, Job (read-only view of a design), runner, plugin loader
    checks.py      rule checks (no datasheet);  partchecks.py  datasheet checks (phase 3)
    gen_docs.py, gen_build.py, gen_mech.py   generators
    blocks.py + blocks/*.json   verified sub-circuits (pins by name, symbol/footprint alternatives)
    projects.py    projects, context, house rules, interfaces, build log, check.project
    guide.py       provenance, review guide, walkthrough, approaches
    aids.py        brief check, starters, requirements check, ask
    usage.py, notify.py, config.py
tests/           fast regression tests and fixtures (python3 -m pytest tests -q, about 10 s, no model calls)
```

Data lives in `~/.local/share/kipipe` (`KIPIPE_HOME` overrides): `jobs/`, `partdb/`, `projects/`, `blocks/`,
`addons/`, `libs/`, `rules.txt`, `config.json`, `usage.jsonl`.

**Testing recipe**
1. `python3 -m pytest tests -q` after every change.
2. A spec-file run exercises everything after the designer and costs nothing:
   `kipipe run --spec tests/fixtures/specs/lt1175_bipolar.json --flags ipc,pour,checks -o /tmp/x`
3. Model paths: test logic with a fake backend (see `tests/test_core.py::_Fake`), then one real run on the cheapest plan.
4. UI: drive it with Playwright and look at the screenshot. Unclicked buttons have been wrong before.
5. `tests/fixtures/partdb/parts/LT1175.json` is a stand-in whose pin numbers were corrected by hand. It is a test
   fixture, not a real datasheet read.

## 3. What is verified where

| Area | KiCad 7 (workspace) | KiCad 10 (Ben) |
|---|---|---|
| Wired schematics, placement, routing, DRC | yes | yes, earlier runs |
| Docling datasheet path, GPU detection | cannot run here | yes (doctor shows GPU; speed not yet timed) |
| Toolbox generators and rule checks | yes | not reported |
| Datasheet checks (phase 3) | yes, 3 records | no |
| Levels, projects, guide, compare, UI rework | yes, headless browser | not reported |
| Preflight (questions, missing parts) | yes | one run: questions and uploads worked |
| Stacked pin numbers `[1,15,38,39]` | hand-made symbol only | hand-made symbol only (2026-11-22; real stacked-pin part still unseen) |
| Relocated symbols (`Device:Q_NMOS_GSD`) | by analogy only | **yes**, 2026-11-22: `Device:Q_NMOS_GSD` resolves to `Transistor_FET:Q_NMOS_GSD` against the real KiCad 10 libraries |
| Correction guard | fake backend | fake backend, real KiCad 10 validation (2026-11-22); logger run 4 reached the model with 0 correction rounds needed, so a real round is still unseen |
| Power-flag global label fix | cannot run ERC here | **fixed 2026-11-22**, see problem 4 below |
| Blocks with pins by name | yes | **yes**, 2026-11-22: `blocks_usb_3v3.json` ran end to end (ERC 0 errors, DRC 0 errors) |
| Derive / stacking, `flip_fp` | yes | **yes**, 2026-11-22: `stack_base.json` + `stack_top_header.json` in one project, mirrored mate under the board, ERC/DRC clean |

**Logger request, run 4 (2026-11-23):** ran end to end through the model (Opus, ~$1.76, 395 s, 29 library lookups
in the design step) and stopped at ERC with 2 errors. Both were "Pins of type Power output and Power output are
connected" between three `LM66100DCK` ideal-diode power-path controllers the model added (to OR the USB, buck and
boost 5 V sources onto one `+5V` net, answering the "what powers 3.3 V from the LiPo alone" preflight question).
That circuit is correct: each ideal diode independently blocks reverse current, so tying their `power_out` pins
together is how a diode-OR is built, not two drivers fighting over a net. KiCad's ERC default treats any two
`power_out` pins on a net as a conflict and does not know the difference. **Fixed 2026-11-23** in `pipeline.erc()`:
a `POWER_OR_PARTS` allow-list (`LM66100`, `LM74610`/`LM74700`, `TPS211x`/`TPS2663`, `AP22814`, `SGM2576`) downgrades
this specific conflict to a warning only when every pin in it belongs to a recognised part, so two real regulators
mistakenly tied together still fail. Covered by `test_orring_power_controllers_do_not_trip_erc` and
`test_two_real_regulators_tied_together_still_fails_erc` in `tests/test_core.py`; verified directly against two
minimal fixtures (not yet against this exact run, which cost real money - see below). Part data also reconfirmed
known problem 1 (ESP32 pin table): `U8 ESP32-WROOM-32E-N4` got "wrong variant" against the `ESP32-WROOM` record.

**Next job for whoever continues: rerun the logger request end to end** now that the ERC fix is in, and read the
log for whatever surfaces next. The run's `spec.json` was not kept (it lived under `/tmp`, which this machine
clears), so confirming the fix against this exact design needs a fresh model call (~$1.76 for the design step
alone going by run 4, plus `inspect`/`review`/`video` if left at their defaults - say $2-3 total). Cheaper option:
drop `inspect,review,video,render3d` for the rerun (`--flags ask,checks,ipc,pour`) to isolate the ERC/DRC result
first, then do one full-flags run once that is clean.

```
Battery-powered environmental logger. Power: USB-C (5 V sink, power only) and a single-cell LiPo on a JST-PH
connector, MCP73831 charger at 500 mA. A TPS54202 buck converter makes 5 V at 1.5 A from a separate 12 V input on
a 2-pin screw terminal; an AMS1117-3.3 makes 3.3 V at 600 mA from 5 V. ESP32-WROOM-32 with reset and boot buttons
and a 4-pin UART header. On I2C: TCA9548A mux with an ADXL345 on channel 0 and a BME280 on channel 1, channels 2
and 3 on 4-pin headers. microSD socket on SPI. Two low-side N-MOSFET outputs for 12 V motors, 1 A each, on screw
terminals on the right edge. Power LED per rail, one user LED. Four M3 mounting holes. Assembled by a fab service.
4 layers.
```

## 4. Known problems, most important first

Each has where to look and what I would try. Two are now fixed (marked below); the rest are not.

0. **FIXED 2026-11-22: KiCad's Flatpak sandbox has no access to `/tmp`.** `flatpak info --show-permissions
   org.kicad.KiCad` shows `filesystems=home;/media;/run/media` only; `TMPDIR=/var/tmp` is set but that is a hint,
   not a grant, and does not help either. Any kicad-cli or pcbnew call on a file under `/tmp` failed with
   "Schematic file does not exist or is not accessible" even though the file was right there. This broke two
   tests (`tests/test_core.py::test_long_wires_become_labels_and_netlist_still_matches`,
   `tests/test_determinism.py::test_same_spec_gives_the_same_board`, both use pytest's `tmp_path` under `/tmp`)
   and would have broken the testing recipe above (`-o /tmp/x`) on Ben's machine specifically, since the workspace
   here has no KiCad 10 Flatpak to hit this. Fixed by adding `--filesystem=/tmp` to the `flatpak run` invocations
   in `kicad_lib.py` (both `cli_cmd` and `python_cmd`). This is a per-invocation override, not a persistent
   `flatpak override`, so it does not touch the user's system-wide Flatpak permissions.

1. **ESP32 module datasheet: 21 of 38 pins read.** The record then counts as another variant and every datasheet
   check skips the part. Look at `docparse.pins_from` with that PDF's Docling JSON: likely a pin table continued
   across pages or two tables side by side. Fix idea: merge consecutive pin tables with the same header; if the
   pin count is well below the symbol's, mark the record "pin table incomplete" and fall back to a model read of
   the pin pages.
2. **No application circuit was read from any of five datasheets** in Ben's last run. `docparse.figure` finds the
   figure by caption ("Typical Application"). Log which captions were seen; widen the match ("Application
   Circuit", "Reference Schematic", "Typical Connection"); for modules there may be none, which is fine but should
   be said.
3. **Pinout from page images returned 0 pins (AMS1117) under the Igor plan.** The `extract` role goes to the local
   model, which may not read images. Route image reads to a model that can (`ClaudeCode.ask(..., tools=["Read"])`)
   regardless of plan, or detect a non-vision local model once and remember it.
4. **FIXED 2026-11-22: "Input Power pin not driven" ERC stop.** Root cause: `schwire.flag_nets()` decided whether
   a net needs a `PWR_FLAG` by looking only at the electrical types of real component pins. It never accounted for
   `place_power()` attaching a library power symbol (`+5V`, `+3V3`, `GND`...) to that net, whose own single pin is
   `power_in` in every KiCad power symbol. A net with only passive/unspecified component pins (a bare connector,
   say) but a power-symbol label therefore got an implicit undriven `power_in` with no flag to satisfy ERC.
   `flag_nets()` now also flags a net when `is_power(net)` is true. Confirmed on KiCad 10:
   `tests/fixtures/specs/stack_base.json` (ERC was 3 errors, now 0) and the LT1175 and blocks fixtures (0 errors).
5. **Local model quality (qwen 9b).** It can no longer wreck a design, but it still wastes rounds. Cheap wins, all
   in code and before any model: when exactly one close footprint match exists, substitute it (like
   `spec._relocated` does for symbols); when a net references a pin number that does not exist but the name is
   unambiguous, fix it. Then fewer errors reach a model at all.
6. **Part data stage wording.** It says "0 of N ICs/modules" while counting diodes and switches the triage moved
   up. Triage is now restricted, but check the count against `partdb.classify` tiers.
7. **Datasheets are read one after another** (about 7 minutes for five). Each read is already parallel inside.
   Run two or three reads at once in `pipeline.read_datasheets`; keep the log readable by prefixing lines.
8. **Preflight symbol matching is by name.** Ordering codes map to the nearest family name and are flagged
   "check the variant". A better match would compare pin count with the datasheet record when one exists.
9. **Placement knows nothing about switching regulators.** A buck converter gets a valid netlist and a poor
   layout. Records have `layout_notes`; turning the common ones (input cap, inductor and diode next to the switch
   pin) into `near` rules in the floorplan would be deterministic.
10. **Occasional single unrouted net** on boards with wide power tracks and through-hole parts. Not investigated.
11. **ESP32-WROOM on a 2-layer board fails DRC** on the footprint's own 0.2 mm thermal vias against the 0.3 mm
    minimum. The log explains it. Whether the fab limits in `pipeline.FABS` are current was not checked: verify
    against the fab's published capabilities before relaxing anything.
12. **UI paths never clicked in a browser:** the Fetch button and per-run datasheet links box, vendor zip upload
    on the preflight page, approaches button, build log, fix-with-Revise button, anything below 1280 px wide.
13. **Compare view** shows both boards at their own scale.
14. **Enclosure heights** come from a footprint-name table (`gen_mech.HEIGHTS`). With STEP models available the
    real height could be read from the model's bounding box.
15. **`check.mech`** treats a module's antenna keep-out as its body; RF modules are simply skipped for overhang.

## 5. What to build next

Order reflects what unblocks Ben, not size.

### 5.1 Phase 2: distributor lookups (blocked on Ben's Mouser API key)
Decisions already made:
- Three tiers. Passives: nothing. MOSFETs, diodes, inductors, crystals, connectors: **API parameters only**.
  ICs and modules: full datasheet. The light model may move a part up a tier, never down.
- The datasheet fetcher by link exists (`partdb.fetch_pdf`). Phase 2 adds lookup by part number, which returns
  parameters, availability and a datasheet link to feed to it.
- If the download is refused, ask the user to supply the file. Never work around the refusal.

Suggested shape: `kipipe/distrib.py` with one function `lookup(mpn) -> {params, stock, price_breaks, datasheet_url,
lifecycle}` and a per-part cache file next to the records; key in `config.json` (`mouser.key`), never logged.
Use it in three places: preflight (does the part exist, is it active, here is its datasheet link), the
parameters-only tier (store the handful of ratings the checks need: Vds, Id, Rds(on), Vf, If, Isat), and the BOM
generator (fill MPN gaps and flag not-recommended-for-new-design parts). Do not print prices in outputs unless
Ben asks; they go stale. I could not test any of this: write it against the published API documentation and say so.

### 5.2 Phase 4: shared part database (needs Ben's answers first)
He chose one file per part so it could live on GitHub. He was asked the questions below and has not answered;
do not build until he does.
- Trust: what makes a record good enough to share, and what happens when two reads disagree?
- Identity: when are two records the same part (variants, packages, datasheet revisions)?
- Contribution: automatic, opt-in per record, or a deliberate act? What stops garbage?
- What is shared: facts are one thing; PDFs and cropped figures belong to the manufacturer.
- Mechanics: whole database or on demand; offline behaviour.
- Incentive: why would anyone contribute?

My recommendation, for him to accept or change:
- Share **facts only**: the record JSON without `source.file`, without page crops, with the datasheet's URL and
  SHA-256 so anyone can fetch the same PDF and re-check.
- A public GitHub repository, one JSON per part under `parts/<first letter>/<MPN>.json`. Contribution is a
  deliberate act: `kipipe parts share <MPN>` opens a pull request (through `gh`, with Ben's or the user's own
  account). Nothing is uploaded automatically, ever.
- Pulling is on demand: when a planned part has no local record, look for `<MPN>.json` in the repository's raw
  files, verify it parses, and store it locally marked `origin: shared`. Offline simply means no lookup.
- Trust is visible, not binary. Each record already carries verification counts. Add `confirmations`: how many
  independent reads (different contributor, same datasheet hash) agree field by field. The UI shows "read once by
  one person" differently from "confirmed by three". Checks quote that status in their findings.
- Two automatic gates before a shared record is used: its pin table must agree with the KiCad symbol by pin
  name (the `part.pinout` logic), and it must come from a datasheet hash the record states. A mismatch means the
  record is shown but not used for checks.
- Disagreements between reads are kept as alternates on the field, not overwritten.
- Poisoning is the real risk (a wrong pinout that looks verified). The gates above, review of pull requests, and
  never treating a shared record as stronger than a local read are the mitigations. Say plainly that this is a
  community database with no warranty.

### 5.3 Closing the loop on findings
The Toolbox has "Fix ticked with Revise" (fills the Revise box; nothing is sent automatically). Next steps, in
order of safety:
- Deterministic fixers for findings that have one right answer: swap a reversed LED or polarised capacitor, add
  the missing 5.1 k CC resistors, add I2C pull-ups, split a shared CC resistor. Each produces a spec patch the
  user sees before it is applied. No model.
- For the rest, one Revise call per batch, capped at one round, only for findings the user ticked.
- Never auto-fix a heuristic finding without the user seeing it: the checks have false alarms by design.

### 5.4 Still on the earlier list
- **Schematic readability check**: code first (wire crossings, label-only nets that could be wires, text
  overlap, rails pointing the wrong way), then the light model only for a verdict on a rendered image.
- **Revise that moves or rotates parts**: map "move U1 to the left edge" to floorplan changes or direct moves
  (`pcbgen` already applies `moves` and locks them).
- **Cheaper final inspection**: skip the model when DRC and all checks are clean and nothing changed since review.
- **Library index for the designer**: `preflight.symbol_index()` already indexes every symbol name in under 5 s
  and caches it. Give the designer the matching names for its planned parts up front (the preflight block does
  this for planned parts) so it stops spending turns grepping library files; the last run spent 19 lookups.
- **Support-part matching**, **schematic bunching of identical parts**, **crowding near constraints**: unchanged.

### 5.5 Ideas raised and not built
- **Back-side placement in general.** The placer is top-side only; `derive` puts mates on the underside as
  locked parts, and the area over them is blocked on both sides. A real two-sided placer is a large change.
- **Stack height and collisions between stacked boards.** Needs part heights (see 4.14) and a standoff height
  in the project.
- **Slots and card-edge connectors** for stacking.
- **Provenance for every design decision**, not just active parts (which value came from a datasheet equation,
  a block, or memory).
- **Lessons from builds.** The build log turns a user-written lesson into a house rule. Anything more automatic
  risks over-generalising from one failure; prefer turning a lesson into a check or a block note.
- **An expert's review.** Ben's coach is a software person; someone who lays out boards for a living should be
  shown the shareable report and asked what the tool gets wrong. No amount of code substitutes for that.

### 5.6 3D build video (built last, partly untested)

`video.py` now defaults to `video_style="3d"`: an orbiting perspective camera, the board as a slab, parts as boxes (heights from `gen_mech.part_height`, colours by ref prefix) that drop in as they are placed, then the routing replay on the 3D board. `make()` falls back to the flat style if the 3D renderer throws. CLI: `--video-style 3d|flat`, `--turntable N`.

- Tested here (KiCad 7) on the ESP32 and 555 runs: renders fine, ~15 s per video.
- NOT tested: the closing turntable `kicad_turntable()`, which calls `kicad-cli pcb render` N times (KiCad 9+, 4 in parallel). Check on Ben's KiCad 10: the `--rotate "-55,0,a"` sign and axis order, `--zoom`, whether `--quality basic` is fast enough (36 renders), and flatpak with parallel calls. If it fails it logs and ends on the drawn board instead.
- Known look issues: boxes use the courtyard (so an ESP32 module's antenna keepout makes it overhang the board); no bottom-side parts.

### 5.7 Feeding the model examples of good boards (Ben asked; recommendation)

Worth doing, but aimed at the right stage. The model writes the spec (parts and connections) and the floorplan; code does placement and Freerouting does routing. So:
1. Spec examples help most. Build a small curated library (20 to 50) of known-good designs converted to the kipipe spec format, tagged by function (USB power, LiPo charger, ESP32 core, RF front end). Retrieve 1 or 2 by similarity to the prompt, never dump them all (token cost, and the model copies wholesale). The blocks library is already the reliable version of this for sub-circuits.
2. Layout examples should not go to the model as pictures. Extract measurable rules from good boards (decoupling cap distance to its pin, crystal distance, connector-at-edge, loop area of switchers) and add them to the placer's cost function and to the checks.
3. Sources: open hardware KiCad projects with clear licences (CERN-OHL, CC-BY-SA such as Adafruit and SparkFun). Keep licence and attribution in each example record.
4. Measure it: fixed prompt set (the tests fixtures plus the logger prompt), run with and without examples, compare check findings, ERC/DRC and correction rounds. Adopt only if it wins.

### 5.8 Build video with real part models (built, tested here with 6 downloaded STEP files)

`meshvideo.py`: reads each footprint's `(model ...)` from the finished .kicad_pcb, finds the .step next to the named .wrl in the KiCad 3D library (`model_dirs()`: KICADn_3DMODEL_DIR, next to the footprint libs, flatpak runtimes, KIPIPE_MODEL_DIR), loads it with trimesh + cascadio, caches it as .glb in ~/.cache/kipipe/meshes, and renders with pyrender (EGL by default; PYOPENGL_PLATFORM=osmesa for software). Parts without a model become boxes. Falls back to the box renderer if anything is missing. `--no-models` turns it off.
- Ben must install: `pip install pyrender cascadio shapely mapbox-earcut "PyOpenGL>=3.1.7"` (pyrender pins PyOpenGL 3.1.0, which breaks; override it). Also missing on his venv as of 2026-11-23: plain `numpy` (the fallback box-style video still rendered fine without it; only the real-models path needs it, same as the rest of this list). None of these are installed yet, so the real-STEP-model path still has not run on Ben's machine.
- Untested: Ben's flatpak 3D library path, bottom-side parts (`_R("y",180)` flip), model `rotate` sign (KiCad applies negative angles; most library models have 0), EGL on his AMD card. Colours from some STEP files come out pale.
- Speed here (CPU, llvmpipe): 32 s for a 15-part board. First run per model pays the STEP parse (seconds each), then cached.
- Next: hide the kicad-cli turntable when models were used (it is only used by the box style now); silkscreen texture on the board top from pcb_top.svg.

### 5.9 Schematic readability (Ben's list; partly built)

Built: connections longer than `schematic.max_wire_mm` (default 50) become net labels at both ends instead of a wire across the sheet; every separate wire piece of a label-joined net gets its own label (this also fixed a latent case where a failed route left a wired piece unnamed); MPN and datasheet fields on symbols when the spec has them; `mpn` in SPEC_DOC; `check.readable` (unnamed nets on connector pins, parts with no value, ICs with no part number). Tests cover the netlist staying identical.
Not built, in order of value:
1. Copy the mpn the pre-design step already finds for each planned part into the spec components (preflight.plan -> apply_preflight), so most parts get part numbers for free.
2. Function regions on the sheet: group parts by the spec floorplan/groups (power, MCU, I/O), place each group in its own area with a titled dashed box. Cheaper than true hierarchical sheets and keeps one netlist.
3. Flow: input connectors left, outputs right, power symbols up and ground down (already), in the placement cost of schwire.place().
4. Real hierarchical sheets (one per function) once 2 works: needs sheet symbols, hierarchical labels and per-sheet instances; netlist check must handle sheet paths.
5. Connector pin names: when a connector symbol has generic names (Pin_1), write the net name next to each pin (a text item), or pick a symbol with named pins.

### 5.10 Roadmap from Ben's LinkedIn survey (Nov 22; not started)

In priority order. Reasons are in the chat; short version here.
1. DONE Nov 22 (calc.py, spec and route caches, seeded UUIDs, tests/test_determinism.py). Still open: show each component's `calc` note in the guide, report and design doc; cache revise_spec too; more fab stackup presets; the I2C default assumes a 150 pF bus. Finding: Freerouting 2.1 gives a different routing for the identical DSN on every run, even with one optimizer thread and -XX:hashCode=3, so the routing cache is a workaround; Router v2 should be deterministic by construction. Original plan: The model picks parts and topology; code computes every number (LED resistor, divider, regulator feedback, I2C pull-up, trace width for current, impedance/stackup from fab presets such as JLCPCB's published stackups). Cache: same prompt + answers + model -> same spec (hash key), so reruns are identical unless asked.
2. MCP server: expose checks, blocks, calculators, stackup, footprint lookup and the pipeline as MCP tools so any assistant (Claude Code, Codex, local) can call them. The add-on registry already has the shape.
3. Benchmark harness: run kipipe on public sets and publish scores. HWE-Bench (requirements -> schematic) fits the front half; PCBWorld D3 boards (KiCad 9 format) fit routing. Check each licence first.
4. Router v2 (PCBRouteAgent pattern): model plans net order, corridors and layers once; KiCad's own push-and-shove router executes through kicad-python / IPC API (KiCad 9+); structured failure packets go back for a bounded number of replans; accepted routes are kept. Code: github.com/chip-ai-agent/PCBRouteAgent, BSD-3-Clause (Ben checked), so code can be reused with the copyright notice kept. Freerouting stays as the fallback. Add explicit diff-pair and length constraints (their USB failure).
5. Design document on demand: theory of operation per block, every calculation with its inputs, decisions with provenance, what it is least sure about, test plan, BOM. Mostly assembles gen.report, gen.bringup, guide and provenance.
6. Firmware: pin map generated from the netlist (no transcription errors), driver lookup per mpn from the PlatformIO registry / GitHub, a bring-up sketch per part, export to Wokwi diagram.json or a Renode platform for simulated runs.
7. Enclosure interference checks with the STEP meshes meshvideo already loads (trimesh collision), including a deliberate 1 mm offset to prove the check fires.
8. On-the-spot sims: SPICE per block (exists in part), thermal estimate, openEMS for antennas later.

## 6. Prompt changes made this round (watch for side effects)

The designer's spec format gained optional sections: `blocks`, `assumptions`, `requirements`, `rails`, `derive`.
Each was tested with one or two real Sonnet runs and never with Opus or the local model as designer. If spec
quality drops or correction rounds rise, these are the first suspects; each can be removed from `SPEC_DOC`
independently, and the code tolerates their absence.

## 7. Costs seen

- Preflight: about $0.003 and 25 s per run (Haiku).
- Walkthrough, ask, approaches: about $0.002 to $0.004 each.
- One Opus design of the logger: about $1.30, plus $1.20 to $2.50 per Opus correction round. Correction rounds are
  where the money goes; every library error fixed in code saves one. Run 4 (2026-11-23, on Ben's machine): $1.756,
  395 s, 29 library lookups, 0 correction rounds - the design was valid first try, it just failed ERC afterward.
- Fable: a one-word test call cost about $0.20. The `fable` plan was never run end to end.

## 8. Context from the planning chat (Nov 14 to 22) not recorded elsewhere

**First steps on Ben's machine**
- There is no git history yet. Run `git init` and commit the unpacked tree before changing anything, so every later fix is a reviewable diff.
- Then: `python3 -m kipipe.cli doctor`, `python3 -m pytest tests -q` (written and passing on KiCad 7 only), the logger prompt (section 3), and the "needs a KiCad 10 run" rows in FEATURES.md.
- 3D video with real models needs `pip install pyrender cascadio shapely mapbox-earcut "PyOpenGL>=3.1.7"`; the KiCad 3D library is probably inside the Flatpak runtime (see `meshvideo.model_dirs`).

**Ben's machine**: Linux Mint, KiCad 10.0.8 Flatpak, Ryzen 7 9700X, Radeon RX 9070 XT with ROCm 7.2 (doctor shows Docling on the GPU; datasheet read speed not yet timed). Claude Pro subscription, used through the claude-code backend.

**Who it is for**: beginners and intermediates first; experts get speed on large projects and less explanation (the level setting), with the full detail always reachable.

**Open source**: Ben will release it. No licence chosen yet. Freerouting is GPL-3 but runs as a separate process; PCBRouteAgent code is BSD-3 (keep its notice if copied). Check any benchmark data licence before shipping or training on it.

**Positioning Ben agreed with** (competitors: Flux, CopperPilot, Resin, BoardRepo): native KiCad files, local-first, works with any model including local ones, same input gives the same board, shows its work and says what it is unsure about. Publish benchmark scores (HWE-Bench, PCBWorld). An MCP server is the next build (roadmap 5.10 item 2).

**FPGA (discussed, not built)**: small FPGAs are in reach: Lattice iCE40 (UP5K QFN-48, HX TQFP-144), Gowin GW1N LQFP, MachXO2 TQFP. Needs a block per family (rails, SPI config flash, oscillator, config pull-ups), a `check.fpga` (bank VCCIO against attached parts, CRESET/CDONE, PLL supply filter, power order) and datasheet records. Best feature: `.pcf` / `.xdc` pin constraints generated from the netlist like `gen.firmware`, later pin swapping within a bank to untangle routing. BGA FPGAs wait for Router v2 plus a fanout generator. iCEBreaker and UPduino are open designs to use as references and tests.

**Waiting on Ben**: Mouser API key (5.1), answers to the shared-database questions (5.2), the LT1175 read time on the GPU, whether to spend ~$2-3 on a full clean logger rerun now that the ERC fix is in (section 3).

**Writing for Ben** (docs, logs, UI text): no em dashes; plain words over stiff ones; no filler openers or closers; sentence-case headings; minimal bold; concrete facts over praise.
