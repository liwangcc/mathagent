import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, reactive, computed, watch } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

const source = readFileSync(new URL('../src/views/Exercise.vue', import.meta.url), 'utf8').split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')

function restore(raw, query = { mode: 'correction', student_id: '12' }) {
  let mounted
  const captures = [], warnings = [], removed = [], downloads = []
  const auth = { user: { id: 7, education_stage: 'senior' } }
  const api = {
    bank: { chapters: async () => [] }, student: { listAll: async () => [] },
    file: { downloadUrl: async () => {} },
    exercise: {
      exportDocx: async data => { captures.push({ kind: 'student-and-teacher', data }); return { paper_url: '/api/mock-paper', answer_url: '/api/mock-teacher' } },
      exportAnswerSheetDocx: async data => { captures.push({ kind: 'answer-sheet', data }); return { download_url: '/api/mock-card' } },
    },
  }
  const storage = { getItem: () => raw, removeItem: key => removed.push(key) }
  const setup = new Function('ref', 'reactive', 'computed', 'watch', 'onMounted', 'onUnmounted', 'onBeforeRouteLeave', 'useRoute', 'useAuthStore', 'api', 'sessionStorage', 'ElMessage', 'cleanExerciseText', 'setRouteBusy', 'fetch', 'document', source + '\nreturn { mode, mistakeForm, resultQuestions, paperTeachingContext, stageLabel, onExport, onExportAnswerSheet, downloadFromUrl, paperUrl, answerUrl };')
  const fetchMock = async url => { assert.ok(url.startsWith('/api/mock-')); downloads.push(url); return { ok: true, blob: async () => new Blob(['mock document']) } }
  const state = setup(ref, reactive, computed, watch, fn => { mounted = fn }, () => {}, () => {}, () => ({ query }), () => auth, api, storage, { success() {}, error(message) { throw new Error(message) }, warning: message => warnings.push(message) }, text => String(text || ''), () => {}, fetchMock, { createElement: () => ({ click() {} }) })
  mounted()
  return { state, captures, removed, warnings, auth, downloads }
}

test('history-router query restores correction questions and frozen stage into all three exports', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/exercise', component: {} }] })
  await router.push('/exercise?mode=correction&student_id=12')
  const context = { education_stage: 'junior', curriculum_version: 'frozen-catalog', rules_version: 'frozen-rules' }
  const questions = [{ id: '12', type: 'fill', question: '求值：____', answer: '3', subquestions: ['保留题型元数据'] }]
  const { state, captures, auth, downloads } = restore(JSON.stringify({ title: '初中订正卷', exercises: questions, teaching_context: context }), router.currentRoute.value.query)
  assert.equal(state.mode.value, 'correction')
  assert.equal(state.mistakeForm.studentId, 12)
  assert.deepEqual(state.resultQuestions.value, questions)
  assert.deepEqual(state.paperTeachingContext.value, context)
  assert.equal(state.stageLabel.value, '初中')
  auth.user.education_stage = 'senior'
  await state.onExport()
  await state.downloadFromUrl(state.answerUrl.value, 'teacher.docx')
  await state.onExportAnswerSheet()
  assert.equal(state.paperUrl.value, '/api/mock-paper')
  assert.equal(state.answerUrl.value, '/api/mock-teacher')
  assert.deepEqual(downloads, ['/api/mock-paper', '/api/mock-teacher', '/api/mock-card'])
  assert.deepEqual(captures.map(call => call.kind), ['student-and-teacher', 'answer-sheet'])
  for (const call of captures) {
    assert.deepEqual(call.data.teaching_context, context)
    assert.deepEqual(call.data.questions, questions)
  }
})

test('legacy restoration remains empty-context and malformed cache cannot abort page setup', async () => {
  const legacy = restore(JSON.stringify({ exercises: [{ type: 'choice', question: '旧题', answer: 'A' }] }))
  await legacy.state.onExport()
  assert.deepEqual(legacy.captures[0].data.teaching_context, {})
  assert.equal(legacy.state.stageLabel.value, '历史未标学段')
  const broken = restore('{broken-json')
  assert.equal(broken.state.resultQuestions.value.length, 0)
  assert.equal(broken.warnings.length, 1)
  assert.deepEqual(broken.removed, ['mathagent_generated_correction_paper'])
})
