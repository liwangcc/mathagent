// Template geometry as the template editor on the grading page (views/Upload.vue)
// reviews and saves it: model-confidence and review diagnostics, teacher
// confirmation and provenance, page-normalised boxes, question anchors and the
// answer / handwriting region lists sent back to the server.
import { templateSlotBoxes } from './templateSlots.js'

// Template geometry is a separate confirmation contract from the answer text.
// Keep the client-side gate fail-closed when a server result carries a review
// marker, while accepting old artifacts that predate confidence metadata.
export const TEMPLATE_GEOMETRY_CONFIDENCE_THRESHOLD = 0.75

export const TEMPLATE_PROVENANCE_KEYS = [
  'geometry_kind',
  'confidence',
  'human_confirmed',
  'confidence_source',
  'column_id',
  'column_index',
  'column_order',
  'layout_revision',
  'image_index',
  'coordinate_trust',
  'source',
  'provenance',
]

export function templatePageIndex(page, fallback = 0) {
  const value = Number(page?.page_index ?? page?.pageIndex ?? fallback)
  return Number.isInteger(value) && value >= 0 ? value : fallback
}

export function templateQuestionNumber(value) {
  return String(value ?? '').trim()
}

export function templateHumanConfirmed(item) {
  if (!item || typeof item !== 'object') return false
  if (item.human_confirmed === true || item.teacher_confirmed === true) return true
  if (String(item.confidence_source || '').trim().toLowerCase() === 'teacher') return true
  if (typeof item.provenance === 'string' && item.provenance.trim().toLowerCase() === 'teacher') return true
  if (item.provenance && typeof item.provenance === 'object') {
    return String(item.provenance.source || item.provenance.kind || '').trim().toLowerCase() === 'teacher'
  }
  return false
}

export function templateConfidenceRisk(item, threshold = TEMPLATE_GEOMETRY_CONFIDENCE_THRESHOLD) {
  if (!item || typeof item !== 'object' || templateHumanConfirmed(item)) return false
  if (!Object.prototype.hasOwnProperty.call(item, 'confidence')) return false
  const confidence = Number(item.confidence)
  return !Number.isFinite(confidence) || confidence < 0 || confidence > 1 || confidence < threshold
}

export function templateDiagnosticIsLowConfidence(diagnostic) {
  if (!diagnostic || typeof diagnostic !== 'object') return false
  const reason = String(
    diagnostic.review_reason || diagnostic.reviewReason || diagnostic.reason || '',
  ).trim().toLowerCase()
  if (reason === 'low_confidence' || reason.includes('low_confidence')) return true
  const confidence = diagnostic.confidence
  return Boolean(confidence && typeof confidence === 'object' && (
    Array.isArray(confidence.low_confidence_question_anchors)
    || Array.isArray(confidence.low_confidence_handwriting_regions)
  ))
}

export function templateDiagnosticHasPendingGeometryReview(diagnostic) {
  if (!diagnostic || typeof diagnostic !== 'object') return false
  if (diagnostic.image_association?.needs_review === true || diagnostic.image_association?.status === 'needs_review') return true
  if (diagnostic.geometry_review_pending === true) return true
  // An explicit geometry result — including an explicit "no geometry issue" —
  // wins over the generic review flag.  Legacy diagnostics without it stay
  // fail-closed until the teacher saves the page.
  if (diagnostic.geometry_review_pending === false) return false
  return diagnostic.needs_review === true || diagnostic.needsReview === true
}

export function templateDiagnosticReasonText(diagnostic) {
  const single = diagnostic?.review_reason || diagnostic?.reviewReason || diagnostic?.reason
  if (String(single || '').trim()) return String(single).trim()
  const listed = Array.isArray(diagnostic?.review_reasons)
    ? diagnostic.review_reasons.map((item) => String(item || '').trim()).filter(Boolean)
    : []
  return listed[0] || 'needs_review'
}

export const TEMPLATE_SEMANTIC_REVIEW_REASONS = new Set([
  'column_identity_conflict',
  'image_association_conflict',
  'image_association',
])

export const TEMPLATE_REVIEW_PENDING_PAGE_KEYS = [
  'layout_review_pending_pages',
  'anchor_review_pending_pages',
  'geometry_review_pending_pages',
]

export function templatePageReviewPending(page, questionMode = page?.modes?.question || {}) {
  return page?.layout_review_pending === true
    || page?.anchor_review_pending === true
    || page?.geometry_review_pending === true
    || questionMode?.layout_review_pending === true
    || questionMode?.anchor_review_pending === true
    || questionMode?.geometry_review_pending === true
    || questionMode?.layout_status === 'needs_review'
    || questionMode?.anchor_status === 'needs_review'
}

export function templateGeometryWasTeacherConfirmed(anchors, handwriting) {
  const anchorItems = Array.isArray(anchors)
    ? anchors.filter((item) => item && typeof item === 'object')
    : []
  const regionItems = templateRegionEntries(handwriting)
    .map(({ item, region }) => ({ ...item, ...(region || {}) }))
    .filter((item) => item && typeof item === 'object')
  const geometryItems = [...anchorItems, ...regionItems]
  return geometryItems.length > 0 && geometryItems.every(templateHumanConfirmed)
}

export function templateResolveLowConfidenceDiagnostic(diagnostic, anchors, handwriting) {
  const reason = String(
    diagnostic?.review_reason || diagnostic?.reviewReason || diagnostic?.reason || '',
  ).trim().toLowerCase()
  const isSemanticReview = TEMPLATE_SEMANTIC_REVIEW_REASONS.has(reason)
  const pendingGeometryReview = templateDiagnosticHasPendingGeometryReview(diagnostic)
  if (!isSemanticReview && !pendingGeometryReview && !templateDiagnosticIsLowConfidence(diagnostic)) return diagnostic
  if (!templateGeometryWasTeacherConfirmed(anchors, handwriting)) return diagnostic
  const resolvedFrom = String(
    diagnostic.review_reason || diagnostic.reviewReason
    || (Array.isArray(diagnostic.review_reasons) ? diagnostic.review_reasons[0] : '')
    || (isSemanticReview ? 'semantic_review' : pendingGeometryReview ? 'model_geometry_review' : 'low_confidence'),
  )
  // Saving the page is the teacher's explicit acknowledgement of every visible
  // geometry item and the server clears the identical review markers on that
  // save.  Keep both layers in the same state so the confirmation gate cannot
  // keep re-blocking with a review the server no longer holds.
  return {
    ...diagnostic,
    needs_review: false,
    needsReview: false,
    geometry_review_pending: false,
    ...(diagnostic.image_association ? { image_association: {
      ...diagnostic.image_association, needs_review: false, status: 'teacher_confirmed',
    } } : {}),
    review_reason: 'teacher_confirmed',
    resolved_from: resolvedFrom,
    resolved_by: 'teacher',
  }
}

export function templateResolveDiagnosticField(target, key, anchors, handwriting) {
  if (!target || typeof target !== 'object' || !target[key]) return
  target[key] = templateResolveLowConfidenceDiagnostic(target[key], anchors, handwriting)
}

export function templateBBoxIsValid(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return false
  const [left, top, right, bottom] = bbox.map(Number)
  return [left, top, right, bottom].every(Number.isFinite)
    && 0 <= left && left < right && right <= 1000
    && 0 <= top && top < bottom && bottom <= 1000
}

export function templatePreserveProvenance(source, target = {}) {
  if (!source || typeof source !== 'object') return target
  TEMPLATE_PROVENANCE_KEYS.forEach((key) => {
    if (Object.prototype.hasOwnProperty.call(source, key)) target[key] = source[key]
  })
  return target
}

export function templateRegionEntries(rawRegions) {
  if (!Array.isArray(rawRegions)) return []
  const entries = []
  rawRegions.forEach((item) => {
    if (!item || typeof item !== 'object') {
      entries.push({ item: {}, box: null })
      return
    }
    if (Array.isArray(item.regions)) {
      item.regions.forEach((region) => {
        const box = normalizeTemplateBBoxForPage(
          Array.isArray(region) ? region : region?.bbox,
        )
        const entry = {
          item,
          box,
          region: region && typeof region === 'object' && !Array.isArray(region) ? region : null,
        }
        entries.push(entry)
      })
      return
    }
    entries.push({ item, box: normalizeTemplateBBoxForPage(item.bbox), region: item })
  })
  return entries
}

export function templateBoxStyle(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return {}
  return {
    left: `${bbox[0] / 10}%`,
    top: `${bbox[1] / 10}%`,
    width: `${(bbox[2] - bbox[0]) / 10}%`,
    height: `${(bbox[3] - bbox[1]) / 10}%`,
  }
}

export function normalizeTemplateBBoxForPage(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return []
  const values = bbox.map(Number)
  if (!values.every(Number.isFinite)) return []
  const left = Math.max(0, Math.min(1000, values[0]))
  const top = Math.max(0, Math.min(1000, values[1]))
  const right = Math.max(0, Math.min(1000, values[2]))
  const bottom = Math.max(0, Math.min(1000, values[3]))
  if (!(left < right && top < bottom)) return []
  return [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)]
}

export function cloneTemplateColumns(columns) {
  return (columns || []).map((column, index) => ({
    ...column,
    column_index: index + 1,
    x1_1000: Number(column.x1_1000),
    x2_1000: Number(column.x2_1000),
  }))
}

export function markTemplateGeometryConfirmed(item) {
  if (!item || typeof item !== 'object') return item
  item.human_confirmed = true
  item.confidence_source = 'teacher'
  item.confidence = 1
  return item
}

export function cloneQuestionAnchors(anchors, { teacherConfirmed = false } = {}) {
  return (anchors || []).map((anchor) => {
    const cloned = templatePreserveProvenance(anchor, {
      ...anchor,
      question_number: String(anchor?.question_number ?? anchor?.question_no ?? ''),
    })
    // Older provider payloads called this box `bbox` or `question_bbox`.
    // Normalize it at the UI boundary so the teacher can see and adjust it;
    // the canonical server field remains `question_number_bbox`.
    if (!Array.isArray(cloned.question_number_bbox)) {
      const legacyBox = cloned.question_bbox ?? cloned.bbox
      const normalizedBox = normalizeTemplateBBoxForPage(legacyBox)
      if (normalizedBox.length === 4) cloned.question_number_bbox = normalizedBox
    } else {
      cloned.question_number_bbox = normalizeTemplateBBoxForPage(cloned.question_number_bbox)
    }
    if (teacherConfirmed) markTemplateGeometryConfirmed(cloned)
    return cloned
  })
}

export function templateQuestionAnchorBBox(anchor) {
  if (!anchor || typeof anchor !== 'object') return []
  return normalizeTemplateBBoxForPage(
    anchor.question_number_bbox ?? anchor.question_bbox ?? anchor.bbox,
  )
}

export function templateVisibleAnswerBoxes(page) {
  // Legacy exemption rows may repeat drawings. A pure graphic has only its
  // drawing box; a mixed item keeps separate text boxes beside its drawing.
  return page.answerBoxes.filter((box) => {
    if (box.bbox?.length !== 4) return false
    const question = (page.questionSchema || []).find((item) => String(item.question_number ?? item.question_no) === String(box.question_number))
    if (question?.answer_kind === 'drawing') return false
    if (question?.answer_kind !== 'mixed') return true
    return box.geometry_kind !== 'label_anchor' && !(page.drawingBoxes || []).some((drawing) =>
      String(drawing.question_number) === String(box.question_number)
      && drawing.bbox.every((value, index) => Number(value) === Number(box.bbox[index])))
  })
}

export function flattenTemplateHandwritingRegions(regions) {
  return (regions || []).flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    // Model/provider responses have used both the canonical `regions` list
    // and the older answer-area aliases (`answer_regions`, `answer_bbox`,
    // `bbox`).  Keep the visual review tolerant of all of them.  The server
    // normalizes new artifacts, but the teacher must still be able to review
    // an in-flight candidate produced by an older route.
    const rawRegions = Array.isArray(item.regions)
      ? item.regions
      : Array.isArray(item.answer_regions)
        ? item.answer_regions
        : (item.answer_bbox ?? item.student_answer_bbox ?? item.bbox) != null
          ? [item.answer_bbox ?? item.student_answer_bbox ?? item.bbox]
          : []
    if (rawRegions.length) {
      return rawRegions.flatMap((region, regionIndex) => {
        const source = region && typeof region === 'object' && !Array.isArray(region)
          ? region
          : {}
        const box = normalizeTemplateBBoxForPage(
          Array.isArray(region)
            ? region
            : (region?.bbox ?? region?.answer_bbox ?? region?.student_answer_bbox),
        )
        if (box.length !== 4) return []
        const flattened = templatePreserveProvenance(item, templatePreserveProvenance(source, {
          ...source,
          question_number: String(item.question_number ?? item.question_no ?? ''),
          ...(Array.isArray(item.slot_ids) ? { slot_id: item.slot_ids[regionIndex] } : {}),
          bbox: [...box],
        }))
        return [flattened]
      })
    }
    return []
  })
}

// 作答框是模板几何的唯一来源。旧模板仍可能只保存
// template_handwriting_regions，因此在进入编辑器时把旧字段作为兼容回退，
// 并按题号和坐标去重，避免同一位置出现两套重叠框。
export function mergeTemplateAnswerRegionSources(answerRegions, legacyHandwritingRegions) {
  const merged = []
  const seen = new Set()
  const primary = flattenTemplateHandwritingRegions(answerRegions)
  const contractedNumbers = new Set(primary.filter(box => box.slot_id).map(box => box.question_number))
  for (const box of [
    ...primary,
    ...flattenTemplateHandwritingRegions(legacyHandwritingRegions).filter(box => !contractedNumbers.has(box.question_number)),
  ]) {
    const number = String(box.question_number ?? '')
    const bbox = normalizeTemplateBBoxForPage(box.bbox)
    if (!number || bbox.length !== 4) continue
    const key = `${number}:${box.slot_id || bbox.join(',')}`
    if (seen.has(key)) continue
    seen.add(key)
    merged.push({ ...box, question_number: number, bbox })
  }
  return merged
}

export function templateRegionCandidateUsable(regions) {
  return Array.isArray(regions) && flattenTemplateHandwritingRegions(regions).length > 0
}

export function serializeTemplateHandwritingRegions(boxes, anchors, { teacherConfirmed = false } = {}) {
  const grouped = new Map((anchors || []).map((anchor) => [
    String(anchor.question_number),
    { regions: [], slot_ids: [], provenance: {} },
  ]))
  for (const box of boxes || []) {
    const number = String(box.question_number)
    if (!grouped.has(number)) continue
    const target = grouped.get(number)
    const normalizedBox = normalizeTemplateBBoxForPage(box.bbox)
    if (normalizedBox.length === 4) {
      target.regions.push(normalizedBox)
      target.slot_ids.push(box.slot_id)
    }
    TEMPLATE_PROVENANCE_KEYS.forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(box, key) && target.provenance[key] === undefined) {
        target.provenance[key] = box[key]
      }
    })
    if (teacherConfirmed) target.provenance = {
      ...target.provenance,
      human_confirmed: true,
      confidence_source: 'teacher',
      confidence: 1,
    }
  }
  return Array.from(grouped, ([question_number, target]) => {
    const result = { question_number, regions: target.regions }
    if (target.slot_ids.some(Boolean)) {
      if (!target.slot_ids.every(Boolean) || new Set(target.slot_ids).size !== target.slot_ids.length) throw new Error(`Q${question_number} 作答框槽编号缺失或重复`)
      result.slot_ids = [...target.slot_ids]
    }
    return templatePreserveProvenance(target.provenance, result)
  })
}

export function equalTemplateColumns(count) {
  return Array.from({ length: count }, (_, index) => ({
    column_index: index + 1,
    x1_1000: Math.round(index * 1000 / count),
    x2_1000: Math.round((index + 1) * 1000 / count),
  }))
}

// Exactly the boxes one page save sends: text boxes (visible ones without a
// slot, and every slot with drawing slots resolved to their drawing area) and
// the drawing areas without a slot.
export function templatePageSaveBoxes(page) {
  const textBoxes = [...templateVisibleAnswerBoxes(page).filter(box => !box.slot_id), ...templateSlotBoxes(page)]
  return { textBoxes, drawingBoxes: page.drawingBoxes.filter(box => !box.slot_id) }
}
