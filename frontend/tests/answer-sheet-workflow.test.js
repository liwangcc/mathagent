import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { loadApiClient } from './helpers/apiClient.js'

const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const view = read('../src/views/AnswerSheet.vue')
const api = read('../src/api/index.js')
const router = read('../src/router/index.js')
const layout = read('../src/views/Layout.vue')

// Ids that need encodeURIComponent, so every path segment proves it is encoded.
const SESSION = 's 1/x'
const BASE = '/answer-sheet/sessions/s%201%2Fx'
const SHEET_CODE = 'AS-1111-2222-3333-4444'
const body = config => (typeof config.data === 'string' ? JSON.parse(config.data) : config.data)
const sent = requests => requests.map(config => [config.method, config.url])
const studentPage = () => new File(['front'], '张三_正面.jpg', { type: 'image/jpeg' })

// SSE methods go through fetch, not axios: record each call and its Response.
function recordFetch(t) {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const response = new Response('', { status: 200 })
    calls.push({ url, ...options, response })
    return response
  })
  return calls
}

test('答题卡批阅是详细批改同级入口并使用独立懒加载路由', () => {
  assert.match(router, /path: 'answer-sheet'.*import\('@\/views\/AnswerSheet\.vue'\).*title: '答题卡批阅'/)
  const uploadIndex = layout.indexOf('index="/upload"')
  const answerSheetIndex = layout.indexOf('index="/answer-sheet"')
  const studentsIndex = layout.indexOf('index="/students"')
  assert.ok(uploadIndex >= 0 && uploadIndex < answerSheetIndex && answerSheetIndex < studentsIndex)
  assert.match(layout, /<Tickets \/>/)
})

test('API 客户端覆盖当前后端已开放的会话、语义与版面合同', async (t) => {
  const client = await loadApiClient()
  const { answerSheet } = client.api
  const sourceForm = new FormData()
  const layoutForm = new FormData()
  const questions = [{ question_no: '1', question_type: 'choice', standard_answer: 'A' }]
  const layoutPayload = { answer_regions: [], identity_regions: [] }
  const image = new Blob(['png'], { type: 'image/png' })
  client.respond(config => ({ data: config.responseType === 'blob' ? image : {} }))
  const created = []
  t.mock.method(URL, 'createObjectURL', (blob) => {
    created.push(blob)
    return 'blob:layout-page'
  })
  const streams = recordFetch(t)
  const { signal } = new AbortController()

  await answerSheet.createSession({ paper_template_code: 'PT-AAAA-BBBB-CCCC-DDDD' })
  await answerSheet.getSession(SESSION)
  await answerSheet.deleteSession(SESSION)
  await answerSheet.uploadSource(SESSION, sourceForm)
  await answerSheet.recognizeSource(SESSION)
  await answerSheet.confirmAnswers(SESSION, questions)
  await answerSheet.uploadLayout(SESSION, layoutForm)
  await answerSheet.saveLayout(SESSION, layoutPayload)
  await answerSheet.freezeLayout(SESSION)
  assert.equal(await answerSheet.layoutPageImage(SESSION, 'page/2'), 'blob:layout-page')
  await answerSheet.startStudentUpload(SESSION)
  await answerSheet.uploadStudents(SESSION, 'upload/1', studentPage())
  await answerSheet.discardStudentUpload(SESSION, 'upload/1')
  await answerSheet.processStudentUploadStream(SESSION, 'upload/1', signal)

  assert.deepEqual(sent(client.requests), [
    ['post', '/answer-sheet/sessions'],
    ['get', BASE],
    ['delete', BASE],
    ['post', `${BASE}/source/upload`],
    ['post', `${BASE}/source/recognize`],
    ['post', `${BASE}/answers/confirm`],
    ['post', `${BASE}/layout/upload`],
    ['put', `${BASE}/layout`],
    ['post', `${BASE}/layout/freeze`],
    ['get', `${BASE}/layout/pages/page%2F2/image`],
    ['post', `${BASE}/students/upload/start`],
    ['post', `${BASE}/students/upload`],
    ['delete', `${BASE}/students/upload/upload%2F1`],
  ])
  const [create, , , source, , confirm, layoutUpload, save, , pageImage] = client.requests
  assert.deepEqual(body(create), { paper_template_code: 'PT-AAAA-BBBB-CCCC-DDDD' })
  assert.equal(source.data, sourceForm)
  assert.deepEqual(body(confirm), { questions })
  assert.equal(layoutUpload.data, layoutForm)
  assert.deepEqual(body(save), layoutPayload)
  assert.equal(pageImage.responseType, 'blob')
  assert.deepEqual(created, [image])
  assert.deepEqual(streams.map(call => [call.method, call.url]), [
    ['POST', `/api${BASE}/students/upload/upload%2F1/process/stream`],
  ])
})

test('五阶段工作流持久化 session id 并从 GET 会话刷新恢复', () => {
  for (const heading of ['原始试卷与答案来源', '教师确认题目与答案', '上传空白答题卡', '版面绑定与冻结', '学生答题卡与批阅']) {
    assert.match(view, new RegExp(heading))
  }
  assert.match(view, /mathagent_answer_sheet_session_id/)
  assert.match(view, /localStorage\.setItem\(STORAGE_KEY, data\.session_id\)/)
  assert.match(view, /api\.answerSheet\.getSession\(saved\)/)
  assert.match(view, /已恢复上次未完成的答题卡会话/)
  assert.match(view, /statusOrder/)
})

test('前端明确分离三层材料并保留三种答案来源', () => {
  for (const source of ['embedded', 'separate', 'ai_generated']) assert.match(view, new RegExp(`value="${source}"`))
  assert.match(view, /原始试卷提供题目语义，空白答题卡提供版面，学生答题卡才是批阅对象/)
  assert.match(view, /form\.append\('source_files'/)
  assert.match(view, /form\.append\('answer_files'/)
  assert.match(view, /form\.append\('answer_source'/)
})

test('答题卡可选复用模板码，新建模板仍兼容外部版式', () => {
  assert.match(view, /可直接复用共享模板码，也可上传新的空白答题卡/)
  assert.match(view, /或首次建立模板/)
  assert.match(view, /支持自制及其他平台版式；上传后自动粗定位，按页面顺序选择/)
})

test('模板码输入框显示当前教师历史并且复用路径不再定位', () => {
  assert.match(view, /v-for="item in templateCodeHistory"/)
  assert.match(view, /输入模板码，或从历史记录中选择/)
  assert.match(view, /api\.answerSheet\.templateHistory\(\)/)
  assert.match(view, /api\.answerSheet\.useTemplateCode/)
  assert.match(view, /模板码已套用，无需模型重新定位/)
  assert.match(view, /provider_route !== 'qwen3\.8max'/)
  assert.match(view, /确认本页定位/)
  assert.match(view, /function confirmCurrentPageRegions\(\)/)
})

test('模板码历史、套用、发布与重新定位接口各自命中后端端点', async () => {
  const client = await loadApiClient()
  const { answerSheet } = client.api
  const templates = [{ template_code: SHEET_CODE }]
  client.respond(config => ({ data: config.url.endsWith('/templates/history') ? { templates } : {} }))

  assert.deepEqual(await answerSheet.templateHistory(), { templates })
  await answerSheet.useTemplateCode(SESSION, SHEET_CODE)
  await answerSheet.publishTemplateCode(SESSION)
  await answerSheet.locateLayout(SESSION)

  assert.deepEqual(sent(client.requests), [
    ['get', '/answer-sheet/templates/history'],
    ['post', `${BASE}/layout/templates/use`],
    ['post', `${BASE}/layout/templates/publish`],
    ['post', `${BASE}/layout/locate`],
  ])
  assert.deepEqual(body(client.requests[1]), { template_code: SHEET_CODE })
})

test('模板页和学生答题卡使用各自的文件数量上限', () => {
  assert.equal((view.match(/:max-files="20"/g) || []).length, 3)
  assert.match(view, /:max-files="200"/)
  assert.match(view, /selected\.slice\(0, props\.maxFiles\)/)
  assert.match(view, /单次最多选择 \$\{props\.maxFiles\} 个文件/)
})

test('答题卡页面失败关闭整链路状态并在故障时保留已有证据查看', () => {
  for (const label of ['检测中', '可用', '需排队', '暂不可用', '链路故障']) assert.match(view, new RegExp(label))
  assert.match(view, /window\.setInterval\(refreshAvailability, 15000\)/)
  assert.match(view, /\['checking', 'unavailable', 'fault'\]\.includes\(serviceState\.value\.status\)/)
  assert.match(view, /workflowReadonly = computed\(\(\) => serviceBlocksNewWork\.value && session\.value\?\.status !== 'archived'\)/)
  assert.match(view, /canDelete = computed\(\(\) => !serviceBlocksNewWork\.value/)
  assert.match(view, /:disabled="!canCreate \|\| serviceBlocksNewWork"/)
  assert.match(view, /\.workflow-readonly \.review-student-tabs/)
  assert.match(view, /\.workflow-readonly \.student-paper-pane/)
  assert.doesNotMatch(api, /active_teachers|queue_length|free_slots|queue_position/)
})

test('答题卡可用性探测以 10 秒超时无参 GET 且失败不弹全局提示，硬件归一化只输出状态与文案', async () => {
  const client = await loadApiClient()
  client.respond(() => ({ status: 503, data: { detail: '答题卡服务暂不可用' } }))

  await assert.rejects(client.api.answerSheet.availability(), { message: '答题卡服务暂不可用' })
  assert.deepEqual(client.messages, [])
  // The same failure on an ordinary request is toasted, so the silence above is availability's own opt-out.
  await assert.rejects(client.api.answerSheet.listSessions())
  assert.deepEqual(client.messages, [{ type: 'error', message: '答题卡服务暂不可用' }])

  const [probe] = client.requests
  assert.equal(probe.method, 'get')
  assert.equal(probe.url, '/answer-sheet/availability')
  assert.equal(probe.timeout, 10000)
  assert.equal(probe.params, undefined)
  assert.equal(probe.data, undefined)

  const queueInternals = { active_teachers: 5, queue_length: 3, free_slots: 0, queue_position: 2 }
  const hardware = { status: 'waiting_accelerator', message: '排队等待加速节点' }
  assert.deepEqual(
    client.module.normalizeHardwareAvailability({ ...queueInternals, hardware: { ...hardware, ...queueInternals } }),
    hardware,
  )
})

test('答题卡可用性轮询单飞并保持运行期链路故障，未知状态也失败关闭', () => {
  assert.match(view, /let availabilityCheckPending = false/)
  assert.match(view, /if \(availabilityCheckPending\) return/)
  assert.match(view, /availabilityCheckPending = true/)
  assert.match(view, /finally \{\s*availabilityCheckPending = false\s*\}/)
  assert.match(view, /const SERVICE_FAULT_HOLD_MS = 60_000/)
  assert.match(view, /Date\.now\(\) < serviceFaultHoldUntil/)
  assert.match(view, /if \(!labels\[state\?\.status\]\) \{\s*markServiceFault/)
  assert.match(view, /if \(effectiveStatus === 'fault'\) \{\s*markServiceFault/)
})

test('答题卡页面消费机会式硬件状态，等待节点不锁死恢复，故障节点关闭新图像工作', () => {
  assert.match(view, /normalizeHardwareAvailability\(state\)/)
  assert.match(view, /mergeHardwareAvailabilityStatus\(state\.status, hardware\.status\)/)
  assert.match(view, /hardware\.message \|\| state\.message/)
  assert.match(view, /\['unavailable', 'fault'\]\.includes\(hardwareState\.value\.status\)/)
  assert.match(view, /workflowReadonly = computed\(\(\) => serviceBlocksNewWork\.value && session\.value\?\.status !== 'archived'\)/)
  assert.match(view, /const hardwareServiceStatus = mergeHardwareAvailabilityStatus\('available', hardware\.status\)/)
  assert.match(view, /hardwareServiceStatus === effectiveStatus[\s\S]*hardware\.message \|\| state\.message[\s\S]*state\.message \|\| hardware\.message/)
})

test('硬件状态只认五种机会式状态，等待节点映射为需排队，空文案按状态回退', async () => {
  const { module } = await loadApiClient()
  const { normalizeHardwareAvailability: normalize, mergeHardwareAvailabilityStatus: merge } = module

  for (const status of ['ready', 'waiting_accelerator', 'unavailable', 'fault', 'bypass']) {
    assert.deepEqual(normalize({ hardware: { status, message: ' 节点自述 ' } }), { status, message: '节点自述' })
  }
  assert.equal(normalize({ hardware: { status: 'overloaded', message: '节点自述' } }).status, 'fault')
  assert.equal(merge('available', 'waiting_accelerator'), 'queued')

  const fallbacks = ['waiting_accelerator', 'unavailable', 'fault']
    .map(status => normalize({ hardware: { status, message: '  ' } }).message)
  for (const message of fallbacks) assert.ok(message.length > 0)
  assert.equal(new Set(fallbacks).size, fallbacks.length)
  for (const status of ['ready', 'bypass']) assert.equal(normalize({ hardware: { status } }).message, '')
})

test('答题卡新任务有函数级硬件门禁，不仅依赖鼠标样式', () => {
  assert.match(view, /function requireAnswerSheetService\(\)/)
  for (const functionName of [
    'runAnswerSheetStream',
    'createSession',
    'uploadSource',
    'uploadLayout',
    'uploadStudents',
    'restartStudentUpload',
    'confirmOrientation',
  ]) {
    assert.match(view, new RegExp(`(?:async )?function ${functionName}\\([^)]*\\) \\{\\s*if \\(!requireAnswerSheetService\\(\\)\\) return`))
  }
})

test('答题卡写操作把网络、5xx 与长请求超时升级为链路故障', () => {
  assert.match(view, /isSseServiceFault,/)
  assert.match(view, /error\?\.response\?\.status \|\| error\?\.status/)
  for (const code of ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK']) assert.match(view, new RegExp(code))
  assert.match(view, /isSseServiceFault\(normalizeServiceError\(error\)\)/)
  assert.match(view, /markServiceFault\('答题卡批阅链路中断/)
})

test('答题卡长耗时写操作统一使用 660 秒请求超时', async () => {
  const client = await loadApiClient()
  const { answerSheet } = client.api
  const form = new FormData()

  await answerSheet.uploadSource(SESSION, form)
  await answerSheet.recognizeSource(SESSION)
  await answerSheet.uploadLayout(SESSION, form)
  await answerSheet.processLayout(SESSION)
  await answerSheet.locateLayout(SESSION)
  await answerSheet.useTemplateCode(SESSION, SHEET_CODE)
  await answerSheet.publishTemplateCode(SESSION)
  await answerSheet.uploadStudents(SESSION, 'upload/1', studentPage())
  await answerSheet.confirmOrientation(SESSION, 'paper/1', 1)
  await answerSheet.recognizeStudents(SESSION)
  await answerSheet.gradingDraft(SESSION)

  assert.equal(client.requests.length, 11)
  for (const request of client.requests) assert.equal(request.timeout, 660000, request.url)
})

test('三条长链路使用有心跳的 SSE 并可从持久化检查点恢复', () => {
  for (const method of ['recognizeSourceStream', 'recognizeStudentsStream', 'gradingDraftStream']) {
    assert.match(view, new RegExp(`api\\.answerSheet\\.${method}`))
  }
  assert.match(view, /consumeSse\(response/)
  assert.match(view, /new SseTransportError/)
  assert.match(view, /answerSheetStreamController\.abort\(\)/)
  assert.match(view, /api\.answerSheet\.getSession\(session\.value\.session_id\)/)
  assert.match(view, /recognition_checkpoint/)
  assert.match(view, /grading_draft_checkpoint/)
  assert.match(view, /进度会自动保存/)
})

test('三条长链路 SSE 带登录令牌与取消信号 POST 到各自流式端点，并交回原始响应', async (t) => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  const { answerSheet } = client.api
  const streams = recordFetch(t)
  const { signal } = new AbortController()

  const responses = [
    await answerSheet.recognizeSourceStream(SESSION, signal),
    await answerSheet.recognizeStudentsStream(SESSION, signal),
    await answerSheet.gradingDraftStream(SESSION, signal),
  ]

  assert.deepEqual(streams.map(call => call.url), [
    `/api${BASE}/source/recognize/stream`,
    `/api${BASE}/students/recognize/stream`,
    `/api${BASE}/grading/draft/stream`,
  ])
  for (const [index, call] of streams.entries()) {
    assert.equal(call.method, 'POST')
    assert.equal(call.signal, signal)
    assert.equal(call.headers.Authorization, 'Bearer teacher-token')
    assert.equal(responses[index], call.response)
  }
  assert.equal(client.requests.length, 0)
})

test('SSE 加速节点状态区分等待、不可用与故障，并保留已保存进度', () => {
  for (const code of ['ACCELERATOR_WAITING', 'ACCELERATOR_UNAVAILABLE', 'ACCELERATOR_FAULT']) {
    assert.match(view, new RegExp(`${code}:`))
  }
  assert.match(view, /ACCELERATOR_WAITING:[\s\S]*hardwareStatus: 'waiting_accelerator'[\s\S]*serviceStatus: 'queued'[\s\S]*label: '等待加速节点'/)
  assert.match(view, /ACCELERATOR_UNAVAILABLE:[\s\S]*hardwareStatus: 'unavailable'[\s\S]*serviceStatus: 'unavailable'[\s\S]*label: '加速节点不可用'/)
  assert.match(view, /ACCELERATOR_FAULT:[\s\S]*hardwareStatus: 'fault'[\s\S]*serviceStatus: 'fault'[\s\S]*label: '加速节点故障'/)
  assert.match(view, /applyAcceleratorStreamError\(normalizedError\)/)
  assert.match(view, /if \(!acceleratorHandled && isSseServiceFault/)
  assert.match(view, /serviceStatus === 'queued'\) ElMessage\.warning/)
  assert.match(view, /if \(!acceleratorHandled\) notifyProcessingError\(error,/)
  assert.match(view, /已完成进度保留在当前会话/)

  const streamCatch = view.slice(
    view.indexOf('const normalizedError = normalizeServiceError(error)'),
    view.indexOf('return null', view.indexOf('const normalizedError = normalizeServiceError(error)')),
  )
  assert.ok(streamCatch.indexOf('applyAcceleratorStreamError') < streamCatch.indexOf('isSseServiceFault'))
})

test('链路探测不阻塞已保存会话恢复', () => {
  const availabilityIndex = view.indexOf('void refreshAvailability()')
  const restoreIndex = view.indexOf('api.answerSheet.getSession(saved)')
  assert.ok(availabilityIndex >= 0 && availabilityIndex < restoreIndex)
  assert.doesNotMatch(view, /await refreshAvailability\(\).*api\.answerSheet\.getSession\(saved\)/s)
})

test('版面表单支持归一化矩形、题号绑定、身份区域、保存与冻结门禁', () => {
  for (const type of ['choice_mark', 'choice_handwritten', 'fill', 'solve']) assert.match(view, new RegExp(type))
  for (const type of ['name', 'student_no', 'exam_no_text', 'qr', 'barcode', 'exam_no_marks', 'page_number']) assert.match(view, new RegExp(`${type}:`))
  assert.match(view, /min: 0, max: 1000/)
  assert.match(view, /answer_regions: answerRegions\.value, identity_regions: identityRegions\.value/)
  assert.match(view, /:disabled="!layoutSummary\.can_freeze"/)
  assert.match(view, /URL\.revokeObjectURL\(layoutImageUrl\.value\)/)
})

test('学生答题卡阶段调用真实上传、方向、识别、复核、判分与归档接口', () => {
  assert.match(view, /api\.answerSheet\.uploadStudents/)
  assert.match(view, /api\.answerSheet\.confirmOrientation/)
  assert.match(view, /api\.answerSheet\.recognizeStudents/)
  assert.match(view, /api\.answerSheet\.confirmStudentReview/)
  assert.match(view, /api\.answerSheet\.gradingDraft/)
  assert.match(view, /api\.answerSheet\.archive/)
  assert.doesNotMatch(view, /后端接口待开放/)
})

test('学生答题卡的上传、方向、识别、复核、判分与归档接口命中真实端点', async () => {
  const client = await loadApiClient()
  const { answerSheet } = client.api
  const review = { student_id: 7, answers: { 1: 'A', 2: '3/4' } }

  await answerSheet.uploadStudents(SESSION, 'upload/1', studentPage())
  await answerSheet.confirmOrientation(SESSION, 'paper/1', 3)
  await answerSheet.recognizeStudents(SESSION)
  await answerSheet.confirmStudentReview(SESSION, 'paper/1', review)
  await answerSheet.gradingDraft(SESSION)
  await answerSheet.archive(SESSION)

  assert.deepEqual(sent(client.requests), [
    ['post', `${BASE}/students/upload`],
    ['post', `${BASE}/students/paper%2F1/orientation`],
    ['post', `${BASE}/students/recognize`],
    ['post', `${BASE}/students/paper%2F1/review-confirm`],
    ['post', `${BASE}/grading/draft`],
    ['post', `${BASE}/grading/archive`],
  ])
  assert.deepEqual(body(client.requests[1]), { quarter_turns_clockwise: 3 })
  assert.deepEqual(body(client.requests[3]), review)
})

test('学生答题卡按单文件请求持久上传并可恢复处理', () => {
  const viewUploadStart = view.indexOf('async function uploadStudents()')
  const viewUploadEnd = view.indexOf('async function restartStudentUpload()', viewUploadStart)
  assert.ok(viewUploadStart >= 0 && viewUploadEnd > viewUploadStart)
  const viewUpload = view.slice(viewUploadStart, viewUploadEnd)
  assert.match(viewUpload, /for \(const \[index, file\] of files\.entries\(\)\)/)
  assert.match(viewUpload, /api\.answerSheet\.uploadStudents\([\s\S]*durableUploadId,[\s\S]*file,[\s\S]*\)/)
  assert.doesNotMatch(viewUpload, /new FormData|append\('files'/)
  assert.match(viewUpload, /startStudentUpload\(session\.value\.session_id, false\)/)

  assert.match(view, /api\.answerSheet\.processStudentUploadStream/)
  assert.match(view, /runAnswerSheetStream\([\s\S]*processStudentUploadStream/)
  assert.match(view, /startStudentUpload\(session\.value\.session_id, true\)/)
  assert.match(view, /:disabled="(?:busy \|\| )?studentUploadActive/)
  for (const field of ['uploaded_count', 'processed_count', 'total_bytes']) {
    assert.match(view, new RegExp(field))
  }
})

test('学生答题卡上传接口一次请求只带一个文件，持久批次可重开、处理与丢弃', async (t) => {
  const client = await loadApiClient()
  const { answerSheet } = client.api
  const streams = recordFetch(t)
  const { signal } = new AbortController()
  const page = studentPage()

  await answerSheet.startStudentUpload(SESSION)
  await answerSheet.startStudentUpload(SESSION, 1)
  await answerSheet.uploadStudents(SESSION, 'upload/1', page)
  await answerSheet.discardStudentUpload(SESSION, 'upload/1')
  await answerSheet.processStudentUploadStream(SESSION, 'upload/1', signal)

  assert.deepEqual(sent(client.requests), [
    ['post', `${BASE}/students/upload/start`],
    ['post', `${BASE}/students/upload/start`],
    ['post', `${BASE}/students/upload`],
    ['delete', `${BASE}/students/upload/upload%2F1`],
  ])
  const [fresh, restart, upload] = client.requests
  assert.deepEqual(body(fresh), { restart: false })
  assert.deepEqual(body(restart), { restart: true })
  const files = upload.data.getAll('files')
  assert.equal(files.length, 1)
  assert.equal(files[0], page)
  assert.equal(upload.data.get('upload_id'), 'upload/1')
  assert.deepEqual(streams.map(call => [call.method, call.url]), [
    ['POST', `/api${BASE}/students/upload/upload%2F1/process/stream`],
  ])
  assert.equal(streams[0].signal, signal)
})
