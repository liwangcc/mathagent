import JSZip from 'jszip'
import crc32 from 'jszip/lib/crc32.js'
import { IMPORT_LIMITS, detectImportFormat, importError, isOsMetadataPath, safeImportPath, imageImportFile } from './fileImportPolicy.js'
import { validateArchiveEnvelope } from './archiveEnvelope.js'

// Decoders are local and run only inside a disposable Worker in the browser.
export async function extractArchive(file, createRarExtractor, limits = IMPORT_LIMITS) {
  let current = file.name
  const invalid = reason => importError('IMPORT_ARCHIVE_INVALID', current, reason, '请检查完整性；分卷请合并、加密包请解密后再上传。')
  try {
    const bytes = new Uint8Array(await file.arrayBuffer())
    const format = detectImportFormat(bytes)
    const seen = new Set()
    let total = 0
    let count = 0
    let entries = 0
    const inspect = (name, size, directory, encrypted) => {
      current = `${file.name}/${name}`
      if (++entries > limits.maxEntries) throw importError('IMPORT_ENTRY_LIMIT', current, '压缩包目录项目过多')
      const path = safeImportPath(directory ? name.replace(/\/$/, '') : name)
      if (seen.has(path)) throw importError('IMPORT_DUPLICATE_PATH', current, '压缩包内存在重复路径', '请重命名后重新打包。')
      seen.add(path)
      if (encrypted) throw importError('IMPORT_ENCRYPTED', current, '压缩包已加密', '请在本地解密后重新上传。')
      if (!directory) {
        if (!Number.isSafeInteger(size) || size < 0 || size > limits.maxFileBytes) throw importError('IMPORT_FILE_LIMIT', current, '展开后单文件超过 20MB 或大小无效')
        total += size
        if (++count > limits.maxFiles || total > limits.maxTotalBytes || total > Math.max(1, file.size) * limits.maxRatio) {
          throw importError('IMPORT_RESOURCE_LIMIT', current, '展开后数量、总大小或压缩比超过安全上限', '请拆分压缩包。')
        }
      }
      return path
    }
    const result = []
    const add = async (data, path, expectedSize) => {
      path = safeImportPath(path)
      current = `${file.name}/${path}`
      if (!data || data.length !== expectedSize) throw invalid('解包后长度与目录不符')
      if (isOsMetadataPath(path)) return
      const image = await imageImportFile(new File([data], path.split('/').pop()), current)
      result.push({ file: image, path })
    }
    if (format === 'zip') {
      const central = validateArchiveEnvelope(bytes, file.name)
      for (const item of central) inspect(item.name, item.size, item.directory, item.encrypted)
      const zip = await JSZip.loadAsync(bytes)
      if (Object.keys(zip.files).length !== central.length) throw invalid('ZIP 路径发生覆盖或目录不一致')
      for (const info of central) {
        if (info.directory) continue
        current = `${file.name}/${info.name}`
        const entry = zip.files[info.name]
        if (!entry || (entry.unixPermissions & 0o170000) === 0o120000) throw importError('IMPORT_UNSAFE_ENTRY', current, '不支持链接或特殊文件')
        const data = await new Promise((resolve, reject) => {
          const chunks = []
          let actual = 0
          const stream = entry.internalStream('uint8array')
          stream.on('data', chunk => {
            actual += chunk.length
            if (actual > info.size || actual > limits.maxFileBytes) { stream.pause(); reject(invalid('ZIP 展开长度超过目录声明')); return }
            chunks.push(chunk)
          }).on('error', reject).on('end', () => {
            const data = new Uint8Array(actual)
            let offset = 0
            for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length }
            resolve(data)
          }).resume()
        })
        if ((crc32(data) >>> 0) !== info.crc) throw invalid('ZIP CRC 校验失败')
        await add(data, info.name, info.size)
      }
    } else if (format === 'rar') {
      const extractor = await createRarExtractor(bytes.buffer)
      const list = extractor.getFileList()
      if (list.arcHeader.flags.volume) throw importError('IMPORT_MULTIPART', current, '不支持分卷 RAR', '请先在本地合并解开。')
      if (list.arcHeader.flags.headerEncrypted) throw importError('IMPORT_ENCRYPTED', current, 'RAR 目录已加密', '请先在本地解密。')
      for (const entry of list.fileHeaders) inspect(entry.name, entry.unpSize, entry.flags.directory, entry.flags.encrypted)
      // Guard JS-side output too: UnRAR writes into JS buffers, not only WASM.
      const write = extractor.write.bind(extractor)
      const written = new Map()
      let actualTotal = 0
      extractor.write = (fd, pointer, length) => {
        const size = (written.get(fd) || 0) + length
        actualTotal += length
        if (size > limits.maxFileBytes || actualTotal > total || actualTotal > limits.maxTotalBytes) throw invalid('RAR 展开长度超过安全上限')
        written.set(fd, size)
        return write(fd, pointer, length)
      }
      for (const entry of extractor.extract().files) {
        if (entry.fileHeader.flags.directory) continue
        await add(entry.extraction, entry.fileHeader.name, entry.fileHeader.unpSize)
      }
    } else throw importError('IMPORT_UNSUPPORTED_ARCHIVE', current, '仅支持 ZIP、RAR 压缩包', '请在本地解开后上传图片。')
    if (!result.length) throw importError('IMPORT_EMPTY_ARCHIVE', current, '压缩包内没有图片')
    return result
  } catch (error) {
    if (error.code) throw error
    if (error.file) current = `${file.name}/${error.file}`
    const message = String(error.message || error)
    if (/password|encrypt/i.test(`${error.reason || ''} ${message}`)) throw importError('IMPORT_ENCRYPTED', current, '压缩包已加密', '请在本地解密后重新上传。')
    throw invalid(`解包失败（${error.reason || message}）`)
  }
}