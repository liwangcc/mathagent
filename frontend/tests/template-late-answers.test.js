import assert from 'node:assert/strict'
import test from 'node:test'
import { takeTemplateAnswers } from '../src/utils/templateLateAnswers.js'
import { jsonResponse } from './helpers/memoryFetch.js'
import {
  mountTemplateEditor, templateJob, templateJobRoute, templateServer, TEMPLATE_CONFIRM,
} from './helpers/templateReviewFlow.js'

// A template page can be delivered while its slowest question is still being
// solved: that question arrives marked `answer_pending`, with no answer. The
// editor (the real 批改 page, Upload.vue) shows it as being generated, reads the
// template again every three seconds, takes the answer when the server has it,
// and does not let the template be saved before.

const SAVE = '保存模板（全部页）'
const GENERATING = '标准答案生成中…'
const TEMPLATE = 'tpl-late'
const JOB = templateJobRoute(TEMPLATE)

const solved = (no, answer, extra = {}) => ({
  question_no: String(no), question_type: 'solve', question_text: `第 ${no} 题题干`, standard_answer: answer,
  max_score: 5, directory_node_id: `p1-n00${no}`, ...extra,
})
const beingSolved = (no, extra = {}) => solved(no, '', { answer_pending: true, ...extra })
const pageOf = (...questions) => [{ page_index: 0, questions }]

async function openEditor(t, pages, options = {}) {
  const editor = await mountTemplateEditor(t, { pages, templateId: TEMPLATE, ...options })
  const row = label => editor.dialog().find(el => el.tagName === 'section' && el.getAttribute('aria-label') === label)
  return Object.assign(editor, {
    row,
    rowText: label => row(label)?.text ?? null,
    reads: () => editor.fetches(JOB).length,
    serve: served => editor.stream(JOB, () => jsonResponse(templateJob(TEMPLATE, served))),
    saveButton: () => editor.button(SAVE) ?? editor.button(GENERATING),
    question: no => editor.page.setup.templatePages[0].questions.find(question => question.question_no === String(no)),
    tags: no => editor.dialog().allByClass('tpl-q')
      .find(el => el.byClass('tpl-q-identity')?.text.startsWith(`Q${no}`))?.allByClass('tag-pending').map(el => el.text) ?? [],
  })
}

const isDisabled = el => el.disabled === true || el.hasAttribute('disabled')

test('a question whose answer is still being generated says so and holds the save back', async (t) => {
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)))

  assert.match(editor.rowText('标准答案生成中'), /第 2 题的标准答案正在生成，完成后自动填入；生成完成后才能保存模板。/)
  assert.deepEqual(editor.tags(2), ['标准答案生成中'])
  assert.deepEqual(editor.tags(1), [])
  // It is not reported as an answer the teacher forgot.
  assert.equal(editor.rowText('待补充标准答案'), null)
  assert.equal(editor.rowText('保存前需要调整'), null)
  assert.equal(editor.page.setup.templateAnswersPending, true)
  assert.equal(editor.page.setup.templateCanConfirm, false)
  assert.equal(editor.button(SAVE), null)
  assert.equal(isDisabled(editor.button(GENERATING)), true)
  assert.deepEqual(editor.requests(TEMPLATE_CONFIRM), [])
})

test('the editor reads the template again and takes the answer once the server has it', async (t) => {
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)))
  const opened = editor.reads()

  await editor.tick(2999)
  assert.equal(editor.reads(), opened)
  await editor.tick(1)
  assert.equal(editor.reads(), opened + 1)
  assert.equal(editor.page.setup.templateAnswersPending, true)

  editor.serve(pageOf(solved(1, '服务器上的第一题答案'), solved(2, 'AD=√3', { solution_steps: ['由勾股定理得 BC⊥AB', '解得 t²=3'] })))
  await editor.tick(3000)

  assert.equal(editor.reads(), opened + 2)
  const second = editor.question(2)
  assert.equal(second.standard_answer, 'AD=√3')
  assert.equal(second.solution_steps_text, '由勾股定理得 BC⊥AB\n解得 t²=3')
  assert.equal('answer_pending' in second, false)
  // Only the question that was waiting is touched.
  assert.equal(editor.question(1).standard_answer, 'x=2')
  assert.equal(editor.rowText('标准答案生成中'), null)
  assert.deepEqual(editor.tags(2), [])
  assert.equal(editor.page.setup.templateAnswersPending, false)
  assert.equal(isDisabled(editor.button(SAVE)), false)
  // Nothing is waiting any more: the template is not read again.
  await editor.tick(30000)
  assert.equal(editor.reads(), opened + 2)

  await editor.save()
  const [confirmed] = editor.confirmed()
  assert.deepEqual(confirmed.pages[0].questions.map(question => question.standard_answer), ['x=2', 'AD=√3'])
})

test('an answer the teacher typed while waiting is the one that stays', async (t) => {
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)))
  editor.question(2).standard_answer = '老师先写下的答案'
  await editor.settle()
  // Typed in or not, the template is not saved while the generated answer is on its way.
  assert.equal(isDisabled(editor.saveButton()), true)
  assert.equal(editor.page.setup.templateCanConfirm, false)

  editor.serve(pageOf(solved(1, 'x=2'), solved(2, 'AD=√3')))
  await editor.tick(3000)

  assert.equal(editor.question(2).standard_answer, '老师先写下的答案')
  assert.equal(editor.page.setup.templateAnswersPending, false)
  assert.equal(isDisabled(editor.button(SAVE)), false)
})

test('a question whose answer will not come is handed to the teacher and the reading stops', async (t) => {
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)))
  const opened = editor.reads()
  const reason = '标准答案生成超时，请填写标准答案或重新导入模板'
  editor.serve(pageOf(solved(1, 'x=2'), solved(2, '', { answer_failed: reason })))

  await editor.tick(3000)

  assert.equal(editor.reads(), opened + 1)
  assert.equal(editor.question(2).answer_failed, reason)
  assert.deepEqual(editor.tags(2), ['标准答案未生成，请填写'])
  assert.equal(editor.rowText('标准答案生成中'), null)
  // From here on it is an ordinary missing answer: named, and blocking only until the teacher fills it in.
  assert.match(editor.rowText('保存前需要调整'), /第 1 页第 2 题缺少标准答案/)
  assert.equal(isDisabled(editor.button(SAVE)), false)
  await editor.tick(30000)
  assert.equal(editor.reads(), opened + 1)
})

test('a read that fails is tried again, and leaving the template stops the reading', async (t) => {
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)))
  const opened = editor.reads()
  editor.stream(JOB, () => new Response('upstream unavailable', { status: 502 }))

  await editor.tick(3000)
  await editor.tick(3000)
  assert.equal(editor.reads(), opened + 2)
  assert.equal(editor.page.setup.templateAnswersPending, true)

  await editor.click('取消')
  await editor.tick(30000)
  assert.equal(editor.reads(), opened + 2)
})

test('a question with answer positions that is still being generated is not listed as missing its answers', async (t) => {
  const slots = { question_type: 'fill', blank_count: 2, answer_slots: ['', ''], slot_ids: ['2[1]', '2[2]'], region_kinds: ['answer', 'answer'] }
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2, slots)))

  assert.match(editor.rowText('标准答案生成中'), /第 2 题的标准答案正在生成/)
  assert.equal(editor.rowText('待补充标准答案'), null)

  editor.serve(pageOf(solved(1, 'x=2'), solved(2, '', { ...slots, answer_failed: '标准答案生成失败，请填写标准答案或重新导入模板' })))
  await editor.tick(3000)
  // Once no answer is coming, its two empty positions are the teacher's to fill.
  assert.match(editor.rowText('待补充标准答案'), /第 2 题有 2 个作答区域未设置标准答案/)
})

test('a read that comes back after another template was opened is dropped', async (t) => {
  const OTHER = 'tpl-other'
  const otherPages = pageOf(solved(1, '另一份模板的第一题'), beingSolved(2))
  const other = templateServer(OTHER, otherPages)
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)), { streams: other.streams, responses: other.responses })
  let answerFirstTemplate
  editor.stream(JOB, () => new Promise((resolve) => { answerFirstTemplate = resolve }))
  await editor.tick(3000)
  assert.equal(typeof answerFirstTemplate, 'function')

  // The teacher imports another template while that read is still on its way.
  await editor.page.setup.applyTemplateRecognitionResult(OTHER, otherPages)
  await editor.settle()
  answerFirstTemplate(jsonResponse(templateJob(TEMPLATE, pageOf(solved(1, 'x=2'), solved(2, '第一份模板迟到的答案')))))
  await editor.settle()

  // Both templates call that question p1-n002: the late answer of the first must not land in the second.
  assert.equal(editor.question(2).standard_answer, '')
  assert.equal(editor.question(2).answer_pending, true)
  const firstReads = editor.reads()
  await editor.tick(3000)
  assert.equal(editor.reads(), firstReads)
  assert.equal(editor.fetches(templateJobRoute(OTHER)).length, 1)
})

test('a read is only taken for the template it was started for', async (t) => {
  const editor = await openEditor(t, pageOf(solved(1, 'x=2'), beingSolved(2)))
  let answer
  editor.stream(JOB, () => new Promise((resolve) => { answer = resolve }))
  await editor.tick(3000)
  const reads = editor.reads()

  // The page moves on to another template id by a path that starts no new reading.
  editor.page.setup.pendingTemplateId = 'tpl-elsewhere'
  answer(jsonResponse(templateJob(TEMPLATE, pageOf(solved(1, 'x=2'), solved(2, '迟到的答案')))))
  await editor.settle()

  assert.equal(editor.question(2).standard_answer, '')
  await editor.tick(30000)
  assert.equal(editor.reads(), reads)
})

test('a template that is gone is not read again', async (t) => {
  const editor = await openEditor(t, pageOf(beingSolved(1)))
  const opened = editor.reads()
  editor.stream(JOB, () => new Response('not found', { status: 404 }))

  await editor.tick(3000)
  await editor.tick(30000)

  assert.equal(editor.reads(), opened + 1)
})

test('arrived answers go to the questions waiting for them, whatever happened to their numbers', () => {
  const slots = { slot_ids: ['3[1]', '3[2]'], region_kinds: ['answer', 'answer'] }
  const waiting = { question_no: '4', directory_node_id: 'p1-n003', answer_pending: true, standard_answer: '', answer_slots_text: ['', ''], slot_ids: ['4[1]', '4[2]'], region_kinds: ['answer', 'answer'] }
  const untouched = { question_no: '1', directory_node_id: 'p1-n001', standard_answer: '原答案' }
  const still = { question_no: '5', directory_node_id: 'p1-n004', answer_pending: true, standard_answer: '' }
  const pages = [{ page_index: 0, questions: [untouched, waiting, still] }]
  // The server still numbers it 3: the teacher inserted a question before it, which is not saved yet.
  const server = [{ page_index: 0, questions: [
    { question_no: '1', directory_node_id: 'p1-n001', standard_answer: '服务器答案' },
    { question_no: '3', directory_node_id: 'p1-n003', question_type: 'fill', standard_answer: '平行; √3', answer_slots: ['平行', '√3'], ...slots },
    { question_no: '4', directory_node_id: 'p1-n004', answer_pending: true, standard_answer: '' },
  ] }]

  assert.equal(takeTemplateAnswers(pages, server), 1)

  assert.equal(waiting.standard_answer, '平行; √3')
  assert.deepEqual(waiting.answer_slots_text, ['平行', '√3'])
  assert.deepEqual(waiting.slot_ids, ['4[1]', '4[2]'])
  assert.equal('answer_pending' in waiting, false)
  assert.equal(untouched.standard_answer, '原答案')
  assert.equal(still.answer_pending, true)
})

test('an arrived answer does not overwrite slots the teacher has since changed, and always brings the solver’s reports', () => {
  const flag = { scope: 'question', status: 'extra', reason: '该编号不是独立题目', source: 'solver' }
  const reshaped = { question_no: '2', directory_node_id: 'p1-n002', answer_pending: true, standard_answer: '', answer_slots_text: ['', '', ''], slot_ids: ['2[1]', '2[2]', '2[3]'] }
  const typed = { question_no: '3', directory_node_id: 'p1-n003', answer_pending: true, standard_answer: '老师的答案' }
  // Questions of an older draft carry no directory node: they are told apart by page and number.
  const numbered = { question_no: '7', answer_pending: true, standard_answer: '' }
  const nextNumbered = { question_no: '8', answer_pending: true, standard_answer: '' }
  const pages = [{ page_index: 2, questions: [reshaped, typed, numbered, nextNumbered] }]
  const server = [{ page_index: 2, questions: [
    { question_no: '2', directory_node_id: 'p1-n002', standard_answer: '甲; 乙', answer_slots: ['甲', '乙'], slot_ids: ['2[1]', '2[2]'] },
    { question_no: '3', directory_node_id: 'p1-n003', standard_answer: '生成的答案', max_flags: [flag] },
    { question_no: '7', standard_answer: '按页码和题号对上的答案' },
    { question_no: '8', standard_answer: '第八题的答案' },
  ] }]

  assert.equal(takeTemplateAnswers(pages, server), 0)

  // The whole-question answer arrives; the three slots the teacher made are left for the teacher.
  assert.equal(reshaped.standard_answer, '甲; 乙')
  assert.deepEqual(reshaped.answer_slots_text, ['', '', ''])
  assert.equal(typed.standard_answer, '老师的答案')
  assert.deepEqual(typed.max_flags, [flag])
  assert.equal(numbered.standard_answer, '按页码和题号对上的答案')
  assert.equal(nextNumbered.standard_answer, '第八题的答案')
})
