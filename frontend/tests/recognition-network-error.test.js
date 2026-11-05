import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('student recognition replaces generic browser network errors with an actionable message', () => {
  assert.match(upload, /rawMessage === 'Failed to fetch'/)
  assert.match(upload, /rawMessage === 'Network Error'/)
  assert.match(upload, /识别连接中断：服务器可能正在重启或预处理超时/)
  assert.match(upload, /请勿连续重复提交/)
})

// The backend half of this contract (heartbeats while preprocessing, OCR and
// grading are still running) is behaviour-tested in
// tests/test_recognition_heartbeats.py instead of matching routers/grade.py text.
