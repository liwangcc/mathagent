import { normalizeUploadSourcePath } from './uploadSource.js'

export const IMPORT_ACCEPT = '.jpg,.jpeg,.png,.webp,.gif,.bmp,.zip,.rar'
export const IMPORT_FORMATS = 'JPG / PNG / WebP / GIF / BMP 图片；ZIP / RAR 压缩包'
export const IMPORT_LIMITS = Object.freeze({
  maxFiles: 200,
  maxEntries: 2048,
  maxFileBytes: 20 * 1024 * 1024,
  maxArchiveBytes: 64 * 1024 * 1024,
  maxTotalBytes: 200 * 1024 * 1024,
  maxRatio: 200,
  timeoutMs: 30000,
})

// Folder pickers and macOS archives carry operating-system metadata next to
// real files. It is never a student image, so it is skipped instead of failing
// the whole import; every other non-image still fails by name.
const OS_METADATA_NAMES = new Set(['.ds_store', 'thumbs.db', 'desktop.ini'])

export function isOsMetadataPath(path) {
  const parts = String(path || '').replace(/\\/g, '/').split('/')
  const name = parts[parts.length - 1].toLowerCase()
  return parts.includes('__MACOSX') || OS_METADATA_NAMES.has(name) || name.startsWith('._')
}

export function importError(code, source, reason, advice = '请检查该文件后重新选择。') {
  const error = new Error(`[${code}] ${source}：${reason}；${advice}`)
  error.code = code
  error.source = source
  error.stage = 'file_import'
  return error
}

export function safeImportPath(path) {
  try {
    const safe = normalizeUploadSourcePath(path)
    // These paths must never reach object-key based archive helpers either.
    if (safe.split('/').some(part => ['__proto__', 'constructor', 'prototype'].includes(part))) throw new Error()
    return safe
  } catch {
    throw importError('IMPORT_UNSAFE_PATH', String(path), '路径包含越界、绝对路径或无效目录')
  }
}

export function detectImportFormat(bytes) {
  const at = (offset, values) => values.every((value, i) => bytes[offset + i] === value)
  const text = (offset, value) => at(offset, [...value].map(char => char.charCodeAt(0)))
  if (at(0, [0xff, 0xd8, 0xff])) return 'image/jpeg'
  if (at(0, [137, 80, 78, 71, 13, 10, 26, 10])) return 'image/png'
  if (text(0, 'GIF87a') || text(0, 'GIF89a')) return 'image/gif'
  if (text(0, 'BM')) return 'image/bmp'
  if (text(0, 'RIFF') && text(8, 'WEBP')) return 'image/webp'
  if (at(0, [0x50, 0x4b, 3, 4]) || at(0, [0x50, 0x4b, 5, 6])) return 'zip'
  if (at(0, [0x52, 0x61, 0x72, 0x21, 0x1a, 7, 0])) return 'rar'
  if (at(0, [0x52, 0x61, 0x72, 0x21, 0x1a, 7, 1, 0])) return 'rar'
  if (at(0, [0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c])) return '7z'
  if (text(257, 'ustar')) return 'tar'
  if (at(0, [0x1f, 0x8b])) return 'tar.gz'
  if (text(0, 'BZh')) return 'tar.bz2'
  if (at(0, [0xfd, 0x37, 0x7a, 0x58, 0x5a, 0])) return 'tar.xz'
  if (text(0, '%PDF-')) return 'pdf'
  return ''
}

export async function imageImportFile(file, source) {
  if (!file.size) throw importError('IMPORT_EMPTY_FILE', source, '文件为空')
  if (file.size > IMPORT_LIMITS.maxFileBytes) throw importError('IMPORT_FILE_LIMIT', source, '单张图片超过 20MB')
  const format = detectImportFormat(new Uint8Array(await file.slice(0, 512).arrayBuffer()))
  if (!format.startsWith('image/')) {
    throw importError('IMPORT_UNSUPPORTED_FILE', source, `不是支持的图片（检测为 ${format || '未知/损坏'}）`,
      'PDF、Word 请先逐页导出图片；嵌套压缩包请先在本地解开。')
  }
  const extension = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/bmp': 'bmp', 'image/webp': 'webp' }[format]
  // MIME is derived from bytes, not JSZip's empty Blob.type or the filename.
  const name = file.name.replace(/\.[^.]+$/, '') || 'image'
  return new File([file], `${name}.${extension}`, { type: format, lastModified: file.lastModified })
}
