# kipipe feature list

What exists, what state it is in, and what is only planned. Keep this file in sync: `tests/test_features.py`
fails when an add-on, block, CLI command or pipeline flag exists in the code but is not named here.

Status words:
- **works**: used in real runs on KiCad 10 (Ben's machine)
- **built**: runs in the tests or on KiCad 7 here; not yet confirmed on KiCad 10
- **beta / preview**: runs, but known gaps (preview = a starting point you finish by hand)
- **planned**: not written yet (HANDOFF.md section in brackets)

## 1. The pipeline (request to board)

| Feature | Status | Notes |
|---|---|---|
| Text or image request to a design spec (model) | works | `kipipe run`, web app |
| Pre-design step: questions, planned parts, missing datasheet/symbol/footprint/STEP | works | one KiCad 10 run; all levels |
| Spec validation against your installed KiCad libraries | works | |
| Correction loop with guard (rejects fixes that shrink the design) | built | not seen on KiCad 10 yet |
| Relocated symbols, KiCad 10 stacked pin numbers | built | **needs a KiCad 10 run** |
| Wired schematic generator (symbols placed, wires routed, labels) | works | |
| Long wires drawn as net labels (`schematic.max_wire_mm`, default 50) | built | |
| MPN and datasheet fields on symbols | built | only when the spec has them |
| Datasheet-style drawing of application circuits | works | `dsdraw.py` |
| Netlist cross-check (schematic matches spec) | works | |
| ERC with explained messages | works | power-flag fix **needs a KiCad 10 run** |
| Board outline shapes (rect, rounded, circle, plus, hex, octagon, polygon) | works | |
| Placer (floorplan regions, edge connectors, annealing) | works | |
| Floorplan review by the model (flag `review`) | works | 1 model call |
| Freerouting autorouting | works | `kipipe fetch-router` |
| IPC-2221 trace widths, auto-widen power nets (flag `ipc`) | works | `physics.py` |
| Controlled-impedance RF widths from stackup (flag `rf`) | works | closed-form microstrip only |
| Ground pour (flag `pour`) | works | |
| DRC with explained messages | works | flag `force` exports fab files despite ERC/DRC errors |
| Visual inspection in three passes (flag `inspect`) | works | 3 model calls |
| openEMS S-parameters via gerber2ems (flag `openems`) | beta | slow, off by default |
| KiCad 3D render still (flag `render3d`) | works | KiCad 9+ |
| Design checks during the run (flag `checks`) | works | |
| Verified blocks expanded into the design (flag `blocks`) | built | off by default; **needs a KiCad 10 run** |
| Questions before designing (flag `ask`) | works | |
| Expert review gate (pause before layout) | built | Expert level |
| Reroute after moving parts | works | `kipipe reroute` |
| Revise a finished design from a description | works | `kipipe revise` |
| Derive a board from another in the project (same outline, stacking connectors) | built | mirrored mate tested; **needs a KiCad 10 run** |

## 1b. Same input, same board

| Feature | Status | Notes |
|---|---|---|
| Calculators: the model states the goal, code computes the value (`calc` entries) | built | LED resistor, divider, regulator feedback, I2C pull-up, crystal caps, RC filter; E12/E24/E96 |
| Design cache: an identical request reuses its validated design | built | key = model + full prompt + images; `--fresh` asks again |
| Routing cache: an identical board reuses its routing | built | Freerouting differs run to run even single-threaded (measured), so the first result is kept |
| Fixed UUIDs in schematic and board files | built | byte-identical files on reruns, clean git diffs |
| Cache management | built | `kipipe cache` (show / clear) |
| Proof test: one spec run twice gives byte-identical .kicad_pcb and .kicad_sch | built | `tests/test_determinism.py` |
| CI on KiCad 10 and 9 (GitHub Actions in KiCad's Docker images) | built | `.github/workflows/tests.yml`; runs once the repo is on GitHub, **not run yet** |
| Fab stackup presets for impedance (JLC 7628 4-layer, 2-layer FR4) | works | `physics.py`; more fabs planned |

## 2. Parts and libraries

| Feature | Status | Notes |
|---|---|---|
| Part database: datasheets read once, facts reused | works | `kipipe parts` |
| Datasheet reading with Docling on GPU | works | speed not yet timed |
| Datasheet download from a link | built | `kipipe parts add <url>` and per-run links |
| Vendor library import (SnapEDA, Ultra Librarian, SamacSys, DigiKey) | works | `kipipe lib` |
| KiCad 5 legacy library conversion | works | `legacy.py` |
| Distributor lookup (stock, price, alternates) | planned | [5.1] needs a Mouser API key |
| Shared part database between users | planned | [5.2] needs Ben's answers |

## 3. Checks (Toolbox)

Rule checks, no datasheet needed: `check.nets`, `check.decoupling`, `check.i2c`, `check.leds`, `check.polarity`,
`check.crystal`, `check.usb`, `check.mcu`, `check.mech`, `check.handsolder`, `check.buttons` (beta),
`check.readable`, `check.requirements` (beta), `check.project` (beta), `check.provenance`. All **built**, tested here.

Datasheet checks, need a part record: `part.pinout`, `part.ratings`, `part.support`, `part.equations`,
`part.rails`. All **built**; tested on 3 records (ADXL345, LT1175, TCA9548A).

Run them alone: `kipipe check <job or spec.json>`.

## 4. Generators (outputs)

| Add-on | What | Status |
|---|---|---|
| `gen.bom` | BOM and placement files for assembly houses | built |
| `gen.fabsheet` | Order sheet numbers | built |
| `gen.bringup` | First power-on checklist | built |
| `gen.pinout` | Printable connector card | built |
| `gen.readme` | GitHub README with pictures | built |
| `gen.report` | One-file HTML report | built |
| `gen.diff` | Compare two runs | built |
| `gen.assembly` | Interactive hand-assembly page | built |
| `gen.firmware` | pins.h / pins.py from the netlist | built |
| `gen.testpoints` | Test point suggestions | beta |
| `gen.spice` | SPICE netlist starting point | preview |
| `gen.powertree` | Power tree diagram | beta |
| `gen.enclosure` | OpenSCAD two-part case | beta |
| `gen.step` | Assembled board as STEP | built |
| `gen.docpack` | PDF drawings | built |
| `gen.panel` | Panel plan | preview |
| `gen.guide` | Review guide (what to check, by level) | built |

## 5. Workflow and learning aids

| Feature | Status | Notes |
|---|---|---|
| Levels: Guided, Standard, Expert | built | changes questions, explanation depth, review gate |
| Projects: shared context, other boards, connector interfaces | built | `kipipe project` |
| House rules applied to every design | built | `kipipe rules` |
| Optional free-text context per run | built | |
| Building blocks library (9 built in, save your own) | built | `kipipe block`; `aid.saveblock` (beta) |
| `aid.ask` ask about this board | beta | |
| `aid.explain` walk me through this board | beta | depth by level |
| `aid.interface` make a connector a project interface | beta | |
| `aid.buildlog` build log | built | |
| `aid.notify` desktop / webhook / ntfy when runs finish | preview | |
| Compare approaches before designing | built | web app |
| Starters and request brief chips | built | web app |
| Fix ticked findings with Revise | built | web app |
| Findings fed back automatically as edit prompts | planned | [5.3] |

## 6. Models and cost

| Feature | Status | Notes |
|---|---|---|
| Claude Code backend (Pro subscription, no API billing) | works | |
| Anthropic API, Codex, OpenAI API, Ollama backends | built | Ollama used in `igor` plan runs |
| Model plans: mixed, opus, sonnet, cheap, fable, igor | works | per-step model choice |
| Usage ledger and optional budget limit | built | `kipipe usage`, `kipipe config` |

## 7. Video and 3D

| Feature | Status | Notes |
|---|---|---|
| Build video (flag `video`), flat style | works | `--video-style flat` |
| Build video, 3D with real STEP models | built | needs `pip install pyrender cascadio shapely mapbox-earcut "PyOpenGL>=3.1.7"`; **needs a run on Ben's machine** |
| Build video, 3D boxes (fallback) | built | `--no-models` |
| KiCad turntable at the end of the box video | built | KiCad 9+, **untested** |

## 8. Interfaces

- Web app (`kipipe serve`): request panel, grouped tabs (Circuit, Board, Understand, Toolbox), stage dots,
  preflight page, guide, compare runs, fetch by link. **works / built**
- CLI: `serve`, `run`, `reroute`, `revise`, `lib`, `parts`, `check`, `addon`, `block`, `project`, `rules`,
  `usage`, `config`, `cache`, `doctor`, `fetch-router`.
- Your own add-ons: drop a `.py` in `~/.local/share/kipipe/addons/`.
- Tests: `python3 -m pytest tests -q`.

## 9. Planned (not built)

From HANDOFF.md, in Ben's current priority order:
1. Determinism follow-ups: calc results in the guide/report, cache for revisions, more fab stackups, Router v2 removes the Freerouting cache workaround [5.10.1]
2. MCP server exposing checks, blocks, calculators and the pipeline [5.10.2]
3. Benchmark harness (HWE-Bench, PCBWorld) [5.10.3]
4. Router v2: model plans, KiCad push-and-shove executes, replan from failures (PCBRouteAgent, BSD-3) [5.10.4]
5. Design document on demand [5.10.5]
6. Firmware: driver lookup, bring-up sketches, Wokwi / Renode export [5.10.6]
7. Enclosure interference checks with STEP meshes [5.10.7]
8. Quick sims per block, thermal [5.10.8]
9. Schematic: part numbers from preflight, function regions, left-to-right flow, hierarchical sheets, connector pin names [5.9]
10. Examples of good designs retrieved per request [5.7]
