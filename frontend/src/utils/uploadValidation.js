import { getUploadSourcePath, withUploadSourcePath } from './uploadSource.js'

const ALLOWED_TYPES = new Set(["image/jpeg","image/png","image/gif","image/bmp","image/webp","application/pdf"])
const DEFAULT_MAX_FILE_SIZE = 20 * 1024 * 1024
const DEFAULT_MAX_EDGE = 1920
const DEFAULT_QUALITY = 0.88
const MAX_DECODED_SOURCE_PIXELS = 16_000_000
const MAX_IMAGE_HEADER_BYTES = 512 * 1024
const MAX_INK_FILTER_STRIP_BYTES = 512 * 1024
const MAX_INK_FILTER_STRIP_ROWS = 64
const OCR_READY_SUFFIX = '.ocr-ready-v1.jpg'
const OCR_READY_NAME_RE = /\.ocr-ready-v1\.jpg$/i
let imagePreprocessTail = Promise.resolve()

export function validateUploadList(list, options = {}) {
  const {
    maxFiles = 20,
    maxFileSize = DEFAULT_MAX_FILE_SIZE,
  } = options

  const errors = []
  let files = Array.isArray(list) ? [...list] : []

  if (files.length > maxFiles) {
    errors.push(`单次最多上传 ${maxFiles} 个文件`)
    files = files.slice(0, maxFiles)
  }

  const validFiles = []
  for (const file of files) {
    const raw = file?.raw || file
    if (!raw?.type || !ALLOWED_TYPES.has(raw.type)) {
      errors.push((file.name || raw?.name || '文件') + " 类型不支持，仅允许图片和PDF")
      continue
    }
    if (raw?.size > maxFileSize) {
      errors.push(`${file.name || raw.name || '文件'} 超过 20MB`)
      continue
    }
    validFiles.push(file)
  }

  return {
    files: validFiles,
    errors,
  }
}

/**
 * 压缩图片：限制最大边长 + JPEG质量压缩
 * @param {File} file - 原始图片文件
 * @param {number} maxEdge - 最大边长（默认1920px）
 * @param {number} quality - JPEG质量（默认0.88）
 * @returns {Promise<Blob>} 压缩后的Blob
 */
function loadImageElement(file) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    const url = URL.createObjectURL(file)
    img.onload = () => {
      URL.revokeObjectURL(url)
      resolve({
        source: img,
        width: img.naturalWidth || img.width,
        height: img.naturalHeight || img.height,
        close: () => {
          img.onload = null
          img.onerror = null
          img.removeAttribute?.('src')
        },
      })
    }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('load failed')) }
    img.src = url
  })
}

function readUint24LE(bytes, offset) {
  return bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16)
}

function hasBytes(bytes, offset, expected) {
  if (offset < 0 || offset + expected.length > bytes.length) return false
  return expected.every((value, index) => bytes[offset + index] === value)
}

function parseTiffOrientation(bytes, tiffOffset, limit = bytes.length) {
  if (tiffOffset < 0 || tiffOffset + 8 > limit) return 1
  const littleEndian = hasBytes(bytes, tiffOffset, [0x49, 0x49])
  const bigEndian = hasBytes(bytes, tiffOffset, [0x4d, 0x4d])
  if (!littleEndian && !bigEndian) return 1

  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const read16 = (offset) => {
    if (offset < tiffOffset || offset + 2 > limit) return null
    return view.getUint16(offset, littleEndian)
  }
  const read32 = (offset) => {
    if (offset < tiffOffset || offset + 4 > limit) return null
    return view.getUint32(offset, littleEndian)
  }
  if (read16(tiffOffset + 2) !== 42) return 1
  const firstIfdOffset = read32(tiffOffset + 4)
  if (firstIfdOffset === null) return 1
  const ifdOffset = tiffOffset + firstIfdOffset
  const entryCount = read16(ifdOffset)
  if (entryCount === null) return 1

  const boundedEntryCount = Math.min(entryCount, Math.floor((limit - ifdOffset - 2) / 12))
  for (let index = 0; index < boundedEntryCount; index += 1) {
    const entryOffset = ifdOffset + 2 + (index * 12)
    if (read16(entryOffset) !== 0x0112) continue
    const type = read16(entryOffset + 2)
    const count = read32(entryOffset + 4)
    if (type !== 3 || count !== 1) return 1
    const orientation = read16(entryOffset + 8)
    return orientation >= 1 && orientation <= 8 ? orientation : 1
  }
  return 1
}

function parseJpegMetadata(bytes) {
  if (!hasBytes(bytes, 0, [0xff, 0xd8])) return null
  let offset = 2
  let width = 0
  let height = 0
  let orientation = 1
  while (offset + 1 < bytes.length) {
    while (offset < bytes.length && bytes[offset] === 0xff) offset += 1
    if (offset >= bytes.length) break
    const marker = bytes[offset]
    offset += 1
    if (marker === 0xd9 || marker === 0xda) break
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue
    if (offset + 2 > bytes.length) break
    const segmentLength = (bytes[offset] << 8) | bytes[offset + 1]
    if (segmentLength < 2) break
    const segmentEnd = offset + segmentLength
    if (segmentEnd > bytes.length) break

    if (marker === 0xe1 && segmentLength >= 8 && hasBytes(bytes, offset + 2, [0x45, 0x78, 0x69, 0x66, 0, 0])) {
      orientation = parseTiffOrientation(bytes, offset + 8, segmentEnd)
    }
    const isStartOfFrame = (
      (marker >= 0xc0 && marker <= 0xc3)
      || (marker >= 0xc5 && marker <= 0xc7)
      || (marker >= 0xc9 && marker <= 0xcb)
      || (marker >= 0xcd && marker <= 0xcf)
    )
    if (isStartOfFrame && segmentLength >= 7) {
      height = (bytes[offset + 3] << 8) | bytes[offset + 4]
      width = (bytes[offset + 5] << 8) | bytes[offset + 6]
    }
    offset = segmentEnd
  }
  return width && height ? { width, height, orientation } : null
}

function parsePngMetadata(bytes) {
  if (!hasBytes(bytes, 0, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return null
  if (bytes.length < 24 || !hasBytes(bytes, 12, [0x49, 0x48, 0x44, 0x52])) return null
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const width = view.getUint32(16, false)
  const height = view.getUint32(20, false)
  let orientation = 1
  let offset = 8
  while (offset + 12 <= bytes.length) {
    const chunkLength = view.getUint32(offset, false)
    const dataOffset = offset + 8
    const chunkEnd = dataOffset + chunkLength
    if (!Number.isSafeInteger(chunkEnd) || chunkEnd + 4 > bytes.length) break
    if (hasBytes(bytes, offset + 4, [0x65, 0x58, 0x49, 0x66])) {
      orientation = parseTiffOrientation(bytes, dataOffset, chunkEnd)
    }
    if (hasBytes(bytes, offset + 4, [0x49, 0x44, 0x41, 0x54])) break
    offset = chunkEnd + 4
  }
  return width && height ? { width, height, orientation } : null
}

function parseGifMetadata(bytes) {
  const validSignature = hasBytes(bytes, 0, [0x47, 0x49, 0x46, 0x38, 0x37, 0x61])
    || hasBytes(bytes, 0, [0x47, 0x49, 0x46, 0x38, 0x39, 0x61])
  if (!validSignature || bytes.length < 10) return null
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const width = view.getUint16(6, true)
  const height = view.getUint16(8, true)
  return width && height ? { width, height, orientation: 1 } : null
}

function parseBmpMetadata(bytes) {
  if (!hasBytes(bytes, 0, [0x42, 0x4d]) || bytes.length < 26) return null
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const dibSize = view.getUint32(14, true)
  let width
  let height
  if (dibSize === 12) {
    width = view.getUint16(18, true)
    height = view.getUint16(20, true)
  } else if (dibSize >= 40) {
    width = Math.abs(view.getInt32(18, true))
    height = Math.abs(view.getInt32(22, true))
  }
  return width && height ? { width, height, orientation: 1 } : null
}

function parseWebpMetadata(bytes) {
  if (!hasBytes(bytes, 0, [0x52, 0x49, 0x46, 0x46]) || !hasBytes(bytes, 8, [0x57, 0x45, 0x42, 0x50])) return null
  if (bytes.length < 20) return null
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  let width = 0
  let height = 0
  let orientation = 1
  let offset = 12
  while (offset + 8 <= bytes.length) {
    const chunkSize = view.getUint32(offset + 4, true)
    const dataOffset = offset + 8
    const chunkEnd = dataOffset + chunkSize
    if (!Number.isSafeInteger(chunkEnd)) break
    if (hasBytes(bytes, offset, [0x56, 0x50, 0x38, 0x58]) && chunkSize >= 10 && dataOffset + 10 <= bytes.length) {
      width = readUint24LE(bytes, dataOffset + 4) + 1
      height = readUint24LE(bytes, dataOffset + 7) + 1
    } else if (hasBytes(bytes, offset, [0x56, 0x50, 0x38, 0x20]) && chunkSize >= 10 && dataOffset + 10 <= bytes.length) {
      if (hasBytes(bytes, dataOffset + 3, [0x9d, 0x01, 0x2a])) {
        width = view.getUint16(dataOffset + 6, true) & 0x3fff
        height = view.getUint16(dataOffset + 8, true) & 0x3fff
      }
    } else if (hasBytes(bytes, offset, [0x56, 0x50, 0x38, 0x4c]) && chunkSize >= 5 && dataOffset + 5 <= bytes.length) {
      if (bytes[dataOffset] === 0x2f) {
        const packed = view.getUint32(dataOffset + 1, true)
        width = (packed & 0x3fff) + 1
        height = ((packed >>> 14) & 0x3fff) + 1
      }
    } else if (hasBytes(bytes, offset, [0x45, 0x58, 0x49, 0x46])) {
      const hasExifPrefix = hasBytes(bytes, dataOffset, [0x45, 0x78, 0x69, 0x66, 0, 0])
      orientation = parseTiffOrientation(
        bytes,
        dataOffset + (hasExifPrefix ? 6 : 0),
        Math.min(chunkEnd, bytes.length),
      )
    }
    if (chunkEnd > bytes.length) break
    offset = chunkEnd + (chunkSize % 2)
  }
  return width && height ? { width, height, orientation } : null
}

function parseImageMetadata(bytes) {
  return parseJpegMetadata(bytes)
    || parsePngMetadata(bytes)
    || parseGifMetadata(bytes)
    || parseBmpMetadata(bytes)
    || parseWebpMetadata(bytes)
}

async function inspectImageBeforeDecode(file) {
  const headerSize = Math.min(Number(file?.size) || 0, MAX_IMAGE_HEADER_BYTES)
  if (!headerSize || typeof file?.slice !== 'function') {
    const error = new Error('unable to read image dimensions before decoding')
    error.code = 'IMAGE_HEADER_INVALID'
    throw error
  }
  const bytes = new Uint8Array(await file.slice(0, headerSize).arrayBuffer())
  const metadata = parseImageMetadata(bytes)
  if (!metadata) {
    const error = new Error('unable to read image dimensions before decoding')
    error.code = 'IMAGE_HEADER_INVALID'
    throw error
  }
  assertDecodedImageWithinPixelBudget(metadata.width, metadata.height)
  return metadata
}

function calculateTargetDimensions(metadata, maxEdge) {
  const swapsAxes = metadata.orientation >= 5 && metadata.orientation <= 8
  const orientedWidth = swapsAxes ? metadata.height : metadata.width
  const orientedHeight = swapsAxes ? metadata.width : metadata.height
  const scale = Math.min(1, maxEdge / Math.max(orientedWidth, orientedHeight))
  return {
    width: Math.max(1, Math.round(orientedWidth * scale)),
    height: Math.max(1, Math.round(orientedHeight * scale)),
  }
}

async function decodeImageForCanvas(file, target) {
  if (typeof globalThis.createImageBitmap === 'function') {
    try {
      // Canvas output drops EXIF. Decode with the camera orientation applied
      // first so the normalized pixels remain correct in the saved upload.
      const bitmap = await globalThis.createImageBitmap(file, {
        imageOrientation: 'from-image',
        resizeWidth: target.width,
        resizeHeight: target.height,
        resizeQuality: 'high',
      })
      return {
        source: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        close: () => bitmap.close?.(),
      }
    } catch {
      try {
        // Older implementations expose createImageBitmap but reject resize options.
        const bitmap = await globalThis.createImageBitmap(file, {
          imageOrientation: 'from-image',
        })
        return {
          source: bitmap,
          width: bitmap.width,
          height: bitmap.height,
          close: () => bitmap.close?.(),
        }
      } catch {
        // Fall back to the browser image decoder below.
      }
    }
  }
  return loadImageElement(file)
}

function assertDecodedImageWithinPixelBudget(width, height) {
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) {
    throw new Error('invalid image dimensions')
  }
  if (width > Math.floor(MAX_DECODED_SOURCE_PIXELS / height)) {
    const error = new Error(`decoded image exceeds ${MAX_DECODED_SOURCE_PIXELS} pixel limit`)
    error.code = 'IMAGE_DECODE_PIXEL_LIMIT_EXCEEDED'
    error.width = width
    error.height = height
    error.maxPixels = MAX_DECODED_SOURCE_PIXELS
    throw error
  }
}

function suppressColoredReviewInk(ctx, width, height) {
  const rowsByMemory = Math.max(1, Math.floor(MAX_INK_FILTER_STRIP_BYTES / (width * 4)))
  const stripRows = Math.min(MAX_INK_FILTER_STRIP_ROWS, rowsByMemory)
  for (let y = 0; y < height; y += stripRows) {
    const currentRows = Math.min(stripRows, height - y)
    let imageData = null
    let pixels = null
    try {
      imageData = ctx.getImageData(0, y, width, currentRows)
      pixels = imageData.data
      let changed = false
      for (let offset = 0; offset < pixels.length; offset += 4) {
        const red = pixels[offset]
        const green = pixels[offset + 1]
        const blue = pixels[offset + 2]
        const spread = Math.max(red, green, blue) - Math.min(red, green, blue)
        const redInk = red >= 110 && red - green >= 35 && red - blue >= 35
        const greenInk = green >= 90 && green - red >= 25 && green - blue >= 20
        if ((redInk || greenInk) && spread >= 40) {
          pixels[offset] = 255
          pixels[offset + 1] = 255
          pixels[offset + 2] = 255
          pixels[offset + 3] = 255
          changed = true
        }
      }
      if (changed) ctx.putImageData(imageData, 0, y)
    } finally {
      pixels = null
      imageData = null
    }
  }
}

function createOcrReadyFile(blob, source) {
  const sourceName = String(source?.name || 'image')
  const baseName = sourceName
    .replace(OCR_READY_NAME_RE, '')
    .replace(/\.[^.]+$/, '') || 'image'
  const file = new File([blob], `${baseName}${OCR_READY_SUFFIX}`, {
    type: 'image/jpeg',
    lastModified: Number(source?.lastModified) || Date.now(),
  })
  return withUploadSourcePath(file, getUploadSourcePath(source))
}

// `suppressReviewInk: false` keeps red/green ink, for photos that are stored
// as the student took them (student self-upload) rather than sent to OCR.
export async function compressImage(
  file,
  maxEdge = DEFAULT_MAX_EDGE,
  quality = DEFAULT_QUALITY,
  quarterTurnsClockwise = 0,
  { suppressReviewInk = true } = {},
) {
  const metadata = await inspectImageBeforeDecode(file)
  const target = calculateTargetDimensions(metadata, maxEdge)
  let decoded = await decodeImageForCanvas(file, target)
  let canvas = null
  try {
    let { width, height } = decoded
    assertDecodedImageWithinPixelBudget(width, height)
    const scale = Math.min(1, maxEdge / Math.max(width, height))
    width = Math.round(width * scale)
    height = Math.round(height * scale)
    const turns = ((Number(quarterTurnsClockwise) % 4) + 4) % 4
    const swapsAxes = turns % 2 === 1
    canvas = document.createElement('canvas')
    canvas.width = swapsAxes ? height : width
    canvas.height = swapsAxes ? width : height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('canvas context unavailable')
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    if (turns) {
      ctx.translate(canvas.width / 2, canvas.height / 2)
      ctx.rotate(turns * Math.PI / 2)
      ctx.drawImage(decoded.source, -width / 2, -height / 2, width, height)
      ctx.setTransform(1, 0, 0, 1, 0, 0)
    } else {
      ctx.drawImage(decoded.source, 0, 0, width, height)
    }
    if (suppressReviewInk) suppressColoredReviewInk(ctx, canvas.width, canvas.height)
    return await new Promise((resolve, reject) => {
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error('toBlob failed'))),
        'image/jpeg',
        quality,
      )
    })
  } finally {
    try {
      if (canvas) {
        canvas.width = 0
        canvas.height = 0
      }
    } finally {
      decoded?.close()
      decoded = null
      canvas = null
    }
  }
}

/**
 * 批量压缩图片文件列表
 * @param {File[]} files - 图片文件数组
 * @param {number} maxEdge - 最大边长
 * @param {number} quality - JPEG质量
 * @param {{onFileError?: Function}} options - 单文件失败回调
 * @returns {Promise<File[]>} 按输入顺序处理后的文件数组
 */
export async function compressImageFiles(
  files,
  maxEdge = DEFAULT_MAX_EDGE,
  quality = DEFAULT_QUALITY,
  options = {},
) {
  let releaseBatch
  const previousBatch = imagePreprocessTail
  imagePreprocessTail = new Promise((resolve) => { releaseBatch = resolve })
  await previousBatch
  try {
    const results = []
    for (const [index, file] of files.entries()) {
      const raw = file?.raw || file
      if (!raw?.type?.startsWith('image/')) {
        results.push(file)
        continue
      }
      try {
        const blob = await compressImage(raw, maxEdge, quality)
        results.push(createOcrReadyFile(blob, raw))
      } catch (error) {
        try {
          options.onFileError?.({ file: raw, index, error })
        } catch {
          // A UI notification failure must not interrupt the sequential batch.
        }
      }
    }
    return results
  } finally {
    releaseBatch()
  }
}

export async function createRotatedOcrReadyFile(
  source,
  quarterTurnsClockwise,
  maxEdge = DEFAULT_MAX_EDGE,
  quality = DEFAULT_QUALITY,
) {
  const blob = await compressImage(
    source,
    maxEdge,
    quality,
    quarterTurnsClockwise,
  )
  return createOcrReadyFile(blob, source)
}
