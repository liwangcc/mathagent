import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import JSZip from 'jszip'
import { decodeArchive, NodeArchiveWorker } from './helpers/archiveDecoder.js'
import { extractImagesFromFileList, extractImagesFromDrop, extractArchiveInWorker } from '../src/utils/fileExtraction.js'
import { detectImportFormat, IMPORT_LIMITS, IMPORT_ACCEPT } from '../src/utils/fileImportPolicy.js'
import { getUploadSourcePath } from '../src/utils/uploadSource.js'
import { boundArchiveMemory } from '../src/utils/archiveMemory.js'

globalThis.Worker = NodeArchiveWorker
const fixture = name => new File([readFileSync(new URL(`./fixtures/import/${name}`, import.meta.url))], name)
const jpeg = readFileSync(new URL('./fixtures/import/page.jpg', import.meta.url))
async function zipFile(entries, name = 'papers.zip') {
  const zip = new JSZip()
  for (const [path, data] of entries) zip.file(path, data, { createFolders: false })
  return new File([await zip.generateAsync({ type: 'uint8array' })], name)
}

for (const ext of ['jpg', 'png', 'gif', 'bmp', 'webp']) {
  test(`ordinary ${ext}: MIME comes from content, not browser metadata`, async () => {
    const [image] = await extractImagesFromFileList([fixture(`page.${ext}`)])
    assert.equal(image.type, ext === 'jpg' ? 'image/jpeg' : `image/${ext}`)
    assert.equal(image.size, fixture(`page.${ext}`).size)
  })
}
for (const name of ['pages.zip', 'pages-rar4.rar', 'pages-rar5.rar']) {
  test(`${name}: real decoder preserves every image byte and nested path`, async () => {
    const files = await extractImagesFromFileList([fixture(name)])
    assert.equal(files.length, 2)
    for (const file of files) {
      const ext = file.type === 'image/jpeg' ? 'jpg' : 'png'
      assert.deepEqual(new Uint8Array(await file.arrayBuffer()), new Uint8Array(await fixture(`page.${ext}`).arrayBuffer()))
      assert.ok(getUploadSourcePath(file).startsWith(`${name}/`))
    }
  })
}

test('RAR renamed ZIP, ZIP renamed RAR and image without suffix use actual bytes', async () => {
  for (const [source, name, count] of [['pages-rar5.rar', 'misnamed.zip', 2], ['pages.zip', 'misnamed.rar', 2], ['page.jpg', 'no-extension', 1]]) {
    assert.equal((await extractImagesFromFileList([new File([fixture(source)], name)])).length, count)
  }
})

for (const name of ['encrypted.zip', 'encrypted.rar', 'encrypted-header.rar']) {
  test(`${name}: encryption has actionable code`, async () => {
    await assert.rejects(extractImagesFromFileList([fixture(name)]), error => error.code === 'IMPORT_ENCRYPTED' && error.message.includes(name))
  })
}

test('mixed valid image and unsupported file reject whole selection, not silently lose one', async () => {
  await assert.rejects(extractImagesFromFileList([fixture('page.jpg'), new File(['not a picture'], 'notes.docx')]), /IMPORT_UNSUPPORTED_FILE.*notes.docx/)
})
test('PDF cannot appear accepted here when grade backend rejects it', async () => {
  assert.ok(!IMPORT_ACCEPT.includes('.pdf'))
  await assert.rejects(extractImagesFromFileList([new File(['%PDF-1.7'], 'paper.pdf')]), /PDF.*逐页导出图片/)
})
test('nested archive is explicit, never silently skipped', async () => {
  await assert.rejects(extractImagesFromFileList([await zipFile([['nested.zip', await fixture('pages.zip').arrayBuffer()]])]), /IMPORT_UNSUPPORTED_FILE.*nested.zip/)
})
for (const path of ['../escape.jpg', '/absolute.jpg', 'C:/escape.jpg', 'a\\..\\escape.jpg', '__proto__/page.jpg']) {
  test(`unsafe path ${path} rejects before output`, async () => {
    await assert.rejects(extractImagesFromFileList([await zipFile([[path, jpeg]])]), /IMPORT_UNSAFE_PATH/)
  })
}
for (const name of ['pages.zip', 'pages-rar4.rar', 'pages-rar5.rar']) {
  test(`truncated ${name} cannot report success`, async () => {
    const original = fixture(name)
    const corrupt = new File([original.slice(0, Math.floor(original.size / 2))], name)
    await assert.rejects(extractImagesFromFileList([corrupt]), /IMPORT_ARCHIVE_INVALID/)
  })
}
test('ZIP CRC corruption fails with member path', async () => {
  const file = await zipFile([['nested/page.jpg', jpeg]])
  const data = new Uint8Array(await file.arrayBuffer())
  data[30 + 'nested/page.jpg'.length + 20] ^= 1
  await assert.rejects(extractImagesFromFileList([new File([data], 'crc.zip')]), /IMPORT_ARCHIVE_INVALID.*crc.zip/)
})
test('empty archive is explicit', async () => {
  await assert.rejects(extractImagesFromFileList([await zipFile([])]), /IMPORT_EMPTY_ARCHIVE/)
})
test('limits apply to count, per-file and total BEFORE decompression', async () => {
  const file = fixture('pages.zip')
  for (const [limit, value, code] of [['maxFiles', 1, 'IMPORT_RESOURCE_LIMIT'], ['maxFileBytes', 1, 'IMPORT_FILE_LIMIT'], ['maxTotalBytes', 1, 'IMPORT_RESOURCE_LIMIT'], ['maxEntries', 1, 'IMPORT_ENTRY_LIMIT'], ['maxRatio', 0.001, 'IMPORT_RESOURCE_LIMIT']]) {
    await assert.rejects(decodeArchive(file, { ...IMPORT_LIMITS, [limit]: value }), error => error.code === code)
  }
})
test('combined archives share batch limits; template max=20 is enforced', async () => {
  await assert.rejects(extractImagesFromFileList([fixture('pages.zip'), fixture('pages.zip')], { maxFiles: 3 }), /IMPORT_RESOURCE_LIMIT/)
  await assert.rejects(extractImagesFromFileList(Array.from({ length: 21 }, () => fixture('page.jpg')), { maxFiles: 20 }), /IMPORT_BATCH_LIMIT/)
})
test('oversized input is rejected before any arrayBuffer read', async () => {
  await assert.rejects(extractImagesFromFileList([{ name: 'large.rar', size: IMPORT_LIMITS.maxArchiveBytes + 1 }]), /IMPORT_ARCHIVE_LIMIT/)
})
test('failed folder read is named, not replaced by empty success', async () => {
  await assert.rejects(extractImagesFromDrop({ dataTransfer: { items: [{ kind: 'file', webkitGetAsEntry: () => ({ name: 'locked.jpg', isFile: true, file(_resolve, reject) { reject(new Error('denied')) } }) }], files: [] } }), /IMPORT_READ_FAILED.*locked.jpg/)
})
test('missing decoder and timeout terminate their workers', async () => {
  let terminated = 0
  try {
    globalThis.Worker = class { constructor() { throw new Error('missing asset') } }
    await assert.rejects(extractArchiveInWorker(fixture('pages.zip')), /IMPORT_DECODER_UNAVAILABLE/)
    globalThis.Worker = class { postMessage() {} terminate() { terminated += 1 } }
    await assert.rejects(extractArchiveInWorker(fixture('pages.zip'), { ...IMPORT_LIMITS, timeoutMs: 5 }), /IMPORT_TIMEOUT/)
    assert.equal(terminated, 1)
  } finally { globalThis.Worker = NodeArchiveWorker }
})
test('unknown and executable signatures are not image extensions', async () => {
  assert.equal(detectImportFormat(new TextEncoder().encode('MZunsafe.jpg')), '')
  await assert.rejects(extractImagesFromFileList([new File(['MZunsafe'], 'image.jpg', { type: 'image/jpeg' })]), /IMPORT_UNSUPPORTED_FILE/)
})

test('RAR5 preserves Unicode directories and repeated basenames', async () => {
  const files = await extractImagesFromFileList([fixture('unicode.rar')])
  assert.deepEqual(files.map(getUploadSourcePath).sort(), ['unicode.rar/班级/张三/试卷.jpg', 'unicode.rar/班级/李四/试卷.jpg'].sort())
})
for (const [name,code] of [['duplicate.zip','IMPORT_DUPLICATE_PATH'],['unsupported.zip','IMPORT_UNSUPPORTED_FILE'],['symlink.zip','IMPORT_UNSAFE_ENTRY'],['missing-footer.zip','IMPORT_ARCHIVE_INVALID']]) {
  test(`${name} reports ${code}`, async () => {
    await assert.rejects(extractImagesFromFileList([fixture(name)]), error=>error.code===code)
  })
}
for (const name of ['pages.7z','pages.tar','pages.tar.gz','pages.tar.bz2','pages.tar.xz']) {
  test(`${name} is explicitly unsupported, with local-extraction advice`, async () => {
    await assert.rejects(extractImagesFromFileList([fixture(name)]), /IMPORT_UNSUPPORTED_ARCHIVE.*本地解开/)
  })
}

test('unreadable dropped file cannot silently disappear', async () => {
  const event = { dataTransfer: { items: [{ kind: 'file', getAsFile: () => null }], files: [] } }
  await assert.rejects(extractImagesFromDrop(event), /IMPORT_READ_FAILED/)
})

test('unreadable file-list input includes stable code and source', async () => {
  const file = { name: 'offline.jpg', size: 10, slice() { throw new Error('access denied') } }
  await assert.rejects(extractImagesFromFileList([file]), /IMPORT_READ_FAILED.*offline.jpg/)
})

test('RAR4 payload CRC corruption is rejected by the real decoder', async () => {
  const data = new Uint8Array(await fixture('pages-rar4.rar').arrayBuffer())
  const position = Buffer.from(data).indexOf(jpeg)
  assert.ok(position > 0)
  data[position + 20] ^= 1
  await assert.rejects(extractImagesFromFileList([new File([data], 'crc.rar')]), /IMPORT_ARCHIVE_INVALID/)
})

test('RAR WASM maximum linear memory is encoded as 256MiB', async () => {
  const data = boundArchiveMemory(readFileSync(new URL('../node_modules/node-unrar-js/esm/js/unrar.wasm', import.meta.url)))
  assert.ok(WebAssembly.validate(data))
  let pos = 8
  function integer() {
    let value = 0, shift = 0, part
    do { part = data[pos++]; value += (part & 127) * 2 ** shift; shift += 7 } while (part & 128)
    return value
  }
  while (pos < data.length) {
    const id = data[pos++]
    const size = integer()
    if (id !== 5) { pos += size; continue }
    assert.equal(integer(), 1)
    assert.equal(integer(), 1)
    assert.ok(integer() <= 4096)
    assert.equal(integer(), 4096)
    return
  }
  assert.fail('decoder memory section missing')
})
