import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const statsUrl = new URL('../src/views/Stats.vue', import.meta.url)

test('student statistics consumes the complete backend contract', async () => {
  const source = await readFile(statsUrl, 'utf8')

  assert.match(source, /d\.total_gradings \?\? d\.total_exams/)
  assert.match(source, /metric\.questions = d\.total_questions \?\? 0/)
  assert.match(source, /d\.accuracy[\s\S]*d\.average_correct_rate[\s\S]*d\.accuracy_percent/)
  assert.match(source, /stats\.correct_count/)
  assert.match(source, /stats\.partial_count/)
  assert.match(source, /stats\.wrong_count/)
})

test('student statistics hides empty knowledge buckets instead of showing rows of zeroes', async () => {
  const source = await readFile(statsUrl, 'utf8')

  assert.match(source, /\.filter\(\(item\) => Number\(item\.count \|\| 0\) > 0\)/)
  assert.doesNotMatch(source, /error_distribution\.map/)
})
