const ENVELOPE_MAGIC = Uint8Array.from([0x4d, 0x41, 0x54, 0x48, 0x43, 0x41, 0x4e, 0x31])
const ENVELOPE_HEADER_BYTES = 12
const MAX_METADATA_BYTES = 32 * 1024
const MAX_CANONICAL_BYTES = 32 * 1024 * 1024
const SHA256 = /^[0-9a-f]{64}$/

const CANONICAL_FIELDS = new Set([
  'version',
  'algorithm',
  'source_coordinate_space',
  'canonical_coordinate_space',
  'source_format',
  'exif_orientation',
  'encoded_width',
  'encoded_height',
  'source_width',
  'source_height',
  'source_to_canonical',
  'detected_page_quad',
  'detection_method',
  'confidence',
  'page_margin_px',
  'source_sha256',
  'canonical_sha256',
  'canonical_width',
  'canonical_height',
])

function asBytes(value) {
  if (value instanceof Uint8Array) return value
  if (value instanceof ArrayBuffer) return new Uint8Array(value)
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  }
  throw new Error('invalid desktop canonical response')
}

function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0
}

function validateMatrix(value, rows, columns) {
  return Array.isArray(value)
    && value.length === rows
    && value.every((row) => Array.isArray(row)
      && row.length === columns
      && row.every(Number.isFinite))
}

function validateCanonicalMetadata(metadata) {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
    throw new Error('invalid desktop canonical metadata')
  }
  if (Object.keys(metadata).some((field) => !CANONICAL_FIELDS.has(field))) {
    throw new Error('unexpected desktop canonical metadata')
  }
  if (
    metadata.version !== 'paper-canonical-v1'
    || metadata.algorithm !== 'opencv_page_quad_v1'
    || metadata.source_coordinate_space !== 'exif_display_pixels_top_left'
    || metadata.canonical_coordinate_space !== 'canonical_template_v1'
    || !['jpeg', 'png'].includes(metadata.source_format)
    || !Number.isInteger(metadata.exif_orientation)
    || metadata.exif_orientation < 1
    || metadata.exif_orientation > 8
    || !isPositiveInteger(metadata.encoded_width)
    || !isPositiveInteger(metadata.encoded_height)
    || !isPositiveInteger(metadata.source_width)
    || !isPositiveInteger(metadata.source_height)
    || !validateMatrix(metadata.source_to_canonical, 3, 3)
    || !validateMatrix(metadata.detected_page_quad, 4, 2)
    || !['detected_quadrilateral', 'full_frame_scan'].includes(metadata.detection_method)
    || !Number.isFinite(metadata.confidence)
    || metadata.confidence < 0
    || metadata.confidence > 1
    || metadata.page_margin_px !== 32
    || !SHA256.test(metadata.source_sha256)
    || !SHA256.test(metadata.canonical_sha256)
    || !isPositiveInteger(metadata.canonical_width)
    || !isPositiveInteger(metadata.canonical_height)
    || Math.max(metadata.canonical_width, metadata.canonical_height) !== 2064
  ) {
    throw new Error('desktop canonical metadata did not match')
  }
  return metadata
}

function validatePng(bytes, metadata) {
  const signature = [137, 80, 78, 71, 13, 10, 26, 10]
  if (
    bytes.length < 24
    || signature.some((value, index) => bytes[index] !== value)
    || String.fromCharCode(...bytes.subarray(12, 16)) !== 'IHDR'
  ) {
    throw new Error('desktop canonical image was not PNG')
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  if (
    view.getUint32(16, false) !== metadata.canonical_width
    || view.getUint32(20, false) !== metadata.canonical_height
  ) {
    throw new Error('desktop canonical image dimensions did not match')
  }
}

export function decodeCanonicalTemplateEnvelope(value) {
  const bytes = asBytes(value)
  if (
    bytes.length <= ENVELOPE_HEADER_BYTES
    || ENVELOPE_MAGIC.some((expected, index) => bytes[index] !== expected)
  ) {
    throw new Error('invalid desktop canonical envelope')
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const metadataLength = view.getUint32(ENVELOPE_MAGIC.length, true)
  const imageOffset = ENVELOPE_HEADER_BYTES + metadataLength
  if (
    metadataLength === 0
    || metadataLength > MAX_METADATA_BYTES
    || imageOffset >= bytes.length
    || bytes.length - imageOffset > MAX_CANONICAL_BYTES
  ) {
    throw new Error('invalid desktop canonical envelope size')
  }
  let metadata
  try {
    const raw = bytes.subarray(ENVELOPE_HEADER_BYTES, imageOffset)
    metadata = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw))
  } catch {
    throw new Error('invalid desktop canonical metadata JSON')
  }
  validateCanonicalMetadata(metadata)
  const pngBytes = bytes.subarray(imageOffset)
  validatePng(pngBytes, metadata)
  return { metadata, pngBytes }
}

export async function appendDesktopCanonicalCandidates(formData, files) {
  const invoke = globalThis.window?.__TAURI_INTERNALS__?.invoke
  if (typeof invoke !== 'function') return false
  if (!formData || typeof formData.append !== 'function' || !Array.isArray(files) || !files.length) {
    return false
  }

  try {
    const metadataItems = []
    const canonicalFiles = []
    for (let pageIndex = 0; pageIndex < files.length; pageIndex += 1) {
      const source = files[pageIndex]?.raw || files[pageIndex]
      if (!source || typeof source.arrayBuffer !== 'function') {
        throw new Error('invalid template source')
      }
      const response = await invoke(
        'canonicalize_template_image',
        new Uint8Array(await source.arrayBuffer()),
      )
      const { metadata, pngBytes } = decodeCanonicalTemplateEnvelope(response)
      metadataItems.push(metadata)
      canonicalFiles.push(new File(
        [pngBytes],
        `canonical-page-${pageIndex + 1}.png`,
        { type: 'image/png' },
      ))
    }
    canonicalFiles.forEach((file) => formData.append('canonical_files', file))
    formData.append('canonical_metadata', JSON.stringify(metadataItems))
    return true
  } catch {
    return false
  }
}
