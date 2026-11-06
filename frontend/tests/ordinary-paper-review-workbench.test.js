import assert from 'node:assert/strict'
import test from 'node:test'
import { memoryDocument } from './helpers/memoryDom.js'
import { mountUpload, paperFile, TEACHER } from './helpers/uploadPage.js'
import { renderAnswerLatex } from '../src/utils/latex.js'

// The 原卷与批阅结果 workbench of the real Upload page, opened from a review
// the page restores (or has just graded): the authenticated review image per
// paper, paging, the answer-sheet style verdict grid and the decisions it
// saves. Layout rules (overflow, object-fit, grid tracks, widths) need a
// browser's layout engine and are not checked here.

const BATCH = 'b-review'
const REVIEW_POINTER = `mathagent_review_batch_id:${TEACHER.id}`
const ANONYMOUS_REVIEW_POINTER = `mathagent_review_batch_id:anonymous:${TEACHER.id}`
const REVIEW = `GET /api/batch-grade/review/${BATCH}`
const imageRoute = paperId => `GET /api/batch-grade/${BATCH}/paper/${paperId}/image`
const PREVIEW = 'POST /api/batch-grade/review/image-preview'
const DECISION = 'POST /api/batch-grade/review/decision'
const EXPORT_IMAGES = 'POST /api/batch-grade/review/export/images'

// 甲's two pages, graded as one review group. Question 3 is listed with row
// p1's results, but the server places it on page 2 (paper p2); questions 12
// and 13 carry no page reference and stay on their own row's page.
function groupRows({ disposition = 'active' } = {}) {
  return [
    {
      paper_id: 'p1',
      review_group_id: 'g1',
      student_name: '甲',
      filename: '甲-1.png',
      page_index: 0,
      pages: [{ paper_id: 'p1', page_index: 0, filename: '甲-1.png' }, { paper_id: 'p2', page_index: 1, filename: '甲-2.png' }],
      disposition,
      correct: 1,
      wrong: 1,
      total: 2,
      details: [
        { question_no: '3', question_type: 'choice', paper_id: 'p2', page_index: 1, student_answer: 'A', correct_answer: 'B', result: 'wrong' },
        { question_no: '12', question_type: 'fill', student_answer: '$x^2$', correct_answer: '$x^2$', result: 'correct' },
      ],
    },
    {
      paper_id: 'p2',
      review_group_id: 'g1',
      student_name: '甲',
      filename: '甲-2.png',
      page_index: 1,
      disposition,
      needs_review: 1,
      total: 1,
      // No question_type: the workbench treats it as a 简答题.
      details: [
        { question_no: '13', question: '题干X：证明两个三角形全等', student_answer: '$\\frac{1}{2}$', correct_answer: '$\\sqrt{2}$', result: 'needs_review' },
      ],
    },
  ]
}

function reviewPayload(rows, extra = {}) {
  return { batch_id: BATCH, status: 'review_ready', results: rows, summary: { total: rows.length }, review_revision: 3, ...extra }
}

const pageImage = name => new Blob([`review image of ${name}`], { type: 'image/png' })

// The page restored from the saved review pointer, with the server's review
// images for both papers.
async function restoredReview(t, { rows = groupRows(), payload = {}, responses = {}, anonymous = false, ...options } = {}) {
  return mountUpload(t, {
    props: anonymous ? { anonymous: true } : {},
    localStorage: { [anonymous ? ANONYMOUS_REVIEW_POINTER : REVIEW_POINTER]: BATCH },
    responses: {
      [REVIEW]: reviewPayload(rows, payload),
      [`GET /api/batch-grade/report/${BATCH}`]: { status: 'ready', text: '', stats: {} },
      [imageRoute('p1')]: pageImage('p1'),
      [imageRoute('p2')]: pageImage('p2'),
      [PREVIEW]: config => ({ status: 200, data: pageImage(`preview of ${JSON.parse(config.data).paper_id}`) }),
      ...responses,
    },
    ...options,
  })
}

function workbench(upload) {
  return upload.page.root.find(el => el.tagName === 'el-dialog' && String(el.attributes.title || '').startsWith('原卷与批阅结果'))
}

async function openWorkbench(upload, row = 0) {
  const buttons = upload.page.root.findAll(el => el.tagName === 'button' && el.text === '原卷与批阅结果')
  await buttons[row].dispatch('click')
  await upload.settle()
  const dialog = workbench(upload)
  assert.equal(dialog?.attributes['data-open'], 'true', 'the workbench is open')
  return dialog
}

const pager = dialog => dialog.byClass('manual-review-pager')
const pageShown = dialog => pager(dialog).byTag('strong')[0].text
const pagerButton = (dialog, text) => pager(dialog).byText('button', text)
const shownImage = dialog => dialog.byClass('manual-review-image-stage').byTag('img')[0]?.attributes.src ?? null
const compactCells = (dialog, kind) => dialog.byClass('manual-review-sheet-compact').allByClass(`manual-review-sheet-${kind}`)

// Object URLs the page gives up; the real URL.revokeObjectURL still runs.
function recordRevokedUrls(t) {
  const revoke = URL.revokeObjectURL
  const revoked = []
  URL.revokeObjectURL = (url) => { revoked.push(url); return revoke.call(URL, url) }
  t.after(() => { URL.revokeObjectURL = revoke })
  return revoked
}

// Files the page hands to the browser: it clicks a download link attached to
// the document body, as triggerBlobDownload and the per-page download do.
function recordDownloads(t, upload) {
  const downloads = []
  const onClick = (event) => {
    if (event.target.tagName === 'a') downloads.push({ href: event.target.href, download: event.target.download })
  }
  memoryDocument.body.addEventListener('click', onClick)
  t.after(() => memoryDocument.body.removeEventListener('click', onClick))
  // triggerBlobDownload makes its object URL through window.URL.
  upload.page.win.URL = URL
  return downloads
}

const verdictSelect = (dialog, questionNo) => (
  dialog.find(el => el.tagName === 'select' && el.attributes['aria-label'] === `第${questionNo}题最终判定`)
)

// Pick a verdict as a teacher does. The select has both v-model and @change:
// in a browser v-model's change listener, added when the element is created,
// runs before the template's @change listener, so @change sees the new value.
// helpers/memoryDom.js runs `on*` handlers before listeners, so the change is
// delivered here in the browser's order.
async function chooseVerdict(upload, dialog, questionNo, verdict) {
  const select = verdictSelect(dialog, questionNo)
  assert.ok(select, `question ${questionNo} has a verdict select`)
  assert.equal(select.disabled, false, `question ${questionNo} can be decided`)
  for (const option of select.options) option.selected = String(option._value ?? option.value) === verdict
  const event = { type: 'change', target: select, currentTarget: select, stopPropagation() {}, preventDefault() {} }
  for (const listener of select.listeners.get('change') ?? []) listener(event)
  await select.handlers.change?.(event)
  await upload.settle()
}

// renderLatex decodes HTML entities the browser's way, through a <textarea>
// (innerHTML in, value out); helpers/memoryDom.js parses no HTML, so this
// stand-in does that one decoding while the test runs.
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' }
function decodeEntitiesThroughTextareas(t) {
  const createElement = memoryDocument.createElement
  const withTextareas = (tag) => {
    const el = createElement(tag)
    if (tag !== 'textarea') return el
    let html = ''
    Object.defineProperty(el, 'innerHTML', { get: () => html, set: (value) => { html = String(value) }, configurable: true })
    Object.defineProperty(el, 'value', {
      configurable: true,
      get: () => html.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, name) => {
        if (name[0] === '#') return String.fromCodePoint(Number.parseInt(name.slice(name[1].toLowerCase() === 'x' ? 2 : 1), name[1].toLowerCase() === 'x' ? 16 : 10))
        return ENTITIES[name.toLowerCase()] ?? entity
      }),
    })
    return el
  }
  memoryDocument.createElement = withTextareas
  t.after(() => { if (memoryDocument.createElement === withTextareas) memoryDocument.createElement = createElement })
}

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

test('ordinary paper review uses authenticated full-page workbench', async (t) => {
  const revoked = recordRevokedUrls(t)
  const upload = await restoredReview(t)
  const downloads = recordDownloads(t, upload)
  const dialog = await openWorkbench(upload)
  assert.equal(dialog.attributes.title, '原卷与批阅结果 · 甲')

  // Page 1 of 甲's two pages: the server-rendered review image, fetched with
  // the teacher's login. No marks are drawn over it in the browser.
  const [first] = upload.requests(imageRoute('p1'))
  assert.equal(first.params.variant, 'review')
  assert.equal(first.responseType, 'blob')
  assert.equal(first.headers.Authorization, 'Bearer teacher-token')
  const firstUrl = shownImage(dialog)
  assert.match(firstUrl, /^blob:/)
  assert.equal(pageShown(dialog), '1 / 2')
  assert.equal(pagerButton(dialog, '上一页').disabled, true)
  assert.equal(pagerButton(dialog, '下一页').disabled, false)
  assert.equal(Boolean(dialog.byClass('manual-review-mark-layer')), false, 'no marks are drawn in the browser')
  assert.ok(dialog.byText('button', '下载批阅痕迹图'))

  // 下一页 shows page 2 and gives up page 1's image.
  await pagerButton(dialog, '下一页').dispatch('click')
  await upload.settle()
  assert.equal(pageShown(dialog), '2 / 2')
  assert.equal(upload.requests(imageRoute('p2')).length, 1)
  assert.deepEqual(revoked, [firstUrl])
  const secondUrl = shownImage(dialog)
  assert.notEqual(secondUrl, firstUrl)
  assert.equal(pagerButton(dialog, '下一页').disabled, true)

  // A teacher verdict that differs from the model's previews the page with
  // the teacher's decisions for that paper only.
  await chooseVerdict(upload, dialog, '13', 'partial')
  assert.deepEqual(upload.requests(PREVIEW).map(config => JSON.parse(config.data)), [
    { batch_id: BATCH, paper_id: 'p2', decisions: [{ question_no: '13', result: 'partial' }] },
  ])
  assert.equal(upload.requests(PREVIEW)[0].responseType, 'blob')
  assert.deepEqual(revoked, [firstUrl, secondUrl])
  const previewUrl = shownImage(dialog)
  assert.ok(previewUrl && ![firstUrl, secondUrl].includes(previewUrl))

  // 下载批阅痕迹图 saves the image being shown.
  await dialog.byText('button', '下载批阅痕迹图').dispatch('click')
  assert.deepEqual(downloads, [{ href: previewUrl, download: '甲_第2页_批阅痕迹.png' }])

  // 上一页 goes back to page 1, whose own image carries no teacher decision.
  await pagerButton(dialog, '上一页').dispatch('click')
  await upload.settle()
  assert.equal(pageShown(dialog), '1 / 2')
  assert.equal(upload.requests(imageRoute('p1')).length, 2)
  assert.equal(upload.requests(PREVIEW).length, 1)

  // Closing the workbench gives up the image it showed last.
  const lastUrl = shownImage(dialog)
  await dialog.handlers['update:modelValue'](false)
  await upload.settle()
  assert.equal(revoked.at(-1), lastUrl)
  assert.deepEqual(upload.client.messages.filter(message => message.type === 'error'), [])
})

test('batch review image export remains available after archive and outside precise mode', async (t) => {
  for (const scenario of ['archived, simple mode', 'anonymous page']) {
    const anonymous = scenario === 'anonymous page'
    const packing = deferred()
    const upload = await restoredReview(t, {
      timers: true,
      anonymous,
      rows: anonymous
        ? [{ paper_id: 'p1', student_name: '第 1 页', disposition: 'active', correct: 1, total: 1, details: [{ question_no: '1', result: 'correct' }] }]
        : groupRows({ disposition: 'archived' }),
      payload: anonymous ? { identity_mode: 'anonymous' } : {},
      responses: { [EXPORT_IMAGES]: () => packing.promise },
    })
    const downloads = recordDownloads(t, upload)
    if (!anonymous) {
      await upload.select(upload.page.root.find(el => el.id === 'batch-grade-mode'), 'simple')
      assert.match(upload.text(), /本批审阅已结束/)
      assert.equal(Boolean(upload.button('导出老师汇总')), false, 'simple mode has no precise-only exports')
      assert.equal(upload.button('归档已完成结果').disabled, true, 'the batch is archived')
    }

    const exportButton = upload.button('批量下载批阅痕迹图')
    assert.ok(exportButton, `${scenario}: the batch export is offered`)
    assert.equal(exportButton.disabled, false, `${scenario}: the batch export is enabled`)
    await upload.click('批量下载批阅痕迹图')
    const [request] = upload.requests(EXPORT_IMAGES)
    assert.deepEqual(JSON.parse(request.data), { batch_id: BATCH })
    assert.equal(request.timeout, 600000)
    assert.equal(request.responseType, 'blob')
    assert.equal(upload.button('正在打包批阅图…')?.disabled, true, `${scenario}: busy while packing`)

    packing.resolve({ status: 200, data: new Blob(['zip'], { type: 'application/zip' }) })
    await upload.settle()
    assert.equal(upload.button('批量下载批阅痕迹图').disabled, false)
    assert.deepEqual(downloads.map(item => item.download), ['批阅痕迹图.zip'])
    assert.match(downloads[0].href, /^blob:/)

    if (!anonymous) {
      // The workbench's per-page download saves the image it shows.
      const dialog = await openWorkbench(upload)
      await dialog.byText('button', '下载批阅痕迹图').dispatch('click')
      assert.deepEqual(downloads[1], { href: shownImage(dialog), download: '甲_第1页_批阅痕迹.png' })
    }
    assert.deepEqual(upload.client.messages.filter(message => message.type === 'error'), [])
    upload.unmount()
  }
})

test('review cards omit confirmed question text and enlarge the paper workspace', async (t) => {
  const upload = await restoredReview(t)
  decodeEntitiesThroughTextareas(t)
  const dialog = await openWorkbench(upload)
  assert.doesNotMatch(dialog.text, /题干X|证明两个三角形全等/)
  assert.equal(Boolean(dialog.find(el => el.tagName === 'span' && el.text === '题目')), false, 'no 题目 field')

  const [studentAnswer, referenceAnswer] = dialog.byClass('manual-review-solve-evidence').children
  assert.equal(studentAnswer.byTag('span')[0].text, '学生答案')
  assert.equal(studentAnswer.byTag('p')[0].innerHTML, renderAnswerLatex('$\\frac{1}{2}$'))
  assert.equal(referenceAnswer.byTag('span')[0].text, '参考答案')
  assert.equal(referenceAnswer.byTag('p')[0].innerHTML, renderAnswerLatex('$\\sqrt{2}$'))
  assert.match(studentAnswer.byTag('p')[0].innerHTML, /class="katex"/)
  // Compact rows render their answers as LaTeX too.
  const [, fillAnswer] = compactCells(dialog, 'answer')
  assert.equal(fillAnswer.byTag('span')[0].innerHTML, renderAnswerLatex('$x^2$'))
})

test('review cards use compact choice/fill rows and can hide the paper for a full-width review', async (t) => {
  const upload = await restoredReview(t)
  let dialog = await openWorkbench(upload)

  // Choice and fill questions share one answer-sheet grid; a question without
  // a type is a 简答题 with its own row.
  const compact = dialog.byClass('manual-review-sheet-compact')
  assert.equal(compact.byClass('manual-review-sheet-heading').byTag('strong')[0].text, '选择 / 填空')
  assert.deepEqual(compactCells(dialog, 'number').map(cell => cell.text), ['3', '12'])
  assert.equal(compact.byClass('manual-review-sheet-grid').style['--sheet-columns'], 2)
  const solveRows = dialog.byClass('manual-review-sheet-solve').allByClass('manual-review-solve-row')
  assert.deepEqual(solveRows.map(row => row.byClass('manual-review-solve-number').text), ['第 13 题'])
  assert.equal(solveRows[0].byClass('manual-review-type').text, '简答题')
  // Answer cells on the page being shown (page 1: question 12) are marked.
  assert.equal(compactCells(dialog, 'answer').filter(cell => cell.classList.contains('is-current-page')).length, 1)
  assert.equal(compactCells(dialog, 'answer')[1].classList.contains('is-current-page'), true)

  const legend = dialog.find(el => el.attributes['aria-label'] === '判定符号说明')
  assert.deepEqual(legend.children.map(item => item.text), ['·未识别', '✓对', '△半对', '✗错'])
  assert.deepEqual(legend.byTag('b').map(symbol => symbol.className), [
    'manual-review-legend-symbol is-unread',
    'manual-review-legend-symbol is-correct',
    'manual-review-legend-symbol is-partial',
    'manual-review-legend-symbol is-wrong',
  ])
  for (const questionNo of ['3', '12', '13']) {
    assert.deepEqual(verdictSelect(dialog, questionNo).options.map(option => [option._value, option.text]), [
      ['', '·'], ['correct', '✓'], ['partial', '△'], ['wrong', '✗'],
    ], `question ${questionNo}`)
  }

  // 关闭原卷 hides the paper and gives the review list the full width.
  const toggle = () => dialog.byClass('manual-review-paper-toggle')
  const workspace = () => dialog.byClass('manual-review-workspace')
  assert.equal(toggle().text, '关闭原卷')
  assert.equal(dialog.classList.contains('manual-review-dialog'), true)
  assert.equal(dialog.classList.contains('is-paper-hidden'), false)
  assert.equal(workspace().classList.contains('is-paper-hidden'), false)
  await toggle().dispatch('click')
  await upload.settle()
  assert.equal(toggle().text, '显示原卷')
  assert.equal(dialog.classList.contains('manual-review-dialog'), true)
  assert.equal(dialog.classList.contains('is-paper-hidden'), true)
  assert.equal(workspace().classList.contains('is-paper-hidden'), true)
  await toggle().dispatch('click')
  await upload.settle()
  assert.equal(toggle().text, '关闭原卷')
  assert.equal(dialog.classList.contains('is-paper-hidden'), false)

  // Opening the workbench again shows the paper again.
  await toggle().dispatch('click')
  await upload.settle()
  await dialog.handlers['update:modelValue'](false)
  await upload.settle()
  dialog = await openWorkbench(upload)
  assert.equal(dialog.classList.contains('is-paper-hidden'), false)
  assert.equal(dialog.byClass('manual-review-paper-toggle').text, '关闭原卷')
})

test('question cards jump only by explicit backend page reference', async (t) => {
  const upload = await restoredReview(t)
  const dialog = await openWorkbench(upload)
  assert.equal(pageShown(dialog), '1 / 2')
  const jump = async (cell, expectedPage, paperId) => {
    const before = upload.requests(imageRoute(paperId)).length
    await cell.dispatch('click')
    await upload.settle()
    assert.equal(pageShown(dialog), expectedPage)
    assert.equal(upload.requests(imageRoute(paperId)).length, before + 1, `shows ${paperId}`)
  }
  const [three, twelve] = compactCells(dialog, 'number')
  // Question 3 is on page 2 because the server says so, although it is listed
  // with page 1's results and its number is the lowest.
  await jump(three, '2 / 2', 'p2')
  // Question 12 has no reference of its own: it stays on its row's page 1.
  await jump(twelve, '1 / 2', 'p1')
  await jump(dialog.byClass('manual-review-solve-number'), '2 / 2', 'p2')
  await jump(compactCells(dialog, 'answer')[1], '1 / 2', 'p1')
})

// A grading run through the page: the template is saved, two pages of 甲 are
// recognized, and the grading stream reports two per-page results.
const GRADE_BATCH = 'b-fresh'
const RECOGNIZED = [
  ['paper', { batch_id: GRADE_BATCH, paper_id: 'p1', filename: 'a.png', identity: { status: 'matched', student_id: 11, student_name: '甲' } }],
  ['paper', { batch_id: GRADE_BATCH, paper_id: 'p2', filename: 'b.png', identity: { status: 'matched', student_id: 11, student_name: '甲' } }],
  ['done', { batch_id: GRADE_BATCH, can_grade: true, uploaded_papers: 2, total_papers: 2, succeeded_papers: 2, questions: [{ question_no: '3' }] }],
]
const GRADED = [
  ['result', { paper_id: 'p1', student_name: '流式行一', correct: 0, total: 1 }],
  ['result', { paper_id: 'p2', student_name: '流式行二', correct: 0, total: 1 }],
  ['done', { total: 2, review_revision: 1 }],
]

test('fresh grading reloads authoritative persisted review contracts', async (t) => {
  const persisted = groupRows().map(row => ({ ...row }))
  const upload = await mountUpload(t, {
    streams: {
      'POST /api/batch-grade/recognize': RECOGNIZED,
      'POST /api/batch-grade/confirm': GRADED,
    },
    responses: {
      [`GET /api/batch-grade/review/${GRADE_BATCH}`]: { ...reviewPayload(persisted), batch_id: GRADE_BATCH, review_revision: 2 },
      [`GET /api/batch-grade/report/${GRADE_BATCH}`]: { status: 'ready', text: '', stats: {} },
      [`GET /api/batch-grade/${GRADE_BATCH}/paper/p1/image`]: pageImage('p1'),
      [`GET /api/batch-grade/${GRADE_BATCH}/paper/p2/image`]: pageImage('p2'),
      [DECISION]: config => ({ status: 200, data: { ok: true, review_revision: 3, review: { paper_id: JSON.parse(config.data).paper_id, disposition: 'active', result: {} } } }),
    },
  })
  // The template editor saves the teacher-confirmed template and leaves its id.
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.settle()
  await upload.choosePapers([paperFile('a.png'), paperFile('b.png')])
  assert.equal(upload.fetches('POST /api/batch-grade/confirm').length, 1, 'grading ran')
  assert.deepEqual(JSON.parse(upload.fetches('POST /api/batch-grade/confirm')[0].init.body).batch_id, GRADE_BATCH)

  // The page reloads the saved review once and shows it, not the stream rows.
  assert.equal(upload.requests(`GET /api/batch-grade/review/${GRADE_BATCH}`).length, 1)
  const names = upload.page.root.allByClass('review-row__name').map(name => name.byTag('strong')[0].text)
  assert.deepEqual(names, ['甲', '甲'])
  assert.doesNotMatch(upload.text(), /流式行/)
  assert.equal(upload.storage()[REVIEW_POINTER], GRADE_BATCH)

  // The saved group and page references are there at once: the workbench
  // shows both of 甲's pages and question 3 on page 2.
  const dialog = await openWorkbench(upload)
  assert.equal(pageShown(dialog), '1 / 2')
  await compactCells(dialog, 'number')[0].dispatch('click')
  await upload.settle()
  assert.equal(pageShown(dialog), '2 / 2')
  assert.equal(upload.requests(`GET /api/batch-grade/${GRADE_BATCH}/paper/p2/image`).length, 1)

  // Decisions use the saved review's revision.
  await chooseVerdict(upload, dialog, '13', 'correct')
  await upload.click('保存该学生判定')
  assert.equal(JSON.parse(upload.requests(DECISION)[0].data).review_revision, 2)
  assert.deepEqual(upload.client.messages.filter(message => message.type === 'error'), [])
})

test('multi-page teacher decisions remain audited per original paper id', async (t) => {
  let revision = 3
  const upload = await restoredReview(t, {
    responses: {
      [DECISION]: (config) => {
        const body = JSON.parse(config.data)
        revision += 1
        return {
          status: 200,
          data: {
            ok: true,
            review_revision: revision,
            review: { paper_id: body.paper_id, disposition: 'active', result: { correct_count: body.decisions.length, total: body.decisions.length, details: body.decisions } },
          },
        }
      },
    },
  })
  const dialog = await openWorkbench(upload)
  const save = () => upload.button('保存该学生判定')
  assert.equal(save().disabled, true, 'question 13 has no verdict yet')
  await chooseVerdict(upload, dialog, '13', 'partial')
  await chooseVerdict(upload, dialog, '3', 'correct')
  assert.equal(save().disabled, false)
  await upload.click('保存该学生判定')

  // One audited decision per original paper, each with only its own
  // questions; the second carries the revision the first one returned.
  assert.deepEqual(upload.requests(DECISION).map(config => JSON.parse(config.data)), [
    {
      batch_id: BATCH,
      paper_id: 'p2',
      review_revision: 3,
      decisions: [{ question_no: '3', result: 'correct', note: '' }, { question_no: '13', result: 'partial', note: '' }],
    },
    {
      batch_id: BATCH,
      paper_id: 'p1',
      review_revision: 4,
      decisions: [{ question_no: '12', result: 'correct', note: '' }],
    },
  ])
  assert.equal(workbench(upload).attributes['data-open'], 'false')
  assert.deepEqual(upload.client.messages, [{ type: 'success', message: '已保存该学生的人工判定' }])

  // The next decision starts from the latest revision.
  await openWorkbench(upload)
  await upload.click('保存该学生判定')
  assert.deepEqual(upload.requests(DECISION).slice(2).map(config => JSON.parse(config.data).review_revision), [5, 6])
})
