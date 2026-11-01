import test from 'node:test'
import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

// The lesson-flow layer (引入 → 探究 → 归纳 → 练习) is presentation only: it is
// injected as two exactly reversible blocks, so stripping it must give back the
// catalogued chapter byte for byte. That is how we prove mathematics, scene
// scripts and vendored libraries were not edited by the visual upgrade.
const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..', '..')
const layerDir = path.join(root, 'scripts', 'courseware_lesson_layer')
const manifest = JSON.parse(fs.readFileSync(path.join(layerDir, 'manifest.json'), 'utf8'))
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'courseware', 'catalog_v4.json'), 'utf8'))
const source = name => fs.readFileSync(path.join(layerDir, name), 'utf8').replace(/\r\n/g, '\n').trim()
const css = source('lesson-flow.css')
const js = source('lesson-flow.js')
const solid = source('solid-depth.js')
const sha = data => crypto.createHash('sha256').update(data).digest('hex')
// Pages without explicit </head> / </body> carry the blocks before <main> / </html>.
const STYLE = /<style id="lesson-flow-style">([\s\S]*?)<\/style>(?=<\/head>|<main>)/
const SCRIPT = /<script id="lesson-flow">([\s\S]*?)<\/script>(?=<\/body>|<\/html>)/
// Render-only depth upgrade for 3D solids, injected before the app script (or after it).
const SOLID = /<script id="solid-depth">([\s\S]*?)<\/script>/
const chapters = Object.entries(manifest.chapters)

test('课堂节奏层可逆：去掉后逐字节还原目录登记的章节', () => {
  assert.ok(chapters.length > 0)
  for (const [code, item] of chapters) {
    const file = path.join(root, item.path)
    const html = fs.readFileSync(file, 'utf8')
    assert.equal(html.match(/<style id="lesson-flow-style">/g)?.length, 1, `${code}: one layer style`)
    assert.equal(html.match(/<script id="lesson-flow">/g)?.length, 1, `${code}: one layer script`)
    const base = html.replace(STYLE, '').replace(SCRIPT, '').replace(SOLID, '')
    assert.equal(sha(Buffer.from(base, 'utf8')), item.base_sha256, `${code}: base changed — math/scene code must stay byte-identical`)
    const bytes = fs.readFileSync(file)
    assert.equal(sha(bytes), item.sha256, `${code}: manifest hash`)
    const entry = catalog.entries.find(e => e.code === code)
    if (item.catalog === false) assert.equal(entry, undefined, `${code}: extra page is not a catalog chapter`)
    else assert.equal(entry.sha256, sha(bytes), `${code}: catalog_v4.json sha256 in sync`)
    const publicPath = entry ? entry.publicPath : item.path.replace(/^courseware\//, 'frontend/public/courseware/samples/')
    assert.ok(fs.readFileSync(path.join(root, publicPath)).equals(bytes), `${code}: public sample is byte-identical`)
    assert.equal(!!html.match(SOLID), !!item.solid_depth, `${code}: solid-depth block matches the manifest`)
    if (item.solid_depth) assert.equal(html.match(SOLID)[1], solid, `${code}: carries the reviewed solid-depth source (re-run apply.py)`)
    assert.equal(html.match(STYLE)[1], css, `${code}: carries the reviewed layer CSS (re-run apply.py after editing the layer)`)
    assert.equal(html.match(SCRIPT)[1], js, `${code}: carries the reviewed layer JS (re-run apply.py after editing the layer)`)
  }
  assert.equal(manifest.layer.css_sha256, sha(Buffer.from(css)))
  assert.equal(manifest.layer.js_sha256, sha(Buffer.from(js)))
  assert.equal(manifest.layer.solid_depth_sha256, sha(Buffer.from(solid)))
})

test('三维实体深度升级：只进入含三维渲染器的章节，离线、可解析、不引外链', () => {
  new vm.Script(solid)
  assert.doesNotMatch(solid, /https?:\/\//)
  assert.doesNotMatch(solid, /<\/script/i)
  const withSolid = chapters.filter(([, item]) => item.solid_depth).map(([code]) => code).sort()
  assert.deepEqual(withSolid, ['H09', 'H12', 'H16', 'H21', 'J17', 'P10', 'SPACE'].filter(c => manifest.chapters[c]).sort())
  // Teacher feedback 2026-09-28: no hairline solid edges, occlusion via depth, hidden parts dashed,
  // perspective (except H16, whose panel is labelled orthographic), ground + triad reference.
  for (const hook of ['GreaterDepth', 'CylinderGeometry', 'PerspectiveCamera', 'function visibility', 'triadSvg', 'ShadowMaterial', 'stroke-dasharray="6 5"']) assert.ok(solid.includes(hook), hook)
})

test('课堂节奏层离线、可解析、键盘与减少动态可用，且不写数学状态', () => {
  new vm.Script(js)
  for (const text of [css, js]) {
    assert.doesNotMatch(text, /https?:\/\//)
    assert.doesNotMatch(text, /<\/(?:script|style)/i)
  }
  // Clicker keys, direct stage keys, current-step semantics and inert folding.
  for (const hook of ['PageDown', 'PageUp', "'aria-current', 'step'", '.inert', 'say(']) assert.ok(js.includes(hook), hook)
  assert.match(css, /prefers-reduced-motion:reduce/)
  assert.match(css, /body\.low-motion .lf-card/)
  // Views only: no parameter writes, presets or undo through the runtime.
  assert.doesNotMatch(js, /\.param\(|applyControl|CW\.set|\.replace\(\{|\.undo\(|\.reset\(/)
  // The only runtime call besides reads is the chapter's own scene navigation.
  assert.match(js, /goScene: i => window\.CW\.app\.go\(i\)/)
})

test('P10 恢复为目录登记的 v4 章节：3D 优先且 2D 降级诚实可见', () => {
  const p10 = manifest.chapters.P10
  assert.ok(p10, 'P10 is uplifted')
  assert.match(p10.base_origin, /^archive:MathAgent_小学数学交互课件_完整版_v4\.zip!/)
  const html = fs.readFileSync(path.join(root, p10.path), 'utf8')
  assert.match(html, /WebGLRenderer/)
  assert.match(html, /id="engine-badge"/)
  assert.match(html, /id="fallback-warning"/)
  assert.match(html, /Three\.js · 真三维/)
  assert.match(html, /SVG · 投影降级/)
  assert.match(html, /正在显示同一几何数据的可旋转投影替代图/)
  // Five catalogued scenes, not the single cube/cylinder placeholder page.
  for (const id of ['P10-S01', 'P10-S02', 'P10-S03', 'P10-S04', 'P10-S05']) assert.ok(html.includes(`id:'${id}'`), id)
})

test('H09/H12 恢复为目录登记的高中章节：v4 原章 + W3 WebGL 出口，SVG 投影降级诚实可见', () => {
  const expected = { H09: 5, H12: 4 }
  for (const [code, scenes] of Object.entries(expected)) {
    const item = manifest.chapters[code]
    assert.ok(item, `${code} is uplifted`)
    assert.match(item.base_origin, /^catalog-verified rebuild: v4 archive entry /)
    const html = fs.readFileSync(path.join(root, item.path), 'utf8')
    assert.match(html, /WebGLRenderer/)
    assert.match(html, /var W3 = /)
    assert.match(html, /id="engine-badge"/)
    assert.match(html, /id="fallback-warning"/)
    assert.match(html, /SVG · 投影降级/)
    for (let i = 1; i <= scenes; i++) assert.ok(html.includes(`"id": "${code}-S0${i}"`), `${code}-S0${i}`)
    // Not the generic primary-school cube/cylinder page that had replaced these chapters.
    assert.doesNotMatch(html, /正方体有6个面、12条棱、8个顶点；圆柱体侧面是曲面/)
  }
})
