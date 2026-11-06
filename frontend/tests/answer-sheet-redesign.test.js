import test from 'node:test'
import assert from 'node:assert/strict'
import { batchFixture, body, flush, openAnswerSheet, PAPER, SESSION_KEY, SHEET } from './helpers/answerSheetPage.js'
import { loadApiClient } from './helpers/apiClient.js'

// The create-batch card: its form items by label, and the controls in them.
const createCard = sheet => sheet.root.byClass('create-card')
const formItem = (sheet, label) => createCard(sheet).byTag('el-form-item').find(el => el.getAttribute('label') === label)
const paperSelect = sheet => formItem(sheet, '试卷模板（必选）').byTag('el-select')[0]
const sheetSelect = sheet => formItem(sheet, '答题卡模板（可选，也可稍后选择或新建）').byTag('el-select')[0]
const registration = sheet => createCard(sheet).byClass('template-code-actions')
const choose = async (control, value) => {
  control.handlers['update:modelValue'](value)
  await flush()
}
const option = el => ({ label: el.getAttribute('label'), value: el._value })
const disabled = el => el.hasAttribute('disabled')
const label = `${SHEET}-20260927-143000`
const created = '/answer-sheet/sessions'

test('批次不再选择批阅类型或班级，班级来自全局当前班级', async (t) => {
  const sheet = await openAnswerSheet(t, {
    classes: { classes: [{ id: 10, class_name: '一班' }, { id: 11, class_name: '二班' }], current_class_id: 11 },
    paperTemplates: [{ template_code: PAPER, question_count: 4 }],
    respond: config => (config.method === 'post' && config.url === created ? { data: batchFixture() } : undefined),
  })
  assert.deepEqual(createCard(sheet).byTag('el-form-item').map(el => el.getAttribute('label')), [
    '当前班级（顶部导航切换，批次自动继承）',
    '试卷模板（必选）',
    '答题卡模板（可选，也可稍后选择或新建）',
  ])
  const classInput = formItem(sheet, '当前班级（顶部导航切换，批次自动继承）').byTag('el-input')[0]
  assert.equal(classInput.getAttribute('model-value'), '二班')
  assert.ok(disabled(classInput), '班级只读，跟随顶部导航')
  assert.doesNotMatch(sheet.page.html(), /批阅类型|考试名称/)

  await choose(paperSelect(sheet), PAPER)
  await sheet.click('创建批次并继续')
  // The class comes from the server-side current class: the request names none, nor a grading type.
  assert.deepEqual(body(sheet.request('post', created)), { paper_template_code: PAPER })
})

test('批次列表接口按答题卡模板号加时间戳展示', async (t) => {
  const sheet = await openAnswerSheet(t, {
    saved: null,
    batches: [
      { session_id: 'b1', batch_label: label, paper_template_code: PAPER, status: 'frozen', created_at: '2026-09-27T06:30:00+00:00' },
      { session_id: 'b2', batch_label: '', paper_template_code: PAPER, status: 'answers_confirmed', created_at: '2026-09-27T07:00:00+00:00' },
    ],
    sessions: [batchFixture({ session_id: 'b1', status: 'frozen', batch_label: label })],
  })
  const rows = sheet.root.allByClass('batch-row')
  assert.deepEqual(rows.map(row => row.byTag('strong')[0].text), [label, '待绑定答题卡模板'])
  assert.deepEqual(rows.map(row => row.byTag('span').map(span => span.text)), [
    [`试卷模板 ${PAPER}`, '版面已冻结'],
    [`试卷模板 ${PAPER}`, '答案已确认'],
  ])

  await rows[0].dispatch('click')
  await flush()
  const meta = sheet.root.byClass('session-meta')
  const batch = meta.byTag('span').find(span => span.text.startsWith('批次'))
  assert.equal(batch.text, `批次 ${label}`)
  assert.equal(batch.getAttribute('title'), label)
})

test('批次列表接口不带参数 GET /answer-sheet/sessions 并返回后端批次', async () => {
  const client = await loadApiClient()
  const batches = [{ session_id: 'b1', batch_label: 'AS-1111-2222-3333-4444-20260927-143000' }]
  client.respond(() => ({ data: { batches } }))

  assert.deepEqual(await client.api.answerSheet.listSessions(), { batches })
  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'get')
  assert.equal(request.url, '/answer-sheet/sessions')
  assert.equal(request.params, undefined)
  assert.equal(request.data, undefined)
})

test('建批次向导使用试卷模板选择与登记，不再提交空请求', async (t) => {
  const sheet = await openAnswerSheet(t, {
    paperTemplates: [
      { template_code: PAPER, alias: '期中A卷', exam_name: '期中考试', question_count: 4 },
      { template_code: 'PT-EEEE-FFFF-GGGG-HHHH', alias: '', exam_name: '', question_count: 2 },
    ],
    paperSources: [
      { template_id: 'tpl-1', exam_name: '期中考试', question_count: 4, confirmed_at: '2026-09-27T06:00:00+00:00' },
      { template_id: 'tpl-2', exam_name: '', question_count: 3 },
    ],
    respond: config => (config.method === 'post' && config.url === '/answer-sheet/paper-templates'
      ? { data: { template: { template_code: PAPER } } }
      : undefined),
  })
  assert.deepEqual(paperSelect(sheet).byTag('el-option').map(option), [
    { label: `期中A卷　${PAPER}　4题`, value: PAPER },
    { label: 'PT-EEEE-FFFF-GGGG-HHHH　2题', value: 'PT-EEEE-FFFF-GGGG-HHHH' },
  ])
  const sources = registration(sheet).byTag('el-select')[0].byTag('el-option').map(option)
  assert.deepEqual(sources.map(item => item.value), ['tpl-1', 'tpl-2'])
  assert.match(sources[0].label, /^期中考试　4题　确认于 \S/)
  assert.equal(sources[1].label, '详细批改模板 tpl-2　3题')

  // Nothing chosen: neither registering nor creating can send a request.
  assert.ok(disabled(sheet.button('登记试卷模板')))
  assert.ok(disabled(sheet.button('创建批次并继续')))
  await sheet.click('创建批次并继续')
  await sheet.click('登记试卷模板')
  assert.deepEqual(sheet.writes(), [])

  await choose(registration(sheet).byTag('el-select')[0], 'tpl-1')
  await sheet.click('登记试卷模板')
  assert.deepEqual(sheet.writes(), [['post', '/answer-sheet/paper-templates']])
  assert.deepEqual(body(sheet.request('post', '/answer-sheet/paper-templates')), { template_id: 'tpl-1', alias: '' })
})

test('创建批次必须先选试卷模板，请求只携带模板号（无班级/类型字段）', async (t) => {
  const sheet = await openAnswerSheet(t, {
    paperTemplates: [{ template_code: PAPER, question_count: 1 }],
    respond: config => (config.method === 'post' && config.url === created ? { data: batchFixture() } : undefined),
  })
  assert.ok(disabled(sheet.button('创建批次并继续')))
  await choose(paperSelect(sheet), PAPER)
  assert.equal(disabled(sheet.button('创建批次并继续')), false)
  await sheet.click('创建批次并继续')

  assert.deepEqual(sheet.writes(), [['post', created]])
  assert.deepEqual(body(sheet.request('post', created)), { paper_template_code: PAPER })
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '答题卡批次已创建'))
  // A batch shows its paper template where an ordinary session uploads the source paper.
  assert.deepEqual(sheet.stageButtons().slice(0, 2).map(el => el.byTag('strong')[0].text), ['试卷模板', '题目答案'])
  assert.ok(!sheet.section('原始试卷与答案来源'))
  assert.match(sheet.section('试卷模板').text, new RegExp(`已绑定试卷模板 ${PAPER}，共 1 题，答案已冻结。`))
  // Answers come from the frozen paper template: continue at the answer-sheet stage.
  assert.equal(sheet.activeStage(), 2)
  assert.equal(sheet.client.localStorage.getItem(SESSION_KEY), 's-new')

  // Back on the list, the form starts empty for the next batch.
  await sheet.click('批次列表')
  assert.equal(paperSelect(sheet).getAttribute('modelValue'), '')
  assert.equal(sheetSelect(sheet).getAttribute('modelValue'), '')
  assert.ok(disabled(sheet.button('创建批次并继续')))
})

test('建批次时选择答题卡模板号：规范为大写提交，批次获得模板号加时间戳标签', async (t) => {
  const sheet = await openAnswerSheet(t, {
    paperTemplates: [{ template_code: PAPER, question_count: 1 }],
    history: [{ template_code: SHEET, page_count: 1, question_count: 1 }],
    respond: config => (config.method === 'post' && config.url === created
      ? { data: batchFixture({ status: 'frozen', answer_sheet_template_code: body(config).answer_sheet_template_code, batch_label: label }) }
      : undefined),
  })
  assert.deepEqual(sheetSelect(sheet).byTag('el-option').map(item => item._value), [SHEET])
  await choose(paperSelect(sheet), PAPER)
  await choose(sheetSelect(sheet), ` ${SHEET.toLowerCase()} `)
  await sheet.click('创建批次并继续')

  assert.deepEqual(body(sheet.request('post', created)), { paper_template_code: PAPER, answer_sheet_template_code: SHEET })
  const batch = sheet.root.byClass('session-meta').byTag('span').find(span => span.text.startsWith('批次'))
  assert.equal(batch.text, `批次 ${label}`)
  // A batch with a frozen answer-sheet template continues at the student stage.
  assert.equal(sheet.activeStage(), 4)
  assert.equal(sheet.count('get', '/answer-sheet/templates/history'), 2, '使用的模板码进入历史')
})

test('登记试卷模板后刷新列表并自动选中新模板号', async (t) => {
  let registered = []
  const sheet = await openAnswerSheet(t, {
    paperTemplates: () => registered,
    paperSources: [{ template_id: 'tpl-1', exam_name: '期中考试', question_count: 4, confirmed_at: '2026-09-27T06:00:00+00:00' }],
    respond: (config) => {
      if (config.method !== 'post' || config.url !== '/answer-sheet/paper-templates') return undefined
      registered = [{ template_code: PAPER, alias: body(config).alias, exam_name: '期中考试', question_count: 4 }]
      return { data: { template: registered[0] } }
    },
  })
  assert.equal(paperSelect(sheet).byTag('el-option').length, 0)
  const [sourceSelect] = registration(sheet).byTag('el-select')
  assert.deepEqual(sourceSelect.byTag('el-option').map(item => item._value), ['tpl-1'])

  await choose(sourceSelect, 'tpl-1')
  await choose(registration(sheet).byTag('el-input')[0], ' 期中A卷 ')
  await sheet.click('登记试卷模板')

  assert.deepEqual(body(sheet.request('post', '/answer-sheet/paper-templates')), { template_id: 'tpl-1', alias: '期中A卷' })
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '试卷模板已登记，可在多个批次复用'))
  assert.equal(sheet.count('get', '/answer-sheet/paper-templates'), 2)
  assert.deepEqual(paperSelect(sheet).byTag('el-option').map(option), [{ label: `期中A卷　${PAPER}　4题`, value: PAPER }])
  assert.equal(paperSelect(sheet).getAttribute('modelValue'), PAPER)
  assert.equal(registration(sheet).byTag('el-select')[0].getAttribute('modelValue'), '')
  assert.equal(registration(sheet).byTag('el-input')[0].getAttribute('modelValue'), '')
  assert.equal(disabled(sheet.button('创建批次并继续')), false)
})

test('批次列表按标签重新打开批次；返回列表不删除批次', async (t) => {
  const sheet = await openAnswerSheet(t, {
    saved: null,
    batches: [{ session_id: 'b1', batch_label: label, paper_template_code: PAPER, status: 'frozen', created_at: '2026-09-27T06:30:00+00:00' }],
    sessions: [batchFixture({ session_id: 'b1', status: 'frozen', batch_label: label })],
  })
  assert.equal(sheet.root.byClass('batch-row').byTag('strong')[0].text, label)
  await sheet.root.byClass('batch-row').dispatch('click')
  await flush()

  assert.equal(sheet.count('get', '/answer-sheet/sessions/b1'), 1)
  assert.ok(!createCard(sheet))
  assert.ok(sheet.root.byClass('session-meta').byTag('span').some(span => span.text === '会话 b1'))
  assert.equal(sheet.client.localStorage.getItem(SESSION_KEY), 'b1')

  const listed = sheet.count('get', '/answer-sheet/sessions')
  await sheet.click('批次列表')
  assert.ok(createCard(sheet))
  assert.equal(sheet.client.localStorage.getItem(SESSION_KEY), null)
  assert.deepEqual(sheet.writes(), [], '返回列表不删除批次')
  assert.equal(sheet.count('get', '/answer-sheet/sessions'), listed + 1)
  assert.equal(sheet.root.byClass('batch-row').byTag('strong')[0].text, label)
})
