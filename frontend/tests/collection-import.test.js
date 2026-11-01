import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import JSZip from 'jszip'
import { NodeArchiveWorker } from './helpers/archiveDecoder.js'
import { extractImagesFromFileList, extractImagesFromDrop } from '../src/utils/fileExtraction.js'
import { appendStudentUploadFiles, getUploadSourcePath, withUploadSourcePath } from '../src/utils/uploadSource.js'
import { compressImageFiles, createRotatedOcrReadyFile } from '../src/utils/uploadValidation.js'

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

const uploadSource = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
function handlers() {
  const state = { prepared: [], warnings: [], cleared: 0 }
  const deps = {
    recognitionAutoArmed: { value: true },
    imagePreprocessing: { value: false }, imageExtracting: { value: false },
    fileList: { value: [] }, extractionProgress: { value: {} }, isDragover: { value: false },
    ElMessage: { warning: text => state.warnings.push(text), success() {}, error: text => assert.fail(text) },
    requireRecognitionService: () => true,
    clearReviewDisplay: () => { state.cleared += 1 },
    validateUploadList: files => ({ files: files.map(item => item.raw), errors: [] }),
    compressImageFiles: async files => { state.prepared.push(...files); return files },
    extractImagesFromFileList: async () => [file('synthetic.jpg')],
    extractImagesFromDrop: async () => [file('synthetic.jpg')],
  }
  const start = uploadSource.indexOf('async function processFiles(')
  const end = uploadSource.indexOf('function resetFiles()', start)
  assert.ok(start >= 0 && end > start)
  const code = uploadSource.slice(start, end)
  return { state, deps, ...new Function(...Object.keys(deps), `${code}\nreturn {onFileInputChange,onFolderInputChange,onDrop};`)(...Object.values(deps)) }
}

for (const [handler, name] of [['onFileInputChange', 'synthetic.zip'], ['onFolderInputChange', 'synthetic.jpg'], ['onDrop', 'synthetic.jpg'], ['onFileInputChange', 'synthetic.jpg']]) {
  test(`${handler} ${name} actually adds extracted images, not just a success message`, async () => {
    const run = handlers()
    await run[handler]({ target: { files: [file(name)], value: '' } })
    await Promise.resolve()
    assert.equal(run.deps.fileList.value.length, 1)
    assert.equal(run.state.prepared.length, 1)
    assert.equal(run.state.cleared, 1)
    assert.deepEqual(run.state.warnings, [])
  })
}

test('a second import cannot interrupt active extraction', async () => {
  const run = handlers()
  run.deps.imageExtracting.value = true
  await run.onFolderInputChange({ target: { files: [file('a.jpg')], value: '' } })
  assert.equal(run.state.prepared.length, 0)
  assert.equal(run.deps.imageExtracting.value, true)
})
