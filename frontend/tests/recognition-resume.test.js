import assert from 'node:assert/strict'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'
import { sseResponse } from './helpers/memoryFetch.js'
import { HEALTHY_SERVICE, liveSse, mountUpload, paperFile, TEACHER } from './helpers/uploadPage.js'

// Durable recognition recovery on the real Upload page: the batch pointer it
// keeps in localStorage, what it offers after a refresh, the resume requests
// it sends and what it tells the teacher when recovery cannot continue.

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const AVAILABILITY = 'GET /api/batch-grade/availability'
const TEMPLATE_AVAILABILITY = 'GET /api/batch-grade/template/availability'
const ORIENTATION_POINTER = `mathagent_orientation_batch_id:${TEACHER.id}`
const resumeRoute = batchId => `POST /api/batch-grade/${batchId}/recognize/resume`
const orientationRoute = batchId => `GET /api/batch-grade/${batchId}/orientation`
const RESUME_BUTTON = '继续处理未完成试卷'
// A resume run the server ends with the batch still unfinished.
const UNFINISHED = batchId => [['done', { batch_id: batchId, can_grade: false, uploaded_papers: 1, total_papers: 0, unresolved_papers: 1 }]]

// The durable batch state the server returns for a saved pointer.
function batchState(batchId, fields = {}) {
  return {
    batch_id: batchId,
    category: 'daily',
    mode: 'precise',
    source_mode: 'template',
    template_id: 'tpl-saved',
    uploaded_papers: 1,
    papers: [{ paper_id: 'p1', filename: 'a.png', student_name: '甲', ocr_status: 'queued' }],
    ...fields,
  }
}

// A page opened again with the teacher's saved recognition pointer.
function refreshedPage(t, batchId, state, options = {}) {
  return mountUpload(t, {
    ...options,
    localStorage: { [ORIENTATION_POINTER]: batchId, ...options.localStorage },
    responses: { [orientationRoute(batchId)]: state, ...options.responses },
  })
}

async function saveTemplate(upload, id = 'tpl-1') {
  upload.page.setup.confirmedTemplateId = id
  await upload.settle()
}

const alerts = upload => upload.page.root.findAll(el => el.getAttribute('role') === 'alert').map(el => el.text)
const paperRows = upload => upload.page.root.byClass('result-table').byTag('tr').slice(1)
  .map(row => row.byTag('td').slice(0, 4).map(cell => cell.text))

test('recognition checkpoints its batch id on the first progress event', async (t) => {
  const live = liveSse(t)
  const upload = await mountUpload(t, { streams: { [RECOGNIZE]: live.respond } })
  await saveTemplate(upload)
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.equal(upload.client.localStorage.getItem(ORIENTATION_POINTER), null)
  live.send('progress', { batch_id: 'b-77', current: 0, total: 1, message: '正在识别' })
  await upload.settle()
  assert.equal(upload.client.localStorage.getItem(ORIENTATION_POINTER), 'b-77')

  // A refresh while the batch is still running reads that batch back.
  const saved = upload.storage()
  upload.unmount()
  const refreshed = await refreshedPage(t, 'b-77', batchState('b-77', { status: 'recognizing_remaining' }), { localStorage: saved, timers: true })
  assert.equal(refreshed.requests(orientationRoute('b-77')).length, 1)
  assert.match(refreshed.text(), /识别未完成/)
})

test('unfinished recognition can resume from the durable endpoint after refresh', async (t) => {
  for (const state of [
    { status: 'recognizing_remaining' },
    { status: 'recognition_interrupted' },
    { status: 'failed', retryable_failed_papers: 1, failed_papers: 1 },
  ]) {
    const upload = await refreshedPage(t, 'b1', batchState('b1', state), {
      timers: true,
      streams: { [resumeRoute('b1')]: UNFINISHED('b1') },
    })
    assert.equal(upload.fetches(resumeRoute('b1')).length, 0, `${state.status}: a refresh only offers the resume`)
    await upload.click(RESUME_BUTTON)
    assert.equal(upload.fetches(resumeRoute('b1')).length, 1, `${state.status}: resumed from the saved batch`)
    assert.equal(upload.fetches(resumeRoute('b1'))[0].init.method, 'POST')
    upload.unmount()
  }
})

test('a refreshed batch with nothing retryable or unfinished offers no resume', async (t) => {
  const upload = await refreshedPage(t, 'b1', batchState('b1', {
    status: 'failed',
    failed_papers: 1,
    retryable_failed_papers: 0,
    unresolved_papers: 0,
    papers: [{ paper_id: 'p1', filename: 'a.png', ocr_status: 'failed', error: '图片无法识别' }],
  }))
  assert.equal(upload.button(RESUME_BUTTON), null)
  assert.ok(alerts(upload).some(text => /不可自动恢复/.test(text)), alerts(upload).join(' | '))
})

test('recognition resume POSTs the saved batch id to the durable endpoint and returns the event stream', async (t) => {
  const { api } = await loadApiClient({ localStorage: { mathagent_token: 'tok-resume' } })
  const stream = new Response('event: progress\ndata: {}\n\n', { headers: { 'Content-Type': 'text/event-stream' } })
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => stream)
  const controller = new AbortController()

  const response = await api.grade.recognitionResume('batch 7/a', controller.signal)

  assert.equal(response, stream)
  assert.equal(fetchMock.mock.callCount(), 1)
  const [url, init] = fetchMock.mock.calls[0].arguments
  assert.equal(url, '/api/batch-grade/batch%207%2Fa/recognize/resume')
  assert.equal(init.method, 'POST')
  assert.equal(init.signal, controller.signal)
  assert.equal(init.headers.Authorization, 'Bearer tok-resume')
})

test('accelerator waiting auto-resumes with a bounded manual fallback', async (t) => {
  let resumes = 0
  const live = liveSse(t)
  const upload = await refreshedPage(t, 'b1', batchState('b1', { status: 'recognition_interrupted' }), {
    timers: true,
    streams: {
      // The busy accelerator and the batch lock both answer "wait".
      [resumeRoute('b1')]: () => {
        resumes += 1
        const code = resumes % 2 ? 'ACCELERATOR_WAITING' : 'BATCH_RECOGNITION_BUSY'
        return sseResponse([['error', { code, message: '加速节点仍在处理' }]])
      },
    },
  })
  const resumed = () => upload.fetches(resumeRoute('b1')).length
  // The refresh itself does not resume: it schedules a check of the saved batch.
  assert.equal(resumed(), 0)
  assert.match(upload.text(), /系统将在 3 秒后检查并继续.*（1\/6）/)
  await upload.tick(2999)
  assert.equal(resumed(), 0)
  await upload.tick(1)
  assert.equal(resumed(), 1)
  for (let check = 2; check <= 6; check++) await upload.tick(3000)
  assert.equal(resumed(), 6)
  assert.match(upload.text(), /自动恢复检查已达上限/)
  await upload.tick(30000)
  assert.equal(resumed(), 6, 'automatic checks stop at the limit')
  assert.equal(upload.requests(orientationRoute('b1')).length, 13, 'the refresh, then each check and each failed resume re-read the batch')

  // The manual fallback stays, unusable while the service blocks new work.
  assert.equal(upload.button(RESUME_BUTTON).disabled, false)
  upload.answer(AVAILABILITY, { status: 'unavailable', label: '暂不可用', message: '维护中' })
  await upload.tick(15000)
  assert.equal(upload.button(RESUME_BUTTON).disabled, true)
  await assert.rejects(upload.click(RESUME_BUTTON), /disabled/)
  upload.answer(AVAILABILITY, HEALTHY_SERVICE[AVAILABILITY])
  await upload.tick(15000)
  upload.stream(resumeRoute('b1'), live.respond)
  await upload.click(RESUME_BUTTON)
  assert.equal(resumed(), 7)
  assert.equal(upload.button(RESUME_BUTTON), null, 'not offered while the resume is running')
  live.send('error', { code: 'ACCELERATOR_WAITING', message: '加速节点仍在处理' })
  await upload.settle()
  assert.match(upload.text(), /系统将在 3 秒后检查并继续.*（1\/6）/, 'a manual resume starts a new round of checks')
})

test('queued and running OCR rows are unresolved rather than failed papers', async (t) => {
  const upload = await refreshedPage(t, 'b1', batchState('b1', {
    status: 'recognizing_remaining',
    uploaded_papers: 4,
    papers: [
      // A server explanation on a queued row does not make it a failure.
      { paper_id: 'p1', filename: 'a.png', student_name: '甲', ocr_status: 'queued', error: '等待教师客户端加速节点完成图片处理' },
      { paper_id: 'p2', filename: 'b.png', student_name: '乙', ocr_status: 'running' },
      { paper_id: 'p3', filename: 'c.png', student_name: '丙', ocr_status: 'succeeded', matched_count: 3, questions_count: 3 },
      { paper_id: 'p4', filename: 'd.png', student_name: '丁', ocr_status: 'failed', error: '图片无法识别' },
    ],
  }), { timers: true })
  assert.match(upload.page.root.byClass('recognize-summary').text, /已处理 2 \/ 4 张；成功 1 张，失败 1 张，需人工复核 0 张，未完成 2 张。/)
  assert.deepEqual(paperRows(upload).map(row => row[3]), ['等待教师客户端加速节点完成图片处理', '正在识别处理', '3 / 3', '图片无法识别'])
  const table = upload.page.root.byClass('result-table')
  assert.deepEqual(table.allByClass('tag-pending').map(el => el.text), ['等待教师客户端加速节点完成图片处理', '正在识别处理'])
  assert.deepEqual(table.allByClass('tag-fail').map(el => el.text), ['图片无法识别'])
  assert.doesNotMatch(upload.text(), /等待加速节点返回处理结果/)
})

test('saved-batch resume bypasses the accelerator free-slot gate but new uploads do not', async (t) => {
  const upload = await refreshedPage(t, 'b1', batchState('b1', { status: 'failed', unresolved_papers: 1 }), {
    responses: {
      [TEMPLATE_AVAILABILITY]: { ...HEALTHY_SERVICE[TEMPLATE_AVAILABILITY], registration_hardware: { status: 'waiting_accelerator' } },
    },
    streams: { [resumeRoute('b1')]: UNFINISHED('b1') },
  })
  assert.equal(upload.page.setup.recognitionBlocksNewWork, true, 'the registration accelerator has no free slot')
  // Student pages of this session, ready for a new upload.
  upload.page.setup.fileList = [new File(['jpeg'], 'a.ocr-ready-v1.jpg', { type: 'image/jpeg' })]
  await upload.settle()
  assert.equal(upload.button('重新识别全部').disabled, true)
  await upload.page.setup.runRecognize()
  upload.page.setup.onStartRecognize()
  await upload.settle()
  assert.equal(upload.fetches(RECOGNIZE).length, 0)
  assert.deepEqual(upload.client.messages.filter(message => message.type === 'error').map(message => message.message), [
    '加速节点不可用，请使用客户端',
    '加速节点不可用，请使用客户端',
  ])
  const batchDialog = upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '确认批次信息')
  assert.equal(batchDialog.attributes['data-open'], 'false')

  await upload.click(RESUME_BUTTON)
  assert.equal(upload.fetches(resumeRoute('b1')).length, 1)
})

test('failed resume refreshes durable state and explains terminal failures', async (t) => {
  // A new upload that fails after the server created its batch.
  const first = await mountUpload(t, {
    responses: {
      [orientationRoute('b-new')]: batchState('b-new', {
        status: 'failed',
        failed_papers: 1,
        papers: [{ paper_id: 'p1', filename: 'a.png', ocr_status: 'failed', error: '图片无法识别' }],
      }),
    },
    streams: {
      [RECOGNIZE]: [
        ['progress', { batch_id: 'b-new', current: 0, total: 1 }],
        ['error', { code: 'OCR_RETRY_EXHAUSTED', message: '识别重试次数已用完' }],
      ],
    },
  })
  await saveTemplate(first)
  await first.choosePapers([paperFile('a.png')])
  assert.equal(first.fetches(RECOGNIZE).length, 1)
  assert.equal(first.requests(orientationRoute('b-new')).length, 1)
  assert.ok(alerts(first).some(text => /识别重试已耗尽/.test(text)), alerts(first).join(' | '))
  first.unmount()

  // Resuming a saved batch that fails for good, and one the service fails.
  for (const [code, explanation] of [
    ['OCR_NO_RETRYABLE_FAILURES', /当前失败试卷不可自动恢复/],
    ['GRADING_CHAIN_FAILED', /批改链路当前故障，服务恢复后可继续处理未完成试卷/],
  ]) {
    const upload = await refreshedPage(t, 'b1', batchState('b1', { status: 'failed', unresolved_papers: 1 }), {
      streams: { [resumeRoute('b1')]: [['error', { code, message: '恢复失败' }]] },
    })
    assert.equal(upload.requests(orientationRoute('b1')).length, 1)
    await upload.click(RESUME_BUTTON)
    assert.equal(upload.fetches(resumeRoute('b1')).length, 1)
    assert.equal(upload.requests(orientationRoute('b1')).length, 2, `${code}: the failure re-reads the batch once`)
    assert.ok(alerts(upload).some(text => explanation.test(text)), `${code}: ${alerts(upload).join(' | ')}`)
    upload.unmount()
  }
})

test('recognition replay replaces papers by stable paper id', async (t) => {
  const live = liveSse(t)
  const upload = await refreshedPage(t, 'b1', batchState('b1', {
    status: 'failed',
    uploaded_papers: 3,
    unresolved_papers: 3,
    papers: [
      { paper_id: 'p1', filename: 'a.png', student_name: '甲', ocr_status: 'queued' },
      { filename: 'b.png', student_name: '乙', ocr_status: 'queued' },
    ],
  }), { streams: { [resumeRoute('b1')]: live.respond } })
  await upload.click(RESUME_BUTTON)
  // The resumed stream replays every paper's current outcome.
  live.send('paper', { batch_id: 'b1', paper_id: 'p1', filename: 'a-renamed.png', student_name: '甲', matched_count: 2, questions_count: 2 })
  live.send('paper', { batch_id: 'b1', filename: 'b.png', student_name: '乙', matched_count: 1, questions_count: 2 })
  // A paper with an id never takes over a row by its filename.
  live.send('paper', { batch_id: 'b1', paper_id: 'p3', filename: 'b.png', student_name: '丙', matched_count: 2, questions_count: 2 })
  await upload.settle()
  assert.deepEqual(paperRows(upload), [
    ['1', 'a-renamed.png', '甲', '2 / 2'],
    ['2', 'b.png', '乙', '1 / 2'],
    ['3', 'b.png', '丙', '2 / 2'],
  ])
})

test('recognition paper events replace stale outcome fields instead of shallow-merging', async (t) => {
  const live = liveSse(t)
  const upload = await refreshedPage(t, 'b1', batchState('b1', {
    status: 'failed',
    unresolved_papers: 1,
    papers: [{ paper_id: 'p1', filename: 'a.png', student_name: '甲', ocr_status: 'queued', error: '等待教师客户端加速节点完成图片处理' }],
  }), { streams: { [resumeRoute('b1')]: live.respond } })
  assert.deepEqual(paperRows(upload), [['1', 'a.png', '甲', '等待教师客户端加速节点完成图片处理']])
  await upload.click(RESUME_BUTTON)
  // A successful outcome omits the failure fields instead of sending them empty.
  live.send('paper', { batch_id: 'b1', paper_id: 'p1', filename: 'a.png', student_name: '甲', matched_count: 3, questions_count: 3 })
  await upload.settle()
  assert.deepEqual(paperRows(upload), [['1', 'a.png', '甲', '3 / 3']])
  assert.deepEqual(Object.keys(upload.page.setup.papers[0]).filter(key => ['error', 'ocr_status'].includes(key)), [])
})
