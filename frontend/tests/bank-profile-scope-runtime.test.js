import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, reactive, computed, watch } from 'vue'
import { normalizeEducationStage } from '../src/utils/educationStage.js'

const source = readFileSync(new URL('../src/views/Bank.vue', import.meta.url), 'utf8')
  .split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const flush = () => new Promise(resolve => setImmediate(resolve))

function setup(overrides = {}, initialStage = 'junior') {
  const auth = reactive({ token: 'synthetic-token-7', user: { id: 7, education_stage: initialStage } })
  const calls = [], notices = [], downloads = [], stops = []
  let unmount
  const api = { bank: {
    list: async params => { calls.push(['list', params]); return { items: [{ id: params.education_stage }], total: 1 } },
    chapters: async params => { calls.push(['chapters', params]); return [{ chapter: params.education_stage }] },
    exportPrivate: async params => { calls.push(['export', params]); return { ok: true, json: async () => ({ items: [], count: 0 }) } },
    remove: async () => ({}),
    ...overrides,
  } }
  const run = new Function('ref', 'computed', 'watch', 'onBeforeUnmount', 'useRouter', 'useAuthStore', 'api', 'ElMessage', 'normalizeEducationStage', 'URL', 'document', source + '\nreturn { educationStage, chapter, page, rows, chapters, total, loading, exporting, load, loadChapters, onExport };')
  const state = run(ref, computed, (...args) => { stops.push(watch(...args)) }, fn => { unmount = fn }, () => ({ push() {} }), () => auth, api,
    { success: text => notices.push(text), error: text => notices.push(text) }, normalizeEducationStage,
    { createObjectURL: blob => { downloads.push(blob); return 'blob:synthetic' }, revokeObjectURL() {} },
    { createElement: () => ({ click() {} }) })
  return { auth, state, calls, notices, downloads, close() { unmount(); stops.forEach(stop => stop()) } }
}

test('profile changes reset filters and reload matching lists and chapters', async () => {
  const view = setup()
  try {
    await flush()
    assert.equal(view.state.educationStage.value, 'junior')
    view.state.chapter.value = 'junior chapter'
    view.state.page.value = 3
    view.auth.user.education_stage = 'senior'
    assert.deepEqual(view.state.rows.value, [])
    assert.equal(view.state.chapter.value, '')
    assert.equal(view.state.page.value, 1)
    await flush()
    assert.deepEqual(view.state.rows.value, [{ id: 'senior' }])
    assert.deepEqual(view.state.chapters.value, [{ chapter: 'senior' }])
    await view.state.onExport()
    assert.equal(view.calls.at(-1)[1].education_stage, 'senior')
  } finally { view.close() }
})

test('late old-stage reads cannot overwrite fresh profile results', async () => {
  const oldList = deferred(), oldChapters = deferred()
  const view = setup({
    list: params => params.education_stage === 'junior' ? oldList.promise : Promise.resolve({ items: [{ id: 'senior' }], total: 1 }),
    chapters: params => params.education_stage === 'junior' ? oldChapters.promise : Promise.resolve([{ chapter: 'senior' }]),
  })
  try {
    view.auth.user.education_stage = 'senior'
    await flush()
    oldList.resolve({ items: [{ id: 'old junior' }], total: 999 })
    oldChapters.resolve([{ chapter: 'old junior' }])
    await flush()
    assert.deepEqual(view.state.rows.value, [{ id: 'senior' }])
    assert.deepEqual(view.state.chapters.value, [{ chapter: 'senior' }])
    assert.equal(view.state.total.value, 1)
  } finally { view.close() }
})

test('missing profile makes no requests and immediately clears existing results', async () => {
  const view = setup({}, '')
  try {
    await view.state.onExport()
    assert.equal(view.calls.length, 0)
    view.auth.user.education_stage = 'junior'
    await flush()
    view.auth.user.education_stage = ''
    assert.deepEqual(view.state.rows.value, [])
    assert.deepEqual(view.state.chapters.value, [])
    assert.equal(view.state.loading.value, false)
    const count = view.calls.length
    await view.state.onExport()
    assert.equal(view.calls.length, count)
  } finally { view.close() }
})

for (const change of ['stage', 'account', 'unmount']) {
  test(`export never downloads an old scope after ${change}`, async () => {
    const oldBody = deferred()
    const view = setup({ exportPrivate: async () => ({ ok: true, json: () => oldBody.promise }) })
    try {
      const pending = view.state.onExport()
      await flush()
      if (change === 'stage') view.auth.user.education_stage = 'senior'
      else if (change === 'account') view.auth.user = { id: 8, education_stage: 'junior' }
      else view.close()
      oldBody.resolve({ items: [{ question: 'synthetic old scope' }], count: 1 })
      await pending
      assert.equal(view.downloads.length, 0)
      assert.equal(view.notices.length, 0)
    } finally { view.close() }
  })
}

test('late same-stage account response is discarded', async () => {
  const old = deferred()
  let count = 0
  const view = setup({ list: () => ++count === 1 ? old.promise : Promise.resolve({ items: [{ id: 'new-account' }], total: 1 }) })
  try {
    view.auth.user = { id: 8, education_stage: 'junior' }
    await flush()
    old.resolve({ items: [{ id: 'old-account' }], total: 1 })
    await flush()
    assert.deepEqual(view.state.rows.value, [{ id: 'new-account' }])
  } finally { view.close() }
})

test('a failed export shows the server error and never downloads the error body', async () => {
  const view = setup({
    exportPrivate: async () => ({ ok: false, status: 409, json: async () => ({ detail: { message: '题库学段已跟随个人资料更新，请刷新后重试' } }) }),
  })
  try {
    await flush()
    await view.state.onExport()
    assert.equal(view.downloads.length, 0)
    assert.deepEqual(view.notices, ['题库学段已跟随个人资料更新，请刷新后重试'])
  } finally { view.close() }
})
