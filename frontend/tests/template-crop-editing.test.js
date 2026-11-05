import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { loadApiClient } from './helpers/apiClient.js'

const source = fs.readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('label points retain geometry while their text stays in the external legend', () => {
  assert.match(source, /const TEMPLATE_PROVENANCE_KEYS = \[\s*'geometry_kind'/)
  assert.match(source, /'is-label-anchor': box.geometry_kind === 'label_anchor'/)
  assert.match(source, /v-if="box.geometry_kind !== 'label_anchor'"/)
  assert.match(source, /<template #geometry-labels>/)
  assert.match(source, /<TemplateGeometryLegend/)
  assert.doesNotMatch(source, /is-label-anchor span \{/)
  assert.match(source, /templatePreserveProvenance\(item, templatePreserveProvenance/)
  assert.match(source, /TEMPLATE_PROVENANCE_KEYS.forEach/)
})

test('teacher can adjust and persist template columns and blue answer regions', () => {
  assert.match(source, /startTemplateDividerMove/)
  assert.match(source, /startTemplateQuestionMove/)
  assert.match(source, /startTemplateQuestionResize/)
  assert.match(source, /startTemplateHandwritingMove/)
  assert.match(source, /startTemplateHandwritingResize/)
  assert.match(source, /saveTemplateLayoutPage/)
  assert.match(source, /saveAllTemplateLayoutPages/)
  assert.match(source, /统一保存所有页，无需逐页保存/)
  assert.doesNotMatch(source, />保存本页(?:定位)?调整<\/button>/)
  assert.match(source, /question_anchors: cloneQuestionAnchors/)
  assert.match(source, /question_number_bbox/)
  assert.match(source, /template_answer_regions: blankTemplate \|\| page\.hasAnswerRegionContract \? confirmedAnswerRegions/)
  assert.match(source, /template_handwriting_regions: confirmedHandwriting/)
  assert.doesNotMatch(source, /deleteTemplateCrop/)
})

test('模板分栏与作答框以 PUT 持久化到 layout-overrides，失败时不额外弹通用提示', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'tok-layout' } })
  const payload = {
    page_index: 1,
    columns: [{ x1_1000: 0, x2_1000: 500 }, { x1_1000: 500, x2_1000: 1000 }],
    question_anchors: [{ question_number: '1', question_number_bbox: [10, 10, 40, 30], human_confirmed: true }],
    template_answer_regions: [{ question_number: '1', regions: [[50, 60, 400, 200]] }],
    template_handwriting_regions: [{ question_number: '1', regions: [[50, 60, 400, 200]] }],
  }
  client.respond(() => ({ data: { success: true, page_index: 1, columns: payload.columns } }))

  const saved = await client.api.grade.templateLayoutOverrides('tpl 1/2', payload)

  assert.deepEqual(saved, { success: true, page_index: 1, columns: payload.columns })
  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'put')
  assert.equal(request.url, '/batch-grade/template/tpl%201%2F2/layout-overrides')
  assert.deepEqual(JSON.parse(request.data), payload)
  assert.equal(request.headers.Authorization, 'Bearer tok-layout')

  client.respond(() => ({ status: 422, data: { detail: '第 1 题作答框越界' } }))
  await assert.rejects(client.api.grade.templateLayoutOverrides('tpl 1/2', payload), { message: '第 1 题作答框越界' })
  assert.deepEqual(client.messages, [])
})

test('整题增删折叠在题目管理内，并与仅删除定位标记明确区分', () => {
  assert.match(source, /insertTemplateQuestion/)
  assert.match(source, /deleteTemplateQuestion/)
  assert.match(source, /<details class="tpl-question-management">/)
  assert.match(source, /在前面插入题目/)
  assert.match(source, /末尾追加题目/)
  assert.match(source, /删除整道题/)
  assert.match(source, /以下操作增删整道题及答案，不是删除图上的框/)
  assert.match(source, /@click="deleteTemplateSelection\(page\)">删除所选/)
  // 教师标注：新增题号由老师输入（仅数字和点），随请求发送并由服务端校验；其他题号保持不变
  assert.match(source, /新增时请输入题号（仅数字和点）；其他题号保持不变/)
  assert.match(
    source,
    /\{ action: 'insert', before_question_number: target, new_question_number: number \}/,
  )
})

test('整题增删以 PUT 发往模板题目接口，原样携带教师输入的题号并返回服务端编号', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'tok-question' } })
  const renumbered = { success: true, questions: [{ question_number: '2.1' }, { question_number: '3' }] }
  client.respond(() => ({ data: renumbered }))
  const insert = { page_index: 0, action: 'insert', before_question_number: '3', new_question_number: '2.1' }

  const result = await client.api.grade.templateQuestionEdit('tpl 1/2', insert)

  assert.deepEqual(result, renumbered)
  const remove = { page_index: 0, action: 'delete', question_number: '3' }
  await client.api.grade.templateQuestionEdit('tpl 1/2', remove)
  assert.equal(client.requests.length, 2)
  client.requests.forEach((request) => {
    assert.equal(request.method, 'put')
    assert.equal(request.url, '/batch-grade/template/tpl%201%2F2/questions')
    assert.equal(request.headers.Authorization, 'Bearer tok-question')
  })
  assert.deepEqual(client.requests.map((request) => JSON.parse(request.data)), [insert, remove])
})

test('空白与有痕迹模板都允许教师查看、编辑并保存多个豁免框', () => {
  // 模板是否空白不再由老师选择；保存时仍按模式口径区分（normalizeTemplateMode）。
  assert.match(source, /const blankTemplate = normalizeTemplateMode\(templateMode\.value, 'marked'\) === 'blank'/)
  assert.doesNotMatch(source, /红色题号框/)
  assert.match(source, /紫色豁免框用于去除模板笔迹/)
  assert.match(source, /startTemplateGeometryCreation/)
  assert.match(source, /commitTemplateGeometryCreation/)
  assert.match(source, /deleteTemplateSelection/)
  // 豁免框已并入统一作答框：工具栏现在只有四个标记工具。
  assert.match(source, /key: 'answer', label: '蓝色作答框'/)
  assert.match(source, /@click="armTemplateGeometryTool\(page, tool.key\)"/)
  assert.match(source, /删除所选<\/button>/)
  assert.match(source, /const boxes = kind === 'drawing' \? page\.drawingBoxes : page\.answerBoxes/)
  assert.match(source, /boxes\.push\(\{ question_number: number, bbox \}\)/)
  assert.match(source, /boxes\.splice\(index, 1\)/)
  assert.match(source, /tpl-template-handwriting/)
  assert.match(source, /template_handwriting_regions: confirmedHandwriting/)
  assert.match(source, /template_answer_regions: blankTemplate \|\| page\.hasAnswerRegionContract \? confirmedAnswerRegions : undefined/)
  assert.match(source, /previewPage\.handwritingBoxes = previewPage\.answerBoxes/)
  assert.doesNotMatch(source, /template_handwriting_regions: templateMode\.value === 'marked'/)
  assert.doesNotMatch(source, /page\.handwritingBoxes = templateMode\.value === 'marked'/)
})

test('题干或题型编辑只使作图参考失效，不再自动请求答案草稿', () => {
  assert.match(source, /@input="markTemplateQuestionInput\(q\)"/)
  assert.match(source, /@change="onQuestionTypeChange\(q\); markTemplateQuestionInput\(q\)"/)
  assert.doesNotMatch(source, /templateAnswerDraft|answer-draft|_answerDraftStale/)
  const start = source.indexOf('function templateQuestionSignature')
  const end = source.indexOf('function markTemplateQuestionInput', start)
  assert.ok(start >= 0 && end > start)
  assert.doesNotMatch(source.slice(start, end), /student_answer/)
})

test('保存以老师可见答案为准，不要求再次生成', () => {
  const confirmStart = source.indexOf('async function onConfirmTemplate()')
  const confirmEnd = source.indexOf('async function loadTemplateCorrectionCandidates', confirmStart)
  assert.ok(confirmStart >= 0 && confirmEnd > confirmStart)
  const confirmBody = source.slice(confirmStart, confirmEnd)
  const validationIndex = confirmBody.indexOf('if (!templateCanConfirm.value)')
  assert.ok(validationIndex >= 0)
  assert.doesNotMatch(confirmBody, /TemplateAnswerDraft/)
  assert.match(source, /if \(templateBankImported\.value\) return false/)
  assert.match(
    source,
    /<button class="btn btn-primary" @click="onConfirmTemplate" :disabled="confirmingTemplate \|\| importingTemplateBank \|\| templateCutBusy">/,
  )
  assert.match(source, /保存模板（全部页）/)
})

test('模板几何风险会消费服务端复核状态并禁用确认', () => {
  assert.match(source, /layout_review_pending_pages/)
  assert.match(source, /anchor_review_pending_pages/)
  assert.match(source, /geometry_review_pending_pages/)
  assert.match(source, /templateReviewPendingPageIndexes/)
  assert.match(source, /missingQuestionNumbers/)
  assert.match(source, /duplicateQuestionNumbers/)
  assert.match(source, /orphanQuestionNumbers/)
  assert.match(source, /outOfBoundsQuestionNumbers/)
  assert.match(source, /outOfBoundsHandwritingQuestions/)
  assert.match(source, /lowConfidenceQuestionNumbers/)
  assert.match(source, /lowConfidenceHandwritingQuestions/)
  assert.match(source, /diagnosticReasons/)
  assert.match(source, /const templateGeometryReview = computed\(templateGeometryReviewState\)/)
  assert.match(source, /!templateGeometryReview\.value\.needsReview/)
  assert.match(source, /serverPendingPages\.size === 0/)
  assert.match(source, /请核对本页定位/)
  const reviewStart = source.indexOf('function templateGeometryReviewState()')
  const reviewEnd = source.indexOf('const templateGeometryReview = computed', reviewStart)
  assert.ok(reviewStart >= 0 && reviewEnd > reviewStart)
  const reviewBody = source.slice(reviewStart, reviewEnd)
  const needsReviewStart = reviewBody.indexOf('needsReview: Boolean(')
  const needsReviewEnd = reviewBody.indexOf('),\n    layoutReviewPendingPages:', needsReviewStart)
  assert.ok(needsReviewStart >= 0 && needsReviewEnd > needsReviewStart)
  const needsReviewBody = reviewBody.slice(needsReviewStart, needsReviewEnd)
  ;[
    'pendingPages.size',
    'missingQuestionNumbers.length',
    'duplicateQuestionNumbers.length',
    'orphanQuestionNumbers.length',
    'outOfBoundsQuestionNumbers.length',
    'outOfBoundsHandwritingQuestions.length',
    'lowConfidenceQuestionNumbers.length',
    'lowConfidenceHandwritingQuestions.length',
    'diagnosticReasons.length',
  ].forEach((risk) => assert.ok(needsReviewBody.includes(risk), `${risk} 必须禁用模板确认`))
  assert.match(source, /请处理未绑定到模板题目的题号定位/)
  assert.match(source, /模型定位或逐图关联仍需人工复核/)
})

test('语义待复核候选只初始化编辑器，教师保存后清除候选与待复核标记', () => {
  assert.match(source, /question_anchor_candidates/)
  assert.match(source, /template_handwriting_region_candidates/)
  const loadStart = source.indexOf('async function loadTemplateCutPreviews')
  const loadEnd = source.indexOf('async function pollTemplateCutState', loadStart)
  assert.ok(loadStart >= 0 && loadEnd > loadStart)
  const loadBody = source.slice(loadStart, loadEnd)
  assert.match(loadBody, /Array\.isArray\(page\.question_anchor_candidates\)/)
  assert.match(loadBody, /templateRegionCandidateUsable\(page\.template_handwriting_region_candidates\)/)
  assert.match(loadBody, /questionAnchors: cloneQuestionAnchors\(rawAnchors\)/)
  assert.match(loadBody, /const mergedAnswerBoxes = mergeTemplateAnswerRegionSources\(rawAnswerRegions, rawHandwritingRegions\)/)
  assert.match(loadBody, /handwritingBoxes: mergedAnswerBoxes,/)
  const saveStart = source.indexOf('async function saveTemplateLayoutPage')
  const saveEnd = source.indexOf('async function editTemplateQuestion', saveStart)
  assert.ok(saveStart >= 0 && saveEnd > saveStart)
  const saveBody = source.slice(saveStart, saveEnd)
  assert.match(saveBody, /delete resultPage\.question_anchor_candidates/)
  assert.match(saveBody, /delete resultPage\.template_handwriting_region_candidates/)
  assert.match(saveBody, /TEMPLATE_REVIEW_PENDING_PAGE_KEYS\.forEach/)
  const clearStart = source.indexOf('function clearTemplateResolvedGeometryDiagnostics')
  const clearEnd = source.indexOf('function templateBBoxIsValid', clearStart)
  assert.ok(clearStart >= 0 && clearEnd > clearStart)
  const clearBody = source.slice(clearStart, clearEnd)
  ;[
    'delete target.layout_review_pending',
    'delete target.anchor_review_pending',
    'delete target.geometry_review_pending',
    'target.layoutReviewPending = false',
    'target.anchorReviewPending = false',
    'target.geometryReviewPending = false',
  ].forEach((statement) => assert.ok(clearBody.includes(statement), `${statement} 必须在教师保存后执行`))
})

test('复核页不把等分栏伪装成模型检测结果，默认栏数为双栏', () => {
  assert.match(source, /const templateColumnMode = ref\('double'\)/)
  assert.match(source, /<option value="double">双栏<\/option>/)
  const loadStart = source.indexOf('async function loadTemplateCutPreviews')
  const loadEnd = source.indexOf('async function pollTemplateCutState', loadStart)
  assert.ok(loadStart >= 0 && loadEnd > loadStart)
  const loadBody = source.slice(loadStart, loadEnd)
  assert.doesNotMatch(loadBody, /columns:\s*equalTemplateColumns\(/)
  assert.match(loadBody, /\n\s*columns,\n/)
  assert.match(loadBody, /const columnCount = columns\.length[\s\S]*?\? Number\([\s\S]*?: 0/)
  // A candidate layout arrives unconfirmed and under review; the review gate
  // and the unsaved-draft signal must both stay on for it.
  assert.match(loadBody, /columnsSource: planColumns\.length \? 'model' : 'model_candidate'/)
  assert.match(loadBody, /columnsConfirmed: !layoutReviewPending && planColumns\.length > 0/)
  assert.match(loadBody, /dirty:\s*\(reviewPending \|\| layoutReviewPending\) && columns\.length > 0/)
  assert.match(source, /columnsSource: 'teacher_draft'/)
  assert.match(source, /page\.columnsConfirmed = false/)
  assert.match(source, /Number\(page\.columnCount\) \|\| 2/)
  assert.match(source, /<option :value="0" disabled>选择栏数<\/option>/)
  assert.match(source, /:disabled="templatePageEditingLocked\(page\)" @change="resetTemplateColumns\(page\)"/)
  assert.match(source, /if \(!await saveAllTemplateLayoutPages\(\)\) return false/)
  const stateStart = source.indexOf('async function loadTemplateCutState')
  const stateEnd = source.indexOf('function templateCutErrorMessage', stateStart)
  assert.ok(stateStart >= 0 && stateEnd > stateStart)
  assert.match(source.slice(stateStart, stateEnd), /state\.status === 'complete'[\s\S]*loadTemplateCutPreviews\(state, expectedRunToken\)/)
})

test('取消模板识别会清空当前草稿并恢复默认选项，旧裁题响应不能回写', () => {
  assert.match(source, /@click="cancelTemplateDialog"/)
  assert.match(source, /function cancelTemplateDialog\(\)/)
  assert.match(source, /function clearTemplateDraft\(\)/)
  assert.match(source, /clearTemplateDraft\(\)/)
  assert.match(source, /templateCutController\?\.abort\(\)/)
  assert.match(source, /revokeTemplateCutPreviews\(\)/)
  assert.match(source, /pendingTemplateId\.value = ''/)
  assert.match(source, /templateDialogVisible\.value = false/)
  assert.match(source, /category: 'daily'/)
  assert.match(source, /const templateMode = ref\('marked'\)/)
  assert.match(source, /const templateColumnMode = ref\('double'\)/)
  assert.match(source, /let templateCutRunToken = 0/)
  assert.match(source, /if \(!pendingTemplateId\.value \|\| expectedRunToken !== templateCutRunToken\) return/)
  assert.match(source, /runToken !== templateCutRunToken \|\| templateId !== pendingTemplateId\.value/)
})

test('教师保存定位会携带并升级人工 provenance，低置信夹具默认仍待复核', () => {
  assert.match(source, /question_anchors: cloneQuestionAnchors\(page\.questionAnchors, \{ teacherConfirmed: true \}\)/)
  assert.match(source, /serializeTemplateHandwritingRegions\(textBoxes, page\.questionAnchors, \{ teacherConfirmed: true \}\)/)
  assert.match(source, /serializeTemplateHandwritingRegions\(\[\.\.\.textBoxes, \.\.\.drawingBoxes\], page\.questionAnchors, \{ teacherConfirmed: true \}\)/)
  assert.match(source, /return \{ textBoxes, drawingBoxes: page\.drawingBoxes\.filter\(box => !box\.slot_id\) \}/)
  assert.match(source, /template_handwriting_regions: confirmedHandwriting/)
  assert.match(source, /human_confirmed = true/)
  assert.match(source, /confidence_source = 'teacher'/)
  assert.match(source, /const TEMPLATE_GEOMETRY_CONFIDENCE_THRESHOLD = 0\.75/)
  const lowConfidenceFixture = {
    question_anchors: [{ question_number: '1', confidence: 0.2 }],
    template_handwriting_regions: [{ question_number: '1', regions: [[20, 20, 80, 80]], confidence: 0.2 }],
  }
  assert.equal(lowConfidenceFixture.question_anchors[0].confidence < 0.75, true)
  assert.equal(lowConfidenceFixture.template_handwriting_regions[0].confidence < 0.75, true)
  assert.match(source, /templateDiagnosticIsLowConfidence/)
  assert.match(source, /resolved_by: 'teacher'/)
  assert.match(source, /column_identity_conflict/)
  assert.match(source, /image_association_conflict/)
})

test('学生答案显示保持原文，不把 4√5 交给模板编辑器归一化', () => {
  assert.match(source, /student_answer \|\| '未识别到作答，请以左侧原卷为准'/)
  assert.doesNotMatch(source, /normalize(?:Choice)?Answer\([^)]*student_answer/)
  const studentAnswer = '4√5'
  assert.equal(studentAnswer, '4√5')
})
