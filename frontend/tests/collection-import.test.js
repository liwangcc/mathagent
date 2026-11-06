import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import JSZip from 'jszip'
import { NodeArchiveWorker } from './helpers/archiveDecoder.js'
import { extractImagesFromFileList, extractImagesFromDrop } from '../src/utils/fileExtraction.js'
import { appendStudentUploadFiles, getUploadSourcePath, withUploadSourcePath } from '../src/utils/uploadSource.js'
import { compressImageFiles, createRotatedOcrReadyFile } from '../src/utils/uploadValidation.js'
import { mountUpload, TEACHER } from './helpers/uploadPage.js'

globalThis.Worker = NodeArchiveWorker
const jpeg = readFileSync(new URL('./fixtures/import/page.jpg', import.meta.url))
const file = name => new File([jpeg], name, { type: 'image/jpeg' })

test('ZIP preserves all three layouts and does not overwrite repeated image basenames', async () => {
  const zip = new JSZip()
  const paths = ['张三/abc.jpg', '李四/abc.jpg', '随机目录1/张三_1.jpg', '随机目录2/李四_2.jpg', '张三_3.jpg']
  paths.forEach((path) => zip.file(path, jpeg))
  const result = await extractImagesFromFileList([new File([await zip.generateAsync({ type: 'uint8array' })], '收集.zip')])
  assert.deepEqual(result.map(getUploadSourcePath), paths.map(path => `收集.zip/${path}`))
  assert.deepEqual(result.map(item => item.name), ['abc.jpg', 'abc.jpg', '张三_1.jpg', '李四_2.jpg', '张三_3.jpg'])
  for (const image of result) assert.deepEqual(new Uint8Array(await image.arrayBuffer()), new Uint8Array(jpeg))
})

test('folder picker preserves webkitRelativePath through the multipart metadata contract', async () => {
  const files = [file('abc.jpg'), file('abc.jpg')]
  files.forEach((item, index) => Object.defineProperty(item, 'webkitRelativePath', { value: `主文件夹/目录${index}/张三_${index + 1}.jpg` }))
  const result = await extractImagesFromFileList(files)
  const form = new FormData()
  appendStudentUploadFiles(form, result)
  assert.equal(form.getAll('files').length, 2)
  assert.deepEqual(JSON.parse(form.get('source_paths')), files.map(item => item.webkitRelativePath))
})

test('same-named ZIPs stay separate for directory-level teacher confirmation', async () => {
  const zip = new JSZip()
  zip.file('随机目录/张三_1.jpg', jpeg)
  const bytes = await zip.generateAsync({ type: 'uint8array' })
  const files = await extractImagesFromFileList([new File([bytes], '收集.zip'), new File([bytes], '收集.zip')])
  assert.deepEqual(files.map(getUploadSourcePath), ['多项导入/收集.zip/随机目录/张三_1.jpg', '多项导入/收集.zip (2)/随机目录/张三_1.jpg'])
})

test('several top-level student folders or archives share a root so their names count; one stays the container', async () => {
  const dropped = await extractImagesFromDrop({ dataTransfer: {
    items: [
      { kind: 'file', webkitGetAsEntry: () => directory('张三', [entry('a.jpg')]) },
      { kind: 'file', webkitGetAsEntry: () => directory('李四', [entry('a.jpg')]) },
    ],
    files: [],
  } })
  assert.deepEqual(dropped.map(getUploadSourcePath), ['多项导入/张三/a.jpg', '多项导入/李四/a.jpg'])

  const zip = async () => {
    const archive = new JSZip()
    archive.file('1.jpg', jpeg)
    return archive.generateAsync({ type: 'uint8array' })
  }
  const archives = await extractImagesFromFileList([new File([await zip()], '张三.zip'), new File([await zip()], '李四.zip')])
  assert.deepEqual(archives.map(getUploadSourcePath), ['多项导入/张三.zip/1.jpg', '多项导入/李四.zip/1.jpg'])

  const single = await extractImagesFromDrop({ dataTransfer: {
    items: [{ kind: 'file', webkitGetAsEntry: () => directory('张三', [entry('a.jpg')]) }], files: [],
  } })
  assert.deepEqual(single.map(getUploadSourcePath), ['张三/a.jpg'])
  const loose = await extractImagesFromFileList([file('张三_1.jpg'), new File([await zip()], '李四.zip')])
  assert.deepEqual(loose.map(getUploadSourcePath), ['张三_1.jpg', '李四.zip/1.jpg'])
})

function directory(name, children) {
  return {
    name, isDirectory: true,
    createReader() {
      let index = 0
      return { readEntries(resolve) { resolve(index < children.length ? [children[index++]] : []) } }
    },
  }
}
function entry(name) {
  return { name, isFile: true, file(resolve) { resolve(file(name)) } }
}

test('dragged directory preserves nested paths across paginated readEntries calls', async () => {
  const root = directory('主文件夹', [directory('张三', [entry('abc.jpg'), entry('def.jpg')]), directory('随机目录', [entry('李四_1.jpg')])])
  const files = await extractImagesFromDrop({ dataTransfer: {
    items: [{ kind: 'file', webkitGetAsEntry: () => root }], files: [],
  } })
  assert.deepEqual(files.map(getUploadSourcePath), ['主文件夹/张三/abc.jpg', '主文件夹/张三/def.jpg', '主文件夹/随机目录/李四_1.jpg'])
})

test('image preprocessing and rotation retain the original source path', async () => {
  const oldBitmap = globalThis.createImageBitmap
  const oldDocument = globalThis.document
  const gif = new Uint8Array([71, 73, 70, 56, 57, 97, 1, 0, 1, 0])
  globalThis.createImageBitmap = async () => ({ width: 1, height: 1, close() {} })
  globalThis.document = { createElement: () => ({
    getContext: () => ({
      fillRect() {}, drawImage() {}, translate() {}, rotate() {}, setTransform() {},
      getImageData: () => ({ data: new Uint8ClampedArray([0, 0, 0, 255]) }),
    }),
    toBlob: callback => callback(new Blob([gif], { type: 'image/jpeg' })),
  }) }
  try {
    const original = withUploadSourcePath(new File([gif], 'abc.gif', { type: 'image/gif' }), '收集.zip/张三/abc.gif')
    const [prepared] = await compressImageFiles([original])
    assert.equal(prepared.name, 'abc.ocr-ready-v1.jpg')
    assert.equal(getUploadSourcePath(prepared), '收集.zip/张三/abc.gif')
    const rotated = await createRotatedOcrReadyFile(prepared, 1)
    assert.equal(getUploadSourcePath(rotated), '收集.zip/张三/abc.gif')
    const form = new FormData()
    appendStudentUploadFiles(form, [rotated])
    assert.deepEqual(JSON.parse(form.get('source_paths')), ['收集.zip/张三/abc.gif'])
  } finally {
    if (oldBitmap === undefined) delete globalThis.createImageBitmap
    else globalThis.createImageBitmap = oldBitmap
    if (oldDocument === undefined) delete globalThis.document
    else globalThis.document = oldDocument
  }
})

// What the real Upload page does with each way of importing student papers
// (选择文件 with an archive or images, 选择文件夹, dragging onto the upload
// area): the extracted images are prepared and listed for recognition, and a
// review still shown from the last batch is cleared.
const REVIEW_POINTER = `mathagent_review_batch_id:${TEACHER.id}`

async function pageWithLastReview(t) {
  const upload = await mountUpload(t, {
    localStorage: { [REVIEW_POINTER]: 'b-last' },
    responses: {
      'GET /api/batch-grade/review/b-last': {
        batch_id: 'b-last',
        status: 'review_ready',
        results: [{ paper_id: 'old-paper', student_name: '上一批', disposition: 'active', correct: 1, total: 1, details: [{ question_no: '1', result: 'correct' }] }],
        summary: { total: 1 },
      },
      'GET /api/batch-grade/report/b-last': { status: 'ready', text: '', stats: {} },
    },
  })
  assert.ok(upload.page.root.byClass('review-board'), 'the last review is shown')
  return upload
}

const gradeCard = upload => upload.page.root.byClass('grade-card')
const folderInput = upload => gradeCard(upload).find(el => el.tagName === 'input' && el.type === 'file' && el.hasAttribute('webkitdirectory'))
const uploadArea = upload => gradeCard(upload).byClass('upload-area')
const studentSummary = upload => gradeCard(upload).byClass('file-summary')?.text ?? null

// 选择文件夹: the folder picker's change event, as the browser fires it.
async function pickFolder(upload, files) {
  const input = folderInput(upload)
  input.files = files
  input.value = 'C:\\fakepath\\folder'
  input.dispatch('change').catch(err => upload.page.errors.push({ err, info: 'change handler' }))
  await upload.settle()
  return input
}

// Dragging over the upload area, then dropping `items` (DataTransferItems).
async function dropOnUploadArea(upload, items) {
  const area = uploadArea(upload)
  await area.dispatch('dragover')
  await upload.settle()
  assert.ok(area.classList.contains('is-dragover'))
  area.dispatch('drop', { dataTransfer: { items, files: [] } }).catch(err => upload.page.errors.push({ err, info: 'drop handler' }))
  await upload.settle()
}

async function zipOf(entries) {
  const zip = new JSZip()
  for (const [path, bytes] of entries) zip.file(path, bytes)
  return zip.generateAsync({ type: 'uint8array' })
}

const IMPORTS = {
  'onFileInputChange synthetic.zip': {
    run: async upload => upload.choosePapers([new File([await zipOf([['synthetic.jpg', jpeg]])], 'synthetic.zip')]),
    source: 'synthetic.zip/synthetic.jpg',
  },
  'onFolderInputChange synthetic.jpg': {
    run: async (upload) => {
      const picked = file('synthetic.jpg')
      Object.defineProperty(picked, 'webkitRelativePath', { value: '收集/synthetic.jpg' })
      const input = await pickFolder(upload, [picked])
      assert.equal(input.value, '', 'the picker is cleared for the next selection')
    },
    source: '收集/synthetic.jpg',
  },
  'onDrop synthetic.jpg': {
    run: async upload => dropOnUploadArea(upload, [{ kind: 'file', webkitGetAsEntry: () => entry('synthetic.jpg') }]),
    source: 'synthetic.jpg',
  },
  'onFileInputChange synthetic.jpg': {
    run: async upload => upload.choosePapers([file('synthetic.jpg')]),
    source: 'synthetic.jpg',
  },
}

for (const [handler, name] of [['onFileInputChange', 'synthetic.zip'], ['onFolderInputChange', 'synthetic.jpg'], ['onDrop', 'synthetic.jpg'], ['onFileInputChange', 'synthetic.jpg']]) {
  test(`${handler} ${name} actually adds extracted images, not just a success message`, async (t) => {
    const upload = await pageWithLastReview(t)
    const { run, source } = IMPORTS[`${handler} ${name}`]
    await run(upload)
    assert.deepEqual(upload.page.errors, [])
    assert.equal(studentSummary(upload), '已选 1 个文件 清空')
    // The listed image is the prepared, OCR-ready one, still carrying where it came from.
    const [listed] = upload.page.setup.fileList
    assert.equal(listed.name, 'synthetic.ocr-ready-v1.jpg')
    assert.equal(listed.type, 'image/jpeg')
    assert.equal(getUploadSourcePath(listed), source)
    assert.equal(Boolean(upload.page.root.byClass('review-board')), false, 'the last review is cleared')
    assert.equal(uploadArea(upload).classList.contains('is-dragover'), false)
    assert.equal(uploadArea(upload).getAttribute('aria-busy'), null)
    assert.deepEqual(upload.client.messages, [{ type: 'success', message: '成功导入 1 张图片' }])
  })
}

test('a second import cannot interrupt active extraction', async (t) => {
  const upload = await mountUpload(t)
  // A dragged folder whose contents the browser hands over only later.
  const listing = deferred()
  const slowFolder = {
    name: '主文件夹',
    isDirectory: true,
    createReader() {
      let read = false
      return {
        readEntries(resolve) {
          if (read) resolve([])
          else { read = true; listing.promise.then(resolve) }
        },
      }
    },
  }
  await dropOnUploadArea(upload, [{ kind: 'file', webkitGetAsEntry: () => slowFolder }])
  assert.equal(uploadArea(upload).getAttribute('aria-busy'), 'true', 'the dragged folder is being extracted')
  assert.equal(upload.button('选择文件夹').disabled, true)

  // A second import arriving meanwhile is dropped: nothing is prepared or
  // listed, and the running extraction keeps the import controls locked.
  const picked = file('a.jpg')
  Object.defineProperty(picked, 'webkitRelativePath', { value: '另一个/a.jpg' })
  const input = await pickFolder(upload, [picked])
  assert.equal(input.value, '', 'the picker is cleared for the next selection')
  assert.equal(studentSummary(upload), null)
  assert.deepEqual(upload.page.setup.fileList, [])
  assert.equal(uploadArea(upload).getAttribute('aria-busy'), 'true')
  assert.equal(upload.button('选择文件夹').disabled, true)
  assert.deepEqual(upload.client.messages, [])

  // The dragged folder then finishes and is the only import.
  listing.resolve([entry('abc.jpg')])
  await upload.settle()
  assert.deepEqual(upload.page.errors, [])
  assert.equal(studentSummary(upload), '已选 1 个文件 清空')
  assert.deepEqual(upload.page.setup.fileList.map(getUploadSourcePath), ['主文件夹/abc.jpg'])
  assert.equal(uploadArea(upload).getAttribute('aria-busy'), null)
  assert.deepEqual(upload.client.messages, [{ type: 'success', message: '成功导入 1 张图片' }])
})

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
