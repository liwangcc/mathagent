// Display/identity metadata only. Never use these paths as server storage paths.
export function normalizeUploadSourcePath(value) {
  const path = String(value || '').replace(/\\/g, '/')
  const parts = path.split('/')
  if (!path || path.length > 2048 || parts.length > 32
      || /[\x00-\x1f\x7f:]/.test(path)
      || parts.some(part => !part || part === '.' || part === '..')) {
    throw new Error('上传文件的相对路径无效，请检查文件夹或压缩包')
  }
  return path
}

export function getUploadSourcePath(source) {
  const file = source?.raw || source
  return normalizeUploadSourcePath(file?.sourceRelativePath || file?.webkitRelativePath || file?.name)
}

export function withUploadSourcePath(file, path) {
  Object.defineProperty(file, 'sourceRelativePath', {
    value: normalizeUploadSourcePath(path), configurable: true,
  })
  return file
}

export function appendStudentUploadFiles(form, files) {
  files.forEach(file => form.append('files', file?.raw || file))
  form.append('source_paths', JSON.stringify(files.map(getUploadSourcePath)))
}
