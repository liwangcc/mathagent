/**
 * Student answer sheets: choosing and ordering the files, uploading and processing them, confirming
 * orientation, recognizing and reviewing them, and the grading drafts with their per-question details.
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { shallowRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import { renderLatex } from '@/utils/latex'
import { isSseServiceFault } from '@/utils/sse'

export function useAnswerSheetStudents(ctx) {
  const {
    auth, busy, operationMessage, operationProgress, session, studentFiles, studentOrderSource,
    classStudents, gradingDrafts, activeDraftIndex, currentReviewPaperId, captureSourcePath,
    currentDraft, currentDraftPages, currentReviewPageIndex, studentUpload, studentUploadActive,
    studentUploadProcessing, applySession, markServiceFault, requireAnswerSheetService,
    normalizeServiceError, run, runAnswerSheetStream,
  } = ctx

  const studentImageRequestId = shallowRef(0)

  function updateStudentUploadProgress(data) {
    if (data?.session?.session_id) {
      applySession(data.session, { chooseStage: false })
      return
    }
    if (!session.value || !studentUpload.value.upload_id) return
    const progress = data?.student_upload && typeof data.student_upload === 'object'
      ? data.student_upload
      : data
    const next = { ...studentUpload.value, status: 'processing' }
    for (const field of ['uploaded_count', 'processed_count', 'total_bytes']) {
      const value = Number(progress?.[field])
      if (Number.isFinite(value) && value >= 0) next[field] = value
    }
    if (!Number.isFinite(Number(progress?.processed_count))) {
      const current = Number(progress?.current)
      if (Number.isFinite(current) && current >= 0) next.processed_count = current
    }
    session.value = { ...session.value, student_upload: next }
  }
  function moveStudentFile(index, offset) {
    if (studentUploadActive.value) return
    const nextIndex = index + offset
    if (index < 0 || nextIndex < 0 || nextIndex >= studentFiles.value.length) return
    const next = [...studentFiles.value]
    const [file] = next.splice(index, 1)
    next.splice(nextIndex, 0, file)
    studentFiles.value = next
    studentOrderSource.value = {
      source: 'manual',
      label: '手动调整',
      message: '老师已调整当前列表，系统将按下面的最终顺序上传。',
    }
  }
  function removeStudentFile(index) {
    if (studentUploadActive.value || index < 0 || index >= studentFiles.value.length) return
    studentFiles.value = studentFiles.value.filter((_file, fileIndex) => fileIndex !== index)
    studentOrderSource.value = {
      source: 'manual',
      label: '手动调整',
      message: '老师已调整当前列表，系统将按下面的最终顺序上传。',
    }
  }
  async function processStudentUpload(uploadId = studentUpload.value.upload_id) {
    const durableUploadId = String(uploadId || '')
    if (!durableUploadId) return
    const data = await runAnswerSheetStream(
      signal => api.answerSheet.processStudentUploadStream(session.value.session_id, durableUploadId, signal),
      '正在处理已上传的学生答题卡',
      '学生答题卡已完成预处理',
      updateStudentUploadProgress,
    )
    const completedSession = data?.session || (data?.session_id ? data : null)
    if (completedSession) applySession(completedSession, { chooseStage: false })
  }
  async function uploadStudents() {
    if (!requireAnswerSheetService()) return
    if (busy.value) return
    const files = [...studentFiles.value]
    if (!files.length || studentUploadProcessing.value) return
    let durableUploadId = ''
    busy.value = true
    operationMessage.value = studentUploadActive.value ? '正在恢复学生答题卡上传' : '正在创建学生答题卡上传批次'
    try {
      const started = await api.answerSheet.startStudentUpload(session.value.session_id, false)
      applySession(started, { chooseStage: false })
      durableUploadId = String(started?.student_upload?.upload_id || '')
      if (!durableUploadId) throw new Error('服务器未返回可恢复的上传批次')
      for (const [index, file] of files.entries()) {
        operationMessage.value = '正在持久保存学生答题卡'
        operationProgress.value = { current: index, total: files.length }
        const latest = await api.answerSheet.uploadStudents(
          session.value.session_id,
          durableUploadId,
          file,
          captureSourcePath(file),
        )
        applySession(latest, { chooseStage: false })
      }
      studentFiles.value = []
      ElMessage.success('学生答题卡已逐页持久保存')
    } catch (error) {
      try {
        const latest = await api.answerSheet.getSession(session.value.session_id)
        applySession(latest, { chooseStage: false })
      } catch {
        // Keep the last page-level checkpoint if the session endpoint is unreachable.
      }
      if (isSseServiceFault(normalizeServiceError(error))) {
        markServiceFault('学生答题卡上传中断，已保存页不会丢失；链路恢复后可继续上传。')
      }
      if (!error?.response) notifyProcessingError(error, error?.message || '学生答题卡上传失败')
      return
    } finally {
      operationMessage.value = ''
      operationProgress.value = null
      busy.value = false
    }
    await processStudentUpload(durableUploadId)
  }
  async function restartStudentUpload() {
    if (!requireAnswerSheetService()) return
    await ElMessageBox.confirm(
      '已持久保存但尚未完成处理的页面将被丢弃，确定重新开始吗？',
      '重新开始学生答题卡上传',
      { type: 'warning' },
    )
    const restarted = await run(
      () => api.answerSheet.startStudentUpload(session.value.session_id, true),
      '已建立新的学生答题卡上传批次',
    )
    applySession(restarted, { chooseStage: false })
  }
  async function confirmOrientation(paperId, turns) {
    if (!requireAnswerSheetService()) return
    applySession(await run(() => api.answerSheet.confirmOrientation(session.value.session_id, paperId, turns), '方向已确认'), { chooseStage: false })
  }
  async function recognizeStudents() {
    const data = await runAnswerSheetStream(
      signal => api.answerSheet.recognizeStudentsStream(session.value.session_id, signal),
      '正在识别学生答题卡',
      '识别完成，请处理待复核项',
    )
    if (!data?.session) return
    applySession(data.session, { chooseStage: false })
    // Identity candidates come from the class the batch inherited at creation,
    // even if the teacher has since switched the global current class.
    classStudents.value = await api.classroom.students(session.value.class_id || auth.currentClassId)
  }
  async function confirmStudentReview(page) { const answers = Object.fromEntries((page.answers || []).map(item => [item.question_no, item.answer])); applySession(await run(() => api.answerSheet.confirmStudentReview(session.value.session_id, page.paper_id, { student_id: page.review_student_id || page.identity?.student_id, answers }), '本页已确认'), { chooseStage: false }) }
  async function buildDraft() {
    const data = await runAnswerSheetStream(
      signal => api.answerSheet.gradingDraftStream(session.value.session_id, signal),
      '正在生成批阅草稿',
      '批阅草稿已生成',
    )
    if (!data) return
    session.value.review_revision = data.review_revision
    session.value.grouping_decisions = data.drafts || []
    const lifecycle = { ...(session.value.asset_lifecycle || {}) }
    delete lifecycle.grading_draft_checkpoint
    session.value.asset_lifecycle = lifecycle
    gradingDrafts.value = data.drafts || []
    setActiveDraft(0)
  }
  async function saveDraftDecision() {
    const draft = currentDraft.value
    const details = draft.result?.details || []
    if (!details.length || details.some(detail => !['correct', 'partial', 'wrong'].includes(detail.result))) {
      ElMessage.warning('请先为全部题目选择正确、部分正确或错误')
      return
    }
    const payload = {
      student_id: draft.student_id,
      review_revision: session.value.review_revision,
      decisions: details.map((detail, index) => ({
        question_no: String(reviewDetailQuestionNo(detail, index)),
        result: detail.result,
        note: String(detail.teacher_note || ''),
      })),
    }
    try {
      await run(
        () => api.answerSheet.gradingDecision(session.value.session_id, payload),
        '人工判定已保存',
      )
    } catch {
      const latest = await api.answerSheet.getSession(session.value.session_id)
      applySession(latest, { chooseStage: false })
      ElMessage.warning('批阅草稿已更新，已载入服务器最新版本')
      return
    }
    const latest = await api.answerSheet.getSession(session.value.session_id)
    applySession(latest, { chooseStage: false })
  }
  async function archiveDrafts() { await run(() => api.answerSheet.archive(session.value.session_id), '答题卡批阅结果已归档'); applySession(await api.answerSheet.getSession(session.value.session_id), { chooseStage: false }) }

  function setActiveDraft(index) {
    activeDraftIndex.value = index
    currentReviewPaperId.value = gradingDrafts.value[index]?.paper_ids?.[0] || ''
  }
  function moveReviewPage(offset) {
    const next = currentReviewPageIndex.value + offset
    if (next >= 0 && next < currentDraftPages.value.length) currentReviewPaperId.value = currentDraftPages.value[next].paper_id
  }
  function openDetailPage(detail) {
    const paperIds = detail.paper_ids || []
    if (!paperIds.length || paperIds.includes(currentReviewPaperId.value)) return
    currentReviewPaperId.value = paperIds[0]
  }
  function formatNumber(value) {
    const number = Number(value)
    if (!Number.isFinite(number)) return '—'
    return Number.isInteger(number) ? String(number) : number.toFixed(1)
  }
  function formatBytes(value) {
    const bytes = Number(value)
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
    if (bytes < 1024) return `${Math.round(bytes)} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }
  function formatDraftMetric(result) {
    if (Number(result?.needs_review_count || 0) > 0) return '待教师复核'
    if (result?.max_score != null) return `${formatNumber(result?.score)} / ${formatNumber(result?.max_score)} 分`
    const rate = Number(result?.correct_rate)
    return Number.isFinite(rate) ? `${Math.round(rate * 100)}%` : '待教师复核'
  }
  function reviewDetailQuestionNo(detail, index) { return detail.question_no || detail.question_number || detail.parent_question_no || index + 1 }
  function reviewDetailClass(detail) { return ['correct', 'partial', 'wrong', 'needs_review'].includes(detail.result) ? detail.result : 'needs_review' }
  function reviewDetailLabel(detail) { return ({ correct: '正确', partial: '部分正确', wrong: '错误', needs_review: '待复核' })[reviewDetailClass(detail)] }
  function reviewDetailAnalysis(detail) { return detail.explanation || detail.hint || '' }
  function renderReviewText(value) {
    const normalized = String(value || '').replaceAll(String.fromCharCode(13), '')
    return normalized.split(String.fromCharCode(10)).map(line => renderLatex(line)).join('<br>')
  }

  return {
    studentImageRequestId, updateStudentUploadProgress, moveStudentFile, removeStudentFile,
    processStudentUpload, uploadStudents, restartStudentUpload, confirmOrientation,
    recognizeStudents, confirmStudentReview, buildDraft, saveDraftDecision, archiveDrafts,
    setActiveDraft, moveReviewPage, openDetailPage, formatNumber, formatBytes, formatDraftMetric,
    reviewDetailQuestionNo, reviewDetailClass, reviewDetailLabel, reviewDetailAnalysis,
    renderReviewText,
  }
}
