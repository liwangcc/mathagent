import assert from 'node:assert/strict'
import test from 'node:test'
import { compressImageFiles, createRotatedOcrReadyFile } from '../src/utils/uploadValidation.js'
import { memoryDocument } from './helpers/memoryDom.js'
import { mountAnswerSheet } from './helpers/sourcesReview.js'
import { formFields, mountUpload } from './helpers/uploadPage.js'

// Every image a teacher picks is prepared in the browser (decoded, scaled to
// at most 1920 px, written as a 0.88-quality OCR-ready JPEG) before anything
// is uploaded, one image at a time, while the pages that take the images keep
// their import controls locked.

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
// `encodes` lists each JPEG written with its quality and pixel size. With
// `document` (a mounted page's) only its canvas elements are replaced;
// without one a stand-in global `document` provides them.
function installImagePipeline({ beforeDecode, decodedSize, encodeFails, document } = {}) {
  const previous = { createImageBitmap: globalThis.createImageBitmap, document: globalThis.document }
  const pipeline = { log: [], canvases: [], encodes: [], active: 0, maxActive: 0, decoding: '' }
  globalThis.createImageBitmap = async (file, options) => {
    pipeline.log.push(`decode ${file.name}`)
    pipeline.decoding = file.name
    pipeline.active += 1
    pipeline.maxActive = Math.max(pipeline.maxActive, pipeline.active)
    await beforeDecode?.(file)
    const size = decodedSize?.(file) ?? { width: options.resizeWidth, height: options.resizeHeight }
    return { ...size, close() { pipeline.active -= 1; pipeline.log.push(`close ${file.name}`) } }
  }
  const createCanvas = (tag) => {
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
        pipeline.encodes.push({ name: pipeline.decoding, quality, width: canvas.width, height: canvas.height })
        callback(encodeFails?.(pipeline.decoding) ? null : new Blob(['jpeg'], { type }))
      },
    }
    pipeline.canvases.push(canvas)
    return canvas
  }
  const createElement = document?.createElement
  if (document) document.createElement = tag => (tag === 'canvas' ? createCanvas(tag) : createElement(tag))
  else globalThis.document = { createElement: createCanvas }
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
    if (document) document.createElement = createElement
    for (const [name, value] of Object.entries(previous)) {
      if (document && name === 'document') continue
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
  return pipeline
}

const stepsFor = (name, quality = 0.88) => [`read ${name}`, `decode ${name}`, 'canvas canvas', `encode ${quality}`, `close ${name}`]

// The image pipeline of a mounted page; `hold()` keeps the next decode of a
// named image waiting until the returned release() is called.
function pagePipeline(t, options = {}) {
  const holds = new Map()
  const pipeline = installImagePipeline({
    ...options,
    document: memoryDocument,
    beforeDecode: file => holds.get(file.name)?.promise,
  })
  // Preparation is serial across the whole app: a decode a failed test left
  // held would block every later test, so it is let go first.
  t.after(async () => {
    for (const gate of holds.values()) gate.resolve()
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve))
    pipeline.restore()
  })
  pipeline.hold = (name) => {
    const gate = deferred()
    holds.set(name, gate)
    return () => { holds.delete(name); gate.resolve() }
  }
  return pipeline
}

// Choose files through a file input as the browser does; the change event
// fires even if a test does it while the input is disabled, to check that the
// page itself ignores it.
async function choose(input, files, settle) {
  input.files = files
  input.value = 'C:\\fakepath\\picked'
  input.dispatch('change')
  await settle()
}

test('AnswerSheet FilePicker serially prepares every selected image and blocks duplicate selection', async (t) => {
  const sheet = await mountAnswerSheet(t, {
    session: { session_id: 's-draft', status: 'draft', answer_source: 'separate', source_question_drafts: [], answer_sheet_pages: [] },
    responses: { 'POST /api/answer-sheet/sessions/s-draft/source/upload': { session_id: 's-draft', status: 'source_uploaded', answer_source: 'separate' } },
  })
  const pipeline = pagePipeline(t)
  const pickers = () => sheet.page.root.allByClass('file-picker')
  const input = picker => picker.byTag('input')[0]
  const locked = picker => picker.getAttribute('aria-disabled') === 'true' && input(picker).disabled === true
  const uploadSource = () => sheet.page.root.byText('el-button', '上传材料')
  // The original paper, its separate answers and the blank answer sheet.
  assert.deepEqual(pickers().map(picker => picker.byTag('strong')[0].text), ['选择原始试卷图片', '选择答案文件图片', '选择空白答题卡图片'])
  const [source, answers, layout] = pickers()

  // While the paper images are prepared, one at a time, every picker is locked
  // and a second selection in the busy picker is ignored.
  const releasePaper = pipeline.hold('a.png')
  await choose(input(source), [pipeline.file('a.png', 3840, 2880), pipeline.file('b.png', 800, 600)], sheet.settle)
  assert.deepEqual(pipeline.log, ['read a.png', 'decode a.png'])
  assert.equal(source.getAttribute('aria-busy'), 'true')
  assert.equal(source.classList.contains('processing'), true)
  assert.equal(source.byTag('strong')[0].text, '正在逐张处理图片')
  assert.ok(pickers().every(locked), 'all pickers are locked')
  await choose(input(source), [pipeline.file('c.png', 800, 600)], sheet.settle)
  releasePaper()
  await sheet.settle()
  assert.equal(source.byTag('strong')[0].text, '已选择 2 个文件')
  assert.equal(source.getAttribute('aria-busy'), null)
  assert.ok(pickers().every(picker => !locked(picker)), 'all pickers are free again')
  assert.deepEqual(pipeline.log, [...stepsFor('a.png'), ...stepsFor('b.png')], 'c.png was never read')
  assert.equal(pipeline.maxActive, 1)
  // OCR-ready: at most 1920 px on the long edge, JPEG quality 0.88.
  assert.deepEqual(pipeline.encodes.map(({ width, height, quality }) => [width, height, quality]), [[1920, 1440, 0.88], [800, 600, 0.88]])

  await choose(input(answers), [pipeline.file('key.png', 1200, 1600)], sheet.settle)
  assert.equal(answers.byTag('strong')[0].text, '已选择 1 个文件')
  assert.equal(uploadSource().disabled, false)

  // Preparing the blank answer sheet locks the other pickers and the upload.
  const releaseLayout = pipeline.hold('sheet.png')
  await choose(input(layout), [pipeline.file('sheet.png', 2480, 3508)], sheet.settle)
  assert.equal(layout.byTag('strong')[0].text, '正在逐张处理图片')
  assert.ok(locked(source) && locked(answers), 'the paper and answer pickers are locked')
  assert.equal(uploadSource().disabled, true)
  releaseLayout()
  await sheet.settle()
  assert.equal(layout.byTag('strong')[0].text, '已选择 1 个文件')
  assert.ok(!locked(source) && !locked(answers))
  assert.equal(uploadSource().disabled, false)
  assert.deepEqual(pipeline.encodes.at(-1), { name: 'sheet.png', quality: 0.88, width: 1357, height: 1920 })

  // Only the prepared images are uploaded.
  await uploadSource().dispatch('click')
  await sheet.settle()
  const fields = formFields(sheet.requests('POST /api/answer-sheet/sessions/s-draft/source/upload')[0].data)
  assert.deepEqual([fields.source_files].flat().map(file => [file.name, file.type]), [
    ['a.ocr-ready-v1.jpg', 'image/jpeg'], ['b.ocr-ready-v1.jpg', 'image/jpeg'],
  ])
  assert.equal(fields.answer_files.name, 'key.ocr-ready-v1.jpg')
  assert.deepEqual(sheet.client.messages.filter(message => message.type !== 'success'), [])
  sheet.page.unmount()

  // The student answer sheets (the fourth picker, which also takes folders
  // and archives) share the lock with the upload button.
  const students = await mountAnswerSheet(t, {
    session: { session_id: 's-frozen', status: 'frozen', answer_source: 'separate', answer_spec_snapshot: [], answer_sheet_pages: [], student_upload: {} },
  })
  assert.equal(students.page.root.allByClass('file-picker').length, 4)
  const studentPicker = students.page.root.byClass('file-picker-import')
  const pickFiles = () => studentPicker.byTag('input')[0]
  const uploadStudents = () => students.page.root.byText('el-button', '上传并处理')
  await choose(pickFiles(), [pipeline.file('甲_正面.png', 1200, 1600), pipeline.file('甲_反面.png', 1200, 1600)], students.settle)
  assert.equal(studentPicker.byTag('strong')[0].text, '已选择 2 个文件')
  assert.equal(uploadStudents().disabled, false)
  const releaseStudent = pipeline.hold('乙_正面.png')
  await choose(pickFiles(), [pipeline.file('乙_正面.png', 1200, 1600), pipeline.file('乙_反面.png', 1200, 1600)], students.settle)
  assert.equal(studentPicker.byTag('strong')[0].text, '正在导入并处理图片')
  assert.equal(studentPicker.getAttribute('aria-busy'), 'true')
  assert.ok(studentPicker.byTag('input').every(item => item.disabled === true))
  assert.equal(uploadStudents().disabled, true)
  releaseStudent()
  await students.settle()
  assert.equal(studentPicker.byTag('strong')[0].text, '已选择 2 个文件')
  assert.equal(uploadStudents().disabled, false)
  assert.ok(pipeline.encodes.slice(-2).every(item => item.quality === 0.88))
})

test('Upload prepares both template and student images before either recognition path', async (t) => {
  const upload = await mountUpload(t, {
    images: false,
    timers: true,
    responses: {
      'POST /api/capture/sessions': { session_id: 'cap-1', token: 'capture-token', images: [{ seq: 1, width: 3840, height: 2880 }] },
      'GET /api/capture/sessions/cap-1': { session_id: 'cap-1', images: [{ seq: 1, width: 3840, height: 2880 }] },
      'GET /api/capture/sessions/cap-1/images/1': new Blob([pngBytes(3840, 2880)], { type: 'image/png' }),
    },
    streams: { 'POST /api/batch-grade/template/recognize': [['error', { message: '测试到此为止' }]] },
  })
  const pipeline = pagePipeline(t, { encodeFails: name => name.startsWith('broken') })
  const settle = upload.settle
  const card = cls => upload.page.root.byClass(cls)
  const templateInput = () => card('template-card').find(el => el.tagName === 'input' && el.type === 'file')
  const templateArea = () => card('template-upload')
  const studentInputs = () => card('grade-card').findAll(el => el.tagName === 'input' && el.type === 'file')
  const studentArea = () => card('upload-area')
  const IMPORT_BUTTONS = ['选择文件', '选择文件夹', '手机拍摄', '学生上传']
  const errors = () => upload.client.messages.filter(message => message.type === 'error').map(message => message.message)

  // Template images: busy and locked while prepared, then OCR-ready.
  const releaseTemplate = pipeline.hold('page1.png')
  await choose(templateInput(), [pipeline.file('page1.png', 3840, 2880)], settle)
  assert.equal(templateArea().getAttribute('aria-busy'), 'true')
  assert.equal(templateArea().classList.contains('is-disabled'), true)
  assert.equal(templateArea().byClass('t-label').text, '正在逐张处理模板图片')
  assert.equal(templateInput().disabled, true)
  assert.equal(upload.button('识别模板').disabled, true)
  releaseTemplate()
  await settle()
  assert.equal(templateArea().getAttribute('aria-busy'), null)
  assert.equal(templateInput().disabled, false)
  assert.match(card('template-card').byClass('file-summary').text, /已选 1 页/)
  assert.deepEqual(pipeline.encodes.at(-1), { name: 'page1.png', quality: 0.88, width: 1920, height: 1440 })
  await upload.click('识别模板')
  const [recognizeTemplate] = upload.fetches('POST /api/batch-grade/template/recognize')
  const sentTemplate = formFields(recognizeTemplate.init.body).files
  assert.deepEqual([sentTemplate.name, sentTemplate.type], ['page1.ocr-ready-v1.jpg', 'image/jpeg'])

  // A template image that cannot be prepared clears the selection.
  await choose(templateInput(), [pipeline.file('page2.png', 1200, 1600), pipeline.file('broken-page.png', 1200, 1600)], settle)
  assert.equal(Boolean(card('template-card').byClass('file-summary')), false, 'no template pages are selected')
  assert.match(errors().at(-1), /^\[IMPORT_IMAGE_INVALID\] broken-page\.png：1 张模板图片损坏或尺寸超限，本次选择已清空/)

  // Student papers: the import controls are locked while they are prepared.
  const releaseStudent = pipeline.hold('s1.png')
  await upload.choosePapers([pipeline.file('s1.png', 3840, 2880), pipeline.file('s2.png', 1200, 1600)])
  assert.equal(studentArea().getAttribute('aria-busy'), 'true')
  assert.equal(studentArea().classList.contains('is-disabled'), true)
  for (const text of IMPORT_BUTTONS) assert.equal(upload.button(text).disabled, true, `${text} is locked`)
  assert.ok(studentInputs().every(input => input.disabled === true))
  releaseStudent()
  await settle()
  assert.equal(studentArea().getAttribute('aria-busy'), null)
  for (const text of IMPORT_BUTTONS) assert.equal(upload.button(text).disabled, false, `${text} is free`)
  assert.match(card('grade-card').byClass('file-summary').text, /已选 2 个文件/)
  assert.deepEqual(upload.page.setup.fileList.map(file => [file.name, file.type]), [
    ['s1.ocr-ready-v1.jpg', 'image/jpeg'], ['s2.ocr-ready-v1.jpg', 'image/jpeg'],
  ])
  assert.deepEqual(pipeline.encodes.find(item => item.name === 's1.png'), { name: 's1.png', quality: 0.88, width: 1920, height: 1440 })

  // One paper that cannot be prepared clears the whole selection: neither the
  // prepared pages nor the raw originals are kept for recognition.
  await upload.choosePapers([pipeline.file('s3.png', 1200, 1600), pipeline.file('broken-paper.png', 1200, 1600)])
  assert.equal(Boolean(card('grade-card').byClass('file-summary')), false, 'no student papers are selected')
  assert.deepEqual(upload.page.setup.fileList, [])
  assert.match(errors().at(-1), /^\[IMPORT_IMAGE_INVALID\] broken-paper\.png：1 张学生试卷图片损坏或尺寸超限，本次选择已清空/)

  // Phone photos skip extraction and go straight to preparation, which alone
  // keeps the page busy.
  await upload.click('手机拍摄')
  const releasePhoto = pipeline.hold('capture-0001.png')
  const phoneImport = upload.page.root.find(el => el.attributes['data-test'] === 'phone-capture-import')
  assert.equal(phoneImport.text, '导入 1 张')
  phoneImport.dispatch('click')
  await settle()
  assert.ok(pipeline.log.includes('decode capture-0001.png'), `the photo is being prepared: ${pipeline.log.slice(-3)}`)
  assert.equal(studentArea().getAttribute('aria-busy'), 'true')
  assert.equal(studentArea().byClass('u-label').text, '正在逐张处理试卷图片')
  assert.equal(studentArea().byClass('u-sub').text, '完成后即可开始识别')
  for (const text of IMPORT_BUTTONS) assert.equal(upload.button(text).disabled, true, `${text} is locked`)
  assert.ok(studentInputs().every(input => input.disabled === true))
  releasePhoto()
  await settle()
  assert.equal(studentArea().getAttribute('aria-busy'), null)
  assert.match(card('grade-card').byClass('file-summary').text, /已选 1 个文件/)
  assert.deepEqual(pipeline.encodes.at(-1), { name: 'capture-0001.png', quality: 0.88, width: 1920, height: 1440 })
  assert.equal(upload.client.messages.at(-1).message, '已导入 1 张手机照片')
})

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
