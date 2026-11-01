import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const uploadUrl = new URL('../src/views/Upload.vue', import.meta.url)

test('batch report keeps a compact top-five view and an expandable full distribution', async () => {
  const source = await readFile(uploadUrl, 'utf8')

  assert.match(source, /高频错题（前五道）/)
  assert.match(source, /batch-report-ring/)
  assert.match(source, /batchReportOutcomeSegments/)
  assert.match(source, /知识点分布/)
  assert.match(source, /确认后可写入学生档案与班级统计/)
  assert.match(source, /无需绑定学生，按页面查看整批学情/)
  assert.match(source, /batchReport\.stats\.all_errors\?\.length/)
  assert.match(source, /查看全部错题分布/)
  assert.match(source, /题号.*错题次数.*已判 \/ 作答.*错误率.*题干摘要/)
  assert.doesNotMatch(source, /辅助错因分布/)
  assert.match(source, /知识点分布/)
  assert.match(source, /教学建议/)
  assert.match(source, /buildClientBatchReportFallback/)
  assert.match(source, /上方题目判定统计仍然有效/)
})

test('knowledge distribution fills the card after error-type distribution removal', async () => {
  const source = await readFile(uploadUrl, 'utf8')
  assert.match(source, /\.batch-report-distributions\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/)
  assert.doesNotMatch(source, /\.batch-report-distributions\s*\{[^}]*repeat\(2,/)
  assert.match(source, /\.batch-report-bar-label span\s*\{[^}]*overflow-wrap:\s*anywhere/)
  assert.doesNotMatch(source, /\.batch-report-bar-label span\s*\{[^}]*(?:text-overflow:\s*ellipsis|white-space:\s*nowrap)/)
  assert.match(source, /\.batch-report-bar-label strong\s*\{[^}]*flex:\s*0 0 auto[^}]*white-space:\s*nowrap/)
})
