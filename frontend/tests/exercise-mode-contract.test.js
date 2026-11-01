import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

const source = fs.readFileSync(
  path.resolve(import.meta.dirname, '../src/views/Exercise.vue'),
  'utf8',
)

test('exercise page separates weakness practice from original correction papers', () => {
  assert.match(source, /个人薄弱巩固/)
  assert.match(source, /班级共性强化/)
  assert.match(source, /correction_scope/)
  assert.match(source, /班级统一卷/)
  assert.match(source, /correctionCount/)
  assert.match(source, /payload\.chapters = \[\.\.\.weaknessForm\.chapters\]/)
  assert.match(source, /payload\.topics = \[\.\.\.weaknessForm\.topics\]/)
})

test('exercise generation is capped at 22 questions for the answer-sheet layout', () => {
  assert.match(source, /correctionCount" :min="1" :max="22"/)
  assert.match(source, /总数最多 22 道/)
  assert.match(source, /typeCounts\.solution" :min="0" :max="6"/)
  assert.match(source, /解答题最多 6 道/)
  assert.match(source, /totalCount\.value > 22/)
  assert.match(source, /单次出题最多 22 道/)
})

test('exercise exports retain the generated teaching snapshot rather than the current profile', () => {
  assert.match(source, /paperTeachingContext = ref\(\{\}\)/)
  assert.match(source, /paperTeachingContext\.value = \{ \.\.\.\(data\.teaching_context \|\| \{\}\) \}/)
  assert.equal((source.match(/teaching_context: paperTeachingContext\.value/g) || []).length, 2)
  assert.doesNotMatch(source, /teaching_context: auth\.user/)
  assert.match(source, /新建出题：\{\{ profileStageLabel \}\} · 自动跟随个人资料/)
  assert.match(source, /\{\{ stageLabel \}\} · 本试卷学段已固定/)
})
