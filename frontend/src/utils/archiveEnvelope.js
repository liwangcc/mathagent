import { detectImportFormat, importError } from './fileImportPolicy.js'

// Validate ZIP directory before JSZip normalizes paths or overwrites duplicates.
export function validateArchiveEnvelope(bytes, source) {
  const format = detectImportFormat(bytes)
  const invalid = () => { throw importError('IMPORT_ARCHIVE_INVALID', source, '压缩包目录或尾部损坏/缺失', '请重新下载完整压缩包；分卷请先在本地合并解开。') }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  if (format === 'zip') {
    let end = -1
    for (let pos = bytes.length - 22; pos >= Math.max(0, bytes.length - 65557); pos--) {
      if (view.getUint32(pos, true) === 0x06054b50 && pos + 22 + view.getUint16(pos + 20, true) === bytes.length) { end = pos; break }
    }
    if (end < 0) invalid()
    const count = view.getUint16(end + 10, true)
    const size = view.getUint32(end + 12, true)
    const offset = view.getUint32(end + 16, true)
    if (view.getUint16(end + 4, true) || view.getUint16(end + 6, true)) {
      throw importError('IMPORT_MULTIPART', source, '不支持分卷压缩包', '请在本地合并解开后上传。')
    }
    if (count === 65535 || size === 0xffffffff || offset === 0xffffffff) {
      throw importError('IMPORT_UNSUPPORTED_ARCHIVE', source, '不支持 ZIP64 容器', '请重新打包为普通 ZIP（每包不超过 64MB）。')
    }
    if (offset + size !== end || view.getUint16(end + 8, true) !== count) invalid()
    const entries = []
    let pos = offset
    for (let i = 0; i < count; i++) {
      if (pos + 46 > end || view.getUint32(pos, true) !== 0x02014b50) invalid()
      const nameLength = view.getUint16(pos + 28, true)
      let name
      try { name = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(pos + 46, pos + 46 + nameLength)) }
      catch { throw importError('IMPORT_FILENAME_ENCODING', source, 'ZIP 文件名不是 UTF-8', '请使用 UTF-8 文件名重新打包，或解开后直接选文件夹。') }
      entries.push({ name, size: view.getUint32(pos + 24, true), crc: view.getUint32(pos + 16, true), encrypted: Boolean(view.getUint16(pos + 8, true) & 1), directory: name.endsWith('/') })
      pos += 46 + view.getUint16(pos + 28, true) + view.getUint16(pos + 30, true) + view.getUint16(pos + 32, true)
    }
    if (pos !== end) invalid()
    return entries
  }
}
