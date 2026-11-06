import assert from 'node:assert/strict'
import test from 'node:test'
import { brief, mountTemplateEditor, TEMPLATE_DISCOVERY } from './helpers/templateReviewFlow.js'

// Template correction on the real Upload page: when the template editor opens,
// the page asks the server which of the class's historical wrong questions
// this template's questions can correct, shows them under each question, and
// saving the template approves the visible relations.

const PAGES = [{
  page_index: 0,
  questions: [
    { question_no: '1', question_type: 'fill', blank_count: 1, standard_answer: '7', question_text: '本次题干：解方程 x + 3 = 10' },
    { question_no: '2', question_type: 'fill', blank_count: 1, standard_answer: '9', question_text: '本次题干：计算 3 × 3' },
  ],
}]

const HISTORY = [
  { cycle_id: 'c1', student_id: 11, student_name: '甲', student_no: '01', relation: 'same_original', knowledge_point: '一元一次方程', historical_question: '历史题干：解方程 x + 2 = 5' },
  { cycle_id: 'c2', student_id: 12, relation: 'similar', historical_question: '历史题干：解方程 2x = 8' },
]
const FOUND = { candidates: [{ question_no: '1', candidates: HISTORY }] }

const panel = question => question.byClass('correction-review-panel')
const decision = question => panel(question).byClass('correction-decision-btn')
const saved = (payload, no) => payload.pages[0].questions.find(q => q.question_no === no)
const relations = (payload, no) => {
  const q = saved(payload, no)
  return { excluded: q.correction_excluded, ids: q.authorized_wrong_cycle_ids }
}

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

test('template correction shows current and historical questions and saves discoveries by default', async (t) => {
  const editor = await mountTemplateEditor(t, { pages: PAGES, discovery: FOUND })
  // The editor asks about exactly this template's questions.
  assert.equal(editor.discoveries().length, 1)
  const [asked] = editor.discoveries()
  assert.equal(asked.template_id, editor.templateId)
  assert.deepEqual(asked.pages[0].questions.map(q => [q.question_no, q.question_text]), PAGES[0].questions.map(q => [q.question_no, q.question_text]))

  const question = editor.question('1')
  assert.equal(question.byClass('correction-discovery-badge').text, '可纠错 2 项')
  // A question with discoveries opens its details by itself.
  assert.notEqual(question.byClass('tpl-q-details').style.display, 'none')
  const review = panel(question)
  assert.match(review.text, /发现可纠错题目/)
  assert.match(review.text, /保存模板即同意建立下列关系/)
  assert.equal(review.byClass('correction-current-question').text, '本次题目本次题干：解方程 x + 3 = 10')
  const rows = review.allByClass('correction-history-row')
  assert.equal(rows.length, 2)
  assert.match(rows[0].text, /^甲\s*学号 01\s*同一原题\s*一元一次方程/)
  assert.match(rows[0].text, /历史错题历史题干：解方程 x \+ 2 = 5$/)
  assert.match(rows[1].text, /^学生 #12\s*相似题\s*知识点未标注/)
  assert.match(rows[1].text, /历史错题历史题干：解方程 2x = 8$/)
  assert.equal(decision(question).text, '不纠错')
  assert.equal(brief(panel(editor.question('2'))), null)

  // Saving without touching anything approves every visible relation.
  await editor.save()
  assert.equal(editor.confirmed().length, 1)
  assert.deepEqual(relations(editor.confirmed()[0], '1'), { excluded: false, ids: ['c1', 'c2'] })
  assert.deepEqual(relations(editor.confirmed()[0], '2'), { excluded: false, ids: [] })
})

test('same knowledge point is a summary, never a record the template corrects', async (t) => {
  const editor = await mountTemplateEditor(t, {
    pages: PAGES,
    discovery: {
      candidates: [{
        question_no: '2',
        candidates: [],
        knowledge_summary: { topic: '有理数乘法', student_count: 3, record_count: 5 },
        // Other wrong questions on the same knowledge point are not corrections.
        same_knowledge_point: [{ cycle_id: 'k1', student_id: 21, historical_question: '同知识点的另一道题' }],
      }],
    },
  })
  const question = editor.question('2')
  assert.equal(brief(panel(question)), null, 'no correction relation is offered')
  assert.equal(brief(question.byClass('correction-discovery-badge')), null)
  await question.byText('button', '查看详情').dispatch('click')
  await editor.settle()
  assert.notEqual(question.byClass('tpl-q-details').style.display, 'none')
  assert.equal(
    question.byClass('correction-knowledge-note').text,
    '同知识点「有理数乘法」：本班 3 名学生仍有 5 道错题未纠正。同知识点的不同题目做对不会关闭这些错题。',
  )
  assert.doesNotMatch(question.text, /历史错题|同知识点的另一道题/)

  await editor.save()
  const [payload] = editor.confirmed()
  assert.deepEqual(relations(payload, '2'), { excluded: false, ids: [] })
  assert.doesNotMatch(JSON.stringify(payload), /k1|same_knowledge_point/)
})

test('teacher can exclude one current question and persist that decision', async (t) => {
  const editor = await mountTemplateEditor(t, { pages: PAGES, discovery: FOUND })
  const question = () => editor.question('1')
  await decision(question()).dispatch('click')
  await editor.settle()
  assert.equal(decision(question()).text, '恢复纠错')
  assert.ok(panel(question()).classList.contains('is-excluded'))
  assert.equal(panel(question()).byClass('correction-excluded-note').text, '此题及上列历史错题已从本模板纠错关系中排除。')
  assert.equal(question().byClass('correction-discovery-badge').text, '已排除纠错')

  await editor.save()
  assert.deepEqual(relations(editor.confirmed()[0], '1'), { excluded: true, ids: [] })

  // Editing the saved template again keeps the teacher's decision; restoring
  // it brings every relation back.
  await editor.click('再次编辑模板')
  assert.equal(editor.isOpen(), true)
  assert.equal(editor.discoveries().length, 2)
  assert.equal(decision(question()).text, '恢复纠错')
  await decision(question()).dispatch('click')
  await editor.settle()
  assert.equal(decision(question()).text, '不纠错')
  assert.equal(brief(panel(question()).byClass('correction-excluded-note')), null)
  assert.equal(question().byClass('correction-discovery-badge').text, '可纠错 2 项')

  await editor.save()
  assert.equal(editor.confirmed().length, 2)
  assert.deepEqual(relations(editor.confirmed()[1], '1'), { excluded: false, ids: ['c1', 'c2'] })
})

test('template save approves visible corrections without requiring another discovery or confirmation', async (t) => {
  // The first discovery is still running when the teacher saves; the one
  // asked when the template is edited again stays open until the end.
  const late = deferred()
  const reopened = deferred()
  const answers = [late.promise, reopened.promise]
  const editor = await mountTemplateEditor(t, { pages: PAGES, discovery: () => answers.shift() })
  assert.equal(editor.discoveries().length, 1)

  await editor.save()
  assert.equal(editor.confirmed().length, 1, 'saving does not wait for the running discovery')
  assert.deepEqual(relations(editor.confirmed()[0], '1'), { excluded: false, ids: [] })
  assert.equal(editor.discoveries().length, 1, 'saving asks for no new discovery')
  assert.deepEqual(editor.page.boxes, [])
  assert.deepEqual(editor.page.win.confirmations, [])
  assert.equal(editor.isOpen(), false)

  // The late answer of the superseded discovery must not change the approved draft.
  late.resolve({ status: 200, data: FOUND })
  await editor.settle()
  await editor.click('再次编辑模板')
  assert.equal(editor.discoveries().length, 2)
  assert.equal(brief(panel(editor.question('1'))), null, 'the late discovery was dropped')
  await editor.save()
  assert.deepEqual(relations(editor.confirmed()[1], '1'), { excluded: false, ids: [] })
  reopened.resolve({ status: 200, data: { candidates: [] } })
  await editor.settle()
  assert.doesNotMatch(editor.client.messages.map(message => message.message).join('\n'), /未保存模板/)
})

test('a failed correction discovery leaves the template savable', async (t) => {
  const editor = await mountTemplateEditor(t, {
    pages: PAGES,
    discovery: () => ({ status: 503, data: { detail: '纠错服务暂不可用' } }),
  })
  assert.equal(editor.requests(TEMPLATE_DISCOVERY).length, 1)
  assert.equal(brief(panel(editor.question('1'))), null)
  await editor.save()
  assert.equal(editor.confirmed().length, 1)
  assert.deepEqual(relations(editor.confirmed()[0], '1'), { excluded: false, ids: [] })
  assert.equal(editor.isOpen(), false)
  assert.doesNotMatch(editor.client.messages.map(message => message.message).join('\n'), /未保存模板/)
})
