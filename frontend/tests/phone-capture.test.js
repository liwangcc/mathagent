import assert from 'node:assert/strict'
import test from 'node:test'
import {
  BLURRY_BELOW,
  FOCUS_MAX_WAIT_MS,
  SAME_SHEET_CHANGED_FRACTION,
  STABLE_MS,
  captureHints,
  changedFraction,
  choosePhotoTurn,
  containBox,
  createAutoCapture,
  currentScreenAngle,
  detectPaper,
  deviceTiltDegrees,
  downsample2x,
  edgeCrispness,
  focusBox,
  frameDifference,
  inkFraction,
  isSideways,
  levelState,
  nextShootAngle,
  normalizeRotation,
  paperCropBox,
  rotatedSize,
  sharpness,
  shootingLayout,
} from '../src/utils/captureVision.js'
import { COOLDOWN_CHOICES, loadCaptureSettings, saveCaptureSettings } from '../src/utils/captureSettings.js'
import { createCaptureUploads } from '../src/utils/captureUploads.js'
import {
  isCaptureSessionId,
  phoneCaptureFileName,
  phoneCaptureFiles,
  phoneCaptureOrigin,
  phoneCaptureOriginWarning,
  phoneCaptureUrl,
  takePhoneCaptureToken,
} from '../src/utils/phoneCaptureSession.js'
import { getUploadSourcePath } from '../src/utils/uploadSource.js'

const W = 96
const H = 136

// Desk at `desk` brightness with an A4-shaped sheet rotated by `degrees`,
// supersampled so edges are soft like a downscaled camera frame.  `ink`
// scatters dark marks on the sheet to mimic printed text and handwriting.
function scene({ degrees = 0, scale = 0.7, dx = 0, dy = 0, desk = 55, sheet = 215, ink = 7, seed = 1, width = W, height = H } = {}) {
  const gray = new Uint8Array(width * height)
  const theta = (degrees * Math.PI) / 180
  const halfH = (height * scale) / 2
  const halfW = halfH / Math.SQRT2
  const cx = width / 2 + dx
  const cy = height / 2 + dy
  let state = seed
  const random = () => {
    state = (state * 1103515245 + 12345) & 0x7fffffff
    return state / 0x7fffffff
  }
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let inside = 0
      for (let sy = 0; sy < 3; sy += 1) {
        for (let sx = 0; sx < 3; sx += 1) {
          const px = x + (sx + 0.5) / 3 - cx
          const py = y + (sy + 0.5) / 3 - cy
          const u = px * Math.cos(theta) + py * Math.sin(theta)
          const v = -px * Math.sin(theta) + py * Math.cos(theta)
          if (Math.abs(u) <= halfW && Math.abs(v) <= halfH) inside += 1
        }
      }
      let value = desk + ((sheet - desk) * inside) / 9
      if (inside === 9 && random() < ink / 100) value -= 60
      gray[y * width + x] = Math.max(0, Math.min(255, Math.round(value + (random() - 0.5) * 2)))
    }
  }
  return gray
}

function uniform(value, seed = 3) {
  let state = seed
  return Uint8Array.from({ length: W * H }, () => {
    state = (state * 1103515245 + 12345) & 0x7fffffff
    return Math.round(value + ((state / 0x7fffffff) - 0.5) * 2)
  })
}

test('detects a sheet on a dark desk and measures its skew', () => {
  for (const degrees of [0, 2.5, -3, 6, -8]) {
    const paper = detectPaper(scene({ degrees }), W, H)
    assert.equal(paper.present, true, `${degrees}°`)
    assert.equal(paper.confident, true, `${degrees}°`)
    assert.ok(Math.abs(paper.skewDegrees - degrees) < 1, `${degrees}° measured ${paper.skewDegrees}`)
    assert.equal(paper.outline.length, 4)
  }
})

test('skew is measured the same way for landscape sheets', () => {
  const paper = detectPaper(scene({ degrees: 90 + 5, scale: 0.6 }), W, H)
  assert.equal(paper.confident, true)
  assert.ok(Math.abs(paper.skewDegrees - 5) < 1, String(paper.skewDegrees))
})

test('a sheet running past the frame edge is present but never blocks on skew', () => {
  const paper = detectPaper(scene({ degrees: 8, scale: 1.1 }), W, H)
  assert.equal(paper.present, true)
  assert.equal(paper.touchesEdge, true)
  assert.equal(paper.skewDegrees, null)
  assert.ok(!captureHints({ paper, tilt: 0 }).some((hint) => hint.tone === 'block'))
})

test('empty dark desk has no paper; a white desk is unseparable, not skewed', () => {
  assert.equal(detectPaper(uniform(60), W, H).present, false)
  const white = detectPaper(uniform(230), W, H)
  assert.equal(white.present, true)
  assert.equal(white.separable, false)
  assert.equal(white.skewDegrees, null)
})

test('frame difference ignores auto-exposure brightness shifts', () => {
  const frame = scene({ seed: 5 })
  const brighter = frame.map((value) => Math.min(255, value + 12))
  assert.equal(frameDifference(frame, frame), 0)
  assert.ok(frameDifference(frame, brighter) < 1)
  assert.ok(frameDifference(frame, scene({ dx: 6, seed: 5 })) > 4)
})

// A camera view rendered at signature resolution: the printed page is shared
// by every student (`print` seed), handwriting differs (`hand` seed), and
// every frame gets fresh sensor noise.
let noiseSeed = 1000
function view({ hand = 1, dx = 0, dy = 0, empty = false } = {}) {
  const width = W * 2
  const height = H * 2
  const base = empty
    ? new Uint8Array(width * height).fill(55)
    : scene({ seed: 7, ink: 4, dx: dx * 2, dy: dy * 2, width, height })
  const signature = new Uint8Array(base)
  let state = hand * 7919
  const random = () => {
    state = (state * 1103515245 + 12345) & 0x7fffffff
    return state / 0x7fffffff
  }
  if (!empty) {
    // Handwriting: short soft strokes inside the answer area of the sheet.
    for (let stroke = 0; stroke < 140; stroke += 1) {
      const x0 = Math.round(width * (0.3 + random() * 0.4) + dx * 2)
      const y0 = Math.round(height * (0.25 + random() * 0.5) + dy * 2)
      const length = 3 + Math.round(random() * 5)
      for (let k = 0; k < length; k += 1) signature[y0 * width + x0 + k] = Math.max(0, signature[y0 * width + x0 + k] - 70)
    }
  }
  noiseSeed += 1
  let noise = noiseSeed
  for (let i = 0; i < signature.length; i += 1) {
    noise = (noise * 1103515245 + 12345) & 0x7fffffff
    signature[i] = Math.max(0, Math.min(255, signature[i] + Math.round(((noise / 0x7fffffff) - 0.5) * 6)))
  }
  return { gray: downsample2x(signature, width, height), signature }
}

// Runs frames through auto-capture the way the page does: a capture decision
// counts as a photo taken at once, which starts the cooldown.
function feed(auto, makers, { start = 0, step = 120, ...extra } = {}) {
  const actions = []
  const decisions = []
  let time = start
  for (const make of makers) {
    const { gray, signature } = make()
    const paper = detectPaper(gray, W, H)
    const ink = inkFraction(signature, W * 2, H * 2, focusBox(paper))
    const decision = auto.update({ gray, signature, time, paper, ink, ...extra })
    decisions.push(decision)
    if (decision.action === 'capture') {
      actions.push(time)
      auto.startCooldown(time)
    }
    time += step
  }
  return { actions, decisions, time }
}

const hold = (options, count) => Array.from({ length: count }, () => () => view(options))
const settled = Math.ceil(STABLE_MS / 120) + 2

test('same-sheet test separates sensor noise from a classmate\'s copy', () => {
  const a = view({ hand: 1 }).signature
  assert.ok(changedFraction(a, view({ hand: 1 }).signature) < SAME_SHEET_CHANGED_FRACTION / 4, 'noise only')
  assert.ok(changedFraction(a, view({ hand: 2 }).signature) > SAME_SHEET_CHANGED_FRACTION * 2, 'same print, new handwriting, no shift')
})

test('auto-capture shoots once per sheet and waits for the next one', () => {
  const auto = createAutoCapture()
  let clock = 0
  const run = (makers) => {
    const result = feed(auto, makers, { start: clock })
    clock = result.time
    return result.actions.length
  }
  const handOver = () => view({ hand: 1, dx: 14 })
  assert.equal(run(hold({ empty: true }, 5)), 0, 'empty desk')
  assert.equal(run([() => view({ hand: 1, dy: 20 }), () => view({ hand: 1, dy: 8 }), ...hold({ hand: 1 }, settled)]), 1, 'first sheet once')
  assert.equal(run(hold({ hand: 1 }, settled * 2)), 0, 'no repeat while untouched')
  assert.equal(run([handOver, handOver, ...hold({ hand: 1 }, settled)]), 0, 'a hand passing over the same sheet')

  // Next student's copy stacked exactly on top: only the handwriting differs.
  assert.equal(run([handOver, () => view({ hand: 2, dx: 9 }), ...hold({ hand: 2 }, settled)]), 1, 'stacked next sheet')

  // Taking the sheet away always re-arms, even if the same sheet comes back.
  assert.equal(run([...hold({ empty: true }, 3), ...hold({ hand: 2 }, settled)]), 1, 'removed and replaced')
})

test('after a photo, the next automatic shot waits out the chosen interval', () => {
  const auto = createAutoCapture()
  const cooldownMs = 3000
  const first = feed(auto, hold({ hand: 1 }, settled), { cooldownMs })
  assert.equal(first.actions.length, 1)
  // The next sheet goes down at once and settles long before the interval ends.
  const next = feed(auto, [() => view({ hand: 2, dx: 9 }), ...hold({ hand: 2 }, 40)], { start: first.time, cooldownMs })
  assert.equal(next.actions.length, 1, 'next sheet shot once')
  assert.ok(next.actions[0] - first.actions[0] >= cooldownMs, `shot ${next.actions[0] - first.actions[0]} ms after the last photo`)
  const waiting = next.decisions.filter((decision) => decision.state === 'cooldown')
  assert.ok(waiting.length > 5, 'the sheet was ready but waited')
  assert.ok(waiting.every((decision) => decision.waitMs > 0 && decision.waitMs <= cooldownMs))
})

test('the shot interval ends with a reset', () => {
  const auto = createAutoCapture()
  const first = feed(auto, hold({ hand: 7 }, settled), { cooldownMs: 5000 })
  assert.equal(first.actions.length, 1)
  auto.reset()
  const again = feed(auto, hold({ hand: 7 }, settled), { start: first.time, cooldownMs: 5000 })
  assert.equal(again.actions.length, 1, 'the same sheet is shot again without waiting')
})

test('auto-capture holds while blocked, disabled or moving', () => {
  assert.equal(feed(createAutoCapture(), hold({ hand: 3 }, settled), { blocked: true }).actions.length, 0)
  assert.equal(feed(createAutoCapture(), hold({ hand: 3 }, settled), { enabled: false }).actions.length, 0)
  const jitter = Array.from({ length: settled * 2 }, (_, index) => () => view({ hand: 3, dx: index % 2 ? 4 : -4 }))
  assert.equal(feed(createAutoCapture(), jitter).actions.length, 0)
})

// Views the old detector took for a sheet: only a whole, sheet-shaped,
// printed page large enough in the frame may be shot automatically.
function still(signature) {
  return () => ({ signature, gray: downsample2x(signature, W * 2, H * 2) })
}

function shape(inside, { bright = 215, desk = 55, ink = 0 } = {}) {
  const signature = new Uint8Array(W * 2 * H * 2)
  let state = 11
  for (let y = 0; y < H * 2; y += 1) {
    for (let x = 0; x < W * 2; x += 1) {
      state = (state * 1103515245 + 12345) & 0x7fffffff
      const lit = inside(x / (W * 2), y / (H * 2))
      const value = lit && state / 0x7fffffff < ink / 100 ? bright - 60 : lit ? bright : desk
      signature[y * W * 2 + x] = value
    }
  }
  return signature
}

test('auto-capture skips views that are not a whole printed sheet', () => {
  const sheetAt = (options) => scene({ ...options, width: W * 2, height: H * 2 })
  const cases = {
    'light/dark boundary': [shape((x) => x < 0.55, { ink: 4 }), 'unsure'],
    'bright strip': [shape((x, y) => x > 0.08 && x < 0.92 && y > 0.35 && y < 0.6, { ink: 4 }), 'unsure'],
    'bright triangle': [shape((x, y) => x > 0.15 && y > 0.15 && y < 0.85 && x - 0.15 < (y - 0.15) * 0.9, { ink: 4 }), 'unsure'],
    'bright empty desk': [shape(() => true, { bright: 230 }), 'unsure'],
    'sheet past the frame edge': [sheetAt({ scale: 1.1, ink: 4 }), 'unsure'],
    'sheet too small in the frame': [sheetAt({ scale: 0.45, ink: 4 }), 'small'],
    'blank board': [sheetAt({ ink: 0 }), 'blank'],
    'badly defocused sheet': [boxBlur(sheetAt({ ink: 4 }), W * 2, H * 2, 3), 'blank'],
  }
  for (const [name, [signature, state]] of Object.entries(cases)) {
    const { actions, decisions } = feed(createAutoCapture(), Array.from({ length: settled * 2 }, () => still(signature)))
    assert.equal(actions.length, 0, name)
    assert.equal(decisions.at(-1).state, state, name)
  }
  const printed = feed(createAutoCapture(), Array.from({ length: settled * 2 }, () => still(sheetAt({ ink: 4 }))))
  assert.equal(printed.actions.length, 1, 'a printed sheet is shot')
})

test('ink is measured locally: print counts, shading and noise do not', () => {
  const width = W * 2
  const height = H * 2
  const shaded = Uint8Array.from({ length: width * height }, (_, i) => 120 + Math.round((100 * (i % width)) / width))
  assert.equal(inkFraction(shaded, width, height), 0, 'smooth shading')
  assert.equal(inkFraction(view({ empty: true }).signature, width, height), 0, 'noisy desk')
  assert.ok(inkFraction(view({ hand: 1 }).signature, width, height) > 0.01, 'printed sheet')
})

test('phone level comes from device orientation', () => {
  assert.equal(deviceTiltDegrees(0, 0), 0)
  assert.ok(Math.abs(deviceTiltDegrees(10, 0) - 10) < 1e-9)
  assert.ok(Math.abs(deviceTiltDegrees(3, 4) - 5) < 0.05)
  assert.equal(deviceTiltDegrees(null, 3), null)
  assert.deepEqual([levelState(null), levelState(3), levelState(9), levelState(20)], ['unknown', 'level', 'slight', 'tilted'])
})

test('a measured skew blocks and tells which way to turn', () => {
  const skewed = detectPaper(scene({ degrees: 7 }), W, H)
  const hints = captureHints({ paper: skewed, tilt: 20 })
  assert.equal(hints[0].tone, 'block')
  assert.match(hints[0].text, /逆时针/)
  assert.ok(hints.some((hint) => hint.code === 'tilt'))
  const other = captureHints({ paper: detectPaper(scene({ degrees: -7 }), W, H), tilt: 0 })
  assert.match(other[0].text, /顺时针/)
  assert.deepEqual(captureHints({ paper: detectPaper(scene({ degrees: 2 }), W, H), tilt: 2 }), [])
})

test('glare: a saturated patch blocks automatic shots, a sheet bright all over does not', () => {
  const gray = scene()
  for (let y = 50; y < 75; y += 1) for (let x = 35; x < 60; x += 1) gray[y * W + x] = 255
  const paper = detectPaper(gray, W, H)
  const glare = captureHints({ paper, tilt: 0, torchOff: true }).find((hint) => hint.code === 'glare')
  assert.equal(glare.tone, 'block')
  assert.match(glare.text, /补光/, 'suggests the torch while it is off')
  assert.doesNotMatch(captureHints({ paper, tilt: 0 }).find((hint) => hint.code === 'glare').text, /补光/)
  // A dark desk makes the camera brighten the sheet until it saturates; the ink still shows.
  assert.ok(!captureHints({ paper: detectPaper(scene({ sheet: 254 }), W, H), tilt: 0 }).some((hint) => hint.code === 'glare'))
})

test('rotation geometry', () => {
  assert.deepEqual([normalizeRotation(-90), normalizeRotation(450), normalizeRotation(89)], [270, 90, 90])
  assert.deepEqual([0, 90, 180, 270, -90].map(isSideways), [false, true, false, true, true])
  assert.deepEqual(rotatedSize(1920, 1080, 90), [1080, 1920])
  assert.deepEqual(rotatedSize(1920, 1080, 180), [1920, 1080])
  assert.deepEqual(containBox(1080, 1920, 400, 400), { x: 87.5, y: 0, width: 225, height: 400 })
})

test('capture link: fragment token, phone-reachable origin and warnings', () => {
  const url = phoneCaptureUrl('https://grade.example.cn', '0123456789abcdef01234567', 'tok_-9')
  assert.equal(url, 'https://grade.example.cn/capture/0123456789abcdef01234567#t=tok_-9')
  assert.equal(phoneCaptureOrigin({ VITE_API_ORIGIN: 'https://api.example.cn/' }, { origin: 'http://tauri.localhost' }), 'https://api.example.cn')
  assert.equal(phoneCaptureOrigin({}, { origin: 'https://grade.example.cn' }), 'https://grade.example.cn')
  assert.match(phoneCaptureOriginWarning('http://localhost:5173'), /本机地址/)
  assert.match(phoneCaptureOriginWarning('http://192.168.1.20:5173'), /HTTPS/)
  assert.equal(phoneCaptureOriginWarning('https://grade.example.cn'), '')
})

test('imported phone photos are ordered by server sequence with ASCII names', () => {
  const blob = (type = 'image/jpeg') => new Blob([new Uint8Array([1, 2, 3])], { type })
  const files = phoneCaptureFiles([
    { seq: 7, blob: blob() },
    { seq: 2, blob: blob('image/png') },
    { seq: 12, blob: blob() },
  ])
  assert.deepEqual(files.map((file) => file.name), ['capture-0001.png', 'capture-0002.jpg', 'capture-0003.jpg'])
  assert.deepEqual(files.map(getUploadSourcePath), ['capture-0001.png', 'capture-0002.jpg', 'capture-0003.jpg'])
  assert.equal(phoneCaptureFileName(199), 'capture-0200.jpg')
})

function boxBlur(source, width, height, radius) {
  if (!radius) return source
  const out = new Uint8Array(source.length)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let sum = 0
      let count = 0
      for (let dy = -radius; dy <= radius; dy += 1) {
        for (let dx = -radius; dx <= radius; dx += 1) {
          const yy = Math.min(height - 1, Math.max(0, y + dy))
          const xx = Math.min(width - 1, Math.max(0, x + dx))
          sum += source[yy * width + xx]
          count += 1
        }
      }
      out[y * width + x] = Math.round(sum / count)
    }
  }
  return out
}

test('sharpness drops with defocus', () => {
  const sharp = view({ hand: 4 }).signature
  const values = [0, 1, 2, 3].map((radius) => sharpness(boxBlur(sharp, W * 2, H * 2, radius), W * 2, H * 2))
  for (let i = 1; i < values.length; i += 1) assert.ok(values[i] < values[i - 1] * 0.8, `radius ${i}: ${values}`)
})

test('auto-capture waits for autofocus to settle on a still sheet', () => {
  const sharpFrame = view({ hand: 5 })
  // Still for 700 ms but autofocus is only converging when stillness alone
  // would allow the shot (720 ms); it sharpens continuously, then holds.
  const radii = [3, 3, 3, 3, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0]
  const frames = radii.map((radius) => {
    const signature = boxBlur(sharpFrame.signature, W * 2, H * 2, radius)
    return { signature, gray: downsample2x(signature, W * 2, H * 2), radius }
  })
  const auto = createAutoCapture()
  let shotRadius = null
  frames.forEach((frame, index) => {
    const decision = auto.update({ ...frame, time: index * 120, paper: detectPaper(frame.gray, W, H), sharpness: sharpness(frame.signature, W * 2, H * 2), ink: 1 })
    if (decision.action === 'capture' && shotRadius == null) shotRadius = frame.radius
  })
  assert.equal(shotRadius, 0, 'shot only once the frame is fully in focus')
})

test('auto-capture stops waiting for focus that keeps hunting', () => {
  const base = view({ hand: 6 })
  const auto = createAutoCapture()
  let shotAt = null
  for (let index = 0; index * 120 < STABLE_MS + FOCUS_MAX_WAIT_MS + 600; index += 1) {
    // Focus oscillates between sharp and slightly soft, never plateauing.
    const signature = boxBlur(base.signature, W * 2, H * 2, index % 2)
    const gray = downsample2x(signature, W * 2, H * 2)
    const decision = auto.update({ gray, signature, time: index * 120, paper: detectPaper(gray, W, H), sharpness: sharpness(signature, W * 2, H * 2), ink: 1 })
    if (decision.action === 'capture') {
      shotAt = index * 120
      break
    }
  }
  assert.ok(shotAt != null && shotAt >= STABLE_MS + FOCUS_MAX_WAIT_MS, `shot at ${shotAt}`)
})

test('focus is judged inside the sheet, away from its edges', () => {
  const paper = detectPaper(scene({ scale: 0.6 }), W, H)
  const box = focusBox(paper)
  const xs = paper.outline.map(([x]) => x)
  const ys = paper.outline.map(([, y]) => y)
  assert.ok(box.x0 > Math.min(...xs) && box.x1 < Math.max(...xs) && box.y0 > Math.min(...ys) && box.y1 < Math.max(...ys))
  assert.deepEqual(focusBox(detectPaper(uniform(60), W, H)), { x0: 0.2, y0: 0.2, x1: 0.8, y1: 0.8 }, 'no sheet: middle of the frame')
})

test('photo crispness: sharp print passes, blur and faint soft ink are flagged, blank pages are not judged', () => {
  const width = 600
  const height = 800
  const photo = (ink) => {
    const gray = new Uint8Array(width * height).fill(235)
    const random = (() => {
      let state = 9
      return () => {
        state = (state * 1103515245 + 12345) & 0x7fffffff
        return state / 0x7fffffff
      }
    })()
    // Rows of short strokes, like print and handwriting at grading scale.
    for (let row = 0; row < 30; row += 1) {
      for (let stroke = 0; stroke < 40; stroke += 1) {
        const x = 40 + Math.floor(random() * (width - 100))
        const y = 40 + row * 24 + Math.floor(random() * 6)
        const length = 2 + Math.floor(random() * 10)
        const thickness = 2 + Math.floor(random() * 3)
        for (let yy = y; yy < y + thickness; yy += 1) gray.fill(ink, yy * width + x, yy * width + x + length)
      }
    }
    return gray
  }
  const crispness = (ink, radius) => edgeCrispness(boxBlur(photo(ink), width, height, radius), width, height)
  assert.ok(crispness(30, 0) > BLURRY_BELOW * 4, 'sharp')
  assert.ok(crispness(30, 1) > BLURRY_BELOW, 'slightly soft is still fine')
  assert.ok(crispness(30, 5) < BLURRY_BELOW, 'heavily blurred')
  assert.ok(crispness(160, 0) > BLURRY_BELOW, 'pale but sharp photocopy')
  assert.ok(crispness(160, 2) < BLURRY_BELOW, 'pale and soft: too faint to trust')
  assert.equal(edgeCrispness(new Uint8Array(width * height).fill(235), width, height), null, 'blank page')
})

test('crop keeps a margin around a confident sheet and skips uncertain ones', () => {
  const paper = detectPaper(scene({ degrees: 3, scale: 0.6 }), W, H)
  const box = paperCropBox(paper)
  const xs = paper.outline.map(([x]) => x)
  const ys = paper.outline.map(([, y]) => y)
  assert.ok(box.x0 <= Math.min(...xs) - 0.039 && box.x1 >= Math.max(...xs) + 0.039)
  assert.ok(box.y0 <= Math.min(...ys) - 0.039 && box.y1 >= Math.max(...ys) + 0.039)
  assert.equal(paperCropBox(detectPaper(scene({ scale: 1.1 }), W, H)), null, 'sheet past the edge')
  assert.equal(paperCropBox(detectPaper(uniform(230), W, H)), null, 'no visible edge')
  assert.equal(paperCropBox(detectPaper(scene({ scale: 0.97 }), W, H)), null, 'sheet already fills the frame')
})

test('still photo orientation: trust EXIF-applied photos, resolve sideways ones by content', () => {
  assert.equal(choosePhotoTurn({ 0: 6, 90: 30, 180: 9, 270: 31 }, false, false), 0)
  assert.equal(choosePhotoTurn({ 0: 20, 90: 30, 180: 6, 270: 31 }, false, false), 180, 'sensor mounted upside down')
  assert.equal(choosePhotoTurn({ 0: 40, 90: 7, 180: 41, 270: 25 }, true, false), 90)
  assert.equal(choosePhotoTurn({ 0: 40, 90: 25, 180: 41, 270: 7 }, true, false), 270)
  assert.equal(choosePhotoTurn({ 0: 40, 90: 20, 180: 41, 270: 22 }, true, false), null, 'ambiguous: keep the live frame')
  assert.equal(choosePhotoTurn({ 0: 30, 90: 6, 180: 31, 270: 29 }, false, false), null, 'matching shape but content disagrees')
})

test('dark scene suggests the torch only when it can be switched on', () => {
  const dark = detectPaper(scene({ desk: 10, sheet: 90 }), W, H)
  assert.match(captureHints({ paper: dark, tilt: 0, torchOff: true }).find((hint) => hint.code === 'dark').text, /补光/)
  assert.match(captureHints({ paper: dark, tilt: 0 }).find((hint) => hint.code === 'dark').text, /开灯/)
  // A dark desk mat (recommended) with or without a well-lit sheet is not "too dark".
  assert.ok(!captureHints({ paper: detectPaper(uniform(52), W, H), tilt: 0 }).some((hint) => hint.code === 'dark'))
  assert.ok(!captureHints({ paper: detectPaper(scene({ desk: 20 }), W, H), tilt: 0 }).some((hint) => hint.code === 'dark'))
})

// Where a point of a w x h frame lands after a clockwise quarter-turn rotation.
function turnPoint([x, y], w, h, rotation) {
  if (rotation === 90) return [h - y, x]
  if (rotation === 180) return [w - x, h - y]
  if (rotation === 270) return [y, w - x]
  return [x, y]
}

test('portrait/landscape modes: the page turns itself and the photo turns back', () => {
  assert.deepEqual(shootingLayout(0, 0), { uiTurn: 0, imageRotation: 0, landscape: false })
  assert.deepEqual(shootingLayout(90, 0), { uiTurn: 90, imageRotation: 270, landscape: true })
  assert.deepEqual(shootingLayout(270, 0), { uiTurn: 270, imageRotation: 90, landscape: true })
  assert.deepEqual(shootingLayout(180, 0), { uiTurn: 180, imageRotation: 180, landscape: false })
  // The system already turned the screen (and the camera frames): nothing to add.
  assert.deepEqual(shootingLayout(90, 90), { uiTurn: 0, imageRotation: 0, landscape: true })

  // Phone lying flat, turned so its top points left; a landscape sheet lies
  // normally in front of the teacher, so its top is at the frame's right edge.
  const [w, h] = [480, 640]
  const top = turnPoint([w, h / 2], w, h, shootingLayout(90, 0).imageRotation)
  assert.deepEqual(top, [h / 2, 0], 'sheet top ends at the photo top')
  // Turned the other way, the flip (270) makes it upright again.
  assert.deepEqual(turnPoint([0, h / 2], w, h, shootingLayout(270, 0).imageRotation), [h / 2, 0])
})

test('mode buttons: switching picks the usual side, tapping again flips', () => {
  assert.equal(nextShootAngle(0, 'landscape', 0), 90)
  assert.equal(nextShootAngle(90, 'landscape', 0), 270)
  assert.equal(nextShootAngle(270, 'landscape', 0), 90)
  assert.equal(nextShootAngle(90, 'portrait', 0), 0)
  assert.equal(nextShootAngle(0, 'portrait', 0), 180)
  assert.equal(nextShootAngle(0, 'landscape', 270), 270, 'keeps the side the system already shows')
})

test('screen angle from the Screen Orientation API or the legacy property', () => {
  assert.equal(currentScreenAngle({ orientation: { angle: 90 } }, {}), 90)
  assert.equal(currentScreenAngle({}, { orientation: -90 }), 270)
  assert.equal(currentScreenAngle({}, {}), 0)
})

function memoryStorage() {
  const values = new Map()
  return {
    values,
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
  }
}

test('capture settings: defaults, saved choices, invalid values ignored', () => {
  const storage = memoryStorage()
  globalThis.localStorage = storage
  try {
    const defaults = { shootAngle: 0, auto: true, sound: true, hd: true, torch: false, cooldown: 2 }
    assert.deepEqual(loadCaptureSettings(), defaults)
    assert.deepEqual(COOLDOWN_CHOICES, [1, 2, 3, 4, 5])
    saveCaptureSettings({ ...defaults, shootAngle: 270, torch: true, cooldown: 4 })
    assert.deepEqual(loadCaptureSettings(), { ...defaults, shootAngle: 270, torch: true, cooldown: 4 })
    storage.setItem('mathagent_capture_prefs_v1', JSON.stringify({ shootAngle: 89, auto: 'no', cooldown: 9 }))
    assert.deepEqual(loadCaptureSettings(), { ...defaults, shootAngle: 90 })
    // Saved before the interval existed: everything else is kept.
    storage.setItem('mathagent_capture_prefs_v1', JSON.stringify({ shootAngle: 90, auto: false, sound: true, hd: false, torch: false }))
    assert.deepEqual(loadCaptureSettings(), { ...defaults, shootAngle: 90, auto: false, hd: false })
    storage.setItem('mathagent_capture_prefs_v1', '{broken')
    assert.deepEqual(loadCaptureSettings(), defaults)
  } finally {
    delete globalThis.localStorage
  }
  assert.equal(loadCaptureSettings().cooldown, 2, 'no storage at all')
})

test('phone link: the token moves from the address bar into this tab', () => {
  const sid = '0123456789abcdef01234567'
  const history = { state: { router: true }, calls: [], replaceState(...args) { this.calls.push(args) } }
  globalThis.sessionStorage = memoryStorage()
  try {
    assert.equal(takePhoneCaptureToken(sid, { hash: '#t=tok_-9', pathname: `/capture/${sid}` }, history), 'tok_-9')
    assert.deepEqual(history.calls, [[{ router: true }, '', `/capture/${sid}`]], 'fragment cleared, router state kept')
    assert.equal(takePhoneCaptureToken(sid, { hash: '', pathname: `/capture/${sid}` }, history), 'tok_-9', 'a reload finds it')
    assert.equal(takePhoneCaptureToken('fedcba9876543210fedcba98', { hash: '', pathname: '/' }, history), '', 'tokens are per session')
    assert.equal(history.calls.length, 1)
    globalThis.sessionStorage = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
    assert.equal(takePhoneCaptureToken(sid, { hash: '#t=abc', pathname: '/' }, history), 'abc', 'blocked storage still works once')
    assert.equal(takePhoneCaptureToken(sid, { hash: '', pathname: '/' }, history), '')
  } finally {
    delete globalThis.sessionStorage
  }
  assert.equal(isCaptureSessionId(sid), true)
  assert.equal(isCaptureSessionId(sid.toUpperCase()), false)
  assert.equal(isCaptureSessionId('../capture_sessions'), false)
})

async function until(check, label) {
  for (let i = 0; i < 500; i += 1) {
    if (check()) return
    await new Promise((resolve) => setImmediate(resolve))
  }
  assert.fail(`timed out waiting for ${label}`)
}

function httpError(status, message = `HTTP ${status}`) {
  return Object.assign(new Error(message), { status })
}

// An upload queue against a scripted server; `respond(blob)` returns the
// seq or throws.
function uploadQueue(respond, extra = {}) {
  const log = { sent: [], removed: [], waits: [], ended: [], failed: 0 }
  const uploads = createCaptureUploads({
    upload: async (blob) => {
      const seq = await respond(blob)
      log.sent.push(blob)
      return { image: { seq } }
    },
    remove: async (seq) => { log.removed.push(seq) },
    onSessionEnded: (message) => log.ended.push(message),
    onFailed: () => { log.failed += 1 },
    wait: async (ms) => { log.waits.push(ms) },
    ...extra,
  })
  const add = (...blobs) => blobs.forEach((blob) => uploads.add({ blob, thumbUrl: `blob:${blob}`, info: {}, mode: 'frame' }))
  return { uploads, log, add }
}

test('uploads go strictly in order and retry after a network error', async () => {
  let dropB = 1
  const { uploads, log, add } = uploadQueue((blob) => {
    if (blob === 'B' && dropB-- > 0) throw httpError(0, '网络异常')
    return log.sent.length + 1
  })
  add('A', 'B', 'C')
  await until(() => uploads.uploadedCount.value === 3, 'three uploads')
  assert.deepEqual(log.sent, ['A', 'B', 'C'])
  assert.deepEqual(log.waits, [2000], 'one back-off before B went again')
  assert.deepEqual(uploads.shots.value.map((shot) => shot.seq), [1, 2, 3])
  assert.ok(uploads.shots.value.every((shot) => shot.blob === null), 'photo data released once uploaded')
  assert.equal(new Set(uploads.shots.value.map((shot) => shot.clientId)).size, 3)
  assert.equal(uploads.hasUnsent.value, false)
})

test('a refused photo holds the queue until it is retried', async () => {
  let full = true
  const { uploads, log, add } = uploadQueue((blob) => {
    if (blob === 'A' && full) throw httpError(409, '本次拍摄已达 200 张上限')
    return log.sent.length + 1
  })
  add('A', 'B')
  await until(() => uploads.hasFailed.value, 'A failed')
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(log.sent, [], 'B waits behind A')
  assert.equal(uploads.sessionFull.value, true)
  assert.equal(log.failed, 1)
  assert.equal(uploads.shots.value[0].error, '本次拍摄已达 200 张上限')
  full = false
  uploads.retry(uploads.shots.value[0])
  await until(() => uploads.uploadedCount.value === 2, 'both uploaded')
  assert.deepEqual(log.sent, ['A', 'B'])
  assert.equal(uploads.sessionFull.value, false, 'room again once a photo fits')
})

test('a replaced link ends the session and stops uploading', async () => {
  const { uploads, log, add } = uploadQueue(() => { throw httpError(403, '拍摄链接无效或已被替换，请在电脑上重新扫码') })
  add('A', 'B')
  await until(() => log.ended.length === 1, 'session end')
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(log.ended, ['拍摄链接无效或已被替换，请在电脑上重新扫码'])
  assert.deepEqual(uploads.shots.value.map((shot) => shot.status), ['failed', 'pending'])
})

test('deleting photos: refused mid-upload, removed from the server once uploaded', async () => {
  let finish
  const { uploads, log, add } = uploadQueue(() => new Promise((resolve) => { finish = () => resolve(7) }))
  add('A')
  await until(() => uploads.shots.value[0]?.status === 'uploading', 'upload started')
  const [shot] = uploads.shots.value
  assert.equal(await uploads.discard(shot), false, 'not while uploading')
  finish()
  await until(() => shot.status === 'done', 'upload done')
  uploads.sessionFull.value = true
  assert.equal(await uploads.discard(shot), true)
  assert.deepEqual(log.removed, [7])
  assert.deepEqual(uploads.shots.value, [])
  assert.equal(uploads.sessionFull.value, false, 'a photo left the server, so there is room')
})

test('deleting an uploaded photo the server no longer has, or cannot delete', async () => {
  let removeError = httpError(0, '网络异常')
  const { uploads, add } = uploadQueue(() => 3, { remove: async () => { throw removeError } })
  add('A')
  await until(() => uploads.uploadedCount.value === 1, 'upload done')
  const [shot] = uploads.shots.value
  assert.equal(await uploads.discard(shot), false)
  assert.equal(shot.error, '网络异常', 'kept, with the reason')
  removeError = httpError(410, '该照片不存在或已删除')
  assert.equal(await uploads.discard(shot), true, 'already gone counts as deleted')
  assert.deepEqual(uploads.shots.value, [])
})
