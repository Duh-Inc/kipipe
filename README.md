<img src="kipipe/static/logo.svg" width="56" alt="">

# kipipe

Text or image in, routed and checked KiCad board out.

```
prompt / sketch ──► model ──► design spec (JSON) ──► validate against your KiCad libs ──┐
                      ▲                                                                   │ errors
                      └───────────────────────────────────────────────────────────────────┘
spec ──► .kicad_sch (wired) ──► kicad-cli netlist ──► cross-check vs spec ──► ERC
     ──► net classes + fab rules (.kicad_pro) ──► place ──► Freerouting ──► ground pour
     ──► DRC + schematic parity ──► IPC-2221 check ──► Gerbers/drill/pos ──► [openEMS]
```

The model never writes KiCad files. It writes a small JSON spec; every symbol,
footprint and pin number is checked against the libraries installed on your
machine, and errors (with close-match suggestions and the real pin list) go
back to the model until it is valid. Everything after that is deterministic.

## Install

Needs KiCad 8+ (10 recommended), Java 21+, Python 3.10+. Native (apt) and
Flatpak (`org.kicad.KiCad`) KiCad both work. With Flatpak, `kicad-cli` and the
pcbnew steps run inside the sandbox via `flatpak run`, and the library
directories are located on the host automatically (override with
`KIPIPE_SYMBOL_DIR` / `KIPIPE_FOOTPRINT_DIR`). The sandbox must be able to read
your home directory; if `kipipe doctor` reports a pcbnew permission error:

```bash
flatpak override --user --filesystem=home org.kicad.KiCad
```

```bash
./install.sh
kipipe doctor
kipipe serve --open        # web UI on http://127.0.0.1:8765
```

## Model backends

| backend | auth | notes |
|---|---|---|
| `claude-code` (default, Opus) | your Claude Pro/Max login via Claude Code | runs `claude -p` headless; it can grep your KiCad libraries for exact pin numbers. Usage counts against your plan. `ANTHROPIC_API_KEY` is stripped so it never bills the API. |
| `anthropic-api` | `ANTHROPIC_API_KEY` | pay per token; Pro does not include API access |
| `codex` | your ChatGPT Plus/Pro login via the Codex CLI | runs `codex exec` read-only; it can grep your KiCad libraries. Usage counts against your plan; `OPENAI_API_KEY` is stripped so it never bills the API. Uses Codex's default model unless you name one |
| `openai-api` | `OPENAI_API_KEY` | Responses API, pay per token, or the monthly API credits ChatGPT Pro includes (claimed for a linked API organisation). Plan tiers pick your account's newest big and mini/nano models; `OPENAI_BASE_URL` points it at any compatible server |
| `ollama` | none | local vision model, e.g. `qwen2.5vl:7b`; host via `KIPIPE_OLLAMA_HOST` |

Skip the model entirely with `--spec design.json` or `--sch existing.kicad_sch`.

### Which model does what

Only these steps call a model; schematic layout, placement and routing are deterministic code.

| step (role) | `mixed` (default) | why |
|---|---|---|
| design the circuit (`spec`) | Opus | decides everything downstream |
| fix validation errors (`fix`) | Sonnet | mechanical: wrong pin numbers, missing nets |
| fix rounds 3+ (`escalate`) | Opus | Sonnet failed twice, so it is not mechanical |
| placement review (`review`) | Sonnet | one image, a short checklist |
| inspection detail tiles (`inspect_detail`) | Haiku | up to 16 images: the bulk of the tokens |
| inspection larger tiles (`inspect_blocks`) | Sonnet | confirms or drops pass-1 findings |
| inspection whole board (`inspect_final`) | Opus | verdict and part moves, told to discard wrong earlier findings |

Other plans: `opus`, `sonnet`, `cheap` (Sonnet + Haiku), `fable` (Fable, the tier above Opus, designs the circuit and gives the final verdict; billed per token on most plans; falls back to Opus if your account cannot use it). Override single steps with `role=model`:

```bash
kipipe run -p "..." --models mixed,review=opus      # plan plus overrides
kipipe run -p "..." --models inspect=sonnet         # all three inspection passes
kipipe run -p "..." --model opus                    # one model for everything
```

With the Ollama backend, its one local model does every step.

### Igor: Opus thinks, your GPU does the grunt work

`--models igor` (or **Igor** in the web UI) keeps Claude for the judgment calls and sends the mechanical steps to a
local Ollama model: fixing validation errors (it gets the whole spec plus the errors) and the first inspection pass
over the detail tiles. Default local model `qwen3.5:9b` (`--local-model`, or the **Local model** field); thinking is
off and the context is raised to 16k (32k with images). Any step can be sent local with `role=local` or
`role=ollama:<tag>`, e.g. `--models mixed,review=ollama:gemma4:12b`. If the local model fails or Ollama isn't
reachable, that step goes to Claude (Sonnet) for the rest of the run, and the log says so. kipipe still validates
everything against your libraries, so a weaker helper costs extra rounds, not a broken board.

## Terminal use

```bash
kipipe run -p "5V USB-C to 3.3V, ESP32-C3 module, I2C header, 1A rail" --layers 2 --fab jlcpcb
kipipe run -i sketch.jpg -p "transcribe this, add decoupling" --add openems
kipipe run --spec examples/lpf_2m.json --flags rf,pour,openems
kipipe run --sch myboard.kicad_sch --pcb myboard.kicad_pcb     # hybrid, see below
```

## Floorplan: layout intent the placer must obey

The model (or you) fills a `floorplan` section in the spec; the placer treats it as hard constraints:

```json
"floorplan": {
  "grids":    [{"refs": ["D1", "...", "D64"], "cols": 8, "pitch_mm": 5}],
  "regions":  [{"refs": ["U2", "C1", "C2"], "area": "top-left"}],
  "near":     [{"ref": "Y1", "to": "U1.9", "max_mm": 3}],
  "keepouts": [{"ref": "L1", "mm": 4}]
}
```

Automatic rules, no spec needed:

- **matrices** (N identical 2-pin parts on R row nets x C column nets) are detected and placed as a rigid grid
  (default pitch: part size + 1.6 mm, i.e. 5 mm for 0805 LEDs)
- **decoupling caps** (<= 1 uF, supply to ground) are pinned to the IC's actual power pin (from the symbol), 4 mm
  (6 mm on modules), and placed straight after their IC
- **antenna modules** (`RF_Module:*`, ESP32/nRF/LoRa footprints) go to an edge (top by default) with the antenna
  overhanging, and nothing is placed beside the antenna along that edge
- **many-pin parts** get 0.8 mm extra clearance for routing escape
- a grid whose members all share an `edge` is placed on that edge as a block (a row of buttons)

If a constraint can't be met, the part is placed without it and the Placement stage says so.

### Review (default on)

After placement the model gets a render of the board plus the floorplan and is asked whether it matches the
request. If it answers `revise`, its revised edges/floorplan are validated and the board is re-placed once.
Costs one model call (with an image); turn off with `--flags ipc,rf,pour`.

## Inspection (default on)

After routing, the model inspects the board coarse-to-fine in one conversation: ~18 mm detail tiles, then larger
tiles (boards over 40 mm), then the whole board. Images are drawn from the board data (top copper red, bottom
blue, pads, vias, refs, mm grid; the ground pour is left out so tracks stay visible). It also gets the DRC and
unrouted results. The **Review** tab lists findings by severity; clicking one shows the tile it is in. If the
final pass suggests part moves, **apply suggested moves** reroutes with them (terminal:
`kipipe reroute <job> --moves <job>/out/inspect/moves.json`). Costs 2-3 model calls with images; disable by
leaving `inspect` out of `--flags`.

Each finding gets an id; later passes can dismiss earlier ones (e.g. a short Haiku misread on a tile). Dismissed
findings are struck out at the end of the list with the reason, and don't count toward the verdict.

## Reasoning tab

The model writes a `rationale` with the design: how the circuit works (with the calculations), why each
non-trivial part and value, and the layout plan. The Reasoning tab shows it next to the constraints the placer was
given, the rules the placer applied on its own (decoupling, grids, antenna edges), each placement-review round, and
which model did each step. It is the model's written explanation, not a transcript of its internal reasoning.

## Board shape

Rectangle by default. **Shape** in the form (or `--shape`): rounded, circle, plus, hexagon, octagon; the model also
picks one if the description asks ("a round 50 mm board"), and can give any polygon in the spec
(`"shape": {"polygon": [[x, y], ...]}`). Width/height are the bounding box (`--size 50x50`); left blank, the shape
grows until the parts fit. Parts stay inside the shape with the usual edge clearance, mounting holes go where they fit
(around a circle, at the ends of a plus), connectors marked for an edge go against that side of the outline
(edge-mount ones may overhang), and the edge keepout follows the shape. Circles are drawn as true circles.

**Any other outline:** draw it on Edge.Cuts in KiCad (or import a DXF there), save, and drop the `.kicad_pcb` under
Files. kipipe reads the real outline and places everything inside it. Footprints already on that board stay as obstacles.

## Datasheets: the part database

**From a link.** Instead of a file you can give a direct link to the PDF: paste it in the Datasheets panel, put
links in the "Datasheet links for this run" box under Files, or `kipipe parts add https://.../part.pdf`. The
download follows redirects and, if the link is a web page with exactly one PDF on it, that PDF. A site that wants
a login or refuses scripted downloads is reported as such; download the file in your browser and add it by hand.
Nothing looks up a part number for you yet (that is the distributor step).

Models misremember datasheets: a pin's alternate function, an absolute maximum, a required pull-up. kipipe reads
a datasheet once, stores the facts, and every later design uses the stored facts instead.

**With Docling (recommended, optional):** [Docling](https://github.com/docling-project/docling) is IBM's open-source
PDF parser. It finds every table, caption, formula and figure with its page and position, and kipipe turns that
into the record with plain code: pin tables (a number column per package), absolute maximum, recommended operating,
thermal and timing tables, the "unless otherwise noted" condition of each table, and design equations (decoded
from LaTeX, every symbol checked against the datasheet's own tables, OCR slips like "|" for "I" corrected and
marked). Those items are tagged **parsed** and come out the same on every run. A model only sorts out the
electrical characteristics table's merged rows (it gets the parsed grid plus a crop of the table, and any number
it returns that isn't in the grid is dropped) and reads the application circuit drawing (a crop of the figure
found by its caption). Install it once (it pulls in PyTorch, a couple of GB):

    python3 -m venv ~/.local/share/kipipe/docling
    ~/.local/share/kipipe/docling/bin/pip install docling

kipipe also finds `docling` on PATH or at `$KIPIPE_DOCLING`. `kipipe doctor` says whether Docling runs on the
GPU: it checks for an AMD (ROCm) or NVIDIA card and whether Docling's own PyTorch is a GPU build that sees it, and
prints the install command if the card is there but unused. Parses are cached, so this only speeds up the first
read of each datasheet. The first run downloads its models; a 40-page
datasheet takes about a minute on CPU, and the result is cached. Without Docling, the steps below read everything
with the model.

**Adding one:** drop the PDF on **Datasheets** in the sidebar (or with a run's files, or
`kipipe parts add datasheet.pdf`). The read takes a few minutes:

1. The text is pulled out page by page and only the pages that matter go to the model: pin tables, ratings
   tables, typical application and design procedure (not package drawings, tape-and-reel or legal pages).
2. Two independent reads, in parallel: the first by your local model if the plan is **Igor** (otherwise Haiku), the
   second by Haiku. Each covers three sections: pins, ratings, application (support parts, typical circuit,
   design equations).
   Before them, a cheap pass (Igor or Haiku) reads every passage in the whole document that sets a rule for
   the rest: "Note to Reader" sign conventions (a negative regulator written as positive numbers), table-wide
   conditions ("TA = 25°C unless otherwise noted"), which package figure pin numbers refer to. Every read gets
   those rules, the record stores them, and the design prompt shows them above the part's numbers.
3. The reads are compared item by item, with units normalised (50 uA = 0.05 mA) and equations compared by
   computing them. Only items where the reads disagree, or that one read missed, go to Sonnet to settle.
4. The record is stored with every item marked **agreed**, **settled** or **unchecked**, plus the page it came from.

**What a record holds:** identity (part number, ordering variants, revision), every pin with its numbers per package,
type and alternate functions, absolute max / recommended / key electrical ratings with their conditions, the support
parts the part needs and between which pins, the typical application circuit in kipipe's spec format, design
equations in a form that can be computed (`VOUT = VREF * (1 + R1 / R2)`, every variable defined), layout notes,
and free-form notes for the rest.

**How it's used:** the design prompt lists the stored parts, and gives the full facts for any part the request names.
After the spec is made, the **Part data** stage sorts the parts into tiers and reports which ICs had records:

- **skip:** resistors, capacitors and other plain passives
- **api:** discretes (MOSFETs, diodes, inductors, crystals, connectors); their few key numbers will come from
  distributor data (not built yet)
- **datasheet:** ICs and modules, always

A light model may move an unusual part up a tier (a Kelvin current-sense resistor, say); nothing moves a part down.
An IC without a record is flagged amber, because it was designed from model memory.

**Storage:** `~/.local/share/kipipe/partdb/parts/<MPN>.json` (one file per part), the PDFs next to them, and a
search index rebuilt from the files. **CSV** downloads the core as spreadsheets (parts, pins, ratings, support parts,
equations).

Terminal: `kipipe parts add|list|show|rm|export|reindex`. `--models igor` makes the first read local.

## Your own parts (SnapEDA, Ultra Librarian, SamacSys, DigiKey)

Open **My parts** in the sidebar, type a part number and press **Find**: it opens SnapEDA and gives links to the
other sites. Download the **KiCad** format (V6 or later if offered; older files work too) and drop the zip on the
box. kipipe unpacks it into its own library, `kipipe_parts` (in `~/.local/share/kipipe/libs/`):

- symbols merge into `kipipe_parts.kicad_sym`, footprints into `kipipe_parts.pretty/`, STEP/WRL models into
  `kipipe_parts.3dshapes/`;
- each symbol's footprint field is pointed at the footprint from the same zip, and each footprint's 3D model at
  the copied STEP file, so the 3D view shows the real part;
- legacy `.lib` / `.mod` files are converted (with kicad-cli where it can, otherwise a built-in converter that
  keeps pins, pads, fields and simple graphics).

The model gets every imported part with its pins in its instructions and uses them when they fit, so you can say
"use the TPS61088 I imported". Jobs that use them get a `sym-lib-table` / `fp-lib-table` so the project opens in
KiCad with the parts found. Remove a part with the × next to it.

Terminal: `kipipe lib add part.zip [more.zip ...]`, `kipipe lib list`, `kipipe lib rm TPS61088RHLR`.

SnapEDA has no public API and needs a login, so kipipe can't download for you; the download is one click on their
site. Libraries you've already added in KiCad's own library manager (global tables) are used as they are.

## Revising a design

On a finished job, type a change into **Revise this design** ("make the LED blue, add a power switch on the left
edge, use 1206 resistors"). The model gets the current design and your request and returns an updated one, which goes
through the same validation, schematic, checks and routing. With **Keep unchanged parts where they are** (default),
every part with the same reference and footprint stays exactly where it was and only new or changed parts are placed;
if they don't fit, the board is placed again from scratch. Each revision is a new job (`<job>-r1`, `-r2`, ...) and the
Reasoning tab lists what changed. Terminal: `kipipe revise <job> -p "what to change"` (`--replace` to re-place all).

## Moving parts after a run

**In the web UI:** open the **Edit layout** tab on a finished job. Drag parts (0.25 mm snap, Shift for free),
`R` rotates the selected part, Ctrl+Z undoes, the wheel zooms. Overlaps and parts outside the board turn red.
Then:

- **reroute, keep others**: moved parts are locked, nothing else moves, the board is routed again
- **reroute, re-place others**: moved parts are locked, every unlocked part is placed again around them

Each edit runs as a new revision (`<job>-e1`, `-e2`, ...); the original stays untouched.

**After moving parts in KiCad:** save the board in the job folder, then

```bash
kipipe reroute ~/.local/share/kipipe/jobs/<job>            # route again as is (unlocked tracks are cleared)
kipipe reroute <job> --replace                              # also re-place everything you didn't lock
kipipe reroute <job> --moves moves.json                     # {"U1": {"x": 12.5, "y": 8, "rot": 90}} board mm
```

Lock hand-routed tracks in KiCad if you want them kept.

## Hybrid mode (large boards)

Place the parts that matter in KiCad, **lock them** (L), hand-route critical
nets and lock those tracks, save, then pass the `.kicad_pcb` with `--pcb` (or
drop it in the UI). Locked footprints and tracks are kept; everything else is
placed around them and autorouted. Mounting holes, fiducials and logos already
on the board are kept and treated as obstacles. The existing outline is reused.

## Finding your way around the web app

- **Sidebar**: *Request* holds what changes every run (level, the request, project and context, images, files,
  board size). *Library and settings* holds what you set once (model, checks, rules, your parts, datasheets,
  blocks, usage). The Run button stays visible in both.
- **Run status**: one dot per step; warnings and failures are named next to them. Click for every step and its detail.
- **Result tabs** are grouped: *Circuit* (schematic, connections, spec), *Board* (top, bottom, copper, edit
  layout, 3D), *Understand* (guide, reasoning, compare runs), *Toolbox*.
- **Findings** in the Toolbox are grouped by part (or by check, or flat) and can be filtered to errors only.
  **show on board** under any finding or guide item draws the board with those parts lit.
- **Compare runs** shows two runs side by side with what was added, removed, changed, rewired and moved.
- Generated files that are pictures or text (pinout card, power tree, order sheet, pin map) preview inside
  their Toolbox card.

## Before the design: questions and missing pieces

With the `ask` option on (the default), one small model call reads the request before anything is designed and
returns two things: questions whose answer would change the circuit, and the ICs and modules the design will use.
Code then checks each planned part on this machine: a datasheet record in the part database, a symbol in the KiCad
or your own libraries, the symbol's footprint, and a 3D model.

If nothing is open, the run carries on without a word. Otherwise it stops once:

- **Web app**: a "Before the design" page lists the questions (each with options; the first is what would be
  assumed) and the parts that are missing something, with a box for a datasheet link or file and for a vendor
  library zip (symbol, footprint, 3D model). Answer what you care about, skip the rest, press Continue.
- **Terminal**: the same questions are asked at the prompt. With no terminal attached (a script, a pipe), the
  first option of each question is assumed and that is logged.

The answers and the planned parts are handed to the designer, so the design uses the parts that were just checked.
Not used for spec files or schematics. Turn it off by unticking `ask`.

## Level, projects, context

**Level** (top of the sidebar, `--level` in a terminal) changes how much is explained and how much is asked.
It never changes the design: the same request gives the same board at every level.

- **Guided**: after the run, the Guide tab opens with a plain-language tour of the board (one small-model call)
  and a ranked list of what to check, why, and how.
- **Standard**: the ranked list without the tour (the tour is one click away).
- **Expert**: no explanations, notes hidden. In the web app the run stops once the circuit is designed, before
  any layout work, so you can read the spec, the assumptions and the checks, then **Continue to layout** or Revise.

**Projects** group boards that belong to one thing. A project carries its own description, a summary of every
board already designed in it (rails, connectors, pinouts, known problems from the build log) and its connector
**interfaces** into each new design. The `check.project` check then compares shared connectors pin for pin: a
connector carrying an interface name must match the interface, and connectors with the same name on two boards
must agree. It cannot see mirrored board-to-board stacks or crossed cables.

```
kipipe project new "OSCAR CubeSat" "1.5U, PC/104-style stack, 3.3 V and 5 V buses"
kipipe run --project oscar-cubesat --context "EPS board, sits at the bottom of the stack" -p "..."
kipipe addon run <job> aid.interface -o ref=J2 -o name=STACK
```

**Deriving from another board.** Inside a project, a request can say "use the same footprint as base_board and
stack on top of it, plugging into its STACK header". The designer then writes a `derive` section and code does
the mechanics:

- the outline, shape and mounting-hole type are copied, so the holes land in the same places;
- the mating connector is fixed exactly over the other board's connector, on the underside when this board sits
  above (`"side": "bottom"`), or at the same spot on top for pass-through stacking headers (`"side": "top"`);
- a part on the underside is mirrored, so code works out from the footprints which pad lies over which and moves
  the nets onto the pads that actually meet. The designer wires the connector pin for pin and never has to think
  about mirroring.

The `Fits its project` check then confirms it on the finished board: same outline, holes in line, every pad of
the mate over its counterpart, and the nets that meet agree. In a spec file:

```
"derive": {"board": "base_board", "outline": true, "mates": [{"ref": "J1", "with": "J1", "side": "bottom"}]}
```

Limits: the mating part must have the same pad pattern (a PinSocket for a PinHeader of the same size); the other
board's connector must be on its top side; nothing checks the height between the boards or parts that collide
across the gap; the area over a fixed connector is kept clear on both sides.

**Context** is background for one board that is not a request. **My rules** (`kipipe rules "always use 0805"`)
are standing preferences given to every design. **Build log** (Toolbox) records what happened to the real board;
a problem is passed to later boards of the project, and a lesson you write becomes a rule.

Also in this round: **Compare approaches first** (one small call lists two or three ways to build the request,
before anything is designed), **Where each choice came from** (which ICs rest on a datasheet record, a block,
your library, or only the model's memory), **Fix ticked with Revise** (tick check findings, they are written
into the Revise box for you to read and send), and the `fable` model plan.

## Toolbox: checks, generators, design aids

Everything that works on a design without being part of the text -> board path is an add-on. Each one says how
finished it is: no label means tested end to end, **beta** works but leans on heuristics, **preview** is a first
cut whose output is a starting point. In the web app they are in the **Toolbox** tab of a run; in a terminal:

```
kipipe addon list                         # everything, with its label
kipipe check <job folder | job id | spec.json>     # the design checks, no model, no KiCad run
kipipe addon run <job> gen.enclosure -o wall=2.4
```

**Design checks** (the `checks` flag, on by default, a second or two, never stops a build). Fixed rules for common
mistakes, not physics and not datasheet facts: net names that differ only in spelling, supplies nothing feeds,
IC supply pins without a bypass capacitor (and, once placed, how far away it sits), I2C pull-ups, LED current and
direction, reversed polarised capacitors (including on negative rails), crystal load capacitors, USB-C CC
resistors, boot/reset/strap pins of ESP32, ESP8266, AVR, RP2040 and STM32, parts under screw heads, connectors
set back from their edge, hand-solderability. A clean result means none of these rules fired, nothing more.
Skip one with `kipipe config set checks.skip check.handsolder`.

**Datasheet checks** (same stage, same flag). These compare the design with the part database, so they only
cover ICs whose datasheet has been read; every other part is listed as not checked. No model is called.

| | |
|---|---|
| Pin numbers for the package | the symbol's pin numbers against the datasheet's pin table for the package on the board; supply and ground pins on the wrong net; unconnected supplies; floating inputs |
| Voltages and currents against ratings | each pin's voltage against absolute maximum and recommended ranges (including limits like `VDD + 0.3 V` and pin-to-pin differentials); regulator output current against the rail's stated current |
| Support parts | every part the datasheet says the IC needs: present between the right pins, and at least the stated value |
| Equations with your values | the datasheet's design equations evaluated with the fitted parts, compared with what the board is meant to do (a feedback divider against the rail it feeds) |
| Regulators against their rails | output voltage from the part number against the output net, input headroom against dropout, dissipation and junction temperature |

A net's voltage is known from its name (`+3V3`, `-5V`) or from the spec's new `rails` section, which the designer
model now fills in for every supply net, including ones like `VBAT` whose name says nothing:
`"rails": {"VBAT": {"volts": 9.0, "amps": 0.3}}`. If the symbol and the record disagree on most pins, that is
reported once as "settle which is right", and the other checks match that part by pin name instead of number.
Findings quote the datasheet page. A record marked as read only once says so.

**Generators** (run on a finished job, files land in `out/addons/<id>/`):

| | |
|---|---|
| BOM and placement files | grouped BOM, plus BOM and CPL in assembly-house columns |
| Order sheet | size, layers, smallest track and hole, pad counts, read from the board file |
| Assembly guide | interactive page: click a BOM line, its parts light up on the board; tick off as you solder |
| Bring-up plan | first power-on checklist for this board: what to measure, where, what to expect |
| Pinout card | printable SVG of every connector |
| Firmware pin map | `pins.h`, `pins.py`, a pin table and a PlatformIO starting point, from the netlist |
| Printable enclosure (beta) | parametric OpenSCAD case: standoffs at the mounting holes, wall openings for edge connectors, lid holes over LEDs and buttons; STL too if `openscad` is installed. Part heights are estimates from footprint names |
| 3D model (STEP), PDF drawings | via kicad-cli |
| Power tree (beta) | diagram of inputs, regulators, protection parts and rails |
| Test point suggestions (beta) | which nets deserve one, and a spec with them added |
| Shareable report, README | one self-contained HTML file; a project page for a repository |
| Compare with another run | parts, connections and placement that changed |
| SPICE netlist (preview) | passives, diodes and sources are real; ICs are labelled stubs for the maker's model |
| Panel plan (preview) | how many fit, a drawing, and the KiKit command; does not write the panel itself |

**Design aids**

- **Brief check**: while you type, plain pattern matching lists what the request leaves open (power source, load
  current, connectors, programming, size, assembly). Click one to add the line to the request. No model.
- **Starters**: example requests to edit.
- **Blocks** (beta): sub-circuits known to be right (LDO, USB-C power input, I2C pull-ups, reset button, crystal,
  MOSFET switch, divider, protected input) that code places instead of the model re-deriving them. In a spec:
  `"blocks": [{"use": "ldo_ams1117", "name": "REG", "ports": {"VIN": "+5V", "VOUT": "+3V3", "GND": "GND"}}]`.
  The `blocks` flag offers them to the designer model. `kipipe block list | show <id> | check`; your own go in
  `~/.local/share/kipipe/blocks/`, or use **Save parts as a block** on a board that has proven itself.
- **Assumptions and requirements**: the designer model now lists what it assumed and ties each thing you asked
  for to parts; a check flags requirements it could not meet or that point at parts that do not exist.
- **Ask about this board** (beta): a question in plain words, answered by the small model from the board's own
  data (spec, nets, check findings, part records).
- **Usage**: every model call with its step, model, time and the cost the backend reports (`kipipe usage`).
  Optional limits: `kipipe config set budget.usd_month 10`, `kipipe config set budget.calls_day 200`.
- **Notifications** (preview): `kipipe config set notify.desktop true`, `notify.webhook <Discord webhook URL>` or
  `notify.ntfy <topic>`; a message goes out when a run finishes. Only project name and outcome are sent.

**Your own add-ons**: a `.py` file in `~/.local/share/kipipe/addons/`:

```python
def register(api):
    @api.addon(id="my.fuse", title="Input fuse", kind="check", auto=True, summary="Every supply input has a fuse.")
    def run(r):
        if r.job.of_kind("J") and not r.job.of_kind("F"):
            r.finding("warning", "No fuse on this board", "House rule.")
```

`r.job` gives the spec, nets, pin names, placement and pads; `r.write(name, text)` saves an output file. The
built-in ones in `kipipe/addons/` are written the same way.

## Check flags

Always on: spec validation, schematic netlist == spec, ERC, DRC (+ schematic
parity on KiCad 8+; pcbnew's DRC engine on 7).

| flag | default | what it does |
|---|---|---|
| `ipc` | on | sizes power traces from `current_a` (IPC-2221, 10 °C rise) before routing, verifies after |
| `rf` | on | 50 Ω (or any) microstrip width from the stackup for nets in `rf` |
| `pour` | on | GND pour on every copper layer |
| `openems` | off | builds a gerber2ems project for the `rf` nets (ports at the pads) and runs it if installed; slow |
| `render3d` | on | 3D render plus an interactive model in the 3D tab: right-drag rotate, middle-drag pan, wheel zoom, left-click a part for its value, footprint, pins, nets, neighbours and why it was chosen; arrows rotate, Shift+arrows pan, R/T/B views. KiCad 9+ |
| `inspect` | on | coarse-to-fine model inspection of the routed board, Review tab, suggested moves (2-3 model calls) |
| `review` | on | model checks a render of the placement and revises the floorplan once (1 model call) |
| `video` | on | MP4 of the build: placement as it happened (recorded by the placer), routing replay, finished board. `--video-size 1080x1920` for vertical; wide boards are rotated to fill it. Needs ffmpeg (falls back to GIF) |
| `force` | off | export fab files despite ERC/DRC/unrouted errors |

## Limits, plainly

- **Placement is heuristic**: connectivity-greedy plus annealing on wirelength, inside the floorplan constraints.
  The floorplan and review stop the absurd mistakes; they don't make it an expert. For switching regulators,
  RF, or dense boards, place the critical parts yourself and use hybrid mode.
- Courtyards are treated as rectangles, so L-shaped or T-shaped courtyards waste some space.
- **Freerouting is geometric.** It knows clearances and widths, not return paths or impedance. Hand-route
  and lock RF nets; the `rf` flag only sets the width.
- Freerouting 2.1.0 ignores max-passes; the router time budget (`--timeout`, default 300 s) is what limits it.
  Freerouting 2.2+ needs Java 25; the app picks the newest jar your Java can run.
- Generated schematics are drawn with real wires: parts placed next to what they connect to, orthogonal wires
  that only cross straight through other nets, junction dots at T-joins, standard power symbols, one name per
  net, matrices as straight row/column lines. Big buses are name tags instead of wires: the row/column lines of
  matrices with 8+ lines, and bundles of 6+ two-ended nets between the same two parts. A connection the wire router can't fit gets a name tag instead. KiCad's
  extracted netlist must match the spec exactly; if it doesn't, or ERC finds more errors than the old label
  layout, kipipe falls back to the label layout (`--sch-style labels` to always use it). Flat sheets only; dense
  boards with many buses are still busy.
- Hierarchical `.kicad_sch` input is not supported.
- KiCad's SWIG Python bindings (`pcbnew`) are removed in KiCad 11; board steps will need porting to the IPC API then.

## Layout

```
kipipe/
  spec.py      spec format + validation against installed libraries
  kicad_lib.py library tables, symbols (incl. extends), pins, footprints
  partdb.py    part database: datasheet reads, comparison, records, index, tiers, equations
  libs.py      imported parts (vendor zips -> kipipe_parts library); legacy.py converts KiCad 5 files
  schwire.py   spec -> wired .kicad_sch (placement, wire router, power symbols)
  schgen.py    label-layout fallback, netlist cross-check
  pcbgen.py    pcbnew side: build/place, DSN export, SES import, pours, DRC fallback (system python)
  placer.py    pure-Python placer
  router.py    Freerouting fetch/run
  physics.py   IPC-2221, Hammerstad-Jensen microstrip, stackups
  ems.py       gerber2ems project + run
  llm.py       backends + correction loop
  pipeline.py  orchestration
  app.py       Flask UI, cli.py terminal
  addons/      Toolbox: core.py (registry, Job view of a design, runner, plugin loader), checks.py,
               gen_docs.py / gen_build.py / gen_mech.py (generators), blocks.py + blocks/*.json, aids.py,
               usage.py (ledger, budget), notify.py, config.py
```

Job folders live in `~/.local/share/kipipe/jobs/` (UI) or wherever `-o` points.

## Same input, same board

Ask an AI for the same board twice and you normally get two different boards. kipipe pins that down:

- **Computed values.** The model writes the goal ("this LED at 5 mA", "5 V from this regulator, Vref 0.596 V") as a
  `calc` entry and leaves the value as `?`; code picks the standard E-series value and logs the working on the part.
  Kinds: `led`, `divider`, `feedback`, `i2c_pullup`, `crystal`, `rc` (see `kipipe/calc.py`).
- **Design cache.** An identical request to the same model (same text, images, answers, project, house rules and
  parts records) reuses the design that validated the first time.
- **Routing cache.** Freerouting routes the same board differently on every run, so an identical board reuses its
  first routing.
- **Fixed file IDs.** Schematic and board UUIDs come from a fixed sequence, so reruns write byte-identical files.

```
kipipe run --fresh ...      # ask the model and route again this once
kipipe cache                # what is cached
kipipe cache clear
```

`tests/test_determinism.py` runs one spec twice and compares the board files byte for byte.
