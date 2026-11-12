function toAnswerSlots(question = {}) {
  const raw = question.answer_slots_text ?? question.answer_slots ?? []
  if (Array.isArray(raw)) return raw.map((item) => String(item).trim()).filter(Boolean)
  return String(raw).split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

/** Q is a display prefix. Keep dotted IDs as strings, including repeated numbering. */
export function templateQuestionIdentity(question = {}) {
  const number = String(question.question_no ?? question.question_number ?? '').trim()
  const path = Array.isArray(question.printed_path)
    ? question.printed_path.map((part) => String(part).trim()).filter(Boolean)
    : []
  return `${number ? `Q${number}` : '待绑定题号'}${path.length ? ` · 原卷 ${path.join(' / ')}` : ''}`
}

export function isTemplateDirectoryLeaf(question = {}) {
  return Boolean(String(question.directory_node_id || '').trim())
}

const TEMPLATE_QUESTION_METADATA = [
  'directory_node_id', 'directory_parent_id', 'printed_path',
  'answer_kind', 'drawing_regions', 'label_anchor', 'region_kinds', 'slot_ids',
]

/** Explicitly copy the server-owned contract without sharing mutable geometry arrays. */
export function templateQuestionMetadata(question = {}) {
  return Object.fromEntries(TEMPLATE_QUESTION_METADATA
    .filter((key) => Object.prototype.hasOwnProperty.call(question, key))
    .map((key) => [key, Array.isArray(question[key])
      ? question[key].map((item) => Array.isArray(item) ? [...item] : item)
      : question[key]]))
}

/** Editable answer contract is separate from immutable Q/region metadata. */
export function templateDrawingReference(question = {}) {
  return question.drawing_reference && typeof question.drawing_reference === 'object'
    ? { drawing_reference: JSON.parse(JSON.stringify(question.drawing_reference)) } : {}
}

function validNormalizedGeometry(value, length) {
  return Array.isArray(value) && value.length === length
    && value.every((part) => typeof part === 'number' && Number.isFinite(part) && part >= 0 && part <= 1000)
    && (length !== 4 || (value[0] < value[2] && value[1] < value[3]))
}

/** Default drawing Q marker: just left of the first drawing area, vertically centred. */
export function defaultTemplateDrawingLabel(boxes = []) {
  const first = (boxes || []).find((box) => validNormalizedGeometry(box, 4))
  if (!first) return null
  return [Math.max(0, first[0] - 15), Math.round((first[1] + first[3]) / 2)]
}

// Box limits the server enforces on every saved page (services/template_evidence.py).
export const TEMPLATE_MAX_BOX_AREA = 250_000
export const TEMPLATE_MAX_PAGE_BOX_AREA = 600_000

const boxArea = (bbox) => (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])

/**
 * Boxes the server would reject for size, named so the teacher can fix them.
 * ``boxes`` is exactly what the page save sends: [{ box, kind }] with kind
 * 'answer' or 'drawing'. A model box this large still opens in the editor;
 * the teacher decides how to shrink or remove it.
 */
export function templateBoxSizeProblems(boxes = [], pageNumber = 1) {
  const sized = boxes.filter(({ box }) => validNormalizedGeometry(box?.bbox, 4))
  const problems = sized.filter(({ box }) => boxArea(box.bbox) > TEMPLATE_MAX_BOX_AREA).map(({ box, kind }) => {
    const name = kind === 'drawing' ? '画图区' : '作答框'
    return { box, kind, message: `Q${box.slot_id || box.question_number} 的${name}超过整页的四分之一，可能框得过大：请拖动右下角缩小到实际${kind === 'drawing' ? '作图' : '作答'}范围，或删除后重新框选` }
  })
  if (sized.reduce((total, { box }) => total + boxArea(box.bbox), 0) > TEMPLATE_MAX_PAGE_BOX_AREA) {
    problems.push({ box: null, kind: 'page', message: `第 ${pageNumber} 页的作答框和画图区合计覆盖超过整页的六成，请缩小过大的框` })
  }
  return problems
}

/**
 * Whether a fixed standard figure leaves its drawing areas. Only the centre
 * line is checked, so this never flags a figure the server accepts; the
 * server still checks the inked width at confirmation.
 */
export function templateReferenceLeavesRegions(reference, regions = []) {
  if (reference?.comparison_mode !== 'fixed') return false
  const boxes = (regions || []).filter((box) => validNormalizedGeometry(box, 4))
  if (!boxes.length) return false
  const inside = ([x, y]) => boxes.some(([x0, y0, x1, y1]) => x >= x0 && x <= x1 && y >= y0 && y <= y1)
  const path = (points, closed) => {
    const ends = closed ? [...points, points[0]] : points
    const samples = [points[0]]
    for (let index = 1; index < ends.length; index++) {
      const [ax, ay] = ends[index - 1], [bx, by] = ends[index]
      const steps = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / 2))
      for (let step = 1; step <= steps; step++) samples.push([ax + (bx - ax) * step / steps, ay + (by - ay) * step / steps])
    }
    return samples
  }
  for (const variant of reference.variants || []) {
    for (const stroke of variant?.strokes || []) {
      let samples = []
      if (stroke.kind === 'point') samples = [stroke.point]
      else if (stroke.kind === 'ellipse') {
        samples = Array.from({ length: 72 }, (_, index) => [
          stroke.center[0] + stroke.radii[0] * Math.cos(index * Math.PI / 36),
          stroke.center[1] + stroke.radii[1] * Math.sin(index * Math.PI / 36),
        ])
      } else if (Array.isArray(stroke.points) && stroke.points.length) samples = path(stroke.points, stroke.kind === 'polygon')
      if (samples.some((point) => Array.isArray(point) && !inside(point))) return true
    }
  }
  return false
}

/** Drawing subtype changes Q placement only, not the existing choice/fill/solve types. */
export function templateDrawingReview(schema = [], { includeConflictingGeometry = false } = {}) {
  const drawingBoxes = []
  const drawingLabels = []
  for (const question of schema || []) {
    const regions = Array.isArray(question.drawing_regions) ? question.drawing_regions : []
    const hasConflictingGeometry = includeConflictingGeometry
      && question.answer_kind === 'non_graphic'
      && (regions.length > 0 || question.label_anchor != null)
    if (!['drawing', 'mixed'].includes(question.answer_kind) && !hasConflictingGeometry) continue
    const question_number = String(question.question_number ?? question.question_no ?? '').trim()
    if (!question_number) continue
    for (const bbox of regions) {
      if (validNormalizedGeometry(bbox, 4)) drawingBoxes.push({ question_number, bbox: [...bbox] })
    }
    if (validNormalizedGeometry(question.label_anchor, 2)) {
      drawingLabels.push({ question_number, label_anchor: [...question.label_anchor] })
    }
  }
  return { drawingBoxes, drawingLabels }
}

/** Invalid model geometry stays an editable candidate, never a frozen identity. */
export function mergeTemplateDrawingCandidates(schema = [], candidates = [], reviewPending = false) {
  const byNumber = new Map((reviewPending && Array.isArray(candidates) ? candidates : [])
    .map((item) => [String(item.question_number ?? item.question_no ?? ''), item]))
  return schema.map((question) => {
    const result = { ...question, ...templateQuestionMetadata(question) }
    const candidate = byNumber.get(String(question.question_number ?? question.question_no ?? ''))
    if (!candidate) return result
    const metadata = templateQuestionMetadata(candidate)
    for (const key of ['answer_kind', 'drawing_regions', 'label_anchor']) {
      if (Object.prototype.hasOwnProperty.call(metadata, key)) result[key] = metadata[key]
    }
    return result
  })
}

/** Keep all leaf identity metadata while updating only teacher-editable drawing geometry. */
export function serializeTemplateDrawingReview(schema = [], boxes = [], labels = [], { preserveConflictingGeometry = false } = {}) {
  return schema.map((question) => {
    const result = { ...question, ...templateQuestionMetadata(question) }
    // Rebuild geometry from the editable marker collections even for a
    // non-graphic question that still carries stale model candidate fields.
    // Deleting a marker must remove it from the wire payload as well; keeping
    // the old schema arrays makes the server see "non_graphic + drawing" and
    // reject an otherwise valid teacher correction.
    // The teacher's per-region marks are the source of truth (same rule as
    // updateSlotStructure and the server contract R4): a stale model
    // answer_kind re-derives from them at serialize time.
    const kinds = Array.isArray(question.region_kinds) && question.region_kinds.length ? question.region_kinds : null
    const answerKind = kinds
      ? (kinds.includes('drawing') ? (kinds.includes('answer') ? 'mixed' : 'drawing') : 'non_graphic')
      : question.answer_kind
    if (kinds) result.answer_kind = answerKind
    const hasGeometryMetadata = Object.prototype.hasOwnProperty.call(question, 'drawing_regions')
      || Object.prototype.hasOwnProperty.call(question, 'label_anchor')
    if (!['drawing', 'mixed'].includes(answerKind)
      && (!hasGeometryMetadata || preserveConflictingGeometry)) return result
    const number = String(question.question_number ?? question.question_no ?? '').trim()
    result.drawing_regions = boxes.filter((box) => String(box.question_number) === number)
      .map((box) => [...box.bbox])
    const label = labels.find((item) => String(item.question_number) === number)
    // A drawing question always gets its Q marker: a deleted or never-placed
    // one is re-derived beside the first drawing area instead of blocking save,
    // and so is one that area has moved past (labels follow boxes; the server
    // needs the marker at or left of the first area's left edge).
    const main = result.drawing_regions[0]
    const stranded = ['drawing', 'mixed'].includes(answerKind) && !preserveConflictingGeometry
      && Boolean(label && main && label.label_anchor[0] > main[0])
    result.label_anchor = label && !stranded ? [...label.label_anchor]
      : ['drawing', 'mixed'].includes(answerKind) && !preserveConflictingGeometry
        ? defaultTemplateDrawingLabel(result.drawing_regions) : null
    return result
  })
}

function toAnswerSlotDrafts(question = {}) {
  const raw = question.answer_slots_text ?? question.answer_slots ?? []
  if (Array.isArray(raw)) return raw.map((item) => String(item).trim())
  if (!String(raw).length) return []
  return String(raw).split(/\r?\n/).map((item) => item.trim())
}

const CHOICE_ANSWER_RE = /^(?:答案\s*[：:]\s*|选\s*)?([A-D](?:\s*[,，、;/；|+\s]?\s*[A-D]){0,3})(?:[.。．])?$/i

/** Normalize unique A-D options into option order; duplicates/invalid text fail closed. */
export function normalizeChoiceAnswer(value) {
  const match = CHOICE_ANSWER_RE.exec(String(value ?? '').trim())
  if (!match) return ''
  const letters = match[1].toUpperCase().match(/[A-D]/g) || []
  if (!letters.length || new Set(letters).size !== letters.length) return ''
  return [...letters].sort().join('')
}

/** Normalize an in-progress answer-card edit without invalidating the field mid-entry. */
export function normalizeChoiceInput(value) {
  const letters = String(value ?? '').toUpperCase().match(/[A-D]/g) || []
  return [...new Set(letters)].sort().join('')
}

function fillAnswerSlotCount(question = {}) {
  const existing = toAnswerSlotDrafts(question).length
  return Math.min(20, Math.max(1, Number(question.blank_count) || existing || 1))
}

/** Return fixed-position fill slots so the answer card can edit each blank independently. */
export function templateFillAnswerSlots(question = {}) {
  // Typed positions are authoritative: never infer, pad, truncate or drop an empty drawing slot.
  if (Object.hasOwn(question, 'region_kinds') || Object.hasOwn(question, 'slot_ids')) {
    const raw = question.answer_slots_text ?? question.answer_slots
    if (Array.isArray(raw)) return [...raw]
    return typeof raw === 'string' ? raw.split(/\r?\n/) : []
  }
  const count = fillAnswerSlotCount(question)
  let slots = toAnswerSlotDrafts(question)
  if (!slots.length && count > 1) {
    slots = String(question.standard_answer ?? '').split(/[;；]/).map((item) => item.trim())
  }
  return Array.from({ length: count }, (_, index) => slots[index] || '')
}

/** Update one fill slot and keep the serialized template contract in sync. */
export function setTemplateFillAnswerSlot(question = {}, index, value) {
  const slots = templateFillAnswerSlots(question)
  if (!Number.isInteger(index) || index < 0 || index >= slots.length) return slots
  if (Object.hasOwn(question, 'region_kinds') && question.region_kinds?.[index] !== 'answer') return slots
  slots[index] = String(value ?? '').trim()
  question.answer_slots_text = Object.hasOwn(question, 'region_kinds') || Object.hasOwn(question, 'slot_ids') ? [...slots] : slots.join('\n')
  if (!Array.isArray(question.region_kinds) || !question.region_kinds.includes('drawing')) {
    question.standard_answer = slots.join('; ')
  }
  return slots
}

/** Only an explicit, aligned typed contract may exempt drawing slots from text answers. */
export function templateRegionSlotsValid(question = {}) {
  if (!Object.hasOwn(question, 'region_kinds')) return true
  const count = Number(question.blank_count)
  const kinds = question.region_kinds
  const slots = templateFillAnswerSlots(question)
  return Number.isInteger(count) && count >= 1 && count <= 20
    && Array.isArray(kinds) && kinds.length === count && slots.length === count
    && Array.from(kinds).every((kind, index) => typeof slots[index] === 'string'
      && (kind === 'drawing' ? !slots[index].trim() : kind === 'answer' && Boolean(slots[index].trim())))
}

/** The server is still generating this question's standard answer; it arrives by itself. */
export const templateAnswerPending = (question) => question?.answer_pending === true

/** One sentence naming the questions (entries of { question }) whose standard answer is still being generated. */
export function templatePendingAnswersNotice(entries = []) {
  const numbers = entries.map(({ question }) => String(question?.question_no ?? question?.question_number ?? '')).filter(Boolean)
  if (!numbers.length) return ''
  return `第 ${numbers.join('、')} 题的标准答案正在生成，完成后自动填入；生成完成后才能保存模板。`
}

/** Derive missing text answers from the live draft, preserving empty positions. */
export function templateMissingAnswerSlots(question = {}) {
  // An answer on its way is not one the teacher has to supply.
  if (templateAnswerPending(question) || !Array.isArray(question.slot_ids)) return []
  const answers = templateFillAnswerSlots(question)
  return question.slot_ids.flatMap((slotId, index) => {
    if (question.region_kinds?.[index] === 'drawing') return []
    return typeof answers[index] === 'string' && answers[index].trim() ? [] : [{ slotId, index }]
  })
}

export function templateMissingAnswerNotice(question = {}) {
  const count = templateMissingAnswerSlots(question).length
  if (!count) return ''
  const number = String(question.question_no ?? question.question_number ?? '')
  return `第 ${number} 题有 ${count} 个作答区域未设置标准答案。请核对原题：真实作答区域请补充标准答案，误识别的多余区域请删除，完成后再批阅。`
}

/** Explicit Max extra/refused reports that still block saving. */
export function templateMaxFlagEntries(question = {}) {
  const flags = Array.isArray(question.max_flags) ? question.max_flags : []
  const kept = new Set(Array.isArray(question.max_flags_kept) ? question.max_flags_kept : [])
  const slots = new Set(Array.isArray(question.slot_ids) ? question.slot_ids : [])
  // Deleting a reported position resolves its flag; keeping it is explicit.
  return flags.filter((flag) => (!flag?.slot_id || slots.has(flag.slot_id)) && !kept.has(flag?.slot_id || '*'))
}

export function templateMaxFlagNotice(question = {}) {
  const flags = templateMaxFlagEntries(question)
  if (!flags.length) return ''
  const number = String(question.question_no ?? question.question_number ?? '')
  // Locator and solver may report the same position; say it once.
  const items = [...new Set(flags.map((flag) => `${flag.slot_id ? `作答位 Q${flag.slot_id}` : '整题'}${flag.status === 'extra' ? '为目录多余项' : '无法按目录作答'}${flag.reason ? `（${flag.reason}）` : ''}`))]
  return `第 ${number} 题：Max 判定${items.join('；')}。请核对原题：确为多余请删除；确需保留请点“确认保留”，并补齐答案和作答框后再保存。`
}

/** Teacher keeps every currently reported position of this question. */
export function keepTemplateMaxFlags(question = {}) {
  const keys = templateMaxFlagEntries(question).map((flag) => flag.slot_id || '*')
  question.max_flags_kept = [...new Set([...(Array.isArray(question.max_flags_kept) ? question.max_flags_kept : []), ...keys])]
  return keys.length
}

export function templateIsPureDrawing(question = {}) {
  if (Array.isArray(question.region_kinds) && question.region_kinds.length) {
    return question.region_kinds.every((kind) => kind === 'drawing')
  }
  return question.answer_kind === 'drawing'
}

export function choiceContractValid(question = {}) {
  if (question.question_type !== 'choice' || templateIsPureDrawing(question)) return true
  const answer = normalizeChoiceAnswer(question.standard_answer ?? question.answer)
  if (!answer) return false
  if (Object.hasOwn(question, 'slot_ids') && !question.region_kinds?.includes('drawing')) {
    const slots = templateFillAnswerSlots(question)
    if (question.slot_ids?.length !== 1 || slots.length !== 1 || normalizeChoiceAnswer(slots[0]) !== answer) return false
  }
  if (question.choice_mode === 'single') return answer.length === 1
  if (question.choice_mode === 'multiple') return answer.length >= 2
  return false
}

/** Entries safe to show and edit in the compact A/B/C/D answer card. */
export function templateChoiceReviewEntries(pages = []) {
  return templateReviewEntries(pages, 'choice')
    .filter(({ question }) => choiceContractValid(question))
}

/** OCR says choice, but its answer violates the choice-answer contract. */
/** Say exactly what keeps a choice question unresolved and what the teacher can do. */
export function templateChoiceConflictReason(question = {}) {
  const number = String(question.question_no ?? question.question_number ?? '')
  const count = Array.isArray(question.slot_ids) ? question.slot_ids.length : 0
  if (count > 1 && !question.region_kinds?.includes('drawing')) {
    return `Q${number} 是选择题，但有 ${count} 个作答框：请只保留 1 个（删除多余作答框），或改回原题型`
  }
  if (!['single', 'multiple'].includes(question.choice_mode)) return `Q${number} 请在选择题答题卡中选择“单选”或“多选”`
  return `Q${number} 请在选择题答题卡中点选标准答案（${question.choice_mode === 'multiple' ? '至少两个选项' : '一个选项'}）`
}

/** Every reason one choice question is unresolved, so the teacher fixes them in one pass. */
export function templateChoiceConflictReasons(question = {}) {
  if (question.question_type !== 'choice' || choiceContractValid(question)) return []
  const number = String(question.question_no ?? question.question_number ?? '')
  const count = Array.isArray(question.slot_ids) ? question.slot_ids.length : 0
  const reasons = []
  if (count > 1 && !question.region_kinds?.includes('drawing')) {
    reasons.push(`Q${number} 是选择题，但有 ${count} 个作答框：请只保留 1 个（删除多余作答框），或改回原题型`)
  }
  const modeSet = ['single', 'multiple'].includes(question.choice_mode)
  if (!modeSet) reasons.push(`Q${number} 请在选择题答题卡中选择“单选”或“多选”`)
  if (!normalizeChoiceAnswer(question.standard_answer ?? question.answer) || !reasons.length) {
    reasons.push(`Q${number} 请在选择题答题卡中点选标准答案（${question.choice_mode === 'multiple' ? '至少两个选项' : '一个选项'}）`)
  }
  return reasons
}

export function templateChoiceConflictEntries(pages = []) {
  return templateReviewEntries(pages, 'choice')
    .filter(({ question }) => !choiceContractValid(question))
}

/** Return one stable, page-aware entry per question of the requested type. */
export function templateReviewEntries(pages = [], type) {
  return pages.flatMap((page, pageIndex) => (page.questions || [])
    .map((question, questionIndex) => ({ pageIndex, questionIndex, question }))
    .filter((entry) => entry.question.question_type === type))
}

/** Render a compact answer-card value while preserving all detailed inputs elsewhere. */
export function templateAnswerText(question = {}) {
  const slots = toAnswerSlots(question)
  if (question.question_type === 'fill' && slots.length) return slots.join('、')
  return String(question.standard_answer ?? question.answer ?? '').trim() || '—'
}

/**
 * Apply an exam score to one question type only.  Custom multi-blank scoring is
 * intentionally excluded because its per-blank numbers are a teacher decision.
 */
export function applyUniformTemplateScore(pages = [], type, score) {
  const numericScore = Number(score)
  if (!Number.isFinite(numericScore) || numericScore <= 0) {
    throw new RangeError('统一分值必须大于 0')
  }

  let affected = 0
  for (const page of pages) {
    for (const question of page.questions || []) {
      const isCustomMultiBlank = type === 'fill'
        && Number(question.blank_count) > 1
        && question.fill_scoring_mode === 'custom'
      const isInvalidChoice = type === 'choice'
        && !choiceContractValid(question)
      if (question.question_type !== type || isCustomMultiBlank || isInvalidChoice) continue
      question.max_score = numericScore
      if (type === 'fill') question.blank_scores = []
      affected += 1
    }
  }
  return affected
}
