import assert from 'node:assert/strict'
import test from 'node:test'

import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  reportSseServiceFault,
  SseTransportError,
} from '../src/utils/sse.js'

function responseFromReads(reads) {
  let index = 0
  return {
    body: {
      getReader() {
        return {
          async read() {
            const next = reads[index++]
            if (next instanceof Error) throw next
            return next || { done: true }
          },
          async cancel() {},
        }
      },
    },
  }
}

test('consumeSse returns after done without waiting for a clean EOF', async () => {
  const encoder = new TextEncoder()
  let reads = 0
  const response = {
    body: {
      getReader() {
        return {
          async read() {
            reads += 1
            if (reads === 1) {
              return {
                done: false,
                value: encoder.encode('event: done\ndata: {"total":1}\n\n'),
              }
            }
            throw new TypeError('NetworkError')
          },
          async cancel() {},
        }
      },
    },
  }
  const events = []

  const terminal = await consumeSse(response, (event, data) => {
    events.push({ event, data })
  })

  assert.equal(terminal, 'done')
  assert.deepEqual(events, [{ event: 'done', data: { total: 1 } }])
  assert.equal(reads, 1)
})

test('consumeSse classifies a reader failure and an early EOF as transport faults', async () => {
  let readerFailure
  let earlyEof
  await assert.rejects(
    consumeSse(responseFromReads([new TypeError('NetworkError')]), () => {}),
    (error) => {
      readerFailure = error
      return error instanceof SseTransportError
    },
  )
  await assert.rejects(
    consumeSse(responseFromReads([{ done: true }]), () => {}),
    (error) => {
      earlyEof = error
      return error instanceof SseTransportError
    },
  )

  for (const error of [readerFailure, earlyEof]) {
    const messages = []
    assert.equal(
      reportSseServiceFault(error, (message) => messages.push(message), '批改连接中断'),
      true,
    )
    assert.deepEqual(messages, ['批改连接中断'])
  }
})

test('consumeSse turns a silent stream into a visible transport fault', async () => {
  let cancelled = false
  const response = {
    body: {
      getReader() {
        return {
          async read() { return new Promise(() => {}) },
          async cancel() { cancelled = true },
        }
      },
    },
  }

  await assert.rejects(
    consumeSse(response, () => {}, { idleTimeoutMs: 10 }),
    (error) => (
      error instanceof SseTransportError
      && /未返回进度/.test(error.message)
    ),
  )
  assert.equal(cancelled, true)
})

test('HTTP service failures enter the fault window while user input errors do not', async () => {
  const serviceError = await createSseResponseError({
    status: 503,
    async json() { return { detail: { code: 'MODEL_UNAVAILABLE', message: '数学模型不可用' } } },
  }, '批改请求失败')
  const validationError = await createSseResponseError({
    status: 422,
    async json() { return { detail: { error: 'VALIDATION_ERROR', message: '题号重复' } } },
  }, '批改请求失败')
  const messages = []

  assert.equal(reportSseServiceFault(serviceError, (message) => messages.push(message)), true)
  assert.equal(reportSseServiceFault(validationError, (message) => messages.push(message)), false)
  assert.deepEqual(messages, ['数学模型不可用'])
  assert.equal(validationError.message, '题号重复')
})

test('explicit chain failures and browser fetch failures enter the fault window, cancellation does not', () => {
  const messages = []
  const mark = (message) => messages.push(message)

  assert.equal(reportSseServiceFault(
    createSseEventError({ code: 'GRADING_CHAIN_FAILED', message: '已保留完成结果' }),
    mark,
  ), true)
  assert.equal(reportSseServiceFault(
    createSseEventError({ code: 'TEMPLATE_IMPORT_TIMEOUT', message: '模板任务已超时' }),
    mark,
  ), true)
  assert.equal(reportSseServiceFault(
    createSseEventError({ code: 'BATCH_GRADING_BUSY', message: '批次忙' }),
    mark,
  ), false)
  assert.equal(reportSseServiceFault(new TypeError('Failed to fetch'), mark, '网络中断'), true)
  const abortError = new Error('cancelled')
  abortError.name = 'AbortError'
  assert.equal(reportSseServiceFault(abortError, mark, '不应记录'), false)
  const controller = new AbortController()
  controller.abort()
  const browserAbortRace = new TypeError('Failed to fetch')
  assert.equal(isSseRequestCancelled(browserAbortRace, controller.signal), true)
  assert.equal(
    reportSseServiceFault(browserAbortRace, mark, '不应记录', { signal: controller.signal }),
    false,
  )
  assert.deepEqual(messages, ['已保留完成结果', '模板任务已超时', '网络中断'])
})

function trackedResponse(chunks, failure) {
  const cleanup = { cancel: 0, release: 0, reads: 0 }
  const response = {
    body: { getReader: () => ({
      async read() {
        cleanup.reads += 1
        if (chunks.length) return { done: false, value: chunks.shift() }
        if (failure) throw failure
        return { done: true }
      },
      async cancel() { cleanup.cancel += 1 },
      releaseLock() { cleanup.release += 1 },
    }) },
  }
  return { response, cleanup }
}

const CRLF_BYTES = new TextEncoder().encode(
  ': 心跳\r\nevent: progress\r\ndata: {"message":"恢复识别📝",\r\ndata: "current":1}\r\n\r\n'
  + 'event: done\r\ndata: {"total":1}\r\n\r\n',
)
const CRLF_EVENTS = [
  ['progress', { message: '恢复识别📝', current: 1 }],
  ['done', { total: 1 }],
]

// All byte split points include the CR/LF boundary and UTF-8 continuation bytes.
for (let split = 1; split < CRLF_BYTES.length; split += 1) {
  test(`consumeSse preserves CRLF framing and UTF-8 at byte split ${split}`, async () => {
    const { response, cleanup } = trackedResponse([
      CRLF_BYTES.slice(0, split), CRLF_BYTES.slice(split),
    ], new TypeError('NetworkError after done'))
    const events = []
    assert.equal(await consumeSse(response, (event, data) => events.push([event, data])), 'done')
    assert.deepEqual(events, CRLF_EVENTS)
    assert.equal(cleanup.cancel, 1)
    assert.equal(cleanup.release, 1)
  })
}

test('consumeSse preserves progress and done when CRLF/UTF-8 arrives one byte at a time', async () => {
  const { response, cleanup } = trackedResponse(Array.from(CRLF_BYTES, (byte) => Uint8Array.of(byte)))
  const events = []
  assert.equal(await consumeSse(response, (event, data) => events.push([event, data])), 'done')
  assert.deepEqual(events, CRLF_EVENTS)
  assert.equal(cleanup.cancel, 1)
  assert.equal(cleanup.release, 1)
})

for (const text of [
  'event: progress\ndata: {"current":1}\n\n',
  'event: done\ndata: {"total":1}',
  'event: done\ndata: {"total":1}\n',
  'event: done\r\ndata: {"total":1}\r\n',
  'event: done\ndata: {"total":',
  'event: done\ndata: {"total":\n\n',
]) {
  test(`consumeSse rejects EOF without a fully framed valid terminal: ${JSON.stringify(text)}`, async () => {
    const { response, cleanup } = trackedResponse([new TextEncoder().encode(text)])
    await assert.rejects(consumeSse(response, () => {}), SseTransportError)
    assert.equal(cleanup.cancel, 1)
    assert.equal(cleanup.release, 1)
  })
}

for (const event of ['progress', 'done', 'error']) {
  test(`consumeSse propagates ${event} business callback failures without swallowing or reclassifying`, async () => {
    const { response, cleanup } = trackedResponse([
      new TextEncoder().encode(`event: ${event}\ndata: {"message":"业务失败"}\n\n`),
    ])
    const error = createSseEventError({ code: 'BUSINESS_FAILURE', message: '教师确认未完成' })
    await assert.rejects(consumeSse(response, () => { throw error }), (caught) => caught === error)
    assert.equal(cleanup.cancel, 1)
    assert.equal(cleanup.release, 1)
  })
}

test('consumeSse returns a framed error terminal and cleans up without reading EOF', async () => {
  const { response, cleanup } = trackedResponse([
    new TextEncoder().encode('event: error\r\ndata: {"code":"BUSY"}\r\n\r\n'),
  ], new TypeError('NetworkError'))
  const events = []
  assert.equal(await consumeSse(response, (event, data) => events.push([event, data])), 'error')
  assert.deepEqual(events, [['error', { code: 'BUSY' }]])
  assert.equal(cleanup.reads, 1)
  assert.equal(cleanup.cancel, 1)
  assert.equal(cleanup.release, 1)
})

test('consumeSse propagates cancellation and releases the reader even if cancel itself rejects', async () => {
  const error = Object.assign(new Error('aborted'), { name: 'AbortError' })
  const { response, cleanup } = trackedResponse([], error)
  const getReader = response.body.getReader
  response.body.getReader = () => ({
    ...getReader(),
    async cancel() { cleanup.cancel += 1; throw new Error('already closed') },
  })
  await assert.rejects(consumeSse(response, () => {}), (caught) => caught === error)
  assert.equal(cleanup.cancel, 1)
  assert.equal(cleanup.release, 1)
})
