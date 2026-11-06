// The template editor's answer specification of one question (views/Upload.vue):
// the editor's normalised form, blank and sub-question scoring, choice answers,
// and marking a field as edited by the teacher.
import { isTemplateDirectoryLeaf, normalizeChoiceAnswer, templateFillAnswerSlots, normalizeChoiceInput, setTemplateFillAnswerSlot } from './templateReview.js'

export const choiceOptions = Object.freeze(['A', 'B', 'C', 'D'])

export function defaultGradingMethod(questionType) {
  return { choice: 'exact', fill: 'equivalent', solve: '' }[questionType] || ''
}

export function toTextLines(value) {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean).join('\n')
  return String(value || '').trim()
}

export function toStringList(value) {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean)
  return String(value || '').split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

export function markTemplateQuestionInput(question) {
  // The teacher drew the standard figure; a text edit never discards it. It
  // only needs re-checking, which the next template save records.
  if (question?.drawing_reference) question.drawing_reference.teacher_confirmed = false
}

export function markTemplateAnswerManualEdit(question, field = 'standard_answer') {
  if (!question || typeof question !== 'object') return
  // A choice has one physical answer slot; teacher edits own both views.
  if (field !== 'solution_steps_text' && question.question_type === 'choice'
    && question.slot_ids?.length === 1 && !question.region_kinds?.includes('drawing')) {
    question.answer_slots_text = [String(question.standard_answer ?? '')]
    question.answer_slots = [...question.answer_slots_text]
  }
  // Text answers and the teacher-drawn figure are independent answer parts.
  if (question.drawing_reference && field !== 'solution_steps_text') question.drawing_reference.teacher_confirmed = false
}

export function inferSubquestionCount(question) {
  const text = `${question.question_text || question.content || ''}\n${question.standard_answer || question.answer || ''}`
  const matches = [...text.matchAll(/[（(]\s*(\d{1,2})\s*[）)]/g)]
  if (!matches.length) return 1
  return Math.min(20, Math.max(...matches.map((item) => Number(item[1]) || 1)))
}

export function inferBlankCount(question) {
  const slots = Array.isArray(question.answer_slots) ? question.answer_slots.filter(Boolean) : []
  if (slots.length) return Math.min(20, slots.length)
  const text = question.question_text || question.content || ''
  const runs = text.match(/_{2,}|＿{2,}|﹏{2,}/g) || []
  return Math.min(20, Math.max(1, runs.length))
}

export function defaultSubquestionScores(question, count) {
  const raw = Array.isArray(question.subquestion_scores) ? question.subquestion_scores : []
  if (raw.length) return raw.map((value) => Number(value) || 0)
  const defaults = [5, 7, 8]
  return defaults.map((value, index) => (index < count ? value : 0))
}

export function equalBlankScores(total, count) {
  const safeCount = Math.min(20, Math.max(1, Number(count) || 1))
  const safeTotal = Math.max(0.5, Number(total) || 5)
  const base = Math.round((safeTotal / safeCount) * 10000) / 10000
  const scores = Array(safeCount).fill(base)
  scores[safeCount - 1] = Math.round((safeTotal - base * (safeCount - 1)) * 10000) / 10000
  return scores
}

export function normalizedBlankScores(question, count, total) {
  const raw = Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : []
  if (raw.length === count && raw.every((value) => Number.isFinite(value) && value > 0)) return raw
  return equalBlankScores(total, count)
}

export function isCustomFill(question) {
  return question?.question_type === 'fill' && Number(question.blank_count) > 1 && question.fill_scoring_mode === 'custom'
}

export function fillScoreTotal(question) {
  return (Array.isArray(question?.blank_scores) ? question.blank_scores : [])
    .reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0)
}

export function onFillScoringModeChange(question) {
  const count = Math.min(20, Math.max(1, Number(question.blank_count) || 1))
  if (!['equal', 'all_or_nothing', 'custom'].includes(question.fill_scoring_mode)) question.fill_scoring_mode = 'equal'
  if (question.fill_scoring_mode === 'custom') {
    question.blank_scores = equalBlankScores(question.max_score, count)
    question.max_score = fillScoreTotal(question)
  } else {
    question.blank_scores = []
  }
}

export function onBlankScoresChange(question) {
  if (!isCustomFill(question)) return
  const count = Math.min(20, Math.max(1, Number(question.blank_count) || 1))
  const raw = Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : []
  if (raw.length === count && raw.every((value) => Number.isFinite(value) && value > 0)) {
    question.max_score = raw.reduce((sum, value) => sum + value, 0)
  }
}

export function onBlankCountChange(question) {
  question.blank_count = Math.min(20, Math.max(1, Number(question.blank_count) || 1))
  if (isCustomFill(question)) {
    question.blank_scores = equalBlankScores(question.max_score, question.blank_count)
    question.max_score = fillScoreTotal(question)
  }
}

export function normalizeAnswerSpecForEditor(question) {
  const questionType = question.question_type || 'solve'
  const directoryLeaf = isTemplateDirectoryLeaf(question)
  const typedSlots = Object.hasOwn(question, 'region_kinds') || Object.hasOwn(question, 'slot_ids')
  const blankCount = typedSlots ? question.blank_count
    : directoryLeaf ? (Number(question.blank_count) || 1) : Math.max(Number(question.blank_count) || 1, inferBlankCount(question))
  const subquestionCount = directoryLeaf ? 1 : Math.max(Number(question.subquestion_count) || 1, inferSubquestionCount(question))
  const maxScore = Number(question.max_score) > 0 ? Number(question.max_score) : 5
  const standardAnswer = String(question.standard_answer ?? question.answer ?? '').trim()
  const normalizedChoiceAnswer = questionType === 'choice' ? normalizeChoiceAnswer(standardAnswer) : ''
  // Keep typed drafts as arrays so missing/invalid positions cannot be repaired by joining text.
  const normalizedAnswerSlots = typedSlots ? templateFillAnswerSlots(question)
    : directoryLeaf || (questionType === 'fill' && blankCount > 1)
    ? templateFillAnswerSlots({ ...question, blank_count: blankCount, standard_answer: standardAnswer }).join('\n')
    : toTextLines(question.answer_slots_text || question.answer_slots)
  const typedGraphic = Array.isArray(question.region_kinds) && question.region_kinds.includes('drawing')
  const fillScoringMode = !typedGraphic && ['equal', 'all_or_nothing', 'custom'].includes(question.fill_scoring_mode)
    ? question.fill_scoring_mode : 'equal'
  return {
    ...question,
    standard_answer: normalizedChoiceAnswer || standardAnswer,
    question_type: questionType,
    choice_mode: questionType === 'choice' && ['single', 'multiple'].includes(question.choice_mode)
      ? question.choice_mode : 'unknown',
    grading_method: question.grading_method || defaultGradingMethod(questionType),
    answer_variants_text: toTextLines(question.answer_variants_text || question.answer_variants),
    solution_steps_text: toTextLines(question.solution_steps_text || question.solution_steps),
    max_score: maxScore,
    blank_count: typedSlots ? blankCount : Math.min(20, Math.max(1, blankCount)),
    answer_slots_text: normalizedAnswerSlots,
    fill_scoring_mode: fillScoringMode,
    blank_scores: typedGraphic ? [] : typedSlots && !Number.isInteger(Number(blankCount))
      ? (Array.isArray(question.blank_scores) ? [...question.blank_scores] : [])
      : normalizedBlankScores(question, blankCount, maxScore),
    subquestion_count: Math.min(20, Math.max(1, subquestionCount)),
    subquestion_scores: directoryLeaf
      ? [Number(question.subquestion_scores?.[0]) || maxScore]
      : defaultSubquestionScores(question, subquestionCount),
  }
}

export function onQuestionTypeChange(question) {
  const normalized = normalizeAnswerSpecForEditor({ ...question, question_type: question.question_type })
  Object.assign(question, normalized, { grading_method: defaultGradingMethod(question.question_type) })
}

export function choiceModeLabel(mode) {
  return mode === 'single' ? '单选' : mode === 'multiple' ? '多选' : '待判型'
}

export function setTemplateChoiceMode(question, mode) {
  if (question?.question_type !== 'choice' || !['single', 'multiple'].includes(mode)) return
  if (question.choice_mode === mode) return
  question.choice_mode = mode
  markTemplateQuestionInput(question)
}

export function setSingleChoiceAnswer(question, option) {
  if (question?.question_type !== 'choice' || !choiceOptions.includes(option)) return
  question.standard_answer = option
  markTemplateAnswerManualEdit(question)
}

export function onChoiceAnswerChange(question) {
  question.standard_answer = normalizeChoiceAnswer(question.standard_answer)
  markTemplateAnswerManualEdit(question)
}

export function onChoiceAnswerInput(question, event) {
  const normalized = normalizeChoiceInput(event?.target?.value)
  question.standard_answer = normalized
  markTemplateAnswerManualEdit(question)
  if (event?.target && event.target.value !== normalized) event.target.value = normalized
}

export function onTemplateFillSlotChange(question, slotIndex, event) {
  if (Object.hasOwn(question, 'region_kinds') && question.region_kinds?.[slotIndex] !== 'answer') return
  setTemplateFillAnswerSlot(question, slotIndex, event?.target?.value)
  markTemplateAnswerManualEdit(question)
}

export function onTemplateAnswerSlotsInput(question) {
  if (!Array.isArray(question.region_kinds) || !question.region_kinds.includes('drawing')) {
    question.standard_answer = String(question.answer_slots_text || '').split(/\r?\n/).map((slot) => slot.trim()).join('; ')
  }
  markTemplateAnswerManualEdit(question)
}

export function addSubquestionScore(question) {
  if (isTemplateDirectoryLeaf(question)) return
  if (!Array.isArray(question.subquestion_scores)) question.subquestion_scores = []
  if (question.subquestion_scores.length < 20) question.subquestion_scores.push(8)
}

export function removeSubquestionScore(question, index) {
  if (!Array.isArray(question.subquestion_scores) || question.subquestion_scores.length <= 1) return
  question.subquestion_scores.splice(index, 1)
}
