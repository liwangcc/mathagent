import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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

test('模板确认只让待判型选择题出现判型控件，并提供紧凑答案卡编辑', async () => {
  const upload = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

  assert.match(upload, /v-for="option in choiceOptions"/)
  assert.match(upload, /v-else-if="entry\.question\.choice_mode === 'multiple'"/)
  assert.match(upload, /@input="onChoiceAnswerInput\(entry\.question, \$event\)"/)
  assert.match(upload, /v-else class="tpl-choice-mode-picker"/)
  assert.match(upload, /class="tpl-choice-mode-badge tpl-q-mode-badge"/)
  assert.match(upload, /templateFillAnswerSlots\(entry\.question\)/)
  assert.doesNotMatch(upload, /class="tpl-choice-conflicts"/)
})

test('保存全部页模板时一次性应用选择题和填空题分值', async () => {
  const upload = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

  assert.match(upload, /function applyTemplateBatchScores\(\)/)
  assert.match(upload, /applyUniformTemplateScore\(templatePages\.value, 'choice', templateChoiceScore\.value\)/)
  assert.match(upload, /applyUniformTemplateScore\(templatePages\.value, 'fill', templateFillScore\.value\)/)
  assert.match(upload, /applyTemplateBatchScores\(\)/)
  assert.match(upload, /保存模板（全部页）/)
  assert.match(upload, /saveAllTemplateLayoutPages/)
  assert.doesNotMatch(upload, /class="tpl-batch-apply"/)
})
