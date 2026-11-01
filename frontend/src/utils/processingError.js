// Framework-free error identity and rendering contract, shared by HTTP/SSE.
const ID = /^[a-f0-9]{32}$/
const SEEN_KEY = 'mathagent.processing.seen.v1'

export function enrichProcessingError(error, payload = {}, fallback = '处理失败') {
  const info = payload?.error_info || payload?.detail?.error_info
  if (info && typeof info === 'object' && ID.test(info.error_id) && ID.test(info.correlation_id)) {
    error.errorInfo = { ...info }
    error.errorId = info.error_id
    error.correlationId = info.correlation_id
    error.stage = info.stage
    error.processingCode = info.code
    // Preserve legacy business wording when no more specific classification exists.
    const summary = ['PROCESSING_FAILED', 'REQUEST_REJECTED', 'FORMAT_INVALID'].includes(info.code)
      ? (error.message || fallback) : info.message
    error.message = `${summary}；${info.action || ''}（诊断编号 ${info.correlation_id}）`
  }
  return error
}

export function createErrorNotifier(show, storage = null) {
  const seenObjects = new WeakSet()
  let seenIds = new Set()
  try {
    const stored = JSON.parse(storage?.getItem(SEEN_KEY) || '[]')
    if (Array.isArray(stored)) seenIds = new Set(stored.filter(x => typeof x === 'string' && ID.test(x)).slice(-512))
  } catch { /* storage is optional; private browsing may disable it */ }
  return (error, fallback = '') => {
    if (error?.name === 'AbortError' || error?.name === 'CanceledError') return false
    const object = error && typeof error === 'object' ? error : null
    const id = object?.errorId || object?.error_info?.error_id
    if (object && seenObjects.has(object)) return false
    if (id && seenIds.has(id)) return false
    if (object) seenObjects.add(object)
    if (typeof id === 'string' && ID.test(id)) {
      seenIds.add(id)
      if (seenIds.size > 512) seenIds.delete(seenIds.values().next().value)
      try { storage?.setItem(SEEN_KEY, JSON.stringify([...seenIds])) } catch { /* optional */ }
    }
    // Do not dedupe text: two independent failures may have the same message.
    show(object?.errorInfo ? object.message : (fallback || object?.message || '处理失败'))
    return true
  }
}
