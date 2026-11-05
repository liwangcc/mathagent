import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { loadApiClient } from './helpers/apiClient.js'

const source = fs.readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('template recognition resumes detached server work after an SSE disconnect', () => {
  assert.match(source, /templateRecognizeMessage\.value = data\.message/)
  assert.match(source, /templateRecognizeError/)
  assert.match(source, /识别连接已结束，但没有收到模板结果/)
  assert.match(source, /response\.headers\.get\('X-Template-ID'\)/)
  assert.match(source, /waitForTemplateRecognition\(tid, signal\)/)
  assert.match(source, /templateRecognitionState\(templateId, signal\)/)
  assert.match(source, /模板识别连接中断，正在恢复服务器任务/)
  assert.match(source, /无法读取服务器任务状态/)
  assert.match(source, /rememberTemplateRecognition\(tid, templateMode\.value\)/)
  assert.match(source, /resumeTemplateRecognitionCheckpoint\(\)/)
  assert.match(source, /mathagent_template_recognition/)
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
