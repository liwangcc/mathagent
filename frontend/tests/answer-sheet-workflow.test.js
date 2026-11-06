import test from 'node:test'
import assert from 'node:assert/strict'
import { AxiosError } from 'axios'
import { createRenderer, h } from 'vue'
import {
  body as requestBody,
  chooseFiles,
  flush,
  installImageDecoding,
  liveStreams,
  openAnswerSheet,
  PAPER,
  pickFiles,
  pngFile,
  SESSION_KEY,
  sessionFixture,
  SHEET,
  TEACHER,
  teleportToBody,
} from './helpers/answerSheetPage.js'
import { loadApiClient } from './helpers/apiClient.js'
import { renderedFiles, signedInTeacherRouter } from './helpers/appRouter.js'
import { jsonResponse, sseResponse } from './helpers/memoryFetch.js'
import { MElement, nodeOps, patchProp } from './helpers/memoryDom.js'
import { mountComponent } from './helpers/mountComponent.js'

// Ids that need encodeURIComponent, so every path segment proves it is encoded.
const SESSION = 's 1/x'
const BASE = '/answer-sheet/sessions/s%201%2Fx'
const SHEET_CODE = 'AS-1111-2222-3333-4444'
const body = config => (typeof config.data === 'string' ? JSON.parse(config.data) : config.data)
const sent = requests => requests.map(config => [config.method, config.url])
const studentPage = () => new File(['front'], '张三_正面.jpg', { type: 'image/jpeg' })
const S1 = '/answer-sheet/sessions/s1'

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

const image = (name, type = 'image/jpeg') => new File([name], name, { type })
const hasDisabled = el => el.hasAttribute('disabled')
const option = el => ({ label: el.getAttribute('label'), value: el._value })

// A layout draft with one choice, one fill and one solve question on two pages.
function layoutDraft(overrides = {}) {
  return sessionFixture({
    status: 'layout_draft',
    answer_spec_snapshot: [
      { question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: 'A' },
      { question_no: '2', question_type: 'fill', standard_answer: '3' },
      { question_no: '3', question_type: 'solve', standard_answer: '略' },
    ],
    answer_sheet_pages: [{ page_id: 'p1', index: 0 }, { page_id: 'p2', index: 1 }],
    answer_regions: [
      { region_id: 'a1', page_id: 'p1', question_no: '1', region_type: 'choice_mark', bbox: [100, 100, 300, 200], teacher_confirmed: false },
      { region_id: 'a2', page_id: 'p1', question_no: '2', region_type: 'fill', bbox: [100, 300, 300, 400], teacher_confirmed: false },
      { region_id: 'a3', page_id: 'p2', question_no: '3', region_type: 'solve', bbox: [100, 500, 900, 800], teacher_confirmed: false },
    ],
    identity_regions: [
      { region_id: 'i1', page_id: 'p1', identity_type: 'name', bbox: [50, 30, 450, 90], teacher_confirmed: false, config: {} },
    ],
    ...overrides,
  })
}

test('答题卡批阅路由在进入时才懒加载自己的页面，标题为答题卡批阅', async () => {
  const router = await signedInTeacherRouter()
  const record = router.resolve('/answer-sheet').matched.at(-1)
  assert.equal(typeof record.components.default, 'function', '进入前只有加载函数')
  await router.push('/answer-sheet')
  const route = router.currentRoute.value
  assert.equal(route.name, 'answer-sheet')
  assert.equal(route.meta.title, '答题卡批阅')
  assert.deepEqual(renderedFiles(route), ['views/Layout.vue', 'views/AnswerSheet.vue'])
})

// The path data an icon component draws, rendered on its own.
function iconPaths(component) {
  const root = new MElement('div')
  createRenderer({ ...nodeOps, patchProp }).createApp({ render: () => h(component) }).mount(root)
  return root.findAll(el => el.tagName === 'path').map(el => el.getAttribute('d'))
}

test('答题卡批阅菜单位于详细批改与班级管理之间并使用票据图标', async (t) => {
  await loadApiClient({ localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER) } })
  teleportToBody(t)
  const { default: Layout } = await import('../src/views/Layout.vue?sfc')
  const { Tickets, Upload } = await import('@element-plus/icons-vue')
  const page = await mountComponent(t, { render: () => null }, { path: '/answer-sheet', layout: Layout })

  const items = page.root.byTag('el-menu-item')
  const indexes = items.map(el => el.getAttribute('index'))
  const uploadIndex = indexes.indexOf('/upload')
  const answerSheetIndex = indexes.indexOf('/answer-sheet')
  const studentsIndex = indexes.indexOf('/students')
  assert.ok(uploadIndex >= 0 && uploadIndex < answerSheetIndex && answerSheetIndex < studentsIndex, indexes.join(' '))

  const entry = items[answerSheetIndex]
  assert.equal(entry.byTag('el-slot').find(slot => slot.getAttribute('name') === 'title').text, '答题卡批阅')
  const drawn = entry.byTag('path').map(el => el.getAttribute('d'))
  assert.deepEqual(drawn, iconPaths(Tickets))
  assert.notDeepEqual(drawn, iconPaths(Upload))
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

test('五阶段工作流持久化 session id 并从 GET 会话刷新恢复', async (t) => {
  const sheet = await openAnswerSheet(t, {
    session: layoutDraft({ session_id: 's 1' }),
    batches: [{ session_id: 's 1', batch_label: '', paper_template_code: '', status: 'layout_draft' }],
  })

  assert.equal(sheet.count('get', '/answer-sheet/sessions/s%201'), 1, sheet.requests().join(' | '))
  assert.ok(sheet.alerts().includes('已恢复上次未完成的答题卡会话。'))
  assert.equal(sheet.root.byClass('session-meta').byText('span', '会话').text, '会话 s 1')
  assert.deepEqual(sheet.stageButtons().map(el => el.byTag('strong')[0].text), ['原始试卷', '教师确认', '空白答题卡', '绑定冻结', '学生答题卡'])
  for (const heading of ['原始试卷与答案来源', '教师确认题目与答案', '选择答题卡模板', '版面绑定与冻结', '学生答题卡与批阅']) {
    assert.ok(sheet.section(heading), heading)
  }
  assert.ok(sheet.button('上传空白答题卡'), '确认答案后进入空白答题卡阶段的入口')
  // A layout draft continues at the binding stage; later stages stay closed.
  assert.equal(sheet.activeStage(), 3)
  assert.deepEqual(sheet.stageButtons().map(hasDisabled), [false, false, false, false, true])

  // Leaving the batch forgets the stored id; opening it again stores it.
  await sheet.click('批次列表')
  assert.equal(sheet.client.localStorage.getItem(SESSION_KEY), null)
  await sheet.root.byClass('batch-row').dispatch('click')
  await flush()
  assert.equal(sheet.count('get', '/answer-sheet/sessions/s%201'), 2)
  assert.equal(sheet.client.localStorage.getItem(SESSION_KEY), 's 1')
})

test('打开的会话写入本地存储，存储的会话打不开时清除并提示', async (t) => {
  // Opened from a link (?session=), nothing stored yet: applying it stores it.
  const sheet = await openAnswerSheet(t, { session: layoutDraft({ session_id: 'q 2' }), saved: null, path: '/answer-sheet?session=q%202' })
  assert.equal(sheet.count('get', '/answer-sheet/sessions/q%202'), 1)
  assert.equal(sheet.client.localStorage.getItem(SESSION_KEY), 'q 2')
  sheet.close()

  // A saved id that no longer opens is dropped, and the page says so.
  const gone = await openAnswerSheet(t, { saved: 'gone' })
  assert.equal(gone.count('get', '/answer-sheet/sessions/gone'), 1)
  assert.ok(gone.alerts().includes('答题卡会话无法恢复，可能已删除或不属于当前账号。'))
  assert.equal(gone.client.localStorage.getItem(SESSION_KEY), null)
  assert.ok(gone.root.byClass('create-card'))
})

test('每个会话状态从对应阶段继续，之后的阶段不能提前打开', async (t) => {
  const expected = { draft: 0, failed: 0, source_uploaded: 0, source_recognized: 1, answers_confirmed: 2, layout_draft: 3, frozen: 4, recognizing: 4, review: 4, archived: 4 }
  for (const [status, stage] of Object.entries(expected)) {
    const sheet = await openAnswerSheet(t, { session: sessionFixture({ status }) })
    assert.equal(sheet.activeStage(), stage, status)
    assert.deepEqual(sheet.stageButtons().map(hasDisabled), [0, 1, 2, 3, 4].map(index => index > stage), status)
    sheet.close()
  }
})

test('前端明确分离三层材料并保留三种答案来源', async (t) => {
  const sheet = await openAnswerSheet(t, {
    session: sessionFixture({ status: 'draft' }),
    respond: config => (config.method === 'post' && config.url === `${S1}/source/upload`
      ? { data: sessionFixture({ status: 'source_uploaded', answer_source: 'separate', source_exam_pages: [{ page_id: 'e1' }, { page_id: 'e2' }] }) }
      : undefined),
  })
  assert.equal(sheet.root.byClass('page-heading').byTag('p')[0].text, '原始试卷提供题目语义，空白答题卡提供版面，学生答题卡才是批阅对象。')

  const sources = sheet.root.byTag('el-radio-button')
  assert.deepEqual(sources.map(el => [el.value, el.text]), [
    ['embedded', '试卷内含答案'],
    ['separate', '另传答案文件'],
    ['ai_generated', 'AI 生成候选'],
  ])
  assert.ok(!sheet.picker('选择答案文件图片'), '默认答案在试卷内，不要求答案文件')
  sheet.root.byTag('el-radio-group')[0].handlers['update:modelValue']('separate')
  await flush()
  assert.ok(sheet.picker('选择答案文件图片'))

  const exam = [image('试卷1.jpg'), image('试卷2.jpg')]
  const answers = [image('答案.jpg')]
  await pickFiles(sheet, '选择原始试卷图片', exam)
  await pickFiles(sheet, '选择答案文件图片', answers)
  await sheet.click('上传材料')

  const upload = sheet.request('post', `${S1}/source/upload`)
  assert.ok(upload, sheet.writes().join(' | '))
  // deepEqual sees every File as equal: compare the files themselves.
  assert.deepEqual(upload.data.getAll('source_files').map(item => item.name), ['试卷1.jpg', '试卷2.jpg'])
  assert.ok(upload.data.getAll('source_files').every((item, index) => item === exam[index]))
  assert.deepEqual(upload.data.getAll('answer_files').map(item => item.name), ['答案.jpg'])
  assert.equal(upload.data.get('answer_files'), answers[0])
  assert.equal(upload.data.get('answer_source'), 'separate')
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '材料已安全保存'))
  assert.equal(sheet.root.byText('div', '已保存 2 页原始试卷').text, '已保存 2 页原始试卷，答案来源：另传答案文件')
})

test('答题卡可选复用模板码，新建模板仍兼容外部版式', async (t) => {
  const sheet = await openAnswerSheet(t, { session: sessionFixture({ status: 'answers_confirmed' }) })
  assert.equal(sheet.activeStage(), 2)
  const stage = sheet.section('选择答题卡模板')
  assert.match(stage.text, /可直接复用共享模板码，也可上传新的空白答题卡/)
  assert.match(stage.text, /或首次建立模板/)
  const picker = stage.find(el => el.classList.contains('file-picker'))
  assert.equal(picker.byTag('strong')[0].text, '选择空白答题卡图片')
  assert.match(picker.byTag('span')[0].text, /^支持自制及其他平台版式；上传后自动粗定位，按页面顺序选择/)
})

test('模板码输入框显示当前教师历史并且复用路径不再定位', async (t) => {
  const history = [{ template_code: SHEET, page_count: 2, question_count: 5, question_counts: { choice: 3, fill: 1, solve: 1 } }]
  const applied = layoutDraft({
    shared_template_code: SHEET,
    layout_localization: { provider_route: 'qwen3.8max' },
    answer_sheet_pages: [{ page_id: 'p1', index: 0 }, { page_id: 'p2', index: 1 }],
    answer_regions: [
      { region_id: 'a1', page_id: 'p1', question_no: '1', region_type: 'choice_mark', bbox: [100, 100, 300, 200], teacher_confirmed: false },
      { region_id: 'a3', page_id: 'p2', question_no: '3', region_type: 'solve', bbox: [100, 500, 900, 800], teacher_confirmed: false },
    ],
  })
  const sheet = await openAnswerSheet(t, {
    session: sessionFixture({ status: 'answers_confirmed' }),
    history,
    respond: (config) => {
      if (config.method === 'post' && config.url === `${S1}/layout/templates/use`) return { data: { session: applied, template: { template_code: SHEET } } }
      if (config.method === 'put' && config.url === `${S1}/layout`) return { data: { session: applied, summary: {} } }
      return undefined
    },
  })
  assert.equal(sheet.count('get', '/answer-sheet/templates/history'), 1)
  const stage = sheet.section('选择答题卡模板')
  const select = stage.byTag('el-select')[0]
  assert.equal(select.getAttribute('placeholder'), '输入模板码，或从历史记录中选择')
  assert.deepEqual(select.byTag('el-option').map(option), [{ label: `${SHEET}　2页 / 5题　选3 · 填1 · 解1`, value: SHEET }])

  assert.ok(hasDisabled(sheet.button('直接套用')), '没有模板码时不能套用')
  select.handlers['update:modelValue'](` ${SHEET.toLowerCase()} `)
  await flush()
  await sheet.click('直接套用')

  assert.deepEqual(requestBody(sheet.request('post', `${S1}/layout/templates/use`)), { template_code: SHEET })
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '模板码已套用，无需模型重新定位'))
  assert.equal(sheet.count('post', `${S1}/layout/locate`), 0, '套用模板码不调用模型粗定位')
  assert.equal(sheet.count('get', '/answer-sheet/templates/history'), 2, '套用后刷新历史')
  assert.equal(sheet.activeStage(), 3)

  // The qwen3.8max route already localized the page: no re-locate, the teacher
  // confirms it one page at a time (forms: a1 on p1, a3 on p2, then i1 on p1).
  assert.ok(!sheet.button('重新粗定位'))
  const confirmations = () => sheet.root.findAll(el => el.tagName === 'el-checkbox' && el.text === '教师已确认')
  assert.deepEqual(confirmations().map(el => el.hasAttribute('modelValue')), [false, false, false])
  await sheet.click('确认本页定位')
  assert.deepEqual(confirmations().map(el => el.hasAttribute('modelValue')), [true, false, true], '只确认第 1 页的区域')
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '本页定位已确认，保存版面后即可冻结'))
  assert.ok(!sheet.button('确认本页定位'), '本页已无待确认区域')
  await sheet.click('第 2 页')
  assert.ok(sheet.button('确认本页定位'), '第 2 页的区域仍待确认')
  await sheet.click('保存版面')
  const saved = requestBody(sheet.request('put', `${S1}/layout`))
  assert.deepEqual([...saved.answer_regions, ...saved.identity_regions].map(region => [region.region_id, region.page_id, region.teacher_confirmed]), [
    ['a1', 'p1', true],
    ['a3', 'p2', false],
    ['i1', 'p1', true],
  ])
  sheet.close()

  // A layout localized another way can be re-located, and has nothing to confirm in bulk.
  const manual = await openAnswerSheet(t, { session: layoutDraft({ layout_localization: { provider_route: 'manual' } }) })
  assert.ok(manual.button('重新粗定位'))
  assert.ok(!manual.button('确认本页定位'))
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

test('模板页和学生答题卡使用各自的文件数量上限', async (t) => {
  installImageDecoding(t)
  const pages = (count, prefix) => Array.from({ length: count }, (_, index) => pngFile(`${prefix}-${String(index + 1).padStart(3, '0')}.png`))
  const sheet = await openAnswerSheet(t, { session: sessionFixture({ status: 'draft', answer_source: 'separate' }) })
  for (const title of ['选择原始试卷图片', '选择答案文件图片', '选择空白答题卡图片']) {
    sheet.client.messages.length = 0
    const picker = sheet.picker(title)
    await chooseFiles(picker.byTag('input')[0], pages(25, title))
    // Only the first 20 go on to preprocessing; the list check never sees more.
    assert.deepEqual(sheet.messages(), [['warning', '单次最多选择 20 个文件']], title)
    assert.equal(picker.byTag('strong')[0].text, '已选择 20 个文件', title)
  }
  sheet.close()

  const students = await openAnswerSheet(t, { session: sessionFixture({ status: 'frozen' }) })
  const picker = students.picker('导入学生答题卡')
  await chooseFiles(picker.byTag('input')[0], pages(200, 'student'))
  assert.deepEqual(students.messages(), [])
  assert.equal(picker.byTag('strong')[0].text, '已选择 200 个文件')
  await chooseFiles(picker.byTag('input')[0], pages(201, 'student'))
  assert.equal(students.messages().length, 1)
  const [[type, message]] = students.messages()
  assert.equal(type, 'error')
  assert.match(message, /本次最多 200 张图片/)
})

test('答题卡页面失败关闭整链路状态并在故障时保留已有证据查看', async (t) => {
  const cases = [
    [() => new Promise(() => {}), '检测中', 'info', true],
    [{ status: 'available' }, '可用', 'success', false],
    [{ status: 'queued' }, '需排队', 'warning', false],
    [{ status: 'unavailable' }, '暂不可用', 'danger', true],
    [{ status: 'fault' }, '链路故障', 'danger', true],
  ]
  for (const [availability, label, type, blocks] of cases) {
    const sheet = await openAnswerSheet(t, {
      availability,
      paperTemplates: [{ template_code: PAPER, question_count: 4 }],
      paperSources: [{ template_id: 'tpl-1', exam_name: '期中', question_count: 4 }],
    })
    assert.equal(sheet.serviceTag().text, label)
    assert.equal(sheet.serviceTag().getAttribute('type'), type, label)
    assert.equal(Boolean(sheet.serviceNotice()), label !== '可用', label)
    sheet.root.byTag('el-select')[0].handlers['update:modelValue'](PAPER)
    // Nothing chosen to register yet: the registration button is closed whatever the chain says.
    assert.equal(hasDisabled(sheet.button('登记试卷模板')), true, label)
    sheet.root.byClass('template-code-actions').byTag('el-select')[0].handlers['update:modelValue']('tpl-1')
    await flush()
    assert.equal(hasDisabled(sheet.button('创建批次并继续')), blocks, label)
    assert.equal(hasDisabled(sheet.button('登记试卷模板')), blocks, label)
    sheet.close()
  }

  // A blocked chain leaves an open draft read-only and undeletable; an archive stays browsable.
  const draft = sessionFixture({ status: 'draft' })
  for (const [availability, status, readonly, deletable] of [
    [{ status: 'fault' }, 'draft', true, false],
    [{ status: 'available' }, 'draft', false, true],
    [{ status: 'available' }, 'source_uploaded', false, false],
    [{ status: 'fault' }, 'archived', false, false],
  ]) {
    const sheet = await openAnswerSheet(t, { availability, session: { ...draft, status } })
    const pageRoot = sheet.root.byClass('answer-sheet-page')
    assert.equal(pageRoot.classList.contains('workflow-readonly'), readonly, `${availability.status} ${status}`)
    assert.equal(hasDisabled(sheet.button('删除草稿')), !deletable, `${availability.status} ${status}`)
    sheet.close()
  }
})

test('答题卡可用性每 15 秒重新探测，页面只显示状态与文案而不显示排队内部数据', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  const internals = { active_teachers: 5, queue_length: 3, free_slots: 0, queue_position: 2 }
  const sheet = await openAnswerSheet(t, {
    availability: { status: 'queued', ...internals, hardware: { status: 'waiting_accelerator', message: '排队等待加速节点', ...internals } },
  })
  const probes = () => sheet.count('get', '/answer-sheet/availability')
  assert.equal(probes(), 1)
  t.mock.timers.tick(14_999)
  await flush()
  assert.equal(probes(), 1)
  t.mock.timers.tick(1)
  await flush()
  assert.equal(probes(), 2)
  t.mock.timers.tick(15_000)
  await flush()
  assert.equal(probes(), 3)

  assert.equal(sheet.serviceNotice().getAttribute('title'), '排队等待加速节点')
  assert.doesNotMatch(sheet.page.html(), /active_teachers|queue_length|free_slots|queue_position/)
  sheet.close()
  t.mock.timers.tick(30_000)
  await flush()
  assert.equal(probes(), 3, '离开页面后停止探测')
})

test('答题卡页面故障只读时仍渲染学生页签与完整答题卡，切换学生与翻页照常生效', async (t) => {
  const draft = (studentId, name, paperIds) => ({
    student_id: studentId,
    paper_ids: paperIds,
    result: { student_name: name, needs_review_count: 0, details: [{ question_no: '1', result: 'correct', paper_ids: [paperIds[0]] }] },
  })
  const sheet = await openAnswerSheet(t, {
    availability: { status: 'fault' },
    session: sessionFixture({
      status: 'review',
      review_revision: 3,
      student_pages: [
        { paper_id: 'p1', filename: '张三-1.jpg', orientation_status: 'auto_passed' },
        { paper_id: 'p2', filename: '张三-2.jpg', orientation_status: 'auto_passed' },
        { paper_id: 'p3', filename: '李四-1.jpg', orientation_status: 'auto_passed' },
      ],
      grouping_decisions: [draft(7, '张三', ['p1', 'p2']), draft(8, '李四', ['p3'])],
    }),
  })
  assert.equal(sheet.serviceTag().text, '链路故障')
  assert.ok(sheet.root.byClass('answer-sheet-page').classList.contains('workflow-readonly'))

  const tabs = () => sheet.root.byClass('review-student-tabs').byTag('button')
  const paperPane = () => sheet.root.find(el => el.tagName === 'section' && el.getAttribute('aria-label') === '完整学生答题卡')
  const pagerButton = name => paperPane().find(el => el.getAttribute('aria-label') === name)
  const pageCount = () => paperPane().byClass('review-pager').byTag('span')[0].text
  assert.deepEqual(tabs().map(el => [el.text, el.classList.contains('active')]), [['张三', true], ['李四', false]])
  assert.ok(paperPane())
  assert.equal(sheet.count('get', `${S1}/students/p1/image`), 1)

  // Paging through the current student's sheet still works.
  assert.equal(pagerButton('下一页').hasAttribute('disabled'), false)
  await pagerButton('下一页').dispatch('click')
  await flush()
  assert.equal(pageCount(), '2 / 2')
  assert.equal(sheet.count('get', `${S1}/students/p2/image`), 1)

  // So does switching to another student.
  await tabs()[1].dispatch('click')
  await flush()
  assert.deepEqual(tabs().map(el => el.classList.contains('active')), [false, true])
  assert.equal(sheet.count('get', `${S1}/students/p3/image`), 1)
  assert.equal(paperPane().byClass('review-pane-title').byTag('span')[0].text, '李四')
  assert.equal(pageCount(), '1 / 1')
  assert.deepEqual(sheet.writes(), [], '查看不发写请求')
  assert.deepEqual(sheet.messages().filter(([type]) => type === 'error'), [], '查看不被链路门禁拦截')
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

test('答题卡可用性轮询单飞并保持运行期链路故障，未知状态也失败关闭', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: 0 })
  let release
  const replies = [new Promise((resolve) => { release = resolve })]
  const sheet = await openAnswerSheet(t, { availability: () => replies.shift() ?? { status: 'available' } })
  const probes = () => sheet.count('get', '/answer-sheet/availability')
  const poll = async () => { t.mock.timers.tick(15_000); await flush() }
  const state = () => [sheet.serviceTag().text, sheet.serviceNotice()?.getAttribute('title') ?? '']

  // A poll while the previous probe is still pending sends nothing.
  assert.equal(probes(), 1)
  await poll()
  assert.equal(probes(), 1)
  release({ status: 'available' })
  await flush()
  assert.equal(sheet.serviceTag().text, '可用')
  await poll()
  assert.equal(probes(), 2)

  // A fault holds for 60 s against probes that report the chain available or
  // queued again.
  replies.push({ status: 'fault' })
  await poll() // t = 45 s
  assert.deepEqual(state(), ['链路故障', '答题卡批阅链路发生故障，请稍后重试或联系管理员。'])
  for (const [seconds, reply] of [[60, 'available'], [75, 'queued'], [90, 'available']]) {
    replies.push({ status: reply })
    await poll()
    assert.equal(sheet.serviceTag().text, '链路故障', `${seconds} s: ${reply}`)
  }
  await poll() // t = 105 s: 60 s after the fault, the hold is over
  assert.deepEqual(state(), ['可用', ''])
  assert.equal(probes(), 7)

  // An unknown status fails closed.
  replies.push({ status: 'weird' })
  await poll()
  assert.deepEqual(state(), ['链路故障', '答题卡批阅链路状态响应异常，请稍后重试或联系管理员。'])
  sheet.close()

  // So does a faulty accelerator behind an available service, and a probe that fails.
  for (const [availability, message] of [
    [{ status: 'available', hardware: { status: 'fault', message: '加速节点自检失败' } }, '加速节点自检失败'],
    [() => Promise.reject(new Error('offline')), '无法获取答题卡批阅链路状态，请检查网络或联系管理员。'],
  ]) {
    const probe = await openAnswerSheet(t, {
      availability,
      respond: config => (config.url === '/answer-sheet/availability' && typeof availability === 'function'
        ? { status: 503, data: { detail: '服务不可达' } }
        : undefined),
    })
    assert.deepEqual([probe.serviceTag().text, probe.serviceNotice().getAttribute('title')], ['链路故障', message])
    probe.close()
  }
})

test('答题卡页面消费机会式硬件状态，等待节点不锁死恢复，故障节点关闭新图像工作', async (t) => {
  const paperTemplates = [{ template_code: PAPER, question_count: 4 }]
  for (const [availability, label, message, blocks] of [
    // Waiting for an accelerator queues the work; it does not block it, and
    // the accelerator's own message explains the state it caused.
    [{ status: 'available', message: '服务可用', hardware: { status: 'waiting_accelerator', message: '等待教师客户端加速' } }, '需排队', '等待教师客户端加速', false],
    [{ status: 'available', hardware: { status: 'waiting_accelerator' } }, '需排队', '等待教师客户端加速节点，已保存进度不会丢失。', false],
    // An unavailable or faulty accelerator stops new image work.
    [{ status: 'available', hardware: { status: 'unavailable' } }, '暂不可用', '本地加速节点不可用，已停止新的图像处理任务；已保存进度仍可查看。', true],
    [{ status: 'queued', hardware: { status: 'fault', message: '加速节点离线' } }, '链路故障', '加速节点离线', true],
    // When the service itself is the worse state, its own message and label lead.
    [{ status: 'unavailable', label: '维护中', message: '服务维护中', hardware: { status: 'waiting_accelerator', message: '等待节点' } }, '维护中', '服务维护中', true],
    [{ status: 'queued', message: '排队中', hardware: { status: 'ready', message: '' } }, '需排队', '排队中', false],
  ]) {
    const sheet = await openAnswerSheet(t, { availability, paperTemplates })
    assert.deepEqual([sheet.serviceTag().text, sheet.serviceNotice().getAttribute('title')], [label, message], JSON.stringify(availability))
    sheet.root.byTag('el-select')[0].handlers['update:modelValue'](PAPER)
    await flush()
    assert.equal(hasDisabled(sheet.button('创建批次并继续')), blocks, JSON.stringify(availability))
    sheet.close()
  }

  // A hardware fault the page learnt of blocks new work even while the service state reads available.
  const sheet = await openAnswerSheet(t, { paperTemplates })
  sheet.root.byTag('el-select')[0].handlers['update:modelValue'](PAPER)
  await flush()
  assert.equal(hasDisabled(sheet.button('创建批次并继续')), false)
  sheet.setup.hardwareState = { status: 'fault', message: '加速节点故障' }
  await flush()
  assert.equal(sheet.serviceTag().text, '可用')
  assert.equal(hasDisabled(sheet.button('创建批次并继续')), true)
  await sheet.setup.createSession()
  assert.deepEqual(sheet.writes(), [])
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

test('答题卡新任务有函数级硬件门禁，不仅依赖鼠标样式', async (t) => {
  const sheet = await openAnswerSheet(t, {
    availability: { status: 'fault', message: '整链路故障' },
    session: sessionFixture({
      status: 'frozen',
      source_exam_pages: [{ page_id: 'e1' }],
      student_pages: [{ paper_id: 'p1', filename: '张三.jpg', orientation_status: 'pending' }],
      student_upload: { upload_id: 'u1', status: 'uploading', uploaded_count: 1, processed_count: 0, total_bytes: 10 },
    }),
  })
  assert.equal(sheet.serviceTag().text, '链路故障')
  const refusals = () => sheet.messages().filter(([type, message]) => type === 'error' && message === '整链路故障').length

  // Buttons the page leaves clickable while the chain is down.
  let expected = 0
  for (const text of ['处理已上传页', '丢弃本批并重新开始', '90°']) {
    await sheet.click(text)
    assert.equal(refusals(), ++expected, text)
  }
  await pickFiles(sheet, '导入学生答题卡', [image('张三_正面.jpg')])
  await sheet.click('继续上传并处理')
  assert.equal(refusals(), ++expected, '继续上传并处理')

  // Entry points whose buttons are disabled or hidden refuse when called anyway.
  sheet.section('选择答题卡模板').byTag('el-select')[0].handlers['update:modelValue'](SHEET)
  await flush()
  for (const action of ['createSession', 'uploadSource', 'uploadLayout', 'recognizeSource', 'recognizeStudents', 'buildDraft', 'useTemplateCode']) {
    await sheet.setup[action]()
    await flush()
    assert.equal(refusals(), ++expected, action)
  }

  assert.deepEqual(sheet.writes(), [])
  assert.deepEqual(sheet.streams(), [])
  assert.deepEqual(sheet.page.boxes, [], '门禁先于确认框')
})

const uploadedSource = (checkpoint = true) => sessionFixture({
  status: 'source_uploaded',
  source_exam_pages: [{ page_id: 'e1', ...(checkpoint ? { recognition_checkpoint: { completed_pages: 1 } } : {}) }],
})
const recognizedSource = sessionFixture({
  status: 'source_recognized',
  source_exam_pages: [{ page_id: 'e1' }],
  source_question_drafts: [{ question_no: '1', question_type: 'choice', standard_answer: 'A' }],
})

test('答题卡写操作把网络、5xx 与长请求超时升级为链路故障', async (t) => {
  const failures = [
    ['502', () => ({ status: 502, data: { detail: '网关错误' } }), true],
    ['500', () => ({ status: 500, data: { detail: '服务器内部错误' } }), true],
    ['network', config => { throw new AxiosError('Network Error', 'ERR_NETWORK', config) }, true],
    ['timeout', config => { throw new AxiosError('timeout of 660000ms exceeded', 'ECONNABORTED', config) }, true],
    ['etimedout', config => { throw new AxiosError('connect ETIMEDOUT', 'ETIMEDOUT', config) }, true],
    ['400', () => ({ status: 400, data: { detail: '缺少原始试卷' } }), false],
  ]
  for (const [name, failure, fault] of failures) {
    const sheet = await openAnswerSheet(t, {
      session: sessionFixture({ status: 'draft' }),
      respond: config => (config.method === 'post' && config.url === `${S1}/source/upload` ? failure(config) : undefined),
      allowErrors: true,
    })
    await pickFiles(sheet, '选择原始试卷图片', [image('试卷.jpg')])
    const { done } = await sheet.press('上传材料')
    await assert.rejects(done)
    await flush()
    assert.equal(sheet.count('post', `${S1}/source/upload`), 1, name)
    if (fault) {
      assert.deepEqual(
        [sheet.serviceTag().text, sheet.serviceNotice().getAttribute('title')],
        ['链路故障', '答题卡批阅链路中断，已保存内容仍可查看；请稍后重试，若持续发生请联系管理员。'],
        name,
      )
      assert.ok(sheet.root.byClass('answer-sheet-page').classList.contains('workflow-readonly'), name)
    } else {
      assert.equal(sheet.serviceTag().text, '可用', name)
    }
    sheet.close()
  }

  // A long-chain stream that answers with an HTTP error status carries that
  // status on the error itself (there is no axios response): 5xx, 408 and 429
  // fault the whole chain, other 4xx do not.
  for (const [status, detail, fault] of [
    [502, '网关错误', true],
    [500, '服务器内部错误', true],
    [408, '请求超时', true],
    [429, '请求过多', true],
    [400, '缺少原始试卷', false],
  ]) {
    const sheet = await openAnswerSheet(t, {
      session: uploadedSource(),
      stream: () => jsonResponse({ detail }, { status }),
    })
    await sheet.click('继续识别题目')
    assert.equal(sheet.count('get', S1), 2, `${status}: 读取已保存进度`)
    const readonly = sheet.root.byClass('answer-sheet-page').classList.contains('workflow-readonly')
    if (fault) {
      assert.deepEqual(
        [sheet.serviceTag().text, sheet.serviceNotice().getAttribute('title')],
        ['链路故障', '答题卡批阅链路中断，已完成进度已保存；请稍后重试，若持续发生请联系管理员。'],
        `stream ${status}`,
      )
      assert.equal(readonly, true, `stream ${status}`)
    } else {
      assert.equal(sheet.serviceTag().text, '可用', `stream ${status}`)
      assert.equal(readonly, false, `stream ${status}`)
    }
    sheet.close()
  }
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

test('三条长链路使用有心跳的 SSE 并可从持久化检查点恢复', async (t) => {
  const live = liveStreams()
  const sheet = await openAnswerSheet(t, { session: uploadedSource(), stream: request => live.open(request) })
  assert.ok(!sheet.button('识别题目'))
  const { done } = await sheet.press('继续识别题目')

  const [request] = sheet.backend.requests
  assert.equal(request.url, '/api/answer-sheet/sessions/s1/source/recognize/stream')
  assert.equal(request.init.method, 'POST')
  assert.equal(request.init.headers.Authorization, 'Bearer teacher-token')
  assert.ok(request.init.signal instanceof AbortSignal)
  const veil = () => sheet.root.byClass('busy-veil')
  assert.match(veil().text, /正在识别原始试卷/)
  assert.match(veil().text, /进度会自动保存，中断后可在本批次继续/)

  await live.last.send('progress', { current: 1, total: 3, message: '正在识别第 1 页' })
  const meter = veil().find(el => el.getAttribute('role') === 'progressbar')
  assert.equal(meter.getAttribute('aria-valuenow'), '1')
  assert.equal(meter.getAttribute('aria-valuemax'), '3')
  assert.match(veil().text, /正在识别第 1 页/)

  await live.last.send('done', { session: recognizedSource })
  await done
  await flush()
  assert.ok(!veil())
  assert.equal(sheet.activeStage(), 1)
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '识别完成，请教师检查'))
  assert.equal(sheet.section('教师确认题目与答案').allByClass('question-card').length, 1)
  sheet.close()

  // Without a checkpoint the button starts afresh.
  const fresh = await openAnswerSheet(t, { session: uploadedSource(false) })
  assert.ok(fresh.button('识别题目'))
  assert.ok(!fresh.button('继续识别题目'))
})

test('长链路连接结束却没有完成事件时重新读取会话并报链路故障', async (t) => {
  const live = liveStreams()
  const sheet = await openAnswerSheet(t, { session: uploadedSource(), stream: request => live.open(request) })
  const reads = () => sheet.count('get', S1)
  assert.equal(reads(), 1)
  const { done } = await sheet.press('继续识别题目')
  await live.last.send('progress', { current: 1, total: 3 })
  await live.last.end()
  assert.equal(await done, undefined, '失败已在页面内处理')
  await flush()

  assert.equal(reads(), 2, '失败后读取服务器上已保存的进度')
  assert.deepEqual(
    [sheet.serviceTag().text, sheet.serviceNotice().getAttribute('title')],
    ['链路故障', '答题卡批阅链路中断，已完成进度已保存；请稍后重试，若持续发生请联系管理员。'],
  )
  assert.ok(sheet.messages().some(([type, message]) => type === 'error' && /未收到完成状态/.test(message)), JSON.stringify(sheet.messages()))
  assert.ok(!sheet.root.byClass('busy-veil'))
})

test('离开答题卡页面时取消进行中的长链路', async (t) => {
  const live = liveStreams()
  const sheet = await openAnswerSheet(t, { session: uploadedSource(), stream: request => live.open(request) })
  const { done } = await sheet.press('继续识别题目')
  const { signal } = sheet.backend.requests[0].init
  assert.equal(signal.aborted, false)
  sheet.close()
  assert.equal(signal.aborted, true)
  assert.equal(await done, undefined)
  await flush()
  assert.equal(sheet.count('get', S1), 1, '取消不是失败，不重新读取会话')
  assert.deepEqual(sheet.messages().filter(([type]) => type === 'error'), [])
})

test('学生识别与判分草稿从检查点继续，走各自的流式端点', async (t) => {
  const drafts = [{ student_id: 7, paper_ids: ['p1'], result: { student_name: '张三', needs_review_count: 0, details: [] } }]
  const review = sessionFixture({
    status: 'review',
    student_pages: [{ paper_id: 'p1', filename: '张三.jpg', orientation_status: 'auto_passed', recognition_checkpoint: { done: 1 } }],
    asset_lifecycle: { grading_draft_checkpoint: { drafts: [{ student_id: 7 }] } },
  })
  const sheet = await openAnswerSheet(t, {
    session: review,
    stream: request => sseResponse(request.url.endsWith('/students/recognize/stream')
      ? [['done', { session: review }]]
      : [['progress', { current: 1, total: 1 }], ['done', { drafts, review_revision: 2 }]]),
  })
  await sheet.click('继续识别')
  await sheet.click('继续生成批阅草稿')
  assert.deepEqual(sheet.streams(), [
    ['POST', '/api/answer-sheet/sessions/s1/students/recognize/stream'],
    ['POST', '/api/answer-sheet/sessions/s1/grading/draft/stream'],
  ])
  assert.ok(sheet.root.byClass('review-workbench'))
  // The finished draft retires its checkpoint: going back offers a fresh draft.
  await sheet.click('返回答案复核')
  assert.ok(sheet.button('生成统一批阅草稿'))
  assert.ok(!sheet.button('继续生成批阅草稿'))
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

test('SSE 加速节点状态区分等待、不可用与故障，并保留已保存进度', async (t) => {
  const kept = '已完成进度保留在当前会话，可在节点恢复后继续处理。'
  const cases = [
    ['waiting', sseResponse([['error', { code: 'ACCELERATOR_WAITING', message: '节点排队中' }]]),
      '等待加速节点', 'warning', `节点排队中 ${kept}`, 'warning', false],
    ['unavailable', sseResponse([['error', { code: 'ACCELERATOR_UNAVAILABLE', message: '加速节点离线' }]]),
      '加速节点不可用', 'danger', `加速节点离线 ${kept}`, 'error', true],
    ['fault', sseResponse([['error', { code: 'accelerator_fault', message: '处理失败，进度已保存' }]]),
      '加速节点故障', 'danger', '处理失败，进度已保存', 'error', true],
    ['fault without message', sseResponse([['error', { code: 'ACCELERATOR_FAULT', detail: ' ' }]]),
      '加速节点故障', 'danger', '加速节点处理发生故障，批改已停止；已完成进度保留在当前会话。', 'error', true],
    // A 503 is a chain fault by itself, but the accelerator state it names wins.
    ['waiting over 503', jsonResponse({ detail: { code: 'ACCELERATOR_WAITING', message: '等待节点' } }, { status: 503 }),
      '等待加速节点', 'warning', `等待节点 ${kept}`, 'warning', false],
  ]
  for (const [name, response, label, tagType, message, toast, readonly] of cases) {
    const sheet = await openAnswerSheet(t, { session: uploadedSource(), stream: () => response })
    await sheet.click('继续识别题目')
    assert.equal(sheet.count('get', S1), 2, `${name}: 读取已保存进度`)
    assert.deepEqual([sheet.serviceTag().text, sheet.serviceTag().getAttribute('type')], [label, tagType], name)
    assert.equal(sheet.serviceNotice().getAttribute('title'), message, name)
    assert.deepEqual(sheet.messages(), [[toast, message]], name)
    assert.equal(sheet.root.byClass('answer-sheet-page').classList.contains('workflow-readonly'), readonly, name)
    sheet.close()
  }
})

test('链路探测不阻塞已保存会话恢复', async (t) => {
  const sheet = await openAnswerSheet(t, { session: layoutDraft(), availability: () => new Promise(() => {}) })
  assert.equal(sheet.serviceTag().text, '检测中')
  assert.equal(sheet.count('get', '/answer-sheet/availability'), 1)
  assert.equal(sheet.count('get', S1), 1)
  assert.ok(sheet.alerts().includes('已恢复上次未完成的答题卡会话。'))
  assert.equal(sheet.activeStage(), 3)
})

test('版面表单支持归一化矩形、题号绑定、身份区域、保存与冻结门禁', async (t) => {
  const sheet = await openAnswerSheet(t, {
    session: layoutDraft(),
    respond: (config) => {
      if (config.method === 'put' && config.url === `${S1}/layout`) {
        const { answer_regions: answerRegions, identity_regions: identityRegions } = requestBody(config)
        return { data: { session: layoutDraft({ answer_regions: answerRegions, identity_regions: identityRegions }), summary: { can_freeze: true, bound_count: 3, question_count: 3, identity_region_count: 1 } } }
      }
      return undefined
    },
  })
  const editor = sheet.section('版面绑定与冻结')
  const answerForms = () => editor.findAll(el => el.classList.contains('region-form') && !el.classList.contains('identity-form'))
  const identityForms = () => editor.allByClass('identity-form')

  // Region types follow the bound question's type.
  assert.deepEqual(answerForms().map(form => form.byTag('el-select')[2].byTag('el-option').map(option)), [
    [{ label: '标准涂卡', value: 'choice_mark' }, { label: '手写字母', value: 'choice_handwritten' }],
    [{ label: '填空', value: 'fill' }],
    [{ label: '解答', value: 'solve' }],
  ])
  assert.deepEqual(identityForms()[0].byTag('el-select')[1].byTag('el-option').map(option), [
    { label: '姓名', value: 'name' },
    { label: '学号', value: 'student_no' },
    { label: '准考证号文字', value: 'exam_no_text' },
    { label: '二维码', value: 'qr' },
    { label: '条形码', value: 'barcode' },
    { label: '准考证号涂格', value: 'exam_no_marks' },
    { label: '页码', value: 'page_number' },
  ])

  // Every rectangle is four 0–1000 coordinates.
  const inputs = form => form.byClass('bbox-fields').byTag('input')
  for (const form of [...answerForms(), ...identityForms()]) {
    assert.deepEqual(inputs(form).map(input => [input.type, input.getAttribute('min'), input.getAttribute('max')]), Array(4).fill(['number', '0', '1000']))
  }
  assert.deepEqual(inputs(answerForms()[0]).map(input => input.value), ['100', '100', '300', '200'])

  // Nothing confirmed yet: freezing is closed.
  const summary = () => editor.byClass('layout-summary').text
  const confirm = form => form.find(el => el.tagName === 'el-checkbox' && el.text === '教师已确认').handlers['update:modelValue'](true)
  assert.ok(hasDisabled(sheet.button('冻结版面')))
  assert.equal(summary().includes('已绑定 0 / 3 题'), true, summary())
  assert.equal(summary().includes('漏绑定：1、2、3'), true, summary())
  // Moving a box onto another question's box is a conflict.
  const lower = inputs(answerForms()[1])[1]
  lower.value = '150'
  await lower.dispatch('input')
  await flush()
  // The teacher confirms the answer regions only.
  answerForms().forEach(confirm)
  await flush()
  assert.match(summary(), /冲突 1 组/)
  assert.ok(hasDisabled(sheet.button('冻结版面')))
  lower.value = '300'
  await lower.dispatch('input')
  await flush()
  assert.doesNotMatch(summary(), /冲突/)
  assert.match(summary(), /已绑定 3 \/ 3 题/)
  assert.doesNotMatch(summary(), /漏绑定/)
  // Every question is bound, but an unconfirmed identity region still keeps freezing closed.
  assert.equal(hasDisabled(sheet.button('冻结版面')), true, '身份区域未确认时不能冻结')
  identityForms().forEach(confirm)
  await flush()
  assert.equal(hasDisabled(sheet.button('冻结版面')), false)

  await sheet.click('保存版面')
  const saved = requestBody(sheet.request('put', `${S1}/layout`))
  assert.deepEqual(Object.keys(saved), ['answer_regions', 'identity_regions'])
  assert.deepEqual(saved.answer_regions.map(region => [region.region_id, region.question_no, region.bbox, region.teacher_confirmed]), [
    ['a1', '1', [100, 100, 300, 200], true],
    ['a2', '2', [100, 300, 300, 400], true],
    ['a3', '3', [100, 500, 900, 800], true],
  ])
  assert.deepEqual(saved.identity_regions.map(region => [region.region_id, region.identity_type, region.teacher_confirmed]), [['i1', 'name', true]])

  // The page image is protected: a page switch releases the previous object URL.
  assert.equal(sheet.count('get', `${S1}/layout/pages/p1/image`), 1)
  const [first] = sheet.images.urls()
  assert.equal(editor.find(el => el.tagName === 'img').getAttribute('src'), first)
  await sheet.click('第 2 页')
  assert.ok(sheet.images.revoked.includes(first))
  assert.equal(sheet.count('get', `${S1}/layout/pages/p2/image`), 1)

  await sheet.click('冻结版面')
  assert.equal(sheet.count('post', `${S1}/layout/freeze`), 1)
})

const reviewSession = sessionFixture({
  status: 'review',
  student_pages: [{
    paper_id: 'p1',
    filename: '张三.jpg',
    orientation_status: 'pending',
    review_reasons: ['identity'],
    identity: { student_id: 7 },
    answers: [{ region_id: 'r1', question_no: '1', answer: 'A' }],
  }],
})

test('学生答题卡阶段调用真实上传、方向、识别、复核、判分与归档接口', async (t) => {
  const drafts = [{ student_id: 7, paper_ids: ['p1'], result: { student_name: '张三', needs_review_count: 0, details: [{ question_no: '1', result: 'correct', paper_ids: ['p1'] }] } }]
  const sheet = await openAnswerSheet(t, {
    session: reviewSession,
    // The teacher has since switched the global current class away from the batch's class.
    classes: { classes: [{ id: 10, class_name: '一班' }, { id: 11, class_name: '二班' }], current_class_id: 11 },
    respond: config => (config.method === 'post' ? { data: reviewSession } : undefined),
    stream: request => sseResponse(request.url.endsWith('/students/recognize/stream')
      ? [['done', { session: reviewSession }]]
      : [['done', { drafts, review_revision: 2 }]]),
  })
  assert.doesNotMatch(sheet.page.html(), /后端接口待开放/)

  await sheet.click('90°')
  await sheet.click('执行识别')
  await sheet.click('确认本页身份与答案')
  await sheet.click('生成统一批阅草稿')
  await sheet.click('确认全部并归档')

  assert.deepEqual(sheet.writes(), [
    ['post', `${S1}/students/p1/orientation`],
    ['post', `${S1}/students/p1/review-confirm`],
    ['post', `${S1}/grading/archive`],
  ])
  assert.deepEqual(sheet.streams(), [
    ['POST', '/api/answer-sheet/sessions/s1/students/recognize/stream'],
    ['POST', '/api/answer-sheet/sessions/s1/grading/draft/stream'],
  ])
  assert.deepEqual(requestBody(sheet.request('post', `${S1}/students/p1/orientation`)), { quarter_turns_clockwise: 1 })
  assert.deepEqual(requestBody(sheet.request('post', `${S1}/students/p1/review-confirm`)), { student_id: 7, answers: { 1: 'A' } })
  // Identity candidates come from the class the batch belongs to, not the
  // current class; the archive is re-read.
  assert.equal(sheet.count('get', '/auth/classes'), 1)
  assert.equal(sheet.count('get', '/class/10/students'), 1)
  assert.equal(sheet.count('get', '/class/11/students'), 0)
  assert.equal(sheet.count('get', S1), 2)
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

// A durable student upload batch, and a frozen session that may carry one.
const upload = (fields = {}) => ({ upload_id: 'u1', status: 'uploading', uploaded_count: 0, processed_count: 0, total_bytes: 0, ...fields })
const frozen = studentUpload => sessionFixture({
  status: 'frozen',
  student_pages: [{ paper_id: 'old', filename: '旧页.jpg', orientation_status: 'auto_passed' }],
  ...(studentUpload ? { student_upload: studentUpload } : {}),
})

test('学生答题卡按单文件请求持久上传并可恢复处理', async (t) => {
  let uploaded = 0
  const live = liveStreams()
  const sheet = await openAnswerSheet(t, {
    session: frozen(),
    respond: (config) => {
      if (config.method !== 'post') return undefined
      if (config.url === `${S1}/students/upload/start`) return { data: frozen(upload({ restarted: requestBody(config).restart })) }
      if (config.url === `${S1}/students/upload`) return { data: frozen(upload({ uploaded_count: ++uploaded })) }
      return undefined
    },
    stream: request => live.open(request),
  })
  const files = [image('张三_正面.jpg'), image('张三_反面.jpg'), image('李四_正面.jpg')]
  await pickFiles(sheet, '导入学生答题卡', files)
  assert.equal(hasDisabled(sheet.button('执行识别')), false)
  const { done } = await sheet.press('上传并处理')

  assert.deepEqual(sheet.writes(), [
    ['post', `${S1}/students/upload/start`],
    ['post', `${S1}/students/upload`],
    ['post', `${S1}/students/upload`],
    ['post', `${S1}/students/upload`],
  ])
  const [start, ...pages] = sheet.client.requests.filter(config => config.method === 'post')
  assert.deepEqual(requestBody(start), { restart: false })
  for (const [index, page] of pages.entries()) {
    assert.equal(page.data.getAll('files').length, 1, '一次请求只带一个文件')
    assert.equal(page.data.get('files'), files[index])
    assert.equal(page.data.get('upload_id'), 'u1')
  }
  assert.deepEqual(sheet.streams(), [['POST', '/api/answer-sheet/sessions/s1/students/upload/u1/process/stream']])

  // Durable counts while the server processes the saved pages.
  await live.last.send('progress', { student_upload: { uploaded_count: 3, processed_count: 2, total_bytes: 2048 } })
  const progress = sheet.root.byClass('student-upload-progress')
  assert.match(progress.text, /图片处理中/)
  assert.match(progress.text, /已持久保存 3 页 · 已处理 2 页 · 2\.0 KB/)

  // The stream ends with the batch still open: recognition stays locked until
  // the batch is done, and the teacher can restart it.
  await live.last.send('done', { session: frozen(upload({ uploaded_count: 3, processed_count: 2 })) })
  await done
  await flush()
  assert.match(sheet.root.byClass('student-upload-progress').text, /上传批次可继续/)
  assert.ok(hasDisabled(sheet.button('执行识别')))
  await sheet.click('丢弃本批并重新开始')
  assert.deepEqual(sheet.page.boxes.map(box => [box.kind, box.title]), [['confirm', '重新开始学生答题卡上传']])
  const restart = sheet.client.requests.filter(config => config.url === `${S1}/students/upload/start`)
  assert.deepEqual(restart.map(requestBody), [{ restart: false }, { restart: true }])
})

test('学生答题卡未完成的上传批次在链路正常时可直接处理或继续上传', async (t) => {
  const pending = frozen(upload({ uploaded_count: 1, total_bytes: 10 }))
  const options = {
    session: pending,
    respond: (config) => {
      if (config.method !== 'post') return undefined
      if (config.url === `${S1}/students/upload/start`) return { data: pending }
      if (config.url === `${S1}/students/upload`) return { data: frozen(upload({ uploaded_count: 2, total_bytes: 20 })) }
      return undefined
    },
    stream: () => sseResponse([['done', { session: frozen() }]]),
  }
  const processStream = ['POST', '/api/answer-sheet/sessions/s1/students/upload/u1/process/stream']

  // Nothing newly chosen: the saved pages are processed as they are.
  const sheet = await openAnswerSheet(t, options)
  assert.equal(sheet.serviceTag().text, '可用')
  assert.match(sheet.root.byClass('student-upload-progress').text, /已持久保存 1 页/)
  await sheet.click('处理已上传页')
  assert.deepEqual(sheet.streams(), [processStream])
  assert.deepEqual(sheet.writes(), [])
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '学生答题卡已完成预处理'))
  assert.ok(!sheet.root.byClass('student-upload-progress'), '处理完成后批次关闭')
  sheet.close()

  // More pages chosen: the open batch is resumed (not restarted), then processed.
  const resumed = await openAnswerSheet(t, options)
  await pickFiles(resumed, '导入学生答题卡', [image('李四_正面.jpg')])
  assert.ok(!resumed.button('处理已上传页'), '选了新文件时先上传')
  await resumed.click('继续上传并处理')
  assert.deepEqual(resumed.writes(), [
    ['post', `${S1}/students/upload/start`],
    ['post', `${S1}/students/upload`],
  ])
  assert.deepEqual(requestBody(resumed.request('post', `${S1}/students/upload/start`)), { restart: false })
  assert.equal(resumed.request('post', `${S1}/students/upload`).data.get('upload_id'), 'u1')
  assert.deepEqual(resumed.streams(), [processStream])
})

test('学生答题卡逐页上传遇到传输故障时报链路故障，回读已保存页且不开始处理', async (t) => {
  for (const [name, failure, fault] of [
    ['network', config => { throw new AxiosError('Network Error', 'ERR_NETWORK', config) }, true],
    // A page the server rejects stops the upload too, but the chain is fine.
    ['400', () => ({ status: 400, data: { detail: '图片无法读取' } }), false],
  ]) {
    let uploaded = 0
    let failed = false
    const sheet = await openAnswerSheet(t, {
      session: frozen(),
      respond: (config) => {
        // After the failure the server still holds the first saved page.
        if (config.method === 'get' && config.url === S1 && failed) return { data: frozen(upload({ uploaded_count: 1, total_bytes: 10 })) }
        if (config.method !== 'post') return undefined
        if (config.url === `${S1}/students/upload/start`) return { data: frozen(upload()) }
        if (config.url === `${S1}/students/upload`) {
          uploaded += 1
          if (uploaded === 2) {
            failed = true
            return failure(config)
          }
          return { data: frozen(upload({ uploaded_count: uploaded, total_bytes: 10 })) }
        }
        return undefined
      },
    })
    await pickFiles(sheet, '导入学生答题卡', [image('张三_正面.jpg'), image('张三_反面.jpg'), image('李四_正面.jpg')])
    const reads = () => sheet.count('get', S1)
    assert.equal(reads(), 1, name)
    await sheet.click('上传并处理')

    assert.deepEqual(sheet.writes(), [
      ['post', `${S1}/students/upload/start`],
      ['post', `${S1}/students/upload`],
      ['post', `${S1}/students/upload`],
    ], `${name}: 中断后不再上传后续页`)
    assert.equal(reads(), 2, `${name}: 中断后读取服务器上已保存的页`)
    assert.match(sheet.root.byClass('student-upload-progress').text, /已持久保存 1 页/, name)
    assert.deepEqual(sheet.streams(), [], `${name}: 没有开始处理`)
    if (fault) {
      assert.deepEqual(
        [sheet.serviceTag().text, sheet.serviceNotice().getAttribute('title')],
        ['链路故障', '学生答题卡上传中断，已保存页不会丢失；链路恢复后可继续上传。'],
        name,
      )
      assert.ok(sheet.root.byClass('answer-sheet-page').classList.contains('workflow-readonly'), name)
    } else {
      assert.equal(sheet.serviceTag().text, '可用', name)
      assert.ok(sheet.messages().some(([type, message]) => type === 'error' && message === '图片无法读取'), JSON.stringify(sheet.messages()))
    }
    sheet.close()
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
