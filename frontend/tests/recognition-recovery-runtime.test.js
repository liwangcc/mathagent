import assert from 'node:assert/strict'
import test from 'node:test'
import { jsonResponse, sseResponse } from './helpers/memoryFetch.js'
import { formFields, HEALTHY_SERVICE, liveSse, mountUpload, paperFile, TEACHER } from './helpers/uploadPage.js'

// Recognition recovery on the real 批改 page (Upload.vue): what the page does
// when a recognition stream is cut, the batch lock is busy, OCR fails for good
// or only for now, a saved batch is reopened or abandoned, and how it then
// reaches (or refuses) the grading stream. Every test mounts the page, drives
// it the way a teacher would (choosing pages, clicking its buttons, letting
// its timers run) and checks what it renders, the requests it sends and the
// batch pointers it keeps in localStorage.

const BATCH_ID = 'c609-current-batch'
const OLDER_REVIEW_ID = '1aba-older-review'
const WAITING = '等待教师客户端加速节点完成图片处理'
const TEMPLATE_ID = 'synthetic-template'

// The teacher's batch pointers, per identity mode.
function pointerKeys(anonymous = true) {
  const mode = anonymous ? ':anonymous' : ''
  return {
    review: `mathagent_review_batch_id${mode}:${TEACHER.id}`,
    orientation: `mathagent_orientation_batch_id${mode}:${TEACHER.id}`,
  }
}
const REVIEW_KEY = 'mathagent_review_batch_id:anonymous:7'
const ORIENTATION_KEY = 'mathagent_orientation_batch_id:anonymous:7'

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const RESUME = `POST /api/batch-grade/${BATCH_ID}/recognize/resume`
const CONFIRM = 'POST /api/batch-grade/confirm'
const AVAILABILITY = 'GET /api/batch-grade/availability'
const EXCLUDE = `POST /api/batch-grade/${BATCH_ID}/paper/paper-22/exclude`
const orientationRoute = id => `GET /api/batch-grade/${id}/orientation`
const reviewRoute = id => `GET /api/batch-grade/review/${id}`

const RESUME_BUTTON = '继续处理未完成试卷'
const CONTINUE_BUTTON = '继续开始批阅'
const NEW_BATCH_BUTTON = '开始新批次'
const SERVICE_FAULT_NOTICE = '批改链路当前故障，服务恢复后可继续处理未完成试卷。'

// Row shapes follow routers/grade.py: `_orientation_state_payload` for the
// snapshot restored after ACCELERATOR_WAITING and `_recognition_paper_payload`
// for the resume `paper` events (successful outcomes omit failure fields).
function snapshotRow(index, waiting) {
  return {
    batch_id: BATCH_ID,
    paper_id: `paper-${index}`,
    filename: `匿名页 ${index + 1}`,
    student_name: '',
    student_no: '',
    identity: {},
    identity_resolution: null,
    questions_count: waiting ? 0 : 5,
    matched_count: 0,
    ocr_status: waiting ? 'queued' : 'succeeded',
    error: waiting ? WAITING : '',
    requires_manual_review: false,
    orientation_status: 'auto_passed',
    orientation_reason: 'client_ocr_ready_v1',
  }
}

function resumedPaper(index, extra = {}) {
  return {
    batch_id: BATCH_ID,
    filename: `匿名页 ${index + 1}`,
    student_name: '',
    student_no: '',
    paper_id: `paper-${index}`,
    identity: {},
    questions_count: 5,
    matched_count: 0,
    orientation_status: 'auto_passed',
    orientation_reason: 'client_ocr_ready_v1',
    detected_tilt_degrees: 0,
    ...extra,
  }
}

const SERVER_DONE_23 = {
  batch_id: BATCH_ID,
  total_papers: 23,
  uploaded_papers: 23,
  succeeded_papers: 23,
  manual_review_papers: 0,
  failed_papers: 0,
  unresolved_papers: 0,
  can_grade: true,
}

const QUESTIONS = [{ question_no: '1', content: '1+1=?', answer: '2' }]

// A recognition run that re-emits all 23 papers and ends complete.
const RECOGNIZED_23 = [
  ...Array.from({ length: 23 }, (_, index) => ['paper', resumedPaper(index)]),
  ['done', { ...SERVER_DONE_23, questions: QUESTIONS }],
]

const GRADE_EVENTS_23 = [
  ...Array.from({ length: 23 }, (_, index) => ['result', { paper_id: `paper-${index}`, score: 5 }]),
  ['done', { total: 23, avg_score: 5, review_revision: 1 }],
]

const REVIEW_23 = {
  batch_id: BATCH_ID,
  identity_mode: 'anonymous',
  results: Array.from({ length: 23 }, (_, index) => ({
    paper_id: `paper-${index}`,
    disposition: 'active',
    category: 'daily',
  })),
  summary: { total: 23, avg_score: 5 },
  review_revision: 1,
}

function completedSnapshot(extra = {}) {
  return {
    ...SERVER_DONE_23,
    status: 'ocr_done',
    papers: Array.from({ length: 23 }, (_, index) => snapshotRow(index, false)),
    questions: QUESTIONS,
    source_mode: 'template', template_id: TEMPLATE_ID,
    ...extra,
  }
}

function pendingSnapshot(status = 'recognition_interrupted') {
  return completedSnapshot({
    status, succeeded_papers: 17, unresolved_papers: 6, can_grade: false,
    papers: Array.from({ length: 23 }, (_, index) => snapshotRow(index, index >= 17)),
    questions: [],
  })
}

// What a reopened page reads for a saved batch the server stopped with six
// pages unfinished: it offers 继续处理未完成试卷 and waits for the teacher.
const savedSnapshot = () => pendingSnapshot('failed')

// The incident's checkpoint: 15 pages recognized, 8 waiting for the accelerator.
function incidentSnapshot(status = 'recognition_interrupted') {
  return completedSnapshot({
    status, succeeded_papers: 15, unresolved_papers: 8, can_grade: false,
    papers: Array.from({ length: 23 }, (_, index) => snapshotRow(index, index >= 15)),
    questions: [],
  })
}

function failedRow(index) {
  return { ...snapshotRow(index, false), questions_count: 0, ocr_status: 'failed', error: '本次模板本地配准失败' }
}

const INCOMPLETE_DONE_23 = {
  ...SERVER_DONE_23,
  status: 'ocr_partial_failed',
  total_papers: 22,
  succeeded_papers: 22,
  failed_papers: 1,
  retryable_failed_papers: 1,
  can_grade: false,
  questions: [],
}

// An event stream whose connection drops after `events`: the next read fails
// the way a browser's fetch does when the network goes away mid-response.
function droppedStream(events = []) {
  const chunks = events.length
    ? [new TextEncoder().encode(events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''))]
    : []
  return new Response(new ReadableStream({
    pull(controller) {
      if (chunks.length) controller.enqueue(chunks.shift())
      else controller.error(new TypeError('NetworkError when attempting to fetch resource.'))
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

// ── the page ──

// The anonymous grading page by default, with mocked timers: the 3 s / 15 s
// recovery checks run only when a test advances the clock.
function openUpload(t, { anonymous = true, ...options } = {}) {
  return mountUpload(t, { timers: true, props: { anonymous }, ...options })
}

// Requests, stream calls and timers settle in more rounds than one click.
const settled = upload => upload.settle(40)

// The template editor's save leaves the confirmed template id on the page (the
// editor's own tests drive that save); then the teacher picks the pages, and
// recognition starts by itself.
async function uploadPapers(upload, count = 23) {
  upload.page.setup.confirmedTemplateId = TEMPLATE_ID
  await upload.settle()
  await upload.choosePapers(Array.from({ length: count }, (_, index) => paperFile(`paper-${index}.png`)))
  await settled(upload)
}

// A recognition run, started the two ways a teacher starts one: uploading 23
// new pages ('initial'), or reopening the page on the saved batch and clicking
// 继续处理未完成试卷 ('resume'). `stream` answers the recognize and resume
// requests; `snapshot` is the batch state the server returns when the page
// reads the batch afterwards. `reads()` counts those reads after the start.
async function startRecognition(t, mode, {
  stream, snapshot, anonymous = true, localStorage = {}, responses = {}, streams = {}, ...options
}) {
  const resume = mode === 'resume'
  const upload = await openUpload(t, {
    anonymous,
    localStorage: resume ? { [pointerKeys(anonymous).orientation]: BATCH_ID, ...localStorage } : localStorage,
    responses: { [orientationRoute(BATCH_ID)]: resume ? savedSnapshot() : snapshot, ...responses },
    streams: { [RECOGNIZE]: stream, [RESUME]: stream, ...streams },
    ...options,
  })
  const before = stateReads(upload)
  if (resume) {
    assert.equal(before, 1, 'the reopened page read its saved batch')
    upload.answer(orientationRoute(BATCH_ID), snapshot)
    await upload.click(RESUME_BUTTON)
    await settled(upload)
  } else {
    await uploadPapers(upload)
  }
  return { upload, reads: () => stateReads(upload) - before }
}

// The page reopened on a saved batch while the grading service was down: the
// batch is restored but grading cannot start; then the 15 s availability
// check sees the service back.
async function reopenedDuringOutage(t, snapshot, { anonymous = true, responses = {}, ...options } = {}) {
  const upload = await openUpload(t, {
    anonymous,
    localStorage: { [pointerKeys(anonymous).orientation]: BATCH_ID },
    responses: {
      [AVAILABILITY]: { status: 'unavailable', label: '暂不可用', message: '维护中' },
      [orientationRoute(BATCH_ID)]: snapshot,
      ...responses,
    },
    ...options,
  })
  upload.answer(AVAILABILITY, HEALTHY_SERVICE[AVAILABILITY])
  await upload.tick(15000)
  assert.equal(upload.page.root.byClass('service-alert-global'), null, 'the service is back')
  assert.equal(gradings(upload), 0)
  return upload
}

// The incident, end to end: the teacher's previous review is on screen; a new
// upload of 23 pages checkpoints its batch, the accelerator is busy, the saved
// snapshot has 15 recognized and 8 waiting pages, and the page waits for its
// automatic check 3 s later, which resumes the batch (`resume` answers it).
async function openIncident(t, { resume, confirm, responses = {} }) {
  const upload = await openUpload(t, {
    localStorage: { [REVIEW_KEY]: OLDER_REVIEW_ID },
    responses: {
      [reviewRoute(OLDER_REVIEW_ID)]: { ...REVIEW_23, batch_id: OLDER_REVIEW_ID },
      [reviewRoute(BATCH_ID)]: REVIEW_23,
      [orientationRoute(BATCH_ID)]: incidentSnapshot(),
      ...responses,
    },
    streams: {
      [RECOGNIZE]: [
        ['progress', { batch_id: BATCH_ID, current: 0, total: 23, message: '正在识别' }],
        ['error', { code: 'ACCELERATOR_WAITING', message: WAITING, batch_id: BATCH_ID }],
      ],
      [RESUME]: resume,
      [CONFIRM]: confirm,
    },
  })
  assert.match(upload.text(), /已恢复历史批次 1aba-older-review/)
  await uploadPapers(upload)
  assert.match(noticeTexts(upload), /3 秒后检查并继续/)
  return upload
}

// A page whose previous batch (review and recognition pointers both naming it)
// is on screen when the teacher uploads new pages.
function openPrevious(t, { stream, responses = {} }) {
  return openUpload(t, {
    localStorage: { [ORIENTATION_KEY]: OLDER_REVIEW_ID, [REVIEW_KEY]: OLDER_REVIEW_ID },
    responses: { [reviewRoute(OLDER_REVIEW_ID)]: { ...REVIEW_23, batch_id: OLDER_REVIEW_ID }, ...responses },
    streams: { [RECOGNIZE]: stream },
  })
}

// ── what the page shows and sends ──

const stateReads = (upload, id = BATCH_ID) => upload.requests(orientationRoute(id)).length
const resumes = upload => upload.fetches(RESUME).length
const gradings = upload => upload.fetches(CONFIRM).length
const errorToasts = upload => upload.client.messages.filter(m => m.type === 'error').map(m => m.message)
const faultShown = upload => upload.page.root.byClass('service-alert-fault') !== null
const stepCard = upload => upload.page.root.byClass('grade-card')
// The step card's own title ('识别完成', '识别未完成', '识别中...', or the upload step's).
function stepTitle(upload) {
  const title = stepCard(upload).byClass('card-title')
  return title.childNodes.filter(node => node.nodeType === 3).map(node => node.data).join('').trim()
}
// Recognition errors and recovery notices on the step card.
const notices = upload => stepCard(upload)
  .findAll(el => el.tagName === 'div' && ['recognize-warning', 'recognize-error'].some(name => el.classList.contains(name)))
  .map(el => ({ role: el.getAttribute('role'), text: el.text }))
const noticeTexts = upload => notices(upload).map(notice => notice.text).join(' | ')
const resultRows = upload => upload.page.root.byClass('result-table')?.byTag('tr').slice(1) ?? []
// Each paper row's 匹配/题数 cell: '0 / 5', or the tag it shows ('tag-pending: …').
const paperStatus = upload => resultRows(upload).map((row) => {
  const cell = row.byTag('td')[3]
  const tag = cell.byTag('span')[0]
  return tag?.className ? `${tag.className}: ${tag.text}` : cell.text
})
const excludeButtons = upload => resultRows(upload).map(row => row.byText('button', '排除此卷'))
const progressCard = upload => upload.page.root.byClass('progress-card')
const gradeErrorText = upload => progressCard(upload)?.byClass('recognize-error')?.text ?? ''
const reviewRows = upload => upload.page.root.allByClass('review-row').length
const summaryText = upload => upload.page.root.byClass('summary-bar')?.text ?? ''
const identityDialog = upload => upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '确认学生身份')
const isEnabled = el => Boolean(el) && el.disabled !== true && !el.hasAttribute('disabled')
const batchPointers = upload => Object.fromEntries(Object.entries(upload.storage()).filter(([key]) => key.includes('_batch_id')))

test('a completed 23/23 snapshot clears historical partial-failure and retry-exhaustion warnings', async (t) => {
  const upload = await openUpload(t)
  const payload = completedSnapshot({ status: 'ocr_partial_failed', retryable_failed_papers: 0 })
  for (const failureCode of ['', 'OCR_RETRY_EXHAUSTED', 'OCR_NO_RETRYABLE_FAILURES']) {
    // A genuinely failed snapshot leaves the exhausted warning on screen ...
    upload.page.setup.applyOrientationState(
      completedSnapshot({ status: 'ocr_partial_failed', succeeded_papers: 22, failed_papers: 1, can_grade: false }),
      { failureCode: 'OCR_RETRY_EXHAUSTED' },
    )
    await upload.settle()
    assert.match(noticeTexts(upload), /识别重试已耗尽/)
    // ... the complete snapshot replaces it, whatever failure code came with it.
    upload.page.setup.applyOrientationState(payload, { failureCode })
    await upload.settle()
    assert.equal(stepTitle(upload), '识别完成', failureCode)
    assert.equal(isEnabled(upload.button(CONTINUE_BUTTON)), true, failureCode)
    assert.deepEqual(notices(upload), [], failureCode)
  }
  assert.equal(gradings(upload), 0)
})

test('a successful SSE done clears an earlier recovery warning', async (t) => {
  const resume = liveSse(t)
  const grading = liveSse(t)
  const upload = await openUpload(t, {
    localStorage: { [ORIENTATION_KEY]: BATCH_ID },
    responses: { [orientationRoute(BATCH_ID)]: incidentSnapshot('failed') },
    streams: { [RESUME]: resume.respond, [CONFIRM]: grading.respond },
  })
  await upload.click(RESUME_BUTTON)
  // A warning from earlier in this batch is still on screen while the resumed stream runs.
  upload.page.setup.recognitionRecoveryState = { status: 'unrecoverable', message: 'stale failure' }
  await upload.settle()
  assert.deepEqual(notices(upload), [{ role: 'alert', text: 'stale failure' }])
  for (const [event, data] of RECOGNIZED_23) resume.send(event, data)
  await settled(upload)
  assert.equal(grading.count, 1, 'the complete batch went on to grading')
  assert.deepEqual(notices(upload), [])
})

test('genuine failed or unresolved papers still retain their recovery warning and block grading', async (t) => {
  for (const extra of [
    { succeeded_papers: 22, failed_papers: 1, can_grade: false },
    { succeeded_papers: 22, unresolved_papers: 1, can_grade: false },
    { papers: [snapshotRow(0, false)], can_grade: true },
  ]) {
    const { upload } = await startRecognition(t, 'resume', {
      stream: [['error', { code: 'OCR_RETRY_EXHAUSTED', message: '识别重试次数已用完', batch_id: BATCH_ID }]],
      snapshot: completedSnapshot({ status: 'ocr_partial_failed', ...extra }),
    })
    const label = JSON.stringify(extra).slice(0, 60)
    assert.equal(stepTitle(upload), '识别未完成', label)
    assert.equal(upload.button(CONTINUE_BUTTON), null, label)
    assert.ok(notices(upload).some(n => n.role === 'alert' && /识别重试已耗尽/.test(n.text)), `${label}: ${noticeTexts(upload)}`)
    assert.equal(gradings(upload), 0, label)
    upload.unmount()
  }
})

test('starting a new batch exits recognition and clears only this teacher/mode recovery pointers', async (t) => {
  const otherModeKey = 'mathagent_orientation_batch_id:7'
  const otherTeacherKey = 'mathagent_review_batch_id:anonymous:8'
  const { upload } = await startRecognition(t, 'initial', {
    // A review pointer for this batch the page could not read when it opened
    // (a temporary failure keeps the pointer) ...
    localStorage: { [REVIEW_KEY]: BATCH_ID, [otherModeKey]: 'other-mode', [otherTeacherKey]: 'other-teacher' },
    responses: { [reviewRoute(BATCH_ID)]: () => ({ status: 503, data: { detail: '审阅暂不可用' } }) },
    // ... then the 23 pages are recognized, and grading stops after 22 rows.
    stream: RECOGNIZED_23,
    streams: { [CONFIRM]: [...GRADE_EVENTS_23.slice(0, 22), ['error', { code: 'BATCH_GRADING_BUSY', message: '批次正在批阅' }]] },
  })
  const storage = upload.client.localStorage
  assert.equal(storage.getItem(REVIEW_KEY), BATCH_ID)
  assert.equal(storage.getItem(ORIENTATION_KEY), BATCH_ID)
  assert.equal(stepTitle(upload), '识别完成')
  assert.equal(reviewRows(upload), 22)
  assert.equal(gradeErrorText(upload), '批次正在批阅')
  // What an earlier restored review and identity check leave in the page.
  upload.page.setup.summary = REVIEW_23.summary
  upload.page.setup.identityResolutions = [{ paper_id: 'paper-0' }]
  await upload.settle()
  assert.match(summaryText(upload), /总计 23 页/)
  const sent = { requests: upload.client.requests.length, fetches: upload.fetch.requests.length }

  await upload.click(NEW_BATCH_BUTTON)

  // Back at the upload step with nothing of the old batch on screen.
  assert.equal(stepTitle(upload), '无身份试卷页面')
  assert.equal(stepCard(upload).byClass('file-summary'), null, 'the chosen pages are cleared')
  assert.equal(upload.page.root.byClass('result-table'), null)
  assert.equal(progressCard(upload), null)
  assert.equal(summaryText(upload), '')
  // State the upload step does not render.
  const setup = upload.page.setup
  assert.equal(setup.activeStep, 0)
  assert.equal(setup.batchId, '')
  assert.deepEqual(setup.papers, [])
  assert.deepEqual(setup.questions, [])
  assert.deepEqual(setup.progressList, [])
  assert.deepEqual(setup.identityResolutions, [])
  assert.equal(setup.summary, null)
  assert.equal(setup.gradeError, '')
  assert.equal(setup.recognitionDone, false)
  assert.equal(setup.recognitionCanGrade, false)
  assert.deepEqual(setup.recognitionRecoveryState, { status: 'none', message: '' })
  assert.deepEqual({ ...setup.recognized }, { current: 0, total: 0 })
  // Only this teacher's pointers for this mode are released.
  assert.equal(storage.getItem(REVIEW_KEY), null)
  assert.equal(storage.getItem(ORIENTATION_KEY), null)
  assert.equal(storage.getItem(otherModeKey), 'other-mode')
  assert.equal(storage.getItem(otherTeacherKey), 'other-teacher')
  // Nothing is read, resumed or graded for the abandoned batch.
  assert.equal(upload.client.requests.length, sent.requests)
  assert.equal(upload.fetch.requests.length, sent.fetches)

  // The confirmed template and the batch type stay: the next pages start at once.
  upload.stream(RECOGNIZE, [['done', { batch_id: 'b-next', can_grade: false, uploaded_papers: 1, total_papers: 0, unresolved_papers: 1 }]])
  await upload.choosePapers([paperFile('next.png')])
  const launches = upload.fetches(RECOGNIZE).map(request => formFields(request.init.body))
  assert.equal(launches.length, 2)
  assert.equal(launches[1].template_id, TEMPLATE_ID)
  assert.equal(launches[1].category, 'daily')
  assert.equal([launches[1].files].flat().length, 1)
})

test('new-batch action cannot interrupt recognition or grading', async (t) => {
  for (const busy of ['recognizing', 'grading']) {
    const live = liveSse(t)
    const upload = await openUpload(t, {
      localStorage: { [ORIENTATION_KEY]: BATCH_ID },
      // A saved batch to resume, or a complete one the reopened page grades.
      responses: { [orientationRoute(BATCH_ID)]: busy === 'recognizing' ? savedSnapshot() : completedSnapshot() },
      streams: { [RESUME]: live.respond, [CONFIRM]: live.respond },
    })
    if (busy === 'recognizing') await upload.click(RESUME_BUTTON)
    assert.equal(live.open, true, `${busy}: the stream is running`)
    assert.equal(stepTitle(upload), busy === 'recognizing' ? '识别中...' : '识别完成')
    if (busy === 'recognizing') assert.equal(upload.button(NEW_BATCH_BUTTON), null, 'not offered while recognizing')
    else await assert.rejects(upload.click(NEW_BATCH_BUTTON), /disabled/)
    // Even called directly, the action leaves the running batch alone.
    upload.page.setup.startNewBatch()
    await upload.settle()
    assert.equal(live.open, true, `${busy}: the stream was not interrupted`)
    assert.equal(stepTitle(upload), busy === 'recognizing' ? '识别中...' : '识别完成')
    assert.equal(paperStatus(upload).length, 23, busy)
    assert.equal(upload.page.setup.batchId, BATCH_ID, busy)
    assert.equal(upload.page.setup.activeStep, 1, busy)
    assert.equal(upload.client.localStorage.getItem(ORIENTATION_KEY), BATCH_ID, busy)
    upload.unmount()
  }
})

test('explicit continuation of a complete template batch uses the existing grading path once', async (t) => {
  const upload = await reopenedDuringOutage(t, completedSnapshot(), {
    responses: { [reviewRoute(BATCH_ID)]: REVIEW_23 },
    streams: { [CONFIRM]: GRADE_EVENTS_23 },
  })
  assert.match(gradeErrorText(upload), /^批阅未开始：/)
  await upload.click(CONTINUE_BUTTON)
  await settled(upload)
  assert.equal(gradings(upload), 1)
  assert.equal(JSON.parse(upload.fetches(CONFIRM)[0].init.body).batch_id, BATCH_ID)
  assert.equal(reviewRows(upload), 23)
  assert.match(summaryText(upload), /总计 23 页/)
})

test('explicit continuation preserves the teacher identity gate', async (t) => {
  const upload = await reopenedDuringOutage(t, completedSnapshot(), { anonymous: false })
  assert.equal(identityDialog(upload)?.attributes['data-open'], 'false')
  await upload.click(CONTINUE_BUTTON)
  await settled(upload)
  assert.equal(identityDialog(upload)?.attributes['data-open'], 'true')
  assert.equal(upload.requests(`GET /api/batch-grade/${BATCH_ID}/identity-options`).length, 1)
  assert.equal(gradings(upload), 0)
})

test('explicit continuation cannot submit incomplete or missing-question recognition', async (t) => {
  for (const [extra, offered] of [
    [{ succeeded_papers: 22, failed_papers: 1, can_grade: false }, 'not offered'],
    [{ questions: [] }, 'disabled'],
  ]) {
    const upload = await reopenedDuringOutage(t, completedSnapshot(extra))
    if (offered === 'not offered') assert.equal(upload.button(CONTINUE_BUTTON), null)
    else await assert.rejects(upload.click(CONTINUE_BUTTON), /disabled/)
    // The action itself refuses as well, and says why, before reaching grading.
    upload.client.messages.length = 0
    const gradingCard = gradeErrorText(upload)
    assert.equal(await upload.page.setup.continueAfterRecognition(), false, offered)
    await upload.settle()
    assert.equal(gradings(upload), 0, offered)
    assert.equal(gradeErrorText(upload), gradingCard, `${offered}: no grading attempt is reported`)
    const warnings = upload.client.messages.filter(m => m.type === 'warning').map(m => m.message)
    assert.equal(warnings.length, 1, offered)
    assert.ok(notices(upload).some(n => n.role === 'alert' && n.text === warnings[0]), `${offered}: ${noticeTexts(upload)}`)
    upload.unmount()
  }
})

test('15 recognized + 8 accelerator-waiting rows resume to 23 gradable papers and 23/23 grading', async (t) => {
  const grading = liveSse(t)
  const upload = await openIncident(t, { resume: RECOGNIZED_23, confirm: grading.respond })
  const storage = upload.client.localStorage
  // The new batch is checkpointed and replaces the older review pointer.
  assert.equal(storage.getItem(ORIENTATION_KEY), BATCH_ID)
  assert.equal(storage.getItem(REVIEW_KEY), null)
  const before = paperStatus(upload)
  assert.equal(before.filter(status => status === '0 / 5').length, 15)
  assert.equal(before.filter(status => status === `tag-pending: ${WAITING}`).length, 8)

  await upload.tick(3000)
  await settled(upload)
  assert.equal(resumes(upload), 1)
  // Every row now carries its resumed outcome, and all 23 are sent to grading.
  assert.deepEqual(paperStatus(upload), Array(23).fill('0 / 5'))
  assert.equal(grading.count, 1)
  assert.match(progressCard(upload).text, /已批阅 0 \/ 23 页/)
  for (const [event, data] of GRADE_EVENTS_23) grading.send(event, data)
  await settled(upload)
  assert.equal(gradeErrorText(upload), '')
  assert.match(summaryText(upload), /总计 23 页/)
  assert.equal(reviewRows(upload), 23)
  assert.equal(storage.getItem(REVIEW_KEY), BATCH_ID)
  assert.equal(storage.getItem(ORIENTATION_KEY), null)
})

test('rows still carrying the waiting error (the pre-fix shallow merge) reproduce the incident message; the current batch stays restorable', async (t) => {
  const upload = await openIncident(t, {
    // Only the 15 recognized pages come back, so the 8 waiting rows keep their
    // error as the pre-fix shallow merge left them, while done reports 23.
    resume: [
      ...Array.from({ length: 15 }, (_, index) => ['paper', resumedPaper(index)]),
      ['done', { ...SERVER_DONE_23, questions: QUESTIONS }],
    ],
    confirm: GRADE_EVENTS_23,
  })
  await upload.tick(3000)
  await settled(upload)
  assert.equal(gradings(upload), 1)
  assert.equal(gradeErrorText(upload), '批阅结果不完整：预期 15 张，服务端报告 23 张，实际收到 23 张。请重试批阅。')
  assert.equal(summaryText(upload), '')

  // The server already saved the review; a reload must restore this batch,
  // not the older saved review that used to shadow it.
  assert.equal(upload.client.localStorage.getItem(REVIEW_KEY), BATCH_ID)
  const saved = upload.storage()
  upload.unmount()
  const reopened = await openUpload(t, {
    localStorage: saved,
    responses: {
      [reviewRoute(BATCH_ID)]: REVIEW_23,
      [reviewRoute(OLDER_REVIEW_ID)]: { ...REVIEW_23, batch_id: OLDER_REVIEW_ID },
    },
  })
  assert.equal(reopened.requests(reviewRoute(BATCH_ID)).length, 1)
  assert.equal(reopened.requests(reviewRoute(OLDER_REVIEW_ID)).length, 0)
  assert.match(reopened.text(), /已恢复历史批次 c609-current-batch/)
  assert.equal(reviewRows(reopened), 23)
})

test('a newer unfinished batch drops an older review pointer but keeps its own', async (t) => {
  const live = liveSse(t)
  const upload = await openUpload(t, {
    localStorage: { [REVIEW_KEY]: OLDER_REVIEW_ID },
    responses: { [reviewRoute(OLDER_REVIEW_ID)]: { ...REVIEW_23, batch_id: OLDER_REVIEW_ID } },
    streams: { [RECOGNIZE]: live.respond },
  })
  const storage = upload.client.localStorage
  await uploadPapers(upload)
  live.send('progress', { batch_id: BATCH_ID, current: 0, total: 23 })
  await upload.settle()
  assert.equal(storage.getItem(ORIENTATION_KEY), BATCH_ID)
  assert.equal(storage.getItem(REVIEW_KEY), null)

  // A review pointer naming this same batch survives the next checkpoint.
  storage.setItem(REVIEW_KEY, BATCH_ID)
  live.send('paper', resumedPaper(0))
  await upload.settle()
  assert.equal(storage.getItem(ORIENTATION_KEY), BATCH_ID)
  assert.equal(storage.getItem(REVIEW_KEY), BATCH_ID)
})

test('new genuine failures and still-queued papers keep the resumed batch blocked', async (t) => {
  const upload = await openUpload(t, {
    localStorage: { [ORIENTATION_KEY]: BATCH_ID },
    responses: { [orientationRoute(BATCH_ID)]: incidentSnapshot() },
    streams: {
      [RESUME]: [
        ...Array.from({ length: 21 }, (_, index) => ['paper', resumedPaper(index)]),
        // Paper 21 now fails outright; paper 22 has no outcome yet and stays queued.
        ['paper', resumedPaper(21, { error: 'OCR 识别失败', requires_manual_review: false })],
        ['done', { ...SERVER_DONE_23, succeeded_papers: 21, failed_papers: 1, unresolved_papers: 1, can_grade: false }],
      ],
      [CONFIRM]: GRADE_EVENTS_23,
    },
  })
  await upload.click(RESUME_BUTTON)
  await settled(upload)
  assert.deepEqual(paperStatus(upload), [
    ...Array(21).fill('0 / 5'),
    'tag-fail: OCR 识别失败',
    `tag-pending: ${WAITING}`,
  ])
  assert.equal(upload.page.setup.gradablePaperCount, 21)
  assert.equal(stepTitle(upload), '识别未完成')
  assert.equal(upload.button(CONTINUE_BUTTON), null)
  // Grading itself refuses the batch and says it did not start.
  assert.equal(await upload.page.setup.onConfirmGrade(), false)
  await upload.settle()
  assert.match(gradeErrorText(upload), /^批阅未开始：/)
  assert.equal(gradings(upload), 0)
})

test('the grading stopwatch follows the server clock and reports server-measured grading time', async (t) => {
  const grading = liveSse(t)
  const upload = await openIncident(t, {
    resume: RECOGNIZED_23,
    confirm: grading.respond,
    responses: { [reviewRoute(BATCH_ID)]: { ...REVIEW_23, summary: { ...REVIEW_23.summary, grading_elapsed_seconds: 101.5 } } },
  })
  // The intervals the page keeps ticking (its stopwatch ticks every second).
  const win = upload.page.win
  const ticking = new Map()
  const { setInterval: startInterval, clearInterval: stopInterval } = win
  win.setInterval = (fn, ms) => {
    const id = startInterval(fn, ms)
    ticking.set(id, ms)
    return id
  }
  win.clearInterval = (id) => {
    ticking.delete(id)
    stopInterval(id)
  }
  const stopwatches = () => [...ticking.values()].filter(ms => ms === 1000).length

  await upload.tick(3000)
  await settled(upload)
  assert.equal(grading.count, 1)
  assert.equal(stopwatches(), 1, 'the stopwatch runs while grading')
  // Pipeline grading began during recognition: the server is already 95 s in.
  grading.send('progress', { message: '恢复批改', current: 0, total: 23, grading_elapsed_seconds: 95 })
  await upload.settle()
  assert.match(progressCard(upload).byClass('grading-progress__elapsed')?.text ?? '', /^已用时 1:3[56]$/)

  for (const [event, data] of GRADE_EVENTS_23.slice(0, -1)) grading.send(event, data)
  grading.send('done', { total: 23, avg_score: 5, review_revision: 1, grading_elapsed_seconds: 101.5 })
  await settled(upload)
  assert.match(summaryText(upload), /批阅用时 1:41/)
  assert.match(upload.client.messages.filter(m => m.type === 'success').at(-1).message, /批阅用时 1:41$/)
  // The ticking interval is cleared once grading ends.
  assert.equal(stopwatches(), 0)
  assert.equal(progressCard(upload).byClass('grading-progress__elapsed'), null)
})

for (const mode of ['initial', 'resume']) {
  test(`${mode}: disconnect with reachable pending batch waits for active work then resumes without a service fault`, async (t) => {
    let requests = 0
    const { upload, reads } = await startRecognition(t, mode, {
      stream: () => (++requests === 1
        ? droppedStream([['progress', { batch_id: BATCH_ID }]])
        : sseResponse(RECOGNIZED_23)),
      snapshot: pendingSnapshot('recognizing_remaining'),
      responses: { [reviewRoute(BATCH_ID)]: REVIEW_23 },
      streams: { [CONFIRM]: GRADE_EVENTS_23 },
    })
    assert.equal(faultShown(upload), false)
    assert.deepEqual(notices(upload).filter(n => n.role === 'alert'), [], 'no recognition error')
    assert.match(noticeTexts(upload), /3 秒后检查并继续；服务器仍在处理时只等待，不重复提交（1\/6）/)
    assert.equal(paperStatus(upload).length, 23)
    assert.equal(paperStatus(upload).filter(status => status === '0 / 5').length, 17)
    assert.equal(reads(), 1)

    await upload.tick(3000)
    assert.equal(requests, 1, 'active work must be polled, not resubmitted')
    assert.equal(reads(), 2)
    assert.match(noticeTexts(upload), /（2\/6）/)

    upload.answer(orientationRoute(BATCH_ID), pendingSnapshot())
    await upload.tick(3000)
    await settled(upload)
    assert.equal(requests, 2)
    assert.equal(resumes(upload), mode === 'initial' ? 1 : 2)
    assert.equal(gradings(upload), 1)
    assert.match(summaryText(upload), /总计 23 页/)
    assert.deepEqual(errorToasts(upload), [])
    assert.equal(faultShown(upload), false)
  })
}

test('page reload of an active saved batch uses the same bounded read-before-resume recovery', async (t) => {
  const upload = await openUpload(t, {
    localStorage: { [ORIENTATION_KEY]: BATCH_ID },
    responses: { [orientationRoute(BATCH_ID)]: pendingSnapshot('recognizing_remaining') },
  })
  assert.equal(stateReads(upload), 1)
  assert.equal(paperStatus(upload).length, 23, 'the saved batch is restored')
  assert.match(noticeTexts(upload), /3 秒后检查并继续.*（1\/6）/)
  for (let check = 1; check <= 6; check += 1) await upload.tick(3000)
  assert.equal(stateReads(upload), 7, 'each of the six checks only reads the batch')
  assert.match(noticeTexts(upload), /自动恢复检查已达上限/)
  await upload.tick(30000)
  assert.equal(stateReads(upload), 7, 'active work cannot produce an infinite polling loop')
  assert.equal(resumes(upload), 0)
  assert.equal(faultShown(upload), false)
})

test('a business-failed checkpoint stops connection recovery rather than spending OCR retries', async (t) => {
  const { upload, reads } = await startRecognition(t, 'resume', {
    stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
    snapshot: pendingSnapshot('recognizing_remaining'),
  })
  assert.match(noticeTexts(upload), /（1\/6）/)
  upload.answer(orientationRoute(BATCH_ID), { ...pendingSnapshot('failed'), retryable_failed_papers: 1 })
  await upload.tick(3000)
  assert.equal(reads(), 2)
  await upload.tick(60000)
  assert.equal(resumes(upload), 1, 'a fresh business failure is not a transport retry')
  assert.equal(reads(), 2, 'and no further check is scheduled')
  assert.match(noticeTexts(upload), /本批次仍有未完成或可重试的试卷/)
})

test('an empty checkpoint cannot prove that a batch is recoverable', async (t) => {
  const { upload, reads } = await startRecognition(t, 'resume', {
    stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
    snapshot: { ...pendingSnapshot(), papers: [] },
  })
  assert.equal(reads(), 1)
  assert.doesNotMatch(noticeTexts(upload), /秒后检查并继续/)
  await upload.tick(60000)
  assert.equal(reads(), 1)
  assert.equal(resumes(upload), 1)
})

for (const responseKind of ['event', '409']) {
  test(`resume lock busy (${responseKind}) is bounded and never marks the service broken`, async (t) => {
    const busy = { code: 'BATCH_RECOGNITION_BUSY', message: '该批次仍在识别，请等待当前任务完成，不要重复提交' }
    const { upload } = await startRecognition(t, 'resume', {
      stream: () => (responseKind === 'event'
        ? sseResponse([['error', busy]])
        : jsonResponse({ detail: busy }, { status: 409 })),
      snapshot: pendingSnapshot(),
    })
    assert.match(noticeTexts(upload), /（1\/6）/)
    for (let check = 1; check <= 6; check += 1) await upload.tick(3000)
    await settled(upload)
    assert.equal(resumes(upload), 7, 'lock races share the same six-attempt budget')
    const signals = upload.fetches(RESUME).map(request => request.init.signal)
    assert.equal(signals.every(signal => signal && !signal.aborted), true, 'never cancel server work to clear its lock')
    assert.equal(faultShown(upload), false)
    assert.deepEqual(errorToasts(upload), [])
    assert.match(noticeTexts(upload), /自动恢复检查已达上限/)
    await upload.tick(30000)
    assert.equal(resumes(upload), 7)
  })
}

for (const status of [401, 403, 422, 409]) {
  for (const complete of [false, true]) {
    test(`HTTP ${status} without an explicit busy code never automatically resubmits (complete=${complete})`, async (t) => {
      const { upload } = await startRecognition(t, 'resume', {
        stream: () => jsonResponse({ detail: 'request rejected' }, { status }),
        snapshot: complete ? completedSnapshot() : pendingSnapshot(),
      })
      assert.doesNotMatch(noticeTexts(upload), /秒后检查并继续/)
      await upload.tick(60000)
      assert.equal(resumes(upload), 1)
      assert.equal(gradings(upload), 0)
    })
  }
}

for (const code of ['GRADING_CHAIN_FAILED', 'OCR_RETRY_EXHAUSTED', 'OCR_NO_RETRYABLE_FAILURES', 'BATCH_TEMPLATE_REQUIRED']) {
  test(`business failure ${code} never becomes a connection retry even with a reachable snapshot`, async (t) => {
    const { upload } = await startRecognition(t, 'resume', {
      stream: [['error', { code, batch_id: BATCH_ID, message: 'business failure' }]],
      snapshot: pendingSnapshot(),
    })
    assert.doesNotMatch(noticeTexts(upload), /秒后检查并继续/)
    await upload.tick(60000)
    assert.equal(resumes(upload), 1)
    assert.equal(gradings(upload), 0)
  })
}

test('an unclassified business exception is not a transport recovery signal', async (t) => {
  const { upload } = await startRecognition(t, 'resume', {
    stream: () => { throw new Error('批改处理失败，请稍后重试') },
    snapshot: pendingSnapshot(),
  })
  assert.doesNotMatch(noticeTexts(upload), /秒后检查并继续/)
  await upload.tick(60000)
  assert.equal(resumes(upload), 1)
})

test('explicit cancellation does not refresh or schedule a retry', async (t) => {
  const { upload, reads } = await startRecognition(t, 'resume', {
    stream: () => { throw new DOMException('The operation was aborted.', 'AbortError') },
    snapshot: pendingSnapshot(),
  })
  assert.equal(reads(), 0)
  await upload.tick(60000)
  assert.equal(reads(), 0)
  assert.equal(resumes(upload), 1)
  assert.equal(faultShown(upload), false)
  assert.deepEqual(errorToasts(upload), [])
})

test('leaving the page during a resume cancels it without a refresh or retry', async (t) => {
  const live = liveSse(t)
  const { upload, reads } = await startRecognition(t, 'resume', { stream: live.respond, snapshot: pendingSnapshot() })
  assert.equal(live.open, true)
  // The teacher navigates away and confirms that the running work is interrupted.
  await upload.page.router.push('/__mounted__')
  await settled(upload)
  assert.deepEqual(upload.page.win.confirmations, ['任务正在处理中，离开会中断当前请求。确定离开吗？'])
  assert.equal(live.open, false, 'the resume request was aborted')
  assert.equal(upload.fetches(RESUME)[0].init.signal.aborted, true)
  await upload.tick(60000)
  assert.equal(reads(), 0)
  assert.equal(resumes(upload), 1)
})

test('clearing recovery while a checkpoint read is in flight cannot revive or replace the abandoned batch', async (t) => {
  const { upload, reads } = await startRecognition(t, 'resume', {
    stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
    snapshot: pendingSnapshot(),
  })
  assert.match(noticeTexts(upload), /（1\/6）/)
  let answerRead
  upload.answer(orientationRoute(BATCH_ID), () => new Promise((resolve) => { answerRead = resolve }))
  await upload.tick(3000)
  assert.equal(reads(), 2, 'the automatic check is reading the batch')

  await upload.click(NEW_BATCH_BUTTON)
  answerRead({ status: 200, data: pendingSnapshot() })
  await settled(upload)
  assert.equal(stepTitle(upload), '无身份试卷页面')
  assert.equal(upload.page.root.byClass('result-table'), null)
  assert.equal(upload.page.setup.batchId, '')
  assert.deepEqual(upload.page.setup.papers, [])
  assert.equal(upload.client.localStorage.getItem(ORIENTATION_KEY), null)
  await upload.tick(60000)
  assert.equal(resumes(upload), 1)
  assert.equal(reads(), 2)
})

for (const mode of ['initial', 'resume']) {
  test(`${mode}: disconnect + completed server snapshot continues the existing grading path once`, async (t) => {
    const { upload, reads } = await startRecognition(t, mode, {
      stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
      snapshot: completedSnapshot(),
      responses: { [reviewRoute(BATCH_ID)]: REVIEW_23 },
      streams: { [CONFIRM]: GRADE_EVENTS_23 },
    })
    assert.equal(reads(), 1)
    assert.equal(gradings(upload), 1)
    assert.match(summaryText(upload), /总计 23 页/)
    assert.equal(upload.page.setup.recognitionCanGrade, true)
    assert.equal(upload.page.setup.recognitionError, '')
    assert.equal(upload.page.setup.recognizing, false)
    assert.deepEqual(errorToasts(upload), [])
    assert.equal(faultShown(upload), false)
  })

  test(`${mode}: failed refresh blocks grading and retains the durable pointer on a temporary failure`, async (t) => {
    const { upload, reads } = await startRecognition(t, mode, {
      stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
      snapshot: () => ({ status: 503, data: { detail: '批次状态暂不可用' } }),
    })
    assert.equal(reads(), 1)
    assert.equal(stepTitle(upload), '识别未完成')
    assert.equal(upload.button(CONTINUE_BUTTON), null)
    assert.match(stepCard(upload).byClass('recognize-summary').text, /^已处理 /, 'recognition is not shown as done')
    assert.equal(gradings(upload), 0)
    assert.equal(upload.client.localStorage.getItem(ORIENTATION_KEY), BATCH_ID)
    // The API client reports the failed read; the page reports the failure once.
    assert.deepEqual(errorToasts(upload), ['批次状态暂不可用', SERVICE_FAULT_NOTICE])
  })

  for (const [label, extra] of [
    ['unresolved papers', { unresolved_papers: 1, succeeded_papers: 22, can_grade: false }],
    ['failed papers', { failed_papers: 1, succeeded_papers: 22, can_grade: false }],
    ['no questions', { questions: [] }],
    ['incomplete papers', { papers: [snapshotRow(0, false)] }],
    ['mismatched batch', { batch_id: 'different-batch' }],
  ]) {
    test(`${mode}: refreshed ${label} never bypass the grading completion gate`, async (t) => {
      const { upload } = await startRecognition(t, mode, {
        stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
        snapshot: completedSnapshot(extra),
      })
      await upload.tick(60000)
      assert.equal(gradings(upload), 0)
      assert.notEqual(stepTitle(upload), '识别完成')
      assert.equal(upload.button(CONTINUE_BUTTON), null)
    })
  }

  test(`${mode}: refreshed unknown identity uses the real teacher-resolution gate`, async (t) => {
    const { upload } = await startRecognition(t, mode, {
      anonymous: false,
      stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
      snapshot: completedSnapshot(),
    })
    assert.equal(stepTitle(upload), '识别完成')
    assert.equal(isEnabled(upload.button(CONTINUE_BUTTON)), true)
    assert.equal(identityDialog(upload)?.attributes['data-open'], 'true')
    assert.equal(gradings(upload), 0)
    assert.deepEqual(errorToasts(upload), [])
  })

  test(`${mode}: accelerator-waiting race with completed snapshot does not reschedule or erase completion`, async (t) => {
    const { upload, reads } = await startRecognition(t, mode, {
      stream: [
        ['progress', { batch_id: BATCH_ID }],
        ['error', { code: 'ACCELERATOR_WAITING', message: WAITING, batch_id: BATCH_ID }],
      ],
      snapshot: completedSnapshot(),
      responses: { [reviewRoute(BATCH_ID)]: REVIEW_23 },
      streams: { [CONFIRM]: GRADE_EVENTS_23 },
    })
    assert.equal(gradings(upload), 1)
    assert.match(summaryText(upload), /总计 23 页/)
    assert.equal(upload.page.setup.recognitionCanGrade, true)
    const started = { reads: reads(), resumes: resumes(upload) }
    await upload.tick(60000)
    assert.deepEqual({ reads: reads(), resumes: resumes(upload) }, started, 'nothing is rescheduled')
    assert.equal(gradings(upload), 1)
  })

  test(`${mode}: a grading rejection after snapshot recovery is not re-reported as a recognition failure`, async (t) => {
    const { upload, reads } = await startRecognition(t, mode, {
      stream: () => droppedStream([['progress', { batch_id: BATCH_ID }]]),
      snapshot: completedSnapshot(),
      streams: { [CONFIRM]: [['error', { code: 'BATCH_GRADING_BUSY', message: '批次正在批阅' }]] },
    })
    assert.equal(gradings(upload), 1)
    assert.equal(reads(), 1)
    assert.equal(gradeErrorText(upload), '批次正在批阅')
    assert.deepEqual(notices(upload), [], 'no recognition error on the recognition card')
    assert.equal(stepTitle(upload), '识别完成')
    assert.ok(upload.button(CONTINUE_BUTTON))
    assert.deepEqual(errorToasts(upload), ['批次正在批阅'])
  })

  test(`${mode}: normal done grades once without a recovery refresh`, async (t) => {
    const { upload, reads } = await startRecognition(t, mode, {
      stream: RECOGNIZED_23,
      responses: { [reviewRoute(BATCH_ID)]: REVIEW_23 },
      streams: { [CONFIRM]: GRADE_EVENTS_23 },
    })
    assert.equal(gradings(upload), 1)
    assert.equal(reads(), 0)
    assert.match(summaryText(upload), /总计 23 页/)
  })
}

for (const failure of ['422', 'disconnect']) {
  test(`initial ${failure} before the first batch event never reads or overwrites the previous batch`, async (t) => {
    const upload = await openPrevious(t, {
      stream: () => (failure === '422'
        ? jsonResponse({ detail: 'invalid upload' }, { status: 422 })
        : droppedStream()),
    })
    assert.equal(upload.page.setup.batchId, OLDER_REVIEW_ID, 'the previous batch is on screen')
    await uploadPapers(upload)
    await upload.tick(60000)
    assert.equal(upload.fetches(RECOGNIZE).length, 1)
    assert.equal(upload.client.requests.filter(config => /\/orientation$/.test(config.url)).length, 0)
    assert.equal(upload.page.setup.batchId, '')
    assert.equal(upload.button(RESUME_BUTTON), null, 'the previous batch is not offered as this upload\'s recovery')
    assert.deepEqual(batchPointers(upload), { [ORIENTATION_KEY]: OLDER_REVIEW_ID, [REVIEW_KEY]: OLDER_REVIEW_ID })
    assert.equal(gradings(upload), 0)
    assert.equal(stepTitle(upload), '识别未完成')
  })
}

for (const event of ['progress', 'paper', 'error']) {
  test(`initial ${event} identifies the new durable batch instead of the previous batch`, async (t) => {
    const payload = event === 'paper' ? resumedPaper(0) : { batch_id: BATCH_ID }
    const upload = await openPrevious(t, {
      stream: () => droppedStream([[event, payload]]),
      responses: { [orientationRoute(BATCH_ID)]: completedSnapshot({ can_grade: false }) },
    })
    await uploadPapers(upload)
    assert.equal(stateReads(upload, BATCH_ID), 1)
    assert.equal(stateReads(upload, OLDER_REVIEW_ID), 0)
    assert.deepEqual(batchPointers(upload), { [ORIENTATION_KEY]: BATCH_ID })
    assert.equal(gradings(upload), 0)
  })
}

test('an incomplete done with a retryable OCR failure shows recovery and retries it once automatically', async (t) => {
  const { upload } = await startRecognition(t, 'initial', {
    stream: [
      ...Array.from({ length: 22 }, (_, index) => ['paper', resumedPaper(index)]),
      ['paper', resumedPaper(22, { questions_count: 0, error: '答案识别暂时失败', requires_manual_review: false })],
      ['done', INCOMPLETE_DONE_23],
    ],
  })
  assert.equal(gradings(upload), 0)
  assert.equal(stepTitle(upload), '识别未完成')
  assert.ok(notices(upload).some(n => n.role === 'status' && /15 秒后自动重试/.test(n.text)), noticeTexts(upload))
  // A failed page waiting for its automatic retry is not offered for exclusion.
  assert.deepEqual(excludeButtons(upload), Array(23).fill(null))

  await upload.tick(14999)
  assert.equal(resumes(upload), 0)
  await upload.tick(1)
  await settled(upload)
  assert.equal(resumes(upload), 1)
  // The same batch never loops: the server's one extra attempt was spent.
  assert.match(noticeTexts(upload), /本批次仍有未完成或可重试的试卷/)
  await upload.tick(60000)
  assert.equal(resumes(upload), 1)
})

test('an incomplete done without retryable failures says the page is unrecoverable', async (t) => {
  const { upload } = await startRecognition(t, 'initial', {
    stream: [
      ...Array.from({ length: 22 }, (_, index) => ['paper', resumedPaper(index)]),
      ['paper', resumedPaper(22, { questions_count: 0, error: '本次模板本地配准失败', requires_manual_review: false })],
      ['done', { ...INCOMPLETE_DONE_23, retryable_failed_papers: 0 }],
    ],
  })
  assert.ok(notices(upload).some(n => n.role === 'alert' && /当前失败试卷不可自动恢复/.test(n.text)), noticeTexts(upload))
  await upload.tick(60000)
  assert.equal(resumes(upload), 0)
  // Only the failed page offers exclusion.
  assert.deepEqual(excludeButtons(upload).map(Boolean), [...Array(22).fill(false), true])
})

test('excluding a final OCR failure grades the remaining papers through the normal path once', async (t) => {
  const kept = Array.from({ length: 22 }, (_, index) => snapshotRow(index, false))
  const excludedSnapshot = completedSnapshot({
    succeeded_papers: 22,
    excluded_papers: 1,
    papers: [...kept, { ...failedRow(22), ocr_status: 'excluded', error: '已由教师排除，不参与本批批阅' }],
  })
  const gradeEvents = [
    ...Array.from({ length: 22 }, (_, index) => ['result', { paper_id: `paper-${index}`, score: 5 }]),
    ['done', { total: 22, avg_score: 5, review_revision: 1 }],
  ]
  const review = { ...REVIEW_23, status: 'review_ready', results: REVIEW_23.results.slice(0, 22), summary: { total: 22, avg_score: 5 } }
  const upload = await openUpload(t, {
    localStorage: { [ORIENTATION_KEY]: BATCH_ID },
    responses: {
      [orientationRoute(BATCH_ID)]: completedSnapshot({
        status: 'ocr_partial_failed', succeeded_papers: 22, failed_papers: 1, can_grade: false,
        papers: [...kept, failedRow(22)], questions: [],
      }),
      [EXCLUDE]: excludedSnapshot,
      [reviewRoute(BATCH_ID)]: review,
    },
    streams: { [CONFIRM]: gradeEvents },
  })
  assert.ok(notices(upload).some(n => n.role === 'alert' && /当前失败试卷不可自动恢复/.test(n.text)), noticeTexts(upload))
  assert.deepEqual(excludeButtons(upload).map(Boolean), [...Array(22).fill(false), true])

  await upload.click('排除此卷')
  await settled(upload)
  assert.equal(upload.page.win.confirmations.length, 1)
  assert.match(upload.page.win.confirmations[0], /匿名页 23/)
  assert.equal(upload.requests(EXCLUDE).length, 1)
  assert.ok(upload.client.messages.some(m => m.type === 'success' && m.message === '已排除该卷，开始批阅其余试卷'))
  assert.equal(gradings(upload), 1)
  assert.match(summaryText(upload), /总计 22 页/)
  assert.equal(gradeErrorText(upload), '')
})

test('a review pointer to an unfinished batch is not shown as finished; it resumes via recovery', async (t) => {
  const upload = await openUpload(t, {
    localStorage: { [REVIEW_KEY]: BATCH_ID },
    responses: {
      [reviewRoute(BATCH_ID)]: { ...REVIEW_23, status: 'grading_interrupted' },
      [orientationRoute(BATCH_ID)]: pendingSnapshot(),
    },
  })
  assert.equal(upload.requests(reviewRoute(BATCH_ID)).length, 1)
  assert.equal(reviewRows(upload), 0)
  assert.equal(summaryText(upload), '')
  assert.doesNotMatch(upload.text(), /已恢复历史批次/)
  assert.equal(upload.client.localStorage.getItem(REVIEW_KEY), null)
  assert.equal(upload.client.localStorage.getItem(ORIENTATION_KEY), BATCH_ID)
  // The page picks the batch up through recognition recovery instead.
  assert.equal(stateReads(upload), 1)
  assert.match(noticeTexts(upload), /3 秒后检查并继续/)
})

test('phone capture photos are released only once the server has created the batch', async (t) => {
  const session = '0123456789abcdef01234567'
  const closeRoute = `DELETE /api/capture/sessions/${session}`
  const savedSession = `mathagent_phone_capture:${TEACHER.id}`
  for (const [outcome, recognize] of [
    ['created', [
      ['progress', { batch_id: BATCH_ID, current: 0, total: 1 }],
      ['paper', resumedPaper(0)],
      ['error', { message: 'synthetic stop' }],
    ]],
    ['rejected', () => jsonResponse({ detail: 'busy' }, { status: 503 })],
  ]) {
    const upload = await openUpload(t, {
      responses: {
        'POST /api/capture/sessions': { session_id: session, token: 'phone-token', images: [] },
        [`GET /api/capture/sessions/${session}`]: { session_id: session, images: [{ seq: 1, width: 1200, height: 1600 }] },
        [`GET /api/capture/sessions/${session}/images/1`]: paperFile('photo.png'),
      },
      streams: { [RECOGNIZE]: recognize },
    })
    upload.page.setup.confirmedTemplateId = TEMPLATE_ID
    // The teacher opens 手机拍摄, the phone's photo arrives, and is imported.
    await upload.click('手机拍摄')
    await upload.click('导入 1 张')
    await settled(upload)
    assert.equal(upload.fetches(RECOGNIZE).length, 1, `${outcome}: the phone photo went to recognition`)
    if (outcome === 'created') {
      assert.equal(upload.requests(closeRoute).length, 1, 'released exactly once')
      assert.equal(upload.client.sessionStorage.getItem(savedSession), null)
    } else {
      assert.equal(upload.requests(closeRoute).length, 0, 'a rejected upload keeps the phone photos for a retry')
      assert.notEqual(upload.client.sessionStorage.getItem(savedSession), null)
    }
    upload.unmount()
  }
})
