import test from 'node:test'
import assert from 'node:assert/strict'
import { loadApiClient } from './helpers/apiClient.js'
import { jsonResponse, sseResponse } from './helpers/memoryFetch.js'
import {
  templateCheckpointKey, templateJob, templateJobRoute, templateServer,
} from './helpers/templateReviewFlow.js'
import { formFields, liveSse, mountUpload, paperFile } from './helpers/uploadPage.js'

// Template recognition on the real Upload page when the event stream breaks:
// the server keeps working on the job it announced (X-Template-ID), so the
// page reads that detached job back instead of failing the import.

const TEMPLATE_RECOGNIZE = 'POST /api/batch-grade/template/recognize'
const TID = 'tpl-detached'
const PAGES = [{
  page_index: 0,
  questions: [{ question_no: '1', question_type: 'fill', blank_count: 1, standard_answer: '7', question_text: '解方程 x + 3 = 10' }],
}]
const CHECKPOINT = templateCheckpointKey()
const UNREADABLE_JOB = '模板识别连接中断，且无法读取服务器任务状态。请勿连续重复提交；稍后重试，如仍失败请联系管理员。'

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

// The import stream as the server answers it: the job id in a header, the
// events sent by the test.
function announcedStream(live, templateId = TID) {
  return (request) => {
    const response = live.respond(request)
    const headers = { 'Content-Type': 'text/event-stream' }
    if (templateId) headers['X-Template-ID'] = templateId
    return new Response(response.body, { status: 200, headers })
  }
}

// The page with the template server of helpers/templateReviewFlow.js, the
// import stream and the detached job's state answers (`job`).
async function mountImport(t, { recognize, job, ...options } = {}) {
  const server = templateServer(TID, PAGES)
  return mountUpload(t, {
    timers: true,
    ...options,
    streams: { ...server.streams, [TEMPLATE_RECOGNIZE]: recognize, [templateJobRoute(TID)]: job },
    responses: server.responses,
  })
}

async function chooseTemplate(upload, files = [paperFile('template-1.png')]) {
  const input = upload.page.root.byClass('template-card').find(el => el.tagName === 'input' && el.type === 'file')
  input.files = files
  await input.dispatch('change')
  await upload.settle()
}

const editor = upload => upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
const questionText = upload => editor(upload).find(el => el.tagName === 'textarea' && el.getAttribute('placeholder') === '请输入题干')?.value
const recognizeState = upload => upload.page.root.byClass('tpl-recognize-state')?.text ?? ''
const recognizeError = upload => upload.page.root.byClass('template-card').find(el => el.getAttribute('role') === 'alert' && el.classList.contains('tpl-cut-preview-error'))?.text ?? ''
const checkpoint = upload => JSON.parse(upload.client.localStorage.getItem(CHECKPOINT) || 'null')

test('template recognition resumes detached server work after an SSE disconnect', async (t) => {
  const live = liveSse(t)
  const reads = [deferred(), deferred()]
  const jobAnswers = reads.map(read => read.promise)
  const upload = await mountImport(t, { recognize: announcedStream(live), job: () => jobAnswers.shift() })
  await chooseTemplate(upload)
  await upload.click('识别模板')

  const [sent] = upload.fetches(TEMPLATE_RECOGNIZE)
  assert.equal(formFields(sent.init.body).category, 'daily')
  // The announced job is checkpointed for a reload before any result arrives.
  assert.deepEqual(checkpoint(upload), { template_id: TID, category: 'daily', exam_name: '', template_mode: 'marked', column_mode: 'double' })

  live.send('progress', { message: '正在识别模板第 1 页' })
  await upload.settle()
  assert.match(recognizeState(upload), /正在识别模板第 1 页/)
  assert.equal(upload.button('识别中...').disabled, true)

  // The connection ends without a result: the page asks the server for the job.
  live.close()
  await upload.settle()
  assert.match(recognizeState(upload), /模板识别连接中断，正在恢复服务器任务…/)
  assert.equal(recognizeError(upload), '')
  const jobReads = () => upload.fetches(templateJobRoute(TID))
  assert.equal(jobReads().length, 1)
  assert.equal(jobReads()[0].url, `/api/batch-grade/template/${TID}/recognition`)
  assert.equal(jobReads()[0].init.headers.Authorization, 'Bearer teacher-token')
  assert.ok(jobReads()[0].init.signal instanceof AbortSignal)
  assert.equal(jobReads()[0].init.signal.aborted, false)

  // The server says when to ask again (not the page's 1 s default).
  reads[0].resolve(jsonResponse({ status: 'running', message: '服务器仍在识别第 1 页', retry_after_ms: 2500 }))
  await upload.settle()
  assert.match(recognizeState(upload), /服务器仍在识别第 1 页/)
  await upload.tick(2499)
  assert.equal(jobReads().length, 1)
  await upload.tick(1)
  assert.equal(jobReads().length, 2)
  reads[1].resolve(jsonResponse(templateJob(TID, PAGES)))
  await upload.settle()

  assert.equal(editor(upload).attributes['data-open'], 'true')
  assert.match(editor(upload).text, /第 1 页（1 题）/)
  assert.equal(questionText(upload), '解方程 x + 3 = 10')
  assert.equal(recognizeState(upload), '')
  assert.ok(upload.button('识别模板'))
  assert.equal(upload.fetches(TEMPLATE_RECOGNIZE).length, 1, 'the import is never sent twice')
  // The draft stays recoverable until the teacher saves it.
  assert.equal(checkpoint(upload).template_id, TID)
  await upload.click('保存模板（全部页）')
  assert.equal(upload.client.localStorage.getItem(CHECKPOINT), null)
})

test('a finished stream without template pages is recovered from the announced job', async (t) => {
  const upload = await mountImport(t, {
    recognize: (request) => {
      const response = sseResponse([['done', { pages: [] }]])
      return new Response(response.body, { status: 200, headers: { 'Content-Type': 'text/event-stream', 'X-Template-ID': TID } })
    },
    job: () => jsonResponse(templateJob(TID, PAGES)),
  })
  await chooseTemplate(upload)
  await upload.click('识别模板')
  assert.equal(upload.fetches(templateJobRoute(TID)).length, 1)
  assert.equal(editor(upload).attributes['data-open'], 'true')
  assert.equal(recognizeError(upload), '')
})

test('without an announced job a cut template stream fails with a clear error', async (t) => {
  const live = liveSse(t)
  const upload = await mountImport(t, { recognize: announcedStream(live, ''), job: () => jsonResponse(templateJob(TID, PAGES)) })
  await chooseTemplate(upload)
  await upload.click('识别模板')
  live.send('progress', { message: '正在识别模板第 1 页' })
  live.close()
  await upload.settle()
  assert.equal(upload.fetches(templateJobRoute(TID)).length, 0, 'there is no job to read back')
  assert.equal(recognizeError(upload), UNREADABLE_JOB)
  assert.equal(recognizeState(upload), '')
  assert.equal(editor(upload).attributes['data-open'], 'false')
  assert.equal(checkpoint(upload), null)
})

test('a detached job whose state cannot be read ends with a clear error and stays resumable', async (t) => {
  const live = liveSse(t)
  const upload = await mountImport(t, {
    recognize: announcedStream(live),
    job: () => { throw new TypeError('Failed to fetch') },
  })
  await chooseTemplate(upload)
  await upload.click('识别模板')
  live.close()
  await upload.settle()
  const jobReads = () => upload.fetches(templateJobRoute(TID)).length
  assert.equal(jobReads(), 1)
  assert.match(recognizeState(upload), /模板仍在服务器处理，正在重新连接任务状态…/)
  // Retries back off: 1 s, then 2 s; the third failed read gives up.
  await upload.tick(1000)
  assert.equal(jobReads(), 2)
  await upload.tick(1999)
  assert.equal(jobReads(), 2)
  await upload.tick(1)
  assert.equal(jobReads(), 3)
  assert.equal(recognizeError(upload), UNREADABLE_JOB)
  assert.equal(recognizeState(upload), '')
  assert.ok(upload.client.messages.some(message => message.type === 'error' && message.message === UNREADABLE_JOB))
  await upload.tick(10000)
  assert.equal(jobReads(), 3)
  // A network failure is not a verdict on the job: the checkpoint stays.
  assert.equal(checkpoint(upload).template_id, TID)
})

test('a reload resumes the checkpointed template job', async (t) => {
  const live = liveSse(t)
  const first = await mountImport(t, { recognize: announcedStream(live), job: () => new Promise(() => {}) })
  await chooseTemplate(first)
  await first.click('识别模板')
  const saved = first.storage()
  first.unmount()

  const reloaded = await mountImport(t, { localStorage: saved, job: () => jsonResponse(templateJob(TID, PAGES)) })
  assert.equal(reloaded.fetches(templateJobRoute(TID)).length, 1)
  assert.equal(reloaded.fetches(TEMPLATE_RECOGNIZE).length, 0, 'the reload reads the job instead of importing again')
  assert.equal(editor(reloaded).attributes['data-open'], 'true')
  assert.ok(reloaded.client.messages.some(message => message.type === 'success' && message.message === '已恢复服务器完成的模板识别结果'))
})

test('a reload that cannot read the job state keeps the checkpoint for the next reload', async (t) => {
  const live = liveSse(t)
  const first = await mountImport(t, { recognize: announcedStream(live), job: () => new Promise(() => {}) })
  await chooseTemplate(first)
  await first.click('识别模板')
  const saved = first.storage()
  first.unmount()

  // The network is down after the reload: three reads (1 s, then 2 s apart) fail.
  const offline = await mountImport(t, { localStorage: saved, job: () => { throw new TypeError('Failed to fetch') } })
  const jobReads = () => offline.fetches(templateJobRoute(TID)).length
  assert.equal(jobReads(), 1)
  assert.match(recognizeState(offline), /模板仍在服务器处理，正在重新连接任务状态…/)
  await offline.tick(1000)
  assert.equal(jobReads(), 2)
  await offline.tick(2000)
  assert.equal(jobReads(), 3)
  assert.equal(offline.fetches(TEMPLATE_RECOGNIZE).length, 0, 'the reload never imports again')
  // The third failure ends the resume: no busy state, the failed read reported.
  assert.equal(recognizeState(offline), '')
  assert.equal(recognizeError(offline), 'Failed to fetch')
  assert.ok(offline.client.messages.some(message => message.type === 'error' && message.message === 'Failed to fetch'))
  assert.equal(editor(offline).attributes['data-open'], 'false')
  await offline.tick(10000)
  assert.equal(jobReads(), 3)
  // A network failure is not a verdict on the job: the checkpoint stays.
  assert.equal(checkpoint(offline).template_id, TID)
  const kept = offline.storage()
  offline.unmount()

  // The next reload, with the network back, resumes the same job.
  const resumed = await mountImport(t, { localStorage: kept, job: () => jsonResponse(templateJob(TID, PAGES)) })
  assert.equal(resumed.fetches(templateJobRoute(TID)).length, 1)
  assert.equal(editor(resumed).attributes['data-open'], 'true')
  assert.ok(resumed.client.messages.some(message => message.type === 'success' && message.message === '已恢复服务器完成的模板识别结果'))
})

test('template recognition state is read from the detached job endpoint with the teacher token and caller signal', async (t) => {
  const { api } = await loadApiClient({ localStorage: { mathagent_token: 'tok-state' } })
  const state = { status: 'running', message: '正在识别第 2 页' }
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => Response.json(state))
  const controller = new AbortController()

  assert.deepEqual(await api.grade.templateRecognitionState('tpl 1/2', controller.signal), state)

  assert.equal(fetchMock.mock.callCount(), 1)
  const [url, init] = fetchMock.mock.calls[0].arguments
  assert.equal(url, '/api/batch-grade/template/tpl%201%2F2/recognition')
  assert.equal(init.method ?? 'GET', 'GET')
  assert.equal(init.signal, controller.signal)
  assert.equal(init.headers.Authorization, 'Bearer tok-state')

  fetchMock.mock.mockImplementation(async () => Response.json({ detail: 'busy' }, { status: 503 }))
  await assert.rejects(
    api.grade.templateRecognitionState('tpl 1/2', controller.signal),
    { status: 503, sseFailureKind: 'http' },
  )
})
