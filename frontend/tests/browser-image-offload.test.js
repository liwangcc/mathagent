import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { compressImageFiles, createRotatedOcrReadyFile } from '../src/utils/uploadValidation.js'

const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const answerSheet = read('../src/views/AnswerSheet.vue')
const upload = read('../src/views/Upload.vue')

test('AnswerSheet FilePicker serially prepares every selected image and blocks duplicate selection', () => {
  assert.match(answerSheet, /import \{ compressImageFiles, validateUploadList \} from '@\/utils\/uploadValidation'/)
  assert.equal((answerSheet.match(/<FilePicker/g) || []).length, 4)
  assert.match(answerSheet, /const selecting = ref\(false\)/)
  assert.match(answerSheet, /if \(selecting\.value \|\| props\.disabled \|\| !selected\.length\) return/)
  assert.match(answerSheet, /disabled: props\.disabled \|\| selecting\.value/)
  assert.match(answerSheet, /await compressImageFiles\(result\.files, 1920, 0\.88/)
  assert.match(answerSheet, /fileSelectionProcessingCount\.value \+= 1/)
  assert.match(answerSheet, /:disabled="sourceLocked \|\| fileSelectionProcessing"/)
  assert.match(answerSheet, /:disabled="layoutFrozen \|\| fileSelectionProcessing"/)
  assert.match(answerSheet, /studentUploadProcessing \|\| fileSelectionProcessing/)
})

test('Upload prepares both template and student images before either recognition path', () => {
  const templateStart = upload.indexOf('async function onTemplateFileChange(e)')
  const templateEnd = upload.indexOf('function templateRecognitionStorageKey()', templateStart)
  const studentStart = upload.indexOf('async function processFiles(')
  const studentEnd = upload.indexOf('async function onFileInputChange(e)', studentStart)
  assert.ok(templateStart >= 0 && templateEnd > templateStart)
  assert.ok(studentStart >= 0 && studentEnd > studentStart)

  const templatePath = upload.slice(templateStart, templateEnd)
  const studentPath = upload.slice(studentStart, studentEnd)
  assert.match(templatePath, /templatePreprocessing\.value = true/)
  assert.match(templatePath, /compressImageFiles\(result\.files, 1920, 0\.88/)
  assert.match(studentPath, /imagePreprocessing\.value = true/)
  assert.match(studentPath, /const preparedFiles = await compressImageFiles\(result\.files, 1920, 0\.88/)
  assert.match(studentPath, /if \(failedCount\)[\s\S]*fileList\.value = \[\]/)
  assert.doesNotMatch(studentPath, /fileList\.value = result\.files/)
  assert.match(upload, /:disabled="imagePreprocessing \|\| imageExtracting \|\| recognitionBlocksNewWork"/)
  assert.match(upload, /:disabled="templatePreprocessing \|\| templateServiceBlocksNewWork"/)
})

function pngBytes(width, height) {
  const bytes = new Uint8Array(24)
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52])
  const view = new DataView(bytes.buffer)
  view.setUint32(16, width, false)
  view.setUint32(20, height, false)
  return bytes
}

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

// Stands in for the two browser APIs the preprocessor uses, createImageBitmap
// and a 2D canvas, and logs each step: reading a file's header, decoding,
// allocating a canvas, encoding (with its JPEG quality) and closing a bitmap.
function installImagePipeline({ beforeDecode, decodedSize, encodeFails } = {}) {
  const previous = { createImageBitmap: globalThis.createImageBitmap, document: globalThis.document }
  const pipeline = { log: [], canvases: [], active: 0, maxActive: 0, decoding: '' }
  globalThis.createImageBitmap = async (file, options) => {
    pipeline.log.push(`decode ${file.name}`)
    pipeline.decoding = file.name
    pipeline.active += 1
    pipeline.maxActive = Math.max(pipeline.maxActive, pipeline.active)
    await beforeDecode?.(file)
    const size = decodedSize?.(file) ?? { width: options.resizeWidth, height: options.resizeHeight }
    return { ...size, close() { pipeline.active -= 1; pipeline.log.push(`close ${file.name}`) } }
  }
  globalThis.document = {
    createElement(tag) {
      pipeline.log.push(`canvas ${tag}`)
      const canvas = {
        width: 0,
        height: 0,
        getContext: () => ({
          fillRect() {}, drawImage() {}, translate() {}, rotate() {}, setTransform() {}, putImageData() {},
          getImageData: () => ({ data: new Uint8ClampedArray(4) }),
        }),
        toBlob(callback, type, quality) {
          pipeline.log.push(`encode ${quality}`)
          callback(encodeFails?.(pipeline.decoding) ? null : new Blob(['jpeg'], { type }))
        },
      }
      pipeline.canvases.push(canvas)
      return canvas
    },
  }
  pipeline.file = (name, width, height) => {
    const file = new File([pngBytes(width, height)], name, { type: 'image/png' })
    const slice = file.slice.bind(file)
    file.slice = (...args) => {
      pipeline.log.push(`read ${name}`)
      return slice(...args)
    }
    return file
  }
  pipeline.restore = () => {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
  return pipeline
}

const stepsFor = (name, quality = 0.88) => [`read ${name}`, `decode ${name}`, 'canvas canvas', `encode ${quality}`, `close ${name}`]

test('the shared OCR-ready contract is globally serial and cannot label failed originals ready', async () => {
  // Two callers at once: every image, across both batches, is read, decoded,
  // encoded and released one at a time, in input order.
  const firstDecoding = deferred()
  const releaseFirst = deferred()
  const serial = installImagePipeline({
    beforeDecode: (file) => {
      if (file.name !== 'a1.png') return undefined
      firstDecoding.resolve()
      return releaseFirst.promise
    },
  })
  try {
    const first = compressImageFiles([serial.file('a1.png', 800, 600), serial.file('a2.png', 800, 600)])
    const second = compressImageFiles([serial.file('b1.png', 800, 600)])
    await firstDecoding.promise
    assert.deepEqual(serial.log, ['read a1.png', 'decode a1.png'], 'no other image starts while one is decoding')
    releaseFirst.resolve()
    const results = (await Promise.all([first, second])).flat()
    assert.deepEqual(serial.log, [...stepsFor('a1.png'), ...stepsFor('a2.png'), ...stepsFor('b1.png')])
    assert.equal(serial.maxActive, 1)
    assert.deepEqual(results.map((file) => [file.name, file.type]), [
      ['a1.ocr-ready-v1.jpg', 'image/jpeg'],
      ['a2.ocr-ready-v1.jpg', 'image/jpeg'],
      ['b1.ocr-ready-v1.jpg', 'image/jpeg'],
    ])
    assert.ok(serial.canvases.every((canvas) => canvas.width === 0 && canvas.height === 0), 'canvas memory is released')
  } finally {
    serial.restore()
  }

  // Failures are reported with their position and never returned as ready
  // files. The 16M-pixel budget is checked on the header before decoding and
  // on the decoded bitmap before a canvas is allocated.
  const failing = installImagePipeline({
    decodedSize: (file) => (file.name === 'decoded-huge.png' ? { width: 5000, height: 4000 } : undefined),
    encodeFails: (name) => name === 'retry.ocr-ready-v1.jpg',
  })
  try {
    const failures = []
    const results = await compressImageFiles([
      failing.file('exact.png', 4000, 4000),
      failing.file('over.png', 4001, 4000),
      failing.file('decoded-huge.png', 100, 100),
      failing.file('retry.ocr-ready-v1.jpg', 100, 100),
    ], 1920, 0.88, { onFileError: (failure) => failures.push(failure) })
    assert.deepEqual(results.map((file) => file.name), ['exact.ocr-ready-v1.jpg'])
    assert.deepEqual(failures.map(({ file, index, error }) => [file.name, index, error.code]), [
      ['over.png', 1, 'IMAGE_DECODE_PIXEL_LIMIT_EXCEEDED'],
      ['decoded-huge.png', 2, 'IMAGE_DECODE_PIXEL_LIMIT_EXCEEDED'],
      ['retry.ocr-ready-v1.jpg', 3, undefined],
    ])
    assert.equal(failures[0].error.maxPixels, 16_000_000)
    assert.deepEqual(failing.log, [
      ...stepsFor('exact.png'),
      'read over.png',
      'read decoded-huge.png', 'decode decoded-huge.png', 'close decoded-huge.png',
      ...stepsFor('retry.ocr-ready-v1.jpg'),
    ])
    assert.equal(failing.active, 0, 'every decoded bitmap is closed')
    assert.ok(failing.canvases.every((canvas) => canvas.width === 0 && canvas.height === 0), 'canvas memory is released')
  } finally {
    failing.restore()
  }

  // Teacher rotation produces a new OCR-ready JPEG through the same pipeline.
  const rotation = installImagePipeline()
  try {
    const rotated = await createRotatedOcrReadyFile(rotation.file('student.png', 1200, 800), 1)
    assert.deepEqual([rotated.name, rotated.type], ['student.ocr-ready-v1.jpg', 'image/jpeg'])
    assert.deepEqual(rotation.log, stepsFor('student.png'))
    assert.deepEqual([rotation.canvases[0].width, rotation.canvases[0].height], [0, 0])
  } finally {
    rotation.restore()
  }
})
