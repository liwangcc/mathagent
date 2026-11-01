import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, reactive, computed } from 'vue'

const source = readFileSync(new URL('../src/views/BankPhotoImport.vue', import.meta.url), 'utf8').split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

function setup(chaptersApi) {
  let mounted
  const calls = []
  const api = { bank: { chapters: chaptersApi, photoRecognize: async data => { calls.push(data); return { import_id: 'new', paired: [], teaching_context: { education_stage: 'junior' } } } } }
  const run = new Function('ref', 'reactive', 'computed', 'onMounted', 'useRouter', 'useAuthStore', 'api', 'localStorage', 'ElMessage', 'ElMessageBox', 'validateUploadList', source + '\nreturn { applyRecognition, resetAll, loadChapters, chapters, form, teachingContext, loadedChapterStage, questionFiles, onRecognize };')
  const state = run(ref, reactive, computed, fn => { mounted = fn }, () => ({ push() {} }), () => ({ user: { id: 7, education_stage: 'junior' } }), api, { getItem() {}, setItem() {}, removeItem() {} }, { warning() {}, success() {} }, {}, () => ({}))
  return { state, calls, mounted }
}

test('clearing a restored senior batch clears its chapter and loads current junior catalog', async () => {
  const requests = []
  const { state } = setup(async params => { requests.push(params); return [{ chapter: `${params.education_stage}-chapter` }] })
  state.applyRecognition({ import_id: 'old', teaching_context: { education_stage: 'senior' }, paired: [] })
  state.form.chapter = 'senior-chapter'
  await state.loadChapters()
  await state.resetAll()
  assert.equal(state.form.chapter, '')
  assert.deepEqual(state.teachingContext.value, {})
  assert.equal(state.loadedChapterStage.value, 'junior')
  assert.deepEqual(state.chapters.value, [{ chapter: 'junior-chapter' }])
  assert.deepEqual(requests.map(params => params.education_stage), ['senior', 'junior'])
})

test('late older catalog request cannot replace a fresh reset catalog', async () => {
  const old = deferred(), fresh = deferred()
  const { state } = setup(params => params.education_stage === 'senior' ? old.promise : fresh.promise)
  state.applyRecognition({ import_id: 'old', teaching_context: { education_stage: 'senior' }, paired: [] })
  const previous = state.loadChapters()
  const reset = state.resetAll()
  fresh.resolve([{ chapter: 'junior-chapter' }])
  await reset
  old.resolve([{ chapter: 'senior-chapter' }])
  await previous
  assert.equal(state.loadedChapterStage.value, 'junior')
  assert.deepEqual(state.chapters.value, [{ chapter: 'junior-chapter' }])
})

test('starting new recognition after restoring old stage drops incompatible chapter', async () => {
  const { state, calls } = setup(async params => [{ chapter: `${params.education_stage}-chapter` }])
  state.applyRecognition({ import_id: 'old', teaching_context: { education_stage: 'senior' }, paired: [] })
  await state.loadChapters()
  state.form.chapter = 'senior-chapter'
  state.questionFiles.value = [{ raw: new Blob(['synthetic-only']) }]
  await state.onRecognize()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].get('chapter'), null)
  assert.equal(state.loadedChapterStage.value, 'junior')
})
