// Real Vue pages in Edge with a synthetic phone-stand camera feed and only
// intercepted APIs.  NODE_PATH=<bundled node_modules> node tests/phone-capture.browser.mjs [artifacts-dir]
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import qrcode from 'qrcode-generator'

const { chromium } = createRequire(import.meta.url)('playwright')
const output = resolve(process.argv[2] || '../outputs/phone-capture-browser')
mkdirSync(output, { recursive: true })

// ── Synthetic camera: a phone looking straight down at a dark desk ──
// Timeline (15 s, looped by the browser): empty desk, sheet A slides in and
// rests, a hand passes over A, classmate sheet B is stacked on top and rests,
// everything is cleared and sheet C is placed 8 degrees crooked, empty desk.
const VW = 480, VH = 640, FPS = 8
const PH = Math.round(VH * 0.8), PW = Math.round(PH / Math.SQRT2)

function rng(seed) {
  let state = seed
  return () => { state = (state * 1103515245 + 12345) & 0x7fffffff; return state / 0x7fffffff }
}

function sheet(seed) {
  const page = new Uint8Array(PW * PH).fill(228)
  const rect = (x0, y0, x1, y1, value) => {
    for (let y = Math.max(0, y0); y < Math.min(PH, y1); y += 1) page.fill(value, y * PW + Math.max(0, x0), y * PW + Math.min(PW, x1))
  }
  rect(30, 20, PW - 30, 34, 40)
  for (let row = 0; row < 10; row += 1) for (let block = 0; block < 6; block += 1) rect(30 + block * 48, 95 + row * 38, 64 + block * 48, 101 + row * 38, 70)
  const random = rng(seed * 7919)
  for (let stroke = 0; stroke < 60; stroke += 1) {
    const x = 40 + Math.floor(random() * (PW - 100)), y = 110 + Math.floor(random() * (PH - 140))
    rect(x, y, x + 8 + Math.floor(random() * 30), y + 2, 30)
  }
  rect(40, 50, 60 + seed * 20, 66, 25)
  return page
}

const desk = new Uint8Array(VW * VH).fill(52)

function place(base, page, cx, cy, degrees = 0, pw = PW, ph = PH) {
  const frame = new Uint8Array(base)
  const t = (degrees * Math.PI) / 180, cos = Math.cos(t), sin = Math.sin(t)
  for (let y = 0; y < VH; y += 1) {
    for (let x = 0; x < VW; x += 1) {
      const dx = x - cx, dy = y - cy
      const u = Math.round(dx * cos + dy * sin + pw / 2), v = Math.round(-dx * sin + dy * cos + ph / 2)
      if (u >= 0 && v >= 0 && u < pw && v < ph) frame[y * VW + x] = page[v * pw + u]
    }
  }
  return frame
}

function hand(base, cx) {
  const frame = new Uint8Array(base)
  for (let y = 0; y < VH; y += 1) for (let x = 0; x < VW; x += 1) {
    if (((x - cx) / 70) ** 2 + ((y - VH / 2) / 120) ** 2 <= 1) frame[y * VW + x] = 120
  }
  return frame
}

// Frames as a looping y4m camera feed, with a little sensor noise.
function writeY4m(path, frames) {
  const random = rng(99)
  const chroma = Buffer.alloc((VW / 2) * (VH / 2) * 2, 128)
  const parts = [Buffer.from(`YUV4MPEG2 W${VW} H${VH} F${FPS}:1 Ip A1:1 C420jpeg\n`)]
  for (const frame of frames) parts.push(Buffer.from('FRAME\n'), Buffer.from(frame.map(value => Math.min(255, value + Math.floor(random() * 3)))), chroma)
  writeFileSync(path, Buffer.concat(parts))
  return { path, seconds: frames.length / FPS }
}

function writeDeskVideo(path) {
  const frames = []
  const hold = (frame, seconds) => { for (let i = 0; i < Math.round(seconds * FPS); i += 1) frames.push(frame) }
  const slide = (page, seconds, base, degrees, ox, oy) => {
    const n = Math.round(seconds * FPS)
    for (let i = 0; i < n; i += 1) {
      const k = 1 - (i + 1) / n
      frames.push(place(base, page, VW / 2 + ox * k * VW, VH / 2 + oy * k * VH, degrees))
    }
  }
  const [a, b, c] = [sheet(1), sheet(2), sheet(3)]
  hold(desk, 1.5)
  slide(a, 0.75, desk, 0, 0, 1)
  const stillA = place(desk, a, VW / 2, VH / 2)
  hold(stillA, 2.75)
  for (let i = 0; i < 4; i += 1) frames.push(hand(stillA, -60 + i * 200))
  hold(stillA, 1.5)
  slide(b, 0.75, stillA, 0, 0.4, 0.5)
  hold(place(stillA, b, VW / 2 + 1, VH / 2 - 1), 3.25)
  slide(c, 0.75, desk, 8, -0.5, 0.3)
  hold(place(desk, c, VW / 2, VH / 2, 8), 2.25)
  hold(desk, 1)
  return writeY4m(path, frames)
}

const LW = Math.round(VH * 0.82), LH = Math.round(LW / Math.SQRT2)

function landscapeSheet() {
  const page = new Uint8Array(LW * LH).fill(228)
  const rect = (x0, y0, x1, y1, value) => {
    for (let y = Math.max(0, y0); y < Math.min(LH, y1); y += 1) page.fill(value, y * LW + Math.max(0, x0), y * LW + Math.min(LW, x1))
  }
  rect(30, 16, LW - 30, 30, 40)
  rect(40, 40, 160, 56, 30)
  for (const column of [0, 1]) {
    for (let row = 0; row < 7; row += 1) {
      for (let block = 0; block < 4; block += 1) {
        const x = 30 + column * (LW / 2) + block * 52
        rect(x, 76 + row * 26, x + 38, 82 + row * 26, 70)
      }
    }
  }
  return page
}

function writeLandscapeVideo(path) {
  const sheet = landscapeSheet()
  const frames = []
  const hold = (frame, seconds) => { for (let i = 0; i < Math.round(seconds * FPS); i += 1) frames.push(frame) }
  hold(desk, 1)
  for (let i = 1; i <= 6; i += 1) frames.push(place(desk, sheet, VW / 2 + (6 - i) * 30, VH / 2, 90, LW, LH))
  hold(place(desk, sheet, VW / 2, VH / 2, 90, LW, LH), 4)
  hold(desk, 1)
  return writeY4m(path, frames)
}

// Sheet A, then a classmate's sheet B slid on top right away: B is ready
// long before a 5 s shot interval ends.
function writeQuickSwapVideo(path) {
  const frames = []
  const hold = (frame, seconds) => { for (let i = 0; i < Math.round(seconds * FPS); i += 1) frames.push(frame) }
  const [a, b] = [sheet(4), sheet(5)]
  const stillA = place(desk, a, VW / 2, VH / 2)
  hold(desk, 1)
  hold(stillA, 2.5)
  for (let i = 3; i > 0; i -= 1) frames.push(place(stillA, b, VW / 2 + i * 40, VH / 2 + i * 50))
  hold(place(stillA, b, VW / 2 + 1, VH / 2 - 1), 6)
  hold(desk, 1)
  return writeY4m(path, frames)
}

// A sheet held still but out of focus, as a camera that cannot focus that
// close would see it.
function writeBlurryVideo(path) {
  const sharp = place(desk, sheet(6), VW / 2, VH / 2)
  const blurred = new Uint8Array(sharp.length)
  const radius = 6
  for (let y = 0; y < VH; y += 1) {
    for (let x = 0; x < VW; x += 1) {
      let sum = 0, count = 0
      for (let dy = -radius; dy <= radius; dy += 1) {
        for (let dx = -radius; dx <= radius; dx += 1) {
          sum += sharp[Math.min(VH - 1, Math.max(0, y + dy)) * VW + Math.min(VW - 1, Math.max(0, x + dx))]
          count += 1
        }
      }
      blurred[y * VW + x] = Math.round(sum / count)
    }
  }
  return writeY4m(path, Array.from({ length: 4 * FPS }, () => blurred))
}

const video = writeDeskVideo(resolve(output, 'desk.y4m'))
const sidewaysVideo = writeLandscapeVideo(resolve(output, 'sideways-a3.y4m'))
const quickSwapVideo = writeQuickSwapVideo(resolve(output, 'quick-swap.y4m'))
const blurryVideo = writeBlurryVideo(resolve(output, 'blurry.y4m'))

// ── Mocked capture API (mirrors routers/capture.py) ──
const SID = '0123456789abcdef01234567'
const TOKEN = 'synthetic-capture-token_A1'
const store = { images: [], nextSeq: 1, phoneDeletes: [], teacherDeletes: [], closes: 0, opens: 0, recognize: [] }
const sessionView = () => ({
  session_id: SID, status: 'active', created_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 3 * 3600e3).toISOString(), phone_seen_at: new Date().toISOString(), max_images: 200,
  images: store.images.map(({ seq, client_id, bytes, width, height }) => ({ seq, client_id, bytes, width, height, created_at: new Date().toISOString() })),
})
const phoneView = () => ({ session_id: SID, status: 'active', expires_at: sessionView().expires_at, max_images: 200, images: store.images.map(({ seq, client_id }) => ({ seq, client_id })) })

function jpegSize(buffer) {
  let i = 2
  while (i + 9 < buffer.length) {
    if (buffer[i] !== 0xff) { i += 1; continue }
    const marker = buffer[i + 1]
    if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) return { width: buffer.readUInt16BE(i + 7), height: buffer.readUInt16BE(i + 5) }
    i += 2 + buffer.readUInt16BE(i + 2)
  }
  return null
}

function multipart(request) {
  const boundary = /boundary=(.+)$/.exec(request.headers()['content-type'] || '')?.[1]
  const body = request.postDataBuffer()
  const parts = {}
  const files = []
  let start = body.indexOf(`--${boundary}`)
  while (start >= 0) {
    const next = body.indexOf(`--${boundary}`, start + boundary.length + 2)
    if (next < 0) break
    const part = body.subarray(start + boundary.length + 4, next - 2)
    const split = part.indexOf('\r\n\r\n')
    const head = part.subarray(0, split).toString()
    const content = part.subarray(split + 4)
    const name = /name="([^"]+)"/.exec(head)?.[1]
    const filename = /filename="([^"]+)"/.exec(head)?.[1]
    if (filename) files.push({ name, filename, content })
    else if (name) parts[name] = content.toString()
    start = next
  }
  return { parts, files }
}

const json = (route, status, data) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })

// The synthetic sheets carry a dark title bar at the top and a blank band at
// the bottom, so an upright upload is darker in its top band.
async function topIsDarker(page, buffer) {
  return page.evaluate(async (bytes) => {
    const bitmap = await createImageBitmap(new Blob([new Uint8Array(bytes)], { type: 'image/jpeg' }))
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height)
    const ctx = canvas.getContext('2d')
    ctx.drawImage(bitmap, 0, 0)
    const band = (y0, y1) => {
      const data = ctx.getImageData(Math.round(bitmap.width * 0.25), Math.round(bitmap.height * y0), Math.round(bitmap.width * 0.5), Math.max(1, Math.round(bitmap.height * (y1 - y0)))).data
      let sum = 0
      for (let i = 0; i < data.length; i += 4) sum += data[i]
      return sum / (data.length / 4)
    }
    return band(0.04, 0.14) < band(0.86, 0.96) - 10
  }, [...buffer])
}

// Old (first release) and new shutter cues rendered offline: RMS and peak.
async function measureCues(page) {
  return page.evaluate(async () => {
    const { scheduleCaptureCue, createCueOutput } = await import('/src/utils/captureSound.js')
    const stats = (buffer) => {
      const data = buffer.getChannelData(0)
      let peak = 0
      let sum = 0
      let active = 0
      for (const value of data) {
        peak = Math.max(peak, Math.abs(value))
        sum += value * value
        if (Math.abs(value) > 1e-3) active += 1
      }
      return { peakDb: +(20 * Math.log10(peak)).toFixed(1), rmsDb: +(10 * Math.log10(sum / Math.max(1, active))).toFixed(1), ms: Math.round((active / buffer.sampleRate) * 1000) }
    }
    const fresh = new OfflineAudioContext(1, 48000 * 0.4, 48000)
    scheduleCaptureCue(fresh, createCueOutput(fresh, fresh.destination), 'shutter', 0.01)
    const old = new OfflineAudioContext(1, 48000 * 0.4, 48000)
    for (const [f0, f1, duration, gain] of [[2100, 1500, 0.075, 0.14], [4200, 3000, 0.03, 0.04]]) {
      const oscillator = old.createOscillator()
      const envelope = old.createGain()
      oscillator.frequency.setValueAtTime(f0, 0.01)
      oscillator.frequency.exponentialRampToValueAtTime(f1, 0.01 + duration)
      envelope.gain.setValueAtTime(0.0001, 0.01)
      envelope.gain.exponentialRampToValueAtTime(gain, 0.014)
      envelope.gain.exponentialRampToValueAtTime(0.0001, 0.01 + duration)
      oscillator.connect(envelope).connect(old.destination)
      oscillator.start(0.01)
      oscillator.stop(0.01 + duration + 0.02)
    }
    return { before: stats(await old.startRendering()), after: stats(await fresh.startRendering()) }
  })
}

function apiOnly(route) {
  const request = route.request(), url = new URL(request.url())
  if (url.origin !== base) return route.abort()
  if (!url.pathname.startsWith('/api/')) return route.continue()
  if (url.pathname.startsWith('/api/capture/')) return captureRoutes(route, request, url)
  return json(route, 404, { detail: 'unexpected' })
}

async function captureRoutes(route, request, url) {
  const method = request.method()
  const phone = url.pathname.startsWith(`/api/capture/phone/${SID}`)
  if (phone && request.headers()['x-capture-token'] !== TOKEN) return json(route, 403, { detail: '拍摄链接无效或已被替换，请在电脑上重新扫码' })
  if (phone && request.headers().authorization) throw new Error('phone capture must not send a login header')
  if (url.pathname === `/api/capture/phone/${SID}` && method === 'GET') return json(route, 200, phoneView())
  if (url.pathname === `/api/capture/phone/${SID}/images` && method === 'POST') {
    const { parts, files } = multipart(request)
    const existing = store.images.find(image => image.client_id === parts.client_id)
    if (existing) return json(route, 200, { image: existing, duplicate: true })
    const content = files[0].content
    const image = { seq: store.nextSeq++, client_id: parts.client_id, bytes: content.length, content, ...jpegSize(content), type: request.headers()['content-type'], at: Date.now() }
    store.images.push(image)
    return json(route, 200, { image: { seq: image.seq, client_id: image.client_id }, duplicate: false })
  }
  const phoneDelete = new RegExp(`^/api/capture/phone/${SID}/images/(\\d+)$`).exec(url.pathname)
  if (phoneDelete && method === 'DELETE') {
    store.phoneDeletes.push(Number(phoneDelete[1]))
    store.images = store.images.filter(image => image.seq !== Number(phoneDelete[1]))
    return json(route, 200, phoneView())
  }
  if (url.pathname === '/api/capture/sessions' && method === 'POST') {
    store.opens += 1
    return json(route, 200, { ...sessionView(), token: TOKEN })
  }
  if (url.pathname === `/api/capture/sessions/${SID}` && method === 'GET') return json(route, 200, sessionView())
  if (url.pathname === `/api/capture/sessions/${SID}` && method === 'DELETE') {
    store.closes += 1
    return json(route, 200, { ok: true })
  }
  const teacherImage = new RegExp(`^/api/capture/sessions/${SID}/images/(\\d+)$`).exec(url.pathname)
  if (teacherImage && method === 'GET') {
    const image = store.images.find(item => item.seq === Number(teacherImage[1]))
    return image ? route.fulfill({ status: 200, contentType: 'image/jpeg', body: image.content }) : json(route, 404, { detail: '该照片不存在或已删除' })
  }
  if (teacherImage && method === 'DELETE') {
    store.teacherDeletes.push(Number(teacherImage[1]))
    store.images = store.images.filter(image => image.seq !== Number(teacherImage[1]))
    return json(route, 200, sessionView())
  }
  return json(route, 404, { detail: 'unexpected capture call' })
}

const server = await createServer({
  configFile: false, envFile: false, plugins: [vue()],
  resolve: { alias: { '@': resolve('src') } },
  server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = `http://127.0.0.1:${server.httpServer.address().port}`
// Edge whose camera plays the given synthetic feed.
const launchWithCamera = (feed) => chromium.launch({
  headless: true,
  channel: 'msedge',
  args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', `--use-file-for-fake-video-capture=${feed.path}`],
})
const phoneDevice = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true, permissions: ['camera'] }
const browser = await launchWithCamera(video)
const errors = []
const results = []
const teacher = { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true, current_class_id: 1 }

async function waitFor(check, label, timeout = 30000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (check()) return
    await new Promise(r => setTimeout(r, 100))
  }
  throw new Error(`timed out waiting for ${label}`)
}

try {
  // ── Phone page ──
  const phone = await browser.newContext(phoneDevice)
  await phone.addInitScript(() => {
    // An expired teacher login left on the phone must not bounce the page to /login.
    localStorage.setItem('mathagent_token', 'expired-teacher-login')
    window.__screenAngle = 0
    Object.defineProperty(ScreenOrientation.prototype, 'angle', { configurable: true, get: () => window.__screenAngle })
    window.__oscillators = 0
    const create = AudioContext.prototype.createOscillator
    AudioContext.prototype.createOscillator = function () { window.__oscillators += 1; return create.call(this) }
  })
  await phone.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (url.pathname.startsWith('/api/capture/')) return captureRoutes(route, request, url)
    if (url.pathname === '/api/auth/me') return json(route, 401, { detail: '未登录或token已过期' })
    return json(route, 404, { detail: `unexpected phone call ${url.pathname}` })
  })
  const page = await phone.newPage()
  page.on('pageerror', error => errors.push(`phone: ${error.message}`))
  await page.goto(`${base}/capture/${SID}#t=${TOKEN}`)
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  await page.getByRole('button', { name: '开始拍摄' }).waitFor()
  assert.equal(new URL(page.url()).pathname, `/capture/${SID}`)
  assert.equal(new URL(page.url()).hash, '', 'token is removed from the address bar')
  assert.equal(await page.evaluate(sid => sessionStorage.getItem(`mathagent_capture_token:${sid}`), SID), TOKEN)
  await page.screenshot({ path: resolve(output, 'phone-start.png') })

  await page.getByRole('button', { name: '开始拍摄' }).click()
  const status = page.locator('[data-test=capture-status]')
  await status.waitFor()
  await waitFor(() => store.images.length >= 2, 'two auto-captured sheets')
  await page.waitForFunction(() => document.querySelector('[data-test=capture-status]')?.textContent.includes('歪了'), null, { timeout: 20000 })
  assert.equal(store.images.length, 2, 'A and B shot once each; no re-shot after the hand, none of the crooked sheet')
  assert.equal(await page.locator('.pc-thumb.is-blurry').count(), 0, 'sharp sheets are not flagged as blurry')
  assert.match(await status.textContent(), /歪了约 \d+°，请(逆|顺)时针摆正/)
  await page.screenshot({ path: resolve(output, 'phone-skew-blocked.png') })
  for (const image of store.images) {
    // Cropped to the sheet (A4 PW x PH inside the VW x VH frame) with a margin, never clipped.
    assert.ok(image.width > PW && image.width < VW && image.height > PH && image.height < VH, `cropped upload ${image.width}×${image.height}`)
    assert.ok(image.bytes > 10000)
    assert.match(image.client_id, /^[A-Za-z0-9_-]{8,64}$/)
    assert.equal(await topIsDarker(page, image.content), true, 'upload is upright')
  }
  assert.ok(await page.evaluate(() => window.__oscillators) >= 4, 'a capture cue per auto shot')
  await page.getByText('已拍 2 张 · 已上传 2', { exact: false }).waitFor()
  results.push({ scenario: 'auto capture on synthetic desk feed', shots: 2, blockedSkewHint: true })

  const levelText = text => page.waitForFunction(expected => document.querySelector('[data-test=level]')?.textContent.includes(expected), text, { timeout: 5000 })
  await page.evaluate(() => window.dispatchEvent(new DeviceOrientationEvent('deviceorientation', { beta: 20, gamma: 0, alpha: 0 })))
  await levelText('倾斜 20°')
  await page.evaluate(() => window.dispatchEvent(new DeviceOrientationEvent('deviceorientation', { beta: 2, gamma: 1, alpha: 0 })))
  await levelText('手机水平')
  results.push({ scenario: 'level indicator' })

  // 竖拍/横拍: the page turns itself (a flat phone never auto-rotates);
  // tapping the active mode again flips it 180°.  Auto is off so mode
  // changes cannot trigger extra shots of the still sheet.
  await page.getByRole('button', { name: /^自动/ }).click()
  const rootTurn = () => page.evaluate(() => {
    const matrix = new DOMMatrix(getComputedStyle(document.querySelector('.phone-capture')).transform)
    return Math.round((Math.atan2(matrix.b, matrix.a) * 180) / Math.PI)
  })
  const modes = page.getByRole('group', { name: '拍摄方向' })
  const landscapeButton = modes.getByRole('button', { name: '横拍' })
  const portraitButton = modes.getByRole('button', { name: '竖拍' })
  await landscapeButton.click()
  await page.locator('[data-test=toast]').getByText('横拍', { exact: false }).waitFor()
  assert.equal(await rootTurn(), 90)
  assert.equal(await landscapeButton.getAttribute('aria-pressed'), 'true')
  await page.screenshot({ path: resolve(output, 'phone-landscape-mode.png') })
  await landscapeButton.click()
  await page.locator('[data-test=toast]').getByText('已上下翻转').waitFor()
  assert.equal(await rootTurn(), -90)
  await portraitButton.click()
  assert.equal(await rootTurn(), 0)

  // When the system itself turns the screen, the mode follows and the page
  // adds no turn of its own.
  await page.evaluate(() => { window.__screenAngle = 90; screen.orientation.dispatchEvent(new Event('change')) })
  await page.locator('[data-test=toast]').getByText('已切换为横拍').waitFor()
  assert.equal(await rootTurn(), 0)
  assert.equal(await landscapeButton.getAttribute('aria-pressed'), 'true')
  await page.evaluate(() => { window.__screenAngle = 0; screen.orientation.dispatchEvent(new Event('change')) })
  await page.locator('[data-test=toast]').getByText('已切换为竖拍').waitFor()
  results.push({ scenario: '竖拍/横拍 toggle turns the page, re-tap flips, follows a system rotation' })

  await landscapeButton.click()
  await page.getByRole('button', { name: '手动拍照' }).click()
  await waitFor(() => store.images.length === 3, 'manual landscape shot')
  assert.ok(store.images[2].width > store.images[2].height, '横拍 upload is landscape')
  await portraitButton.click()
  results.push({ scenario: 'manual shutter in 横拍', size: [store.images[2].width, store.images[2].height] })

  await page.locator('.pc-thumb').nth(2).click()
  await page.getByRole('button', { name: '删除这张' }).click()
  await waitFor(() => store.phoneDeletes.length === 1, 'phone delete')
  assert.deepEqual(store.phoneDeletes, [3])
  await page.getByText('已拍 2 张', { exact: false }).waitFor()
  await page.screenshot({ path: resolve(output, 'phone-live.png') })

  const invalid = await phone.newPage()
  await invalid.goto(`${base}/capture/${SID}#t=wrong-token`)
  await invalid.getByText('拍摄已结束').waitFor()
  await invalid.getByText('请在电脑上重新扫码', { exact: false }).waitFor()
  results.push({ scenario: 'wrong token shows a terminal message, no login redirect' })

  const cues = await measureCues(page)
  assert.ok(cues.after.rmsDb - cues.before.rmsDb >= 12, `cue loudness ${JSON.stringify(cues)}`)
  assert.ok(cues.after.peakDb <= 0.1, 'no clipping')
  assert.ok(cues.after.ms <= 220, 'still brief')
  results.push({ scenario: 'louder capture cue (offline render)', ...cues })

  await page.getByRole('button', { name: /^更多/ }).click()
  await page.locator('[data-test=diagnostics]').getByText(`取景 ${VW}×${VH}`, { exact: false }).waitFor()
  // The preview is the video element itself and the readout shows the
  // frames actually delivered (the synthetic camera runs at ${FPS} fps).
  const preview = await page.evaluate(() => {
    const element = document.querySelector('.pc-preview')
    return { tag: element.tagName, playing: !element.paused && Boolean(element.srcObject) }
  })
  assert.equal(preview.tag, 'VIDEO')
  assert.equal(preview.playing, true)
  await page.waitForFunction((fps) => {
    const shown = Number(/(\d+) 帧\/秒/.exec(document.querySelector('[data-test=diagnostics]').textContent)?.[1])
    return shown >= fps - 2 && shown <= fps + 2
  }, FPS, { timeout: 6000 }).catch(async (error) => {
    throw new Error(`fps readout ${await page.locator('[data-test=diagnostics]').textContent()}: ${error.message}`)
  })
  const interval = page.getByRole('group', { name: '拍照间隔' })
  await interval.getByRole('button', { name: '2 秒', pressed: true }).waitFor()
  await interval.getByRole('button', { name: '4 秒' }).click()
  await interval.getByRole('button', { name: '4 秒', pressed: true }).waitFor()
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('mathagent_capture_prefs_v1')).cooldown), 4, 'interval remembered on the phone')
  const before = await page.evaluate(() => window.__oscillators)
  await page.getByRole('button', { name: '试听' }).click()
  await page.waitForFunction((count) => window.__oscillators > count, before)
  await page.screenshot({ path: resolve(output, 'phone-more-sheet.png') })
  await page.getByRole('button', { name: '完成' }).click()
  await phone.close()

  // ── High-resolution still + torch (shimmed device capabilities) ──
  // The shim reports a 4:3 sensor still twice the stream size and returns it
  // sideways (as a phone that ignores orientation would); torch is exposed.
  const hd = await browser.newContext(phoneDevice)
  await hd.addInitScript(() => {
    window.__photoCalls = []
    window.__torch = []
    const capabilities = MediaStreamTrack.prototype.getCapabilities
    MediaStreamTrack.prototype.getCapabilities = function () { return { ...capabilities.call(this), torch: true } }
    const apply = MediaStreamTrack.prototype.applyConstraints
    MediaStreamTrack.prototype.applyConstraints = function (constraints) {
      const torch = constraints?.advanced?.find((item) => 'torch' in item)
      if (torch) { window.__torch.push(torch.torch); return Promise.resolve() }
      return apply.call(this, constraints)
    }
    const Real = window.ImageCapture
    window.ImageCapture = class extends Real {
      async getPhotoCapabilities() {
        return { imageWidth: { max: 1280, min: 1280, step: 0 }, imageHeight: { max: 960, min: 960, step: 0 }, fillLightMode: ['off', 'flash'], redEyeReduction: 'never' }
      }
      async takePhoto(settings) {
        window.__photoCalls.push(settings || {})
        const frame = await createImageBitmap(await super.takePhoto())
        const canvas = new OffscreenCanvas(frame.height * 2, frame.width * 2)
        const ctx = canvas.getContext('2d')
        ctx.translate(0, canvas.height)
        ctx.rotate(-Math.PI / 2)
        ctx.drawImage(frame, 0, 0, frame.width * 2, frame.height * 2)
        return canvas.convertToBlob({ type: 'image/jpeg', quality: 0.95 })
      }
    }
  })
  await hd.route('**/*', apiOnly)
  store.images = []
  const hdPage = await hd.newPage()
  hdPage.on('pageerror', error => errors.push(`hd: ${error.message}`))
  await hdPage.goto(`${base}/capture/${SID}#t=${TOKEN}`)
  await hdPage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  await hdPage.getByRole('button', { name: '开始拍摄' }).click()
  await waitFor(() => store.images.length >= 1, 'first high-resolution shot')
  const still = store.images[0]
  assert.ok(still.height > VH && still.height > still.width, `still is portrait and larger than the ${VW}×${VH} stream: ${still.width}×${still.height}`)
  assert.equal(await topIsDarker(hdPage, still.content), true, 'sideways still was turned upright')
  assert.equal((await hdPage.evaluate(() => window.__photoCalls))[0].imageWidth, 1280)
  await hdPage.locator('[data-test=torch]').click()
  await waitFor(() => store.images.length >= 2, 'shot with torch on', 30000)
  const calls = await hdPage.evaluate(() => window.__photoCalls)
  assert.equal(calls.at(-1).fillLightMode, 'flash', 'torch on asks the still for the flash')
  assert.ok((await hdPage.evaluate(() => window.__torch)).includes(true))
  await hdPage.locator('.pc-thumb').first().click()
  await hdPage.getByText('高清拍照 960×1280', { exact: false }).waitFor()
  await hdPage.screenshot({ path: resolve(output, 'phone-hd-shot.png') })
  results.push({ scenario: 'high-resolution still, sideways photo turned upright, torch + flash', upload: [still.width, still.height] })
  await hd.close()

  // ── Landscape phone ──
  const wide = await browser.newContext({ ...phoneDevice, viewport: { width: 844, height: 390 } })
  await wide.route('**/*', apiOnly)
  const widePage = await wide.newPage()
  widePage.on('pageerror', error => errors.push(`landscape: ${error.message}`))
  await widePage.goto(`${base}/capture/${SID}#t=${TOKEN}`)
  await widePage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  await widePage.getByRole('button', { name: '开始拍摄' }).click()
  await widePage.locator('[data-test=capture-status]').waitFor()
  const layout = await widePage.evaluate(() => {
    const rect = (selector) => document.querySelector(selector).getBoundingClientRect().toJSON()
    const outside = [...document.querySelectorAll('.pc-controls button, .pc-bar')].filter((el) => {
      const r = el.getBoundingClientRect()
      return r.left < 0 || r.top < 0 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1
    }).map((el) => el.textContent.trim())
    return { viewport: rect('.pc-viewport'), controls: rect('.pc-controls'), outside }
  })
  assert.deepEqual(layout.outside, [], 'all controls visible in landscape')
  assert.ok(layout.viewport.width > 844 * 0.6 && layout.viewport.height > 390 * 0.75, JSON.stringify(layout.viewport))
  assert.ok(layout.controls.left > layout.viewport.right - 1, 'controls sit beside the preview')
  await widePage.screenshot({ path: resolve(output, 'phone-landscape.png') })
  results.push({ scenario: 'system-landscape layout', viewport: [Math.round(layout.viewport.width), Math.round(layout.viewport.height)] })
  await wide.close()

  // ── 横拍 end to end: phone lying flat and turned sideways over an A3 sheet ──
  const sidewaysBrowser = await launchWithCamera(sidewaysVideo)
  try {
    const flat = await sidewaysBrowser.newContext(phoneDevice)
    await flat.route('**/*', apiOnly)
    const flatPage = await flat.newPage()
    flatPage.on('pageerror', error => errors.push(`sideways: ${error.message}`))
    const saved = store.images
    store.images = []
    await flatPage.goto(`${base}/capture/${SID}#t=${TOKEN}`)
    await flatPage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    await flatPage.getByRole('group', { name: '拍摄方向' }).getByRole('button', { name: '横拍' }).click()
    await flatPage.getByRole('button', { name: '开始拍摄' }).click()
    await waitFor(() => store.images.length >= 1, 'sideways A3 auto shot')
    const a3 = store.images[0]
    assert.ok(a3.width > a3.height, `A3 upload is landscape: ${a3.width}×${a3.height}`)
    assert.equal(await topIsDarker(flatPage, a3.content), true, 'A3 sheet is upright in the upload')
    const flatLayout = await flatPage.evaluate(() => {
      const outside = [...document.querySelectorAll('.pc-controls button, .pc-bar')].filter((el) => {
        const r = el.getBoundingClientRect()
        return r.left < -1 || r.top < -1 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1
      }).map((el) => el.textContent.trim())
      return { outside, wide: document.querySelector('.phone-capture').classList.contains('is-wide') }
    })
    assert.deepEqual(flatLayout, { outside: [], wide: true }, 'turned page keeps every control on the physical screen')
    await flatPage.screenshot({ path: resolve(output, 'phone-sideways-a3.png') })
    results.push({ scenario: '横拍 on a flat sideways phone: A3 sheet uploaded landscape and upright', upload: [a3.width, a3.height] })
    store.images = saved
    await flat.close()
  } finally {
    await sidewaysBrowser.close()
  }

  // ── Shot interval: the stacked sheet waits until 5 s after the last photo ──
  const quickBrowser = await launchWithCamera(quickSwapVideo)
  try {
    const timed = await quickBrowser.newContext(phoneDevice)
    await timed.addInitScript(() => localStorage.setItem('mathagent_capture_prefs_v1', JSON.stringify({ cooldown: 5 })))
    await timed.route('**/*', apiOnly)
    const timedPage = await timed.newPage()
    timedPage.on('pageerror', error => errors.push(`interval: ${error.message}`))
    const saved = store.images
    store.images = []
    await timedPage.goto(`${base}/capture/${SID}#t=${TOKEN}`)
    await timedPage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    await timedPage.getByText('至少间隔 5 秒', { exact: false }).waitFor()
    await timedPage.getByRole('button', { name: '开始拍摄' }).click()
    await timedPage.locator('[data-test=capture-status]').getByText(/\d 秒后拍照/).waitFor({ timeout: 20000 })
    await waitFor(() => store.images.length >= 2, 'stacked sheet shot after the interval')
    const gapMs = store.images[1].at - store.images[0].at
    assert.ok(gapMs >= 4800, `second photo ${gapMs} ms after the first`)
    results.push({ scenario: '5 s shot interval: a ready sheet waits, then is shot', gapMs })
    store.images = saved
    await timed.close()
  } finally {
    await quickBrowser.close()
  }

  // ── A blurry photo is flagged on the phone and can be deleted for a retake ──
  const blurryBrowser = await launchWithCamera(blurryVideo)
  try {
    const soft = await blurryBrowser.newContext(phoneDevice)
    await soft.addInitScript(() => localStorage.setItem('mathagent_capture_prefs_v1', JSON.stringify({ auto: false })))
    await soft.route('**/*', apiOnly)
    const softPage = await soft.newPage()
    softPage.on('pageerror', error => errors.push(`blurry: ${error.message}`))
    const saved = store.images
    store.images = []
    await softPage.goto(`${base}/capture/${SID}#t=${TOKEN}`)
    await softPage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    await softPage.getByRole('button', { name: '开始拍摄' }).click()
    // The sheet is in view (not "empty"), so the camera is live.
    await softPage.locator('[data-test=capture-status]').getByText('自动拍照已关闭').waitFor({ timeout: 20000 })
    await softPage.getByRole('button', { name: '手动拍照' }).click()
    await softPage.locator('[data-test=toast]').getByText('可能拍糊了', { exact: false }).waitFor()
    await waitFor(() => store.images.length === 1, 'blurry photo uploaded')
    await softPage.locator('.pc-thumb.is-blurry').click()
    await softPage.locator('[data-test=blurry-note]').waitFor()
    await softPage.getByRole('button', { name: '删除并重拍' }).click()
    await softPage.locator('[data-test=toast]').getByText('已删除', { exact: false }).waitFor()
    await waitFor(() => store.images.length === 0, 'blurry photo deleted on the server')
    results.push({ scenario: 'blurry photo flagged on the phone, deleted for a retake' })
    store.images = saved
    await soft.close()
  } finally {
    await blurryBrowser.close()
  }

  // The desktop flow below starts from two phone photos.
  store.images = store.images.slice(0, 2)

  // ── Desktop grading page ──
  const desktop = await browser.newContext({ viewport: { width: 1366, height: 900 } })
  await desktop.addInitScript(() => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
  })
  await desktop.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (url.pathname.startsWith('/api/capture/')) return captureRoutes(route, request, url)
    if (url.pathname === '/api/batch-grade/recognize') {
      const { parts, files } = multipart(request)
      store.recognize.push({ names: files.map(file => file.filename), sourcePaths: JSON.parse(parts.source_paths || '[]'), template: parts.template_id })
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: progress\ndata: {"batch_id":"mock-batch","current":0,"total":2,"message":"本地模拟"}\n\nevent: error\ndata: {"message":"本地模拟：验证到批次创建为止"}\n\n' })
    }
    let data = {}
    if (url.pathname === '/api/auth/me') data = { user: teacher }
    else if (url.pathname === '/api/auth/classes') data = { classes: [{ id: 1, name: '模拟班级', class_name: '模拟班级' }], current_class_id: 1 }
    else if (url.pathname.endsWith('/availability')) data = { status: 'available', service_status: 'available', registration_hardware: { status: 'ready', ready: true }, hardware: { status: 'ready' } }
    else if (url.pathname.includes('/pending')) data = []
    return json(route, 200, data)
  })
  const grading = await desktop.newPage()
  grading.on('pageerror', error => errors.push(`desktop: ${error.message}`))
  await grading.goto(base + '/upload')
  await grading.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  await grading.locator('[data-test=open-phone-capture]').click()
  const dialog = grading.getByRole('dialog', { name: '用手机拍摄试卷' })
  await dialog.waitFor()
  const path = dialog.locator('[data-test=phone-capture-qr] path')
  await path.waitFor()
  const expected = qrcode(0, 'M')
  expected.addData(`${base}/capture/${SID}#t=${TOKEN}`, 'Byte')
  expected.make()
  let expectedPath = ''
  for (let row = 0; row < expected.getModuleCount(); row += 1) for (let col = 0; col < expected.getModuleCount(); col += 1) if (expected.isDark(row, col)) expectedPath += `M${col},${row}h1v1h-1z`
  assert.equal(await path.getAttribute('d'), expectedPath, 'QR encodes the phone URL with the fragment token')
  await dialog.getByText('当前是本机地址，手机无法打开', { exact: false }).waitFor()
  await dialog.locator('[data-test=phone-capture-count]').getByText('已收到 2 张').waitFor()
  await grading.waitForFunction(() => document.querySelectorAll('.pcd-thumb img').length === 2)
  await grading.waitForFunction(() => !document.querySelector('.el-overlay-dialog')?.closest('[class*="fade-enter"]'))
  await grading.waitForTimeout(600)
  await dialog.screenshot({ path: resolve(output, 'desktop-dialog.png') })
  await dialog.locator('.pcd-thumb-size').first().waitFor()
  await dialog.locator('.pcd-thumb .el-image').first().click()
  await grading.locator('.el-image-viewer__wrapper').waitFor()
  await grading.waitForTimeout(400)
  await grading.screenshot({ path: resolve(output, 'desktop-full-size.png') })
  await grading.keyboard.press('Escape')
  await grading.locator('.el-image-viewer__wrapper').waitFor({ state: 'hidden' })

  await dialog.locator('[data-test=phone-capture-import]').click()
  await dialog.waitFor({ state: 'hidden' })
  await grading.getByText('已导入 2 张手机照片').waitFor()
  await grading.getByText('已选 2 个文件', { exact: false }).waitFor()
  assert.equal(store.opens, 1)
  assert.equal(store.closes, 0, 'import alone keeps the photos on the server')

  // Reopening reuses the stored session instead of rotating the token.
  await grading.locator('[data-test=open-phone-capture]').click()
  await dialog.getByText('导入本页', { exact: false }).waitFor()
  assert.equal(store.opens, 1)
  await dialog.getByRole('button', { name: '稍后导入' }).click()
  await dialog.waitFor({ state: 'hidden' })
  results.push({ scenario: 'desktop QR, live thumbnails, import, reopen without token rotation' })

  // Test seam: stand in for a confirmed template so auto-start runs a real
  // recognize request; the phone session must be released by its batch_id.
  await grading.locator('#template-column-mode').waitFor()
  await grading.locator('select.form-select').first().selectOption('daily')
  await grading.evaluate(() => { document.querySelector('.upload-page').__vueParentComponent.setupState.confirmedTemplateId = 'mock-template' })
  await waitFor(() => store.recognize.length === 1, 'recognize request')
  await waitFor(() => store.closes === 1, 'capture session released after batch creation')
  assert.deepEqual(store.recognize[0].sourcePaths, ['capture-0001.jpg', 'capture-0002.jpg'])
  assert.deepEqual(store.recognize[0].names, ['capture-0001.ocr-ready-v1.jpg', 'capture-0002.ocr-ready-v1.jpg'])
  assert.equal(await grading.evaluate(() => sessionStorage.getItem('mathagent_phone_capture:910001')), null)
  results.push({ scenario: 'grading batch created → phone session deleted', sourcePaths: store.recognize[0].sourcePaths })
  await desktop.close()

  assert.deepEqual(errors, [])
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockOnly: true, realAccountRequests: 0, video, sidewaysVideo, results, errors }, null, 2))
  console.log(JSON.stringify({ ok: true, results, output }, null, 2))
} finally {
  await browser.close()
  await server.close()
}
