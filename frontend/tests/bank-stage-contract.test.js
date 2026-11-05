import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { loadApiClient } from './helpers/apiClient.js'

const view = readFileSync(new URL('../src/views/Bank.vue', import.meta.url), 'utf8')
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

test('bank query and export forward optional stage params', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  client.respond(() => ({ data: { chapters: ['c1'] } }))
  assert.deepEqual(await client.api.bank.chapters({ education_stage: 'junior' }), { chapters: ['c1'] })
  assert.deepEqual(await client.api.bank.chapters(), { chapters: ['c1'] })
  const sent = client.requests.map(({ method, url, params, headers }) => ({
    method, url, params, authorization: headers.Authorization,
  }))
  assert.deepEqual(sent, [
    { method: 'get', url: '/teacher/chapters', params: { education_stage: 'junior' }, authorization: 'Bearer teacher-token' },
    { method: 'get', url: '/teacher/chapters', params: undefined, authorization: 'Bearer teacher-token' },
  ])

  const fetched = []
  const realFetch = globalThis.fetch
  globalThis.fetch = async (url, options = {}) => {
    fetched.push({ url, method: options.method ?? 'GET', authorization: options.headers?.Authorization })
    return new Response(`xlsx ${fetched.length}`)
  }
  const exported = []
  try {
    exported.push(await client.api.bank.exportPrivate({ education_stage: 'senior' }))
    exported.push(await client.api.bank.exportPrivate())
  } finally {
    globalThis.fetch = realFetch
  }
  assert.deepEqual(fetched, [
    { url: '/api/bank/export?education_stage=senior', method: 'GET', authorization: 'Bearer teacher-token' },
    { url: '/api/bank/export?', method: 'GET', authorization: 'Bearer teacher-token' },
  ])
  assert.deepEqual(await Promise.all(exported.map(resp => resp.text())), ['xlsx 1', 'xlsx 2'])
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
