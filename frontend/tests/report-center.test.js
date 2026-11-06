import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { loadApiClient } from './helpers/apiClient.js'
import {
  bodyOf,
  button,
  choose,
  flush,
  holdingBackend,
  installClipboard,
  mountView,
  route,
  signIn,
} from './helpers/viewsStageReports.js'

const { default: Reports } = await import('../src/views/Reports.vue?sfc')

const ACTIVE_SHARE = {
  id: 42,
  student_name: '张三',
  student_no: '01',
  status: '有效',
  url: '/letter/abc',
  view_count: 2,
  education_stage: 'junior',
  expires_at: '2099-01-01T00:00:00Z',
}

async function openReports(t, reply, { classes = [7] } = {}) {
  const signedIn = await signIn({ stage: 'junior', classId: classes[0], className: '高一（7）班' })
  signedIn.auth.classes = classes.map(id => ({ id, class_name: `高一（${id}）班`, student_count: 30 }))
  holdingBackend(signedIn.client, reply)
  const page = await mountView(t, Reports, { path: '/reports', pinia: signedIn.pinia })
  return { ...signedIn, page }
}

const metric = (page, label) => page.root.allByClass('hero-metric').find(el => el.text.includes(label))
  ?.byClass('hero-metric__value').text

test('report overview is scoped by current class and period', async (t) => {
  const { client, auth, page } = await openReports(t, (config) => {
    if (route(config) === 'post /reports/summary') {
      const { period, class_id: classId } = bodyOf(config)
      return { data: { total: 3, total_questions: period === 'week' ? 37 : 120 + classId, accuracy: 81.5, summary: `${classId} 班 ${period}` } }
    }
    if (route(config) === 'get /student/list') return { data: { items: [], total: 0 } }
    return { data: { shares: [] } }
  }, { classes: [7, 8] })
  const summaries = () => client.requests.filter(config => route(config) === 'post /reports/summary').map(bodyOf)

  assert.deepEqual(summaries(), [{ education_stage: 'junior', period: 'week', class_id: 7, type: 'all' }])
  assert.equal(metric(page, '作答题数'), '37题', 'the overview shows how many questions were answered')

  await choose(page.root.byClass('ink-segmented'), 'month')
  await flush()
  assert.deepEqual(summaries().at(-1), { education_stage: 'junior', period: 'month', class_id: 7, type: 'all' })
  assert.equal(metric(page, '作答题数'), '127题')

  auth.currentClassId = 8
  await flush()
  assert.deepEqual(summaries().at(-1), { education_stage: 'junior', period: 'month', class_id: 8, type: 'all' })
  assert.equal(metric(page, '作答题数'), '128题')
  assert.match(page.root.byClass('summary-text').text, /8 班 month/)

  // Parent letters are per student: there is no class-wide parent entry.
  assert.doesNotMatch(page.root.text, /班级统一入口/)
  const endpoints = new Set(client.requests.map(route))
  assert.deepEqual([...endpoints].sort(), [
    'get /class/7/letter-shares',
    'get /class/8/letter-shares',
    'get /student/list',
    'post /reports/summary',
  ])
})

test('parent letter links are reusable, refreshable and manageable without raw HTML dialogs', async (t) => {
  const clipboard = installClipboard(t)
  const { client, page } = await openReports(t, (config) => {
    const at = route(config)
    if (at === 'post /class/7/report/batch') {
      return { data: { mode: 'share', shares: [ACTIVE_SHARE], skipped: ['李四'], empty_content: ['王五'] } }
    }
    if (at === 'get /class/7/letter-shares') return { data: { shares: [ACTIVE_SHARE, { ...ACTIVE_SHARE, id: 43, student_name: '赵六', status: '已过期', url: '/letter/old' }] } }
    if (at === 'get /student/list') return { data: { items: [], total: 0 } }
    return { data: {} }
  })
  const since = (count) => client.requests.slice(count).map(config => [route(config), bodyOf(config)])
  const link = 'http://localhost/letter/abc'

  // Preparing reuses valid links (refresh: false), then lists them and copies them.
  let before = client.requests.length
  await button(page, '准备并复制链接').dispatch('click')
  await flush()
  assert.deepEqual(since(before), [
    ['post /class/7/report/batch', { education_stage: 'junior', type: 'parent_letter', lock: 'token', refresh: false }],
    ['get /class/7/letter-shares', null],
  ])
  assert.deepEqual(clipboard, [`张三：${link}`])
  const warning = page.boxes.find(box => box.kind === 'alert')
  assert.match(warning.message, /未生成 1 人：李四/)
  assert.match(warning.message, /王五/)

  // Regenerating asks first, then replaces this stage's links (refresh: true).
  before = client.requests.length
  await button(page, '重新生成').dispatch('click')
  await flush()
  assert.equal(page.boxes.at(-2).title, '重新生成家长链接')
  assert.deepEqual(since(before)[0], ['post /class/7/report/batch', { education_stage: 'junior', type: 'parent_letter', lock: 'token', refresh: true }])

  // The list refreshes on demand.
  before = client.requests.length
  await button(page, '刷新').dispatch('click')
  await flush()
  assert.deepEqual(since(before), [['get /class/7/letter-shares', null]])

  // Copying all copies only the links that still work.
  await button(page, '复制全部有效链接').dispatch('click')
  assert.equal(clipboard.at(-1), `张三：${link}`)

  // A single link is revoked by its id after confirming, then the list reloads.
  before = client.requests.length
  const table = page.root.byClass('letter-share-table')
  await table.byText('el-button', '撤销').dispatch('click')
  await flush()
  assert.equal(page.boxes.at(-1).title, '撤销家长链接')
  assert.deepEqual(since(before), [['post /letter-share/42/revoke', null], ['get /class/7/letter-shares', null]])

  // Every dialog is plain text: none renders raw HTML.
  assert.ok(page.boxes.length >= 3)
  for (const box of page.boxes) assert.notEqual(box.options?.dangerouslyUseHTMLString, true, box.title)
})

test('letter share API lists one class\'s links with the given query and revokes a single link by id', async () => {
  const client = await loadApiClient()
  const shares = { shares: [{ id: 42, status: '有效' }] }
  const query = { education_stage: 'junior', status: 'active', page: 2 }
  client.respond(() => ({ data: shares }))

  assert.deepEqual(await client.api.classroom.letterShares(7, { ...query }), shares)
  await client.api.classroom.revokeLetterShare(42)

  assert.deepEqual(client.requests[0].params, query, 'every query key is forwarded unchanged')
  assert.deepEqual(client.requests.map(({ method, url, params }) => ({ method, url, params })), [
    { method: 'get', url: '/class/7/letter-shares', params: query },
    { method: 'post', url: '/letter-share/42/revoke', params: undefined },
  ])
})

test('API client offers no class-wide parent link', async () => {
  const { module } = await loadApiClient()
  const keys = (value, seen = new Set()) => {
    if (!value || typeof value !== 'object' || seen.has(value)) return []
    seen.add(value)
    return Object.entries(value).flatMap(([key, child]) => [key, ...keys(child, seen)])
  }
  const exposed = [
    ...Object.keys(module),
    ...keys(module.default),
    ...keys(module.phoneCaptureClient('session', 'token')),
    ...keys(module.studentSubmitClient('token')),
  ]

  assert.ok(exposed.includes('letterShares'), 'the walk reaches the classroom namespace')
  assert.equal(exposed.includes('classParentLink'), false)
})

test('generated letter links keep all fields within the desktop width budget', async (t) => {
  const clipboard = installClipboard(t)
  const { client, page } = await openReports(t, (config) => {
    if (route(config) === 'get /class/7/letter-shares') return { data: { shares: [ACTIVE_SHARE] } }
    if (route(config) === 'get /student/list') return { data: { items: [], total: 0 } }
    return { data: {} }
  })
  const table = page.root.byClass('letter-share-table')
  assert.ok(table, 'generated links table is present')
  const columns = table.children.filter(el => el.tagName === 'el-table-column').map((el) => {
    const label = el.getAttribute('label')
    // A column without a width of its own takes element-plus's default
    // minimum (80px), which the budget below would not see.
    const declared = el.getAttribute('width') ?? el.getAttribute('min-width')
    assert.match(String(declared), /^\d+$/, `${label} declares its width in px`)
    return { label, width: Number(declared), fixed: el.getAttribute('fixed'), el }
  })

  assert.deepEqual(columns.map(column => column.label), ['学生', '状态', '查看次数', '学段', '有效期', '操作'])
  const minimumWidth = columns.reduce((sum, column) => sum + column.width, 0)
  assert.ok(minimumWidth <= 558, `column widths ${minimumWidth}px exceed the 1280px desktop table budget`)
  const actions = columns.at(-1)
  assert.equal(actions.fixed, 'right')
  assert.match(table.text, /张三/)
  assert.match(table.text, /初中数学/)

  await actions.el.byText('el-button', '复制').dispatch('click')
  assert.deepEqual(clipboard, ['http://localhost/letter/abc'])
  assert.deepEqual(page.boxes, [])
  const before = client.requests.length
  await actions.el.byText('el-button', '撤销').dispatch('click')
  await flush()
  assert.deepEqual(client.requests.slice(before).map(route), ['post /letter-share/42/revoke', 'get /class/7/letter-shares'])
})
