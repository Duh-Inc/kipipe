// kipipe 3D board viewer (three.js).
// Mouse: middle-drag pan, right-drag rotate, wheel zoom (towards the cursor), left-click a part for details.
// Keys: arrows rotate, Shift+arrows pan, + / - zoom, R reset, T top, B bottom, Esc clear selection.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const REFPAT = /^([A-Z]{1,4}[0-9]+)(?:[ _:\-\[(].*)?$/;
const esc = t => String(t ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function getJSON(url) {
  if (!url) return null;
  try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch (e) { return null; }
}

// Bare-board layers get the look of a real green board: glossy semi-transparent mask over bright copper and dark
// laminate, so traces read as lighter lines. KiCad names these meshes (PCB, copper, soldermask, silkscreen);
// when names are missing, the original material colour decides. Components keep KiCad's materials.
const BOARD_LOOK = {
  mask: () => new THREE.MeshStandardMaterial({ color: 0x0b8a36, roughness: 0.32, metalness: 0, transparent: true, opacity: 0.8, depthWrite: false }),
  copper: () => new THREE.MeshStandardMaterial({ color: 0xe0b45a, roughness: 0.3, metalness: 0.85 }),
  body: () => new THREE.MeshStandardMaterial({ color: 0x14240f, roughness: 0.85, metalness: 0 }),
  silk: () => new THREE.MeshStandardMaterial({ color: 0xf6f6f0, roughness: 0.8, metalness: 0 }),
};

function layerOf(mesh, isPart) {
  const names = [];
  for (let o = mesh; o; o = o.parent) names.push(o.name || "");
  const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
  for (const m of mats) names.push(m?.name || "");
  const s = names.join(" ").toLowerCase();
  if (isPart) return null;
  if (/silk/.test(s)) return "silk";
  if (/mask/.test(s)) return "mask";
  if (/copper|track|(^|[^a-z])(pads?|zones?|vias?)([^a-z]|$)|(^|[^a-z])[fb][._]cu([^a-z]|$)/.test(s)) return "copper";
  if (/(^|[^a-z])pcb([^a-z]|$)|board|substrate|(^|[^a-z])core([^a-z]|$)/.test(s)) return "body";
  // no useful name: judge by the material KiCad gave it
  const m = mats[0];
  if (!m || !m.color) return null;
  const { r, g, b } = m.color;
  if (g > r * 1.25 && g > b * 1.1 && (m.transparent || m.opacity < 1)) return "mask";
  if (r > 0.6 && g > 0.6 && b > 0.6 && Math.abs(r - g) < 0.08 && Math.abs(g - b) < 0.08) return "silk";
  if (r > g && g > b * 1.3 && (m.metalness || 0) > 0.3) return "copper";
  return null;
}

function restyleBoard(model, known) {
  const isPart = o => {
    for (let p = o; p; p = p.parent) {
      const m = REFPAT.exec(p.name || "");
      if (m && (known.size === 0 || known.has(m[1]))) return true;
    }
    return false;
  };
  const counts = {};
  model.traverse(o => {
    if (!o.isMesh) return;
    const layer = layerOf(o, isPart(o));
    if (layer) {
      o.material = BOARD_LOOK[layer]();
      if (layer === "mask") o.renderOrder = 2;
      counts[layer] = (counts[layer] || 0) + 1;
    } else {
      for (const m of Array.isArray(o.material) ? o.material : [o.material]) if (m && "envMapIntensity" in m) m.envMapIntensity = 0.7;
    }
  });
  console.debug("kipipe 3D: restyled board layers", counts);
}

export async function open(container, opts) {
  const { glb, layoutUrl, specUrl, metaUrl } = opts;
  container.innerHTML = `
    <div class="v3">
      <div class="v3-bar">
        <button data-a="reset" title="R">Reset view</button>
        <button data-a="top" title="T">Top</button>
        <button data-a="bottom" title="B">Bottom</button>
        <button data-a="keys" class="v3-kb" aria-expanded="false">Keys</button>
        <span class="v3-help">Right-drag rotate, middle-drag pan, wheel zoom, click a part for details</span>
        <span class="v3-right"><a class="dl3" href="${glb}" download>board.glb</a></span>
      </div>
      <div class="v3-stage" tabindex="0">
        <div class="v3-load">Loading 3D model…</div>
        <div class="v3-tip" hidden></div>
        <aside class="v3-info" hidden></aside>
        <div class="v3-keys" hidden>Arrows: rotate<br>Shift + arrows: pan<br>+ and -: zoom<br>R: reset view, T: top, B: bottom<br>Esc: clear selection</div>
      </div>
    </div>`;
  const stage = container.querySelector(".v3-stage");
  const tip = container.querySelector(".v3-tip");
  const info = container.querySelector(".v3-info");
  const loadEl = container.querySelector(".v3-load");

  const [layout, spec, meta] = await Promise.all([getJSON(layoutUrl), getJSON(specUrl), getJSON(metaUrl)]);
  const parts = Object.fromEntries((layout?.parts || []).map(p => [p.ref, p]));
  const comps = Object.fromEntries((spec?.components || []).map(c => [c.ref, c]));
  const pinNets = {};
  for (const [net, pins] of Object.entries(spec?.nets || {}))
    for (const p of pins) { const [r, n] = p.split("."); (pinNets[r] = pinNets[r] || []).push([n, net]); }
  const known = new Set([...Object.keys(parts), ...Object.keys(comps)]);
  const why = spec?.rationale?.parts || {};
  const netPins = spec?.nets || {};

  // renderer / scene -----------------------------------------------------------------
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.9;
  stage.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x04150d);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;
  const sun = new THREE.DirectionalLight(0xffffff, 0.9); sun.position.set(1, 2, 1.2); scene.add(sun);
  const camera = new THREE.PerspectiveCamera(35, 1, 0.001, 100);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.mouseButtons = { LEFT: null, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.ROTATE };
  controls.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
  controls.enableDamping = true; controls.dampingFactor = 0.12;
  controls.zoomToCursor = true;
  controls.screenSpacePanning = true;
  renderer.domElement.addEventListener("contextmenu", e => e.preventDefault());
  renderer.domElement.addEventListener("mousedown", e => { if (e.button === 1) e.preventDefault(); });  // no autoscroll on M3

  // model ----------------------------------------------------------------------------
  let gltf;
  try { gltf = await new GLTFLoader().loadAsync(glb); }
  catch (e) { loadEl.textContent = "Could not load the 3D model: " + e.message; return { dispose() {} }; }
  loadEl.remove();
  const model = gltf.scene;
  restyleBoard(model, known);
  const raw = new THREE.Box3().setFromObject(model);
  const rawSize = raw.getSize(new THREE.Vector3());
  // the board's thickness is the smallest extent; make that axis point up (+Y)
  const upAxis = ["x", "y", "z"].reduce((a, b) => (rawSize[a] <= rawSize[b] ? a : b));
  const holder = new THREE.Group();
  holder.add(model);
  if (upAxis === "z") holder.rotation.x = -Math.PI / 2;
  if (upAxis === "x") holder.rotation.z = Math.PI / 2;
  scene.add(holder);
  holder.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(holder);
  const center = box.getCenter(new THREE.Vector3());
  holder.position.sub(center);
  holder.updateMatrixWorld(true);
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.z) * 0.5 || 0.05;
  camera.near = radius / 200; camera.far = radius * 200; camera.updateProjectionMatrix();
  controls.minDistance = radius * 0.08; controls.maxDistance = radius * 12;

  // which top-level node is each part?
  const refOf = obj => {
    for (let o = obj; o && o !== holder; o = o.parent) {
      const m = REFPAT.exec(o.name || "");
      if (m && (known.size === 0 || known.has(m[1]))) return { ref: m[1], node: o };
    }
    return null;
  };
  // fallback when names are missing: board mm from the hit point (needs the board-origin export)
  const metres = Math.max(rawSize.x, rawSize.y, rawSize.z) < 2;
  const s = metres ? 1000 : 1;
  const inPlane = ["x", "y", "z"].filter(a => a !== upAxis);
  let named = false;
  model.traverse(o => { const m = REFPAT.exec(o.name || ""); if (m && (known.size === 0 || known.has(m[1]))) named = true; });
  const refAt = worldPoint => {
    if (named || !meta || !meta.board_origin || !layout) return null;
    const p = model.worldToLocal(worldPoint.clone());
    const bx = Math.abs(p[inPlane[0]] * s), by = Math.abs(p[inPlane[1]] * s);
    let best = null;
    for (const q of layout.parts) {
      const g = q.geo[q.rot] || Object.values(q.geo)[0];
      const b = g.box;
      if (bx >= q.x + b[0] && bx <= q.x + b[2] && by >= q.y + b[1] && by <= q.y + b[3]) {
        const area = (b[2] - b[0]) * (b[3] - b[1]);
        if (!best || area < best.area) best = { ref: q.ref, area };
      }
    }
    return best && { ref: best.ref, node: null };
  };

  // views ----------------------------------------------------------------------------
  const view = (name, animate = true) => {
    const d = radius * 2.6;
    const dir = name === "top" ? new THREE.Vector3(0, 1, 0.0001)
      : name === "bottom" ? new THREE.Vector3(0, -1, 0.0001)
      : new THREE.Vector3(0, 0.95, 1.05);
    const to = dir.normalize().multiplyScalar(d);
    if (!animate) { camera.position.copy(to); controls.target.set(0, 0, 0); controls.update(); return; }
    const from = camera.position.clone(), t0 = controls.target.clone(), start = performance.now();
    const step = now => {
      const k = Math.min(1, (now - start) / 380), e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      camera.position.lerpVectors(from, to, e); controls.target.lerpVectors(t0, new THREE.Vector3(), e); controls.update();
      if (k < 1) requestAnimationFrame(step);
    };
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { camera.position.copy(to); controls.target.set(0, 0, 0); controls.update(); }
    else requestAnimationFrame(step);
  };
  view("reset", false);

  // selection -------------------------------------------------------------------------
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  let selected = null, saved = [];
  const pick = e => {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const hits = ray.intersectObject(holder, true);
    for (const h of hits) {
      const f = refOf(h.object) || refAt(h.point);
      if (f) return f;
    }
    return null;
  };
  const meshesOf = (ref, node) => {
    const out = [];
    const root = node || holder;
    root.traverse(o => { if (o.isMesh && (node || refOf(o)?.ref === ref)) out.push(o); });
    return out;
  };
  const clearSel = () => {
    for (const [m, mat] of saved) m.material = mat;
    saved = []; selected = null; info.hidden = true;
  };
  const select = f => {
    clearSel();
    if (!f) return;
    selected = f.ref;
    for (const m of meshesOf(f.ref, f.node)) {
      saved.push([m, m.material]);
      const mk = mat => { const c = mat.clone(); if (c.emissive) { c.emissive = new THREE.Color(0xd8b15a); c.emissiveIntensity = 0.55; } return c; };
      m.material = Array.isArray(m.material) ? m.material.map(mk) : mk(m.material);
    }
    showInfo(f.ref);
  };
  const showInfo = ref => {
    const c = comps[ref] || {}, p = parts[ref];
    const nets = (pinNets[ref] || []).sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }));
    info.innerHTML = `
      <button class="v3-x" title="Esc" aria-label="Close">×</button>
      <h4>${esc(ref)}</h4>
      <div class="v3-val">${esc(c.value || "")}</div>
      <dl>
        ${c.symbol ? `<dt>Symbol</dt><dd>${esc(c.symbol)}</dd>` : ""}
        ${c.footprint || p?.lib ? `<dt>Footprint</dt><dd>${esc(c.footprint || p.lib)}</dd>` : ""}
        ${p ? `<dt>Position</dt><dd>${p.x.toFixed(2)}, ${p.y.toFixed(2)} mm, ${Number(p.rot)}°, ${p.back ? "bottom" : "top"}</dd>` : ""}
        ${c.description ? `<dt>Notes</dt><dd>${esc(c.description)}</dd>` : ""}
      </dl>
      ${why[ref] ? `<div class="v3-why"><b>Why this part</b><p>${esc(why[ref])}</p></div>` : ""}
      ${nets.length ? `<table><tr><th>Pin</th><th>Net</th><th>Also on this net</th></tr>${nets.map(([n, net]) => {
          const others = [...new Set((netPins[net] || []).map(x => x.split(".")[0]).filter(r => r !== ref))];
          const list = others.length > 6 ? others.slice(0, 5).map(esc).join(", ") + ` and ${others.length - 5} more` : others.map(esc).join(", ");
          return `<tr><td>${esc(n)}</td><td>${esc(net)}</td><td class="v3-oth">${list || "nothing else"}</td></tr>`;
        }).join("")}</table>` : ""}`;
    info.hidden = false;
    info.querySelector(".v3-x").onclick = clearSel;
  };

  let down = null;
  renderer.domElement.addEventListener("pointerdown", e => { if (e.button === 0) down = [e.clientX, e.clientY]; stage.focus({ preventScroll: true }); });
  renderer.domElement.addEventListener("pointerup", e => {
    if (e.button !== 0 || !down) return;
    const moved = Math.hypot(e.clientX - down[0], e.clientY - down[1]);
    down = null;
    if (moved < 5) { const f = pick(e); f ? select(f) : clearSel(); }
  });
  let hoverT = 0;
  renderer.domElement.addEventListener("pointermove", e => {
    if (e.buttons) { tip.hidden = true; return; }
    const now = performance.now(); if (now - hoverT < 50) return; hoverT = now;
    const f = pick(e);
    renderer.domElement.style.cursor = f ? "pointer" : "default";
    if (!f) { tip.hidden = true; return; }
    const r = stage.getBoundingClientRect();
    tip.textContent = f.ref + (comps[f.ref]?.value ? "  " + comps[f.ref].value : "");
    tip.style.left = (e.clientX - r.left + 14) + "px"; tip.style.top = (e.clientY - r.top + 12) + "px";
    tip.hidden = false;
  });
  renderer.domElement.addEventListener("pointerleave", () => { tip.hidden = true; });

  // keys: only while the 3D view is on screen
  const keys = e => {
    if (!document.body.contains(stage) || e.target.matches("input, textarea, select")) return;
    const k = e.key, rot = Math.PI / 24;
    const sph = new THREE.Spherical().setFromVector3(camera.position.clone().sub(controls.target));
    let handled = true;
    if (k.startsWith("Arrow") && e.shiftKey) {
      const pan = radius * 0.08;
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0), up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      const d = { ArrowLeft: right.clone().multiplyScalar(-pan), ArrowRight: right.clone().multiplyScalar(pan),
                  ArrowUp: up.clone().multiplyScalar(pan), ArrowDown: up.clone().multiplyScalar(-pan) }[k];
      camera.position.add(d); controls.target.add(d);
    } else if (k === "ArrowLeft" || k === "ArrowRight") { sph.theta += k === "ArrowLeft" ? -rot : rot; }
    else if (k === "ArrowUp" || k === "ArrowDown") { sph.phi = Math.min(Math.PI - 0.01, Math.max(0.01, sph.phi + (k === "ArrowUp" ? -rot : rot))); }
    else if (k === "+" || k === "=") sph.radius = Math.max(controls.minDistance, sph.radius / 1.15);
    else if (k === "-" || k === "_") sph.radius = Math.min(controls.maxDistance, sph.radius * 1.15);
    else if (k === "r" || k === "R") { view("reset"); }
    else if (k === "t" || k === "T") { view("top"); }
    else if (k === "b" || k === "B") { view("bottom"); }
    else if (k === "Escape") { clearSel(); }
    else handled = false;
    if (!handled) return;
    e.preventDefault();
    if (k.startsWith("Arrow") && !e.shiftKey || k === "+" || k === "=" || k === "-" || k === "_") {
      camera.position.copy(new THREE.Vector3().setFromSpherical(sph).add(controls.target));
    }
    controls.update();
  };
  window.addEventListener("keydown", keys);
  container.querySelector(".v3-bar").onclick = e => {
    const a = e.target.dataset.a;
    if (a === "keys") { const k = container.querySelector(".v3-keys"); k.hidden = !k.hidden; e.target.setAttribute("aria-expanded", String(!k.hidden)); }
    else if (a) view(a);
  };

  // render loop ------------------------------------------------------------------------
  const resize = () => {
    const w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  };
  const ro = new ResizeObserver(resize); ro.observe(stage); resize();
  let alive = true;
  const loop = () => { if (!alive) return; controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop); };
  loop();

  return {
    dispose() {
      alive = false; ro.disconnect(); window.removeEventListener("keydown", keys);
      controls.dispose(); pmrem.dispose(); renderer.dispose();
      scene.traverse(o => { if (o.isMesh) { o.geometry?.dispose(); } });
    },
  };
}
