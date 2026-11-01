import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Regression: a page saved by the teacher must clear its model review markers
// on the client exactly like the layout-overrides endpoint does on the server.
// Directory-stage diagnostics carry `review_reasons` lists and an explicit
// `geometry_review_pending` flag; the older resolver only understood
// low-confidence and semantic reasons, so the confirmation gate could keep
// re-blocking "保存模板（全部页）" with a review the server no longer held.

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

function functionCode(name) {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))
  assert.ok(match, `${name} must be present in Upload.vue`)
  return match[0]
}

function constantCode(pattern, label) {
  const match = source.match(pattern)
  assert.ok(match, `${label} must be present in Upload.vue`)
  return match[0]
}

const regionEntries = (raw) => (Array.isArray(raw) ? raw.flatMap((item) => {
  if (!item || typeof item !== 'object') return []
  if (Array.isArray(item.regions)) {
    return item.regions.map((region) => ({ item, region: Array.isArray(region) ? null : region, box: region }))
  }
  return [{ item, region: item, box: item.bbox }]
}) : [])

const resolverSandbox = {
  templateRegionEntries: regionEntries,
}
vm.createContext(resolverSandbox)
vm.runInContext([
  constantCode(/const TEMPLATE_GEOMETRY_CONFIDENCE_THRESHOLD = [\d.]+/, 'TEMPLATE_GEOMETRY_CONFIDENCE_THRESHOLD'),
  constantCode(/const TEMPLATE_SEMANTIC_REVIEW_REASONS = new Set\(\[[^]*?\]\)/, 'TEMPLATE_SEMANTIC_REVIEW_REASONS'),
  functionCode('templateHumanConfirmed'),
  functionCode('templateConfidenceRisk'),
  functionCode('templateDiagnosticIsLowConfidence'),
  functionCode('templateDiagnosticHasPendingGeometryReview'),
  functionCode('templateDiagnosticReasonText'),
  functionCode('templateGeometryWasTeacherConfirmed'),
  functionCode('templateResolveLowConfidenceDiagnostic'),
].join('\n'), resolverSandbox)

const confirmedAnchors = () => [{ question_number: '1', human_confirmed: true }]
const confirmedHandwriting = () => [{ question_number: '1', regions: [[10, 20, 30, 40]], human_confirmed: true }]
const directoryDiagnostic = () => ({
  needs_review: true,
  geometry_review_pending: true,
  review_reasons: ['部分 Q 标签无法放入页面，请教师调整定位'],
  model_stages: { 'template-directory-v1': { status: 'completed' } },
})

test('a saved page resolves directory-stage geometry diagnostics instead of blocking forever', () => {
  const resolved = resolverSandbox.templateResolveLowConfidenceDiagnostic(
    directoryDiagnostic(), confirmedAnchors(), confirmedHandwriting(),
  )
  assert.equal(resolved.needs_review, false)
  assert.equal(resolved.geometry_review_pending, false)
  assert.equal(resolved.review_reason, 'teacher_confirmed')
  assert.equal(resolved.resolved_by, 'teacher')
  assert.equal(resolved.resolved_from, '部分 Q 标签无法放入页面，请教师调整定位')
  assert.equal(resolverSandbox.templateDiagnosticHasPendingGeometryReview(resolved), false)
})

test('an unconfirmed page keeps its pending geometry review fail-closed', () => {
  const diagnostic = directoryDiagnostic()
  const unresolved = resolverSandbox.templateResolveLowConfidenceDiagnostic(diagnostic, [], [])
  assert.equal(unresolved, diagnostic)
  assert.equal(resolverSandbox.templateDiagnosticHasPendingGeometryReview(unresolved), true)
  const partiallyConfirmed = resolverSandbox.templateResolveLowConfidenceDiagnostic(
    diagnostic, confirmedAnchors(), [{ question_number: '1', regions: [[10, 20, 30, 40]] }],
  )
  assert.equal(partiallyConfirmed, diagnostic)
})

test('one page save also resolves nested image association markers and their unsaved-page predicate', () => {
  for (const image_association of [{ needs_review: true }, { status: 'needs_review' }]) {
    const diagnostic = { needs_review: false, geometry_review_pending: false, image_association }
    assert.equal(resolverSandbox.templateDiagnosticHasPendingGeometryReview(diagnostic), true)
    const resolved = resolverSandbox.templateResolveLowConfidenceDiagnostic(diagnostic, confirmedAnchors(), confirmedHandwriting())
    assert.equal(resolved.image_association.needs_review, false)
    assert.equal(resolved.image_association.status, 'teacher_confirmed')
    assert.equal(resolverSandbox.templateDiagnosticHasPendingGeometryReview(resolved), false)
    assert.equal(resolverSandbox.templateResolveLowConfidenceDiagnostic(diagnostic, [], []), diagnostic)
  }
})

test('legacy diagnostics without an explicit geometry result resolve after the teacher save', () => {
  const resolved = resolverSandbox.templateResolveLowConfidenceDiagnostic(
    { needs_review: true, review_reasons: ['模型未能把全部题目逐一绑定目录，请教师核对标准答案'] },
    confirmedAnchors(), confirmedHandwriting(),
  )
  assert.equal(resolved.needs_review, false)
  assert.equal(resolved.resolved_from, '模型未能把全部题目逐一绑定目录，请教师核对标准答案')
  assert.equal(resolverSandbox.templateDiagnosticHasPendingGeometryReview(resolved), false)
})

test('an explicit non-geometry review result is neither blocking nor rewritten', () => {
  const advisory = {
    needs_review: true,
    geometry_review_pending: false,
    review_reasons: ['作图题人工作图由教师逐题生成：确认题干与标准答案后，在该题点击“作图”生成候选'],
  }
  const confirmed = resolverSandbox.templateResolveLowConfidenceDiagnostic(advisory, confirmedAnchors(), confirmedHandwriting())
  const unconfirmed = resolverSandbox.templateResolveLowConfidenceDiagnostic(advisory, [], [])
  assert.equal(confirmed, advisory)
  assert.equal(unconfirmed, advisory)
  assert.equal(resolverSandbox.templateDiagnosticHasPendingGeometryReview(advisory), false)
})

test('low-confidence diagnostics keep resolving exactly as before', () => {
  const resolved = resolverSandbox.templateResolveLowConfidenceDiagnostic(
    { needs_review: true, review_reason: 'low_confidence' }, confirmedAnchors(), confirmedHandwriting(),
  )
  assert.equal(resolved.needs_review, false)
  assert.equal(resolved.resolved_from, 'low_confidence')
})

test('resolving the page diagnostics opens the confirmation gate in the same state', () => {
  const reviewStart = source.indexOf('function templateGeometryReviewState()')
  const reviewEnd = source.indexOf('\nconst templateGeometryReview =', reviewStart)
  assert.ok(reviewStart >= 0 && reviewEnd > reviewStart)
  const helperSource = ['templateDiagnosticHasPendingGeometryReview', 'templateDiagnosticReasonText']
    .map((name) => functionCode(name)).join('\n')
  const boxValidator = new Function(`${functionCode('templateBBoxIsValid')}; return templateBBoxIsValid`)()
  const page = {
    pageIndex: 0,
    questions: [{ question_no: '1' }],
    anchors: confirmedAnchors(),
    handwriting: confirmedHandwriting(),
    columns: [{ x1_1000: 0, x2_1000: 1000 }],
    columnsSource: 'teacher', columnsConfirmed: true,
    diagnostics: [directoryDiagnostic()],
  }
  const run = new Function(
    'templateVisualEvidencePages', 'templateQuestionNumber', 'templateReviewPendingPageIndexes',
    'templateColumnMode', 'templateBBoxIsValid', 'templateConfidenceRisk', 'templateRegionEntries',
    'templateCutResult', 'templateCutPreviewPages',
    `${helperSource}; ${source.slice(reviewStart, reviewEnd)}; return templateGeometryReviewState`,
  )(
    () => [page], String, () => new Set(), { value: 'single' }, boxValidator,
    resolverSandbox.templateConfidenceRisk, regionEntries, { value: {} }, { value: [{}] },
  )
  const blocking = run()
  assert.equal(blocking.needsReview, true)
  assert.deepEqual(blocking.diagnosticReasons, ['部分 Q 标签无法放入页面，请教师调整定位'])
  assert.deepEqual(blocking.diagnosticReviewDetails, ['第1页：部分 Q 标签无法放入页面，请教师调整定位'])
  assert.match(blocking.summary, /模型诊断待复核（第1页：部分 Q 标签无法放入页面/)
  page.diagnostics[0] = resolverSandbox.templateResolveLowConfidenceDiagnostic(
    page.diagnostics[0], page.anchors, page.handwriting,
  )
  const cleared = run()
  assert.equal(cleared.needsReview, false)
  assert.deepEqual(cleared.diagnosticReasons, [])
  assert.deepEqual(cleared.diagnosticReviewDetails, [])
})

test('a page with a pending review diagnostic counts as an unsaved teacher decision', () => {
  const body = functionCode('templatePageNeedsSave')
  assert.match(body, /templateDiagnosticPendingPageIndexes\(\)\.has\(Number\(page\.pageIndex\)\)/)
})

test('unfinished templates stay recoverable until confirmed, cancelled or frozen', () => {
  const applyBody = functionCode('applyTemplateRecognitionResult')
  assert.doesNotMatch(applyBody, /forgetTemplateRecognition/)
  const confirmBody = functionCode('saveAndConfirmTemplateDraft')
  assert.match(confirmBody, /forgetTemplateRecognition\(\)/)
  const resumeBody = functionCode('resumeTemplateRecognitionCheckpoint')
  assert.match(resumeBody, /=== 'frozen'/)
  assert.match(resumeBody, /forgetTemplateRecognition\(\)/)
})

test('the blocking message names the page and the concrete model reason', () => {
  assert.match(source, /模型定位或逐图关联仍需人工复核，请核对后重新保存模板\$\{detail \?/)
  assert.match(source, /diagnosticReviewDetails/)
})
