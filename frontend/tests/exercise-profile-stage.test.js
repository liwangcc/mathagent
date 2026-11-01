import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, effectScope, reactive, ref, watch } from 'vue'

const source = readFileSync(new URL('../src/views/Exercise.vue', import.meta.url), 'utf8')
  .split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const settle = () => new Promise(resolve => setImmediate(resolve))

function mount(t, { user = { id: 7, education_stage: 'senior' }, saved, chapters, students } = {}) {
  const auth = reactive({ user, currentClassId: 4, currentClassName: '示例班', token: 'mock-only' })
  const catalogCalls = [], studentCalls = [], exports = []
  let mounted
  const api = {
    bank: { chapters: (...args) => {
      const scope = { ...auth.user }
      catalogCalls.push({ scope, args })
      return chapters ? chapters(scope) : Promise.resolve([{ chapter: `${scope.education_stage}-chapter` }])
    } },
    student: { listAll: () => {
      const scope = { ...auth.user }
      studentCalls.push(scope)
      return students ? students(scope) : Promise.resolve([{ id: scope.id, name: '虚拟学生' }])
    } },
    exercise: {
      exportDocx: async payload => {
        exports.push(payload)
        return { paper_url: '/api/mock-paper', answer_url: '/api/mock-answer' }
      },
      exportAnswerSheetDocx: async payload => {
        exports.push(payload)
        return { download_url: '/api/mock-card' }
      },
    },
  }
  const run = new Function('ref', 'reactive', 'computed', 'watch', 'onMounted', 'onUnmounted', 'onBeforeRouteLeave',
    'useRoute', 'useAuthStore', 'api', 'sessionStorage', 'ElMessage', 'cleanExerciseText', 'setRouteBusy', 'fetch', 'document', source + `
    return { bankForm, weaknessForm, mistakeForm, includeExtension, chapters, students,
      profileStageLabel, stageLabel, paperTeachingContext, resultQuestions, paperTitle,
      loadChapters, loadStudents, onExport, onExportAnswerSheet, buildPayload };
  `)
  const effect = effectScope()
  t.after(() => effect.stop())
  const state = effect.run(() => run(ref, reactive, computed, watch, fn => { mounted = fn }, () => {}, () => {},
    () => ({ query: { mode: saved ? 'correction' : 'bank', education_stage: 'junior' } }), () => auth, api,
    { getItem: () => saved ? JSON.stringify(saved) : null, removeItem() {} },
    { success() {}, warning() {}, error(message) { throw new Error(message) } }, text => String(text || ''),
    () => {},
    async url => {
      assert.ok(url.startsWith('/api/mock-'))
      return { ok: true, blob: async () => new Blob(['synthetic document']) }
    }, { createElement: () => ({ click() {} }) }))
  mounted()
  return { auth, state, catalogCalls, studentCalls, exports }
}

test('profile stage changes reset new exercise filters and preserve restored paper exports', async t => {
  const context = { education_stage: 'senior', curriculum_version: 'frozen-catalog', rules_version: 'frozen-rules' }
  const questions = [{ type: 'fill', question: '合成题____', answer: '1', subquestions: ['原元数据'] }]
  const { auth, state, catalogCalls, exports } = mount(t, {
    saved: { title: '旧高中订正卷', exercises: questions, teaching_context: context },
  })
  await settle()
  state.bankForm.chapters = ['旧章节']
  state.weaknessForm.chapters = ['旧薄弱章']
  state.weaknessForm.topics = ['旧知识点']
  state.mistakeForm.studentId = 91
  state.includeExtension.value = true
  auth.user.education_stage = 'junior'
  assert.deepEqual(state.bankForm.chapters, [])
  assert.deepEqual(state.weaknessForm.chapters, [])
  assert.deepEqual(state.weaknessForm.topics, [])
  assert.equal(state.mistakeForm.studentId, '')
  assert.equal(state.includeExtension.value, false)
  assert.deepEqual(state.chapters.value, [])
  assert.equal(state.profileStageLabel.value, '初中数学')
  assert.equal(state.stageLabel.value, '高中')
  await settle()
  assert.deepEqual(state.chapters.value, [{ chapter: 'junior-chapter' }])
  assert.deepEqual(catalogCalls.map(call => call.scope.education_stage), ['senior', 'junior'])
  assert.ok(catalogCalls.every(call => call.args.length === 0), 'Catalog requests use server profile, not route query override')
  assert.deepEqual(state.paperTeachingContext.value, context)
  assert.deepEqual(state.resultQuestions.value, questions)
  assert.equal(state.paperTitle.value, '旧高中订正卷')
  await state.onExport()
  await state.onExportAnswerSheet()
  assert.equal(exports.length, 2)
  for (const payload of exports) {
    assert.deepEqual(payload.teaching_context, context)
    assert.deepEqual(payload.questions, questions)
  }
})

test('account changes reload directory and student choices without accepting stale responses', async t => {
  const oldCatalog = deferred(), newCatalog = deferred(), oldStudents = deferred(), newStudents = deferred()
  const { auth, state, catalogCalls } = mount(t, {
    chapters: user => user.id === 7 ? oldCatalog.promise : newCatalog.promise,
    students: user => user.id === 7 ? oldStudents.promise : newStudents.promise,
  })
  state.bankForm.chapters = ['旧账户章节']
  state.mistakeForm.studentId = 7
  auth.user = { id: 8, education_stage: 'senior' }
  assert.deepEqual(state.bankForm.chapters, [])
  assert.equal(state.mistakeForm.studentId, '')
  newCatalog.resolve([{ chapter: '新账户章节' }])
  newStudents.resolve([{ id: 8, name: '新账户学生' }])
  await settle()
  oldCatalog.resolve([{ chapter: '旧账户章节' }])
  oldStudents.resolve([{ id: 7, name: '旧账户学生' }])
  await settle()
  assert.deepEqual(catalogCalls.map(call => call.scope.id), [7, 8])
  assert.deepEqual(state.chapters.value, [{ chapter: '新账户章节' }])
  assert.deepEqual(state.students.value, [{ id: 8, name: '新账户学生' }])
})

test('switching to primary loads its choices and preserves the frozen secondary paper', async t => {
  const context = { education_stage: 'senior', curriculum_version: 'frozen-catalog', rules_version: 'frozen-rules' }
  const questions = [{ type: 'fill', question: '合成历史题____', answer: '1' }]
  const { auth, state, catalogCalls, studentCalls, exports } = mount(t, {
    saved: { title: '历史高中卷', exercises: questions, teaching_context: context },
  })
  await settle()
  auth.user.education_stage = 'primary'
  await settle()
  assert.equal(state.profileStageLabel.value, '小学数学')
  assert.equal(state.stageLabel.value, '高中')
  assert.deepEqual(state.chapters.value, [{ chapter: 'primary-chapter' }])
  assert.equal(catalogCalls.at(-1).scope.education_stage, 'primary')
  assert.equal(studentCalls.at(-1).education_stage, 'primary')
  assert.equal(state.students.value.length, 1)
  await state.onExport()
  for (const payload of exports) assert.deepEqual(payload.teaching_context, context)
})

test('a restored primary paper retains primary labels and export context after profile changes', async t => {
  const context = { education_stage: 'primary', curriculum_version: 'primary-test-catalog', rules_version: 'primary-test-rules' }
  const { auth, state, exports } = mount(t, {
    user: { id: 7, education_stage: 'primary' },
    saved: { title: '小学合成卷', exercises: [{ type: 'fill', question: '2+3=____', answer: '5' }], teaching_context: context },
  })
  await settle()
  assert.equal(state.profileStageLabel.value, '小学数学')
  assert.equal(state.stageLabel.value, '小学')
  auth.user.education_stage = 'junior'
  await settle()
  assert.equal(state.profileStageLabel.value, '初中数学')
  assert.equal(state.stageLabel.value, '小学')
  await state.onExport()
  await state.onExportAnswerSheet()
  assert.equal(exports.length, 2)
  for (const payload of exports) assert.deepEqual(payload.teaching_context, context)
})

test('an older failed catalog request cannot clear the newer profile catalog', async t => {
  const previous = deferred()
  const { auth, state } = mount(t, {
    chapters: user => user.education_stage === 'senior' ? previous.promise : Promise.resolve([{ chapter: '初中章' }]),
    saved: { exercises: [{ type: 'fill', question: '历史题', answer: '2' }] },
  })
  auth.user.education_stage = 'junior'
  await settle()
  previous.reject(new Error('old request failed'))
  await settle()
  assert.deepEqual(state.chapters.value, [{ chapter: '初中章' }])
  assert.deepEqual(state.paperTeachingContext.value, {})
  assert.equal(state.stageLabel.value, '历史未标学段')
})

test('clearing the account invalidates pending choices without making unauthenticated requests', async t => {
  const catalog = deferred(), studentList = deferred()
  const { auth, state, catalogCalls, studentCalls } = mount(t, {
    chapters: () => catalog.promise,
    students: () => studentList.promise,
  })
  auth.user = null
  catalog.resolve([{ chapter: '前账户章节' }])
  studentList.resolve([{ id: 7, name: '前账户学生' }])
  await settle()
  assert.deepEqual(state.chapters.value, [])
  assert.deepEqual(state.students.value, [])
  assert.equal(catalogCalls.length, 1)
  assert.equal(studentCalls.length, 1)
  assert.equal(state.profileStageLabel.value, '请先完善学段')
})
