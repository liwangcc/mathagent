import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const apiSource = fs.readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8')

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
  assert.match(apiSource, /templateRecognitionState: async \(templateId, signal\)/)
  assert.match(apiSource, /batch-grade\/template\/\$\{encodeURIComponent\(templateId\)\}\/recognition/)
})
