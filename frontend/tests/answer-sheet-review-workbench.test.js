import assert from 'node:assert/strict'
import test from 'node:test'
import { body, flush, openAnswerSheet, SESSION_KEY, sessionFixture } from './helpers/answerSheetPage.js'
import { loadApiClient } from './helpers/apiClient.js'
import { mountComponent } from './helpers/mountComponent.js'

const S1 = '/answer-sheet/sessions/s1'

// A session under review with grading drafts, as the server keeps it.
function reviewed({ drafts, revision = 3, status = 'review' } = {}) {
  return sessionFixture({
    status,
    review_revision: revision,
    student_pages: [
      { paper_id: 'p1', filename: '张三-1.jpg', orientation_status: 'auto_passed' },
      { paper_id: 'p2', filename: '张三-2.jpg', orientation_status: 'auto_passed' },
      { paper_id: 'p3', filename: '李四-1.jpg', orientation_status: 'auto_passed' },
    ],
    grouping_decisions: drafts ?? [
      {
        student_id: 7,
        paper_ids: ['p1', 'p2'],
        result: {
          student_name: '张三',
          correct_count: 1,
          wrong_count: 1,
          needs_review_count: 0,
          details: [
            { question_no: '1', result: 'correct', paper_ids: ['p1'], student_answer: 'A', standard_answer: 'A' },
            { question_no: '2', result: 'wrong', paper_ids: ['p2'], student_answer: '5', standard_answer: '3' },
          ],
        },
      },
    ],
  })
}

const workbench = sheet => sheet.root.byClass('review-workbench')
const paperPane = sheet => sheet.root.find(el => el.getAttribute('aria-label') === '完整学生答题卡')
const pager = sheet => paperPane(sheet).byClass('review-pager')
const pagerButton = (sheet, name) => pager(sheet).find(el => el.getAttribute('aria-label') === name)
const detailCards = sheet => sheet.root.allByClass('review-detail-card')
const radioGroups = sheet => detailCards(sheet).map(card => card.byTag('el-radio-group')[0])

test('answer-sheet draft uses the full protected student pages beside grading details', async (t) => {
  const sheet = await openAnswerSheet(t, { session: reviewed() })
  assert.ok(workbench(sheet))
  assert.equal(paperPane(sheet).tagName, 'section')
  assert.equal(pager(sheet).byTag('span')[0].text, '1 / 2')
  assert.ok(pagerButton(sheet, '上一页').hasAttribute('disabled'))
  assert.equal(pagerButton(sheet, '下一页').hasAttribute('disabled'), false)

  // The page image is fetched through the authenticated API, never a public path.
  assert.equal(sheet.count('get', `${S1}/students/p1/image`), 1)
  const [first] = sheet.images.urls()
  const img = () => paperPane(sheet).find(el => el.tagName === 'img')
  assert.equal(img().getAttribute('src'), first)
  assert.equal(img().getAttribute('alt'), '张三答题卡第 1 页')
  assert.deepEqual(detailCards(sheet).map(card => card.classList.contains('active')), [true, false])

  await pagerButton(sheet, '下一页').dispatch('click')
  await flush()
  assert.equal(pager(sheet).byTag('span')[0].text, '2 / 2')
  assert.equal(sheet.count('get', `${S1}/students/p2/image`), 1)
  assert.ok(sheet.images.revoked.includes(first))
  assert.match(paperPane(sheet).byClass('student-paper-meta').text, /张三-2\.jpg/)
  assert.ok(pagerButton(sheet, '下一页').hasAttribute('disabled'))
  assert.deepEqual(detailCards(sheet).map(card => card.classList.contains('active')), [false, true])

  // A question's details jump to the page it was answered on.
  await detailCards(sheet)[0].dispatch('click')
  await flush()
  assert.equal(pager(sheet).byTag('span')[0].text, '1 / 2')
  assert.equal(sheet.count('get', `${S1}/students/p1/image`), 2)
  assert.deepEqual(detailCards(sheet).map(card => card.byTag('strong')[0].text), ['第 1 题', '第 2 题'])
})

test('persisted grading drafts restore the review workbench and gate archive on review count', async (t) => {
  const pending = reviewed({
    drafts: [
      { student_id: 7, paper_ids: ['p1', 'p2'], result: { student_name: '张三', needs_review_count: 0, details: [{ question_no: '1', result: 'correct', paper_ids: ['p1'] }] } },
      { student_id: 8, paper_ids: ['p3'], result: { student_name: '李四', needs_review_count: 1, details: [{ question_no: '1', result: 'needs_review', paper_ids: ['p3'] }] } },
    ],
  })
  const original = structuredClone(pending.grouping_decisions)
  const sheet = await openAnswerSheet(t, { session: pending })

  // Reloading the page brings the drafts back, one tab per student.
  const tabs = () => sheet.root.byClass('review-student-tabs').byTag('button')
  assert.deepEqual(tabs().map(tab => tab.text), ['张三', '李四'])
  assert.ok(sheet.button('确认全部并归档').hasAttribute('disabled'), '还有待复核题目时不能归档')

  // The page edits its own copy, never the session snapshot it restored from.
  radioGroups(sheet)[0].handlers['update:modelValue']('wrong')
  await flush()
  assert.deepEqual(pending.grouping_decisions, original)

  // Switching student shows that student's pages and releases the previous image.
  const [first] = sheet.images.urls()
  await tabs()[1].dispatch('click')
  await flush()
  assert.ok(tabs()[1].classList.contains('active'))
  assert.equal(sheet.count('get', `${S1}/students/p3/image`), 1)
  assert.ok(sheet.images.revoked.includes(first))
  assert.match(sheet.root.byClass('review-counts').text, /待复核 1/)
  sheet.close()

  const done = await openAnswerSheet(t, { session: reviewed() })
  assert.equal(done.button('确认全部并归档').hasAttribute('disabled'), false)
  assert.equal(done.root.byClass('review-counts').text.includes('待复核'), false)
  done.close()

  // Without drafts there is nothing to archive: the student stage shows instead.
  const empty = await openAnswerSheet(t, { session: reviewed({ drafts: [] }) })
  assert.ok(!empty.root.byClass('review-workbench'))
  assert.ok(!empty.button('确认全部并归档'))
})

test('student answer-sheet image API uses an authenticated blob URL, not a public path', async (t) => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  const image = new Blob(['png'], { type: 'image/png' })
  client.respond(() => ({ data: image }))
  const created = []
  t.mock.method(URL, 'createObjectURL', (blob) => {
    created.push(blob)
    return 'blob:student-page'
  })

  const url = await client.api.answerSheet.studentPageImage('s 1', 'paper/1')

  assert.equal(url, 'blob:student-page')
  assert.deepEqual(created, [image])
  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'get')
  assert.equal(request.url, '/answer-sheet/sessions/s%201/students/paper%2F1/image')
  assert.equal(request.responseType, 'blob')
  assert.equal(request.headers.Authorization, 'Bearer teacher-token')
})

test('teacher can save complete per-question verdicts with review revision', async (t) => {
  const draft = revision => reviewed({
    revision,
    drafts: [{
      student_id: 7,
      paper_ids: ['p1', 'p2'],
      result: {
        student_name: '张三',
        needs_review_count: 1,
        details: [
          { question_no: '1', result: 'correct', paper_ids: ['p1'] },
          { question_number: '2', result: 'needs_review', paper_ids: ['p2'] },
        ],
      },
    }],
  })
  let conflict = false
  const sheet = await openAnswerSheet(t, {
    session: draft(3),
    respond: (config) => {
      if (config.method !== 'post' || config.url !== `${S1}/grading/decision`) return undefined
      if (conflict) return { status: 409, data: { detail: '批阅草稿已被更新' } }
      sheet.sessions.set('s1', draft(body(config).review_revision + 1))
      return { data: { session_id: 's1' } }
    },
  })
  for (const card of detailCards(sheet)) {
    const decision = card.byClass('teacher-decision')
    assert.equal(decision.byTag('span')[0].text, '人工判定')
    assert.deepEqual(decision.byTag('el-radio-button').map(el => [el.value, el.text]), [['correct', '正确'], ['partial', '部分正确'], ['wrong', '错误']])
  }

  // Every question needs a verdict first.
  await sheet.click('保存人工判定')
  assert.deepEqual(sheet.messages(), [['warning', '请先为全部题目选择正确、部分正确或错误']])
  assert.deepEqual(sheet.writes(), [])

  radioGroups(sheet)[1].handlers['update:modelValue']('partial')
  detailCards(sheet)[1].byTag('el-input')[0].handlers['update:modelValue']('步骤缺失')
  await flush()
  await sheet.click('保存人工判定')
  const [saved] = sheet.client.requests.filter(config => config.url === `${S1}/grading/decision`)
  assert.deepEqual(body(saved), {
    student_id: 7,
    review_revision: 3,
    decisions: [
      { question_no: '1', result: 'correct', note: '' },
      { question_no: '2', result: 'partial', note: '步骤缺失' },
    ],
  })
  assert.ok(sheet.messages().some(([type, message]) => type === 'success' && message === '人工判定已保存'))
  // The saved review is re-read from the server, so the next save carries its new revision.
  assert.equal(sheet.count('get', S1), 2)
  assert.ok(workbench(sheet), '保存后仍停留在批阅工作台')
  radioGroups(sheet)[1].handlers['update:modelValue']('wrong')
  await flush()
  await sheet.click('保存人工判定')
  assert.deepEqual(sheet.client.requests.filter(config => config.url === `${S1}/grading/decision`).map(config => body(config).review_revision), [3, 4])

  // A stale revision loads the server's latest draft instead of overwriting it.
  conflict = true
  radioGroups(sheet)[1].handlers['update:modelValue']('wrong')
  await flush()
  await sheet.click('保存人工判定')
  assert.equal(sheet.count('get', S1), 4)
  assert.ok(sheet.messages().some(([type, message]) => type === 'warning' && message === '批阅草稿已更新，已载入服务器最新版本'))
})

test('grading decision API posts the full verdict payload to the session decision endpoint', async () => {
  const client = await loadApiClient()
  const payload = {
    student_id: 7,
    review_revision: 3,
    decisions: [
      { question_no: '1', result: 'correct', note: '' },
      { question_no: '2', result: 'partial', note: '步骤缺失' },
    ],
  }
  client.respond(() => ({ data: { session_id: 's 1', review_revision: 4 } }))

  assert.deepEqual(await client.api.answerSheet.gradingDecision('s 1', payload), { session_id: 's 1', review_revision: 4 })
  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'post')
  assert.equal(request.url, '/answer-sheet/sessions/s%201/grading/decision')
  assert.deepEqual(JSON.parse(request.data), payload)
})

test('formal grading record reopens its protected archived answer-sheet session', async (t) => {
  const records = [{ id: 1, student_name: '张三' }, { id: 2, student_name: '李四' }, { id: 3, student_name: '王五' }]
  const details = {
    1: { student_name: '张三', source_assets: { source: 'answer_sheet', session_id: 's 9', available: true } },
    2: { student_name: '李四', source_assets: { source: 'answer_sheet', session_id: 's 8', available: false } },
    3: { student_name: '王五' },
  }
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  client.respond((config) => {
    if (config.url === '/grade/results') return { data: { items: records, total: records.length } }
    const match = /^\/grade\/result\/(\d+)$/.exec(config.url)
    return { data: match ? details[match[1]] : {} }
  })
  const { default: GradeRecords } = await import('../src/views/GradeRecords.vue?sfc')
  const page = await mountComponent(t, GradeRecords, { path: '/grade-records' })
  const openRecord = async (index) => {
    page.root.byTag('el-table')[0].handlers.rowClick(records[index], { type: 'default' }, { target: {} })
    await flush()
    return page.root.find(el => el.tagName === 'el-dialog')
  }
  const sourceButton = dialog => dialog.find(el => el.tagName === 'el-button' && el.text === '查看完整答题卡原卷')
  const expiredNotice = dialog => dialog.find(el => el.tagName === 'el-alert' && el.classList.contains('source-expired'))

  // An expired source says what is kept; a record from another flow mentions no source.
  let dialog = await openRecord(1)
  assert.ok(!sourceButton(dialog))
  assert.equal(expiredNotice(dialog).getAttribute('title'), '原卷已按隐私保留策略清理；逐题批改结果、错题、得分和正确率仍保留。')
  dialog = await openRecord(2)
  assert.ok(!sourceButton(dialog))
  assert.ok(!expiredNotice(dialog))

  dialog = await openRecord(0)
  assert.ok(!expiredNotice(dialog))
  // The answer-sheet route loads its page lazily: wait for the navigation the page started.
  const navigations = []
  const push = page.router.push
  t.mock.method(page.router, 'push', (...args) => {
    const navigation = push(...args)
    navigations.push(navigation)
    return navigation
  })
  await sourceButton(dialog).dispatch('click')
  assert.equal(navigations.length, 1)
  await navigations[0]
  const route = page.router.currentRoute.value
  assert.equal(route.name, 'answer-sheet')
  assert.deepEqual(route.query, { session: 's 9' })
  page.unmount()

  // The answer sheet opens the linked session ahead of the one stored on this device.
  const sheet = await openAnswerSheet(t, {
    path: '/answer-sheet?session=s%209',
    saved: 'unfinished',
    sessions: [reviewed({ status: 'archived' }), { ...reviewed({ status: 'archived' }), session_id: 's 9' }],
  })
  assert.equal(sheet.count('get', '/answer-sheet/sessions/s%209'), 1)
  assert.equal(sheet.count('get', '/answer-sheet/sessions/unfinished'), 0)
  assert.ok(sheet.alerts().includes('已打开该条正式记录关联的答题卡原卷。'))
  assert.ok(sheet.root.byClass('session-meta').byTag('span').some(span => span.text === '会话 s 9'))
  sheet.close()

  // A linked session that cannot open leaves the device's own unfinished session alone.
  const missing = await openAnswerSheet(t, { path: '/answer-sheet?session=gone', saved: 'unfinished' })
  assert.ok(missing.alerts().includes('答题卡会话无法恢复，可能已删除或不属于当前账号。'))
  assert.equal(missing.client.localStorage.getItem(SESSION_KEY), 'unfinished')
})
