// kipipe layout editor: drag/rotate parts on the routed board, then reroute.
// Geometry comes from layout.json (measured by pcbnew at each 90-degree step),
// so no rotation math happens here.
"use strict";

const Editor = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const GRID = 0.25;
  let L, root, svg, gBoard, gRats, gParts, info, parts, sel = null, hist = [], vb, jobId, onReroute;

  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  };
  const rkey = r => String(Number((((r % 360) + 360) % 360).toFixed(1)));
  const snap = v => Math.round(v / GRID) * GRID;

  async function open(container, url, job, cb) {
    jobId = job; onReroute = cb;
    L = await (await fetch(url)).json();
    container.innerHTML = "";
    root = document.createElement("div");
    root.className = "ed";
    root.innerHTML = `
      <div class="ed-bar">
        <button data-a="rot" title="R">rotate</button>
        <button data-a="undo" title="Ctrl+Z">undo</button>
        <button data-a="reset">reset</button>
        <span class="ed-info"></span>
        <span class="ed-go-wrap">
          <button data-a="keep" class="ed-go">reroute, keep others</button>
          <button data-a="replace" class="ed-go">reroute, re-place others</button>
        </span>
      </div>
      <div class="ed-hint">drag parts · R rotate · Ctrl+Z undo · wheel zoom · drag empty space to pan · moved parts get locked</div>`;
    container.append(root);
    info = root.querySelector(".ed-info");
    root.querySelector(".ed-bar").onclick = e => {
      const a = e.target.dataset.a;
      if (a === "rot") rotate();
      else if (a === "undo") undo();
      else if (a === "reset") { parts = initial(); hist = []; draw(); }
      else if (a === "keep" || a === "replace") reroute(a === "replace");
    };
    const [W, H] = L.board;
    vb = [-4, -4, W + 8, H + 8];
    svg = el("svg", { class: "ed-svg", viewBox: vb.join(" "), preserveAspectRatio: "xMidYMid meet" }, root);
    gBoard = el("g", {}, svg);
    if (L.outline) el("polygon", { points: L.outline.map(p => p.join(",")).join(" "), class: "ed-board" }, gBoard);
    else el("rect", { x: 0, y: 0, width: W, height: H, class: "ed-board" }, gBoard);
    gRats = el("g", { class: "ed-rats" }, svg);
    gParts = el("g", {}, svg);
    parts = initial();
    wire();
    draw();
  }

  function initial() {
    const m = {};
    for (const p of L.parts) m[p.ref] = { ...p, x0: p.x, y0: p.y, rot0: p.rot };
    return m;
  }

  function geo(p) { return p.geo[p.rot] || p.geo[Object.keys(p.geo)[0]]; }
  function rect(p) { const b = geo(p).box; return [p.x + b[0], p.y + b[1], p.x + b[2], p.y + b[3]]; }
  const hit = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];

  // point-in-polygon and rect-inside for shaped boards (same rule as the placer)
  function pin(poly, x, y) {
    let c = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if ((yi > y) !== (yj > y) && x < xi + (y - yi) * (xj - xi) / (yj - yi)) c = !c;
    }
    return c;
  }
  function outside(a) {
    const [W, H] = L.board;
    if (!L.outline) return a[0] < -0.05 || a[1] < -0.05 || a[2] > W + 0.05 || a[3] > H + 0.05;
    const corners = [[a[0], a[1]], [a[2], a[1]], [a[2], a[3]], [a[0], a[3]]];
    if (!corners.every(([x, y]) => pin(L.outline, x, y))) return true;
    return L.outline.some(([x, y]) => x > a[0] && x < a[2] && y > a[1] && y < a[3]);
  }

  function problems() {
    const bad = new Set(), list = Object.values(parts);
    for (let i = 0; i < list.length; i++) {
      const a = rect(list[i]);
      if (!list[i].fixed && outside(a) && !list[i].lib.startsWith("RF_Module")
          && !/Connector|Coaxial|USB/.test(list[i].lib)) bad.add(list[i].ref);
      for (let j = i + 1; j < list.length; j++) {
        if (list[i].back !== list[j].back) continue;
        if (hit(a, rect(list[j]))) { bad.add(list[i].ref); bad.add(list[j].ref); }
      }
    }
    return bad;
  }

  function moved() {
    return Object.values(parts).filter(p => Math.abs(p.x - p.x0) > 1e-6 || Math.abs(p.y - p.y0) > 1e-6 || p.rot !== p.rot0);
  }

  function draw() {
    gParts.innerHTML = "";
    const bad = problems();
    const mvset = new Set(moved().map(p => p.ref));
    for (const p of Object.values(parts)) {
      const g = el("g", { class: "ed-part" + (p.back ? " back" : "") + (p.fixed ? " fixed" : "") + (sel === p.ref ? " sel" : "")
                       + (bad.has(p.ref) ? " bad" : "") + (p.locked ? " locked" : ""), "data-ref": p.ref,
                       transform: `translate(${p.x} ${p.y})` }, gParts);
      const gg = geo(p), b = gg.box;
      el("rect", { x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1], class: "ed-court" }, g);
      for (const pd of gg.pads) el("rect", { x: pd[2], y: pd[3], width: pd[4] - pd[2], height: pd[5] - pd[3], class: "ed-pad" }, g);
      const t = el("text", { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, class: "ed-ref" }, g);
      t.textContent = p.ref + ((p.locked || mvset.has(p.ref)) && !p.fixed ? " 🔒" : "");
      t.setAttribute("font-size", Math.max(0.6, Math.min(1.6, (b[2] - b[0]) / Math.max(3, p.ref.length) * 1.4)));
    }
    rats();
    const mv = mvset.size;
    info.textContent = `${mv} moved · ${bad.size ? bad.size + " overlapping/outside" : "no overlaps"}` + (sel ? ` · ${sel}` : "");
    info.classList.toggle("warn", bad.size > 0);
  }

  function rats() {
    gRats.innerHTML = "";
    const nets = {};
    for (const p of Object.values(parts))
      for (const pd of geo(p).pads)
        if (pd[1]) (nets[pd[1]] = nets[pd[1]] || []).push([p.x + (pd[2] + pd[4]) / 2, p.y + (pd[3] + pd[5]) / 2]);
    for (const [n, pts] of Object.entries(nets)) {
      if (pts.length < 2) continue;
      const gnd = /^\/?(GND|AGND|DGND|VSS|0V)$/i.test(n);
      // Prim MST
      const inT = new Array(pts.length).fill(false); inT[0] = true;
      const best = pts.map(q => [Math.hypot(q[0] - pts[0][0], q[1] - pts[0][1]), 0]);
      for (let k = 1; k < pts.length; k++) {
        let j = -1;
        for (let i = 0; i < pts.length; i++) if (!inT[i] && (j < 0 || best[i][0] < best[j][0])) j = i;
        inT[j] = true;
        const a = pts[best[j][1]], c = pts[j];
        el("line", { x1: a[0], y1: a[1], x2: c[0], y2: c[1], class: gnd ? "gnd" : "" }, gRats);
        for (let i = 0; i < pts.length; i++) {
          if (inT[i]) continue;
          const d = Math.hypot(pts[i][0] - c[0], pts[i][1] - c[1]);
          if (d < best[i][0]) best[i] = [d, j];
        }
      }
    }
  }

  function pt(e) {
    const p = svg.createSVGPoint(); p.x = e.clientX; p.y = e.clientY;
    return p.matrixTransform(svg.getScreenCTM().inverse());
  }
  const push = () => hist.push(JSON.stringify(Object.fromEntries(Object.values(parts).map(p => [p.ref, [p.x, p.y, p.rot]]))));

  function undo() {
    if (!hist.length) return;
    const s = JSON.parse(hist.pop());
    for (const [r, [x, y, rot]] of Object.entries(s)) Object.assign(parts[r], { x, y, rot });
    draw();
  }

  function rotate() {
    if (!sel) return;
    const p = parts[sel], next = rkey(Number(p.rot) + 90);
    if (!p.geo[next]) return;
    push(); p.rot = next; draw();
  }

  function wire() {
    let drag = null, pan = null, frame = 0;
    svg.addEventListener("pointerdown", e => {
      const g = e.target.closest(".ed-part");
      const q = pt(e);
      if (g) {
        sel = g.dataset.ref;
        const p = parts[sel];
        push();
        drag = { ref: sel, dx: q.x - p.x, dy: q.y - p.y, moved: false };
        svg.setPointerCapture(e.pointerId);
        draw();
      } else {
        sel = null; draw();
        pan = { x: e.clientX, y: e.clientY, vb: [...vb] };
        svg.setPointerCapture(e.pointerId);
      }
    });
    svg.addEventListener("pointermove", e => {
      if (drag) {
        const q = pt(e), p = parts[drag.ref];
        const nx = e.shiftKey ? q.x - drag.dx : snap(q.x - drag.dx), ny = e.shiftKey ? q.y - drag.dy : snap(q.y - drag.dy);
        if (nx !== p.x || ny !== p.y) { p.x = nx; p.y = ny; drag.moved = true; }
        if (!frame) frame = requestAnimationFrame(() => { frame = 0; draw(); });
      } else if (pan) {
        const r = svg.getBoundingClientRect(), s = Math.max(pan.vb[2] / r.width, pan.vb[3] / r.height);
        vb[0] = pan.vb[0] - (e.clientX - pan.x) * s; vb[1] = pan.vb[1] - (e.clientY - pan.y) * s;
        svg.setAttribute("viewBox", vb.join(" "));
      }
    });
    svg.addEventListener("pointerup", () => {
      if (drag && !drag.moved) hist.pop();
      drag = null; pan = null;
    });
    svg.addEventListener("wheel", e => {
      e.preventDefault();
      const q = pt(e), k = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      vb = [q.x - (q.x - vb[0]) * k, q.y - (q.y - vb[1]) * k, vb[2] * k, vb[3] * k];
      svg.setAttribute("viewBox", vb.join(" "));
    }, { passive: false });
    window.addEventListener("keydown", e => {
      if (!root || !document.body.contains(root)) return;
      if (e.target.matches("input, textarea, select")) return;
      if (e.key === "r" || e.key === "R") { rotate(); e.preventDefault(); }
      else if ((e.ctrlKey || e.metaKey) && e.key === "z") { undo(); e.preventDefault(); }
      else if (e.key === "Escape") { sel = null; draw(); }
    });
  }

  async function reroute(replace) {
    const mv = moved();
    const bad = problems();
    if (!mv.length && !confirm("Nothing moved. Reroute anyway?")) return;
    if (bad.size && !replace && !confirm(`${bad.size} parts overlap or sit outside the board. Reroute anyway? (DRC will flag it)`)) return;
    const moves = Object.fromEntries(mv.map(p => [p.ref, { x: +p.x.toFixed(3), y: +p.y.toFixed(3), rot: Number(p.rot) }]));
    const r = await fetch(`/api/jobs/${jobId}/reroute`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ moves, replace }) });
    const j = await r.json();
    if (!r.ok) { alert(j.error || "reroute failed"); return; }
    onReroute(j.id);
  }

  return { open };
})();
