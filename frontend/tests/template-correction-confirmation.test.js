import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const upload = fs.readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('template correction shows current and historical questions and saves discoveries by default', () => {
  assert.match(upload, /发现可纠错题目/)
  assert.match(upload, /保存模板即同意建立下列关系/)
  assert.match(upload, /本次题目/)
  assert.match(upload, /历史错题/)
  assert.match(upload, /q\.correction_candidates\.map\(\(candidate\) => candidate\.cycle_id\)/)
})

test('same knowledge point is a summary, never a record the template corrects', () => {
  assert.doesNotMatch(upload, /same_knowledge_point/)
  assert.match(upload, /q\.correction_knowledge_summary = discovery\?\.knowledge_summary \|\| null/)
  assert.match(upload, /同知识点的不同题目做对不会关闭这些错题/)
})

test('teacher can exclude one current question and persist that decision', () => {
  assert.match(upload, /toggleQuestionCorrection\(q\)/)
  assert.match(upload, /q\.correction_excluded \? '恢复纠错' : '不纠错'/)
  assert.match(upload, /correction_excluded: q\.correction_excluded === true/)
  assert.match(upload, /question\.authorized_wrong_cycle_ids = question\.correction_excluded/)
})

test('template save approves visible corrections without requiring another discovery or confirmation', () => {
  const save = upload.slice(upload.indexOf('async function saveAndConfirmTemplateDraft'), upload.indexOf('async function loadTemplateCorrectionCandidates'))
  assert.doesNotMatch(save, /loadTemplateCorrectionCandidates\(/)
  assert.match(save, /authorized_wrong_cycle_ids: q\.authorized_wrong_cycle_ids \|\| \[\]/)
  assert.match(save, /templateCorrectionCandidatesGeneration \+= 1/)
  assert.doesNotMatch(upload, /纠错候选加载失败，未保存模板/)
})
