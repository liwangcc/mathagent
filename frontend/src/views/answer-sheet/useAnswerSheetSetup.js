/**
 * From a new session to a frozen layout.
 *
 * - Batches and paper templates: creating, opening, closing and removing a session, registering a template.
 * - The original paper: uploading and recognizing it, confirming its answers; template codes and their history.
 * - The blank answer sheet's regions: locating, editing, uploading, accelerated processing, saving,
 *   publishing the template code and freezing the layout.
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { appendDesktopCanonicalCandidates } from '@/utils/desktopTemplateCanonicalization'

export function useAnswerSheetSetup(ctx) {
  const {
    STORAGE_KEY, busy, operationMessage, restoreError, session, activeStage, answerSource,
    sourceFiles, answerFiles, layoutFiles, templateCodeInput, templateCodeHistory, batchSetup,
    paperTemplates, paperTemplateSources, paperRegistration, batches, questionDrafts, answerRegions,
    identityRegions, layoutSummary, currentPageId, answerSpecs, visibleAnswerRegions,
    visibleIdentityRegions, layoutAccelerationMessage, applySession, requireAnswerSheetService, run,
    runAnswerSheetStream,
  } = ctx

  function formatBatchTime(value) {
    const date = new Date(value || '')
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
  }
  function paperTemplateLabel(item) {
    const name = String(item?.alias || item?.exam_name || '').trim()
    return `${name ? `${name}　` : ''}${item.template_code}　${item.question_count}题`
  }
  function paperSourceLabel(item) {
    const name = String(item?.exam_name || '').trim() || `详细批改模板 ${item.template_id}`
    const confirmed = formatBatchTime(item?.confirmed_at)
    return `${name}　${item.question_count}题${confirmed ? `　确认于 ${confirmed}` : ''}`
  }
  async function loadBatchChoices() {
    const [templates, sources, listed] = await Promise.allSettled([
      api.answerSheet.paperTemplates(),
      api.answerSheet.paperTemplateSources(),
      api.answerSheet.listSessions(),
    ])
    const items = (result, key) => (
      result.status === 'fulfilled' && Array.isArray(result.value?.[key]) ? result.value[key] : []
    )
    paperTemplates.value = items(templates, 'templates')
    paperTemplateSources.value = items(sources, 'sources')
    batches.value = items(listed, 'batches')
  }
  async function registerPaperTemplate() {
    if (!requireAnswerSheetService()) return
    const { template_id: templateId, alias } = paperRegistration.value
    if (!templateId) return
    const data = await run(
      () => api.answerSheet.registerPaperTemplate(templateId, String(alias || '').trim()),
      '试卷模板已登记，可在多个批次复用',
    )
    paperRegistration.value = { template_id: '', alias: '' }
    await loadBatchChoices()
    if (data?.template?.template_code) batchSetup.value.paper_template_code = data.template.template_code
  }
  async function createSession() {
    if (!requireAnswerSheetService()) return
    // The batch inherits the global current class on the server; there is no
    // per-batch class or grading-type choice.  Questions and answers come from
    // the selected frozen paper template.
    const payload = { paper_template_code: batchSetup.value.paper_template_code }
    const sheetCode = String(batchSetup.value.answer_sheet_template_code || '').trim().toUpperCase()
    if (sheetCode) payload.answer_sheet_template_code = sheetCode
    const data = await run(() => api.answerSheet.createSession(payload), '答题卡批次已创建')
    batchSetup.value = { paper_template_code: '', answer_sheet_template_code: '' }
    applySession(data)
    if (sheetCode) void loadTemplateCodeHistory()
  }
  async function openBatch(sessionId) {
    const data = await run(() => api.answerSheet.getSession(sessionId))
    restoreError.value = ''
    applySession(data)
  }
  function closeSession() {
    // Only leaves the batch view; the batch stays stored and listed.
    localStorage.removeItem(STORAGE_KEY)
    session.value = null
    activeStage.value = 0
    restoreError.value = ''
    void loadBatchChoices()
  }
  async function uploadSource() {
    if (!requireAnswerSheetService()) return
    const form = new FormData()
    sourceFiles.value.forEach(file => form.append('source_files', file))
    answerFiles.value.forEach(file => form.append('answer_files', file))
    form.append('answer_source', answerSource.value)
    applySession(await run(() => api.answerSheet.uploadSource(session.value.session_id, form), '材料已安全保存'))
  }
  async function recognizeSource() {
    const data = await runAnswerSheetStream(
      signal => api.answerSheet.recognizeSourceStream(session.value.session_id, signal),
      '正在识别原始试卷',
      '识别完成，请教师检查',
    )
    if (!data?.session) return
    applySession(data.session)
    activeStage.value = 1
  }
  async function confirmAnswers() { const questions = questionDrafts.value.map(({ solution_steps_text, ...item }) => ({ ...item, solution_steps: solution_steps_text.split('\n').map(value => value.trim()).filter(Boolean) })); applySession(await run(() => api.answerSheet.confirmAnswers(session.value.session_id, questions), '题目与答案已确认')); activeStage.value = 2 }
  function templateHistoryLabel(item) {
    const counts = item?.question_counts || {}
    const types = [
      counts.choice ? `选${counts.choice}` : '',
      counts.fill ? `填${counts.fill}` : '',
      counts.solve ? `解${counts.solve}` : '',
    ].filter(Boolean).join(' · ')
    return `${item.template_code}　${item.page_count}页 / ${item.question_count}题${types ? `　${types}` : ''}`
  }
  async function loadTemplateCodeHistory() {
    try {
      const data = await api.answerSheet.templateHistory()
      templateCodeHistory.value = Array.isArray(data?.templates) ? data.templates : []
    } catch {
      templateCodeHistory.value = []
    }
  }
  async function useTemplateCode() {
    if (!requireAnswerSheetService()) return
    const code = String(templateCodeInput.value || '').trim().toUpperCase()
    if (!code) return
    const response = await run(
      () => api.answerSheet.useTemplateCode(session.value.session_id, code),
      '模板码已套用，无需模型重新定位',
    )
    applySession(response.session, { chooseStage: false })
    templateCodeInput.value = response.template.template_code
    await loadTemplateCodeHistory()
    activeStage.value = 3
  }
  async function locateLayoutRegions() {
    operationMessage.value = 'qwen3.8max 正在粗定位答题区域'
    try {
      const response = await run(() => api.answerSheet.locateLayout(session.value.session_id))
      applySession(response.session, { chooseStage: false })
      layoutSummary.value = response.summary
      const missing = response.localization?.missing_questions || []
      if (missing.length) {
        ElMessage.warning(`粗定位已完成，仍需手工补充第 ${missing.join('、')} 题`)
      } else {
        ElMessage.success('粗定位已完成，请逐项检查并勾选教师已确认')
      }
      return response
    } finally {
      operationMessage.value = ''
    }
  }
  function confirmCurrentPageRegions() {
    for (const region of [...visibleAnswerRegions.value, ...visibleIdentityRegions.value]) {
      region.teacher_confirmed = true
    }
    ElMessage.success('本页定位已确认，保存版面后即可冻结')
  }
  async function uploadLayout() {
    if (!requireAnswerSheetService()) return
    const data = await run(async () => {
      const form = new FormData()
      layoutFiles.value.forEach(file => form.append('files', file))
      await appendDesktopCanonicalCandidates(form, layoutFiles.value)
      return api.answerSheet.uploadLayout(session.value.session_id, form)
    })
    if (!data) return
    applySession(data)
    if (data.accelerator || data.asset_lifecycle?.layout_acceleration) {
      ElMessage.warning(data.accelerator?.message || layoutAccelerationMessage.value)
      activeStage.value = 2
      return
    }
    ElMessage.success('空白答题卡已上传，正在进行粗定位')
    activeStage.value = 3
    try {
      await locateLayoutRegions()
    } catch {
      ElMessage.warning('空白页已保留，可稍后重试粗定位或直接手工绑定')
    }
  }
  async function processLayoutAcceleration() {
    if (!session.value?.session_id || busy.value) return
    const data = await run(
      () => api.answerSheet.processLayout(session.value.session_id),
    )
    if (!data) return
    applySession(data, { chooseStage: false })
    if (data.accelerator || data.asset_lifecycle?.layout_acceleration) {
      ElMessage.warning(data.accelerator?.message || layoutAccelerationMessage.value)
      activeStage.value = 2
      return
    }
    ElMessage.success('空白答题卡页面处理完成，正在进行粗定位')
    activeStage.value = 3
    try {
      await locateLayoutRegions()
    } catch {
      ElMessage.warning('空白页已保留，可稍后重试粗定位或直接手工绑定')
    }
  }
  async function saveLayout() { const response = await run(() => api.answerSheet.saveLayout(session.value.session_id, { answer_regions: answerRegions.value, identity_regions: identityRegions.value }), '版面草稿已保存'); applySession(response.session, { chooseStage: false }); layoutSummary.value = response.summary }
  async function publishTemplateCode(advance = false) {
    const response = await run(
      () => api.answerSheet.publishTemplateCode(session.value.session_id),
      '模板码已生成并加入历史记录',
    )
    applySession(response.session, { chooseStage: false })
    templateCodeInput.value = response.template.template_code
    await loadTemplateCodeHistory()
    if (advance) activeStage.value = 4
    return response
  }
  async function freezeLayout() {
    applySession(await run(() => api.answerSheet.freezeLayout(session.value.session_id), '版面已冻结'), { chooseStage: false })
    try {
      await publishTemplateCode(true)
    } catch {
      activeStage.value = 3
      ElMessage.warning('版面已经冻结，但模板码生成失败；可在当前页面重试')
    }
  }
  async function copyTemplateCode() {
    const code = String(session.value?.shared_template_code || '')
    if (!code) return
    try {
      await navigator.clipboard.writeText(code)
      ElMessage.success('模板码已复制')
    } catch {
      ElMessage.warning(`请手动复制：${code}`)
    }
  }

  async function removeSession() { await ElMessageBox.confirm('删除后本次草稿及已上传文件无法恢复，确定继续吗？', '删除答题卡草稿', { type: 'warning' }); await run(() => api.answerSheet.deleteSession(session.value.session_id), '草稿已删除'); localStorage.removeItem(STORAGE_KEY); session.value = null; activeStage.value = 0; void loadBatchChoices() }
  function regionTypeOptions(questionNo) { const type = answerSpecs.value.find(item => String(item.question_no) === String(questionNo))?.question_type; if (type === 'choice') return [{ label: '标准涂卡', value: 'choice_mark' }, { label: '手写字母', value: 'choice_handwritten' }]; if (type === 'fill') return [{ label: '填空', value: 'fill' }]; return [{ label: '解答', value: 'solve' }] }
  function syncRegionType(region) { region.region_type = regionTypeOptions(region.question_no)[0]?.value || 'solve'; const question = answerSpecs.value.find(item => String(item.question_no) === String(region.question_no)); region.allow_multiple = question?.choice_mode === 'multiple' }
  function addAnswerRegion() { const question = answerSpecs.value.find(item => !answerRegions.value.some(region => region.question_no === item.question_no)) || answerSpecs.value[0]; if (!question || !currentPageId.value) return; const region = { region_id: `answer_${Date.now()}`, page_id: currentPageId.value, question_no: String(question.question_no), subquestion_no: '', bbox: [100, 100, 900, 200], region_type: 'solve', reading_order: answerRegions.value.length + 1, required: true, teacher_confirmed: false, choice_options: ['A', 'B', 'C', 'D'], allow_multiple: question.choice_mode === 'multiple' }; syncRegionType(region); answerRegions.value.push(region) }
  function addIdentityRegion() { if (!currentPageId.value) return; identityRegions.value.push({ region_id: `identity_${Date.now()}`, page_id: currentPageId.value, bbox: [50, 30, 450, 100], identity_type: 'name', teacher_confirmed: false, config: {} }) }
  function bboxStyle(bbox) { return { left: `${bbox[0] / 10}%`, top: `${bbox[1] / 10}%`, width: `${(bbox[2] - bbox[0]) / 10}%`, height: `${(bbox[3] - bbox[1]) / 10}%` } }

  return {
    formatBatchTime, paperTemplateLabel, paperSourceLabel, loadBatchChoices, registerPaperTemplate,
    createSession, openBatch, closeSession, uploadSource, recognizeSource, confirmAnswers,
    templateHistoryLabel, loadTemplateCodeHistory, useTemplateCode, locateLayoutRegions,
    confirmCurrentPageRegions, uploadLayout, processLayoutAcceleration, saveLayout,
    publishTemplateCode, freezeLayout, copyTemplateCode, removeSession, regionTypeOptions,
    syncRegionType, addAnswerRegion, addIdentityRegion, bboxStyle,
  }
}
