import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyUniformTemplateScore,
  choiceContractValid,
  normalizeChoiceAnswer,
  normalizeChoiceInput,
  setTemplateFillAnswerSlot,
  templateFillAnswerSlots,
  templateAnswerText,
  templateChoiceConflictEntries,
  templateChoiceReviewEntries,
  templateReviewEntries,
} from '../src/utils/templateReview.js'
import { brief, mountTemplateEditor } from './helpers/templateReviewFlow.js'

test('答题卡按题型保留页码、题号与多空答案', () => {
  const pages = [
    {
      page_index: 0,
      questions: [
        { question_no: '1', question_type: 'choice', standard_answer: 'B' },
        { question_no: '2', question_type: 'fill', answer_slots_text: '3\n-2' },
      ],
    },
    {
      page_index: 1,
      questions: [{ question_no: '1', question_type: 'choice', standard_answer: 'C' }],
    },
  ]

  const choices = templateReviewEntries(pages, 'choice')
  assert.deepEqual(choices.map(({ pageIndex, question }) => [pageIndex, question.question_no, templateAnswerText(question)]), [
    [0, '1', 'B'],
    [1, '1', 'C'],
  ])
  assert.equal(templateAnswerText(pages[0].questions[1]), '3、-2')
})

test('选择题和填空题各自批量设置分值，保留多空自定义分值', () => {
  const pages = [{
    questions: [
      { question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: 'A', max_score: 2 },
      { question_no: '1b', question_type: 'choice', choice_mode: 'single', standard_answer: '-3', max_score: 9 },
      { question_no: '2', question_type: 'fill', max_score: 3, fill_scoring_mode: 'equal', blank_scores: [1.5, 1.5] },
      { question_no: '3', question_type: 'fill', max_score: 5, blank_count: 2, fill_scoring_mode: 'custom', blank_scores: [2, 3] },
    ],
  }]

  assert.equal(applyUniformTemplateScore(pages, 'choice', 4), 1)
  assert.equal(applyUniformTemplateScore(pages, 'fill', 2), 1)
  assert.equal(pages[0].questions[0].max_score, 4)
  assert.equal(pages[0].questions[1].max_score, 9)
  assert.equal(pages[0].questions[2].max_score, 2)
  assert.deepEqual(pages[0].questions[2].blank_scores, [])
  assert.equal(pages[0].questions[3].max_score, 5)
  assert.deepEqual(pages[0].questions[3].blank_scores, [2, 3])
})

test('选择题答题卡接收已确认单选/多选合同，并把未知或冲突交给老师处理', () => {
  const pages = [{
    questions: [
      { question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: '答案：d' },
      { question_no: '2', question_type: 'choice', choice_mode: 'multiple', standard_answer: 'C, A' },
      { question_no: '4', question_type: 'choice', choice_mode: 'single', standard_answer: '-3' },
      { question_no: '5', question_type: 'choice', choice_mode: 'unknown', standard_answer: 'A' },
      { question_no: '15', question_type: 'choice', choice_mode: 'multiple', standard_answer: 'AA' },
      { question_no: '16', question_type: 'fill', standard_answer: '5' },
    ],
  }]

  assert.equal(normalizeChoiceAnswer('答案：d'), 'D')
  assert.equal(normalizeChoiceAnswer('选 B。'), 'B')
  assert.equal(normalizeChoiceAnswer('c, a'), 'AC')
  assert.equal(normalizeChoiceAnswer('AA'), '')
  assert.equal(normalizeChoiceAnswer('-3'), '')
  assert.equal(choiceContractValid(pages[0].questions[1]), true)
  assert.deepEqual(
    templateChoiceReviewEntries(pages).map(({ question }) => question.question_no),
    ['1', '2'],
  )
  assert.deepEqual(
    templateChoiceConflictEntries(pages).map(({ question }) => question.question_no),
    ['4', '5', '15'],
  )
})

test('答题卡编辑选择答案时即时转大写、去重并按选项顺序排列', () => {
  assert.equal(normalizeChoiceInput('ac'), 'AC')
  assert.equal(normalizeChoiceInput('c, a, c'), 'AC')
  assert.equal(normalizeChoiceInput('答案：d'), 'D')
  assert.equal(normalizeChoiceInput(''), '')
})

test('填空答题卡按空位编辑并同步标准答案合同', () => {
  const question = {
    question_type: 'fill',
    blank_count: 2,
    standard_answer: '1; 2',
  }

  assert.deepEqual(templateFillAnswerSlots(question), ['1', '2'])
  setTemplateFillAnswerSlot(question, 1, ' 3 ')
  assert.deepEqual(templateFillAnswerSlots(question), ['1', '3'])
  assert.equal(question.answer_slots_text, '1\n3')
  assert.equal(question.standard_answer, '1; 3')

  setTemplateFillAnswerSlot(question, 0, '')
  assert.deepEqual(templateFillAnswerSlots(question), ['', '3'])
})

// The editor of the real Upload page, opened on a recognized template with
// one question of each answer-card kind.
const REVIEW_PAGES = [{
  page_index: 0,
  questions: [
    { question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: 'B', max_score: 2, question_text: '单选题' },
    { question_no: '2', question_type: 'choice', choice_mode: 'multiple', standard_answer: 'AC', max_score: 2, question_text: '多选题' },
    { question_no: '3', question_type: 'choice', choice_mode: 'unknown', standard_answer: 'A', max_score: 2, question_text: '待判型题' },
    { question_no: '4', question_type: 'choice', choice_mode: 'single', standard_answer: '-3', max_score: 2, question_text: '答案冲突的选择题' },
    { question_no: '5', question_type: 'fill', blank_count: 2, standard_answer: '1; 2', max_score: 3, question_text: '两空填空' },
  ],
}]

const buttonTexts = el => el.byTag('button').map(button => button.text)
const pressed = el => el.byTag('button').filter(button => button.getAttribute('aria-pressed') === 'true').map(button => button.text)
const inputs = el => el.byTag('input')

// What typing (`input`) or leaving an edited field (`change`) does.
async function setInput(upload, el, value, event) {
  el.value = value
  await el.dispatch(event)
  await upload.settle()
}

test('模板确认只让待判型选择题出现判型控件，并提供紧凑答案卡编辑', async (t) => {
  const editor = await mountTemplateEditor(t, { pages: REVIEW_PAGES })
  assert.equal(editor.isOpen(), true)

  // 单选：A-D 四个按钮，点选即改标准答案。
  const single = editor.choiceCell('1')
  assert.deepEqual(buttonTexts(single).slice(1), ['A', 'B', 'C', 'D'])
  assert.deepEqual(pressed(single), ['B'])
  assert.deepEqual(inputs(single).map(brief), [])
  assert.equal(brief(single.byClass('tpl-choice-mode-picker')), null)
  await single.byText('button', 'C').dispatch('click')
  await editor.settle()
  assert.deepEqual(pressed(editor.choiceCell('1')), ['C'])
  assert.equal(editor.question('1').byClass('tpl-q-answer-preview').text, 'C')

  // 多选：一个输入框，输入即转大写、去重并排序。
  const multiple = editor.choiceCell('2')
  assert.equal(brief(multiple.byClass('tpl-choice-options')), null)
  assert.equal(brief(multiple.byClass('tpl-choice-mode-picker')), null)
  const [multipleInput] = inputs(multiple)
  assert.equal(multipleInput.getAttribute('aria-label'), '第 2 题多选答案')
  assert.equal(multipleInput.value, 'AC')
  await setInput(editor, multipleInput, 'dba', 'input')
  assert.equal(multipleInput.value, 'ABD')
  assert.equal(editor.question('2').byClass('tpl-q-answer-preview').text, 'ABD')

  // 只有待判型题出现“单选 / 多选”判型控件。
  const pickers = editor.dialog().allByClass('tpl-choice-mode-picker')
  assert.equal(pickers.length, 1)
  const unknown = editor.choiceCell('3')
  assert.ok(unknown.contains(pickers[0]))
  assert.deepEqual(buttonTexts(pickers[0]), ['单选', '多选'])
  assert.equal(unknown.byClass('tpl-choice-mode-badge').text, '待判型')
  assert.ok(unknown.byClass('tpl-choice-mode-badge').classList.contains('is-pending'))
  assert.equal(brief(unknown.byClass('tpl-choice-options')), null)
  assert.match(unknown.text, /待处理/)

  // 题目详情的标题行也标出选择类型（填空题没有）。
  assert.deepEqual(['1', '2', '3', '4'].map(no => editor.question(no).byClass('tpl-q-mode-badge')?.text), ['单选', '多选', '待判型', '单选'])
  assert.equal(brief(editor.question('5').byClass('tpl-q-mode-badge')), null)

  await pickers[0].byText('button', '多选').dispatch('click')
  await editor.settle()
  assert.equal(editor.dialog().allByClass('tpl-choice-mode-picker').length, 0)
  assert.equal(inputs(editor.choiceCell('3'))[0].getAttribute('aria-label'), '第 3 题多选答案')
  assert.equal(editor.question('3').byClass('tpl-q-mode-badge').text, '多选')

  // 答案冲突的选择题留在答题卡里由老师处理，没有单独的冲突面板。
  const conflict = editor.choiceCell('4')
  assert.ok(conflict.classList.contains('is-unresolved'))
  assert.match(conflict.text, /待处理/)
  assert.deepEqual(pressed(conflict), [])
  assert.equal(brief(editor.dialog().byClass('tpl-choice-conflicts')), null)

  // 两空填空：每空一个输入框，改一空即同步该题答案。
  const fill = editor.fillCell('5')
  const slots = inputs(fill)
  assert.deepEqual(slots.map(el => el.getAttribute('aria-label')), ['第 5 题第 1 空答案', '第 5 题第 2 空答案'])
  assert.deepEqual(slots.map(el => el.value), ['1', '2'])
  await setInput(editor, slots[1], ' 5 ', 'change')
  assert.deepEqual(inputs(editor.fillCell('5')).map(el => el.value), ['1', '5'])
  assert.equal(editor.question('5').byClass('tpl-q-answer-preview').text, '1、5')
  assert.deepEqual(editor.page.errors, [])
})

const EXAM_PAGES = [{
  page_index: 0,
  questions: [
    { question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: 'B', max_score: 2, question_text: '单选题' },
    { question_no: '2', question_type: 'choice', choice_mode: 'multiple', standard_answer: 'AC', max_score: 2, question_text: '多选题' },
    { question_no: '3', question_type: 'fill', blank_count: 1, standard_answer: '7', max_score: 2, question_text: '一空填空' },
    { question_no: '4', question_type: 'fill', blank_count: 2, standard_answer: '1; 2', max_score: 5, fill_scoring_mode: 'custom', blank_scores: [2, 3], question_text: '自定义分值的两空填空' },
  ],
}]

const scoreInput = (editor, label) => editor.dialog().find(el => el.tagName === 'input' && el.getAttribute('aria-label') === label)
const savedScores = payload => payload.pages[0].questions.map(q => [q.question_no, q.max_score, q.blank_scores])

test('保存全部页模板时一次性应用选择题和填空题分值', async (t) => {
  const editor = await mountTemplateEditor(t, { pages: EXAM_PAGES, category: 'exam', examName: '期中考试' })
  const choiceScore = scoreInput(editor, '选择题统一分值')
  const fillScore = scoreInput(editor, '填空题统一分值')
  assert.equal(String(choiceScore.value), '2')
  assert.equal(String(fillScore.value), '2')
  // 没有单独的“应用分值”按钮：分值区只有输入框，保存时自动应用。
  assert.equal(brief(editor.dialog().byClass('tpl-batch-apply')), null)
  const scoreAreas = editor.dialog().allByClass('tpl-batch-score')
  assert.equal(scoreAreas.length, 2)
  for (const area of scoreAreas) {
    assert.deepEqual(area.byTag('button').map(brief), [])
    assert.match(area.text, /点击下方保存并冻结时自动应用/)
  }

  await editor.type(choiceScore, '3')
  await editor.type(fillScore, '4')
  // 输入后尚未应用到各题。
  assert.match(editor.question('1').text, /本题 2 分/)
  assert.equal(editor.confirmed().length, 0)

  await editor.save()
  assert.equal(editor.confirmed().length, 1)
  const [payload] = editor.confirmed()
  assert.equal(payload.category, 'exam')
  assert.equal(payload.exam_name, '期中考试')
  // 一次保存同时应用两种分值；自定义各空分值的多空填空保留原分值。
  assert.deepEqual(savedScores(payload), [
    ['1', 3, []],
    ['2', 3, []],
    ['3', 4, []],
    ['4', 5, [2, 3]],
  ])
  assert.equal(editor.isOpen(), false)
  assert.match(editor.page.root.byClass('tpl-confirmed').text, /本次模板已保存（1 页）/)
})

test('统一分值无效时保存模板被拦下并提示，日常作业不出现统一分值', async (t) => {
  const exam = await mountTemplateEditor(t, { pages: EXAM_PAGES, category: 'exam', examName: '期中考试' })
  await exam.type(scoreInput(exam, '选择题统一分值'), '0')
  await exam.save()
  assert.equal(exam.confirmed().length, 0)
  assert.ok(exam.client.messages.some(message => message.type === 'warning' && message.message === '统一分值必须大于 0'),
    JSON.stringify(exam.client.messages))
  assert.equal(exam.isOpen(), true)
  exam.unmount()

  const daily = await mountTemplateEditor(t, { pages: EXAM_PAGES, category: 'daily' })
  assert.equal(brief(daily.dialog().byClass('tpl-batch-score')), null)
  await daily.save()
  const [payload] = daily.confirmed()
  assert.equal(payload.category, 'daily')
  assert.deepEqual(savedScores(payload).map(([no, score]) => [no, score]), [['1', null], ['2', null], ['3', null], ['4', null]])
})
