// Canvas work for the phone capture page: drawing frames turned upright,
// small grayscale samples for analysis, and encoding the photo to upload.
import { ANALYSIS_WIDTH, BLURRY_BELOW, GRADING_EDGE, detectPaper, edgeCrispness, isSideways, normalizeRotation, paperCropBox, rotatedSize, toGray } from './captureVision.js'

// Rendered intermediate before cropping to the sheet; the upload is capped
// below that, and the grading page still scales to its own 1920 limit.
const RENDER_EDGE = 3200
const MAX_UPLOAD_EDGE = 2560
const UPLOAD_QUALITY = 0.92
const THUMB_EDGE = 160
const THUMB_QUALITY = 0.7

// Intrinsic size of a video, image, canvas or bitmap.
export function sourceSize(source) {
  return [
    source.videoWidth || source.naturalWidth || source.width,
    source.videoHeight || source.naturalHeight || source.height,
  ]
}

// Height that keeps the [width, height] aspect at a new width.
export function scaledHeight(width, [sourceWidth, sourceHeight]) {
  return Math.max(1, Math.round((width * sourceHeight) / sourceWidth))
}

function makeCanvas(width, height) {
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(width))
  canvas.height = Math.max(1, Math.round(height))
  return canvas
}

function smoothContext(canvas) {
  const ctx = canvas.getContext('2d')
  ctx.imageSmoothingQuality = 'high'
  return ctx
}

function jpegBlob(canvas, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('无法生成照片'))), 'image/jpeg', quality)
  })
}

// Draw `source` turned clockwise by `rotation` into `box`, which is given in
// the turned (output) orientation.
export function drawRotatedFrame(ctx, source, rotation, box) {
  const turn = normalizeRotation(rotation)
  const [width, height] = isSideways(turn) ? [box.height, box.width] : [box.width, box.height]
  ctx.save()
  ctx.translate(box.x + box.width / 2, box.y + box.height / 2)
  ctx.rotate((turn * Math.PI) / 180)
  ctx.drawImage(source, -width / 2, -height / 2, width, height)
  ctx.restore()
}

// Grayscale copy of `source` turned clockwise by `turn` and scaled to fit
// (or, with `cover`, to fill) width × height.
//
// For capture-time checks only: it reads through a new CPU canvas, which
// copies the full frame each call but is consistent from frame to frame, as
// the "nothing moved" and orientation checks need.  (Fresh GPU canvases were
// measured to differ by 6-9 levels between frames of a still scene.)
export function grayscale(source, width, height, { turn = 0, cover = false } = {}) {
  const canvas = makeCanvas(width, height)
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  // Large downscales alias differently per source type; high-quality
  // smoothing keeps a canvas, a video and a photo of one scene comparable.
  ctx.imageSmoothingQuality = 'high'
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  const [turnedWidth, turnedHeight] = rotatedSize(...sourceSize(source), turn)
  const scale = (cover ? Math.max : Math.min)(canvas.width / turnedWidth, canvas.height / turnedHeight)
  drawRotatedFrame(ctx, source, turn, {
    x: (canvas.width - turnedWidth * scale) / 2,
    y: (canvas.height - turnedHeight * scale) / 2,
    width: turnedWidth * scale,
    height: turnedHeight * scale,
  })
  return toGray(ctx.getImageData(0, 0, canvas.width, canvas.height).data, canvas.width, canvas.height)
}

// Small grayscale samples of the live video, taken several times a second.
// Copying the full frame to the CPU that often made the preview stutter
// (measured ~5x slower), so the frame is scaled on a GPU canvas that is never
// read back and only the small result is copied to a CPU canvas for reading.
// Both canvases persist, which keeps consecutive samples comparable.
export function createFrameSampler() {
  const scaled = document.createElement('canvas')
  const readable = document.createElement('canvas')
  return function sample(video, turn, width, height) {
    for (const canvas of [scaled, readable]) {
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
      }
    }
    drawRotatedFrame(scaled.getContext('2d'), video, turn, { x: 0, y: 0, width, height })
    const ctx = readable.getContext('2d', { willReadFrequently: true })
    ctx.drawImage(scaled, 0, 0)
    return toGray(ctx.getImageData(0, 0, width, height).data, width, height)
  }
}

// Full-size copy of the current video frame.
export function copyFrame(video) {
  const canvas = makeCanvas(video.videoWidth, video.videoHeight)
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height)
  return canvas
}

// The sheet's part of an upright canvas (with a margin), or all of it when no
// sheet is found with confidence.
function sheetRegion(canvas) {
  const sampleHeight = scaledHeight(ANALYSIS_WIDTH, sourceSize(canvas))
  const box = paperCropBox(detectPaper(grayscale(canvas, ANALYSIS_WIDTH, sampleHeight), ANALYSIS_WIDTH, sampleHeight))
  if (!box) return { x: 0, y: 0, width: canvas.width, height: canvas.height, cropped: false }
  return {
    x: box.x0 * canvas.width,
    y: box.y0 * canvas.height,
    width: (box.x1 - box.x0) * canvas.width,
    height: (box.y1 - box.y0) * canvas.height,
    cropped: true,
  }
}

// Whether a photo looks blurred (or its ink too faint) at the size grading
// will see it.  False when it cannot be judged, such as a blank page.
function looksBlurry(canvas) {
  const scale = Math.min(1, GRADING_EDGE / Math.max(canvas.width, canvas.height))
  const width = Math.round(canvas.width * scale)
  const height = Math.round(canvas.height * scale)
  const crispness = edgeCrispness(grayscale(canvas, width, height), width, height)
  return crispness != null && crispness < BLURRY_BELOW
}

// Turn the source upright, crop to the sheet (so the grading page's 1920 px
// limit is spent on paper, not desk), and encode the upload plus a thumbnail.
export async function renderShot(source, turn) {
  const [width, height] = rotatedSize(...sourceSize(source), turn)
  const scale = Math.min(1, RENDER_EDGE / Math.max(width, height))
  const upright = makeCanvas(width * scale, height * scale)
  drawRotatedFrame(smoothContext(upright), source, turn, { x: 0, y: 0, width: upright.width, height: upright.height })

  const region = sheetRegion(upright)
  const outScale = Math.min(1, MAX_UPLOAD_EDGE / Math.max(region.width, region.height))
  const out = makeCanvas(region.width * outScale, region.height * outScale)
  smoothContext(out).drawImage(upright, region.x, region.y, region.width, region.height, 0, 0, out.width, out.height)
  const blob = await jpegBlob(out, UPLOAD_QUALITY)

  const thumbScale = THUMB_EDGE / Math.max(out.width, out.height)
  const thumb = makeCanvas(out.width * thumbScale, out.height * thumbScale)
  thumb.getContext('2d').drawImage(out, 0, 0, thumb.width, thumb.height)
  return {
    blob,
    thumbUrl: URL.createObjectURL(await jpegBlob(thumb, THUMB_QUALITY)),
    blurry: looksBlurry(out),
    // The source size as seen upright, so it reads the same way as the upload.
    info: { width: out.width, height: out.height, sourceWidth: width, sourceHeight: height, cropped: region.cropped },
  }
}

// An <img> applies the photo's EXIF orientation, so a canvas copy of it is
// upright as shown on screen and carries no EXIF rotation to the server.
// Call `release` once the image has been drawn.
export function loadImageFile(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => resolve({ image, release: () => URL.revokeObjectURL(url) })
    image.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('无法读取照片'))
    }
    image.src = url
  })
}
