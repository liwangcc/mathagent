// Browser-only unpacking: the server continues to receive validated OCR-ready
// images, never archive members or filesystem paths. An import is all-or-nothing.
import { getUploadSourcePath, withUploadSourcePath } from './uploadSource.js'
import { IMPORT_LIMITS, detectImportFormat, imageImportFile, importError, isOsMetadataPath, safeImportPath } from './fileImportPolicy.js'
export { IMPORT_ACCEPT, IMPORT_FORMATS } from './fileImportPolicy.js'

let importTail = Promise.resolve()
export const MULTI_ROOT_IMPORT = '多项导入'

export function extractArchiveInWorker(file, limits = IMPORT_LIMITS) {
  return new Promise((resolve, reject) => {
    let worker
    let timer
    const finish = (error, files) => {
      clearTimeout(timer)
      worker?.terminate()
      if (error) reject(error)
      else resolve(files)
    }
    try {
      worker = new Worker(new URL('./archiveExtraction.worker.js', import.meta.url), { type: 'module' })
      timer = setTimeout(() => finish(importError('IMPORT_TIMEOUT', file.name, '解包超过 30 秒已终止', '请拆分压缩包或先在本地解开。')), limits.timeoutMs)
      worker.onerror = () => finish(importError('IMPORT_DECODER_UNAVAILABLE', file.name, '解包 Worker 不可用', '请刷新页面；管理员需检查前端静态资源部署。'))
      worker.onmessage = ({ data }) => finish(data.error ? Object.assign(new Error(data.error.message), data.error) : null, data.files)
      worker.postMessage({ file, limits })
    } catch {
      finish(importError('IMPORT_DECODER_UNAVAILABLE', file.name, '浏览器不支持或无法加载解包组件', '请使用新版 Chrome/Edge 或先在本地解压。'))
    }
  })
}

async function collectDropFiles(event) {
  const files = []
  let entries = 0
  async function visit(entry, parent = '') {
    if (++entries > IMPORT_LIMITS.maxEntries) throw importError('IMPORT_ENTRY_LIMIT', parent, '文件夹项目过多')
    const path = safeImportPath(parent ? `${parent}/${entry.name}` : entry.name)
    if (entry.isFile) {
      try {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject))
        files.push(withUploadSourcePath(file, path))
      } catch (error) {
        throw importError('IMPORT_READ_FAILED', path, `文件读取失败（${error.message || error}）`)
      }
    } else if (entry.isDirectory) {
      const reader = entry.createReader()
      while (true) {
        let children
        try {
          children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject))
        } catch (error) {
          throw importError('IMPORT_READ_FAILED', path, `目录读取失败（${error.message || error}）`)
        }
        if (!children.length) break
        for (const child of children) await visit(child, path)
      }
    }
  }
  const items = Array.from(event.dataTransfer?.items || [])
  // Capture entries synchronously: the drag data store closes after this event.
  const roots = items.filter(item => item.kind === 'file').map(item => ({
    entry: item.webkitGetAsEntry?.() || item.getAsEntry?.(), file: item.getAsFile?.(),
  }))
  for (const root of roots) {
    if (root.entry) await visit(root.entry)
    else if (root.file) files.push(root.file)
    else throw importError('IMPORT_READ_FAILED', '拖入文件', '浏览器未提供可读取的文件', '请重新选择本地文件。')
  }
  return roots.length ? files : Array.from(event.dataTransfer?.files || [])
}

export async function extractImagesFromDrop(event, options = {}) {
  const files = await collectDropFiles(event)
  return extractImagesFromFileList(files, options)
}

export async function extractImagesFromFileList(files, options = {}) {
  // Serialize template/student imports too; each archive worker has a bounded
  // WASM heap and is destroyed on success, failure and timeout.
  const previous = importTail
  let release
  importTail = new Promise(resolve => { release = resolve })
  await previous
  try {
    const limits = { ...IMPORT_LIMITS, maxFiles: Math.min(options.maxFiles || IMPORT_LIMITS.maxFiles, IMPORT_LIMITS.maxFiles) }
    const input = Array.from(files)
    if (input.length > limits.maxEntries) throw importError('IMPORT_ENTRY_LIMIT', '本次选择', '文件项目过多')
    const result = []
    const archivePaths = new Set()
    let total = 0
    const add = (file, path) => {
      total += file.size
      if (result.length >= limits.maxFiles || total > limits.maxTotalBytes) {
        throw importError('IMPORT_BATCH_LIMIT', path, `本次最多 ${limits.maxFiles} 张图片、展开后合计 200MB`, '请分批导入。')
      }
      result.push(withUploadSourcePath(file, safeImportPath(path)))
    }
    for (const [index, file] of input.entries()) {
      const source = safeImportPath(getUploadSourcePath(file))
      if (isOsMetadataPath(source)) continue
      options.onProgress?.(index, input.length, `正在检查 ${source}...`)
      if (file.size > limits.maxArchiveBytes) throw importError('IMPORT_ARCHIVE_LIMIT', source, '输入文件超过 64MB')
      let format
      try {
        format = detectImportFormat(new Uint8Array(await file.slice(0, 512).arrayBuffer()))
      } catch (error) {
        throw importError('IMPORT_READ_FAILED', source, `文件读取失败（${error.message || error}）`, '请检查本地文件可用性后重新选择。')
      }
      if (!format || format === 'pdf' || format.startsWith('image/')) {
        add(await imageImportFile(file, source), source)
        continue
      }
      let prefix = source
      let copy = 1
      while (archivePaths.has(prefix)) prefix = `${source} (${++copy})`
      archivePaths.add(prefix)
      options.onProgress?.(index, input.length, `正在解压 ${source}（${format}）...`)
      const extracted = await extractArchiveInWorker(file, { ...limits, maxFiles: limits.maxFiles - result.length, maxTotalBytes: limits.maxTotalBytes - total })
      for (const entry of extracted) add(entry.file, `${prefix}/${entry.path}`)
    }
    options.onProgress?.(result.length, result.length, `已提取 ${result.length} 张图片`)
    // The server never reads the first path component as an identity: one
    // picked folder or archive is the teacher's container. Several top-level
    // folders/archives in one import (student folders dragged in together,
    // one ZIP per student) are themselves student containers, so they go
    // under a shared root and their names count like any subfolder.
    const roots = new Set(result.map(file => getUploadSourcePath(file).split('/')).filter(parts => parts.length > 1).map(parts => parts[0]))
    if (roots.size < 2) return result
    return result.map(file => withUploadSourcePath(file, safeImportPath(`${MULTI_ROOT_IMPORT}/${getUploadSourcePath(file)}`)))
  } finally {
    release()
  }
}