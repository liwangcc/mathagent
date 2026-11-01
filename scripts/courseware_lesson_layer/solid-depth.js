/* MathAgent solid depth v1 — render-only depth upgrade for 3D solids.
   Injected immediately before a chapter's app script, so the first frame already uses it.
   It changes how solids are DRAWN, never what they are: every vertex, edge, label, handle
   and number still comes from the chapter's own scene code and state.
   - One right-handed perspective projection shared by the WebGL camera and the SVG
     fallback (the senior painter's screen map was a mirror image of its own depth order).
   - Opaque, depth-tested faces lit by a light fixed in the world, near/far tone separation,
     a darker tone on the back side of thin boards, boards drawn with real thickness.
   - Edges are tubes (feature edges only, no hairlines); hidden edges, rods and points are
     drawn dimmed and dashed — the textbook convention — in both render paths.
   - Ground grid with contact shadow, plus a corner axis triad, as the spatial reference.
   Teacher feedback addressed: 2026-09-28 (paper-thin solids, front/back and spin ambiguity). */
(function () {
  'use strict';
  if (window.SolidDepth) return;
  const SD = { version: 1, family: null, frames: 0, last: null };
  window.SolidDepth = SD;

  /* ---------- vector and colour helpers ---------- */
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const len = a => Math.hypot(a[0], a[1], a[2]);
  const unit = a => { const l = len(a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const centroid = ps => mul(ps.reduce(add, [0, 0, 0]), 1 / ps.length);
  function newell(ps) {
    let n = [0, 0, 0];
    for (let i = 0; i < ps.length; i++) {
      const a = ps[i], b = ps[(i + 1) % ps.length];
      n = add(n, [(a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])]);
    }
    return unit(n);
  }
  function rgb(c) {
    if (typeof c === 'number') return [(c >> 16) & 255, (c >> 8) & 255, c & 255];
    let s = String(c || '#7a9a90').trim();
    if (/^#[0-9a-f]{3}$/i.test(s)) s = '#' + s.slice(1).split('').map(x => x + x).join('');
    const m = /^#([0-9a-f]{6})/i.exec(s);
    if (m) { const n = parseInt(m[1], 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
    const r = /rgba?\(([^)]+)\)/.exec(s);
    return r ? r[1].split(/[ ,/]+/).slice(0, 3).map(Number) : [122, 154, 144];
  }
  const hex = c => '#' + c.map(v => clamp(Math.round(v), 0, 255).toString(16).padStart(2, '0')).join('');
  const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  const BG = [247, 249, 246];
  // Author colours with low opacity become paler, fully opaque tints (never see-through):
  // a faint reference plane (opacity .2) stays pale, a solid face (.48) keeps its hue.
  const tint = (color, opacity) => mix(rgb(color), [255, 255, 255], clamp(0.85 - (opacity == null ? 0.35 : opacity) * 1.3, 0.28, 0.72));
  const LIGHT = unit([0.53, -0.32, 0.94]); // fixed in the world: upper left of the default view
  function shade(c, n, fog, back) {
    const lam = 0.66 + 0.36 * Math.max(0, dot(n, LIGHT));
    let out = c.map(v => v * lam * (back ? 0.8 : 1));
    return mix(out, BG, clamp(fog, 0, 1) * 0.3);
  }

  /* ---------- topology: groups, outward normals, feature edges ---------- */
  function analyze(polys) {
    const key = p => p.map(v => Math.round(v * 1e4)).join(',');
    const edges = new Map();
    polys.forEach((f, i) => {
      f.normal = newell(f.points); f.center = centroid(f.points); f.flip = false;
      for (let j = 0; j < f.points.length; j++) {
        const a = f.points[j], b = f.points[(j + 1) % f.points.length], ka = key(a), kb = key(b);
        if (ka === kb) continue;
        const ek = ka < kb ? ka + '|' + kb : kb + '|' + ka;
        let e = edges.get(ek);
        if (!e) { e = { a, b, faces: [] }; edges.set(ek, e); }
        if (!e.faces.includes(i)) e.faces.push(i);
      }
    });
    const parent = polys.map((_, i) => i);
    const find = i => (parent[i] === i ? i : (parent[i] = find(parent[i])));
    for (const e of edges.values()) for (let k = 1; k < e.faces.length; k++) parent[find(e.faces[k])] = find(e.faces[0]);
    const groups = new Map();
    polys.forEach((f, i) => { const r = find(i); if (!groups.has(r)) groups.set(r, []); groups.get(r).push(i); });
    for (const members of groups.values()) {
      const board = members.length === 1;
      const gc = centroid(members.map(i => polys[i].center));
      for (const i of members) {
        const f = polys[i]; f.board = board; f.group = members[0];
        if (!board && dot(f.normal, sub(f.center, gc)) < 0) { f.normal = mul(f.normal, -1); f.flip = true; }
      }
    }
    // A standalone board (e.g. a cross-section) inside a closed solid makes that solid an
    // x-ray shell: translucent faces, so the object being taught stays visible.
    const boxes = new Map();
    for (const [root, members] of groups) {
      if (members.length < 4) continue;
      const ps = members.flatMap(i => polys[i].points);
      boxes.set(root, [0, 1, 2].map(k => [Math.min(...ps.map(p => p[k])), Math.max(...ps.map(p => p[k]))]));
    }
    const xray = new Set();
    polys.forEach((f, i) => {
      if (!f.board) return;
      for (const [root, b] of boxes) {
        const pad = b.map(([lo, hi]) => (hi - lo) * 0.02 + 1e-4);
        if (f.points.every(p => p.every((v, k) => v >= b[k][0] - pad[k] && v <= b[k][1] + pad[k])) && b.every(([lo, hi], k) => hi - lo > 1e-6)) xray.add(root);
      }
    });
    polys.forEach((f, i) => { f.xray = xray.has(find(i)); });
    const feature = [];
    for (const e of edges.values()) {
      let sharp = e.faces.length === 1;
      if (!sharp) { const n1 = polys[e.faces[0]].normal, n2 = polys[e.faces[1]].normal; sharp = dot(n1, n2) < Math.cos(25 * Math.PI / 180); }
      if (sharp) feature.push(e);
    }
    return { edges: feature, polys, groups: groups.size };
  }

  /* ---------- analytic visibility (shared by both render paths) ---------- */
  // A point is hidden when the ray from the viewer to it crosses a face first. Used to split
  // edges/rods into visible and hidden runs for the SVG path, and to dim labels of hidden
  // anchors in both paths, so both paths agree on what is in front.
  function visibility(polys, P, view) {
    const faces = polys.map(f => {
      const q = f.points.map(P);
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const p of q) { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); }
      return { q, n: f.normal || newell(f.points), p0: f.points[0], box: [x0, y0, x1, y1] };
    });
    const inside = (x, y, q) => {
      let c = false;
      for (let i = 0, j = q.length - 1; i < q.length; j = i++) {
        const a = q[i], b = q[j];
        if ((a[1] > y) !== (b[1] > y) && x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1] || 1e-12) + a[0]) c = !c;
      }
      return c;
    };
    return function hidden(M) {
      const s = P(M);
      for (const F of faces) {
        if (s[0] < F.box[0] || s[0] > F.box[2] || s[1] < F.box[1] || s[1] > F.box[3] || !inside(s[0], s[1], F.q)) continue;
        if (view.eye) {
          const d = sub(M, view.eye), den = dot(F.n, d);
          if (Math.abs(den) < 1e-9) continue;
          const t = dot(F.n, sub(F.p0, view.eye)) / den;
          if (t > 1e-3 && t < 1 - 2e-3) return true;
        } else {
          const den = dot(F.n, view.dir);
          if (Math.abs(den) < 1e-9) continue;
          if (dot(F.n, sub(F.p0, M)) / den > 2e-3) return true;
        }
      }
      return false;
    };
  }
  // Split a segment into pieces, classify each, and merge equal neighbours into runs.
  function runs(a, b, pieces, hidden) {
    const out = [];
    for (let i = 0; i < pieces; i++) {
      const u = i / pieces, v = (i + 1) / pieces, h = hidden(add(a, mul(sub(b, a), (u + v) / 2)));
      const last = out[out.length - 1];
      if (last && last.h === h) last.v = v; else out.push({ u, v, h });
    }
    return out.map(r => ({ a: add(a, mul(sub(b, a), r.u)), b: add(a, mul(sub(b, a), r.v)), hidden: r.h }));
  }

  /* ---------- SVG fallback renderer (same frame data as WebGL) ---------- */
  function svgSolids(frame) {
    const { polys, lines, dots, project, toward, ground } = frame;
    const A = analyze(polys);
    const P = p => project(p);
    const Ds = [];
    for (const f of polys) for (const p of f.points) Ds.push(P(p)[2]);
    for (const l of lines) Ds.push(P(l.a)[2], P(l.b)[2]);
    const dmin = Math.min(...Ds, 0), dmax = Math.max(...Ds, 1e-6);
    const fogOf = d => (dmax - d) / Math.max(1e-6, dmax - dmin);
    const kOf = p => (frame.kAt ? frame.kAt(p) : 1);
    const pts = ps => ps.map(p => { const q = P(p); return q[0].toFixed(1) + ',' + q[1].toFixed(1); }).join(' ');
    const seg = (a, b, attrs) => { const p = P(a), q = P(b); return `<line x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}" ${attrs}/>`; };
    const hidden = visibility(polys, P, frame.view);
    const items = [];
    let top = '', shadow = '', hiddenRuns = 0, visibleRuns = 0, faceCount = 0, hiddenDots = 0;
    const vis = []; // visible edge/rod/point pieces, painted far-to-near after all hidden pieces
    const dm = (a, b) => (P(a)[2] + P(b)[2]) / 2;
    // 1) faces, painter-sorted (boards split into patches so crossing planes interleave)
    polys.forEach(f => {
      const view = unit(toward(f.center));
      const facing = dot(f.normal, view) >= 0;
      const n = facing ? f.normal : mul(f.normal, -1);
      // tintless: pages whose WebGL boards show the full colour keep it in the fallback too (colour coding parity)
      const color = hex(shade(f.tintless ? mix(rgb(f.color), [255, 255, 255], 0.06) : tint(f.color, f.opacity), n, fogOf(P(f.center)[2]), f.board && !facing));
      const patches = [];
      if (f.board && f.points.length === 4) {
        const m = clamp(Math.round(len(sub(f.points[0], f.points[2])) / 0.5), 3, 12);
        const bil = (u, v) => add(add(mul(f.points[0], (1 - u) * (1 - v)), mul(f.points[1], u * (1 - v))), add(mul(f.points[2], u * v), mul(f.points[3], (1 - u) * v)));
        for (let a = 0; a < m; a++) for (let b = 0; b < m; b++) patches.push([bil(a / m, b / m), bil((a + 1) / m, b / m), bil((a + 1) / m, (b + 1) / m), bil(a / m, (b + 1) / m)]);
      } else patches.push(f.points);
      if (f.board) {
        // board thickness: the far layer, offset away from the viewer, shows as a darker rim
        const off = mul(n, -0.07);
        items.push({ d: Math.min(...patches.map(q => P(centroid(q))[2])) - 0.002, svg: `<polygon points="${pts(f.points.map(p => add(p, off)))}" fill="${hex(mix(rgb(color), [30, 45, 42], 0.32))}" stroke="${hex(mix(rgb(color), [30, 45, 42], 0.45))}" stroke-width="1" data-sd="board-rim"/>` });
      }
      for (const q of patches) { faceCount++; items.push({ d: P(centroid(q))[2], svg: f.xray ? `<polygon points="${pts(q)}" fill="${color}" fill-opacity=".3" stroke="none" data-sd="xray-face"/>` : `<polygon points="${pts(q)}" fill="${color}" stroke="${color}" stroke-width=".8" stroke-linejoin="round" data-sd="face"/>` }); }
      if (ground != null) shadow += `<polygon points="${pts(f.points.map(p => [p[0] - LIGHT[0] * (p[2] - ground) / LIGHT[2], p[1] - LIGHT[1] * (p[2] - ground) / LIGHT[2], ground]))}"/>`;
    });
    items.sort((a, b) => a.d - b.d);
    // 2) edges, rods and points on top, split into visible (solid) and hidden (dimmed, dashed) runs
    const edgeColor = f => hex(mix(tint(f.color, f.opacity), [20, 34, 32], 0.55));
    for (const e of A.edges) {
      const col = edgeColor(polys[e.faces[0]]), k = (kOf(e.a) + kOf(e.b)) / 2, n = clamp(Math.round(len(sub(e.b, e.a)) / 0.2), 4, 28);
      for (const r of runs(e.a, e.b, n, hidden)) {
        if (r.hidden) { hiddenRuns++; top += seg(r.a, r.b, `stroke="${col}" stroke-width="${(1.7 * k).toFixed(2)}" stroke-dasharray="6 5" opacity=".55" data-sd="hidden-edge"`); }
        else { visibleRuns++; vis.push({ d: dm(r.a, r.b), svg: seg(r.a, r.b, `stroke="${col}" stroke-width="${(2.8 * k).toFixed(2)}" stroke-linecap="round" data-sd="edge"`) }); }
      }
    }
    for (const l of lines) {
      const k = (kOf(l.a) + kOf(l.b)) / 2, w = Math.max(2.4, (l.width || 3) * 1.15 * k);
      for (const r of runs(l.a, l.b, 40, hidden)) {
        if (r.hidden) { hiddenRuns++; top += seg(r.a, r.b, `stroke="${l.color}" stroke-width="${(w * 0.6).toFixed(2)}" stroke-dasharray="7 6" opacity=".5" data-sd="hidden-rod"`); }
        else { visibleRuns++; vis.push({ d: dm(r.a, r.b), svg: seg(r.a, r.b, `stroke="${l.color}" stroke-width="${w.toFixed(2)}" stroke-linecap="round" ${l.dash ? `stroke-dasharray="${l.dash}"` : ''} data-sd="rod"`) }); }
      }
      if (ground != null) {
        const sh = p => [p[0] - LIGHT[0] * (p[2] - ground) / LIGHT[2], p[1] - LIGHT[1] * (p[2] - ground) / LIGHT[2], ground];
        shadow += seg(sh(l.a), sh(l.b), `stroke="#000" stroke-width="${(w * 0.8).toFixed(1)}" stroke-linecap="round"`);
      }
    }
    for (const d of dots) {
      const q = P(d.p), r = 5.5 * kOf(d.p);
      const dh = hidden(d.p); if (dh) hiddenDots++;
      top += dh
        ? `<circle cx="${q[0].toFixed(1)}" cy="${q[1].toFixed(1)}" r="${r.toFixed(1)}" fill="#fff" stroke="${d.color}" stroke-width="1.6" stroke-dasharray="3 2" opacity=".6" data-sd="hidden-dot"/>`
        : (vis.push({ d: q[2] + 1e-3, svg: `<circle cx="${q[0].toFixed(1)}" cy="${q[1].toFixed(1)}" r="${r.toFixed(1)}" fill="${d.color}" stroke="#fff" stroke-width="2" data-sd="dot"/>` }), '');
    }
    SD.lastSvg = { faces: faceCount, featureEdges: A.edges.length, visibleRuns, hiddenRuns, hiddenDots, boards: polys.filter(f => f.board).length, groups: A.groups };
    return (frame.groundSvg || '') + (shadow ? `<g opacity=".13" fill="#1d2b28" stroke="none" data-sd="shadow">${shadow}</g>` : '') + items.map(x => x.svg).join('') + `<g data-sd="lines">${top}${vis.sort((a, b) => a.d - b.d).map(x => x.svg).join('')}</g>`;
  }

  /* ---------- corner axis triad (SVG overlay; same in both render paths) ---------- */
  function triadSvg(originPx, dirs, colors, labels) {
    // dirs: 3 screen directions with depth, drawn far-to-near so the near axis sits on top
    const [ox, oy] = originPx, L = 34;
    const ground = `<polygon points="${ox},${oy} ${ox + dirs[0][0] * L * 0.9},${oy + dirs[0][1] * L * 0.9} ${ox + (dirs[0][0] + dirs[1][0]) * L * 0.9},${oy + (dirs[0][1] + dirs[1][1]) * L * 0.9} ${ox + dirs[1][0] * L * 0.9},${oy + dirs[1][1] * L * 0.9}" fill="#dfe8e3" stroke="#b9c9c2" stroke-width="1" opacity=".85"/>`;
    const order = [0, 1, 2].sort((a, b) => dirs[a][2] - dirs[b][2]);
    let out = `<g data-sd="triad" pointer-events="none"><circle cx="${ox}" cy="${oy}" r="48" fill="#ffffffd9" stroke="#d0dcd6"/>` + ground;
    for (const i of order) {
      const [dx, dy, dz] = dirs[i], x = ox + dx * L, y = oy + dy * L, far = dz < 0;
      out += `<line data-axis="${labels[i]}" x1="${ox}" y1="${oy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${colors[i]}" stroke-width="${far ? 3 : 4.5}" stroke-linecap="round" opacity="${far ? 0.55 : 1}"/>` +
        `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${far ? 3 : 4.5}" fill="${colors[i]}" opacity="${far ? 0.55 : 1}"/>` +
        `<text x="${(ox + dx * (L + 12)).toFixed(1)}" y="${(oy + dy * (L + 12) + 5).toFixed(1)}" text-anchor="middle" style="font-size:16px;font-weight:800;fill:${colors[i]};stroke:#fff;stroke-width:3px;paint-order:stroke">${labels[i]}</text>`;
    }
    return out + '</g>';
  }

  /* ================= senior runtime: class Graph + W3 WebGL exit ================= */
  function installSenior() {
    SD.family = 'senior';
    const PERSP = 24; // camera distance in world units: moderate, textbook-like foreshortening
    const viewAngles = view => {
      const yaw = Math.PI - (view.yaw ?? 0.72), pitch = view.pitch ?? 0.45; // see note below
      return { c: Math.cos(yaw), s: Math.sin(yaw), cp: Math.cos(pitch), sp: Math.sin(pitch) };
    };
    /* Right-handed screen map. The original map X = x·cy − y·sy drew the mirror image of its own
       depth order (a left-handed world seen from above; its WebGL twin had inverted NDC depth and
       showed the world from below). Negating X fixes handedness. Using π − yaw puts the default
       viewer at (+x, −y, above) — the corner where the scenes place their dimension lines and
       handles — and keeps drags/arrow keys moving the near side the way the hand moves. */
    Graph.prototype.project = function (v) {
      const { c, s, cp, sp } = viewAngles(this.view);
      const xr = v[0] * c - v[1] * s, yr = v[0] * s + v[1] * c, D = cp * yr + sp * v[2];
      const scale = Math.min(this.w / 12, this.h / 10) * (this.view.zoom || 1), k = PERSP / Math.max(PERSP * 0.25, PERSP - D);
      return [this.w / 2 - xr * scale * k, this.h * 0.55 + (sp * yr - cp * v[2]) * scale * k, D];
    };
    const kAt = (g, p) => { const D = g.project(p)[2]; return g.planar ? 1 : PERSP / Math.max(PERSP * 0.25, PERSP - D); };
    const towardFor = g => {
      if (g.planar) return () => unit([Math.SQRT1_2 / 2, -1, Math.SQRT1_2 / 2]); // 斜二测 viewing direction
      const { c, s, cp, sp } = viewAngles(g.view), E = [cp * s, cp * c, sp];
      return p => sub(mul(E, PERSP), p);
    };
    const depthFor = g => {
      if (!g.planar) return p => g.project(p);
      const t = unit([Math.SQRT1_2 / 2, -1, Math.SQRT1_2 / 2]);
      return p => { const q = g.project(p); return [q[0], q[1], dot(p, t)]; }; // correct 斜二测 depth (+y is away)
    };
    function frameOf(g) {
      const polys = [], lines = [], dots = [];
      for (const o of g.items3) {
        if (o.kind === 'poly') polys.push({ points: o.points, color: o.color, opacity: o.opacity });
        else if (o.kind === 'line') lines.push({ a: o.a, b: o.b, color: o.color, width: o.width, dash: o.dash });
        else dots.push({ p: o.p, color: o.color });
      }
      return { polys, lines, dots };
    }
    function groundZ(frame) {
      let z = 0;
      for (const f of frame.polys) for (const p of f.points) z = Math.min(z, p[2]);
      for (const l of frame.lines) z = Math.min(z, l.a[2], l.b[2]);
      for (const d of frame.dots) z = Math.min(z, d.p[2]);
      return z - 0.002;
    }
    function groundGridSvg(g, z0) {
      let out = '<g data-sd="ground" pointer-events="none">';
      for (let i = -6; i <= 6; i++) {
        const strong = i === 0;
        for (const [a, b] of [[[i, -6, z0], [i, 6, z0]], [[-6, i, z0], [6, i, z0]]]) {
          const p = g.project(a), q = g.project(b);
          out += `<line x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}" stroke="${strong ? '#bcccc5' : '#dde6e1'}" stroke-width="${strong ? 1.4 : 1}"/>`;
        }
      }
      return out + '</g>';
    }
    function triadFor(g) {
      if (g.planar) return '';
      const o = g.project([0, 0, 0]), dirs = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map(a => {
        const q = g.project(a), dx = q[0] - o[0], dy = q[1] - o[1], l = Math.hypot(dx, dy) || 1;
        return [dx / l, dy / l, q[2] - o[2]];
      });
      return triadSvg([58, g.h - 58], dirs, ['#bd652c', '#0b8068', '#286faa'], ['x', 'y', 'z']);
    }
    // Author-dashed rods (e.g. the dashed vertical height) stay dashed in WebGL too.
    const origLine3 = Graph.prototype.line3;
    Graph.prototype.line3 = function (a, b, color, width, dash, label) {
      const out = origLine3.apply(this, arguments);
      if (this.gl3 && dash) { const L = this.gl3.frame.lines; if (L.length) L[L.length - 1].dash = dash; }
      return out;
    };
    // Remember each 3D label's anchor so labels of hidden points can be dimmed.
    const origLabel3 = Graph.prototype.label3;
    Graph.prototype.label3 = function (p) {
      const n = this.labels.length, out = origLabel3.apply(this, arguments);
      if (this.labels.length > n) this.labels[this.labels.length - 1].p3 = p;
      return out;
    };
    const viewFor = g => {
      if (g.planar) return { dir: unit([Math.SQRT1_2 / 2, -1, Math.SQRT1_2 / 2]) };
      const { c, s, cp, sp } = viewAngles(g.view);
      return { eye: [cp * s * PERSP, cp * c * PERSP, sp * PERSP] };
    };
    const origFinish = Graph.prototype.finish;
    Graph.prototype.finish = function () {
      if (this.space) {
        // 斜二测 (planar) figures are a drawing convention: always drawn by the SVG path.
        const planarGl = !!(this.gl3 && this.planar);
        let frame;
        if (planarGl) {
          const F = this.gl3.frame;
          frame = { polys: F.polys.splice(0), lines: F.lines.splice(0), dots: F.dots.splice(0) };
        } else frame = this.gl3 ? this.gl3.frame : frameOf(this);
        const z0 = this.planar ? null : groundZ(frame);
        const view = viewFor(this), project = depthFor(this);
        let markup = '';
        if (!this.gl3 || planarGl) {
          markup = svgSolids({ ...frame, project, view, toward: towardFor(this), kAt: p => kAt(this, p), ground: z0, groundSvg: z0 == null ? '' : groundGridSvg(this, z0) });
          this.items3.length = 0;
        }
        // Labels whose anchor is hidden behind a face are dimmed (same test in both paths).
        analyze(frame.polys);
        const hid = visibility(frame.polys.filter(f => !f.xray), project, view); // inside an x-ray shell stays readable
        let dimmed = 0;
        for (const l of this.labels) if (l.p3 && hid(l.p3)) { l.color = hex(mix(rgb(l.color), BG, 0.55)); dimmed++; }
        SD.dimmedLabels = dimmed;
        markup += triadFor(this);
        this.buf.splice(1, 0, markup);
        SD.frames++;
        SD.last = { path: this.gl3 ? 'webgl' : 'svg', planar: !!this.planar, polys: frame.polys.length, lines: frame.lines.length, dots: frame.dots.length };
      }
      return origFinish.call(this);
    };

    /* W3 v2: same public API and badge/warning contract as the recorded W3 module. */
    const W3v2 = (function () {
      const FORCED = new URLSearchParams(location.search).get('forceFallback') === '1';
      let renderer = null, scene = null, camera = null, group = null, canvas = null, svgRef = null, key = null, hemi = null;
      let ok = false, dead = FORCED, scene3d = true, shadowOnly = null;
      function probe() {
        try { const c = document.createElement('canvas'); return ['webgl2', 'webgl', 'experimental-webgl'].some(t => !!c.getContext(t)); } catch (e) { return false; }
      }
      function updateUi() {
        const b = document.getElementById('engine-badge'), w = document.getElementById('fallback-warning');
        if (b) { b.hidden = false; b.textContent = !ok ? 'SVG · 投影降级' : (scene3d ? 'Three.js · 真三维' : 'SVG · 平面场景'); }
        if (w) { w.hidden = ok; w.textContent = '当前环境无可用 WebGL，正在显示同一几何数据的可旋转投影替代图；这不代表 WebGL 三维版已经验收。'; }
      }
      function init() {
        if (ok) return true;
        if (dead) { updateUi(); return false; }
        canvas = document.getElementById('plot-gl');
        if (!canvas || !probe()) { dead = true; updateUi(); return false; }
        try {
          renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
          renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
          renderer.setClearColor(0xffffff, 0);
          renderer.shadowMap.enabled = true;
          renderer.shadowMap.type = THREE.PCFSoftShadowMap;
          scene = new THREE.Scene();
          hemi = new THREE.HemisphereLight(0xffffff, 0x9fb1b5, 1.75); scene.add(hemi);
          key = new THREE.DirectionalLight(0xffffff, 2.1);
          key.position.set(LIGHT[0] * 22, LIGHT[1] * 22, LIGHT[2] * 22);
          key.castShadow = true; key.shadow.mapSize.set(1024, 1024);
          Object.assign(key.shadow.camera, { left: -9, right: 9, top: 9, bottom: -9, near: 1, far: 60 });
          key.shadow.bias = -0.0008;
          scene.add(key);
          camera = new THREE.PerspectiveCamera(30, 1, 0.1, 200);
          shadowOnly = new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false });
          group = new THREE.Group(); scene.add(group);
          canvas.addEventListener('webglcontextlost', e => { e.preventDefault(); degrade(); });
          ok = true; updateUi(); return true;
        } catch (err) { ok = false; dead = true; updateUi(); return false; }
      }
      function degrade() {
        ok = false; dead = true;
        try { renderer && renderer.dispose(); } catch (e) {}
        if (canvas) canvas.hidden = true;
        updateUi();
        try { window.__w3Rerender && window.__w3Rerender(); } catch (e) {}
      }
      function place() {
        if (!canvas || !svgRef) return;
        const host = svgRef.offsetParent || svgRef.parentNode, rb = host.getBoundingClientRect(), rs = svgRef.getBoundingClientRect();
        Object.assign(canvas.style, { left: (rs.left - rb.left - host.clientLeft) + 'px', top: (rs.top - rb.top - host.clientTop) + 'px', width: rs.width + 'px', height: rs.height + 'px' });
      }
      function syncUnderlay(on) { if (svgRef) svgRef.classList.toggle('gl-on', !!(on && ok)); }
      function clearGroup() {
        while (group.children.length) {
          const c = group.children.pop();
          c.traverse && c.traverse(o => { o.geometry && o.geometry.dispose(); const m = o.material; if (m) (Array.isArray(m) ? m : [m]).forEach(x => x.dispose()); });
        }
      }
      // Materials ---------------------------------------------------------------
      const backTone = m => { m.onBeforeCompile = sh => { sh.fragmentShader = sh.fragmentShader.replace('#include <color_fragment>', '#include <color_fragment>\n if(!gl_FrontFacing) diffuseColor.rgb *= 0.72;'); }; m.customProgramCacheKey = () => 'sd-backtone'; return m; };
      const dashed = (m, period, on) => {
        m.onBeforeCompile = sh => {
          sh.vertexShader = 'varying float vSdAlong;\n' + sh.vertexShader.replace('#include <begin_vertex>', '#include <begin_vertex>\n vSdAlong = position.y;');
          sh.fragmentShader = 'varying float vSdAlong;\n' + sh.fragmentShader.replace('void main() {', `void main() {\n if (mod(vSdAlong + 1000.0, ${period.toFixed(3)}) > ${on.toFixed(3)}) discard;`);
        };
        m.customProgramCacheKey = () => 'sd-dash-' + period.toFixed(3) + '-' + on.toFixed(3);
        return m;
      };
      const ghostMat = color => dashed(new THREE.MeshBasicMaterial({ color: new THREE.Color(hex(mix(rgb(color), BG, 0.25))), transparent: true, opacity: 0.55, depthWrite: false, depthFunc: THREE.GreaterDepth }), 0.26, 0.15);
      // Geometry builders ---------------------------------------------------------
      function tube(a, b, r, color, opts = {}) {
        const A = new THREE.Vector3(...a), B = new THREE.Vector3(...b), d = B.clone().sub(A), l = d.length();
        if (!(l > 1e-6)) return;
        const geo = new THREE.CylinderGeometry(r, r, l, 14, 1, false);
        const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), d.clone().normalize());
        const mid = A.clone().add(B).multiplyScalar(0.5);
        let mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(hex(rgb(color))), roughness: 0.42, metalness: 0.06 });
        if (opts.dash) mat = dashed(mat, opts.dash[0] + opts.dash[1], opts.dash[0]);
        const m = new THREE.Mesh(geo, mat);
        m.position.copy(mid); m.quaternion.copy(q); m.castShadow = true; m.userData.role = opts.role || 'edge'; m.layers.set(2);
        group.add(m);
        const g = new THREE.Mesh(new THREE.CylinderGeometry(r * 0.72, r * 0.72, l, 10, 1, false), ghostMat(color));
        // Identical geometry: the ghost only survives GreaterDepth where OTHER geometry covers it.
        g.geometry.dispose(); g.geometry = geo.clone();
        g.position.copy(mid); g.quaternion.copy(q); g.renderOrder = 5; g.userData.role = 'ghost'; g.layers.set(1);
        group.add(g);
      }
      function faceMesh(f, color) {
        const pts = f.flip ? [...f.points].reverse() : f.points, verts = [];
        for (let i = 1; i < pts.length - 1; i++) verts.push(...pts[0], ...pts[i], ...pts[i + 1]);
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
        geo.computeVertexNormals();
        const m = new THREE.Mesh(geo, backTone(new THREE.MeshStandardMaterial({ color: new THREE.Color(hex(color)), roughness: 0.78, metalness: 0, flatShading: true, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1, transparent: !!f.xray, opacity: f.xray ? 0.3 : 1 })));
        m.castShadow = !f.xray; m.receiveShadow = true; m.userData.role = f.xray ? 'xray-face' : 'face';
        group.add(m);
      }
      function slab(f, color, t, towardViewer) {
        // The surface the viewer sees lies exactly on the mathematical plane; the thickness
        // extends away from the viewer, so lines lying in the plane stay visible on it.
        const n = dot(f.normal, towardViewer) >= 0 ? f.normal : mul(f.normal, -1);
        const top = f.points.slice(), bot = f.points.map(p => add(p, mul(n, -t))), verts = [];
        const tri = (a, b, c) => verts.push(...a, ...b, ...c);
        for (let i = 1; i < top.length - 1; i++) { tri(top[0], top[i], top[i + 1]); tri(bot[0], bot[i + 1], bot[i]); }
        const rim = [];
        for (let i = 0; i < top.length; i++) { const j = (i + 1) % top.length; rim.push(...bot[i], ...bot[j], ...top[j], ...bot[i], ...top[j], ...top[i]); }
        const mk = (arr, col, role) => {
          const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.Float32BufferAttribute(arr, 3)); geo.computeVertexNormals();
          const m = new THREE.Mesh(geo, backTone(new THREE.MeshStandardMaterial({ color: new THREE.Color(hex(col)), roughness: 0.8, metalness: 0, flatShading: true, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 })));
          m.castShadow = true; m.receiveShadow = true; m.userData.role = role; m.userData.thickness = t; group.add(m);
        };
        mk(verts, color, 'board');
        mk(rim, mix(color, [30, 45, 42], 0.35), 'board-rim');
      }
      function groundMeshes(z0) {
        const grid = [];
        for (let i = -6; i <= 6; i++) grid.push(i, -6, z0, i, 6, z0, -6, i, z0, 6, i, z0);
        const gg = new THREE.BufferGeometry(); gg.setAttribute('position', new THREE.Float32BufferAttribute(grid, 3));
        const lines = new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0xd3ddd8 }));
        lines.userData.role = 'ground'; group.add(lines);
        const axes = new THREE.BufferGeometry(); axes.setAttribute('position', new THREE.Float32BufferAttribute([0, -6, z0 + 0.001, 0, 6, z0 + 0.001, -6, 0, z0 + 0.001, 6, 0, z0 + 0.001], 3));
        const al = new THREE.LineSegments(axes, new THREE.LineBasicMaterial({ color: 0xaebfb8 })); al.userData.role = 'ground'; group.add(al);
        const plane = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.ShadowMaterial({ opacity: 0.17 }));
        plane.position.z = z0 - 0.001; plane.receiveShadow = true; plane.userData.role = 'ground'; group.add(plane);
      }
      function attach(painter) {
        svgRef = painter.svg;
        if (!init()) { if (canvas) canvas.hidden = true; updateUi(); return null; }
        place(); canvas.hidden = false; updateUi();
        const frame = { polys: [], lines: [], dots: [] };
        return {
          painter, frame,
          line3(a, b, color, width) { frame.lines.push({ a, b, color, width }); },
          polygon3(points, color, opacity) { if (points && points.length >= 3) frame.polys.push({ points, color, opacity }); },
          dot3(p, color) { frame.dots.push({ p, color }); },
          setVisible(v) { const on = !!v && ok && !painter.planar; scene3d = !!v && !painter.planar; if (canvas) canvas.hidden = !on; syncUnderlay(on); updateUi(); },
          render(view, w, h) {
            if (!ok || painter.planar) return;
            place(); syncUnderlay(true);
            renderer.setSize(w, h, false);
            clearGroup();
            const scale = Math.min(w / 12, h / 10) * (view.zoom || 1);
            const { c, s, cp, sp } = viewAngles(view), E = [cp * s, cp * c, sp], U = [-sp * s, -sp * c, cp];
            camera.position.set(E[0] * PERSP, E[1] * PERSP, E[2] * PERSP);
            camera.up.set(U[0], U[1], U[2]);
            camera.lookAt(0, 0, 0);
            camera.fov = 2 * Math.atan((h / 2) / (scale * PERSP)) * 180 / Math.PI;
            camera.aspect = w / h;
            camera.setViewOffset(w, h, 0, -0.05 * h, w, h); // principal point at (w/2, 0.55h), as project()
            camera.updateProjectionMatrix(); camera.updateMatrixWorld(true);
            scene.fog = new THREE.Fog(0xf7f9f6, PERSP - 1, PERSP + 28);
            const A = analyze(frame.polys);
            const k = p => PERSP / Math.max(PERSP * 0.25, PERSP - dot(p, E)), px = 1 / scale;
            for (const f of frame.polys) { const col = tint(f.color, f.opacity); if (f.board) slab(f, col, 0.07, sub(mul(E, PERSP), f.center)); else faceMesh(f, col); }
            for (const e of A.edges) {
              const f = frame.polys[e.faces[0]], col = mix(tint(f.color, f.opacity), [20, 34, 32], 0.55);
              tube(e.a, e.b, 1.5 * px, hex(col), { role: 'edge' });
            }
            for (const l of frame.lines) {
              const r = clamp((l.width || 3) * 0.62 * px, 0.028, 0.12);
              const d = l.dash ? String(l.dash).split(/[ ,]+/).map(Number).filter(x => x > 0) : null;
              tube(l.a, l.b, r, l.color, { role: 'rod', dash: d && d.length ? [d[0] * px * 1.6, (d[1] || d[0]) * px * 1.6] : null });
            }
            for (const d of frame.dots) {
              const m = new THREE.Mesh(new THREE.SphereGeometry(0.12, 22, 16), new THREE.MeshStandardMaterial({ color: new THREE.Color(hex(rgb(d.color))), roughness: 0.3, metalness: 0.08 }));
              m.position.set(...d.p); m.castShadow = true; m.userData.role = 'dot'; m.layers.set(2); group.add(m);
              const gh = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 12), new THREE.MeshBasicMaterial({ color: new THREE.Color(hex(rgb(d.color))), transparent: true, opacity: 0.38, depthWrite: false, depthFunc: THREE.GreaterDepth }));
              gh.position.set(...d.p); gh.renderOrder = 5; gh.userData.role = 'ghost'; gh.layers.set(1); group.add(gh);
            }
            groundMeshes(groundZ(frame));
            // Three passes: faces (+ground, shadows) -> ghosts tested against FACE depth only ->
            // rods/edges/points with normal depth. Hidden parts show through faces, never through
            // another visible rod, so crossings read correctly.
            key.layers.enableAll(); hemi.layers.enableAll(); key.shadow.camera.layers.enableAll();
            // shadow-only pre-pass (faces AND rods cast shadows; nothing is drawn)
            renderer.autoClear = true; renderer.shadowMap.autoUpdate = true;
            camera.layers.set(0); camera.layers.enable(2);
            scene.overrideMaterial = shadowOnly; renderer.render(scene, camera); scene.overrideMaterial = null;
            renderer.autoClear = false; renderer.shadowMap.autoUpdate = false;
            camera.layers.set(0); renderer.render(scene, camera);
            camera.layers.set(1); renderer.render(scene, camera);
            camera.layers.set(2); renderer.render(scene, camera);
            renderer.autoClear = true; renderer.shadowMap.autoUpdate = true; camera.layers.set(0);
            SD.lastGl = { faces: frame.polys.filter(f => !f.board).length, boards: frame.polys.filter(f => f.board).length, featureEdges: A.edges.length, rods: frame.lines.length, dots: frame.dots.length };
            frame.polys.length = 0; frame.lines.length = 0; frame.dots.length = 0;
          },
        };
      }
      return {
        attach, available: () => ok, updateUi,
        debug: () => ({ ok, dead, canvas, group, renderer, camera, scene, version: 2 }),
      };
    })();
    W3 = W3v2; // eslint-disable-line no-global-assign
    SD.w3 = W3v2;
    SD.project = (view, w, h, p) => Graph.prototype.project.call({ view, w, h }, p);
  }

  /* ---------- shared WebGL kit (primary/junior adapters) ---------- */
  function glKit(group) {
    const backTone = m => { m.onBeforeCompile = sh => { sh.fragmentShader = sh.fragmentShader.replace('#include <color_fragment>', '#include <color_fragment>\n if(!gl_FrontFacing) diffuseColor.rgb *= 0.72;'); }; m.customProgramCacheKey = () => 'sd-backtone'; return m; };
    const dashed = (m, period, on) => {
      m.onBeforeCompile = sh => {
        sh.vertexShader = 'varying float vSdAlong;\n' + sh.vertexShader.replace('#include <begin_vertex>', '#include <begin_vertex>\n vSdAlong = position.y;');
        sh.fragmentShader = 'varying float vSdAlong;\n' + sh.fragmentShader.replace('void main() {', `void main() {\n if (mod(vSdAlong + 1000.0, ${period.toFixed(3)}) > ${on.toFixed(3)}) discard;`);
      };
      m.customProgramCacheKey = () => 'sd-dash-' + period.toFixed(3) + '-' + on.toFixed(3);
      return m;
    };
    const col = c => new THREE.Color(hex(Array.isArray(c) ? c : rgb(c)));
    return {
      tube(a, b, r, color, o = {}) {
        const A = new THREE.Vector3(...a), B = new THREE.Vector3(...b), d = B.clone().sub(A), l = d.length();
        if (!(l > 1e-6)) return;
        const geo = new THREE.CylinderGeometry(r, r, l, 14, 1, false);
        const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), d.clone().normalize()), mid = A.clone().add(B).multiplyScalar(0.5);
        const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: col(color), roughness: 0.42, metalness: 0.06 }));
        m.position.copy(mid); m.quaternion.copy(q); m.castShadow = true; m.userData.role = o.role || 'edge'; m.layers.set(2); group.add(m);
        // identical geometry: the ghost only survives GreaterDepth where a FACE covers the edge
        const g = new THREE.Mesh(geo.clone(), dashed(new THREE.MeshBasicMaterial({ color: col(mix(rgb(color), BG, 0.25)), transparent: true, opacity: 0.55, depthWrite: false, depthFunc: THREE.GreaterDepth }), o.period || 0.26, o.on || 0.15));
        g.position.copy(mid); g.quaternion.copy(q); g.renderOrder = 5; g.userData.role = 'ghost'; g.layers.set(1); group.add(g);
      },
      face(points, color, o = {}) {
        const pts = o.flip ? [...points].reverse() : points, verts = [];
        for (let i = 1; i < pts.length - 1; i++) verts.push(...pts[0], ...pts[i], ...pts[i + 1]);
        const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3)); geo.computeVertexNormals();
        const glass = (o.opacity ?? 1) < 0.95;
        const m = new THREE.Mesh(geo, backTone(new THREE.MeshStandardMaterial({ color: col(color), roughness: 0.8, metalness: 0, flatShading: true, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1, transparent: glass, opacity: glass ? Math.max(0.16, o.opacity) : 1, emissive: new THREE.Color(o.emissive || 0x000000), emissiveIntensity: o.emissive ? 0.22 : 0 })));
        m.castShadow = !glass; m.receiveShadow = true; m.userData.role = glass ? 'glass-face' : (o.role || 'face'); m.userData.ref = o.ref;
        group.add(m); return m;
      },
      ground(z0, cx, cy, extent, step) {
        const grid = [];
        for (let v = -extent; v <= extent + 1e-9; v += step) grid.push(cx + v, cy - extent, z0, cx + v, cy + extent, z0, cx - extent, cy + v, z0, cx + extent, cy + v, z0);
        const gg = new THREE.BufferGeometry(); gg.setAttribute('position', new THREE.Float32BufferAttribute(grid, 3));
        const lines = new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0xd3ddd8 })); lines.userData.role = 'ground'; group.add(lines);
        const plane = new THREE.Mesh(new THREE.PlaneGeometry(extent * 6, extent * 6), new THREE.ShadowMaterial({ opacity: 0.17 }));
        plane.position.set(cx, cy, z0 - 0.001); plane.receiveShadow = true; plane.userData.role = 'ground'; group.add(plane);
      },
      // faces (+ground, shadows) -> ghosts against FACE depth only -> edges/rods with normal depth
      render(renderer, scene, camera, lights, shadowOnly) {
        lights.forEach(l => { l.layers.enableAll(); if (l.shadow) l.shadow.camera.layers.enableAll(); });
        const ac = renderer.autoClear;
        renderer.autoClear = true; renderer.shadowMap.autoUpdate = true;
        camera.layers.set(0); camera.layers.enable(2);
        scene.overrideMaterial = shadowOnly; renderer.render(scene, camera); scene.overrideMaterial = null;
        renderer.autoClear = false; renderer.shadowMap.autoUpdate = false;
        camera.layers.set(0); renderer.render(scene, camera);
        camera.layers.set(1); renderer.render(scene, camera);
        camera.layers.set(2); renderer.render(scene, camera);
        renderer.autoClear = ac; renderer.shadowMap.autoUpdate = true; camera.layers.set(0);
      },
    };
  }
  function makeLights(scene) {
    const hemi = new THREE.HemisphereLight(0xffffff, 0x9fb1b5, 1.75); scene.add(hemi);
    const key = new THREE.DirectionalLight(0xffffff, 2.1);
    key.castShadow = true; key.shadow.mapSize.set(1024, 1024); key.shadow.bias = -0.0008; scene.add(key); scene.add(key.target);
    return { hemi, key, shadowOnly: new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false }) };
  }
  function aimKey(key, center, R) {
    key.target.position.set(...center);
    key.position.set(center[0] + LIGHT[0] * R * 4, center[1] + LIGHT[1] * R * 4, center[2] + LIGHT[2] * R * 4);
    Object.assign(key.shadow.camera, { left: -R * 1.8, right: R * 1.8, top: R * 1.8, bottom: -R * 1.8, near: 0.1, far: R * 10 });
    key.shadow.camera.updateProjectionMatrix();
  }
  function groundSvgFor(P, z0, cx, cy, extent, step) {
    let out = '<g data-sd="ground" pointer-events="none">';
    for (let v = -extent; v <= extent + 1e-9; v += step) {
      const strong = Math.abs(v) < 1e-9;
      for (const [a, b] of [[[cx + v, cy - extent, z0], [cx + v, cy + extent, z0]], [[cx - extent, cy + v, z0], [cx + extent, cy + v, z0]]]) {
        const p = P(a), q = P(b);
        out += `<line x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}" stroke="${strong ? '#bcccc5' : '#dde6e1'}" stroke-width="${strong ? 1.4 : 1}"/>`;
      }
    }
    return out + '</g>';
  }
  const shadowOf = (p, z0) => [p[0] - LIGHT[0] * (p[2] - z0) / LIGHT[2], p[1] - LIGHT[1] * (p[2] - z0) / LIGHT[2], z0];

  /* ================= primary runtime (P10): object `Space` + App ================= */
  function installPrimary() {
    SD.family = 'primary';
    const S = Space, base = { init: S.init, projectData: S.projectData };
    const PC = [400, 235]; // the original camera target maps here; perspective scales about it
    S.init = function () {
      const first = !this.tried;
      base.init.call(this);
      if (first && this.webgl && this.renderer) {
        this.scene.children.filter(o => o.isLight).forEach(l => this.scene.remove(l));
        Object.assign(this, makeLights(this.scene));
        this.camera = new THREE.PerspectiveCamera(30, 800 / 470, 0.05, 500);
        this.renderer.shadowMap.enabled = true;
        this.kit = glKit(this.model);
      }
    };
    S.projectData = function (faces, a, opts = {}) {
      const pd = base.projectData.call(this, faces, a, opts);
      const fit = opts.fitPoints || faces.flatMap(f => f.points);
      const ext = (fit.length ? fit : [[0, 0, 0]]).reduce((m, p) => m.map((r, i) => [Math.min(r[0], p[i]), Math.max(r[1], p[i])]), [[Infinity, -Infinity], [Infinity, -Infinity], [Infinity, -Infinity]]);
      const R = Math.max(1, 0.5 * Math.hypot(...ext.map(([lo, hi]) => hi - lo)));
      const dist = 4 * R, ortho = pd.project;
      const kAt = p => dist / Math.max(dist * 0.25, dist - (pd.depth(p) - pd.midD));
      return Object.assign(pd, { orthoProject: ortho, dist, R, ext, kAt, project: p => { const q = ortho(p), k = kAt(p); return [PC[0] + (q[0] - PC[0]) * k, PC[1] + (q[1] - PC[1]) * k]; } });
    };
    S.render = function (faces, labels, a, opts = {}) {
      this.init();
      const enabled = this.webgl && !this.forceFallback;
      document.querySelector('#space-canvas').hidden = !enabled;
      document.querySelector('#camera-tools').hidden = false;
      const badge = document.querySelector('#engine-badge'); badge.hidden = false; badge.textContent = enabled ? 'Three.js · 真三维' : 'SVG · 投影降级';
      const warning = document.querySelector('#fallback-warning'); warning.hidden = enabled;
      warning.textContent = '当前环境无可用 WebGL，正在显示同一几何数据的可旋转投影替代图；这不代表 WebGL 三维版已经验收。';
      const pd = this.projectData(faces, a, opts), P = pd.project, selected = a.selected;
      const pair = selected === 'lw' ? ['top', 'bottom'] : selected === 'lh' ? ['front', 'back'] : selected === 'wh' ? ['left', 'right'] : [selected];
      const unit3 = 1 / pd.scale;
      const offX = pd.midX - (pd.centerX - PC[0]) * unit3, offY = pd.midY + (pd.centerY - PC[1]) * unit3;
      const target = [0, 1, 2].map(i => pd.right[i] * offX + pd.up[i] * offY + pd.eye[i] * pd.midD);
      const camPos = add(target, mul(pd.eye, pd.dist));
      const polys = faces.map(f => ({ points: f.points, color: f.color || faceColors[f.baseId || f.id] || '#8aac98', opacity: f.opacity ?? 1, ref: f, sel: pair.includes(f.baseId || f.id) }));
      const A = analyze(polys);
      const z0 = Math.min(...faces.flatMap(f => f.points.map(p => p[2])), 0) - 0.002;
      const cx = (pd.ext[0][0] + pd.ext[0][1]) / 2, cy = (pd.ext[1][0] + pd.ext[1][1]) / 2, gExt = Math.ceil(pd.R * 1.7), gStep = gExt > 12 ? 2 : 1;
      if (enabled) {
        this.disposeModel();
        const cam = this.camera;
        cam.position.set(...camPos); cam.up.set(...pd.up); cam.lookAt(...target);
        cam.fov = 2 * Math.atan(235 * unit3 / pd.dist) * 180 / Math.PI; cam.aspect = 800 / 470; cam.near = pd.dist * 0.05; cam.far = pd.dist * 6;
        cam.updateProjectionMatrix(); cam.updateMatrixWorld(true);
        this.scene.fog = new THREE.Fog(0xf7f9f6, pd.dist * 0.95, pd.dist * 2.3);
        aimKey(this.key, [cx, cy, z0], pd.R * 1.2);
        for (const f of polys) this.kit.face(f.points, f.color, { opacity: f.opacity, flip: f.flip, ref: f.ref, emissive: f.sel ? 0x382a00 : 0 });
        for (const e of A.edges) {
          const sel = e.faces.some(i => polys[i].sel);
          this.kit.tube(e.a, e.b, 1.6 * unit3, sel ? '#ca8c23' : '#375c4a', { role: 'edge', period: 9 * unit3, on: 5 * unit3 });
        }
        this.kit.ground(z0, cx, cy, gExt, gStep);
        const box = document.querySelector('#diagram').getBoundingClientRect();
        this.renderer.setSize(box.width, box.height, false);
        const parent = document.querySelector('#figure-wrap').getBoundingClientRect(), canvas = document.querySelector('#space-canvas');
        Object.assign(canvas.style, { width: box.width + 'px', height: box.height + 'px', left: (box.left - parent.left) + 'px', top: (box.top - parent.top) + 'px' });
        this.kit.render(this.renderer, this.scene, cam, [this.key, this.hemi], this.shadowOnly);
        SD.lastGl = { faces: polys.length, featureEdges: A.edges.length };
      }
      let out = '';
      const all = polys.map((f, i) => ({ f, i, d: f.points.reduce((s, p) => s + pd.depth(p), 0) / f.points.length })).sort((x, y) => x.d - y.d);
      const dmin = all.length ? all[0].d : 0, dmax = all.length ? all[all.length - 1].d : 1;
      if (!enabled) {
        out += groundSvgFor(P, z0, cx, cy, gExt, gStep);
        let sh = '';
        for (const { f } of all) if (f.opacity >= 0.95) sh += `<polygon points="${f.points.map(p => P(shadowOf(p, z0)).map(v => v.toFixed(1)).join(',')).join(' ')}"/>`;
        out += `<g opacity=".13" fill="#1d2b28" data-sd="shadow">${sh}</g>`;
      }
      for (const { f, d } of all) {
        const r = f.ref, ps = f.points.map(P);
        const extra = `${r.noPick || r.clickable === false ? '' : `data-face="${esc(r.baseId || r.id)}"`} data-world-id="${esc(r.id)}" data-sem="${({ top: 'lw', bottom: 'lw', front: 'lh', back: 'lh', left: 'wh', right: 'wh' })[r.baseId || r.id] || esc(r.baseId || r.id)}"`;
        if (enabled) { if (r.clickable !== false) out += polygon(ps, 'transparent', extra); continue; }
        const facing = dot(f.normal, sub(camPos, centroid(f.points))) >= 0, n = facing ? f.normal : mul(f.normal, -1);
        const fill = hex(shade(rgb(f.color), n, (dmax - d) / Math.max(1e-6, dmax - dmin), false));
        out += f.opacity < 0.95
          ? polygon(ps, fill, `${extra} opacity="${Math.max(0.16, f.opacity)}" stroke="none" data-sd="glass-face"`)
          : polygon(ps, fill, `${extra} stroke="${f.sel ? '#c28b23' : fill}" stroke-width="${f.sel ? 3 : 0.8}" stroke-linejoin="round" data-sd="face"`);
      }
      let hiddenPieces = 0;
      if (!enabled) {
        // edges: visible solid on top, hidden dashed and dimmed (same visibility rule as the WebGL depth buffer)
        const hidden = visibility(polys, p => { const q = P(p); return [q[0], q[1], pd.depth(p)]; }, { eye: camPos });
        let hid = '';
        const vis = [];
        for (const e of A.edges) {
          const sel = e.faces.some(i => polys[i].sel), c = sel ? '#c28b23' : '#375c4a', k = (pd.kAt(e.a) + pd.kAt(e.b)) / 2;
          for (const run of runs(e.a, e.b, clamp(Math.round(len(sub(e.b, e.a)) * pd.scale / 18), 4, 28), hidden)) {
            const p = P(run.a), q = P(run.b), attrs = `x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}"`;
            if (run.hidden) { hiddenPieces++; hid += `<line ${attrs} stroke="${c}" stroke-width="${(1.5 * k).toFixed(2)}" stroke-dasharray="6 5" opacity=".55" data-sd="hidden-edge"/>`; }
            else vis.push({ d: (pd.depth(run.a) + pd.depth(run.b)) / 2, s: `<line ${attrs} stroke="${c}" stroke-width="${(2.6 * k).toFixed(2)}" stroke-linecap="round" data-sd="edge"/>` });
          }
        }
        out += `<g data-sd="lines" pointer-events="none">${hid}${vis.sort((x, y) => x.d - y.d).map(x => x.s).join('')}</g>`;
      }
      // corner triad (same in both paths): the model's length / width / height directions
      const o = P(target), dirs = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map(v => { const q = P(add(target, mul(v, pd.R * 0.2))), dx = q[0] - o[0], dy = q[1] - o[1], l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l, dot(v, pd.eye)]; });
      out += triadSvg(opts.overlay ? [56, 104] : [52, 418], dirs, ['#327fc2', '#ce831d', '#8963bb'], ['长', '宽', '高']); // scenes with bottom view cards: top left
      if (opts.faceLabels) {
        for (const f of faces.filter(x => x.points.length === 4 && !x.id.includes('unit'))) {
          const pts = f.points, center = pts[0].map((_, i) => pts.reduce((s, p) => s + p[i], 0) / pts.length), [x, y] = P(center);
          out += rect(x - 32, y - 16, 64, 28, '#ffffffe8', 'rx="6" pointer-events="none"') + tx(x, y + 4, faceNames[f.baseId || f.id] || f.name || f.id, 'tiny', 'middle');
        }
      }
      // Essential dimensions never depend on face occlusion or hover.
      for (const label of labels) {
        const color = label.color || colors[label.key] || colors.A, at = label.anchor || label.start.map((v, i) => (v + label.end[i]) / 2), mid = P(at), pos = label.pos || [mid[0], mid[1] - 30];
        if (label.start && label.end) { const p = P(label.start), q = P(label.end); out += line(...p, ...q, color, 2, `data-sem="${label.key}"`) + dot(...p, 3, color) + dot(...q, 3, color); }
        out += line(mid[0], mid[1], pos[0], pos[1] - 13, color, 1.3, 'stroke-dasharray="3 3"');
        const width = label.width || 210;
        out += rect(pos[0] - width / 2, pos[1] - 30, width, 40, '#ffffffec', `rx="9" stroke="${color}" stroke-opacity=".3" data-sem="${label.key}"`) + tx(pos[0], pos[1] - 4, label.text, 'label', 'middle', `style="fill:${color}"`);
      }
      if (opts.overlay) out += opts.overlay(P, pd);
      this.lastProjection = pd; this.lastFaces = faces; this.lastLabels = labels; this.lastOptions = opts;
      if (a.canvas) out += a.canvas.spaceHandles(labels, a, pd);
      SD.frames++; SD.last = { path: enabled ? 'webgl' : 'svg', polys: polys.length, featureEdges: A.edges.length };
      SD.lastSvg = enabled ? null : { faces: polys.length, featureEdges: A.edges.length, hiddenRuns: hiddenPieces, hiddenDots: 0 };
      return out;
    };
  }

  /* ================= junior runtime (J17): closure module `Space` + `G3` ================= */
  // J17's Space keeps its camera in a closure, so its public methods are re-implemented here with
  // the same signatures and semantics; only the drawing changes (perspective, opaque shaded faces,
  // tube edges, depth-tested floor grid, shadow, triad). The chapter's own 「显示被遮挡的棱」 toggle
  // (`hidden`) still decides whether occluded edges appear (dashed) or stay hidden.
  function installJunior() {
    SD.family = 'junior';
    const S = Space;
    let camera = null, region = null, P = null, webgl = null, glTried = false, scene = null, model = null, canvas = null, hitFaces = [], lights = null, kit = null, lastSig = '', pending = [], lastShapes = '';
    const raycaster = new THREE.Raycaster();
    function updateEngineBadge() {
      const b = document.getElementById('engine-badge'), w = document.getElementById('fallback-warning'), on = !!webgl;
      if (b) { b.hidden = false; b.textContent = on ? 'Three.js · 真三维' : 'SVG · 投影降级'; }
      if (w) { w.hidden = on; w.textContent = '当前环境无可用 WebGL，正在显示同一几何数据的可旋转投影替代图；这不代表 WebGL 三维版已经验收。'; }
    }
    function setup(plot, v, r, target = [3, 2, 1.5], span = 10) {
      P = plot; region = r; pending = [];
      const aspect = r.w / r.h, half = span * (v.zoom || 1) / 2 * Math.max(1, 1 / aspect);
      const yaw = Number.isFinite(v.yaw) ? v.yaw : 0.65, pitch = Number.isFinite(v.pitch) ? v.pitch : 0.5, dist = span * 2.4;
      camera = new THREE.PerspectiveCamera(2 * Math.atan(half / dist) * 180 / Math.PI, aspect, dist * 0.05, dist * 8);
      camera.up.set(0, 0, 1);
      camera.position.set(target[0] + dist * Math.sin(yaw) * Math.cos(pitch), target[1] - dist * Math.cos(yaw) * Math.cos(pitch), target[2] + dist * Math.sin(pitch));
      camera.lookAt(...target); camera.updateProjectionMatrix(); camera.updateMatrixWorld(true);
      camera.userData = { target, dist, half };
      hitFaces = [];
      canvas = document.getElementById('threeCanvas');
      if (!glTried) {
        glTried = true;
        try {
          const ctx = canvas.getContext('webgl2', { antialias: true, alpha: true }) || canvas.getContext('webgl', { antialias: true, alpha: true });
          if (ctx) {
            webgl = new THREE.WebGLRenderer({ canvas, context: ctx, antialias: true, alpha: true, preserveDrawingBuffer: true });
            webgl.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2)); webgl.setClearColor(0xffffff, 0); webgl.shadowMap.enabled = true;
            scene = new THREE.Scene(); lights = makeLights(scene); model = new THREE.Group(); scene.add(model); kit = glKit(model);
            canvas.addEventListener('webglcontextlost', e => { e.preventDefault(); webgl = null; canvas.hidden = true; updateEngineBadge(); });
          }
        } catch (e) { webgl = null; }
      }
      canvas.hidden = !webgl;
      updateEngineBadge();
      return camera;
    }
    function project(p) { const v = new THREE.Vector3(...p).project(camera); return { x: region.x + (v.x + 1) / 2 * region.w, y: region.y + (1 - v.y) / 2 * region.h, z: v.z }; }
    function visibleFace(face) { const c = G3.center(face.points), eye = camera.position.toArray(); return G3.dot(face.normal, G3.sub(eye, c)) > 1e-7; }
    function clearGPU() { if (model) while (model.children.length) { const ch = model.children.pop(); ch.traverse(o => { o.geometry && o.geometry.dispose(); o.material && o.material.dispose(); }); } }
    function allEdges(faces) {
      const map = new Map(), key = p => p.map(v => Math.round(v * 1e4)).join(',');
      faces.forEach((f, i) => f.points.forEach((a, j) => {
        const b = f.points[(j + 1) % f.points.length], ka = key(a), kb = key(b); if (ka === kb) return;
        const k = ka < kb ? ka + '|' + kb : kb + '|' + ka, e = map.get(k) || { a, b, faces: [] };
        e.faces.push(i); map.set(k, e);
      }));
      return [...map.values()];
    }
    function renderFaces(faces, { selected = -1, selectedFace = -1, dragName = null, hidden = false, doubleSided = false, labels = false } = {}) {
      const colorOf = f => (f.owner === 'cube:' + selected || (f.face === selectedFace && dragName === 'fold') ? '#e9bd60' : f.color);
      const polys = faces.map(f => ({ points: f.points, color: colorOf(f), opacity: 1, ref: f }));
      analyze(polys);
      const edges = allEdges(faces), eye = camera.position.toArray();
      const px = 2 * camera.userData.half / region.h; // world units per pixel at the target
      const z0 = Math.min(0, ...faces.flatMap(f => f.points.map(p => p[2]))) - 0.002;
      if (webgl) {
        const sig = JSON.stringify([faces.map(f => [f.points, colorOf(f)]), hidden, doubleSided, pending.map(l => [l.a, l.b])]);
        if (sig !== lastSig) {
          clearGPU();
          polys.forEach(f => kit.face(f.points, f.color, { flip: f.flip, ref: f.ref }));
          for (const e of edges) kit.tube(e.a, e.b, 1.3 * px, '#3c646a', { role: 'edge', ghost: hidden, period: 9 * px, on: 5 * px });
          if (!hidden) model.children.filter(o => o.userData.role === 'ghost').forEach(o => { model.remove(o); o.geometry.dispose(); o.material.dispose(); });
          // floor-grid lines the scene drew before the faces: depth-tested reference lines in WebGL
          if (pending.length) {
            const arr = []; for (const l of pending) arr.push(...l.a, ...l.b);
            const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(arr, 3));
            const ls = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0xcfdcd6 })); ls.userData.role = 'ground'; model.add(ls);
          }
          const plane = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.17 }));
          plane.position.z = z0; plane.receiveShadow = true; plane.userData.role = 'ground'; model.add(plane);
          lastSig = sig;
        }
        for (const l of pending) P.shapes = P.shapes.replace(l.svg, ''); // withdrawn from the SVG overlay
        const t = camera.userData.target; aimKey(lights.key, t, camera.userData.dist / 2.4 * 0.8);
        scene.fog = new THREE.Fog(0xf7f9f6, camera.userData.dist * 0.95, camera.userData.dist * 2.2);
        const pw = P.w, ph = P.h;
        webgl.setSize(pw, ph, false); webgl.setScissorTest(true);
        webgl.setViewport(region.x, ph - region.y - region.h, region.w, region.h); webgl.setScissor(region.x, ph - region.y - region.h, region.w, region.h);
        kit.render(webgl, scene, camera, [lights.key, lights.hemi], lights.shadowOnly);
        webgl.setScissorTest(false);
        SD.lastGl = { faces: polys.length, edges: edges.length, ghostsShown: hidden };
      }
      const ordered = polys.map((f, i) => ({ f, i, projected: f.points.map(project), front: visibleFace(f.ref) }))
        .sort((a, b) => G3.center(b.projected.map(p => [p.x, p.y, p.z]))[2] - G3.center(a.projected.map(p => [p.x, p.y, p.z]))[2]);
      const zs = ordered.map(o => G3.center(o.projected.map(p => [p.x, p.y, p.z]))[2]), zmin = Math.min(...zs, 0), zmax = Math.max(...zs, 1e-6);
      let shapes = '';
      if (!webgl) {
        let sh = '';
        for (const o of ordered) sh += `<polygon points="${o.f.points.map(p => { const q = project(shadowOf(p, z0)); return q.x.toFixed(1) + ',' + q.y.toFixed(1); }).join(' ')}"/>`;
        shapes += `<g opacity=".13" fill="#1d2b28" data-sd="shadow">${sh}</g>`;
      }
      for (const [k, o] of ordered.entries()) {
        const f = o.f.ref;
        if (!o.front && !doubleSided) continue;
        const coords = o.projected.map(p => p.x.toFixed(2) + ',' + p.y.toFixed(2)).join(' '), owner = f.owner, index = owner.startsWith('cube:') ? Number(owner.split(':')[1]) : -1, sel = index >= 0 && index === selected, sem = dragName === 'fold' ? 'face' + f.face : owner === 'solid' ? 'solid' : owner;
        const drag = dragName || (index >= 0 ? owner : null);
        const n = o.front ? o.f.normal : mul(o.f.normal, -1);
        const fill = hex(shade(rgb(o.f.color), n, (zs[k] - zmin) / Math.max(1e-6, zmax - zmin), !o.front));
        shapes += `<polygon points="${coords}" fill="${webgl ? 'transparent' : fill}" stroke="${sel ? '#a76419' : webgl ? 'none' : fill}" stroke-width="${sel ? 2.2 : 0.8}" stroke-linejoin="round" ${drag ? `data-drag="${drag}" data-face="${f.face}"` : ''} data-sem="${sem}" data-face-id="${f.id}" data-export-fill="${fill}" data-sd="${webgl ? 'pick' : 'face'}"/>`;
        hitFaces.push({ id: f.id, owner, face: f.face, polygon: o.projected.map(p => [p.x, p.y]) });
        if (labels) { const c = project(G3.center(f.points)); P.addLabel(f.id, f.name + '面 ' + M.num(G3.area(f.points)) + ' cm²', c.x, c.y, { sem: 'face' + f.face, color: C.ink, size: 17, priority: 3, unit: 'cm²', offsets: [[9, 9], [-9, 9], [9, -9]] }); }
      }
      let hiddenPieces = 0;
      if (!webgl) {
        // every cube edge: visible solid on top; occluded pieces dashed only when the chapter asks (hidden)
        const occluders = doubleSided ? polys : polys.filter((f, i) => visibleFace(f.ref));
        const P3 = p => { const q = project(p); return [q.x, q.y, -q.z]; };
        const isHidden = visibility(occluders, P3, { eye });
        let hid = ''; const vis = [];
        for (const e of edges) for (const r of runs(e.a, e.b, clamp(Math.round(len(sub(e.b, e.a)) * 6), 3, 24), isHidden)) {
          const p = project(r.a), q = project(r.b), attrs = `x1="${p.x.toFixed(1)}" y1="${p.y.toFixed(1)}" x2="${q.x.toFixed(1)}" y2="${q.y.toFixed(1)}"`;
          if (r.hidden) { hiddenPieces++; if (hidden) hid += `<line ${attrs} stroke="#6d8992" stroke-width="1.3" stroke-dasharray="4 5" opacity=".75" data-sd="hidden-edge"/>`; }
          else vis.push({ d: -(project(r.a).z + project(r.b).z) / 2, s: `<line ${attrs} stroke="#3c646a" stroke-width="2.2" stroke-linecap="round" data-sd="edge"/>` });
        }
        shapes += `<g data-sd="lines" pointer-events="none">${hid}${vis.sort((a, b) => a.d - b.d).map(x => x.s).join('')}</g>`;
      }
      // corner triad (both paths)
      const t = camera.userData.target, o0 = project(t), dirs = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map(v => { const q = project(add(t, v)), dx = q.x - o0.x, dy = q.y - o0.y, l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l, dot(v, unit(sub(eye, t)))]; });
      shapes += triadSvg([region.x + 48, region.y + region.h - 48], dirs, [C.blue, C.amber, C.purple], ['x', 'y', 'z']);
      P.raw(shapes);
      lastShapes = shapes;
      SD.frames++; SD.last = { path: webgl ? 'webgl' : 'svg', polys: polys.length, edges: edges.length };
      SD.lastSvg = webgl ? null : { faces: polys.length, hiddenRuns: hidden ? hiddenPieces : 0, hiddenDots: 0, occludedPieces: hiddenPieces };
    }
    function line(a, b, options = {}) {
      const p = project(a), q = project(b), s = seg(p.x, p.y, q.x, q.y, options);
      P.raw(s);
      if (webgl) pending.push({ a, b, svg: s });
    }
    function marker(id, p, label, o = {}) {
      const q = project(p);
      if (q.x < region.x + 6 || q.x > region.x + region.w - 6 || q.y < region.y + 6 || q.y > region.y + region.h - 6) { P.offscreen.push(label); return; }
      P.handle(id, q.x, q.y, { handle: o.handle || id, sem: o.sem || id, color: o.color || C.teal, label });
      P.addLabel(id, label, q.x, q.y, { ...o, unit: o.unit || 'cm', priority: 5 });
    }
    function dimension(a, b, label, o = {}) { const p = project(a), q = project(b); P.dimension(p.x, p.y, q.x, q.y, label, { ...o, unit: 'cm' }); }
    function axes(origin = [0, 0, 0], size = 1.4) {
      for (const [d, label, color] of [[[size, 0, 0], 'x 长', C.blue], [[0, size, 0], 'y 宽', C.amber], [[0, 0, size], 'z 高', C.purple]]) {
        const b = G3.add(origin, d); line(origin, b, { color, width: 2.5 });
        const p = project(b); P.raw(txt(p.x + 5, p.y - 5, label, { size: 15, color, halo: true }));
      }
      const p = project(origin); P.raw(txt(p.x - 7, p.y + 18, 'O', { size: 15, color: C.muted }));
    }
    function onPlane(px, py, axis, fixed) {
      if (!camera || !region) return null;
      const mouse = new THREE.Vector2((px - region.x) / region.w * 2 - 1, 1 - (py - region.y) / region.h * 2), normal = new THREE.Vector3(...(axis === 'xy' ? [0, 0, 1] : axis === 'xz' ? [0, 1, 0] : [1, 0, 0]));
      raycaster.setFromCamera(mouse, camera);
      if (Math.abs(normal.dot(raycaster.ray.direction)) < 0.04) return null;
      const point = raycaster.ray.intersectPlane(new THREE.Plane(normal, -fixed), new THREE.Vector3());
      return point ? point.toArray() : null;
    }
    function axisVector(axis) { const o = project([0, 0, 0]), q = project(axis === 0 ? [1, 0, 0] : axis === 1 ? [0, 1, 0] : [0, 0, 1]); return { x: q.x - o.x, y: q.y - o.y }; }
    function disable() { const c = document.getElementById('threeCanvas'); if (c) c.hidden = true; }
    Object.assign(S, { setup, project, renderFaces, line, marker, dimension, axes, onPlane, axisVector, disable });
    Object.defineProperty(S, 'mode', { configurable: true, get: () => (webgl ? 'WebGL' : 'SVG兼容渲染') });
    Object.defineProperty(S, 'hitFaces', { configurable: true, get: () => hitFaces });
    Object.defineProperty(S, 'camera', { configurable: true, get: () => camera });
    Object.defineProperty(S, 'region', { configurable: true, get: () => region });
    SD.junior = { get model() { return model; }, get renderer() { return webgl; }, get camera() { return camera; }, get lastShapes() { return lastShapes; } };
  }

  /* ================= H16 runtime: H16Space camera + retained SVG paper box ================= */
  // SVG only (no WebGL in H16). After each render the box faces are re-toned from the same world
  // geometry the chapter exposes (getSolidGeometry): fixed-light shading, a distinct tone for the
  // inside of the open box, depth-scaled edges, dashed hidden edges, contact shadow and triad.
  // The paper walls are NOT thickened: the chapter's volume model treats the card as zero-thickness.
  function installH16() {
    SD.family = 'h16';
    const H = window.H16Space, baseCamera = H.camera;
    const L16 = unit([0.42, 0.9, 0.3]); // y is up in H16
    let cam = null;
    // H16 labels this panel 「三维正交观察」 and reads dimensions from it, so the projection stays
    // orthographic; depth is carried by shading, inside/outside tone, shadow, hidden edges and triad.
    H.camera = function (rect, view) {
      const c = baseCamera(rect, view);
      c.kAt = () => 1;
      cam = c; SD.h16cam = c;
      return c;
    };
    const svg = document.getElementById('chart');
    if (!svg) return;
    const layer = name => svg.querySelector(`g[data-layer="${name}"]`);
    let raf = 0;
    function restyle() {
      raf = 0;
      svg.querySelectorAll('[data-sd]').forEach(n => { if (n.parentNode && n.dataset.sd !== 'keep') n.remove(); });
      const geo = window.H16Courseware && H16Courseware.getSolidGeometry && H16Courseware.getSolidGeometry();
      if (!cam || !geo || !geo.length) return;
      const polys = [];
      for (const o of geo) for (const f of o.faces) {
        const node = svg.querySelector(`[data-node="solid-${o.id}-${f.face}"]`);
        if (!node || node.style.display === 'none') continue;
        const cur = node.getAttribute('fill');
        if (cur !== node.dataset.sdShaded) node.dataset.sdBase = cur;
        polys.push({ points: f.vertices, node, color: node.dataset.sdBase, opacity: 1 });
      }
      if (!polys.length) return;
      analyze(polys);
      const P3 = p => { const q = cam.project(p); return [q.x, q.y, q.depth]; };
      const depths = polys.map(f => dot(f.center, cam.forward)), dmin = Math.min(...depths), dmax = Math.max(...depths);
      polys.forEach((f, i) => {
        const outside = dot(f.normal, cam.forward) >= 0, n = outside ? f.normal : mul(f.normal, -1);
        const lam = 0.66 + 0.36 * Math.max(0, dot(n, L16));
        const c = mix(rgb(f.color).map(v => v * lam * (outside ? 1 : 0.66)), BG, 0.12 * (dmax - depths[i]) / Math.max(1e-6, dmax - dmin));
        const fill = hex(c);
        f.node.setAttribute('fill', fill); f.node.dataset.sdShaded = fill; f.node.dataset.sdSide = outside ? 'outside' : 'inside';
        if (f.node.getAttribute('stroke') === '#446553') f.node.setAttribute('stroke-width', (1.4 * cam.kAt(f.center)).toFixed(2));
      });
      // hidden edges (dashed, dimmed) — every box edge piece hidden behind a wall
      const edges = new Map(), key = p => p.map(v => Math.round(v * 1e4)).join(',');
      polys.forEach(f => f.points.forEach((a, j) => { const b = f.points[(j + 1) % f.points.length], k1 = key(a), k2 = key(b), k = k1 < k2 ? k1 + '|' + k2 : k2 + '|' + k1; if (k1 !== k2 && !edges.has(k)) edges.set(k, { a, b }); }));
      const hidden = visibility(polys, P3, { dir: cam.forward });
      let dash = '', pieces = 0;
      for (const e of edges.values()) for (const r of runs(e.a, e.b, 16, hidden)) if (r.hidden) {
        pieces++; const p = cam.project(r.a), q = cam.project(r.b);
        dash += `<line x1="${p.x.toFixed(1)}" y1="${p.y.toFixed(1)}" x2="${q.x.toFixed(1)}" y2="${q.y.toFixed(1)}" stroke="#446553" stroke-width="1.4" stroke-dasharray="6 5" opacity=".6" data-sd="hidden-edge"/>`;
      }
      const NS = svg.namespaceURI, clip = polys[0].node.getAttribute('clip-path');
      const add2 = (layerName, html, tag) => { const g = document.createElementNS(NS, 'g'); g.setAttribute('data-sd', tag); g.setAttribute('pointer-events', 'none'); if (clip) g.setAttribute('clip-path', clip); g.innerHTML = html; (layer(layerName) || svg).appendChild(g); return g; };
      // contact shadow on the ground (y = 0) along the fixed light
      let sh = '';
      for (const f of polys) sh += `<polygon points="${f.points.map(p => { const t = p[1] / L16[1], q = cam.project([p[0] - L16[0] * t, 0, p[2] - L16[2] * t]); return q.x.toFixed(1) + ',' + q.y.toFixed(1); }).join(' ')}"/>`;
      add2('grid', `<g opacity=".14" fill="#1d2b28">${sh}</g>`, 'shadow');
      add2('marks', dash, 'hidden-edges');
      const o = cam.project([0, 0, 0]), dirs = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map(v => { const q = cam.project(mul(v, 4)), dx = q.x - o.x, dy = q.y - o.y, l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l, dot(v, cam.forward)]; });
      const r = cam.hitRect || cam.rect;
      add2('marks', triadSvg([r.x + 50, r.y + 92], dirs, ['#bd652c', '#286faa', '#0b8068'], ['x', 'y', 'z']), 'triad-wrap');
      SD.frames++; SD.last = { path: 'svg', polys: polys.length };
      SD.lastSvg = { faces: polys.length, hiddenRuns: pieces, hiddenDots: 0, insideFaces: polys.filter(f => f.node.dataset.sdSide === 'inside').length };
    }
    new MutationObserver(ms => {
      const ours = m => [...m.addedNodes, ...m.removedNodes].every(n => n.nodeType !== 1 || n.hasAttribute('data-sd'));
      if (ms.some(m => (m.type === 'attributes' && (m.target.getAttribute('data-node') || '').startsWith('solid-')) || (m.type === 'childList' && !ours(m)))) {
        if (!raf) raf = requestAnimationFrame(restyle);
      }
    }).observe(svg, { subtree: true, childList: true, attributes: true, attributeFilter: ['d'] });
  }

  /* ================= teacher-made 空间几何关系 (global functions panel/rod/label/draw) ================= */
  // Loaded after the page's own script: replaces how rods/boards/labels are DRAWN and rebuilds once.
  // Same relation data, same camera path (yaw/pitch, distance 7, fov 38). WebGL: tube frames instead of
  // hairline EdgesGeometry, three-pass hidden parts, ground grid, triad. SVG fallback: the same
  // perspective camera through THREE's own projection (no WebGL context needed), thick boards,
  // shaded opaque panels, dashed hidden parts, dimmed hidden labels, ground grid, shadow, triad.
  function installSpacePage() {
    SD.family = 'space-page';
    // world is y-up here: Zup maps (x, y_up, z) -> (x, -z, y_up) (a +90° turn about x), Yup is its inverse
    const Zup = p => [p[0], -p[2], p[1]], Yup = p => [p[0], p[2], -p[1]];
    const FLOOR = -1.55;
    let kitS = null, lightsS = null, extra = null;
    function camFor(w, h) {
      const c = new THREE.PerspectiveCamera(38, w / h, 0.1, 100);
      c.position.set(7 * Math.sin(yaw) * Math.cos(pitch), 7 * Math.sin(pitch), 7 * Math.cos(yaw) * Math.cos(pitch));
      c.lookAt(0, 0, 0); c.updateProjectionMatrix(); c.updateMatrixWorld(true);
      return c;
    }
    const projectWith = (c, w, h) => p => { const v = new THREE.Vector3(p[0], p[1], p[2]).project(c); return [(v.x + 1) / 2 * w, (1 - v.y) / 2 * h, -v.z]; };
    function triadMarkup(c, w, h) {
      const P = projectWith(c, w, h), o = P([0, 0, 0]), camDir = c.position.clone().normalize();
      const dirs = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map(v => { const q = P(v), dx = q[0] - o[0], dy = q[1] - o[1], l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l, v[0] * camDir.x + v[1] * camDir.y + v[2] * camDir.z]; });
      return triadSvg([54, h - 54], dirs, ['#b44c47', '#2f7d5f', '#316f99'], ['x', 'y', 'z']);
    }
    function overlay(w, h) {
      if (!extra) {
        extra = document.createElementNS(fallbackSvg.namespaceURI, 'svg');
        extra.setAttribute('data-sd', 'overlay');
        extra.style.cssText = 'position:absolute;pointer-events:none;z-index:4';
        canvas.parentNode.insertBefore(extra, fallbackSvg.nextSibling);
      }
      Object.assign(extra.style, { left: canvas.offsetLeft + 'px', top: canvas.offsetTop + 'px', width: w + 'px', height: h + 'px' });
      extra.setAttribute('viewBox', `0 0 ${w} ${h}`);
      return extra;
    }
    if (!svgMode && renderer) {
      kitS = glKit(model);
      scene.children.filter(o => o.isLight).forEach(l => scene.remove(l));
      lightsS = makeLights(scene);
      aimKey(lightsS.key, [0, FLOOR, 0], 3);
      lightsS.key.position.set(LIGHT[0] * 12, LIGHT[2] * 12, -LIGHT[1] * 12); // same world light, y-up
      const grid = []; for (let i = -4; i <= 4; i++) grid.push(i, FLOOR + 0.001, -4, i, FLOOR + 0.001, 4, -4, FLOOR + 0.001, i, 4, FLOOR + 0.001, i);
      const gg = new THREE.BufferGeometry(); gg.setAttribute('position', new THREE.Float32BufferAttribute(grid, 3));
      const gl = new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0xcfdcd6 })); gl.userData.role = 'ground'; scene.add(gl);
    }
    // --- drawing primitives (WebGL path) ---
    const origRod = rod, origPanel = panel, origLabel = label;
    window.rod = rod = function (a, b, color) {
      if (svgMode) return origRod(a, b, color);
      const obj = origRod(a, b, color);
      obj.layers.set(2);
      const g = new THREE.Mesh(obj.geometry.clone(), new THREE.MeshBasicMaterial({ color: new THREE.Color(hex(mix(rgb(colorCss(color)), BG, 0.25))), transparent: true, opacity: 0.55, depthWrite: false, depthFunc: THREE.GreaterDepth }));
      g.userData.role = 'ghost'; g.layers.set(1); g.renderOrder = 5; obj.add(g); g.layers.set(1);
      obj.userData.role = 'rod';
      return obj;
    };
    window.panel = panel = function (color, y = 0, angle = 0) {
      if (svgMode) return origPanel(color, y, angle);
      const obj = origPanel(color, y, angle);
      obj.children.filter(c => c.isLineSegments).forEach(c => { obj.remove(c); c.geometry.dispose(); c.material.dispose(); });
      obj.userData.role = 'board';
      // tube frame on the board's rim (both faces), dark tone of the board colour; hidden parts ghosted
      const hx = 1.7, hy = 0.0225, hz = 1.2, rim = [[-hx, -hz], [hx, -hz], [hx, hz], [-hx, hz]];
      for (const yy of [hy, -hy]) for (let i = 0; i < 4; i++) {
        const a = rim[i], b = rim[(i + 1) % 4], A = new THREE.Vector3(a[0], yy, a[1]), B = new THREE.Vector3(b[0], yy, b[1]), d = B.clone().sub(A);
        const tg = new THREE.CylinderGeometry(0.02, 0.02, d.length(), 10, 1, false);
        const t = new THREE.Mesh(tg, new THREE.MeshStandardMaterial({ color: new THREE.Color(hex(mix(rgb(colorCss(color)), [20, 34, 32], 0.5))), roughness: 0.45 }));
        t.position.copy(A).add(B).multiplyScalar(0.5); t.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), d.normalize()); t.userData.role = 'edge'; t.layers.set(2); obj.add(t);
        const gh = new THREE.Mesh(tg.clone(), new THREE.MeshBasicMaterial({ color: new THREE.Color(hex(mix(rgb(colorCss(color)), BG, 0.25))), transparent: true, opacity: 0.5, depthWrite: false, depthFunc: THREE.GreaterDepth }));
        gh.position.copy(t.position); gh.quaternion.copy(t.quaternion); gh.userData.role = 'ghost'; gh.layers.set(1); gh.renderOrder = 5; obj.add(gh);
      }
      return obj;
    };
    window.label = label = function (text, pos, color) {
      origLabel(text, pos, color);
      if (svgMode) return;
      const sp = model.children[model.children.length - 1];
      if (!sp || !sp.isSprite) return;
      sp.layers.set(2);
      // Redraw the page's 256×96 label canvas as the SVG path draws labels: heavy type with a white halo
      // instead of thin text in a white box (shrunk to ~35 % on screen that read washed out), sRGB-tagged.
      const map = sp.material.map, cc = map && map.image;
      if (cc && cc.getContext) {
        const ctx = cc.getContext('2d');
        ctx.clearRect(0, 0, cc.width, cc.height);
        ctx.font = '800 68px system-ui, "Microsoft YaHei", sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.lineJoin = 'round'; ctx.lineWidth = 16; ctx.strokeStyle = 'rgba(255,255,255,.96)'; ctx.strokeText(text, 128, 50);
        ctx.fillStyle = color || '#254358'; ctx.fillText(text, 128, 50);
        if (THREE.SRGBColorSpace) map.colorSpace = THREE.SRGBColorSpace;
        map.needsUpdate = true;
      }
      sp.material.fog = false; sp.material.needsUpdate = true;
      sp.scale.multiplyScalar(1.4);
    };
    // --- draw: WebGL three-pass, or the rebuilt SVG fallback ---
    window.drawSvg = drawSvg = function () {
      const w = canvas.clientWidth || fallbackSvg.clientWidth || 800, h = canvas.clientHeight || fallbackSvg.clientHeight || 380;
      Object.assign(fallbackSvg.style, { left: canvas.offsetLeft + 'px', top: canvas.offsetTop + 'px', width: w + 'px', height: h + 'px' });
      fallbackSvg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
      const c = camFor(w, h), PY = projectWith(c, w, h), P = p => PY(Yup(p)), eyeZ = Zup(c.position.toArray());
      const kAt = p => { const d = Math.hypot(...sub(eyeZ, p)); return 7 / Math.max(2, d); };
      const polys = [], lines = [], labels = [];
      for (const p of svgPrimitives) {
        if (p.type === 'rod') lines.push({ a: Zup(p.a), b: Zup(p.b), color: p.color, width: 6 });
        else if (p.type === 'panel') {
          const ang = p.angle || 0, ca = Math.cos(ang), sa = Math.sin(ang);
          // wound so the side facing the default (upper) view is the board's front; its underside reads darker
          polys.push({ points: [[-1.7, 1.2], [1.7, 1.2], [1.7, -1.2], [-1.7, -1.2]].map(q => Zup([q[0], p.y - q[1] * sa, q[1] * ca])), color: p.color, opacity: 0.5, tintless: true });
        } else if (p.type === 'label') labels.push(p);
      }
      const z0 = FLOOR;
      const markup = svgSolids({ polys, lines, dots: [], project: P, view: { eye: eyeZ }, toward: q => sub(eyeZ, q), kAt, ground: z0, groundSvg: groundSvgFor(P, z0, 0, 0, 4, 1) });
      analyze(polys);
      const hid = visibility(polys, P, { eye: eyeZ });
      let texts = '';
      for (const l of labels) {
        const q = PY(l.pos), hidden = hid(Zup(l.pos)), col = hidden ? hex(mix(rgb(l.color), BG, 0.55)) : l.color;
        texts += `<text x="${q[0].toFixed(1)}" y="${q[1].toFixed(1)}" fill="${col}" font-size="28" font-weight="800" text-anchor="middle" dominant-baseline="middle" paint-order="stroke" stroke="#fff" stroke-width="7" stroke-linejoin="round">${l.text}</text>`;
      }
      fallbackSvg.innerHTML = markup + texts + triadMarkup(c, w, h);
    };
    // the page registered its resize handler with the original function object: rebind it
    const origDraw = draw;
    window.removeEventListener('resize', origDraw);
    window.addEventListener('resize', () => draw());
    window.draw = draw = function () {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (svgMode) { updateLabels(); drawSvg(); if (extra) extra.innerHTML = ''; return; }
      if (!w || !h) return; // canvas momentarily out of layout (a resize while the lesson layer re-flows)
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.position.set(7 * Math.sin(yaw) * Math.cos(pitch), 7 * Math.sin(pitch), 7 * Math.cos(yaw) * Math.cos(pitch));
      camera.lookAt(0, 0, 0); camera.updateProjectionMatrix();
      updateLabels();
      kitS.render(renderer, scene, camera, [lightsS.key, lightsS.hemi], lightsS.shadowOnly);
      overlay(w, h).innerHTML = triadMarkup(camera, w, h);
      SD.frames++; SD.last = { path: 'webgl' };
    };
    SD.space = { get model() { return model; }, get renderer() { return renderer; }, get camera() { return camera; } };
    build();
  }

  /* ---------- family detection (runs before the chapter's app script) ---------- */
  try {
    if (typeof Graph === 'function' && typeof W3 !== 'undefined' && Graph.prototype.line3) installSenior();
    else if (typeof Space === 'object' && Space && typeof Space.projectData === 'function' && typeof App === 'object' && typeof faceColors === 'object') installPrimary();
    else if (typeof G3 === 'object' && typeof Space === 'object' && Space && typeof Space.renderFaces === 'function') installJunior();
    else if (window.H16Space && typeof window.H16Space.camera === 'function') installH16();
    else if (typeof panel === 'function' && typeof rod === 'function' && typeof drawSvg === 'function' && typeof svgPrimitives !== 'undefined' && document.getElementById('s-fallback')) installSpacePage();
  } catch (e) { SD.error = String(e && e.message || e); }
})();
