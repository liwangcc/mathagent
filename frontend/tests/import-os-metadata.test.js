import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import JSZip from 'jszip'
import { NodeArchiveWorker } from './helpers/archiveDecoder.js'
import { extractImagesFromFileList } from '../src/utils/fileExtraction.js'
import { isOsMetadataPath } from '../src/utils/fileImportPolicy.js'
import { getUploadSourcePath } from '../src/utils/uploadSource.js'

globalThis.Worker = NodeArchiveWorker
const jpeg = readFileSync(new URL('./fixtures/import/page.jpg', import.meta.url))
const inFolder = (name, path, bytes = jpeg) => {
  const item = new File([bytes], name)
  Object.defineProperty(item, 'webkitRelativePath', { value: path })
  return item
}

test('only exact operating-system metadata names are recognized', () => {
  for (const path of ['班级/.DS_Store', '班级/张三/Thumbs.db', 'desktop.ini', '收集.zip/__MACOSX/张三/._a.jpg', '班级/._a.jpg', 'X\\DESKTOP.INI']) {
    assert.equal(isOsMetadataPath(path), true, path)
  }
  for (const path of ['班级/张三/a.jpg', '班级/张三/notes.docx', '班级/DS_Store.jpg', '班级/_a.jpg', 'MACOSX/a.jpg']) {
    assert.equal(isOsMetadataPath(path), false, path)
  }
})

test('folder import skips system metadata but still rejects other non-images', async () => {
  const files = [
    inFolder('a.jpg', '班级/张三/a.jpg'),
    inFolder('.DS_Store', '班级/.DS_Store', new Uint8Array([0, 0, 0, 1, 66, 117, 100, 49])),
    inFolder('Thumbs.db', '班级/张三/Thumbs.db', new Uint8Array([208, 207, 17, 224])),
    inFolder('desktop.ini', '班级/desktop.ini', new Uint8Array([91, 46])),
  ]
  assert.deepEqual((await extractImagesFromFileList(files)).map(getUploadSourcePath), ['班级/张三/a.jpg'])
  await assert.rejects(
    extractImagesFromFileList([inFolder('a.jpg', '班级/张三/a.jpg'), inFolder('notes.docx', '班级/notes.docx', new Uint8Array([80, 75, 3, 4]))]),
    /IMPORT_(UNSUPPORTED_FILE|ARCHIVE_INVALID).*notes\.docx/,
  )
})

test('macOS-compressed ZIP imports without its __MACOSX and AppleDouble entries', async () => {
  const zip = new JSZip()
  zip.file('张三/a.jpg', jpeg)
  zip.file('__MACOSX/张三/._a.jpg', new Uint8Array([0, 5, 22, 7, 0, 2, 0, 0]))
  zip.file('张三/.DS_Store', new Uint8Array([0, 0, 0, 1, 66, 117, 100, 49]))
  const archive = new File([await zip.generateAsync({ type: 'uint8array' })], '收集.zip')
  assert.deepEqual((await extractImagesFromFileList([archive])).map(getUploadSourcePath), ['收集.zip/张三/a.jpg'])

  const onlyMetadata = new JSZip()
  onlyMetadata.file('__MACOSX/._a.jpg', new Uint8Array([0, 5, 22, 7]))
  const empty = new File([await onlyMetadata.generateAsync({ type: 'uint8array' })], '空.zip')
  await assert.rejects(extractImagesFromFileList([empty]), /IMPORT_EMPTY_ARCHIVE/)
})
