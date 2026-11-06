import assert from 'node:assert/strict'
import test from 'node:test'
import { sseResponse } from './helpers/memoryFetch.js'
import { mountUpload, paperFile } from './helpers/uploadPage.js'

// What the real Upload page tells the teacher when student recognition fails:
// a dropped connection gets an actionable explanation instead of the
// browser's generic network error text.

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const INTERRUPTED = '识别连接中断：服务器可能正在重启或预处理超时，请勿连续重复提交；稍后重试，若仍失败请联系管理员查看该时段服务日志。'

// Starts recognition (saved template + chosen papers) against `answer` and
// returns the page's alerts once the run has failed.
async function failedRecognition(t, answer) {
  const upload = await mountUpload(t, { streams: { [RECOGNIZE]: answer } })
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.equal(upload.page.setup.recognizing, false)
  const alerts = upload.page.root.findAll(el => el.getAttribute('role') === 'alert').map(el => el.text)
  upload.unmount()
  return alerts
}

test('student recognition replaces generic browser network errors with an actionable message', async (t) => {
  for (const error of [
    new TypeError('Failed to fetch'),
    new Error('Network Error'),
    new TypeError('NetworkError when attempting to fetch resource.'),
  ]) {
    const alerts = await failedRecognition(t, () => { throw error })
    assert.ok(alerts.includes(INTERRUPTED), `${error.message}: ${alerts.join(' | ')}`)
    assert.ok(!alerts.some(text => text.includes(error.message)), `${error.message} is not shown as it is`)
  }
})

test('an event stream cut before its result is reported as an interrupted recognition connection', async (t) => {
  const alerts = await failedRecognition(t, () => sseResponse([['progress', { current: 0, total: 1 }]]))
  assert.ok(alerts.includes(INTERRUPTED), alerts.join(' | '))
})

test('other recognition failures keep their own explanation', async (t) => {
  const server = await failedRecognition(t, () => sseResponse([['error', { code: 'TEMPLATE_NOT_CONFIRMED', message: '本次模板尚未确认，请重新保存模板' }]]))
  assert.ok(server.includes('本次模板尚未确认，请重新保存模板'), server.join(' | '))
  assert.ok(!server.includes(INTERRUPTED))
  const unexplained = await failedRecognition(t, () => { throw new Error('') })
  assert.ok(unexplained.includes('识别失败'), unexplained.join(' | '))
  assert.ok(!unexplained.includes(INTERRUPTED))
})
