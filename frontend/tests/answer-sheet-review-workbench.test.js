import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const viewUrl = new URL('../src/views/AnswerSheet.vue', import.meta.url)
const apiUrl = new URL('../src/api/index.js', import.meta.url)
const recordsUrl = new URL('../src/views/GradeRecords.vue', import.meta.url)

test('answer-sheet draft uses the full protected student pages beside grading details', async () => {
  const source = await readFile(viewUrl, 'utf8')

  assert.match(source, /aria-label="完整学生答题卡"/)
  assert.match(source, /currentReviewPageIndex \+ 1/)
  assert.match(source, /currentDraftPages\.length/)
  assert.match(source, /aria-label="上一页"/)
  assert.match(source, /aria-label="下一页"/)
  assert.match(source, /api\.answerSheet\.studentPageImage\(sessionId, paperId\)/)
  assert.match(source, /currentDraftResult\.details/)
  assert.match(source, /openDetailPage\(detail\)/)
  assert.match(source, /detail\.paper_ids/)
})

test('persisted grading drafts restore the review workbench and gate archive on review count', async () => {
  const source = await readFile(viewUrl, 'utf8')

  assert.match(source, /gradingDrafts\.value = structuredClone\(data\.grouping_decisions \|\| \[\]\)/)
  assert.match(source, /draft => Number\(draft\.result\?\.needs_review_count \|\| 0\) === 0/)
  assert.match(source, /:disabled="!canArchiveDrafts"/)
  assert.match(source, /URL\.revokeObjectURL\(studentPageImageUrl\.value\)/)
})

test('student answer-sheet image API uses an authenticated blob URL, not a public path', async () => {
  const source = await readFile(apiUrl, 'utf8')

  assert.match(source, /studentPageImage:\s*async \(sessionId, paperId\)/)
  assert.match(source, /students\/\$\{encodeURIComponent\(paperId\)\}\/image/)
  assert.match(source, /responseType:\s*'blob'/)
  assert.match(source, /return URL\.createObjectURL\(blob\)/)
})

test('teacher can save complete per-question verdicts with review revision', async () => {
  const source = await readFile(viewUrl, 'utf8')
  const apiSource = await readFile(apiUrl, 'utf8')

  assert.match(source, /人工判定/)
  assert.match(source, /value="correct"/)
  assert.match(source, /value="partial"/)
  assert.match(source, /value="wrong"/)
  assert.match(source, /saveDraftDecision/)
  assert.match(source, /review_revision:\s*session\.value\.review_revision/)
  assert.match(source, /applySession\(latest, \{ chooseStage: false \}\)/)
  assert.match(apiSource, /gradingDecision:\s*\(sessionId, payload\)/)
  assert.match(apiSource, /grading\/decision/)
})

test('formal grading record reopens its protected archived answer-sheet session', async () => {
  const source = await readFile(viewUrl, 'utf8')
  const records = await readFile(recordsUrl, 'utf8')

  assert.match(records, /detail\.source_assets\?\.source === 'answer_sheet'/)
  assert.match(records, /detail\.source_assets\?\.available !== false/)
  assert.match(records, /查看完整答题卡原卷/)
  assert.match(records, /原卷已按隐私保留策略清理/)
  assert.match(records, /逐题批改结果、错题、得分和正确率仍保留/)
  assert.match(records, /query:\s*\{ session: sessionId \}/)
  assert.match(source, /route\.query\.session \|\| localStorage\.getItem\(STORAGE_KEY\)/)
  assert.match(source, /api\.answerSheet\.getSession\(saved\)/)
})
