// Frame analysis and auto-capture decisions for the phone capture page.
// Everything here works on small grayscale frames so it can be tested
// without a camera; thresholds are tuned for a phone on a stand above a desk.

export const ANALYSIS_WIDTH = 96
// Finer frame kept only to tell "same sheet untouched" from "next sheet".
export const SIGNATURE_WIDTH = ANALYSIS_WIDTH * 2

// Consecutive analysis frames (~8 per second) closer than this are "still".
const MOTION_THRESHOLD = 4
// A frame this different from the previous one means a hand or paper moved.
export const DISTURB_THRESHOLD = 8
// Same-sheet test: the next student's copy of the same printed page differs
// only by handwriting and a small shift, which a mean difference blurs away.
// Count pixels that moved far beyond sensor noise instead; a few hundred
// such pixels mean a different (or moved) sheet.  Missing a new sheet is
// worse than a visible duplicate the teacher can delete.
const SAME_SHEET_PIXEL_DELTA = 24
export const SAME_SHEET_CHANGED_FRACTION = 0.004
export const STABLE_MS = 700
// Autofocus keeps working after the sheet stops moving.  Shoot only once the
// sharpness has stopped climbing and is close to the best seen while still;
// if focus keeps hunting, give up waiting after this much extra time.
const SHARP_NEAR_PEAK = 0.9
const SHARP_PLATEAU = 0.06
export const FOCUS_MAX_WAIT_MS = 2000
// The server refuses pages tilted more than about 5 degrees.
const SKEW_LIMIT_DEGREES = 4
const LEVEL_OK_DEGREES = 5
const LEVEL_WARN_DEGREES = 12
// Share of the sheet that is saturated white.  A patch of it is a reflection
// that can wash out handwriting, so automatic shots wait; a sheet saturated
// almost all over is just bright (a dark desk makes the camera brighten), and
// its ink still shows.
const GLARE_MIN = 0.04
const GLARE_MAX = 0.5
// Margin around a cropped sheet, so the 96 px detection never clips the page.
const CROP_MARGIN = 0.04
const FRAME_CENTRE = { x0: 0.2, y0: 0.2, x1: 0.8, y1: 0.8 }
// Long side over short side of a paper sheet: US Letter 1.29, A4/A3 1.41,
// 8K/16K about 1.4, Legal 1.65, with room for a slightly tilted phone.  Bright
// strips, squares and table edges fall outside.
const SHEET_RATIO_MIN = 1.15
const SHEET_RATIO_MAX = 1.9
// Automatic shots only: a sheet covering less of the frame than this leaves
// too few pixels on the handwriting.  Not calibrated on real phones yet.
const MIN_SHOT_COVERAGE = 0.25
// Ink is a pixel this much darker than the brightest one within INK_REACH
// pixels (relative to that brightness, with a floor against sensor noise).
// Comparing locally ignores shading across the sheet.
const INK_REACH = 2
const INK_CONTRAST = 0.06
const INK_MIN_STEP = 8
// Automatic shots need at least this share of ink inside the sheet: print is
// on every page, while a bare desk, a board or a badly defocused page has
// none.  Tuned on rendered exam pages (pale photocopies included), not yet
// on real phone frames.
export const MIN_SHOT_INK = 0.005
// Taken photos are judged for blur at the size the grading page scales them
// to (Upload.vue compresses to a 1920 px long edge); see edgeCrispness.
export const GRADING_EDGE = 1920
// Picked by eye on rendered exam pages: print turns hard to read when the
// camera blur is about 3.5 px on a 2560 px photo (0.08-0.12 here).  Phones
// sharpen their photos, so real values will read higher; not yet checked on
// real phone photos.
const CRISP_INSET = 0.1
const EDGE_SHARE = 0.002
const MIN_INK_DEPTH = 20
export const BLURRY_BELOW = 0.1

export function toGray(rgba, width, height) {
  const gray = new Uint8Array(width * height)
  for (let i = 0, p = 0; i < gray.length; i += 1, p += 4) {
    gray[i] = (rgba[p] * 77 + rgba[p + 1] * 150 + rgba[p + 2] * 29) >> 8
  }
  return gray
}

// Otsu split between desk and paper; also returns the two class means.
function otsuSplit(gray) {
  const histogram = new Uint32Array(256)
  let sum = 0
  for (const value of gray) {
    histogram[value] += 1
    sum += value
  }
  const total = gray.length
  const mean = total ? sum / total : 0
  let backgroundWeight = 0
  let backgroundSum = 0
  let best = { threshold: 127, variance: -1, low: mean, high: mean }
  for (let t = 0; t < 256; t += 1) {
    backgroundWeight += histogram[t]
    if (!backgroundWeight) continue
    const foregroundWeight = total - backgroundWeight
    if (!foregroundWeight) break
    backgroundSum += t * histogram[t]
    const low = backgroundSum / backgroundWeight
    const high = (sum - backgroundSum) / foregroundWeight
    const variance = backgroundWeight * foregroundWeight * (low - high) ** 2
    if (variance > best.variance) best = { threshold: t, variance, low, high }
  }
  return { threshold: best.threshold, low: best.low, high: best.high, mean }
}

function largestBrightComponent(gray, width, threshold) {
  const labels = new Int32Array(gray.length).fill(-1)
  const stack = new Int32Array(gray.length)
  let best = { label: -1, size: 0 }
  let label = 0
  for (let start = 0; start < gray.length; start += 1) {
    if (labels[start] !== -1 || gray[start] <= threshold) continue
    let size = 0
    let top = 0
    stack[top++] = start
    labels[start] = label
    // Runs several times a second on the phone: no per-pixel allocations.
    const visit = (next) => {
      if (labels[next] === -1 && gray[next] > threshold) {
        labels[next] = label
        stack[top++] = next
      }
    }
    while (top) {
      const index = stack[--top]
      size += 1
      const x = index % width
      if (x > 0) visit(index - 1)
      if (x < width - 1) visit(index + 1)
      if (index >= width) visit(index - width)
      if (index + width < gray.length) visit(index + width)
    }
    if (size > best.size) best = { label, size }
    label += 1
  }
  return { labels, ...best }
}

// Find the sheet as the largest bright region on a darker desk.  Skew is only
// reported when the region is fully inside the frame and clearly rectangular;
// otherwise the caller must not block on it.
export function detectPaper(gray, width, height) {
  const { threshold, low, high, mean } = otsuSplit(gray)
  // paperLuma: brightness of the bright (paper) class, so a dark desk mat
  // under a well-lit sheet does not read as "too dark".
  const base = { present: false, confident: false, coverage: 0, skewDegrees: null, touchesEdge: false, glare: 0, meanLuma: mean, paperLuma: high, separable: true, outline: null }
  if (high - low < 40) {
    // No desk/paper contrast: either an empty desk or a sheet filling the frame.
    return { ...base, separable: false, present: mean > 140, coverage: mean > 140 ? 1 : 0, paperLuma: mean }
  }
  const component = largestBrightComponent(gray, width, threshold)
  const coverage = component.size / gray.length
  if (component.label < 0 || coverage < 0.15) return { ...base, coverage }

  let sumX = 0
  let sumY = 0
  let edgePixels = 0
  let saturated = 0
  const members = []
  for (let index = 0; index < gray.length; index += 1) {
    if (component.labels[index] !== component.label) continue
    const x = index % width
    const y = (index - x) / width
    members.push(x, y)
    sumX += x
    sumY += y
    if (x === 0 || y === 0 || x === width - 1 || y === height - 1) edgePixels += 1
    if (gray[index] >= 250) saturated += 1
  }
  const count = component.size
  const cx = sumX / count
  const cy = sumY / count
  let mu20 = 0
  let mu02 = 0
  let mu11 = 0
  for (let i = 0; i < members.length; i += 2) {
    const dx = members[i] - cx
    const dy = members[i + 1] - cy
    mu20 += dx * dx
    mu02 += dy * dy
    mu11 += dx * dy
  }
  const angle = 0.5 * Math.atan2(2 * mu11, mu20 - mu02)
  const cos = Math.cos(angle)
  const sin = Math.sin(angle)
  let minU = Infinity; let maxU = -Infinity; let minV = Infinity; let maxV = -Infinity
  for (let i = 0; i < members.length; i += 2) {
    const dx = members[i] - cx
    const dy = members[i + 1] - cy
    const u = dx * cos + dy * sin
    const v = -dx * sin + dy * cos
    if (u < minU) minU = u
    if (u > maxU) maxU = u
    if (v < minV) minV = v
    if (v > maxV) maxV = v
  }
  const sideU = maxU - minU + 1
  const sideV = maxV - minV + 1
  const fill = count / (sideU * sideV)
  const ratio = Math.max(sideU, sideV) / Math.min(sideU, sideV)
  // More than a few border pixels means the sheet runs past the frame edge.
  const touchesEdge = edgePixels > Math.max(2, (width + height) * 0.02)
  const confident = !touchesEdge && fill > 0.82 && ratio >= SHEET_RATIO_MIN && ratio <= SHEET_RATIO_MAX
  const degrees = (angle * 180) / Math.PI
  const skew = degrees - 90 * Math.round(degrees / 90)
  const corners = [[minU, minV], [maxU, minV], [maxU, maxV], [minU, maxV]].map(([u, v]) => [
    (cx + u * cos - v * sin) / width,
    (cy + u * sin + v * cos) / height,
  ])
  return {
    ...base,
    present: true,
    confident,
    coverage,
    touchesEdge,
    skewDegrees: confident ? skew : null,
    glare: saturated / count,
    outline: corners,
  }
}

function comparable(a, b) {
  return Boolean(a && b && a.length && a.length === b.length)
}

// Auto-exposure brightens or darkens the whole frame when a hand enters or
// leaves; that global shift must not count as change.
function brightnessShift(a, b) {
  let sumA = 0
  let sumB = 0
  for (let i = 0; i < a.length; i += 1) {
    sumA += a[i]
    sumB += b[i]
  }
  return (sumA - sumB) / a.length
}

// Mean absolute difference once the brightness shift is removed.
export function frameDifference(a, b) {
  if (!comparable(a, b)) return Infinity
  const shift = brightnessShift(a, b)
  let total = 0
  for (let i = 0; i < a.length; i += 1) total += Math.abs(a[i] - b[i] - shift)
  return total / a.length
}

// Fraction of pixels that changed far beyond sensor noise once the
// brightness shift is removed.
export function changedFraction(a, b) {
  if (!comparable(a, b)) return 1
  const shift = brightnessShift(a, b)
  let changed = 0
  for (let i = 0; i < a.length; i += 1) {
    if (Math.abs(a[i] - b[i] - shift) > SAME_SHEET_PIXEL_DELTA) changed += 1
  }
  return changed / a.length
}

// Halve a grayscale frame with 2x2 averaging (width and height must be even).
export function downsample2x(gray, width, height) {
  const halfWidth = width >> 1
  const halfHeight = height >> 1
  const out = new Uint8Array(halfWidth * halfHeight)
  for (let y = 0; y < halfHeight; y += 1) {
    const row = 2 * y * width
    for (let x = 0; x < halfWidth; x += 1) {
      const i = row + 2 * x
      out[y * halfWidth + x] = (gray[i] + gray[i + 1] + gray[i + width] + gray[i + width + 1] + 2) >> 2
    }
  }
  return out
}

// Angle between the phone's camera axis and straight down, from
// DeviceOrientationEvent beta/gamma (both 0 when lying flat, screen up).
export function deviceTiltDegrees(beta, gamma) {
  if (!Number.isFinite(beta) || !Number.isFinite(gamma)) return null
  const radians = Math.PI / 180
  const cosine = Math.cos(beta * radians) * Math.cos(gamma * radians)
  return Math.acos(Math.max(-1, Math.min(1, cosine))) / radians
}

export function levelState(tilt) {
  if (tilt == null) return 'unknown'
  if (tilt <= LEVEL_OK_DEGREES) return 'level'
  if (tilt <= LEVEL_WARN_DEGREES) return 'slight'
  return 'tilted'
}

// Hints in priority order; the page shows the first one.
export function captureHints({ paper, tilt, resolutionLow, torchOff = false }) {
  const hints = []
  const level = levelState(tilt)
  if (!paper?.present) hints.push({ code: 'no_paper', tone: 'info', text: '请把试卷放进画面' })
  if (paper?.present && paper.skewDegrees != null && Math.abs(paper.skewDegrees) > SKEW_LIMIT_DEGREES) {
    const turn = paper.skewDegrees > 0 ? '逆时针' : '顺时针'
    hints.push({ code: 'skew', tone: 'block', text: `试卷歪了约 ${Math.round(Math.abs(paper.skewDegrees))}°，请${turn}摆正` })
  }
  if (level === 'tilted') hints.push({ code: 'tilt', tone: 'warn', text: `手机倾斜约 ${Math.round(tilt)}°，请把手机放平` })
  if (paper?.present && paper.touchesEdge) hints.push({ code: 'edge', tone: 'warn', text: '试卷超出画面，请调高支架或把纸放正中' })
  if (paper?.present && !paper.separable) hints.push({ code: 'contrast', tone: 'info', text: '看不清纸张边缘，建议垫深色桌面' })
  if (paper?.present && paper.glare > GLARE_MIN && paper.glare < GLARE_MAX) {
    hints.push({ code: 'glare', tone: 'block', text: torchOff ? '纸面反光，可点“补光”或调整灯光角度' : '纸面反光，请调整灯光角度' })
  }
  const dark = paper && (paper.present ? paper.paperLuma < 100 : paper.meanLuma < 25)
  if (dark) hints.push({ code: 'dark', tone: 'warn', text: torchOff ? '光线太暗，可点“补光”' : '光线太暗，请开灯' })
  if (resolutionLow) hints.push({ code: 'resolution', tone: 'info', text: '摄像头分辨率偏低，可改用系统相机' })
  return hints
}

// Variance of a 4-neighbour Laplacian inside a normalized box.  Focus hunting
// and motion blur lower it; it is only compared against the same scene.
export function sharpness(gray, width, height, box = FRAME_CENTRE) {
  const xa = Math.max(1, Math.floor(box.x0 * width))
  const xb = Math.min(width - 1, Math.ceil(box.x1 * width))
  const ya = Math.max(1, Math.floor(box.y0 * height))
  const yb = Math.min(height - 1, Math.ceil(box.y1 * height))
  let sum = 0
  let sumSquares = 0
  let count = 0
  for (let y = ya; y < yb; y += 1) {
    for (let x = xa; x < xb; x += 1) {
      const i = y * width + x
      const value = 4 * gray[i] - gray[i - 1] - gray[i + 1] - gray[i - width] - gray[i + width]
      sum += value
      sumSquares += value * value
      count += 1
    }
  }
  if (!count) return 0
  const mean = sum / count
  return sumSquares / count - mean * mean
}

// Share of pixels inside a normalized box that are ink: clearly darker than
// the brightest pixel around them.  Unlike `sharpness` this is absolute, so
// it can tell a printed page from an empty surface or a blurred one.
export function inkFraction(gray, width, height, box = FRAME_CENTRE) {
  const xa = Math.max(INK_REACH, Math.floor(box.x0 * width))
  const xb = Math.min(width - INK_REACH, Math.ceil(box.x1 * width))
  const ya = Math.max(INK_REACH, Math.floor(box.y0 * height))
  const yb = Math.min(height - INK_REACH, Math.ceil(box.y1 * height))
  let ink = 0
  let count = 0
  for (let y = ya; y < yb; y += 1) {
    for (let x = xa; x < xb; x += 1) {
      let brightest = 0
      for (let dy = -INK_REACH; dy <= INK_REACH; dy += 1) {
        for (let dx = -INK_REACH; dx <= INK_REACH; dx += 1) {
          brightest = Math.max(brightest, gray[(y + dy) * width + x + dx])
        }
      }
      if (brightest - gray[y * width + x] > Math.max(INK_MIN_STEP, brightest * INK_CONTRAST)) ink += 1
      count += 1
    }
  }
  return count ? ink / count : 0
}

// How crisp the print and handwriting in a photo are: the steepest one-pixel
// steps (the top EDGE_SHARE of them) relative to the paper's brightness.
// Sharp ink changes by most of that within a pixel; blur spreads every edge
// over several pixels, and faint ink has shallow edges too, so a low value
// means "blurred or too faint to read".  Only the inner part of the photo is
// used, away from the paper's border with the desk.  Measure at the scale
// the grader sees (GRADING_EDGE).  Null when there is too little ink to
// judge, such as a blank page.
export function edgeCrispness(gray, width, height) {
  const levels = new Uint32Array(256)
  const steps = new Uint32Array(256)
  const x0 = Math.round(width * CRISP_INSET)
  const x1 = Math.round(width * (1 - CRISP_INSET)) - 1
  const y0 = Math.round(height * CRISP_INSET)
  const y1 = Math.round(height * (1 - CRISP_INSET)) - 1
  for (let y = y0; y < y1; y += 1) {
    for (let x = x0; x < x1; x += 1) {
      const i = y * width + x
      const value = gray[i]
      levels[value] += 1
      steps[Math.max(Math.abs(gray[i + 1] - value), Math.abs(gray[i + width] - value))] += 1
    }
  }
  const paper = percentileOf(levels, 0.5)
  if (paper - percentileOf(levels, EDGE_SHARE) < MIN_INK_DEPTH) return null
  return percentileOf(steps, 1 - EDGE_SHARE) / Math.max(1, paper)
}

// The value below which `share` of a histogram's samples fall.
function percentileOf(histogram, share) {
  let total = 0
  for (const count of histogram) total += count
  let seen = 0
  for (let value = 0; value < histogram.length; value += 1) {
    seen += histogram[value]
    if (seen >= total * share) return value
  }
  return histogram.length - 1
}

// Normalized bounding box of the sheet's outline, clamped to the frame.
function outlineBox(outline) {
  const xs = outline.map(([x]) => x)
  const ys = outline.map(([, y]) => y)
  return {
    x0: Math.max(0, Math.min(...xs)),
    y0: Math.max(0, Math.min(...ys)),
    x1: Math.min(1, Math.max(...xs)),
    y1: Math.min(1, Math.max(...ys)),
  }
}

// Where sharpness should track autofocus: the inner part of the sheet, away
// from its high-contrast edges, or the middle of the frame without one.
export function focusBox(paper) {
  if (!paper?.present || !paper.outline) return FRAME_CENTRE
  const { x0, y0, x1, y1 } = outlineBox(paper.outline)
  const insetX = (x1 - x0) * 0.1
  const insetY = (y1 - y0) * 0.1
  return { x0: x0 + insetX, y0: y0 + insetY, x1: x1 - insetX, y1: y1 - insetY }
}

// Normalized crop around a confidently detected sheet, with a margin.  Null
// means "keep the full frame": no trustworthy sheet, or it nearly fills it.
export function paperCropBox(paper) {
  if (!paper?.confident || !paper.outline) return null
  const outline = outlineBox(paper.outline)
  const box = {
    x0: Math.max(0, outline.x0 - CROP_MARGIN),
    y0: Math.max(0, outline.y0 - CROP_MARGIN),
    x1: Math.min(1, outline.x1 + CROP_MARGIN),
    y1: Math.min(1, outline.y1 + CROP_MARGIN),
  }
  return (box.x1 - box.x0) * (box.y1 - box.y0) > 0.9 ? null : box
}

// How a still photo must be turned to line up with the live frame.  `diffs`
// holds the frame difference for each candidate turn.  Photos normally come
// in the frame's orientation (EXIF applied); a portrait/landscape mismatch
// means the device ignored orientation and the content decides 90 vs 270.
// Returns null when the evidence is ambiguous: the caller keeps the frame.
export function choosePhotoTurn(diffs, photoLandscape, frameLandscape) {
  if (photoLandscape === frameLandscape) {
    if (diffs[180] < diffs[0] * 0.6) return 180
    return diffs[0] <= diffs[90] && diffs[0] <= diffs[270] ? 0 : null
  }
  const [best, other] = diffs[90] <= diffs[270] ? [90, 270] : [270, 90]
  return diffs[best] < diffs[other] * 0.75 ? best : null
}

// Auto-capture: shoot once a sheet has been still for STABLE_MS and is in
// focus, then wait for the next sheet.  Only a sheet that is clearly a whole
// page (`confident`), large enough in the frame and carrying ink (`ink`, from
// inkFraction) is shot; anything less waits for the teacher, who can still
// press the shutter.  Taking the sheet out of view always
// re-arms; any other disturbance (a sheet stacked on top, a page turned)
// re-arms unless the settled `signature` (a finer frame of the same view) is
// practically identical to the last shot's.  After each photo the teacher
// gets `cooldownMs` to swap papers before the next automatic shot.
export function createAutoCapture() {
  let previous
  let stillSince
  let lastShot
  let armed
  let disturbed
  let removed
  let peakSharp
  let recentSharp
  let cooldownFrom
  reset()

  // Returns { state, action }: action is 'capture' when it is time to shoot
  // (this view then counts as shot), and the 'cooldown' state carries the
  // remaining `waitMs`.
  function update({ gray, signature, time, paper, sharpness: sharp, ink = 0, blocked = false, enabled = true, cooldownMs = 0 }) {
    const motion = previous ? frameDifference(gray, previous) : Infinity
    previous = gray
    if (motion > DISTURB_THRESHOLD) disturbed = true
    if (!paper?.present) removed = true
    if (motion > MOTION_THRESHOLD) stillSince = null
    else stillSince ??= time
    trackSharpness(sharp)

    if (!paper?.present) return { state: 'empty', action: null }
    if (stillSince == null || time - stillSince < STABLE_MS) return { state: 'moving', action: null }
    if (!paper.confident) return { state: 'unsure', action: null }
    if (paper.coverage < MIN_SHOT_COVERAGE) return { state: 'small', action: null }
    if (!focusSettled(sharp, time)) return { state: 'focusing', action: null }
    if (ink < MIN_SHOT_INK) return { state: 'blank', action: null }
    if (!armed && (removed || disturbed)) {
      armed = removed || changedFraction(signature, lastShot) > SAME_SHEET_CHANGED_FRACTION
      removed = false
      disturbed = false
    }
    if (!armed) return { state: 'captured', action: null }
    if (blocked) return { state: 'blocked', action: null }
    if (!enabled) return { state: 'ready', action: null }
    const waitMs = cooldownFrom + cooldownMs - time
    if (waitMs > 0) return { state: 'cooldown', action: null, waitMs }
    markCaptured(signature)
    return { state: 'ready', action: 'capture' }
  }

  // Sharpness while still: the best value and the last three.
  function trackSharpness(sharp) {
    if (stillSince == null) {
      peakSharp = 0
      recentSharp = []
    } else if (Number.isFinite(sharp)) {
      peakSharp = Math.max(peakSharp, sharp)
      recentSharp = [...recentSharp.slice(-2), sharp]
    }
  }

  function focusSettled(sharp, time) {
    if (!Number.isFinite(sharp) || peakSharp <= 0) return true
    const nearPeak = sharp >= SHARP_NEAR_PEAK * peakSharp
    const plateau = recentSharp.length >= 3
      && Math.max(...recentSharp) <= (1 + SHARP_PLATEAU) * Math.min(...recentSharp)
    const waitedLong = time - stillSince >= STABLE_MS + FOCUS_MAX_WAIT_MS
    return nearPeak && (plateau || waitedLong)
  }

  // This view has been photographed (by the shutter button, or by update):
  // wait for a different sheet.
  function markCaptured(signature) {
    lastShot = signature
    armed = false
    disturbed = false
    removed = false
  }

  // A photo was just taken: the next automatic shot waits out the cooldown.
  function startCooldown(time) {
    cooldownFrom = time
  }

  function reset() {
    previous = null
    stillSince = null
    lastShot = null
    armed = true
    disturbed = false
    removed = false
    peakSharp = 0
    recentSharp = []
    cooldownFrom = -Infinity
  }

  return { update, markCaptured, startCooldown, reset }
}

export function normalizeRotation(value) {
  const rotation = Math.round(Number(value) / 90) * 90
  return ((rotation % 360) + 360) % 360
}

// A quarter turn (90° or 270°) swaps width and height.
export function isSideways(rotation) {
  return normalizeRotation(rotation) % 180 !== 0
}

// Size of a frame after a clockwise quarter-turn rotation.
export function rotatedSize(width, height, rotation) {
  return isSideways(rotation) ? [height, width] : [width, height]
}

// Largest box with the content's aspect that fits the container, centred.
export function containBox(contentWidth, contentHeight, boxWidth, boxHeight) {
  if (!contentWidth || !contentHeight || !boxWidth || !boxHeight) return { x: 0, y: 0, width: 0, height: 0 }
  const scale = Math.min(boxWidth / contentWidth, boxHeight / contentHeight)
  const width = contentWidth * scale
  const height = contentHeight * scale
  return { x: (boxWidth - width) / 2, y: (boxHeight - height) / 2, width, height }
}

// ── Shooting orientation ──
// A phone lying flat on a stand never auto-rotates, so the page turns its own
// interface.  `shootAngle` is the interface angle the teacher chose, measured
// clockwise from the phone's natural portrait: 0/180 portrait (竖拍), 90/270
// landscape (横拍).  The system may already have turned the screen by
// `screenAngle` and turns camera frames with it.  The page adds the rest with
// CSS, and the photo is turned the opposite way, so the preview looks through
// the phone like a window: whenever the interface text reads upright for the
// teacher, a sheet lying normally in front of them is upright in the photo.

export function currentScreenAngle(screenObject = globalThis.screen, windowObject = globalThis.window) {
  const angle = screenObject?.orientation?.angle
  if (Number.isFinite(angle)) return normalizeRotation(angle)
  const legacy = Number(windowObject?.orientation)
  return Number.isFinite(legacy) ? normalizeRotation(legacy) : 0
}

export function shootingLayout(shootAngle, screenAngle) {
  const uiTurn = normalizeRotation(shootAngle - screenAngle)
  return { uiTurn, imageRotation: normalizeRotation(-uiTurn), landscape: isSideways(shootAngle) }
}

// Choosing the mode that is already active flips it 180° (the phone was
// turned the other way); otherwise prefer the system's own angle when it
// already matches the mode, else the usual way (portrait upright, landscape
// with the phone's top to the left).
export function nextShootAngle(current, kind, screenAngle) {
  const wantLandscape = kind === 'landscape'
  if (isSideways(current) === wantLandscape) return normalizeRotation(current + 180)
  if (isSideways(screenAngle) === wantLandscape) return normalizeRotation(screenAngle)
  return wantLandscape ? 90 : 0
}
