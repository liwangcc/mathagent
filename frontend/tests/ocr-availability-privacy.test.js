import assert from 'node:assert/strict'
import test from 'node:test'
import {
  ACCELERATOR_UNAVAILABLE,
  AVAILABILITY,
  TEMPLATE_AVAILABILITY,
  clickTemplateImport,
  dropPapers,
  errorMessages,
  exactButton,
  isDisabled,
  pickersOpenedBy,
  textOf,
} from './helpers/availabilityQuota.js'
import { loadApiClient } from './helpers/apiClient.js'
import { HEALTHY_SERVICE, mountUpload, paperFile } from './helpers/uploadPage.js'
import { consumeSse, createSseEventError, reportSseServiceFault } from '../src/utils/sse.js'

// The first group mounts the real Upload page against its readiness probes
// (GET /batch-grade/availability and /batch-grade/template/availability) and
// checks what a teacher sees and can start; the rest drive the API client and
// the SSE helpers the page uses.

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const MALFORMED = '批改链路状态响应异常，请稍后重试或联系管理员。'
// Counters a readiness answer may carry. The page shows action labels only.
const COUNTERS = { active_teachers: 7, queue_position: 3, free_slots: 0, remaining_request_slots: 2 }
const FORBIDDEN = ['活跃教师', '排队人数', '队列位置', '空闲槽', '剩余请求槽']
const READY_HARDWARE = { status: 'ready' }

const globalAlert = upload => upload.page.root.byClass('service-alert-global')
const templateAlert = upload => upload.page.root.byClass('template-service-alert')
const serviceLabel = upload => upload.page.root.byClass('service-state').text
const papersLocked = upload => isDisabled(exactButton(upload, '选择文件'))
const templateLocked = upload => upload.page.root.byClass('template-upload').getAttribute('aria-disabled') === 'true'
// Every attribute value rendered at or under `el` (title, aria-label, a
// tooltip's content, ...): a hover hint or a screen reader shows them too.
const attributeValues = el => [el, ...el.findAll(() => true)].flatMap(node => Object.values(node.attributes ?? {}))

test('教师OCR状态只显示行动结论，不暴露人数、槽位或队列位置', async (t) => {
  for (const [status, label, blocks] of [
    ['available', '可用', false],
    ['queued', '需排队', false],
    ['unavailable', '暂不可用', true],
    ['fault', '链路故障', true],
  ]) {
    const message = status === 'available' ? '' : '维护窗口内请稍后'
    const upload = await mountUpload(t, {
      responses: {
        [AVAILABILITY]: { status, message, ...COUNTERS },
        [TEMPLATE_AVAILABILITY]: { status, service_status: status, message, ...COUNTERS, registration_hardware: { ...READY_HARDWARE, ...COUNTERS } },
      },
    })
    assert.equal(serviceLabel(upload), label)
    if (status === 'available') {
      assert.equal(textOf(globalAlert(upload)), null)
      assert.equal(textOf(templateAlert(upload)), null)
    } else {
      assert.equal(globalAlert(upload).text, `${label}${message}`)
      assert.equal(globalAlert(upload).getAttribute('role'), blocks ? 'alert' : 'status', status)
      assert.equal(templateAlert(upload).text, `${label}${message}`)
    }
    // Only checking / unavailable / fault close new work; queued still accepts it.
    assert.equal(papersLocked(upload), blocks, status)
    assert.equal(templateLocked(upload), blocks, status)
    const text = upload.text()
    const attributes = attributeValues(upload.page.root).join(' ')
    for (const word of FORBIDDEN) {
      assert.ok(!text.includes(word), `${status}: ${word}`)
      assert.ok(!attributes.includes(word), `${status}: ${word} in an attribute`)
    }
    // The status and its notices carry no count either, as text or as an attribute.
    for (const el of [upload.page.root.byClass('service-state'), globalAlert(upload), templateAlert(upload)].filter(Boolean)) {
      const shown = [el.text, ...attributeValues(el)].join(' ')
      for (const [counter, value] of Object.entries(COUNTERS)) {
        assert.ok(!shown.includes(String(value)), `${status}: ${counter} shown by ${el.className}: ${shown}`)
      }
    }
    upload.unmount()
  }
})

test('探测结果返回前按检测中处理，学生试卷与模板导入都不开放', async (t) => {
  const pending = () => new Promise(() => {})
  const upload = await mountUpload(t, { responses: { [AVAILABILITY]: pending, [TEMPLATE_AVAILABILITY]: pending } })
  assert.equal(serviceLabel(upload), '检测中')
  assert.equal(globalAlert(upload).getAttribute('role'), 'alert')
  assert.equal(papersLocked(upload), true)
  assert.equal(templateLocked(upload), true)
})

test('批改链路故障至少保持60秒，期间探测到可用也不解除', async (t) => {
  let now = Date.UTC(2026, 10, 6, 8)
  t.mock.method(Date, 'now', () => now)
  const upload = await mountUpload(t, { timers: true })
  upload.answer(AVAILABILITY, { status: 'fault', message: '模型服务无响应' })
  await upload.tick(15000)
  assert.equal(serviceLabel(upload), '链路故障')

  upload.answer(AVAILABILITY, { status: 'available', label: '可用', message: '' })
  now += 59_999
  await upload.tick(15000)
  assert.equal(serviceLabel(upload), '链路故障', 'still inside the 60 s hold')
  assert.equal(globalAlert(upload).text, '链路故障模型服务无响应')
  assert.equal(papersLocked(upload), true)
  assert.equal(templateLocked(upload), true)

  now += 1
  await upload.tick(15000)
  assert.equal(serviceLabel(upload), '可用', 'the hold ends after 60 s')
  assert.equal(textOf(globalAlert(upload)), null)
  assert.equal(papersLocked(upload), false)
  assert.equal(templateLocked(upload), false)
})

test('上一次状态探测未返回时，轮询不重复发起探测', async (t) => {
  const upload = await mountUpload(t, { timers: true })
  assert.equal(upload.requests(AVAILABILITY).length, 1)
  let answer
  upload.answer(AVAILABILITY, () => new Promise((resolve) => { answer = resolve }))
  await upload.tick(15000)
  assert.equal(upload.requests(AVAILABILITY).length, 2)
  await upload.tick(15000)
  await upload.tick(15000)
  assert.equal(upload.requests(AVAILABILITY).length, 2, 'no probe while one is still pending')
  assert.equal(upload.requests(TEMPLATE_AVAILABILITY).length, 2)

  answer({ status: 200, data: { status: 'unavailable', message: '维护中' } })
  await upload.settle()
  assert.equal(serviceLabel(upload), '暂不可用')
  upload.answer(AVAILABILITY, HEALTHY_SERVICE[AVAILABILITY])
  await upload.tick(15000)
  assert.equal(upload.requests(AVAILABILITY).length, 3)
  assert.equal(serviceLabel(upload), '可用')
})

test('状态响应缺少已知状态时按链路故障关闭新任务并说明响应异常', async (t) => {
  for (const answer of [{ status: 'checking' }, { status: 'busy', label: '可用' }, {}, null]) {
    const name = JSON.stringify(answer)
    const upload = await mountUpload(t, { responses: { [AVAILABILITY]: answer } })
    assert.equal(serviceLabel(upload), '链路故障', name)
    assert.equal(globalAlert(upload).text, `链路故障${MALFORMED}`, name)
    assert.equal(templateAlert(upload).text, `模板链路故障${MALFORMED}`, name)
    assert.equal(papersLocked(upload), true, name)
    assert.deepEqual(await pickersOpenedBy(upload, () => clickTemplateImport(upload)), [], name)
    assert.deepEqual(errorMessages(upload), [MALFORMED], name)
    upload.unmount()
  }
})

test('注册加速节点不可用时只给出使用客户端的行动结论', async (t) => {
  for (const [hardware, status] of [
    [{ status: 'waiting_accelerator' }, 'unavailable'],
    [{ status: 'pending' }, 'unavailable'],
    [{ status: 'unavailable' }, 'unavailable'],
    [{ status: 'offline' }, 'fault'],
    [undefined, 'fault'],
  ]) {
    const name = JSON.stringify(hardware) ?? 'missing'
    const upload = await mountUpload(t, {
      responses: { [TEMPLATE_AVAILABILITY]: { status: 'available', service_status: 'available', registration_hardware: hardware && { ...hardware, ...COUNTERS } } },
    })
    assert.equal(upload.page.setup.registrationHardwareState.status, status, name)
    assert.equal(upload.page.setup.registrationHardwareState.message, ACCELERATOR_UNAVAILABLE, name)
    assert.equal(serviceLabel(upload), '可用', name)
    assert.equal(papersLocked(upload), true, name)
    await dropPapers(upload)
    assert.deepEqual(errorMessages(upload), [ACCELERATOR_UNAVAILABLE], name)
    // Template correction runs on the server CPU: the accelerator does not gate it.
    assert.equal(templateLocked(upload), false, name)
    upload.unmount()
  }

  // An advisory accelerator state is only recorded, without the client instruction.
  const advisory = await mountUpload(t, {
    responses: { [TEMPLATE_AVAILABILITY]: { status: 'available', service_status: 'available', registration_hardware: { status: 'waiting_accelerator', advisory: true } } },
  })
  const { message } = advisory.page.setup.registrationHardwareState
  assert.ok(message && message !== ACCELERATOR_UNAVAILABLE, message)
  assert.equal(papersLocked(advisory), false)
})

test('注册门禁只看 registration_hardware，不看通用 hardware 字段', async (t) => {
  const template = { status: 'available', service_status: 'available' }
  const ready = await mountUpload(t, {
    responses: { [TEMPLATE_AVAILABILITY]: { ...template, hardware: { status: 'unavailable' }, registration_hardware: READY_HARDWARE } },
  })
  assert.equal(papersLocked(ready), false)
  ready.unmount()

  const missing = await mountUpload(t, {
    responses: { [TEMPLATE_AVAILABILITY]: { ...template, hardware: READY_HARDWARE } },
  })
  assert.equal(papersLocked(missing), true)
})

test('模板导入按模型链路状态开放，缺少有效模型状态时退回模板链路状态', async (t) => {
  for (const [answer, shown, blocks] of [
    [{ status: 'available', service_status: 'available' }, null, false],
    [{ status: 'available', service_status: 'unavailable', message: '模型维护中' }, 'unavailable', true],
    [{ status: 'available', service_status: 'fault', message: '模型无响应' }, 'fault', true],
    [{ status: 'available', service_status: 'queued' }, 'queued', false],
    [{ status: 'unavailable', service_status: 'bogus', message: '模板链路维护' }, 'unavailable', true],
    [{ status: 'queued' }, 'queued', false],
  ]) {
    const name = JSON.stringify(answer)
    const upload = await mountUpload(t, {
      responses: { [TEMPLATE_AVAILABILITY]: { ...answer, registration_hardware: READY_HARDWARE } },
    })
    const alert = templateAlert(upload)
    if (shown === null) assert.equal(textOf(alert), null, name)
    else {
      assert.ok(alert.classList.contains(`service-alert-${shown}`), `${name}: ${alert.className}`)
      assert.equal(alert.getAttribute('role'), blocks ? 'alert' : 'status', name)
    }
    assert.equal(templateLocked(upload), blocks, name)
    assert.deepEqual(await pickersOpenedBy(upload, () => clickTemplateImport(upload)), blocks ? [] : ['template'], name)
    assert.deepEqual(errorMessages(upload), blocks ? [answer.message] : [], name)
    // The core chain is fine: student papers stay open whatever the model says.
    assert.equal(papersLocked(upload), false, name)
    upload.unmount()
  }
})

test('链路不可用时重新裁剪模板和确认试卷方向都不发请求', async (t) => {
  const upload = await mountUpload(t, { responses: { [AVAILABILITY]: { status: 'unavailable', message: '维护中' } } })
  const { setup } = upload.page
  // 重新自动裁剪 is disabled; the page's own handler refuses as well.
  setup.pendingTemplateId = 'tpl-pending'
  await upload.settle()
  await setup.onRunTemplateCut({ force: true })
  await upload.settle()
  assert.deepEqual(upload.fetches('POST /api/batch-grade/template/tpl-pending/cut-candidates'), [])
  assert.deepEqual(errorMessages(upload), ['维护中'])

  // So is 确认文字正立并重新识别此卷 for a paper whose original image is loaded.
  setup.batchId = 'b1'
  setup.orientationPaper = { paper_id: 'p1', filename: 'a.png' }
  setup.orientationImageUrl = 'blob:synthetic-image'
  await upload.settle()
  await setup.confirmOrientationAndRetry()
  await upload.settle()
  assert.deepEqual(upload.fetch.requests.map(request => request.url), [])
  assert.deepEqual(upload.requests('POST /api/batch-grade/b1/paper/p1/orientation-confirm'), [])
  assert.deepEqual(errorMessages(upload), ['维护中', '维护中'])
})

test('识别流里的 GRADING_CHAIN_FAILED 让批改页显示链路故障并关闭新任务', async (t) => {
  const fault = { code: 'GRADING_CHAIN_FAILED', message: '批改链路故障：模型服务无响应' }
  const upload = await mountUpload(t, { streams: { [RECOGNIZE]: [['error', fault]] } })
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.equal(globalAlert(upload).text, `链路故障${fault.message}`)
  assert.equal(globalAlert(upload).getAttribute('role'), 'alert')
  assert.equal(isDisabled(exactButton(upload, '重新识别全部')), true)
  upload.unmount()

  // An ordinary business error leaves the chain usable.
  const business = await mountUpload(t, { streams: { [RECOGNIZE]: [['error', { code: 'ANSWER_SHEET_INVALID', message: '答题卡不完整' }]] } })
  business.page.setup.confirmedTemplateId = 'tpl-1'
  await business.choosePapers([paperFile('a.png')])
  assert.equal(business.fetches(RECOGNIZE).length, 1)
  assert.equal(textOf(globalAlert(business)), null)
  assert.equal(isDisabled(exactButton(business, '重新识别全部')), false)
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
