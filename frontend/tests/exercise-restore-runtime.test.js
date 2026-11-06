import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { mountComponent } from './helpers/mountComponent.js'
import { changeProfile, click, exerciseServer, labelled, select, signedInTeacher } from './helpers/v1ExerciseBank.js'

// A correction paper generated elsewhere (the student detail page) is handed
// to the exercise page through sessionStorage and a history-router link.
const { default: Exercise } = await import('../src/views/Exercise.vue?sfc')

const SAVED_PAPER = 'mathagent_generated_correction_paper'

async function restore(t, raw, path = '/exercise?mode=correction&student_id=12') {
  const user = { id: 7, education_stage: 'senior' }
  const profiles = { 'teacher-7-token': { ...user } }
  const client = await signedInTeacher({ user, sessionStorage: { [SAVED_PAPER]: raw } })
  const server = exerciseServer(t, client, { profiles })
  const page = await mountComponent(t, Exercise, { path })
  return { page, root: page.root, client, server, profiles }
}

const paperStage = root => root.byClass('result-head')?.byClass('card-subtitle')?.text

test('history-router query restores correction questions and frozen stage into all three exports', async t => {
  const context = { education_stage: 'junior', curriculum_version: 'frozen-catalog', rules_version: 'frozen-rules' }
  const questions = [{ id: '12', type: 'fill', question: '求值：____', answer: '3', subquestions: ['保留题型元数据'] }]
  const { page, root, client, server, profiles } = await restore(t, JSON.stringify({ title: '初中订正卷', exercises: questions, teaching_context: context }))
  assert.equal(labelled(root, 'el-radio-button', '原题订正卷').parentNode.props.modelValue, 'correction')
  assert.equal(select(root, '选择学生').props.modelValue, 12)
  assert.equal(root.byClass('result-head').byClass('card-title').text, '初中订正卷')
  assert.equal(paperStage(root), '初中 · 本试卷学段已固定')
  assert.equal(client.sessionStorage.getItem(SAVED_PAPER), null, 'the hand-over is read once')

  changeProfile(page, profiles, { education_stage: 'primary' })
  await page.flush()
  await click(root, 'el-button', '生成 A3 双栏 Word')
  await click(root, 'el-button', '下载答案解析版')
  await click(root, 'el-button', '生成答题卡 Word')
  assert.deepEqual(server.downloads(), ['/api/mock-paper', '/api/mock-teacher', '/api/mock-card'])
  // The student copy stays downloadable from the address the export returned.
  await click(root, 'el-button', '下载学生试卷版')
  assert.equal(server.downloads().at(-1), '/api/mock-paper')
  assert.ok(server.files.requests.every(request => request.init.headers.Authorization === 'Bearer teacher-7-token'))
  assert.deepEqual(server.seen.exports.map(call => call.kind), ['student-and-teacher', 'answer-sheet'])
  for (const call of server.seen.exports) {
    assert.deepEqual(call.data.teaching_context, context)
    assert.deepEqual(call.data.questions, questions)
  }
  assert.equal(paperStage(root), '初中 · 本试卷学段已固定')
})

test('legacy restoration remains empty-context and malformed cache cannot abort page setup', async t => {
  const legacy = await restore(t, JSON.stringify({ exercises: [{ type: 'choice', question: '旧题', answer: 'A' }] }))
  await click(legacy.root, 'el-button', '生成 A3 双栏 Word')
  assert.deepEqual(legacy.server.seen.exports[0].data.teaching_context, {})
  assert.equal(paperStage(legacy.root), '历史未标学段 · 本试卷学段已固定')
  legacy.page.unmount()
  legacy.server.files.restore()

  const broken = await restore(t, '{broken-json')
  assert.equal(broken.root.byClass('result-section'), null)
  assert.deepEqual(broken.client.messages.filter(message => message.type === 'warning'), [
    { type: 'warning', message: '暂存的订正卷无法恢复，请重新生成' },
  ])
  assert.equal(broken.client.sessionStorage.getItem(SAVED_PAPER), null)
  // Setup went on past the broken hand-over: the page loads its choices and stays usable.
  assert.deepEqual(broken.server.keys(), ['get /teacher/chapters', 'get /student/list'])
  assert.equal(labelled(broken.root, 'el-radio-button', '原题订正卷').parentNode.props.modelValue, 'correction')
  assert.equal(select(broken.root, '选择学生').props.modelValue, 12)
})
