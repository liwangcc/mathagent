import test from 'node:test'
import assert from 'node:assert/strict'
import { loadApiClient } from './helpers/apiClient.js'
import { createErrorNotifier, enrichProcessingError } from '../src/utils/processingError.js'
import { createSseEventError, createSseResponseError, consumeSse } from '../src/utils/sse.js'

const info = (id = 'a'.repeat(32)) => ({
  error_id: id, correlation_id: 'c'.repeat(32), code: 'PROVIDER_TIMEOUT',
  stage: 'transcription', message: '答案转录：服务未在时限内完成', action: '稍后重试', retryable: true,
})

test('HTTP interceptor plus page catch notifies exactly once; another failure survives', async () => {
  const client = await loadApiClient()
  const { notifyProcessingError } = await import('../src/utils/errorNotification.js')
  const fail = id => () => ({ status: 500, data: { detail: '处理失败', error_info: info(id) } })

  client.respond(fail('d'.repeat(32)))
  const error = await client.api.grade.results().catch(err => err)
  assert.equal(error.errorId, 'd'.repeat(32))
  assert.equal(notifyProcessingError(error, 'page catch'), false)
  assert.equal(client.messages.length, 1)
  assert.equal(client.messages[0].type, 'error')
  assert.match(client.messages[0].message, /答案转录.*诊断编号/)

  client.respond(fail('e'.repeat(32)))
  const another = await client.api.grade.results().catch(err => err)
  assert.equal(another.message, error.message)
  assert.equal(client.messages.length, 2)
  assert.deepEqual(client.messages[1], client.messages[0])

  // Without error_info only object identity links the toast to the page catch.
  const transport = Object.assign(new Error('old wrapper'), { response: { status: 500, data: { detail: '旧接口失败' } } })
  client.respond(() => { throw transport })
  const legacy = await client.api.grade.results().catch(err => err)
  assert.equal(legacy, transport)
  assert.equal(notifyProcessingError(legacy, 'page catch'), false)
  assert.equal(client.messages.length, 3)
  assert.deepEqual(client.messages[2], { type: 'error', message: '旧接口失败' })
})

test('SSE and polled failed status retain one identity, including reload', async () => {
  const messages = []
  const data = new Map()
  const storage = { getItem: key => data.get(key), setItem: (key, value) => data.set(key, value) }
  const notify = createErrorNotifier(message => messages.push(message), storage)
  const payload = { code: 'TEMPLATE_IMPORT_FAILED', message: '旧文案', error_info: info() }
  const response = new Response(`event: error\ndata: ${JSON.stringify(payload)}\n\n`)
  await assert.rejects(consumeSse(response, (event, body) => {
    if (event === 'error') throw createSseEventError(body)
  }), error => {
    assert.equal(error.processingCode, 'PROVIDER_TIMEOUT')
    assert.equal(notify(error), true)
    return true
  })
  assert.equal(notify(createSseEventError(payload)), false)
  assert.equal(createErrorNotifier(message => messages.push(message), storage)(createSseEventError(payload)), false)
  assert.equal(messages.length, 1)
})

test('HTTP JSON transport preserves actionable diagnostics', async () => {
  const error = await createSseResponseError(new Response(JSON.stringify({ detail: 'failed', error_info: info() }), { status: 503 }))
  assert.equal(error.errorId, 'a'.repeat(32))
  assert.equal(error.stage, 'transcription')
  assert.match(error.message, /稍后重试/)
})

test('template schema details and honest retry advice survive HTTP, SSE and reload', async () => {
  const detail = { ...info(), code: 'TEMPLATE_SCHEMA_INVALID', stage: 'template',
    message: '模板处理：第 1 题作图标签必须位于主作图区左侧，请教师复核',
    action: '重试仅会重新生成模型结果，可能再次违反同一规则，不建议连续重试。', retryable: false }
  const payload = { message: '模板导入失败', error_info: detail }
  const errors = [
    enrichProcessingError(new Error('旧文案'), payload),
    createSseEventError(payload),
    createSseEventError(JSON.parse(JSON.stringify(payload))),
    await createSseResponseError(new Response(JSON.stringify(payload), { status: 422 })),
  ]
  for (const error of errors) {
    assert.match(error.message, /第 1 题.*主作图区左侧/)
    assert.match(error.message, /仅会重新生成.*可能再次/)
    assert.equal(error.errorInfo.retryable, false)
    assert.doesNotMatch(error.message, /不符合格式要求|重新处理；/)
  }
})

test('identical text from independent legacy errors is not suppressed; cancellation is silent', () => {
  const messages = []
  const notify = createErrorNotifier(x => messages.push(x))
  const first = new Error('same')
  assert.equal(notify(first), true)
  assert.equal(notify(first), false)
  assert.equal(notify(new Error('same')), true)
  assert.equal(notify(Object.assign(new Error('cancel'), { name: 'AbortError' })), false)
  assert.equal(messages.length, 2)
})

test('notification cache has bounded size and stores only opaque IDs', () => {
  let raw
  const notify = createErrorNotifier(() => {}, { getItem: () => null, setItem: (_, value) => { raw = value } })
  for (let i = 0; i < 520; i++) notify(enrichProcessingError(new Error('not persisted'), { error_info: info(i.toString(16).padStart(32, '0')) }))
  assert.equal(JSON.parse(raw).length, 512)
  assert.ok(!raw.includes('not persisted'))
})
