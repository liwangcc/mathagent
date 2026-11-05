import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'
import { consumeSse, createSseEventError, reportSseServiceFault } from '../src/utils/sse.js'

test('教师OCR状态只显示行动结论，不暴露人数、槽位或队列位置', async () => {
  const upload = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

  assert.match(upload, /serviceState\.label/)
  for (const label of ['可用', '需排队', '暂不可用']) {
    assert.match(upload, new RegExp(label))
  }
  assert.match(upload, /链路故障/)
  assert.match(upload, /serviceBlocksNewWork/)
  assert.match(upload, /reportSseServiceFault/)
  assert.match(upload, /SERVICE_FAULT_HOLD_MS = 60_000/)
  assert.match(upload, /Date\.now\(\) < serviceFaultHoldUntil/)
  assert.match(upload, /if \(availabilityCheckPending\) return/)
  assert.match(upload, /批改链路状态响应异常/)
  assert.match(upload, /const ACCELERATOR_UNAVAILABLE_MESSAGE = '加速节点不可用，请使用客户端'/)
  assert.match(upload, /\['waiting_accelerator', 'pending', 'unavailable'\]\.includes\(status\)/)
  assert.match(upload, /message: advisory \? '[^']+' : ACCELERATOR_UNAVAILABLE_MESSAGE/)
  assert.match(upload, /serviceState\.status !== 'available'/)
  assert.match(upload, /const serviceBlocksNewWork = computed\(\(\) => \(\s*\['checking', 'unavailable', 'fault'\]\.includes\(serviceState\.value\.status\)\s*\)\)/)
  // 全局链路保持硬门禁；只有后端显式 advisory=true 才豁免额外硬件门禁。
  assert.match(upload, /serviceBlocksNewWork\.value\s*\|\| registrationHardwareBlocksNewWork\.value/)
  assert.match(upload, /const advisory = hardware\?\.advisory === true/)
  assert.match(upload, /async function onRunTemplateCut[\s\S]*?if \(!requireTemplateService\(\)\) return/)
  assert.match(upload, /async function confirmOrientationAndRetry[\s\S]*?if \(!requireRecognitionService\(\)\) return/)
  assert.match(upload, /const registrationHardware = requiredHardwareState\(templateState\.registration_hardware\)/)
  assert.doesNotMatch(upload, /const templateHardware = requiredHardwareState\(templateState\.hardware\)/)
  assert.match(upload, /const templateModelStatus = labels\[templateState\.service_status\]/)
  assert.match(upload, /v-if="templateServiceState\.status !== 'available'"/)
  assert.match(upload, /:role="templateServiceBlocksNewWork \? 'alert' : 'status'"/)
  for (const forbidden of ['活跃教师', '排队人数', '队列位置', '空闲槽', '剩余请求槽']) {
    assert.doesNotMatch(upload, new RegExp(forbidden))
  }
})

test('OCR状态探测 GET /batch-grade/availability 使用10秒超时并关闭通用提示', async () => {
  const client = await loadApiClient()
  const state = { status: 'available' }
  client.respond(() => ({ data: state }))

  assert.deepEqual(await client.api.grade.availability(), state)
  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'get')
  assert.equal(request.url, '/batch-grade/availability')
  assert.equal(request.timeout, 10000)
  assert.equal(request.suppressErrorToast, true)
})

test('OCR状态探测失败只交给页面展示，拦截器不弹通用错误提示', async () => {
  const client = await loadApiClient()
  const detail = '批改链路暂不可用'
  client.respond(() => ({ status: 503, data: { detail } }))

  const probe = await client.api.grade.availability().catch(err => err)
  assert.equal(probe.message, detail)
  assert.deepEqual(client.messages, [])

  // 未声明 suppressErrorToast 的普通请求照常提示，证明是探测请求被豁免。
  const ordinary = await client.api.grade.results().catch(err => err)
  assert.equal(ordinary.message, detail)
  assert.deepEqual(client.messages, [{ type: 'error', message: detail }])
})

test('硬件状态缺省文案只给行动结论，不透传计数字段', async () => {
  const { module } = await loadApiClient()
  const counters = { message: '  ', active_teachers: 7, queue_position: 3, free_slots: 0 }
  const cases = [
    ['waiting_accelerator', '等待教师客户端加速节点'],
    ['unavailable', '本地加速节点不可用'],
  ]
  for (const [status, prefix] of cases) {
    const state = module.normalizeHardwareAvailability({ hardware: { status, ...counters } })
    assert.deepEqual(Object.keys(state).sort(), ['message', 'status'])
    assert.equal(state.status, status)
    assert.ok(state.message.startsWith(prefix), state.message)
  }
})

test('SSE 的 GRADING_CHAIN_FAILED 被判为批改链路故障并带服务端说明', async () => {
  const fault = { code: 'GRADING_CHAIN_FAILED', message: '批改链路故障：模型服务无响应' }
  const response = new Response(`event: error\ndata: ${JSON.stringify(fault)}\n\n`)
  const error = await consumeSse(response, (event, body) => {
    if (event === 'error') throw createSseEventError(body)
  }).catch(err => err)

  const marked = []
  assert.equal(reportSseServiceFault(error, message => marked.push(message), '链路中断'), true)
  assert.deepEqual(marked, [fault.message])

  // 普通业务错误不是链路故障，不能把服务状态打成故障。
  const business = createSseEventError({ code: 'ANSWER_SHEET_INVALID', message: '答题卡不完整' })
  assert.equal(reportSseServiceFault(business, message => marked.push(message), '链路中断'), false)
  assert.deepEqual(marked, [fault.message])
})
