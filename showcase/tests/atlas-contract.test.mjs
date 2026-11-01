import assert from "node:assert/strict"
import test from "node:test"

import { read } from "./source.mjs"

const atlas = read("src/components/InkAtlas.tsx")
const targets = read("src/lib/atlas/targets.ts")
const timeline = read("src/lib/atlas/timeline.ts")

test("the ink atlas tells the five steps and labels its illustrative numbers", () => {
  for (const mark of ["读", "判", "汇", "追", "见"]) assert.match(atlas, new RegExp(`mark: "${mark}"`))
  assert.match(atlas, /看不清的不猜/)
  assert.match(atlas, /错题分布为示意数据/)
  assert.match(atlas, /模拟规模实测 0\.63 秒 · 原做法约 1\.5 分钟/)
  assert.match(read("src/lib/atlas/overlay.ts"), /示意数据/)
})

test("one point is one open wrong record: 50 students x 600 records", () => {
  assert.match(targets, /export const STUDENTS = 50/)
  assert.match(targets, /export const RECORDS_PER_STUDENT = 600/)
  assert.match(targets, /export const POINT_COUNT = STUDENTS \* RECORDS_PER_STUDENT/)
  assert.match(targets, /export const DISTINCT_QUESTIONS = 2_000/)
  assert.match(targets, /export const TEMPLATE_QUESTIONS = 25/)
})

test("knowledge points come from the senior curriculum catalog", () => {
  const data = read("src/lib/atlas/curriculum.ts")
  assert.match(data, /references\/math_curricula\/senior\.v1\.json/)
  assert.match(data, /"导数与函数的单调性"/)
  assert.match(data, /"圆锥曲线的综合应用"/)
})

test("the atlas stops when unseen and gives reduced motion finished frames", () => {
  assert.match(atlas, /const running = \(\) => inView && pageVisible && !reduced/)
  assert.match(atlas, /document\.addEventListener\("visibilitychange"/)
  assert.match(atlas, /reduced \? restingFrame\(progress\(\)\) : frameAt\(progress\(\)\)/)
  assert.match(atlas, /motion: reduced \? 0 : 1/)
  assert.match(atlas, /\{!reduced && \(/)
  assert.match(timeline, /export function restingFrame/)
})

test("the point layout is built only when the section comes near", () => {
  assert.match(atlas, /new IntersectionObserver\(\s*\(\[entry\]\) => \{\s*if \(!entry\.isIntersecting\) return\s*approach\.disconnect\(\)/)
  assert.match(atlas, /rootMargin: "150% 0px 150% 0px"/)
})

test("canvases are decorative and the text stays readable without WebGL", () => {
  assert.equal((atlas.match(/<canvas [^>]*aria-hidden="true"/g) ?? []).length, 3)
  assert.match(atlas, /setSupported\(false\)/)
  assert.match(read("src/styles/atlas.css"), /\.atlas\.is-fallback \.atlas-step \{\s*grid-area: auto;\s*opacity: 1;/)
})

test("handwriting uses installed faces only", () => {
  assert.match(targets, /const LATIN_HAND = '"Segoe Print", "Bradley Hand", "Noteworthy", '/)
  assert.doesNotMatch(targets, /@font-face|url\(/)
})
