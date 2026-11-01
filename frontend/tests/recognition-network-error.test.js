import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const grade = readFileSync(new URL('../../routers/grade.py', import.meta.url), 'utf8')

test('student recognition replaces generic browser network errors with an actionable message', () => {
  assert.match(upload, /rawMessage === 'Failed to fetch'/)
  assert.match(upload, /rawMessage === 'Network Error'/)
  assert.match(upload, /识别连接中断：服务器可能正在重启或预处理超时/)
  assert.match(upload, /请勿连续重复提交/)
})

test('student recognition emits progress while local preprocessing is still running', () => {
  // The recognition streams consume one shared OCR/grading multiplexer that
  // wakes on either result and otherwise emits a heartbeat.
  assert.match(grade, /aclosing\(_ocr_and_grading_events\(result_stream, pipeline\)\)/)
  assert.match(
    grade,
    /asyncio\.wait\(\s*\{next_result, next_graded\}, timeout=_GRADE_HEARTBEAT_SECONDS,\s*return_when=asyncio\.FIRST_COMPLETED,\s*\)/,
  )
  assert.match(grade, /正在进行本地配准与答案区域预处理，请稍候/)
})
