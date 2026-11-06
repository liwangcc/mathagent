// Drawing questions in the template editor (views/Upload.vue): reference
// drawings, their regions and readiness, and drawing labels.
import { templateQuestionMetadata, templateDrawingReview, defaultTemplateDrawingLabel } from './templateReview.js'

export const TEMPLATE_DRAWING_KINDS = [
  { key: 'non_graphic', label: '普通题' },
  { key: 'drawing', label: '作图题' },
  { key: 'mixed', label: '图文混合' },
]

export function updateDrawingReference(question, reference) {
  if (!question) return
  question.drawing_reference = reference
  question._drawingReferenceDirty = true
}

export function templateDrawingReferenceRegionsFor(page, question) {
  return page.drawingBoxes.filter((box) => String(box.question_number) === String(question?.question_no)).map((box) => box.bbox)
}

export function markTemplateReferenceImageReady(page, question) {
  page.referenceImageReadyNumbers ||= {}
  page.referenceImageReadyNumbers[String(question?.question_no || '')] = true
}

export function templateDrawingReferenceSummary(question) {
  const reference = question.drawing_reference
  if (!reference) return '未设置标准图；保存后将按题干和作图区直接交给 Flash 判定。'
  if (reference.comparison_mode === 'manual') return reference.variants?.length
    ? '自定义作图：不按固定坐标比较，批改时把题干和作图区交给 Flash。'
    : '未保存标准笔画；保存后将按题干和作图区直接交给 Flash 判定。'
  return `标准图 ${reference.variants?.length || 0} 个完整方案 · ${reference.teacher_confirmed ? '已随模板保存' : '随保存模板一起确认'}`
}

export function templateDrawingReferenceSnapshot(question) {
  return JSON.stringify({
    question_no: question.question_no, question_text: question.question_text,
    standard_answer: question.standard_answer, answer_kind: question.answer_kind,
    drawing_regions: question.drawing_regions,
    drawing_reference: question.drawing_reference,
  })
}

export function applyTemplateDrawingSchema(page, schema = []) {
  page.questionSchema = (schema || []).map((item) => ({ ...item, ...templateQuestionMetadata(item) }))
  Object.assign(page, templateDrawingReview(page.questionSchema))
}

export function templateHasDrawingQuestion(page, number) {
  return page.questionSchema.some((item) => String(item.question_number ?? item.question_no) === String(number)
    && ['drawing', 'mixed'].includes(item.answer_kind))
}

export function templateDrawingLabelsMissing(page) {
  return page.questionSchema.filter((question) => ['drawing', 'mixed'].includes(question.answer_kind)
    && !page.drawingLabels.some((label) => String(label.question_number) === String(question.question_number ?? question.question_no)))
}

export function templateDrawingKind(page) {
  return page.questionSchema.find((item) => String(item.question_number ?? item.question_no) === String(page.drawingQuestionNumber))?.answer_kind || 'non_graphic'
}

export function ensureTemplateDrawingLabel(page, number) {
  if ((page.drawingLabels || []).some((label) => String(label.question_number) === String(number))) return
  const anchor = defaultTemplateDrawingLabel((page.drawingBoxes || [])
    .filter((box) => String(box.question_number) === String(number)).map((box) => box.bbox))
  if (anchor) page.drawingLabels.push({ question_number: String(number), label_anchor: anchor })
}

// Labels follow boxes: a question's Q marker belongs to its first (main)
// drawing area, so moving that area carries the marker along.
export function templateFollowingDrawingLabel(page, box) {
  const number = String(box?.question_number ?? '')
  const main = (page.drawingBoxes || []).find((item) => String(item.question_number) === number)
  if (!number || main !== box) return null
  return (page.drawingLabels || []).find((label) => String(label.question_number) === number) || null
}
