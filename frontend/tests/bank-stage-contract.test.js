import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/srcModules.js'
import { loadApiClient } from './helpers/apiClient.js'
import { installFetch, jsonResponse } from './helpers/memoryFetch.js'
import { mountComponent } from './helpers/mountComponent.js'
import {
  authOf, body, chooseFiles, click, pageStandIns, pick, select, serve, signedInTeacher,
} from './helpers/v1ExerciseBank.js'

// The question bank and the pages feeding it take the teaching stage from the
// teacher's profile or from what the server recorded, never from the client.
const { default: Bank } = await import('../src/views/Bank.vue?sfc')
const { default: BankPhotoImport } = await import('../src/views/BankPhotoImport.vue?sfc')
const { default: PendingAnswers } = await import('../src/views/PendingAnswers.vue?sfc')

const column = (root, label) => root.find(el => el.tagName === 'el-table-column' && el.attributes.label === label)

test('bank follows profile without independent stage or legacy-adoption controls', async t => {
  const user = { id: 7, education_stage: 'junior', profile_complete: true }
  const client = await signedInTeacher({ user })
  const backend = serve(client, {
    // A legacy question without a recorded stage is listed like any other.
    'get /teacher/bank': () => ({ items: [{ id: 1, teacher_id: 7, source: 'legacy-row', chapter: '函数', topic: '定义域' }], total: 1 }),
    'get /teacher/chapters': () => [{ chapter: '函数', topics: ['定义域'] }],
  })
  const exports = installFetch(t, () => jsonResponse({ items: [], count: 0 }))
  t.mock.method(URL, 'createObjectURL', () => 'blob:bank-export')
  t.mock.method(URL, 'revokeObjectURL', () => {})
  const page = await mountComponent(t, Bank, { path: '/bank', plugins: [pageStandIns] })
  const { root } = page

  assert.equal(root.byClass('bank-scope-note').text, '题库、章节和导出均使用个人资料中的初中学段，无需重复选择。')
  assert.equal(root.byClass('card-head-left').byTag('el-tag')[0].text, '初中数学')
  // The only choice offered is the chapter; no stage picker, no adopting legacy rows.
  assert.deepEqual(root.byTag('el-select').map(el => el.attributes.placeholder), ['全部章节'])
  assert.deepEqual(root.byClass('card-head').byTag('el-button').map(el => el.text), ['查询', '整卷真题', '拍照导入', '导出私库'])
  assert.doesNotMatch(root.text, /历史未标学段/)

  await pick(select(root, '全部章节'), '函数')
  await click(root, 'el-button', '导出私库')
  for (const call of backend.calls('get /teacher/bank')) assert.equal(call.params.education_stage, 'junior')
  assert.deepEqual(backend.calls('get /teacher/chapters').map(call => call.params), [{ education_stage: 'junior' }])
  assert.deepEqual(exports.requests.map(request => request.url), ['/api/bank/export?education_stage=junior'])
  // The page reads the profile stage and never writes it back.
  assert.deepEqual([...new Set(backend.keys())], ['get /teacher/chapters', 'get /teacher/bank'])
  assert.deepEqual(authOf(page).user, user)
  assert.deepEqual(JSON.parse(client.localStorage.getItem('mathagent_user')), user)
})

test('bank query and export forward optional stage params', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  client.respond(() => ({ data: { chapters: ['c1'] } }))
  assert.deepEqual(await client.api.bank.chapters({ education_stage: 'junior' }), { chapters: ['c1'] })
  assert.deepEqual(await client.api.bank.chapters(), { chapters: ['c1'] })
  const sent = client.requests.map(({ method, url, params, headers }) => ({
    method, url, params, authorization: headers.Authorization,
  }))
  assert.deepEqual(sent, [
    { method: 'get', url: '/teacher/chapters', params: { education_stage: 'junior' }, authorization: 'Bearer teacher-token' },
    { method: 'get', url: '/teacher/chapters', params: undefined, authorization: 'Bearer teacher-token' },
  ])

  const fetched = []
  const realFetch = globalThis.fetch
  globalThis.fetch = async (url, options = {}) => {
    fetched.push({ url, method: options.method ?? 'GET', authorization: options.headers?.Authorization })
    return new Response(`xlsx ${fetched.length}`)
  }
  const exported = []
  try {
    exported.push(await client.api.bank.exportPrivate({ education_stage: 'senior' }))
    exported.push(await client.api.bank.exportPrivate())
  } finally {
    globalThis.fetch = realFetch
  }
  assert.deepEqual(fetched, [
    { url: '/api/bank/export?education_stage=senior', method: 'GET', authorization: 'Bearer teacher-token' },
    { url: '/api/bank/export?', method: 'GET', authorization: 'Bearer teacher-token' },
  ])
  assert.deepEqual(await Promise.all(exported.map(resp => resp.text())), ['xlsx 1', 'xlsx 2'])
})

test('photo import returns server batch id on save and restores only that opaque id', async t => {
  const context = { education_stage: 'junior', curriculum_version: 'server-catalog', rules_version: 'server-rules' }
  const paired = [
    { question_no: 1, content: '题干一', answer: '答案一' },
    { question_no: 2, content: '题干二', answer: '' },
  ]
  const routes = {
    'get /teacher/chapters': ({ params }) => [{ chapter: `${params?.education_stage}-chapter` }],
    'post /bank/photo-import/recognize': () => ({ import_id: 'imp-1', teaching_context: context, paired }),
    'post /bank/photo-import/save': () => ({ inserted: 1, skipped: 0 }),
    'get /bank/photo-import/imp-1': () => ({ import_id: 'imp-1', teaching_context: context, paired }),
  }
  const user = { id: 7, education_stage: 'junior' }
  const client = await signedInTeacher({ user })
  const backend = serve(client, routes)
  // /bank is an empty page here: the app's route loads Bank.vue lazily, and
  // that first import would leave the page's (unawaited) push to /bank
  // unfinished for a varying number of rounds after the click settles.
  const page = await mountComponent(t, BankPhotoImport, {
    path: '/bank/photo-import',
    plugins: [pageStandIns],
    routes: [
      { path: '/bank/photo-import', component: { render: () => null } },
      { path: '/bank', component: { render: () => null } },
    ],
  })
  await chooseFiles(page.root, '题干图', [new File(['synthetic-only'], 'question.png', { type: 'image/png' })])
  await click(page.root, 'el-button', '识别并配对')
  const [recognized] = backend.calls('post /bank/photo-import/recognize')
  assert.deepEqual([...recognized.data.keys()], ['question_files'])
  // Only the server's opaque batch id is remembered.
  assert.equal(client.localStorage.getItem('mathagent-bank-import:7'), 'imp-1')

  await click(page.root, 'el-button', '全部入库')
  assert.equal(page.boxes.length, 1)
  assert.match(page.boxes[0].message, /题目仅进入你的私有题库/)
  const [save] = backend.calls('post /bank/photo-import/save')
  assert.deepEqual(body(save), {
    items: [{ question_no: 1, content: '题干一', answer: '答案一', chapter: '' }],
    import_id: 'imp-1',
  })
  assert.equal(client.localStorage.getItem('mathagent-bank-import:7'), null)
  assert.equal(page.router.currentRoute.value.path, '/bank')
  page.unmount()

  // Coming back to an unsaved batch: the page restores it by that id alone.
  const returning = await signedInTeacher({ user, localStorage: { 'mathagent-bank-import:7': 'imp-1' } })
  const restoring = serve(returning, routes)
  const restored = await mountComponent(t, BankPhotoImport, { path: '/bank/photo-import', plugins: [pageStandIns] })
  assert.deepEqual(restoring.keys(), ['get /bank/photo-import/imp-1', 'get /teacher/chapters'])
  assert.deepEqual(restoring.calls('get /teacher/chapters')[0].params, { education_stage: 'junior' })
  assert.deepEqual(column(restored.root, '题号').byTag('el-cell').map(cell => cell.text), ['1', '2'])
  assert.equal(restored.root.byText('el-tag', '本批次已固定').text, '初中数学 · 本批次已固定')
})

test('pending answer confirmation sends teacher-edited answer with its persisted source', async t => {
  const rows = [
    { question: 'q1', question_preview: '题一', student_answer: '3', ai_answer: 'AI 答案一', grading_id: 11, detail_index: 0, teaching_context: { education_stage: 'junior' }, classification_status: 'classified', chapter: '函数', topic: '定义域' },
    { question: 'q2', question_preview: '题二', student_answer: '5', ai_answer: 'AI 答案二', grading_id: 12, detail_index: 3, classification_status: 'failed' },
  ]
  const client = await signedInTeacher({ user: { id: 7, education_stage: 'junior' } })
  const backend = serve(client, {
    'get /bank/pending-answers': () => ({ items: rows, total: rows.length }),
    'get /teacher/chapters': () => [{ chapter: '函数', topics: ['定义域'] }],
    'post /bank/confirm-answers': () => ({}),
    'post /bank/pending-answers/delete': () => ({}),
  })
  const page = await mountComponent(t, PendingAnswers, { path: '/pending-answers' })
  const { root } = page
  assert.deepEqual(root.findAll(el => el.classList.contains('stage-cell')).map(el => el.text), ['初中数学', '历史未标学段'])

  const answers = () => column(root, 'AI标准答案').byTag('el-input')
  const rowButtons = text => column(root, '操作').byTag('el-button').filter(el => el.text === text)
  await pick(answers()[0], '老师改过的答案一')
  await rowButtons('确认入库')[0].dispatch('click')
  await page.flush()
  const [one] = backend.calls('post /bank/confirm-answers').map(body)
  assert.equal(one.answers.length, 1)
  assert.equal(one.answers[0].answer, '老师改过的答案一')
  assert.deepEqual([one.answers[0].grading_id, one.answers[0].detail_index, one.answers[0].question], [11, 0, 'q1'])

  // A batch carries each row's edited answer (the edits survive the reload).
  await pick(answers()[1], '老师改过的答案二')
  root.byTag('el-table')[0].handlers.selectionChange([...page.setup.rows])
  await page.flush()
  await click(root, 'el-button', '批量入库 (2)')
  const batch = backend.calls('post /bank/confirm-answers').map(body)[1]
  assert.deepEqual(batch.answers.map(answer => answer.answer), ['老师改过的答案一', '老师改过的答案二'])

  // Dismissing names the grading record each answer came from.
  await rowButtons('忽略')[1].dispatch('click')
  await page.flush()
  root.byTag('el-table')[0].handlers.selectionChange([...page.setup.rows])
  await page.flush()
  await click(root, 'el-button', '批量忽略 (2)')
  assert.deepEqual(backend.calls('post /bank/pending-answers/delete').map(body), [
    { questions: ['q2'], sources: [{ grading_id: 12, detail_index: 3 }] },
    { questions: ['q1', 'q2'], sources: [{ grading_id: 11, detail_index: 0 }, { grading_id: 12, detail_index: 3 }] },
  ])
  assert.deepEqual(page.boxes.map(box => box.title), ['忽略这道题', '批量忽略'])
})
