import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { mountComponent } from './helpers/mountComponent.js'
import {
  authOf, changeProfile, choose, click, exerciseServer, formItem, labelled, pick, select, signedInTeacher,
} from './helpers/v1ExerciseBank.js'

// What the exercise page asks the server for in each mode, its size limits,
// and which teaching context its exports carry.
const { default: Exercise } = await import('../src/views/Exercise.vue?sfc')

const PAPER = [{ type: 'fill', question: '合成题____', answer: '1' }]

async function openExercise(t, { user = { id: 7, education_stage: 'junior' }, generate } = {}) {
  const profiles = { [`teacher-${user.id}-token`]: { ...user } }
  const client = await signedInTeacher({ user })
  const server = exerciseServer(t, client, {
    profiles,
    chapters: () => [{ chapter: '函数', topics: ['定义域', '值域'] }],
    students: () => [{ id: 3, name: '学生甲' }],
    generate: generate ?? (() => ({ title: '练习卷', exercises: PAPER })),
  })
  const page = await mountComponent(t, Exercise, { path: '/exercise' })
  return { page, root: page.root, client, server, profiles }
}

const typeCount = (root, card) => root.byClass(card).byTag('el-input-number')[0]
const paperStage = root => root.byClass('result-head')?.byClass('card-subtitle')?.text
const newPaperLabel = root => root.find(el => el.classList.contains('card-subtitle') && el.text.startsWith('新建出题'))?.text

test('exercise page separates weakness practice from original correction papers', async t => {
  const { page, root, server } = await openExercise(t)
  // The class the top navigation switched to, as auth.fetchClasses() leaves it.
  const auth = authOf(page)
  auth.classes = [{ id: 4, class_name: '示例班' }]
  auth.currentClassId = 4
  await page.flush()
  assert.deepEqual(labelled(root, 'el-radio-button', '题库抽题').parentNode.byTag('el-radio-button').map(el => el.text),
    ['题库抽题', '个人薄弱巩固', '原题订正卷', '班级共性强化'])
  const typeCounts = { choice: 4, fill: 4, solution: 2 }

  await choose(root, '个人薄弱巩固')
  await pick(select(root, '选择学生'), 3)
  await pick(select(root, '不选则使用全部薄弱范围'), ['函数'])
  await pick(select(root, '不选则使用全部错题知识点'), ['定义域'])
  await click(root, 'el-button', '按题型从题库抽题')

  await choose(root, '班级共性强化')
  assert.equal(formItem(root, '当前班级').byTag('span')[0].text, '示例班')
  await click(root, 'el-button', '按题型从题库抽题')

  await choose(root, '原题订正卷')
  assert.deepEqual(formItem(root, '订正范围').byTag('el-radio-button').map(el => el.text), ['个人', '班级统一卷'])
  await pick(formItem(root, '抽取数量').byTag('el-input-number')[0], 5)
  await click(root, 'el-button', '抽取 5 道原题')

  await choose(root, '班级统一卷')
  assert.equal(select(root, '选择学生'), null)
  await click(root, 'el-button', '抽取 5 道原题')

  assert.deepEqual(server.seen.generate, [
    { mode: 'personal', include_extension: false, question_count: 10, type_counts: typeCounts, student_id: 3, chapters: ['函数'], topics: ['定义域'] },
    { mode: 'class', include_extension: false, question_count: 10, type_counts: typeCounts, class_id: 4, class_name: '示例班', chapters: ['函数'], topics: ['定义域'] },
    { mode: 'correction', include_extension: false, question_count: 5, correction_scope: 'personal', student_id: 3 },
    { mode: 'correction', include_extension: false, question_count: 5, correction_scope: 'class', class_id: 4, class_name: '示例班' },
  ])
})

test('exercise generation is capped at 22 questions for the answer-sheet layout', async t => {
  const { root, client, server } = await openExercise(t)
  const solution = typeCount(root, 'solution-card')
  assert.deepEqual([solution.attributes.min, solution.attributes.max], ['0', '6'])
  for (const card of ['choice-card', 'fill-card']) {
    assert.deepEqual([typeCount(root, card).attributes.min, typeCount(root, card).attributes.max], ['0', '20'])
  }
  assert.equal(root.byClass('count-summary').byTag('small')[0].text, '选择、填空各最多 20 道，解答题最多 6 道，总数最多 22 道')

  await pick(typeCount(root, 'choice-card'), 20)
  await pick(typeCount(root, 'fill-card'), 1)
  assert.equal(root.byClass('count-summary').byTag('span')[0].text, '共 23 道')
  await click(root, 'el-button', '按题型从题库抽题')
  assert.deepEqual(server.seen.generate, [])
  assert.deepEqual(client.messages, [{ type: 'warning', message: '单次出题最多 22 道，请调整题型数量' }])

  await pick(typeCount(root, 'fill-card'), 0)
  await click(root, 'el-button', '按题型从题库抽题')
  assert.equal(server.seen.generate.length, 1)
  assert.equal(server.seen.generate[0].question_count, 22)
  assert.deepEqual(server.seen.generate[0].type_counts, { choice: 20, fill: 0, solution: 2 })

  await choose(root, '原题订正卷')
  const count = formItem(root, '抽取数量').byTag('el-input-number')[0]
  assert.deepEqual([count.attributes.min, count.attributes.max], ['1', '22'])
})

test('exercise exports retain the generated teaching snapshot rather than the current profile', async t => {
  const snapshot = { education_stage: 'junior', curriculum_version: 'junior-catalog-v3', rules_version: 'rules-v2' }
  let reply = { title: '初中练习卷', exercises: PAPER, teaching_context: snapshot }
  const { page, root, server, profiles } = await openExercise(t, { generate: () => reply })
  assert.equal(newPaperLabel(root), '新建出题：初中数学 · 自动跟随个人资料')
  await click(root, 'el-button', '按题型从题库抽题')
  assert.equal(paperStage(root), '初中 · 本试卷学段已固定')

  changeProfile(page, profiles, { education_stage: 'senior' })
  await page.flush()
  assert.equal(newPaperLabel(root), '新建出题：高中数学 · 自动跟随个人资料')
  assert.equal(paperStage(root), '初中 · 本试卷学段已固定')
  await click(root, 'el-button', '生成 A3 双栏 Word')
  await click(root, 'el-button', '生成答题卡 Word')
  assert.deepEqual(server.seen.exports.map(call => call.kind), ['student-and-teacher', 'answer-sheet'])
  for (const call of server.seen.exports) assert.deepEqual(call.data.teaching_context, snapshot)

  // The next paper carries only its own snapshot: none, when the server sent none.
  reply = { title: '无学段练习卷', exercises: PAPER }
  await click(root, 'el-button', '按题型从题库抽题')
  assert.equal(paperStage(root), '历史未标学段 · 本试卷学段已固定')
  await click(root, 'el-button', '生成 A3 双栏 Word')
  assert.deepEqual(server.seen.exports.at(-1).data.teaching_context, {})
})
