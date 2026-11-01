import { enrichProcessingError } from './processingError.js'

function parseBlock(block, onEvent) {
  let evt = 'message'
  const dataLines = []
  for (const line of block.replace(/\r/g, '').split('\n')) {
    if (line.startsWith('event:')) evt = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (!dataLines.length) return null
  let data
  try { data = JSON.parse(dataLines.join('\n')) } catch { return null }
  onEvent(evt, data)
  return evt
}

const SERVICE_FAULT_CODES = new Set([
  'GRADING_CHAIN_FAILED',
  'VISION_AUTH_FAILED',
  'VISION_TIMEOUT',
  'TEMPLATE_OCR_FAILED',
  'TEMPLATE_OUTPUT_INVALID',
  'TEMPLATE_CHAIN_UNAVAILABLE',
  'TEMPLATE_IMPORT_TIMEOUT',
  'TEMPLATE_BACKGROUND_TASK_FAILED',
  'TEMPLATE_IMPORT_FAILED',
  'MODEL_UNAVAILABLE',
  'CREDENTIAL_UNAVAILABLE',
  'SDK_UNAVAILABLE',
  'PROVIDER_REQUEST_FAILED',
  'PROVIDER_UNAVAILABLE',
  'PROVIDER_AUTH_FAILED',
  'PROVIDER_TIMEOUT',
  'PROVIDER_RATE_LIMITED',
  'TRANSCRIPTION_EMPTY',
  'OUTPUT_PARSE_FAILED',
  'DEPENDENCY_MISSING',
])

// Every grading SSE route emits progress at least every 15 seconds while work
// is alive. Eight missed heartbeats distinguishes a stalled transport from
// normal model or hardware latency without using a short end-to-end deadline.
const SSE_IDLE_TIMEOUT_MS = 120_000

// `done` and `error` carry a stream's application-level result.
const TERMINAL_EVENTS = new Set(['done', 'error'])

function payloadErrorInfo(payload, fallbackMessage) {
  const detail = payload?.detail
  const detailObject = detail && typeof detail === 'object' && !Array.isArray(detail)
    ? detail
    : null
  const code = String(
    payload?.code
      || payload?.error
      || detailObject?.code
      || detailObject?.error
      || '',
  ).trim()
  let message = ''
  if (typeof detail === 'string') message = detail
  else if (detailObject?.message) message = String(detailObject.message)
  else if (payload?.message) message = String(payload.message)
  else if (Array.isArray(detail)) {
    message = detail.map((item) => item?.msg || '').filter(Boolean).join('; ')
  }
  return { code, message: message || fallbackMessage }
}

export class SseTransportError extends Error {
  constructor(message = '连接提前结束，未收到完成状态', options = {}) {
    super(message, options)
    this.name = 'SseTransportError'
    this.sseFailureKind = 'transport'
  }
}

export class SseResponseError extends Error {
  constructor(message, { status = 0, code = '', failureKind = 'application' } = {}) {
    super(message)
    this.name = 'SseResponseError'
    this.status = Number(status) || 0
    this.code = String(code || '')
    this.sseFailureKind = failureKind
  }
}

export async function createSseResponseError(response, fallbackMessage = '请求失败') {
  let payload = {}
  try {
    payload = await response.json()
  } catch {
    // A proxy-generated error page is still classified from its HTTP status.
  }
  const status = Number(response?.status) || 0
  const info = payloadErrorInfo(payload, `${fallbackMessage}${status ? ` (${status})` : ''}`)
  return enrichProcessingError(new SseResponseError(info.message, {
    status,
    code: info.code,
    failureKind: 'http',
  }), payload)
}

export function createSseEventError(payload, fallbackMessage = '处理失败') {
  const info = payloadErrorInfo(payload, fallbackMessage)
  return enrichProcessingError(new SseResponseError(info.message, { code: info.code }), payload)
}

export function isSseRequestCancelled(error, signal) {
  return error?.name === 'AbortError' || signal?.aborted === true
}

export function isSseServiceFault(error, { signal } = {}) {
  if (!error || isSseRequestCancelled(error, signal)) return false
  if (error.sseFailureKind === 'transport') return true
  if (SERVICE_FAULT_CODES.has(String(error.code || '').toUpperCase())) return true
  if (SERVICE_FAULT_CODES.has(String(error.processingCode || '').toUpperCase())) return true
  const status = Number(error.status) || 0
  if (status >= 500 || status === 408 || status === 429) return true
  if (status >= 400) return false
  if (String(error.message || '').trim() === '批改处理失败，请稍后重试') return true
  return /failed to fetch|fetch failed|network\s*error|networkerror|load failed|connection (?:was )?(?:lost|closed|reset)/i
    .test(String(error.message || ''))
}

export function reportSseServiceFault(error, markServiceFault, fallbackMessage, options = {}) {
  if (!isSseServiceFault(error, options)) return false
  const useServerMessage = error.sseFailureKind !== 'transport'
    && (error.sseFailureKind === 'http' || error.code)
  markServiceFault(useServerMessage ? error.message : fallbackMessage)
  return true
}

async function readSseChunk(reader, idleTimeoutMs) {
  const timeoutMs = Number(idleTimeoutMs)
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return reader.read()

  let timeoutId
  try {
    return await Promise.race([
      reader.read(),
      new Promise((_, reject) => {
        timeoutId = globalThis.setTimeout(() => {
          reject(new SseTransportError(
            `处理链路超过 ${Math.ceil(timeoutMs / 1000)} 秒未返回进度，连接可能已中断`,
          ))
        }, timeoutMs)
      }),
    ])
  } finally {
    if (timeoutId !== undefined) globalThis.clearTimeout(timeoutId)
  }
}

export async function consumeSse(response, onEvent, options = {}) {
  const { idleTimeoutMs = SSE_IDLE_TIMEOUT_MS } = options
  if (!response?.body?.getReader) {
    throw new SseTransportError('响应不包含可读取的数据流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminalEvent = ''
  let previousWasCr = false
  function appendText(text) {
    // CRLF is a single line ending even when its bytes arrive in two reads.
    // Normalize CR immediately, then suppress only the following LF.
    for (const character of text) {
      if (character === '\r') buffer += '\n'
      else if (character !== '\n' || !previousWasCr) buffer += character
      previousWasCr = character === '\r'
    }
  }
  try {
    while (true) {
      let chunk
      try {
        chunk = await readSseChunk(reader, idleTimeoutMs)
      } catch (error) {
        if (error?.name === 'AbortError') throw error
        if (error instanceof SseTransportError) throw error
        throw new SseTransportError('连接读取中断，未收到完成状态', { cause: error })
      }
      const { done, value } = chunk
      appendText(done ? decoder.decode() : decoder.decode(value, { stream: true }))
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        const evt = parseBlock(block, onEvent)
        if (TERMINAL_EVENTS.has(evt)) {
          // The terminal payload is the application-level result.  Do not
          // wait for a clean transport EOF: proxies may close an SSE stream
          // immediately after sending `done`, which can make the next read
          // reject with a network error after the result was already received.
          terminalEvent = evt
          return terminalEvent
        }
      }
      if (done) break
    }
    // EOF cannot dispatch an unfinished event, even if its JSON looks valid.
    if (!terminalEvent) throw new SseTransportError()
    return terminalEvent
  } finally {
    try {
      await reader.cancel()
    } catch {
      // The transport may already be closed or aborted.
    } finally {
      reader.releaseLock?.()
    }
  }
}
