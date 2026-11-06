import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { installFetch, jsonResponse } from './helpers/memoryFetch.js'
import { flush, mountComponent } from './helpers/mountComponent.js'
import { authOf, click, deferred, options, pageStandIns, pick, select, serve, signedInTeacher } from './helpers/v1ExerciseBank.js'

// The question bank lists, filters and exports for the teacher's profile
// stage and account; whatever an older scope answers late is dropped.
const { default: Bank } = await import('../src/views/Bank.vue?sfc')

async function openBank(t, { stage = 'junior', list, chapters, exported } = {}) {
  const client = await signedInTeacher({ user: { id: 7, education_stage: stage }, token: 'synthetic-token-7' })
  const backend = serve(client, {
    'get /teacher/bank': ({ params }) => (list ? list(params) : { items: [{ id: 1, source: `${params.education_stage}-row` }], total: 1 }),
    'get /teacher/chapters': ({ params }) => (chapters ? chapters(params) : [{ chapter: params.education_stage }]),
  })
  const exports = installFetch(t, () => (exported ? exported() : jsonResponse({ items: [], count: 0 })))
  // What the page saves: the blob behind the download link it clicks.
  const saved = []
  t.mock.method(URL, 'createObjectURL', (blob) => { saved.push(blob); return 'blob:bank-export' })
  t.mock.method(URL, 'revokeObjectURL', () => {})
  const page = await mountComponent(t, Bank, { path: '/bank', plugins: [pageStandIns] })
  return { page, root: page.root, client, backend, exports, saved }
}

// The profile as /me hands it to the page (auth.fetchMe() replaces the user).
const changeUser = (page, changes) => {
  const auth = authOf(page)
  auth.user = { ...auth.user, ...changes }
}

const column = (root, label) => root.find(el => el.tagName === 'el-table-column' && el.attributes.label === label)
const rowSources = root => column(root, '来源').byTag('el-cell').map(cell => cell.text)
const chapterSelect = root => select(root, '全部章节')
const pager = root => root.byTag('el-pagination')[0]
const loadingShown = root => root.byTag('el-table')[0].dataset.loading
const exportButton = root => root.byText('el-button', '导出私库')

// A JSON body the server is still sending until end() is called.
function streamedJson() {
  let controller
  const stream = new ReadableStream({ start(c) { controller = c } })
  return {
    response: () => new Response(stream, { headers: { 'Content-Type': 'application/json' } }),
    end(data) {
      controller.enqueue(new TextEncoder().encode(JSON.stringify(data)))
      controller.close()
    },
  }
}

test('profile changes reset filters and reload matching lists and chapters', async t => {
  const seniorList = deferred()
  const { page, root, backend, exports, saved, client } = await openBank(t, {
    list: params => (params.education_stage === 'senior' ? seniorList.promise : { items: [{ id: 1, source: `junior-row-p${params.page}` }], total: 60 }),
  })
  assert.deepEqual(rowSources(root), ['junior-row-p1'])
  await pick(chapterSelect(root), 'junior')
  const turn = pager(root)
  turn.handlers['update:currentPage'](3)
  turn.handlers.currentChange(3)
  await page.flush()
  assert.deepEqual(backend.calls('get /teacher/bank').at(-1).params, { page: 3, page_size: 20, education_stage: 'junior', chapter: 'junior' })
  assert.equal(pager(root).props['current-page'], 3)

  changeUser(page, { education_stage: 'senior' })
  await page.flush()
  // The junior view is gone at once, before the senior list arrives.
  assert.deepEqual(rowSources(root), [])
  assert.equal(chapterSelect(root).props.modelValue, '')
  assert.equal(pager(root).props['current-page'], 1)
  assert.equal(pager(root).props.total, 0)
  seniorList.resolve({ items: [{ id: 2, source: 'senior-row' }], total: 1 })
  await page.flush()
  assert.deepEqual(rowSources(root), ['senior-row'])
  assert.deepEqual(options(chapterSelect(root)), ['senior'])
  assert.deepEqual(backend.calls('get /teacher/bank').at(-1).params, { page: 1, page_size: 20, education_stage: 'senior' })
  assert.deepEqual(backend.calls('get /teacher/chapters').map(call => call.params), [{ education_stage: 'junior' }, { education_stage: 'senior' }])

  await click(root, 'el-button', '导出私库')
  assert.deepEqual(exports.requests.map(request => request.url), ['/api/bank/export?education_stage=senior'])
  assert.equal(saved.length, 1)
  assert.deepEqual(JSON.parse(await saved[0].text()), { items: [], count: 0 })
  assert.deepEqual(client.messages, [{ type: 'success', message: '已导出 0 道私库题目' }])
})

test('late old-stage reads cannot overwrite fresh profile results', async t => {
  const oldList = deferred(), oldChapters = deferred()
  const { page, root } = await openBank(t, {
    list: params => (params.education_stage === 'junior' ? oldList.promise : { items: [{ id: 2, source: 'senior-row' }], total: 1 }),
    chapters: params => (params.education_stage === 'junior' ? oldChapters.promise : [{ chapter: 'senior' }]),
  })
  changeUser(page, { education_stage: 'senior' })
  await page.flush()
  oldList.resolve({ items: [{ id: 1, source: 'old junior' }], total: 999 })
  oldChapters.resolve([{ chapter: 'old junior' }])
  await page.flush()
  assert.deepEqual(rowSources(root), ['senior-row'])
  assert.deepEqual(options(chapterSelect(root)), ['senior'])
  assert.equal(pager(root).props.total, 1)
  assert.equal(loadingShown(root), 'false')
})

test('missing profile makes no requests and immediately clears existing results', async t => {
  const reload = deferred()
  let listed = 0
  const { page, root, client, exports } = await openBank(t, {
    stage: '',
    list: params => (++listed === 1 ? { items: [{ id: 1, source: `${params.education_stage}-row` }], total: 1 } : reload.promise),
  })
  assert.deepEqual(client.requests, [])
  assert.equal(root.byClass('bank-scope-note').text, '题库、章节和导出均使用个人资料中的未设置学段，无需重复选择。')
  assert.equal(exportButton(root).getAttribute('disabled'), '')
  await page.setup.onExport()
  assert.deepEqual(exports.requests, [])

  changeUser(page, { education_stage: 'junior' })
  await page.flush()
  assert.deepEqual(rowSources(root), ['junior-row'])
  assert.deepEqual(options(chapterSelect(root)), ['junior'])
  // A search is still loading when the stage goes away.
  await click(root, 'el-button', '查询')
  assert.equal(loadingShown(root), 'true')
  const count = client.requests.length

  changeUser(page, { education_stage: '' })
  await page.flush()
  assert.deepEqual(rowSources(root), [])
  assert.deepEqual(options(chapterSelect(root)), [])
  assert.equal(loadingShown(root), 'false')
  reload.resolve({ items: [{ id: 1, source: 'junior-row' }], total: 1 })
  await page.flush()
  assert.deepEqual(rowSources(root), [])
  await page.setup.onExport()
  assert.equal(client.requests.length, count)
  assert.deepEqual(exports.requests, [])
})

for (const change of ['stage', 'account', 'unmount']) {
  test(`export never downloads an old scope after ${change}`, async t => {
    const oldBody = streamedJson()
    const { page, root, client, saved, exports } = await openBank(t, { exported: oldBody.response })
    const pending = exportButton(root).dispatch('click')
    await page.flush()
    assert.equal(exports.requests.length, 1)
    assert.equal(exportButton(root).getAttribute('loading'), '')
    if (change === 'stage') changeUser(page, { education_stage: 'senior' })
    else if (change === 'account') authOf(page).user = { id: 8, education_stage: 'junior' }
    else page.unmount()
    oldBody.end({ items: [{ question: 'synthetic old scope' }], count: 1 })
    await pending
    await flush()
    assert.equal(saved.length, 0)
    assert.deepEqual(client.messages, [])
    // The new scope can export again: the abandoned export no longer shows as running.
    if (change !== 'unmount') assert.equal(exportButton(root).getAttribute('loading'), null)
  })
}

test('late same-stage account response is discarded', async t => {
  const old = deferred()
  let count = 0
  const { page, root } = await openBank(t, {
    list: () => (++count === 1 ? old.promise : { items: [{ id: 2, source: 'new-account' }], total: 1 }),
  })
  authOf(page).user = { id: 8, education_stage: 'junior' }
  await page.flush()
  old.resolve({ items: [{ id: 1, source: 'old-account' }], total: 1 })
  await page.flush()
  assert.deepEqual(rowSources(root), ['new-account'])
})

test('a failed export shows the server error and never downloads the error body', async t => {
  const { root, client, saved } = await openBank(t, {
    exported: () => jsonResponse({ detail: { message: '题库学段已跟随个人资料更新，请刷新后重试' } }, { status: 409 }),
  })
  await click(root, 'el-button', '导出私库')
  assert.equal(saved.length, 0)
  assert.deepEqual(client.messages, [{ type: 'error', message: '题库学段已跟随个人资料更新，请刷新后重试' }])
  assert.equal(exportButton(root).getAttribute('loading'), null)
})
