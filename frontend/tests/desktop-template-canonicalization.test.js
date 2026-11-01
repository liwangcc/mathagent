import assert from 'node:assert/strict'
import test from 'node:test'

import {
  appendDesktopCanonicalCandidates,
  decodeCanonicalTemplateEnvelope,
} from '../src/utils/desktopTemplateCanonicalization.js'

const metadata = {
  version: 'paper-canonical-v1',
  algorithm: 'opencv_page_quad_v1',
  source_coordinate_space: 'exif_display_pixels_top_left',
  canonical_coordinate_space: 'canonical_template_v1',
  source_format: 'png',
  exif_orientation: 1,
  encoded_width: 1200,
  encoded_height: 1600,
  source_width: 1200,
  source_height: 1600,
  source_to_canonical: [[1, 0, 32], [0, 1, 32], [0, 0, 1]],
  detected_page_quad: [[0, 0], [1199, 0], [1199, 1599], [0, 1599]],
  detection_method: 'full_frame_scan',
  confidence: 0.9,
  page_margin_px: 32,
  source_sha256: 'a'.repeat(64),
  canonical_sha256: 'b'.repeat(64),
  canonical_width: 1612,
  canonical_height: 2064,
}

function envelope(overrides = {}) {
  const body = new TextEncoder().encode(JSON.stringify({ ...metadata, ...overrides }))
  const png = new Uint8Array(24)
  png.set([137, 80, 78, 71, 13, 10, 26, 10], 0)
  png.set([73, 72, 68, 82], 12)
  new DataView(png.buffer).setUint32(16, 1612, false)
  new DataView(png.buffer).setUint32(20, 2064, false)
  const output = new Uint8Array(12 + body.length + png.length)
  output.set([77, 65, 84, 72, 67, 65, 78, 49], 0)
  new DataView(output.buffer).setUint32(8, body.length, true)
  output.set(body, 12)
  output.set(png, 12 + body.length)
  return output
}

test('desktop canonical envelope keeps metadata and exact PNG bytes paired', () => {
  const raw = envelope()
  const result = decodeCanonicalTemplateEnvelope(raw)

  assert.deepEqual(result.metadata, metadata)
  assert.equal(result.pngBytes.byteLength, 24)
  assert.deepEqual([...result.pngBytes], [...raw.subarray(raw.length - 24)])
})

test('desktop canonical envelope rejects paths, non-finite geometry and dimension mismatches', () => {
  assert.throws(
    () => decodeCanonicalTemplateEnvelope(envelope({ source_image: 'C:\\secret.png' })),
    /unexpected desktop canonical metadata/,
  )
  assert.throws(
    () => decodeCanonicalTemplateEnvelope(envelope({ source_to_canonical: [[null, 0, 0], [0, 1, 0], [0, 0, 1]] })),
    /metadata did not match/,
  )
  assert.throws(
    () => decodeCanonicalTemplateEnvelope(envelope({ canonical_width: 1600 })),
    /image dimensions did not match/,
  )
})

test('desktop canonical envelope rejects truncation and oversized metadata headers', () => {
  assert.throws(() => decodeCanonicalTemplateEnvelope(new Uint8Array(8)), /envelope/)
  const raw = envelope()
  new DataView(raw.buffer).setUint32(8, 64 * 1024, true)
  assert.throws(() => decodeCanonicalTemplateEnvelope(raw), /envelope size/)
})

test('desktop upload appends only path-free canonical files and ordered metadata', async () => {
  const appended = []
  const previousWindow = globalThis.window
  const previousFile = globalThis.File
  globalThis.File = class TestFile {
    constructor(parts, name, options) {
      this.parts = parts
      this.name = name
      this.type = options.type
    }
  }
  globalThis.window = {
    __TAURI_INTERNALS__: {
      invoke: async (command, body) => {
        assert.equal(command, 'canonicalize_template_image')
        assert.ok(body instanceof Uint8Array)
        return envelope()
      },
    },
  }
  try {
    const formData = { append: (name, value) => appended.push([name, value]) }
    const source = { arrayBuffer: async () => Uint8Array.from([137, 80, 78, 71]).buffer }

    assert.equal(await appendDesktopCanonicalCandidates(formData, [source]), true)
    assert.equal(appended[0][0], 'canonical_files')
    assert.equal(appended[0][1].name, 'canonical-page-1.png')
    assert.equal(appended[1][0], 'canonical_metadata')
    assert.deepEqual(JSON.parse(appended[1][1]), [metadata])
    assert.doesNotMatch(appended[1][1], /source_image|[A-Z]:\\|\/mnt\//)
  } finally {
    globalThis.window = previousWindow
    globalThis.File = previousFile
  }
})
