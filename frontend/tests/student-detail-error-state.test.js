import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


test('student detail distinguishes a load fault from an empty wrong-question history', async () => {
  const source = await readFile(
    new URL('../src/views/StudentDetail.vue', import.meta.url),
    'utf8',
  )

  assert.match(source, /label="原题号"/)
  assert.match(source, /v-if="loadError"/)
  assert.match(source, /学生详情加载失败，当前数据可能不是最新，请重试。/)
  assert.match(source, /loadError\.value = ''/)
  assert.doesNotMatch(source, /catch \{[\s\S]{0,300}detail\.wrong_questions = \[\]/)
})
