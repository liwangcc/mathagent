import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const view = readFileSync(new URL('../src/views/Bank.vue', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8')
const photo = readFileSync(new URL('../src/views/BankPhotoImport.vue', import.meta.url), 'utf8')
const pending = readFileSync(new URL('../src/views/PendingAnswers.vue', import.meta.url), 'utf8')

test('bank follows profile without independent stage or legacy-adoption controls', () => {
  assert.doesNotMatch(view, /v-model="educationStage"|adoptionStage|adoptLegacy|历史未标学段/)
  assert.match(view, /computed\(\(\) => normalizeEducationStage\(auth.user\?\.education_stage\)\)/)
  assert.match(view, /education_stage: scope.stage/)
  assert.match(view, /exportPrivate\(\{ education_stage: scope.stage \}\)/)
  assert.match(view, /chapters\(\{ education_stage: scope.stage \}\)/)
  assert.match(view, /无需重复选择/)
  assert.doesNotMatch(view, /updateProfile|education_stage\s*=(?!=)/)
})

test('bank query and export forward optional stage params', () => {
  assert.match(api, /chapters: \(params\) => http.get\('\/teacher\/chapters', \{ params \}\)/)
  assert.match(api, /exportPrivate: \(params = \{\}\) => authFetch\(`\/api\/bank\/export\?\$\{new URLSearchParams\(params\)\}`\)/)
})

test('photo import returns server batch id on save and restores only that opaque id', () => {
  assert.match(photo, /photoSave\(\{ items, import_id: importId.value \}\)/)
  assert.match(photo, /photoRestore\(savedImport\)/)
  assert.match(photo, /localStorage.setItem\(savedImportKey\(\), importId.value\)/)
  assert.doesNotMatch(photo, /photoSave\([^\n]*teaching_context/)
  assert.match(photo, /题目仅进入你的私有题库/)
})

test('pending answer confirmation sends teacher-edited answer with its persisted source', () => {
  assert.match(pending, /answer: row.ai_answer/)
  assert.match(pending, /answer: r.ai_answer/)
  assert.match(pending, /grading_id: row.grading_id, detail_index: row.detail_index/)
  assert.match(pending, /历史未标学段/)
})
