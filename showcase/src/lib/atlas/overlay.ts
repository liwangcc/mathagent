import { clamp } from "@/lib/hooks"

import type { Box, Scene, Thread, Verdict } from "./targets"
import { type Frame, STAGE, stageWeight } from "./timeline"

/* The plate's linework and lettering. Two layers: `under` holds what the ink sits on (the exam
 * paper, ruled lines, constellation lines, threads); `over` holds every word, so no point ever
 * covers a label. */

export interface OverlayPalette {
  night: string
  paper: string
  paperInk: string
  paperRule: string
  pen: string
  ink: string
  muted: string
  rule: string
  accent: string
  correct: string
  partial: string
  wrong: string
  pending: string
}

interface Layers {
  under: CanvasRenderingContext2D
  over: CanvasRenderingContext2D
}

const MARK: Record<Verdict, string> = { wrong: "✗", correct: "✓", partial: "½", pending: "" }

const CAPTIONS = ["图一　原卷第 12 页（局部）", "图二　转录与逐题判定", "图三　高二（3）班错题星图（示意数据）", "图四　按题目指纹归并", ""]

function verdictColor(palette: OverlayPalette, verdict: Verdict) {
  return verdict === "wrong" ? palette.wrong : verdict === "correct" ? palette.correct : verdict === "partial" ? palette.partial : palette.pending
}

function drawPaper({ under, over }: Layers, scene: Scene, frame: Frame, palette: OverlayPalette) {
  if (frame.from !== STAGE.hand) return
  const scanning = frame.to === STAGE.type
  // Matches the shader: a point a fraction d down the page starts moving at mix = 0.45 d.
  const scan = scanning ? clamp(frame.mix / 0.45) : 0
  const { paper, rows, handSize, fonts } = scene
  const top = paper.y + paper.h * scan
  const bottom = paper.y + paper.h
  if (top >= bottom - 0.5) return

  under.save()
  under.shadowColor = "rgba(0, 0, 0, 0.38)"
  under.shadowBlur = 30
  under.shadowOffsetY = 14
  under.fillStyle = palette.paper
  under.beginPath()
  under.roundRect(paper.x, top, paper.w, bottom - top, scan > 0 ? [0, 0, 6, 6] : 6)
  under.fill()
  under.restore()

  // Everything printed on the paper is cut off where the scan has already passed.
  for (const ctx of [under, over]) {
    ctx.save()
    ctx.beginPath()
    ctx.rect(paper.x, top, paper.w, bottom - top)
    ctx.clip()
  }
  under.strokeStyle = palette.paperRule
  under.lineWidth = 1
  under.strokeRect(paper.x + 0.5, paper.y + 0.5, paper.w - 1, paper.h - 1)
  under.beginPath()
  for (const row of rows) {
    under.moveTo(row.left - 4, row.baseline + handSize * 0.36)
    under.lineTo(paper.x + paper.w - 18, row.baseline + handSize * 0.36)
  }
  under.stroke()
  over.fillStyle = palette.paperInk
  over.font = `12px ${fonts.body}`
  over.textBaseline = "alphabetic"
  over.textAlign = "left"
  over.fillText("函数与导数 · 周测", paper.x + 18, paper.y + 24)
  over.textAlign = "right"
  over.fillText("学生 A　第 12 页", paper.x + paper.w - 18, paper.y + 24)
  over.textAlign = "left"
  over.font = `500 ${handSize * 0.56}px ${fonts.serif}`
  for (const row of rows) over.fillText(`${row.number}.`, paper.x + 18, row.baseline)
  under.restore()
  over.restore()

  if (scanning && scan > 0 && scan < 1) {
    const band = under.createLinearGradient(0, top - 42, 0, top)
    band.addColorStop(0, "rgba(0, 0, 0, 0)")
    band.addColorStop(1, palette.accent)
    under.save()
    under.globalAlpha = 0.22
    under.fillStyle = band
    under.fillRect(paper.x - 12, top - 42, paper.w + 24, 42)
    under.restore()
    over.save()
    over.globalAlpha = 0.9
    over.fillStyle = palette.accent
    over.fillRect(paper.x - 12, top - 0.5, paper.w + 24, 1)
    over.restore()
  }
}

function drawTranscript({ under, over }: Layers, scene: Scene, frame: Frame, palette: OverlayPalette) {
  const typed = stageWeight(frame, STAGE.type) + stageWeight(frame, STAGE.judge)
  const judged = stageWeight(frame, STAGE.judge)
  if (typed <= 0.01) return
  const { paper, rows, handSize, fonts, tray } = scene
  over.save()
  over.textBaseline = "alphabetic"
  over.font = `500 ${handSize * 0.56}px ${fonts.serif}`
  rows.forEach((row) => {
    over.globalAlpha = typed * (row.verdict === "pending" ? 0.45 : 0.7)
    over.fillStyle = palette.muted
    over.fillText(row.number, paper.x + 18, row.baseline)
  })
  if (judged > 0.01) {
    rows.forEach((row) => {
      if (row.verdict === "pending") return
      over.globalAlpha = judged
      over.fillStyle = verdictColor(palette, row.verdict)
      over.font = `600 ${handSize * 0.78}px ${fonts.serif}`
      over.fillText(MARK[row.verdict], row.end + handSize * 0.5, row.baseline)
      if (row.verdict === "partial") {
        // The note follows the mark, or drops under the answer where the line has no room.
        over.font = `13px ${fonts.body}`
        over.globalAlpha = judged * 0.85
        const note = "少一段区间"
        const inline = row.end + handSize * 1.25
        const fits = inline + over.measureText(note).width <= scene.layout.plate.x + scene.layout.plate.w
        over.fillText(note, fits ? inline : row.left, fits ? row.baseline - 2 : row.baseline + handSize * 0.62)
      }
    })
    over.font = `13px ${fonts.body}`
    over.fillStyle = palette.pending
    over.globalAlpha = judged
    over.textAlign = "right"
    over.textBaseline = "middle"
    over.fillText("交给老师，对照原卷确认", tray.x + tray.w - 14, tray.y + tray.h / 2)

    const pending = rows[rows.length - 1]
    under.save()
    under.globalAlpha = judged * 0.8
    under.strokeStyle = palette.pending
    under.setLineDash([4, 5])
    under.beginPath()
    under.moveTo(pending.left, pending.baseline + handSize * 0.3)
    under.lineTo(pending.left + handSize * 4.4, pending.baseline + handSize * 0.3)
    under.roundRect(tray.x + 0.5, tray.y + 0.5, tray.w - 1, tray.h - 1, 8)
    under.stroke()
    under.restore()
  }
  over.restore()
}

/** Puts each highlight label where it overlaps neither an earlier label nor the plate edge. */
function placeLabels(ctx: CanvasRenderingContext2D, scene: Scene, labels: string[], reach: number) {
  const { stars, highlights, layout } = scene
  const { plate } = layout
  const centerX = plate.x + plate.w / 2
  const centerY = plate.y + plate.h / 2
  const placed: Box[] = []
  return highlights.map((index, order) => {
    const star = stars[index]
    const dx = star.x - centerX
    const dy = star.y - centerY
    const length = Math.hypot(dx, dy) || 1
    const width = ctx.measureText(labels[order]).width
    // Point the label outward, unless that side has no room for it; never back across its star.
    let right = dx >= 0
    if (right && star.x + reach + 14 + width > plate.x + plate.w) right = false
    else if (!right && star.x - reach - 14 - width < plate.x) right = true
    let tipX = star.x + (right ? 1 : -1) * Math.abs(dx / length) * reach
    let tipY = clamp(star.y + (dy / length) * reach, plate.y + 10, plate.y + plate.h - 24)
    const box = (): Box => {
      const textX = right ? Math.min(tipX + 14, plate.x + plate.w - width) : Math.max(tipX - 14, plate.x + width)
      return { x: right ? textX : textX - width, y: tipY - 10, w: width, h: 20 }
    }
    const overlaps = (candidate: Box) =>
      placed.some((other) => candidate.x < other.x + other.w && other.x < candidate.x + candidate.w && candidate.y < other.y + other.h && other.y < candidate.y + candidate.h)
    for (let attempt = 1; overlaps(box()) && attempt < 9; attempt += 1) {
      tipY += (attempt % 2 ? 1 : -1) * attempt * 12
      tipX += right ? 6 : -6
    }
    const label = box()
    placed.push(label)
    return { star, tipX, tipY, right, textX: right ? label.x : label.x + label.w }
  })
}

function drawAtlas({ under, over }: Layers, scene: Scene, frame: Frame, palette: OverlayPalette) {
  const shown = stageWeight(frame, STAGE.atlas)
  if (shown <= 0.01) return
  const { stars, constellations, highlights, layout, fonts } = scene
  const { plate, compact } = layout
  // Lines and names arrive after the stars have mostly settled.
  const settle = frame.to === STAGE.atlas && frame.from !== STAGE.atlas ? clamp((frame.mix - 0.55) / 0.45) : shown
  if (settle <= 0.01) return

  under.save()
  under.globalAlpha = settle * 0.75
  under.strokeStyle = palette.rule
  under.lineWidth = 0.75
  under.beginPath()
  for (const constellation of constellations) {
    for (const [a, b] of constellation.links) {
      under.moveTo(stars[a].x, stars[a].y)
      under.lineTo(stars[b].x, stars[b].y)
    }
  }
  under.stroke()
  under.restore()

  over.save()
  const named = new Set(highlights.map((star) => constellations.findIndex((item) => item.stars.includes(star))))
  over.font = `${compact ? 10 : 11}px ${fonts.serif}`
  over.textAlign = "center"
  over.textBaseline = "top"
  over.fillStyle = palette.muted
  constellations.forEach((constellation, index) => {
    if (compact && !named.has(index)) return
    over.globalAlpha = settle * (named.has(index) ? 0.95 : 0.6)
    over.fillText(constellation.name, constellation.x, constellation.y + constellation.radius + 8)
  })

  over.font = `13px ${fonts.body}`
  over.textBaseline = "middle"
  const labels = highlights.map((index) => `${stars[index].name} · ${stars[index].weight} 人次`)
  placeLabels(over, scene, labels, compact ? 26 : 40).forEach(({ star, tipX, tipY, right, textX }, order) => {
    over.globalAlpha = settle
    over.strokeStyle = palette.partial
    over.lineWidth = 1
    over.beginPath()
    over.arc(star.x, star.y, 7, 0, Math.PI * 2)
    const angle = Math.atan2(tipY - star.y, tipX - star.x)
    over.moveTo(star.x + Math.cos(angle) * 8, star.y + Math.sin(angle) * 8)
    over.lineTo(tipX, tipY)
    over.lineTo(textX + (right ? -4 : 4), tipY)
    over.stroke()
    // A night-coloured halo, as on printed star charts, keeps the label legible over stars.
    over.textAlign = right ? "left" : "right"
    over.lineJoin = "round"
    over.lineWidth = 5
    over.strokeStyle = palette.night
    over.strokeText(labels[order], textX, tipY)
    over.fillStyle = palette.ink
    over.fillText(labels[order], textX, tipY)
  })

  over.globalAlpha = settle * 0.7
  over.fillStyle = palette.muted
  over.font = `11px ${fonts.body}`
  over.textAlign = "right"
  over.textBaseline = "alphabetic"
  over.fillText("星越亮，错得越多", plate.x + plate.w, plate.y + plate.h - 4)
  over.restore()
}

function drawLedger({ over }: Layers, scene: Scene, frame: Frame, palette: OverlayPalette) {
  const shown = stageWeight(frame, STAGE.ledger)
  if (shown <= 0.01) return
  const { ledger, fonts } = scene
  const settle = frame.to === STAGE.ledger && frame.from !== STAGE.ledger ? clamp((frame.mix - 0.6) / 0.4) : shown
  over.save()
  over.globalAlpha = settle * 0.85
  over.fillStyle = palette.muted
  over.font = `12px ${fonts.body}`
  over.textBaseline = "alphabetic"
  over.textAlign = "left"
  over.fillText("50 名学生 × 每人 600 条未纠正错题 = 30,000 个墨点", ledger.x, ledger.y - 14)
  over.strokeStyle = palette.rule
  over.beginPath()
  for (let student = 0; student <= 50; student += 10) {
    const y = ledger.y + (student / 50) * ledger.h
    over.moveTo(ledger.x - 10, y)
    over.lineTo(ledger.x - 4, y)
  }
  over.stroke()
  over.restore()
}

function threadPoint(thread: Thread, t: number, vertical: boolean) {
  const { from, to } = thread
  const c1 = vertical ? { x: from.x, y: from.y + (to.y - from.y) * 0.5 } : { x: from.x + (to.x - from.x) * 0.45, y: from.y }
  const c2 = vertical ? { x: to.x, y: from.y + (to.y - from.y) * 0.55 } : { x: from.x + (to.x - from.x) * 0.6, y: to.y }
  const u = 1 - t
  return {
    x: u * u * u * from.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * to.x,
    y: u * u * u * from.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * to.y,
  }
}

function drawClusters({ under, over }: Layers, scene: Scene, frame: Frame, palette: OverlayPalette) {
  const shown = stageWeight(frame, STAGE.clusters)
  if (shown <= 0.01) return
  const { disk, template, threads, fonts, layout } = scene
  const vertical = layout.compact
  const arriving = frame.to === STAGE.clusters && frame.from !== STAGE.clusters
  const settle = arriving ? clamp((frame.mix - 0.6) / 0.4) : shown
  if (settle <= 0.01) return
  // Threads draw during the hold, one after another.
  const reach = arriving ? 0 : frame.from === STAGE.clusters && frame.to === STAGE.clusters ? clamp(frame.local / 0.72) : 1

  under.save()
  under.globalAlpha = settle * 0.55
  under.strokeStyle = palette.rule
  under.beginPath()
  under.arc(disk.x, disk.y, disk.r + 12, 0, Math.PI * 2)
  under.stroke()
  under.lineWidth = 0.9
  under.strokeStyle = palette.accent
  threads.forEach((thread, index) => {
    const start = (index / threads.length) * 0.6
    const length = clamp((reach - start) / 0.4)
    if (length <= 0) return
    under.globalAlpha = settle * 0.85
    under.setLineDash(thread.similar ? [3, 4] : [])
    under.beginPath()
    const steps = 28
    for (let step = 0; step <= steps; step += 1) {
      const point = threadPoint(thread, (step / steps) * length, vertical)
      if (step === 0) under.moveTo(point.x, point.y)
      else under.lineTo(point.x, point.y)
    }
    under.stroke()
    if (length >= 1) {
      under.setLineDash([])
      under.beginPath()
      under.arc(thread.to.x, thread.to.y, 5.5, 0, Math.PI * 2)
      under.stroke()
    }
  })
  under.restore()

  over.save()
  over.globalAlpha = settle * 0.85
  over.fillStyle = palette.muted
  over.font = `12px ${fonts.body}`
  over.textAlign = "center"
  over.textBaseline = "top"
  over.fillText("约 2,000 道不同的题 · 同一道题的错题落在一处", disk.x, disk.y + disk.r + 22)
  over.globalAlpha = settle
  over.strokeStyle = palette.ink
  over.lineWidth = 1
  template.forEach((tick) => over.strokeRect(tick.x - 3, tick.y - 3, 6, 6))
  over.fillStyle = palette.ink
  over.font = `12px ${fonts.body}`
  over.textBaseline = "middle"
  over.textAlign = "left"
  if (vertical) {
    over.fillText("新试卷 · 25 题", template[0].x - 3, template[0].y - 16)
  } else {
    over.fillText("新试卷 · 25 题", template[0].x - 3, template[0].y - 22)
    over.fillStyle = palette.muted
    over.font = `10px ${fonts.body}`
    over.textAlign = "right"
    template.forEach((tick, index) => {
      if (index === 0 || (index + 1) % 5 === 0) over.fillText(String(index + 1), tick.x - 8, tick.y)
    })
  }
  over.restore()
}

function drawCaption({ over }: Layers, scene: Scene, frame: Frame, palette: OverlayPalette) {
  const caption = CAPTIONS[frame.act]
  if (!caption) return
  const { plate } = scene.layout
  over.save()
  over.globalAlpha = 0.7
  over.fillStyle = palette.muted
  over.font = `11px ${scene.fonts.serif}`
  over.textAlign = "left"
  over.textBaseline = "alphabetic"
  over.fillText(caption, plate.x, plate.y + plate.h - 4)
  over.restore()
}

export function drawOverlay(layers: Layers, scene: Scene, frame: Frame, palette: OverlayPalette, pixelRatio: number) {
  for (const ctx of [layers.under, layers.over]) {
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0)
    ctx.clearRect(0, 0, scene.layout.width, scene.layout.height)
  }
  drawPaper(layers, scene, frame, palette)
  drawTranscript(layers, scene, frame, palette)
  drawAtlas(layers, scene, frame, palette)
  drawLedger(layers, scene, frame, palette)
  drawClusters(layers, scene, frame, palette)
  drawCaption(layers, scene, frame, palette)
}
