export function triggerBlobDownload(blob, filename = 'download') {
  const objUrl = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => window.URL.revokeObjectURL(objUrl), 3000)
}
