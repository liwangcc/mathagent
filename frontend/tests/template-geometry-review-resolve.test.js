import assert from 'node:assert/strict'
import test from 'node:test'
import { anchor, CHECKPOINT_KEY, cutPage, openTemplateEditor, question, regions, ROUTES } from './helpers/templateCrop.js'
import { templateDiagnosticHasPendingGeometryReview, templateResolveLowConfidenceDiagnostic } from '../src/utils/templateGeometry.js'

// Regression: a page saved by the teacher must clear its model review markers
// on the client exactly like the layout-overrides endpoint does on the server.
// Directory-stage diagnostics carry `review_reasons` lists and an explicit
// `geometry_review_pending` flag; the older resolver only understood
// low-confidence and semantic reasons, so the confirmation gate could keep
// re-blocking "保存模板（全部页）" with a review the server no longer held.
//
// The real 批改 page (Upload.vue) runs every case: the teacher opens the
// template editor with a diagnostic on the page and saves it; where no button
// reaches a case (geometry the teacher has only partly confirmed), the page's
// resolver itself is called (utils/templateGeometry.js, which the page uses).

const REASON = '部分 Q 标签无法放入页面，请教师调整定位'
const confirmedAnchors = () => [{ question_number: '1', human_confirmed: true }]
const confirmedHandwriting = () => [{ question_number: '1', regions: [[10, 20, 30, 40]], human_confirmed: true }]
const directoryDiagnostic = () => ({
  needs_review: true,
  geometry_review_pending: true,
  review_reasons: [REASON],
  model_stages: { 'template-directory-v1': { status: 'completed' } },
})

// One page with a question-number box and an answer box, carrying `diagnostic`
// as the model's page diagnostic.
function editorWith(t, diagnostic, fields = {}) {
  return openTemplateEditor(t, {
    cut: { pages: [cutPage(0, {
      question_anchors: [anchor('1', [10, 10, 40, 30])],
      template_answer_regions: [regions('1', [[100, 100, 300, 200]])],
      anchor_diagnostics: diagnostic,
      ...fields,
    })] },
  })
}

const storedDiagnostic = editor => editor.page.setup.templateCutResult.pages[0].anchor_diagnostics

test('a saved page resolves directory-stage geometry diagnostics instead of blocking forever', async (t) => {
  const editor = await editorWith(t, directoryDiagnostic())
  assert.match(editor.alert(), /模型诊断待复核/)

  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(body => body.page_index), [0])
  const resolved = storedDiagnostic(editor)
  assert.equal(resolved.needs_review, false)
  assert.equal(resolved.geometry_review_pending, false)
  assert.equal(resolved.review_reason, 'teacher_confirmed')
  assert.equal(resolved.resolved_by, 'teacher')
  assert.equal(resolved.resolved_from, REASON)
  assert.equal(templateDiagnosticHasPendingGeometryReview(resolved), false)
  assert.equal(editor.preview().anchorDiagnostics.resolved_by, 'teacher', 'the editor page holds the same resolution')
  // No review left: the template is confirmed in the same click.
  assert.equal(editor.confirms().length, 1)
})

test('an unconfirmed page keeps its pending geometry review fail-closed', async (t) => {
  // A page whose saved geometry is empty cannot have been confirmed by the teacher.
  const diagnostic = directoryDiagnostic()
  const editor = await editorWith(t, diagnostic, { question_anchors: [], template_answer_regions: [] })
  const before = storedDiagnostic(editor)
  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(body => body.page_index), [0], 'the page is saved')
  assert.equal(storedDiagnostic(editor), before, 'its diagnostic stays as it was')
  assert.equal(templateDiagnosticHasPendingGeometryReview(storedDiagnostic(editor)), true)
  assert.ok(editor.warnings().some(text => text.includes('模型定位或逐图关联仍需人工复核')))
  assert.equal(editor.confirms().length, 0)

  // Partly confirmed geometry (confirmed anchors, an unconfirmed region) leaves it untouched too.
  const resolve = templateResolveLowConfidenceDiagnostic
  assert.equal(resolve(diagnostic, [], []), diagnostic)
  assert.equal(resolve(diagnostic, confirmedAnchors(), [{ question_number: '1', regions: [[10, 20, 30, 40]] }]), diagnostic)
  assert.notEqual(resolve(diagnostic, confirmedAnchors(), confirmedHandwriting()), diagnostic)
})

test('one page save also resolves nested image association markers and their unsaved-page predicate', async (t) => {
  for (const image_association of [{ needs_review: true }, { status: 'needs_review' }]) {
    const diagnostic = { needs_review: false, geometry_review_pending: false, image_association }
    const editor = await editorWith(t, diagnostic)
    // A pending association is an unsaved teacher decision and holds the gate.
    assert.equal(templateDiagnosticHasPendingGeometryReview(diagnostic), true)
    assert.match(editor.card().text, /请核对本页定位/)
    assert.match(editor.alert(), /模型诊断待复核（.*第1页：image_association/)
    assert.equal(templateResolveLowConfidenceDiagnostic(diagnostic, [], []), diagnostic)

    await editor.save()
    const resolved = storedDiagnostic(editor)
    assert.equal(resolved.image_association.needs_review, false)
    assert.equal(resolved.image_association.status, 'teacher_confirmed')
    assert.equal(templateDiagnosticHasPendingGeometryReview(resolved), false)
    assert.equal(editor.confirms().length, 1)
    editor.unmount()
  }
})

test('legacy diagnostics without an explicit geometry result resolve after the teacher save', async (t) => {
  const reason = '模型未能把全部题目逐一绑定目录，请教师核对标准答案'
  const editor = await editorWith(t, { needs_review: true, review_reasons: [reason] })
  assert.match(editor.alert(), /模型诊断待复核/, 'without a geometry result it blocks')
  await editor.save()
  const resolved = storedDiagnostic(editor)
  assert.equal(resolved.needs_review, false)
  assert.equal(resolved.resolved_from, reason)
  assert.equal(templateDiagnosticHasPendingGeometryReview(resolved), false)
  assert.equal(editor.confirms().length, 1)
})

test('an explicit non-geometry review result is neither blocking nor rewritten', async (t) => {
  const advisory = {
    needs_review: true,
    geometry_review_pending: false,
    review_reasons: ['作图题人工作图由教师逐题生成：确认题干与标准答案后，在该题点击“作图”生成候选'],
  }
  const editor = await editorWith(t, advisory)
  assert.equal(editor.alert(), '', 'an advisory note does not hold the geometry gate')
  assert.doesNotMatch(editor.card().text, /请核对本页定位/)
  const pending = templateDiagnosticHasPendingGeometryReview
  const resolve = templateResolveLowConfidenceDiagnostic
  assert.equal(pending(advisory), false)
  assert.equal(resolve(advisory, confirmedAnchors(), confirmedHandwriting()), advisory)
  assert.equal(resolve(advisory, [], []), advisory)

  await editor.save()
  assert.deepEqual(editor.layoutSaves(), [], 'nothing on the page awaits the teacher')
  assert.deepEqual(storedDiagnostic(editor), advisory)
  assert.equal(editor.confirms().length, 1)
})

test('low-confidence diagnostics keep resolving exactly as before', async (t) => {
  const editor = await editorWith(t, { needs_review: true, review_reason: 'low_confidence' })
  await editor.save()
  const resolved = storedDiagnostic(editor)
  assert.equal(resolved.needs_review, false)
  assert.equal(resolved.resolved_from, 'low_confidence')
  assert.equal(editor.confirms().length, 1)
})

test('resolving the page diagnostics opens the confirmation gate in the same state', async (t) => {
  const editor = await editorWith(t, directoryDiagnostic())
  const blocking = editor.page.setup.templateGeometryReview
  assert.equal(blocking.needsReview, true)
  assert.deepEqual(blocking.diagnosticReasons, [REASON])
  assert.deepEqual(blocking.diagnosticReviewDetails, [`第1页：${REASON}`])
  assert.match(blocking.summary, /模型诊断待复核（第1页：部分 Q 标签无法放入页面/)
  assert.equal(editor.alert(), `模板定位仍需教师复核：${blocking.summary}`)
  assert.equal(editor.page.setup.templateCanConfirm, false)

  // Saving the page resolves the diagnostic; the gate reads the same state at once.
  // (The confirmation answer is held back so the editor stays open to look at.)
  let release
  const held = new Promise((done) => { release = done })
  editor.answer(ROUTES.confirm, () => held.then(() => ({ status: 200, data: { success: true, page_count: 1 } })))
  await editor.save()
  const cleared = editor.page.setup.templateGeometryReview
  assert.equal(cleared.needsReview, false)
  assert.deepEqual(cleared.diagnosticReasons, [])
  assert.deepEqual(cleared.diagnosticReviewDetails, [])
  assert.equal(editor.page.setup.templateCanConfirm, true)
  assert.equal(editor.alert(), '')
  assert.equal(editor.confirms().length, 1, 'the confirmation went out in the same save')
  release()
  await editor.settle()
})

test('a page with a pending review diagnostic counts as an unsaved teacher decision', async (t) => {
  // Without a diagnostic the page is clean and the save skips it.
  const clean = await editorWith(t, null)
  assert.doesNotMatch(clean.card().text, /已调整|请核对本页定位/)
  await clean.save()
  assert.deepEqual(clean.layoutSaves(), [])
  clean.unmount()

  // With a pending one, the same page must reach the server, which clears the marker.
  const editor = await editorWith(t, directoryDiagnostic())
  assert.match(editor.card().text, /请核对本页定位/)
  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(body => body.page_index), [0])
  assert.equal(storedDiagnostic(editor).resolved_by, 'teacher')
  assert.equal(editor.confirms().length, 1)
})

test('unfinished templates stay recoverable until confirmed, cancelled or frozen', async (t) => {
  // Opened from the checkpoint, the draft keeps it: a reload must find the editor again.
  const opened = await openTemplateEditor(t)
  assert.equal(opened.dialog().attributes['data-open'], 'true')
  assert.ok(opened.storage()[CHECKPOINT_KEY], 'applying the recognition keeps the checkpoint')
  await opened.save()
  assert.equal(opened.confirms().length, 1)
  assert.equal(opened.storage()[CHECKPOINT_KEY], undefined, 'a confirmed template retires it')
  opened.unmount()

  const cancelled = await openTemplateEditor(t)
  assert.ok(cancelled.storage()[CHECKPOINT_KEY])
  await cancelled.click('取消')
  assert.equal(cancelled.storage()[CHECKPOINT_KEY], undefined, 'a cancelled draft retires it')
  cancelled.unmount()

  // The server already froze the template (student recognition uses it): nothing to reopen.
  const frozen = await openTemplateEditor(t, { recognition: { status: 'frozen' } })
  assert.equal(frozen.storage()[CHECKPOINT_KEY], undefined)
  assert.equal(frozen.dialog().attributes['data-open'], 'false')
  assert.equal(frozen.fetches(ROUTES.cutState).length, 0)
})

test('the blocking message names the page and the concrete model reason', async (t) => {
  const reasons = ['模型未找到第 1 题题号', '题号与作答区无法对应', '目录阶段定位失败', '第四条原因不再列出']
  // A page the model left without any geometry: saving it cannot confirm anything.
  const editor = await openTemplateEditor(t, {
    cut: { pages: [cutPage(0, {
      question_anchors: [],
      anchor_diagnostics: { needs_review: true, review_reasons: [reasons[0]] },
      template_ocr_diagnostics: { needs_review: true, review_reason: reasons[1] },
      modes: { question: {
        anchor_diagnostics: { geometry_review_pending: true, review_reasons: [reasons[2]] },
        diagnostics: { needs_review: true, review_reasons: [reasons[3]] },
      } },
    })] },
  })
  await editor.save()
  assert.equal(editor.confirms().length, 0)
  const [warning] = editor.warnings()
  const detail = reasons.slice(0, 3).map(reason => `第1页：${reason}`).join('；')
  assert.ok(warning.includes(`模型定位或逐图关联仍需人工复核，请核对后重新保存模板（${detail}）`), warning)
  assert.doesNotMatch(warning, /第四条原因不再列出/)
  // The review keeps all of them; the messages name the first three.
  assert.deepEqual(editor.page.setup.templateGeometryReview.diagnosticReviewDetails, reasons.map(reason => `第1页：${reason}`))
  assert.doesNotMatch(editor.alert(), /第四条原因不再列出/)
})
