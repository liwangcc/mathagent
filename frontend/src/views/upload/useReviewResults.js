/**
 * The 批改 page's review of a graded batch: what the teacher does with the results.
 *
 * - The review batch pointer kept in localStorage (reviewStorageKey, rememberReviewBatch, forgetReviewBatch),
 *   restoring a batch's reviews after a reload (resumeReviewBatch, hydrateReviewState) and clearing them
 *   (clearReviewDisplay, maybeForgetTerminalReview, applyReviewToProgressRow).
 * - The manual decision dialog for flagged answers: opening it, the paper image and its pages, saving.
 * - Voiding or archiving reviews, and the downloads (student errors, review images, summary).
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import { reviewErrorMessage, reviewPaperId } from '@/utils/recognitionStatus'

export function useReviewResults(ctx) {
  const {
    anonymous, auth, REVIEW_BATCH_STORAGE_PREFIX, activeStep, form, batchId, progressList,
    gradeTotal, gradeError, summary, reviewRestoredLabel, coursewareRecommendations,
    coursewareRecommendationState, coursewareRecommendationQuery, coursewareRecommendationKey,
    reviewBusy, reviewImagesDownloading, reviewArchived, reviewRevision,
    manualDecisionDialogVisible, manualDecisionRow, manualDecisionDrafts, manualDecisionSaving,
    manualDecisionImageUrl, manualDecisionImageLoading, manualDecisionImageError,
    manualDecisionPages, manualDecisionPageIndex, manualDecisionShowPaper, rememberOrientationBatch,
    resetBatchReport, applyBatchReport, startBatchReportPolling,
  } = ctx

  let manualDecisionImageRequestId = 0

  const hasActiveReviewRows = computed(() => progressList.value.some((row) => row.disposition === 'active'))
  const pendingReviewCount = computed(() => progressList.value
    .filter((row) => row.disposition === 'active')
    .reduce((total, row) => total + Number(row.needs_review || 0), 0))
  const resolvedActiveReviewCount = computed(() => progressList.value.filter((row) =>
    row.disposition === 'active' && Number(row.needs_review || 0) === 0
  ).length)

  const manualDecisionCompactDrafts = computed(() => manualDecisionDrafts.value.filter((item) => manualDecisionIsCompact(item)))
  const manualDecisionSolveDrafts = computed(() => manualDecisionDrafts.value.filter((item) => !manualDecisionIsCompact(item)))
  const manualDecisionHasEditableRows = computed(() => manualDecisionDrafts.value.some((item) => item.editable))
  const manualDecisionComplete = computed(() => manualDecisionHasEditableRows.value && manualDecisionDrafts.value
    .filter((item) => item.editable)
    .every((item) => ['correct', 'partial', 'wrong'].includes(item.result)))

  function reviewStorageKey() {
    const teacherId = auth.user?.id
    const mode = anonymous.value ? ':anonymous' : ''
    return teacherId ? `${REVIEW_BATCH_STORAGE_PREFIX}${mode}:${teacherId}` : ''
  }

  function rememberReviewBatch(id) {
    const key = reviewStorageKey()
    if (key && id) localStorage.setItem(key, String(id))
  }

  function forgetReviewBatch() {
    const key = reviewStorageKey()
    if (key) localStorage.removeItem(key)
  }

  function clearReviewDisplay() {
    summary.value = null
    progressList.value = []
    reviewRestoredLabel.value = ''
    reviewRevision.value = 0
    reviewArchived.value = false
    gradeError.value = ''
    coursewareRecommendations.value = []
    coursewareRecommendationState.value = 'idle'
    coursewareRecommendationQuery.value = {}
    coursewareRecommendationKey.value = ''
    resetBatchReport()
  }

  const REVIEW_COMPLETE_STATUSES = new Set(['review_ready', 'voided', 'archived', 'done'])

  function hydrateReviewState(payload) {
    const rows = Array.isArray(payload?.results) ? payload.results : []
    if (!payload?.batch_id || !rows.length) return false
    // Pipeline grading saves rows while a batch is still being graded; those
    // partial rows must never be shown as a finished review.
    const status = String(payload.status || '')
    if (status && !REVIEW_COMPLETE_STATUSES.has(status)) return false
    const payloadIsAnonymous = String(payload.identity_mode || 'identified') === 'anonymous'
    if (payloadIsAnonymous !== anonymous.value) return false
    batchId.value = String(payload.batch_id)
    progressList.value = rows
    form.category = rows[0]?.category || 'daily'
    form.exam_name = rows[0]?.exam_name || ''
    gradeTotal.value = Number(payload.summary?.total ?? rows.length)
    summary.value = {
      total: Number(payload.summary?.total ?? rows.length),
      avg_score: payload.summary?.avg_score ?? null,
      grading_elapsed_seconds: payload.summary?.grading_elapsed_seconds ?? null,
    }
    reviewRevision.value = Number(payload.review_revision || 0)
    applyBatchReport(payload.report || {})
    reviewArchived.value = !rows.some((row) => row.disposition === 'active')
    manualDecisionDialogVisible.value = false
    manualDecisionRow.value = null
    manualDecisionDrafts.value = []
    activeStep.value = 0
    return true
  }

  async function resumeReviewBatch() {
    const key = reviewStorageKey()
    const savedBatchId = key ? localStorage.getItem(key) : ''
    if (!savedBatchId) return false
    try {
      const payload = await api.grade.reviewGet(savedBatchId)
      if (!hydrateReviewState(payload)) {
        const status = String(payload?.status || '')
        if (payload?.batch_id && status && !REVIEW_COMPLETE_STATUSES.has(status)) {
          // An unfinished batch resumes through recognition/grading recovery.
          forgetReviewBatch()
          rememberOrientationBatch(String(payload.batch_id))
          return false
        }
        throw new Error('审阅批次没有可恢复的结果')
      }
      reviewRestoredLabel.value = `已恢复历史批次 ${savedBatchId}`
      startBatchReportPolling()
      if (reviewArchived.value) forgetReviewBatch()
      return true
    } catch (err) {
      // Only a definitive ownership/expiry response invalidates a saved pointer.
      // Network and server failures must retain it for a later recovery attempt.
      const status = Number(err?.response?.status || err?.status || 0)
      if ([401, 403, 404].includes(status)) forgetReviewBatch()
      return false
    }
  }

  function maybeForgetTerminalReview() {
    if (!hasActiveReviewRows.value) forgetReviewBatch()
  }

  function applyReviewToProgressRow(review) {
    const paperId = reviewPaperId(review)
    const row = progressList.value.find((item) => reviewPaperId(item) === paperId)
    if (!row) return
    const result = review.result || {}
    Object.assign(row, {
      disposition: review.disposition || row.disposition,
      category: review.category || row.category || form.category,
      exam_name: review.exam_name || row.exam_name || '',
      score: result.score ?? null,
      max_score: result.max_score ?? null,
      correct_rate: result.correct_rate ?? null,
      correct: Number(result.correct_count || 0),
      partial: Number(result.partial_count || 0),
      wrong: Number(result.wrong_count || 0),
      needs_review: Number(result.needs_review_count || 0),
      total: Number(result.total || 0),
      details: result.details || [],
    })
  }

  function openManualDecision(row) {
    const groupId = String(row.review_group_id || '')
    const groupRows = progressList.value.filter((item) => (
      groupId ? String(item.review_group_id || '') === groupId : reviewPaperId(item) === reviewPaperId(row)
    ))
    const details = groupRows.flatMap((groupRow) => (
      Array.isArray(groupRow.details) ? groupRow.details : []
    ).map((detail) => ({
      ...detail,
      paper_id: String(detail.paper_id || reviewPaperId(groupRow)),
      page_index: Number(detail.page_index ?? groupRow.page_index ?? 0),
      editable: groupRow.disposition === 'active',
    })))
    if (!details.length) {
      ElMessage.error('该结果缺少逐题明细，请刷新批改结果后重试')
      return
    }
    manualDecisionRow.value = row
    manualDecisionPages.value = Array.isArray(row.pages) && row.pages.length
      ? row.pages.map((page, index) => ({ ...page, page_index: Number(page.page_index ?? index) }))
      : [{ paper_id: reviewPaperId(row), page_index: 0, filename: row.filename || '' }]
    manualDecisionPageIndex.value = Math.min(
      manualDecisionPages.value.length - 1,
      Math.max(0, Number(row.page_index || 0)),
    )
    manualDecisionDrafts.value = details.map((detail) => {
      const verdict = ['correct', 'partial', 'wrong'].includes(detail.result) ? detail.result : ''
      return {
        question_no: String(detail.question_no || detail.question_number || ''),
        paper_id: String(detail.paper_id || ''),
        page_index: Number(detail.page_index || 0),
        editable: detail.editable !== false,
        question: detail.question || detail.question_text || '',
        question_type: detail.question_type || 'solve',
        student_answer: detail.student_answer || '',
        error_type: detail.error_type || '',
        answer_state: detail.answer_state || '',
        slot_identity_error: detail.slot_identity_error || '',
        answer_kind: detail.answer_kind || 'non_graphic',
        correct_answer: detail.correct_answer || detail.standard_answer || '',
        drawing_assessment: detail.drawing_assessment || null,
        text_assessment: detail.text_assessment || null,
        answer_bboxes: detail.answer_bboxes || detail.answer_bbox || detail.student_answer_bbox || null,
        model_result: detail.result || 'needs_review',
        max_score: detail.max_score ?? null,
        awarded_score: detail.awarded_score ?? null,
        result: verdict,
        note: '',
      }
    })
    manualDecisionShowPaper.value = true
    manualDecisionDialogVisible.value = true
    void loadManualDecisionImage()
  }

  function resetManualDecisionImage() {
    manualDecisionImageRequestId += 1
    if (manualDecisionImageUrl.value) URL.revokeObjectURL(manualDecisionImageUrl.value)
    manualDecisionImageUrl.value = ''
    manualDecisionImageLoading.value = false
    manualDecisionImageError.value = ''
  }

  async function loadManualDecisionImage() {
    resetManualDecisionImage()
    const requestId = manualDecisionImageRequestId
    const paperId = String(manualDecisionPages.value[manualDecisionPageIndex.value]?.paper_id || '')
    if (!batchId.value || !paperId) {
      manualDecisionImageError.value = '该结果缺少试卷标识，无法加载批阅图'
      return
    }
    manualDecisionImageLoading.value = true
    try {
      const decisions = manualDecisionDrafts.value
        .filter((item) => item.paper_id === paperId && item.editable && item.result && item.result !== item.model_result)
        .map((item) => ({ question_no: item.question_no, result: item.result }))
      const imageUrl = decisions.length
        ? await api.grade.reviewImage(batchId.value, paperId, decisions)
        : await api.grade.reviewImage(batchId.value, paperId)
      if (requestId !== manualDecisionImageRequestId) {
        URL.revokeObjectURL(imageUrl)
        return
      }
      manualDecisionImageUrl.value = imageUrl
    } catch (err) {
      if (requestId === manualDecisionImageRequestId) {
        manualDecisionImageError.value = reviewErrorMessage(err, '批阅图加载失败，请重试')
      }
    } finally {
      if (requestId === manualDecisionImageRequestId) manualDecisionImageLoading.value = false
    }
  }

  function changeManualDecisionPage(delta) {
    jumpToManualDecisionPage(manualDecisionPageIndex.value + delta)
  }

  function jumpToManualDecisionPage(index, refresh = false) {
    const target = Math.min(manualDecisionPages.value.length - 1, Math.max(0, Number(index) || 0))
    if (!refresh && target === manualDecisionPageIndex.value && manualDecisionImageUrl.value) return
    manualDecisionPageIndex.value = target
    void loadManualDecisionImage()
  }

  function reviewResultText(result) {
    return ({ correct: '正确', partial: '部分正确', wrong: '错误', needs_review: '待复核' })[result] || '未判定'
  }

  function manualDecisionQuestionType(item) {
    const type = String(item?.question_type || '').trim().toLowerCase()
    return ['choice', 'fill', 'solve'].includes(type) ? type : 'solve'
  }

  function manualDecisionIsCompact(item) {
    return ['choice', 'fill'].includes(manualDecisionQuestionType(item))
  }

  function manualDecisionTypeLabel(item) {
    return ({ choice: '选择题', fill: '填空题', solve: '简答题' })[manualDecisionQuestionType(item)]
  }

  function openManualDecisionImage() {
    if (manualDecisionImageUrl.value) {
      window.open(manualDecisionImageUrl.value, '_blank', 'noopener,noreferrer')
    }
  }

  async function saveManualDecision() {
    if (!manualDecisionRow.value || !manualDecisionComplete.value) {
      ElMessage.warning('请为每一道题选择最终判定')
      return
    }
    manualDecisionSaving.value = true
    try {
      const paperIds = [...new Set(manualDecisionDrafts.value
        .filter((item) => item.editable)
        .map((item) => item.paper_id))]
      for (const paperId of paperIds) {
        const res = await api.grade.reviewDecision({
          batch_id: batchId.value,
          paper_id: paperId,
          review_revision: reviewRevision.value,
          decisions: manualDecisionDrafts.value
            .filter((item) => item.editable && item.paper_id === paperId)
            .map((item) => ({
              question_no: item.question_no,
              result: item.result,
              note: item.note,
            })),
        })
        if (!res?.ok || !res.review) throw new Error(res?.detail || '保存人工判定失败')
        applyReviewToProgressRow(res.review)
        reviewRevision.value = Number(res.review_revision || reviewRevision.value)
      }
      manualDecisionDialogVisible.value = false
      startBatchReportPolling()
      ElMessage.success(anonymous.value ? '已保存本页人工判定' : '已保存该学生的人工判定')
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '保存人工判定失败；若结果已更新，请刷新后重试'))
    } finally {
      manualDecisionSaving.value = false
    }
  }

  async function voidOneReview(row) {
    const paperId = reviewPaperId(row)
    if (!paperId) { ElMessage.error('该结果缺少试卷标识，无法作废'); return }
    const targetLabel = anonymous.value ? (row.student_name || '该页面') : (row.student_name || '该学生')
    const archiveHint = anonymous.value ? '该结果会从本批统计中排除。' : '该结果不会进入学生档案。'
    if (!window.confirm(`确定作废“${targetLabel}”的本次批改结果吗？${archiveHint}`)) return
    reviewBusy.value = true
    try {
      const res = await api.grade.reviewVoid({ batch_id: batchId.value, paper_id: paperId, reason: '老师在结果页作废' })
      if (!res?.ok) throw new Error(res?.detail || '作废失败')
      row.disposition = 'voided'
      reviewRevision.value = Number(res.review_revision || reviewRevision.value)
      reviewArchived.value = !hasActiveReviewRows.value
      startBatchReportPolling()
      maybeForgetTerminalReview()
      ElMessage.success(anonymous.value ? '已排除该页面结果' : '已作废该学生结果，不会写入学生档案')
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '作废失败'))
    } finally {
      reviewBusy.value = false
    }
  }

  async function voidAllReviews() {
    if (!window.confirm(anonymous.value
      ? '确定排除本批次所有未排除结果吗？排除后不会计入本批统计。'
      : '确定作废本批次所有未作废结果吗？作废后它们不会进入学生档案。')) return
    reviewBusy.value = true
    try {
      const res = await api.grade.reviewVoidAll({ batch_id: batchId.value })
      if (!res?.ok) throw new Error(res?.detail || '全部作废失败')
      progressList.value.forEach((row) => {
        if (row.disposition === 'active') row.disposition = 'voided'
      })
      reviewRevision.value = Number(res.review_revision || reviewRevision.value)
      reviewArchived.value = !hasActiveReviewRows.value
      startBatchReportPolling()
      maybeForgetTerminalReview()
      ElMessage.success(anonymous.value
        ? `已排除 ${res.voided || 0} 页结果`
        : `已作废 ${res.voided || 0} 份结果`)
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '全部作废失败'))
    } finally {
      reviewBusy.value = false
    }
  }

  async function archiveAllReviews() {
    if (!window.confirm('确认归档所有已完成判定的结果吗？待复核学生会留在本页，不会写入学生档案。')) return
    reviewBusy.value = true
    try {
      const res = await api.grade.reviewArchiveAll({ batch_id: batchId.value })
      if (!res?.ok) throw new Error(res?.detail || '入档失败')
      const archivedIds = new Set(res.archived_paper_ids || [])
      progressList.value.forEach((row) => {
        if (archivedIds.has(reviewPaperId(row))) row.disposition = 'archived'
      })
      reviewRevision.value = Number(res.review_revision || reviewRevision.value)
      reviewArchived.value = !hasActiveReviewRows.value
      maybeForgetTerminalReview()
      ElMessage.success(`已入档 ${res.archived || 0} 人；待复核 ${res.skipped_pending || 0} 人；已作废 ${res.skipped_voided || 0} 人`)
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '全部入档失败'))
    } finally {
      reviewBusy.value = false
    }
  }

  async function downloadStudentErrors(row) {
    const paperId = reviewPaperId(row)
    if (!paperId) { ElMessage.error('该结果缺少试卷标识，无法导出'); return }
    reviewBusy.value = true
    try {
      await api.grade.reviewDownload('/batch-grade/review/export/student', {
        batch_id: batchId.value, paper_id: paperId,
      }, `${row.student_name || '学生'}_错题条.pdf`)
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '错题条导出失败'))
    } finally {
      reviewBusy.value = false
    }
  }

  async function downloadReviewImage(row) {
    const paperId = reviewPaperId(row)
    if (!paperId) { ElMessage.error('该结果缺少试卷标识，无法下载批阅痕迹图'); return }
    reviewBusy.value = true
    let imageUrl = ''
    try {
      imageUrl = await api.grade.reviewImage(batchId.value, paperId)
      const link = document.createElement('a')
      link.href = imageUrl
      link.download = `${row?.student_name || '学生'}_批阅痕迹图.png`
      document.body.appendChild(link)
      link.click()
      link.remove()
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '批阅痕迹图下载失败'))
    } finally {
      if (imageUrl) window.setTimeout(() => URL.revokeObjectURL(imageUrl), 1000)
      reviewBusy.value = false
    }
  }

  function downloadCurrentReviewImage() {
    if (!manualDecisionImageUrl.value || manualDecisionImageLoading.value) return
    const link = document.createElement('a')
    link.href = manualDecisionImageUrl.value
    link.download = `${anonymous.value ? '匿名卷' : (manualDecisionRow.value?.student_name || '学生')}_第${manualDecisionPageIndex.value + 1}页_批阅痕迹.png`
    document.body.appendChild(link)
    link.click()
    link.remove()
  }

  async function downloadAllReviewImages() {
    reviewImagesDownloading.value = true
    try {
      await api.grade.reviewDownload('/batch-grade/review/export/images', {
        batch_id: batchId.value,
      }, '批阅痕迹图.zip', 600000)
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '批阅痕迹图打包失败'))
    } finally {
      reviewImagesDownloading.value = false
    }
  }

  async function downloadAllErrors() {
    reviewBusy.value = true
    try {
      await api.grade.reviewDownload('/batch-grade/review/export/all-errors', {
        batch_id: batchId.value,
      }, '全体学生错题条.zip')
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '全部错题导出失败'))
    } finally {
      reviewBusy.value = false
    }
  }

  async function downloadReviewSummary() {
    reviewBusy.value = true
    try {
      await api.grade.reviewDownload('/batch-grade/review/export/summary', {
        batch_id: batchId.value,
      }, '老师错题汇总.pdf')
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '老师汇总导出失败'))
    } finally {
      reviewBusy.value = false
    }
  }

  return {
    manualDecisionImageRequestId, hasActiveReviewRows, pendingReviewCount,
    resolvedActiveReviewCount, manualDecisionCompactDrafts, manualDecisionSolveDrafts,
    manualDecisionHasEditableRows, manualDecisionComplete, reviewStorageKey, rememberReviewBatch,
    forgetReviewBatch, clearReviewDisplay, REVIEW_COMPLETE_STATUSES, hydrateReviewState,
    resumeReviewBatch, maybeForgetTerminalReview, applyReviewToProgressRow, openManualDecision,
    resetManualDecisionImage, loadManualDecisionImage, changeManualDecisionPage,
    jumpToManualDecisionPage, reviewResultText, manualDecisionQuestionType, manualDecisionIsCompact,
    manualDecisionTypeLabel, openManualDecisionImage, saveManualDecision, voidOneReview,
    voidAllReviews, archiveAllReviews, downloadStudentErrors, downloadReviewImage,
    downloadCurrentReviewImage, downloadAllReviewImages, downloadAllErrors, downloadReviewSummary,
  }
}
