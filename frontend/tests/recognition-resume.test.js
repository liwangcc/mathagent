import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('recognition checkpoints its batch id on the first progress event', () => {
  assert.match(upload, /if \(evt === 'progress'\) \{\s*rememberRecognitionBatch\(data\)/)
  assert.match(upload, /function rememberRecognitionBatch\(payload\)/)
  assert.match(upload, /localStorage\.setItem\(key, String\(id\)\)/)
})

test('unfinished recognition can resume from the durable endpoint after refresh', () => {
  assert.match(upload, /async function resumeRecognitionBatch\(resumeBatchId, \{ automatic = false \} = \{\}\)/)
  assert.match(upload, /\['recognizing_remaining', 'recognition_interrupted'\]/)
  assert.match(upload, /Number\(payload\?\.retryable_failed_papers \|\| 0\)/)
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

test('accelerator waiting auto-resumes with a bounded manual fallback', () => {
  const orientationResume = upload.match(
    /async function resumeOrientationBatch\(\) \{([\s\S]*?)\n\}\n\nasync function refreshRecognitionStateAfterFailure/,
  )
  assert.ok(orientationResume, 'resumeOrientationBatch function should be present')
  assert.doesNotMatch(orientationResume[1], /resumeRecognitionBatch/)
  assert.match(upload, /const ACCELERATOR_AUTO_RESUME_LIMIT = 6/)
  assert.match(upload, /function scheduleRecognitionAutoResume\(targetBatchId\)/)
  assert.match(upload, /\['ACCELERATOR_WAITING', 'BATCH_RECOGNITION_BUSY'\]\.includes\(code\)/)
  assert.match(orientationResume[1], /recoverRecognitionConnection\(savedBatchId, payload\)/)
  assert.match(upload, /resumeRecognitionBatch\(target, \{ automatic: true \}\)/)
  assert.match(upload, />\s*继续处理未完成试卷\s*<\/button>/)
  assert.match(upload, /:disabled="recognizing \|\| serviceBlocksNewWork"\s+@click="resumeRecognitionBatch\(\)"/)
})

test('queued and running OCR rows are unresolved rather than failed papers', () => {
  assert.match(upload, /function paperOcrPending\(paper\)[\s\S]*\['queued', 'running'\]/)
  assert.match(upload, /function paperOcrFailed\(paper\)[\s\S]*if \(status\) return status === 'failed'/)
  assert.match(upload, /已处理 \{\{ recognizeCompletedCount \}\}[\s\S]*未完成 \{\{ recognizeUnresolvedCount \}\}/)
  assert.match(upload, /paperOcrPending\(p\)[^>]*>\{\{ paperOcrPendingLabel\(p\) \}\}/)
  assert.doesNotMatch(upload, /等待加速节点返回处理结果/)
})

test('saved-batch resume bypasses the accelerator free-slot gate but new uploads do not', () => {
  const recognitionResume = upload.match(
    /async function resumeRecognitionBatch\(resumeBatchId, \{ automatic = false \} = \{\}\) \{([\s\S]*?)\n\}\n\nfunction reviewStorageKey/,
  )
  assert.ok(recognitionResume, 'resumeRecognitionBatch function should be present')
  assert.match(recognitionResume[1], /if \(!requireBatchService\(\)\) return false/)
  assert.doesNotMatch(recognitionResume[1], /requireRecognitionService\(\)/)
  assert.match(upload, /function onStartRecognize\(\) \{\s*if \(!requireRecognitionService\(\)\) return/)
  assert.match(upload, /async function runRecognize\(\) \{\s*if \(!requireRecognitionService\(\)\) return/)
})

test('failed resume refreshes durable state and explains terminal failures', () => {
  const recognitionResume = upload.match(
    /async function resumeRecognitionBatch\(resumeBatchId, \{ automatic = false \} = \{\}\) \{([\s\S]*?)\n\}\n\nfunction reviewStorageKey/,
  )
  assert.ok(recognitionResume, 'resumeRecognitionBatch function should be present')
  assert.match(recognitionResume[1], /await refreshRecognitionStateAfterFailure\(targetBatchId/)
  assert.equal(
    (upload.match(/await refreshRecognitionStateAfterFailure\(targetBatchId/g) || []).length,
    2,
    'initial recognition and resume share their refresh for transport, accelerator-waiting and terminal failures',
  )
  assert.match(upload, /const payload = await api\.grade\.orientationState\(targetBatchId\)/)
  assert.match(upload, /OCR_RETRY_EXHAUSTED/)
  assert.match(upload, /OCR_NO_RETRYABLE_FAILURES/)
  assert.match(upload, /识别重试已耗尽/)
  assert.match(upload, /不可自动恢复/)
  assert.match(upload, /批改链路当前故障/)
})

test('recognition replay replaces papers by stable paper id', () => {
  assert.match(upload, /function upsertRecognitionPaper\(payload\)/)
  assert.match(upload, /String\(item\.paper_id \|\| ''\) === paperId/)
  assert.match(upload, /else if \(evt === 'paper'\) \{\s*upsertRecognitionPaper\(data\)/)
})

test('recognition paper events replace stale outcome fields instead of shallow-merging', () => {
  const upsert = upload.match(/function upsertRecognitionPaper\(payload\) \{([\s\S]*?)\n\}/)
  assert.ok(upsert, 'upsertRecognitionPaper function should be present')
  assert.match(upsert[1], /papers\.value\[index\] = mergeRecognitionPaper\(papers\.value\[index\], payload\)/)
  assert.doesNotMatch(upsert[1], /\{ \.\.\.papers\.value\[index\], \.\.\.payload \}/)
})
