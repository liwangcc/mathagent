import { ATLAS_CHAPTERS } from "./curriculum"

/* Builds where every ink point sits in each stage, plus the geometry the overlay draws around them.
 * All coordinates are CSS pixels of the sticky stage; everything is seeded, so a layout never
 * reshuffles between renders. 50 students x 600 open records = POINT_COUNT. */

export const STUDENTS = 50
export const RECORDS_PER_STUDENT = 600
export const POINT_COUNT = STUDENTS * RECORDS_PER_STUDENT
export const DISTINCT_QUESTIONS = 2_000
export const TEMPLATE_QUESTIONS = 25

export interface Box {
  x: number
  y: number
  w: number
  h: number
}

export interface Layout {
  width: number
  height: number
  plate: Box
  compact: boolean
}

export interface Fonts {
  hand: string
  serif: string
  body: string
}

export type Verdict = "wrong" | "correct" | "partial" | "pending"

export interface AnswerRow {
  number: string
  baseline: number
  left: number
  /** Right edge of the transcribed answer, where its verdict mark goes. */
  end: number
  verdict: Verdict
}

export interface Star {
  x: number
  y: number
  weight: number
  name: string
}

export interface Constellation {
  name: string
  x: number
  y: number
  radius: number
  stars: number[]
  links: Array<[number, number]>
}

export interface Thread {
  from: { x: number; y: number }
  to: { x: number; y: number }
  similar: boolean
}

export interface Scene {
  layout: Layout
  fonts: Fonts
  paper: Box
  handSize: number
  rows: AnswerRow[]
  tray: Box
  stars: Star[]
  constellations: Constellation[]
  highlights: number[]
  ledger: Box
  disk: { x: number; y: number; r: number }
  template: Array<{ x: number; y: number }>
  threads: Thread[]
  finale: Box
}

/** Per point: [hand.xy, type.xy], [judge.xy, atlas.xy], [ledger.xy, clusters.xy], [finale.xy, atlas glow, matched], [row, seed, write order, scan order]. */
export interface PointBuffers {
  p01: Float32Array
  p23: Float32Array
  p45: Float32Array
  p6w: Float32Array
  meta: Float32Array
}

interface Point {
  x: number
  y: number
}

/* The answer sheet: question 1 is a wrong choice, 2 keeps the student's unsimplified 2/4,
 * 3 is half done, 4 is a smudge nobody should guess at. */
const ANSWERS: Array<{ number: string; hand: string | null; type: string | null; verdict: Verdict }> = [
  { number: "1", hand: "C", type: "C", verdict: "wrong" },
  { number: "2", hand: "2/4", type: "2/4", verdict: "correct" },
  { number: "3", hand: "f'(x)=2x-2，x>1时递增", type: "f′(x) = 2x − 2，x > 1 时递增", verdict: "partial" },
  { number: "4", hand: null, type: null, verdict: "pending" },
]

/* Illustrative wrong-answer counts for a class term; the heavy ones are typical senior-high weak spots. */
const HEAVY_TOPICS: Record<string, number> = {
  导数与函数的单调性: 46,
  圆锥曲线的综合应用: 41,
  导数与函数的极值最值: 39,
  直线与圆锥曲线的位置关系: 34,
  数列求和: 31,
  三角恒等变换: 28,
  函数的单调性: 24,
}

export const FINALE_LINES = ["让每一份作答，", "被清晰看见。"]

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))

function random(seed: number) {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function gaussian(rand: () => number) {
  const u = Math.max(rand(), 1e-9)
  const v = rand()
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
}

/** Every opaque pixel of whatever `draw` paints inside `box`. */
function inkOf(box: Box, draw: (ctx: CanvasRenderingContext2D) => void, step = 1): Point[] {
  const width = Math.max(1, Math.ceil(box.w))
  const height = Math.max(1, Math.ceil(box.h))
  const canvas = document.createElement("canvas")
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext("2d", { willReadFrequently: true })
  if (!ctx) return []
  ctx.translate(-box.x, -box.y)
  ctx.fillStyle = "#000"
  ctx.strokeStyle = "#000"
  draw(ctx)
  const data = ctx.getImageData(0, 0, width, height).data
  const points: Point[] = []
  for (let y = 0; y < height; y += step) {
    for (let x = 0; x < width; x += step) {
      if (data[(y * width + x) * 4 + 3] > 110) points.push({ x: box.x + x, y: box.y + y })
    }
  }
  return points
}

/** `count` points spread evenly over `pool`; repeats are jittered so no two points coincide. */
function spread(pool: Point[], count: number, rand: () => number, jitter = 0.55): Point[] {
  if (!pool.length) return Array.from({ length: count }, () => ({ x: 0, y: 0 }))
  const order = shuffled(pool.length, rand)
  return Array.from({ length: count }, (_, index) => {
    const point = pool[order[index % order.length]]
    const extra = index >= order.length ? 1.6 : 1
    return {
      x: point.x + (rand() - 0.5) * jitter * 2 * extra,
      y: point.y + (rand() - 0.5) * jitter * 2 * extra,
    }
  })
}

const byReadingOrder = (a: Point, b: Point) => a.x - b.x || a.y - b.y

function measure(font: string, text: string) {
  const ctx = document.createElement("canvas").getContext("2d")
  if (!ctx) return text.length * 20
  ctx.font = font
  return ctx.measureText(text).width
}

function shuffled(count: number, rand: () => number) {
  const order = Array.from({ length: count }, (_, index) => index)
  for (let index = count - 1; index > 0; index -= 1) {
    const swap = Math.floor(rand() * (index + 1))
    ;[order[index], order[swap]] = [order[swap], order[index]]
  }
  return order
}

/* Latin and digits come from a system handwriting face where one exists; Chinese falls through
 * to the site's 楷体 stack glyph by glyph. No font is downloaded. */
const LATIN_HAND = '"Segoe Print", "Bradley Hand", "Noteworthy", '

/** Handwriting: each glyph tilts and drifts a little, the way a pen does; Latin leans forward. */
function drawHand(ctx: CanvasRenderingContext2D, text: string, x: number, baseline: number, font: string, size: number, seed: number) {
  const rand = random(seed)
  ctx.font = font
  let cursor = x
  for (const glyph of text) {
    const width = ctx.measureText(glyph).width
    ctx.save()
    ctx.translate(cursor + width / 2, baseline + (rand() - 0.5) * size * 0.07)
    ctx.rotate((rand() - 0.5) * 0.09)
    if (glyph.charCodeAt(0) < 0x80) ctx.transform(1, 0, -0.14, 1, 0, 0)
    ctx.fillText(glyph, -width / 2, 0)
    ctx.restore()
    cursor += width * (0.92 + rand() * 0.08)
  }
}

/** The illegible fourth answer: overlapping loops, then smeared. */
function drawSmudge(ctx: CanvasRenderingContext2D, box: Box, size: number) {
  const rand = random(404)
  ctx.lineCap = "round"
  ctx.lineJoin = "round"
  const strokes = (alpha: number, width: number) => {
    ctx.globalAlpha = alpha
    ctx.lineWidth = width
    ctx.beginPath()
    let x = box.x + size * 0.1
    let y = box.y + box.h * 0.55
    ctx.moveTo(x, y)
    while (x < box.x + box.w - size * 0.4) {
      const nx = x + size * (0.35 + rand() * 0.35)
      const ny = box.y + box.h * (0.25 + rand() * 0.6)
      ctx.bezierCurveTo(x + size * 0.5, box.y - size * 0.1, nx - size * 0.6, box.y + box.h + size * 0.1, nx, ny)
      x = nx
      y = ny
    }
    ctx.stroke()
  }
  strokes(1, size * 0.075)
  strokes(1, size * 0.06)
  ctx.filter = "blur(1.4px)"
  strokes(0.75, size * 0.16)
  ctx.filter = "none"
  ctx.globalAlpha = 1
}

function fit(min: number, value: number, max: number) {
  return Math.max(min, Math.min(max, value))
}

/* ── Stage geometry ─────────────────────────────────────────────────────── */

function answerSheet(layout: Layout, fonts: Fonts) {
  const { plate, compact } = layout
  const paperW = Math.min(plate.w * (compact ? 1 : 0.86), 640)
  const longest = ANSWERS[2].hand ?? ""
  const unit = measure(`100px ${LATIN_HAND}${fonts.hand}`, longest) / 100
  const gutter = compact ? 30 : 44
  const pad = compact ? 18 : 30
  const size = fit(18, Math.min((paperW - pad * 2 - gutter) / Math.max(unit, 1), plate.h / 12.5), 46)
  const lineHeight = size * 1.78
  const header = size * 1.25
  const paperH = header + lineHeight * ANSWERS.length + pad * 1.2
  const paper: Box = {
    x: plate.x + (plate.w - paperW) / 2,
    y: plate.y + Math.max(0, (plate.h - paperH - size * 3.1) / 2),
    w: paperW,
    h: paperH,
  }
  const left = paper.x + pad + gutter
  const rows: AnswerRow[] = ANSWERS.map((answer, index) => ({
    number: answer.number,
    baseline: paper.y + header + pad * 0.6 + lineHeight * (index + 0.72),
    left,
    end: left,
    verdict: answer.verdict,
  }))
  return { paper, rows, size, pad }
}

function atlasLayout(layout: Layout, rand: () => number) {
  const { plate, compact } = layout
  const chapters = ATLAS_CHAPTERS
  const total = chapters.reduce((sum, chapter) => sum + chapter.topics.length, 0)
  const scale = Math.sqrt((0.5 * plate.w * plate.h) / (Math.PI * 2.4 * total))
  const radii = chapters.map((chapter) => scale * Math.sqrt(chapter.topics.length))
  const cx = plate.x + plate.w / 2
  const cy = plate.y + plate.h / 2
  const centers = chapters.map((_, index) => {
    const t = Math.sqrt((index + 0.5) / chapters.length)
    const angle = index * GOLDEN_ANGLE
    return { x: cx + Math.cos(angle) * t * plate.w * 0.42, y: cy + Math.sin(angle) * t * plate.h * 0.4 }
  })
  // Push overlapping constellations apart, keeping room under each for its name.
  for (let pass = 0; pass < 160; pass += 1) {
    for (let a = 0; a < centers.length; a += 1) {
      for (let b = a + 1; b < centers.length; b += 1) {
        const dx = centers[b].x - centers[a].x
        const dy = centers[b].y - centers[a].y
        const distance = Math.hypot(dx, dy) || 1
        const want = (radii[a] + radii[b]) * 1.32 + 12
        if (distance < want) {
          const push = (want - distance) / 2
          centers[a].x -= (dx / distance) * push
          centers[a].y -= (dy / distance) * push
          centers[b].x += (dx / distance) * push
          centers[b].y += (dy / distance) * push
        }
      }
    }
    centers.forEach((center, index) => {
      const r = radii[index] * 1.15
      center.x = fit(plate.x + r, center.x, plate.x + plate.w - r)
      center.y = fit(plate.y + r, center.y, plate.y + plate.h - r - 18)
    })
  }

  const stars: Star[] = []
  const constellations: Constellation[] = chapters.map((chapter, index) => {
    const center = centers[index]
    const spin = rand() * Math.PI * 2
    const members: number[] = []
    chapter.topics.forEach((name, topic) => {
      const t = 0.22 + 0.78 * Math.sqrt((topic + 0.5) / chapter.topics.length)
      const angle = spin + topic * GOLDEN_ANGLE
      const weight = HEAVY_TOPICS[name] ?? Math.round(3 + rand() * 13)
      members.push(stars.length)
      stars.push({ x: center.x + Math.cos(angle) * radii[index] * t, y: center.y + Math.sin(angle) * radii[index] * t, weight, name })
    })
    // Each star links to its nearest earlier neighbour: a small tree that reads as a constellation.
    const links: Array<[number, number]> = []
    for (let k = 1; k < members.length; k += 1) {
      let best = members[0]
      let bestDistance = Infinity
      for (let j = 0; j < k; j += 1) {
        const d = Math.hypot(stars[members[k]].x - stars[members[j]].x, stars[members[k]].y - stars[members[j]].y)
        if (d < bestDistance) {
          bestDistance = d
          best = members[j]
        }
      }
      links.push([best, members[k]])
    }
    return { name: chapter.name, x: center.x, y: center.y, radius: radii[index], stars: members, links }
  })
  const highlights = stars
    .map((star, index) => ({ index, weight: star.weight }))
    .sort((a, b) => b.weight - a.weight)
    .slice(0, compact ? 2 : 3)
    .map((item) => item.index)
  return { stars, constellations, highlights }
}

/* ── Points ─────────────────────────────────────────────────────────────── */

export function buildScene(layout: Layout, fonts: Fonts): { scene: Scene; buffers: PointBuffers } {
  const N = POINT_COUNT
  const rand = random(20261026)
  const { plate, compact } = layout
  const sheet = answerSheet(layout, fonts)
  const { paper, rows, size } = sheet
  const typeSize = size * 0.84

  // Ink of each answer, handwritten and transcribed.
  const smudgeBox: Box = { x: rows[3].left, y: rows[3].baseline - size * 0.82, w: size * 4.6, h: size * 0.95 }
  const handInk: Point[][] = ANSWERS.map((answer, index) => {
    const row = rows[index]
    const box: Box = { x: paper.x, y: row.baseline - size * 1.2, w: paper.w, h: size * 1.65 }
    if (!answer.hand) return inkOf(box, (ctx) => drawSmudge(ctx, smudgeBox, size))
    return inkOf(box, (ctx) => drawHand(ctx, answer.hand ?? "", row.left, row.baseline, `${size}px ${LATIN_HAND}${fonts.hand}`, size, 7 + index * 31))
  })
  const typeInk: Point[][] = ANSWERS.map((answer, index) => {
    const row = rows[index]
    if (!answer.type) return handInk[index]
    const box: Box = { x: paper.x, y: row.baseline - size * 1.2, w: paper.w, h: size * 1.65 }
    return inkOf(box, (ctx) => {
      ctx.font = `500 ${typeSize}px ${fonts.serif}`
      ctx.fillText(answer.type ?? "", row.left, row.baseline)
    })
  })
  typeInk.forEach((ink, index) => {
    rows[index].end = ink.reduce((right, point) => Math.max(right, point.x), rows[index].left)
  })

  // Split the points between answers by ink, so every answer has the same density.
  const weights = handInk.map((ink, index) => Math.max(ink.length, typeInk[index].length, 1))
  const weightSum = weights.reduce((sum, value) => sum + value, 0)
  const counts = weights.map((value) => Math.floor((value / weightSum) * N))
  counts[2] += N - counts.reduce((sum, value) => sum + value, 0)

  const hand = new Array<Point>(N)
  const typed = new Array<Point>(N)
  const rowOf = new Uint8Array(N)
  const writeOrder = new Float32Array(N)
  const extents = handInk.map((ink) => {
    let min = Infinity
    let max = -Infinity
    for (const point of ink) {
      if (point.x < min) min = point.x
      if (point.x > max) max = point.x
    }
    return Number.isFinite(min) ? { min, width: Math.max(1, max - min) } : { min: 0, width: 1 }
  })
  const penTravel = extents.reduce((sum, extent) => sum + extent.width, 0)
  let cursor = 0
  let penBefore = 0
  counts.forEach((count, row) => {
    // Pair the n-th point from the left in the handwriting with the n-th in the type, glyph to glyph.
    const from = spread(handInk[row], count, rand).sort(byReadingOrder)
    const to = row === 3 ? from.map((point) => ({ ...point })) : spread(typeInk[row], count, rand).sort(byReadingOrder)
    for (let k = 0; k < count; k += 1) {
      const index = cursor + k
      hand[index] = from[k]
      typed[index] = to[k]
      rowOf[index] = row
      writeOrder[index] = (penBefore + (from[k].x - extents[row].min) + (rand() - 0.5) * 3) / penTravel
    }
    cursor += count
    penBefore += extents[row].width
  })

  // Judging: answers stay; the smudge shrinks into the teacher's tray with its original ink.
  const trayLabelW = measure(`13px ${fonts.body}`, "交给老师，对照原卷确认") + 24
  const trayW = Math.min(plate.w, smudgeBox.w * 0.62 + trayLabelW + 28)
  const tray: Box = {
    x: compact ? plate.x + (plate.w - trayW) / 2 : paper.x + paper.w - trayW,
    y: paper.y + paper.h + size * 0.9,
    w: trayW,
    h: size * 1.7,
  }
  const smudgeCenter = { x: smudgeBox.x + smudgeBox.w / 2, y: smudgeBox.y + smudgeBox.h / 2 }
  const trayCenter = { x: tray.x + 14 + (smudgeBox.w * 0.62) / 2, y: tray.y + tray.h / 2 }
  const judged = typed.map((point, index) =>
    rowOf[index] === 3
      ? { x: trayCenter.x + (point.x - smudgeCenter.x) * 0.62, y: trayCenter.y + (point.y - smudgeCenter.y) * 0.62 }
      : point,
  )

  // The atlas: chapter constellations; each star's brightness is its share of the class's mistakes.
  const { stars, constellations, highlights } = atlasLayout(layout, rand)
  const atlas = new Array<Point>(N)
  const glow = new Float32Array(N)
  const maxWeight = Math.max(...stars.map((star) => star.weight))
  const dust = Math.round(N * 0.14)
  const starShares = stars.map((star) => Math.pow(star.weight, 1.2))
  const shareSum = starShares.reduce((sum, value) => sum + value, 0)
  let assigned = 0
  const atlasOrder = shuffled(N, rand)
  stars.forEach((star, s) => {
    const count = s === stars.length - 1 ? N - dust - assigned : Math.floor(((N - dust) * starShares[s]) / shareSum)
    const sigma = 0.8 + 3.4 * Math.sqrt(star.weight / maxWeight)
    const brightness = 0.25 + 0.75 * (star.weight / maxWeight)
    for (let k = 0; k < count; k += 1) {
      const index = atlasOrder[assigned + k]
      const halo = rand() < 0.16 ? 2.8 : 1
      atlas[index] = { x: star.x + gaussian(rand) * sigma * halo, y: star.y + gaussian(rand) * sigma * halo }
      glow[index] = brightness
    }
    assigned += count
  })
  for (let k = assigned; k < N; k += 1) {
    const index = atlasOrder[k]
    atlas[index] = { x: plate.x + rand() * plate.w, y: plate.y + rand() * plate.h }
    glow[index] = 0
  }

  // The ledger: one row per student, one point per open wrong record.
  const ledgerW = plate.w * (compact ? 0.98 : 0.86)
  const ledgerH = Math.min(plate.h * 0.6, ledgerW * (compact ? 0.62 : 0.46))
  const ledger: Box = { x: plate.x + (plate.w - ledgerW) / 2, y: plate.y + (plate.h - ledgerH) / 2, w: ledgerW, h: ledgerH }
  const ledgerPoint = (index: number): Point => {
    const student = Math.floor(index / RECORDS_PER_STUDENT)
    const record = index % RECORDS_PER_STUDENT
    return {
      x: ledger.x + ((record + 0.5 + (rand() - 0.5) * 0.4) / RECORDS_PER_STUDENT) * ledger.w,
      y: ledger.y + ((student + 0.5) / STUDENTS) * ledger.h + (rand() - 0.5) * 0.6,
    }
  }

  // Merging: each student's records are distinct questions drawn from a shared pool with
  // uneven popularity; records of the same question fall into one cluster.
  const popularity = Array.from({ length: DISTINCT_QUESTIONS }, (_, q) => 1 / Math.pow(q + 24, 0.6))
  const questionOf = new Uint16Array(N)
  const clusterSize = new Uint16Array(DISTINCT_QUESTIONS)
  for (let student = 0; student < STUDENTS; student += 1) {
    const keyed = popularity.map((weight, q) => ({ q, key: Math.pow(rand(), 1 / weight) }))
    keyed.sort((a, b) => b.key - a.key)
    for (let record = 0; record < RECORDS_PER_STUDENT; record += 1) {
      const q = keyed[record].q
      questionOf[student * RECORDS_PER_STUDENT + record] = q
      clusterSize[q] += 1
    }
  }
  const disk = compact
    ? { x: plate.x + plate.w / 2, y: plate.y + plate.h * 0.57, r: Math.min(plate.w * 0.44, plate.h * 0.3) }
    : { x: plate.x + plate.w * 0.6, y: plate.y + plate.h / 2, r: Math.min(plate.w * 0.36, plate.h * 0.45) }
  const bySize = Array.from({ length: DISTINCT_QUESTIONS }, (_, q) => q).sort((a, b) => clusterSize[b] - clusterSize[a])
  const clusterAt = new Array<Point>(DISTINCT_QUESTIONS)
  bySize.forEach((q, rank) => {
    const t = Math.sqrt((rank + 0.5) / DISTINCT_QUESTIONS)
    const angle = rank * GOLDEN_ANGLE
    clusterAt[q] = { x: disk.x + Math.cos(angle) * t * disk.r, y: disk.y + Math.sin(angle) * t * disk.r }
  })

  // The new 25-question paper: most questions retest one earlier question, a few also a similar one.
  const template = Array.from({ length: TEMPLATE_QUESTIONS }, (_, index) => {
    const t = (index + 0.5) / TEMPLATE_QUESTIONS
    return compact
      ? { x: plate.x + plate.w * (0.04 + 0.92 * t), y: plate.y + plate.h * 0.08 }
      : { x: plate.x + plate.w * 0.06, y: disk.y - disk.r + 2 * disk.r * t }
  })
  const matched = new Uint8Array(DISTINCT_QUESTIONS)
  const threads: Thread[] = []
  const candidates = bySize.slice(30, 900)
  template.forEach((tick, index) => {
    if (index % 4 === 3) return // a few new questions retest nothing
    const pickOne = () => {
      for (;;) {
        const q = candidates[Math.floor(rand() * candidates.length)]
        if (!matched[q]) {
          matched[q] = 1
          return q
        }
      }
    }
    threads.push({ from: tick, to: clusterAt[pickOne()], similar: false })
    if (index % 5 === 1) threads.push({ from: tick, to: clusterAt[pickOne()], similar: true })
  })

  // The tagline, set large in the plate.
  const finaleSize = fit(28, Math.min(plate.w / (compact ? 7.4 : 8.2), plate.h / 3.4), 96)
  const finaleWidth = Math.max(...FINALE_LINES.map((line) => measure(`500 ${finaleSize}px ${fonts.serif}`, line)))
  const finale: Box = {
    x: plate.x + (plate.w - finaleWidth) / 2,
    y: plate.y + plate.h / 2 - finaleSize * 1.25,
    w: finaleWidth,
    h: finaleSize * 2.6,
  }
  const finaleInk = inkOf({ x: finale.x - 8, y: finale.y - 8, w: finale.w + 16, h: finale.h + 16 }, (ctx) => {
    ctx.font = `500 ${finaleSize}px ${fonts.serif}`
    FINALE_LINES.forEach((line, index) => ctx.fillText(line, finale.x, finale.y + finaleSize * (1 + index * 1.3)))
  })
  const finalePoints = spread(finaleInk, N, rand, 0.6)

  const buffers: PointBuffers = {
    p01: new Float32Array(N * 4),
    p23: new Float32Array(N * 4),
    p45: new Float32Array(N * 4),
    p6w: new Float32Array(N * 4),
    meta: new Float32Array(N * 4),
  }
  for (let index = 0; index < N; index += 1) {
    const o = index * 4
    const q = questionOf[index]
    const cluster = clusterAt[q]
    const spreadRadius = 0.45 + 0.24 * Math.sqrt(clusterSize[q])
    const ledgerAt = ledgerPoint(index)
    buffers.p01.set([hand[index].x, hand[index].y, typed[index].x, typed[index].y], o)
    buffers.p23.set([judged[index].x, judged[index].y, atlas[index].x, atlas[index].y], o)
    buffers.p45.set(
      [ledgerAt.x, ledgerAt.y, cluster.x + gaussian(rand) * spreadRadius, cluster.y + gaussian(rand) * spreadRadius],
      o,
    )
    buffers.p6w.set([finalePoints[index].x, finalePoints[index].y, glow[index], matched[q]], o)
    buffers.meta.set([rowOf[index], rand(), writeOrder[index], (hand[index].y - paper.y) / paper.h], o)
  }

  return {
    scene: {
      layout,
      fonts,
      paper,
      handSize: size,
      rows,
      tray,
      stars,
      constellations,
      highlights,
      ledger,
      disk,
      template,
      threads,
      finale,
    },
    buffers,
  }
}
