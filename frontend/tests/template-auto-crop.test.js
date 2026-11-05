import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('模板识别完成后自动裁剪并显示受鉴权模板覆盖框', () => {
  assert.match(upload, /templateDialogVisible\.value = true[\s\S]*templateCutCandidatesState\(tid\)[\s\S]*applyTemplateCutState\(existingCut, runToken\)[\s\S]*void onRunTemplateCut\(\)/)
  assert.match(upload, /模板分栏与框选定位/)
  assert.match(upload, /templateCutPreviewPages\.length/)
  assert.match(upload, /page\.anchor_crop_plan\?\.parameters\?\.columns/)
  assert.match(upload, /const rawAnchors = reviewPending && Array\.isArray\(page\.question_anchor_candidates\)[\s\S]*?page\.question_anchors[\s\S]*?artifact\.question_anchors/)
  assert.match(upload, /page\.question_anchor_candidates\.length/)
  assert.match(upload, /const rawHandwritingRegions = reviewPending && templateRegionCandidateUsable\(page\.template_handwriting_region_candidates\)[\s\S]*?page\.template_handwriting_regions[\s\S]*?artifact\.template_handwriting_regions/)
  assert.match(upload, /const mergedAnswerBoxes = mergeTemplateAnswerRegionSources\(rawAnswerRegions, rawHandwritingRegions\)/)
  assert.match(upload, /templateRegionCandidateUsable\(page\.template_handwriting_region_candidates\)/)
  assert.match(upload, /const rawAnswerRegions = reviewPending && templateRegionCandidateUsable\(page\.template_answer_region_candidates\)[\s\S]*?page\.template_answer_regions[\s\S]*?artifact\.template_answer_regions/)
  assert.match(upload, /templateRegionCandidateUsable\(page\.template_answer_region_candidates\)/)
  assert.match(upload, /questionAnchors: cloneQuestionAnchors\(rawAnchors\)/)
  assert.match(upload, /templateQuestionAnchorBBox\(anchor\)[\s\S]*templateBoxStyle\(templateQuestionAnchorBBox\(anchor\)\)/)
  assert.match(upload, /Older provider payloads called this box `bbox` or `question_bbox`/)
  assert.match(upload, /item\.answer_regions[\s\S]*item\.answer_bbox[\s\S]*item\.student_answer_bbox/)
  assert.match(upload, /tpl-question-anchor/)
  assert.match(upload, /tpl-template-handwriting/)
  assert.match(upload, /api\.grade\.templatePageImage\(templateId, Number\(page\.page_index\)\)/)
})

test('模板页图以带登录令牌的 blob 请求取回，并返回由该图生成的对象 URL', async (t) => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'tok-page' } })
  const image = new Blob(['page-image'], { type: 'image/png' })
  client.respond(() => ({ data: image, headers: { 'content-type': 'image/png' } }))
  const createObjectURL = t.mock.method(URL, 'createObjectURL', () => 'blob:template-page-3')

  const objectUrl = await client.api.grade.templatePageImage('tpl 1/2', 3)

  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'get')
  assert.equal(request.url, '/batch-grade/template/tpl%201%2F2/pages/3/image')
  assert.equal(request.responseType, 'blob')
  assert.equal(request.headers.Authorization, 'Bearer tok-page')
  assert.equal(createObjectURL.mock.callCount(), 1)
  assert.equal(createObjectURL.mock.calls[0].arguments[0], image)
  assert.equal(objectUrl, 'blob:template-page-3')
})

test('手动按钮只用于重新自动裁剪，不是首次裁剪门禁', () => {
  assert.match(upload, /@click="onRunTemplateCut\(\{ force: true \}\)"/)
  assert.match(upload, /重新自动裁剪/)
  assert.doesNotMatch(upload, />生成题块候选</)
})

test('轮询先看到完成时只渲染一次并立即解锁', () => {
  // The POST may stay open after the server already finished; polling observes
  // that completion, so it must render once and release the editor lock instead
  // of rebuilding every preview on each tick while the POST hangs.
  assert.match(upload, /let templateCutCompletedRunToken = null/)
  assert.match(upload, /function releaseFinishedTemplateCut\(runToken\) \{[\s\S]*?templateCutBusy\.value = false[\s\S]*?stopTemplateCutPolling\(\)/)
  assert.match(upload, /const finished = \['complete', 'failed'\]\.includes\(data\.status\)/)
  assert.match(upload, /if \(templateCutCompletedRunToken === expectedRunToken\) \{[\s\S]*?releaseFinishedTemplateCut\(expectedRunToken\)[\s\S]*?return/)
  assert.match(upload, /if \(finished\) \{\s*templateCutCompletedRunToken = expectedRunToken\s*releaseFinishedTemplateCut\(expectedRunToken\)/)
})

function layoutDecision(page, { pendingPages = new Set(), reviewPending = false } = {}) {
  const start = upload.indexOf('      const planColumns = cloneTemplateColumns(')
  const end = upload.indexOf('      const artifact = page.artifact', start)
  assert.ok(start > 0 && end > start, 'Read the live layout decision block')
  const cloneStart = upload.indexOf('function cloneTemplateColumns(')
  const cloneEnd = upload.indexOf('function markTemplateGeometryConfirmed(')
  assert.ok(cloneStart > 0 && cloneEnd > cloneStart, 'Read the live column cloner')
  const body = `${upload.slice(cloneStart, cloneEnd)}${upload.slice(start, end)}`
    + 'return { columns, columnCount, layoutReviewPending, reviewPending,'
    + ' columnsSource: columnState.columnsSource, columnsConfirmed: columnState.columnsConfirmed }'
  return new Function('page', 'pageIndex', 'pendingPages', 'templatePageReviewPending', body)(
    page, Number(page.page_index ?? 0), pendingPages, () => reviewPending,
  )
}

test('crop plan 为空时回退读取 modes.question 候选列布局且从不自动确认', () => {
  const planPage = {
    anchor_crop_plan: { parameters: { columns: [{ x1_1000: 0, x2_1000: 500 }, { x1_1000: 500, x2_1000: 1000 }], column_count: 2 } },
    modes: { question: {} },
  }
  const planned = layoutDecision(planPage)
  assert.deepEqual(planned.columns.map((column) => [column.x1_1000, column.x2_1000]), [[0, 500], [500, 1000]])
  assert.equal(planned.columnCount, 2)
  assert.equal(planned.columnsSource, 'model')
  assert.equal(planned.columnsConfirmed, true)
  assert.equal(planned.layoutReviewPending, false)

  const candidatePage = { modes: { question: { column_layout: { columns: [{ x1_1000: 0, x2_1000: 480 }, { x1_1000: 480, x2_1000: 1000 }], column_count: 2 } } } }
  const candidate = layoutDecision(candidatePage)
  assert.deepEqual(candidate.columns.map((column) => [column.x1_1000, column.x2_1000]), [[0, 480], [480, 1000]])
  assert.equal(candidate.columnCount, 2)
  assert.equal(candidate.columnsSource, 'model_candidate')
  assert.equal(candidate.columnsConfirmed, false)
  assert.equal(candidate.layoutReviewPending, true)

  const none = layoutDecision({ modes: { question: { column_layout: { columns: [] } } } })
  assert.deepEqual(none.columns, [])
  assert.equal(none.columnCount, 0)
  assert.equal(none.columnsSource, 'teacher_draft')
  assert.equal(none.columnsConfirmed, false)
  assert.equal(none.layoutReviewPending, true)

  const reviewedPlan = layoutDecision(planPage, { reviewPending: true })
  assert.equal(reviewedPlan.columnsConfirmed, false)
  assert.equal(reviewedPlan.layoutReviewPending, true)
  const reviewedCandidate = layoutDecision(candidatePage, { pendingPages: new Set([0]) })
  assert.equal(reviewedCandidate.columnsConfirmed, false)
  assert.equal(reviewedCandidate.layoutReviewPending, true)
})

test('候选列布局接入页面对象：dirty 与复核算子随候选开启，默认值不被冒充', () => {
  assert.match(upload, /columnCount,\s*\n\s*\.\.\.columnState,/)
  assert.match(upload, /dirty: \(reviewPending \|\| layoutReviewPending\) && columns\.length > 0/)
  assert.match(upload, /columnsSource: planColumns\.length \? 'model' : 'model_candidate'/)
  assert.match(upload, /columnsConfirmed: !layoutReviewPending && planColumns\.length > 0/)
  assert.match(upload, /questionMode\?\.column_layout\?\.column_count \|\| columns\.length/)
})
