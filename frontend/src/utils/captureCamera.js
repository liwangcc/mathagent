// Camera access for the phone capture page: the live stream and the frame
// rate it really delivers, the torch, and full-resolution stills where the
// browser offers ImageCapture (Android Chrome).
import { ANALYSIS_WIDTH, DISTURB_THRESHOLD, choosePhotoTurn, frameDifference } from './captureVision.js'
import { grayscale, scaledHeight, sourceSize } from './captureCanvas.js'

// Live stream size: 1920 is plenty for a 30 fps preview and detection where
// stills come from the photo pipeline; otherwise try 4K and keep it only if
// the phone still delivers MIN_SMOOTH_FPS.
const STREAM_EDGE = 1920
const STREAM_EDGE_HIGH = 3840
const TARGET_FPS = 30
const MIN_SMOOTH_FPS = 20
const VIDEO_START_TIMEOUT_MS = 8000
const PHOTO_CAPABILITIES_TIMEOUT_MS = 3000
const PHOTO_TIMEOUT_MS = 4000
// A still costs about a second; it is only worth that when clearly larger
// than the stream.
const MIN_STILL_GAIN = 1.2

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))])
}

function streamConstraints(edge) {
  // Equal ideals in both directions let the browser pick the mode closest to
  // `edge` whichever way the camera is oriented.  Without a frame-rate ask,
  // phones often hand out their largest mode at 10-15 fps.  Prefer native
  // modes: Chrome may otherwise crop the camera to a square to hit edge×edge,
  // cutting off the top and bottom of the sheet.
  return { width: { ideal: edge }, height: { ideal: edge }, frameRate: { ideal: TARGET_FPS }, resizeMode: { ideal: 'none' } }
}

// The back camera, asking for less after each failure; a refused
// permission is final.
export async function openCameraStream() {
  const attempts = [
    { facingMode: { ideal: 'environment' }, ...streamConstraints(STREAM_EDGE) },
    { facingMode: { ideal: 'environment' }, frameRate: { ideal: TARGET_FPS } },
    { facingMode: { ideal: 'environment' } },
  ]
  let lastError = null
  for (const video of attempts) {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: false, video })
    } catch (err) {
      lastError = err
      if (err?.name === 'NotAllowedError' || err?.name === 'SecurityError') break
    }
  }
  throw lastError
}

export function cameraErrorMessage(err) {
  const name = String(err?.name || '')
  if (name === 'NotAllowedError' || name === 'SecurityError') return '未获得相机权限。请在浏览器设置中允许本网站使用相机，或改用系统相机。'
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return '没有找到可用的后置摄像头，请改用系统相机。'
  if (name === 'NotReadableError') return '相机正被其他应用占用，请关闭其他相机应用后重试。'
  return '无法打开相机，请改用系统相机。'
}

export async function preferContinuousFocus(track) {
  try {
    await track?.applyConstraints?.({ advanced: [{ focusMode: 'continuous' }] })
  } catch {
    // Only a hint; many browsers do not expose focus modes.
  }
}

export function waitForVideo(video) {
  if (video.videoWidth) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('camera timeout')), VIDEO_START_TIMEOUT_MS)
    video.addEventListener('loadedmetadata', () => {
      clearTimeout(timer)
      resolve()
    }, { once: true })
  })
}

// Reports the frames actually shown per second (not the requested rate)
// after each `windowMs`.  presentedFrames counts every frame, even when the
// browser merges callbacks, so the rate stays right under load.  Returns a
// stop function, or null where the browser cannot count frames.
export function watchFrameRate(video, onRate, windowMs = 1000) {
  if (typeof video.requestVideoFrameCallback !== 'function') return null
  let stopped = false
  let since = null
  let handle = 0
  const count = (now, metadata) => {
    if (!since) since = { now, frames: metadata.presentedFrames }
    else if (now - since.now >= windowMs) {
      onRate(((metadata.presentedFrames - since.frames) * 1000) / (now - since.now))
      since = { now, frames: metadata.presentedFrames }
    }
    if (!stopped) handle = video.requestVideoFrameCallback(count)
  }
  handle = video.requestVideoFrameCallback(count)
  return () => {
    stopped = true
    video.cancelVideoFrameCallback?.(handle)
  }
}

// The frame rate over the next `ms`: null when the browser cannot tell, 0
// when not even one window completes in time.
function measureFrameRate(video, ms) {
  return new Promise((resolve) => {
    const stop = watchFrameRate(video, (fps) => {
      stop()
      resolve(fps)
    }, ms)
    if (!stop) return resolve(null)
    setTimeout(() => {
      stop()
      resolve(0)
    }, ms + 1000)
  })
}

// Without full-resolution stills the stream is the photo: try 4K, and keep
// it only while the preview stays smooth.
export async function upgradeStreamIfSmooth(track, video) {
  if (!track?.applyConstraints) return
  try {
    await track.applyConstraints(streamConstraints(STREAM_EDGE_HIGH))
    await sleep(400)
    if (Math.max(video.videoWidth, video.videoHeight) <= STREAM_EDGE) return
    const fps = await measureFrameRate(video, 1500)
    if (fps != null && fps < MIN_SMOOTH_FPS) await track.applyConstraints(streamConstraints(STREAM_EDGE))
  } catch {
    // Keep whatever stream is running.
  }
}

export function hasTorch(track) {
  const torch = track?.getCapabilities?.()?.torch
  return torch === true || (Array.isArray(torch) && torch.includes(true))
}

export function setTorch(track, on) {
  return track.applyConstraints({ advanced: [{ torch: on }] })
}

// Full-resolution stills from the camera's photo mode, or null where they
// would not beat the stream.
export async function openStillCamera(track, video) {
  if (typeof globalThis.ImageCapture !== 'function' || !track) return null
  try {
    const capture = new globalThis.ImageCapture(track)
    const capabilities = await withTimeout(capture.getPhotoCapabilities(), PHOTO_CAPABILITIES_TIMEOUT_MS)
    const width = capabilities?.imageWidth?.max || 0
    const height = capabilities?.imageHeight?.max || 0
    if (Math.max(width, height) <= Math.max(video.videoWidth, video.videoHeight) * MIN_STILL_GAIN) return null
    return {
      width,
      height,
      canFlash: Boolean(capabilities.fillLightMode?.includes?.('flash')),
      async takePhoto({ flash = false } = {}) {
        const light = flash ? { fillLightMode: 'flash' } : {}
        try {
          return await withTimeout(capture.takePhoto({ imageWidth: width, imageHeight: height, ...light }), PHOTO_TIMEOUT_MS)
        } catch {
          // Some cameras refuse an explicit size; take what they offer.
          return withTimeout(capture.takePhoto(light), PHOTO_TIMEOUT_MS)
        }
      },
    }
  } catch {
    return null
  }
}

// Take a still and find the turn that lines it up with `frame`, the live
// frame copied just before.  Resolves to null (and frees the still) when the
// view moved while it was taken or its orientation is ambiguous, so the
// caller uses the frame instead; rejects when the camera fails.
export async function takeAlignedStill(camera, video, frame, { flash = false } = {}) {
  // Compare the live video with itself before and after, so only real
  // movement counts, not how differently a still and a frame are scaled.
  const probeHeight = scaledHeight(ANALYSIS_WIDTH, sourceSize(video))
  const before = grayscale(video, ANALYSIS_WIDTH, probeHeight)
  const still = await createImageBitmap(await camera.takePhoto({ flash }))
  const after = grayscale(video, ANALYSIS_WIDTH, probeHeight)
  const turn = frameDifference(before, after) > DISTURB_THRESHOLD ? null : stillTurn(still, frame)
  if (turn == null) {
    still.close()
    return null
  }
  return { image: still, turn }
}

function stillTurn(still, frame) {
  const width = 64
  const height = scaledHeight(width, sourceSize(frame))
  const reference = grayscale(frame, width, height, { cover: true })
  const diffs = {}
  for (const turn of [0, 90, 180, 270]) {
    diffs[turn] = frameDifference(grayscale(still, width, height, { turn, cover: true }), reference)
  }
  return choosePhotoTurn(diffs, still.width > still.height, frame.width > frame.height)
}
