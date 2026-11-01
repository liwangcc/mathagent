import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('grading upload does not advertise unsupported PDF input', () => {
  assert.doesNotMatch(source, /accept="image\/\*,\.pdf"/)
  assert.doesNotMatch(source, /支持图片 \/ PDF/)
  assert.match(source, /批改仅支持图片/)
})
