import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/srcModules.js'
import { mountComponent } from './helpers/mountComponent.js'
import { body, click, pageStandIns, pick, serve, signedInTeacher } from './helpers/v1ExerciseBank.js'
import {
  CLASSIFICATION_LABELS, catalogOptions, classificationState, hasKnowledge,
} from '../src/utils/knowledgeClassification.js'

const { default: Bank } = await import('../src/views/Bank.vue?sfc')
const { default: PendingAnswers } = await import('../src/views/PendingAnswers.vue?sfc')

const CATALOG = [{ chapter: '函数', topics: ['定义域', '值域'] }]
// The two-level chapter → topic picker the catalog becomes.
const PICKER = [{
  value: '函数', label: '函数',
  children: [{ value: '定义域', label: '定义域' }, { value: '值域', label: '值域' }],
}]
const column = (root, label) => root.find(el => el.tagName === 'el-table-column' && el.attributes.label === label)

test('placeholder labels are not knowledge points', () => {
  assert.equal(hasKnowledge({ chapter: '函数', topic: '定义域' }), true)
  for (const topic of ['', '未分类', '未知', '综合']) {
    assert.equal(hasKnowledge({ chapter: '函数', topic }), false)
  }
})

test('missing labels read as in progress or failed, never 未分类', () => {
  assert.equal(classificationState({ chapter: '函数', topic: '定义域', classification_status: 'manual' }), 'classified')
  assert.equal(classificationState({ classification_status: 'pending' }), 'pending')
  assert.equal(classificationState({ classification_status: '' }), 'pending')
  for (const status of ['manual', 'failed', 'confirmed', 'suggested']) {
    assert.equal(classificationState({ topic: '未分类', classification_status: status }), 'failed')
  }
  assert.equal(CLASSIFICATION_LABELS.failed, '分类失败')
  assert.doesNotMatch(JSON.stringify(CLASSIFICATION_LABELS), /未分类/)
})

test('catalog becomes a two-level chapter/topic picker', () => {
  assert.deepEqual(catalogOptions([{ chapter: '函数', topics: ['定义域', '值域'] }, { chapter: '' }]), [{
    value: '函数', label: '函数',
    children: [{ value: '定义域', label: '定义域' }, { value: '值域', label: '值域' }],
  }])
  assert.deepEqual(catalogOptions(null), [])
})

test('bank and pending pages offer a manual pick when classification is missing', async t => {
  const client = await signedInTeacher({ user: { id: 7, education_stage: 'junior' } })
  const bankRows = [
    { id: 1, teacher_id: 7, chapter: '函数', topic: '未分类', classification_status: 'failed', classification_error: '模型超时', source: 'r1' },
    { id: 2, teacher_id: 7, classification_status: 'pending', source: 'r2' },
    { id: 3, teacher_id: 7, chapter: '函数', topic: '定义域', classification_status: 'manual', source: 'r3' },
  ]
  const backend = serve(client, {
    'get /teacher/bank': () => ({ items: bankRows, total: bankRows.length }),
    'get /teacher/chapters': () => CATALOG,
    'put /teacher/bank/1': () => ({}),
  })
  const bank = await mountComponent(t, Bank, { path: '/bank', plugins: [pageStandIns] })
  const knowledge = column(bank.root, '知识点')
  const tags = knowledge.byTag('el-cell').map(cell => cell.byTag('el-tag')[0])
  assert.deepEqual(tags.map(tag => [tag.text, tag.attributes.type]), [['分类失败', 'danger'], ['分类中', 'info'], ['定义域', undefined]])
  assert.equal(tags[0].attributes.title, '模型超时')
  assert.doesNotMatch(knowledge.text, /未分类/)
  const pickers = knowledge.byTag('el-cascader')
  assert.equal(pickers.length, 2, 'only rows missing a knowledge point offer the picker')
  assert.deepEqual(pickers[0].props.options, PICKER)
  await pick(pickers[0], ['函数', '值域'])
  assert.deepEqual(backend.calls('put /teacher/bank/1').map(body), [{ chapter: '函数', topic: '值域' }])
  assert.deepEqual(client.messages, [{ type: 'success', message: '已设置知识点' }])
  assert.equal(backend.calls('get /teacher/bank').length, 2, 'the list reloads with the saved label')
  bank.unmount()

  const pendingRows = [
    { question: 'q1', question_preview: '题一', ai_answer: '1', grading_id: 11, detail_index: 0, teaching_context: { education_stage: 'junior' }, classification_status: 'failed' },
    { question: 'q2', question_preview: '题二', ai_answer: '2', grading_id: 12, detail_index: 1, teaching_context: { education_stage: 'junior' }, classification_status: 'pending' },
  ]
  const teacher = await signedInTeacher({ user: { id: 7, education_stage: 'junior' } })
  const server = serve(teacher, {
    'get /bank/pending-answers': () => ({ items: pendingRows, total: pendingRows.length }),
    'get /teacher/chapters': () => CATALOG,
    'post /bank/confirm-answers': () => ({}),
  })
  const pending = await mountComponent(t, PendingAnswers, { path: '/pending-answers' })
  const cells = () => column(pending.root, '知识点').byTag('el-cell')
  assert.deepEqual(cells().map(cell => cell.byTag('el-tag')[0].text), [CLASSIFICATION_LABELS.failed, CLASSIFICATION_LABELS.pending])
  assert.deepEqual(cells()[0].byTag('el-cascader')[0].props.options, PICKER)
  await pick(cells()[0].byTag('el-cascader')[0], ['函数', '值域'])
  assert.equal(cells()[0].byTag('el-tag')[0].text, '手动选择')
  await column(pending.root, '操作').byTag('el-button').find(el => el.text === '确认入库').dispatch('click')
  await pending.flush()
  const [one] = server.calls('post /bank/confirm-answers').map(body)
  assert.deepEqual([one.answers[0].question, one.answers[0].chapter, one.answers[0].topic], ['q1', '函数', '值域'])

  pending.root.byTag('el-table')[0].handlers.selectionChange([...pending.setup.rows])
  await pending.flush()
  await click(pending.root, 'el-button', '批量入库 (2)')
  const batch = server.calls('post /bank/confirm-answers').map(body)[1]
  assert.deepEqual(batch.answers.map(answer => [answer.question, answer.chapter, answer.topic]), [['q1', '函数', '值域'], ['q2', '', '']])
})
