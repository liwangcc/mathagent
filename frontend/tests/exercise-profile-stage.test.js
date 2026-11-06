import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { mountComponent } from './helpers/mountComponent.js'
import {
  authOf, changeProfile, choose, click, deferred, exerciseServer, failure, options, pick, select, signedInTeacher,
} from './helpers/v1ExerciseBank.js'

// The exercise page follows the teacher's profile for new papers, while a
// paper it already holds keeps the stage it was made for.
const { default: Exercise } = await import('../src/views/Exercise.vue?sfc')

const SAVED_PAPER = 'mathagent_generated_correction_paper'

async function openExercise(t, { user = { id: 7, education_stage: 'senior' }, saved, chapters, students } = {}) {
  const token = `teacher-${user.id}-token`
  const profiles = { [token]: { ...user } }
  const client = await signedInTeacher({
    user,
    token,
    sessionStorage: saved ? { [SAVED_PAPER]: JSON.stringify(saved) } : {},
  })
  const server = exerciseServer(t, client, { profiles, chapters, students })
  // A link carrying a stage of its own: the page must not take it over the profile.
  const page = await mountComponent(t, Exercise, {
    path: `/exercise?mode=${saved ? 'correction' : 'bank'}&education_stage=junior`,
  })
  return { page, root: page.root, profiles, server, auth: authOf(page) }
}

const bankChapters = root => select(root, '不选则从综合题库抽取')
const weakChapters = root => select(root, '不选则使用全部薄弱范围')
const weakTopics = root => select(root, '不选则使用全部错题知识点')
const student = root => select(root, '选择学生')
const extension = root => root.byText('el-checkbox', '包含选修')
const newPaperLabel = root => root.find(el => el.classList.contains('card-subtitle') && el.text.startsWith('新建出题'))?.text
const paperHead = root => root.byClass('result-head')
const paperStage = root => paperHead(root)?.byClass('card-subtitle')?.text
const optionValues = el => el.byTag('el-option').map(option => [option.attributes.label, option._value])

async function exportBoth(root) {
  await click(root, 'el-button', '生成 A3 双栏 Word')
  await click(root, 'el-button', '生成答题卡 Word')
}

test('profile stage changes reset new exercise filters and preserve restored paper exports', async t => {
  const context = { education_stage: 'senior', curriculum_version: 'frozen-catalog', rules_version: 'frozen-rules' }
  const questions = [{ type: 'fill', question: '合成题____', answer: '1', subquestions: ['原元数据'] }]
  const juniorCatalog = deferred()
  const { page, root, profiles, server } = await openExercise(t, {
    saved: { title: '旧高中订正卷', exercises: questions, teaching_context: context },
    chapters: profile => (profile.education_stage === 'junior' ? juniorCatalog.promise : [{ chapter: 'senior-chapter', topics: ['旧知识点'] }]),
  })
  // The teacher narrows a new paper in each mode first.
  await pick(student(root), 91)
  await choose(root, '题库抽题')
  await pick(bankChapters(root), ['旧章节'])
  await pick(extension(root), true)
  await choose(root, '个人薄弱巩固')
  await pick(weakChapters(root), ['旧薄弱章'])
  await pick(weakTopics(root), ['旧知识点'])
  assert.deepEqual(weakTopics(root).props.modelValue, ['旧知识点'])
  assert.equal(student(root).props.modelValue, 91)

  changeProfile(page, profiles, { education_stage: 'junior' })
  await page.flush()
  // Nothing chosen for the senior profile is left while the junior catalog loads.
  assert.deepEqual(weakChapters(root).props.modelValue, [])
  assert.deepEqual(weakTopics(root).props.modelValue, [])
  assert.equal(student(root).props.modelValue, '')
  assert.deepEqual(options(weakChapters(root)), [])
  await choose(root, '题库抽题')
  assert.deepEqual(bankChapters(root).props.modelValue, [])
  assert.equal(extension(root).props.modelValue, false)
  assert.deepEqual(options(bankChapters(root)), [])
  assert.equal(newPaperLabel(root), '新建出题：初中数学 · 自动跟随个人资料')
  assert.equal(paperStage(root), '高中 · 本试卷学段已固定')

  juniorCatalog.resolve([{ chapter: 'junior-chapter' }])
  await page.flush()
  assert.deepEqual(options(bankChapters(root)), ['junior-chapter'])
  assert.deepEqual(server.seen.chapters.map(call => call.profile.education_stage), ['senior', 'junior'])
  assert.ok(server.seen.chapters.every(call => call.params === undefined), 'Catalog requests use server profile, not route query override')
  // The restored paper is untouched.
  assert.equal(paperHead(root).byClass('card-title').text, '旧高中订正卷')
  assert.equal(paperHead(root).byTag('el-tag')[0].text, '1 道题')
  await exportBoth(root)
  assert.deepEqual(server.seen.exports.map(call => call.kind), ['student-and-teacher', 'answer-sheet'])
  for (const { data } of server.seen.exports) {
    assert.deepEqual(data.teaching_context, context)
    assert.deepEqual(data.questions, questions)
    assert.equal(data.title, '旧高中订正卷')
  }
})

test('account changes reload directory and student choices without accepting stale responses', async t => {
  const oldCatalog = deferred(), newCatalog = deferred(), oldStudents = deferred(), newStudents = deferred()
  const { page, root, profiles, server, auth } = await openExercise(t, {
    chapters: user => (user.id === 7 ? oldCatalog.promise : newCatalog.promise),
    students: user => (user.id === 7 ? oldStudents.promise : newStudents.promise),
  })
  await pick(bankChapters(root), ['旧账户章节'])
  await choose(root, '个人薄弱巩固')
  await pick(student(root), 7)
  // Another teacher signs in on the same computer.
  profiles['teacher-8-token'] = { id: 8, education_stage: 'senior' }
  auth.login('teacher-8-token', { id: 8, education_stage: 'senior' })
  await page.flush()
  assert.equal(student(root).props.modelValue, '')
  newCatalog.resolve([{ chapter: '新账户章节' }])
  newStudents.resolve([{ id: 8, name: '新账户学生' }])
  await page.flush()
  oldCatalog.resolve([{ chapter: '旧账户章节' }])
  oldStudents.resolve([{ id: 7, name: '旧账户学生' }])
  await page.flush()
  assert.deepEqual(server.seen.chapters.map(call => call.profile.id), [7, 8])
  assert.deepEqual(server.seen.students.map(call => call.id), [7, 8])
  assert.deepEqual(optionValues(student(root)), [['新账户学生', 8]])
  assert.deepEqual(options(weakChapters(root)), ['新账户章节'])
  await choose(root, '题库抽题')
  assert.deepEqual(bankChapters(root).props.modelValue, [])
  assert.deepEqual(options(bankChapters(root)), ['新账户章节'])
})

test('switching to primary loads its choices and preserves the frozen secondary paper', async t => {
  const context = { education_stage: 'senior', curriculum_version: 'frozen-catalog', rules_version: 'frozen-rules' }
  const questions = [{ type: 'fill', question: '合成历史题____', answer: '1' }]
  const { page, root, profiles, server } = await openExercise(t, {
    saved: { title: '历史高中卷', exercises: questions, teaching_context: context },
  })
  changeProfile(page, profiles, { education_stage: 'primary' })
  await page.flush()
  assert.equal(newPaperLabel(root), '新建出题：小学数学 · 自动跟随个人资料')
  assert.equal(paperStage(root), '高中 · 本试卷学段已固定')
  assert.equal(server.seen.chapters.at(-1).profile.education_stage, 'primary')
  assert.equal(server.seen.students.at(-1).education_stage, 'primary')
  assert.deepEqual(optionValues(student(root)), [['虚拟学生', 7]])
  await choose(root, '题库抽题')
  assert.deepEqual(options(bankChapters(root)), ['primary-chapter'])
  await click(root, 'el-button', '生成 A3 双栏 Word')
  assert.equal(server.seen.exports.length, 1)
  for (const { data } of server.seen.exports) assert.deepEqual(data.teaching_context, context)
})

test('a restored primary paper retains primary labels and export context after profile changes', async t => {
  const context = { education_stage: 'primary', curriculum_version: 'primary-test-catalog', rules_version: 'primary-test-rules' }
  const { page, root, profiles, server } = await openExercise(t, {
    user: { id: 7, education_stage: 'primary' },
    saved: { title: '小学合成卷', exercises: [{ type: 'fill', question: '2+3=____', answer: '5' }], teaching_context: context },
  })
  assert.equal(newPaperLabel(root), '新建出题：小学数学 · 自动跟随个人资料')
  assert.equal(paperStage(root), '小学 · 本试卷学段已固定')
  changeProfile(page, profiles, { education_stage: 'junior' })
  await page.flush()
  assert.equal(newPaperLabel(root), '新建出题：初中数学 · 自动跟随个人资料')
  assert.equal(paperStage(root), '小学 · 本试卷学段已固定')
  await exportBoth(root)
  assert.equal(server.seen.exports.length, 2)
  for (const { data } of server.seen.exports) assert.deepEqual(data.teaching_context, context)
})

test('an older failed catalog request cannot clear the newer profile catalog', async t => {
  const previous = deferred()
  const { page, root, profiles, server } = await openExercise(t, {
    chapters: user => (user.education_stage === 'senior' ? previous.promise : [{ chapter: '初中章' }]),
    saved: { exercises: [{ type: 'fill', question: '历史题', answer: '2' }] },
  })
  changeProfile(page, profiles, { education_stage: 'junior' })
  await page.flush()
  previous.resolve(failure(500, { detail: '旧目录请求失败' }))
  await page.flush()
  await choose(root, '题库抽题')
  assert.deepEqual(options(bankChapters(root)), ['初中章'])
  // The legacy paper restored without a stage stays labelled so and exports no context.
  assert.equal(paperStage(root), '历史未标学段 · 本试卷学段已固定')
  await click(root, 'el-button', '生成 A3 双栏 Word')
  assert.deepEqual(server.seen.exports[0].data.teaching_context, {})
})

test('clearing the account invalidates pending choices without making unauthenticated requests', async t => {
  const catalog = deferred(), studentList = deferred()
  const { page, root, server, auth } = await openExercise(t, {
    chapters: () => catalog.promise,
    students: () => studentList.promise,
  })
  auth.clearSession()
  catalog.resolve([{ chapter: '前账户章节' }])
  studentList.resolve([{ id: 7, name: '前账户学生' }])
  await page.flush()
  assert.deepEqual(options(bankChapters(root)), [])
  await choose(root, '个人薄弱巩固')
  assert.deepEqual(options(student(root)), [])
  assert.deepEqual(options(weakChapters(root)), [])
  assert.equal(server.calls('get /teacher/chapters').length, 1)
  assert.equal(server.calls('get /student/list').length, 1)
  assert.deepEqual(server.keys(), ['get /teacher/chapters', 'get /student/list'])
  assert.equal(newPaperLabel(root), '新建出题：请先完善学段 · 自动跟随个人资料')
})

test('clearing the account removes already loaded students and chapters without new requests', async t => {
  const { page, root, server, auth } = await openExercise(t)
  await choose(root, '个人薄弱巩固')
  // The signed-in teacher's students and directory have arrived.
  assert.deepEqual(optionValues(student(root)), [['虚拟学生', 7]])
  assert.deepEqual(options(weakChapters(root)), ['senior-chapter'])
  const sent = server.keys()
  assert.deepEqual(sent, ['get /teacher/chapters', 'get /student/list'])

  auth.clearSession()
  await page.flush()
  // Nothing from the signed-out account is left to pick.
  assert.deepEqual(options(student(root)), [])
  assert.deepEqual(options(weakChapters(root)), [])
  await choose(root, '题库抽题')
  assert.deepEqual(options(bankChapters(root)), [])
  assert.deepEqual(server.keys(), sent)
  assert.equal(newPaperLabel(root), '新建出题：请先完善学段 · 自动跟随个人资料')
})
