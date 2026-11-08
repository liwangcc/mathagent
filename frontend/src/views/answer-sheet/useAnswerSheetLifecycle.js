/**
 * Watchers that keep the layout summary and the page images in step with the session, and what mounting
 * and unmounting do (availability poll, restoring the saved session, releasing images and streams).
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { onBeforeUnmount, onMounted, watch } from 'vue'
import api from '@/api'

export function useAnswerSheetLifecycle(ctx) {
  const {
    STORAGE_KEY, auth, route, busy, availabilityPollTimer, restoreError, session, answerRegions,
    identityRegions, layoutSummary, currentPageId, layoutImageUrl, layoutImageReady,
    layoutImageError, currentReviewPaperId, studentPageImageUrl, studentPageImageError,
    studentImageRequestId, answerSheetStreamController, currentPage, applySession, summarizeLayout,
    refreshAvailability, loadBatchChoices, loadTemplateCodeHistory,
  } = ctx

  watch([answerRegions, identityRegions], () => { if (session.value?.status === 'layout_draft') layoutSummary.value = summarizeLayout() }, { deep: true })
  watch([() => session.value?.session_id, currentPageId, () => currentPage.value?.working_sha256], async ([sessionId, pageId], _previous, onCleanup) => {
    let cancelled = false
    onCleanup(() => { cancelled = true })
    if (layoutImageUrl.value) URL.revokeObjectURL(layoutImageUrl.value)
    layoutImageUrl.value = ''
    layoutImageReady.value = false
    layoutImageError.value = ''
    if (!sessionId || !pageId) return
    try {
      const nextUrl = await api.answerSheet.layoutPageImage(sessionId, pageId)
      if (cancelled) {
        URL.revokeObjectURL(nextUrl)
        return
      }
      layoutImageUrl.value = nextUrl
    } catch {
      if (!cancelled) layoutImageError.value = '答题卡图像读取失败，请重新打开当前页。'
    }
  })
  watch([() => session.value?.session_id, currentReviewPaperId], async ([sessionId, paperId]) => {
    const requestId = ++studentImageRequestId.value
    if (studentPageImageUrl.value) URL.revokeObjectURL(studentPageImageUrl.value)
    studentPageImageUrl.value = ''
    studentPageImageError.value = ''
    if (!sessionId || !paperId) return
    try {
      const nextUrl = await api.answerSheet.studentPageImage(sessionId, paperId)
      if (requestId !== studentImageRequestId.value) {
        URL.revokeObjectURL(nextUrl)
        return
      }
      studentPageImageUrl.value = nextUrl
    } catch {
      if (requestId === studentImageRequestId.value) studentPageImageError.value = '当前答题卡页面读取失败，请重试。'
    }
  })
  onMounted(async () => {
    void refreshAvailability()
    void loadTemplateCodeHistory()
    void loadBatchChoices()
    availabilityPollTimer.value = window.setInterval(refreshAvailability, 15000)
    if (!auth.classes.length) void auth.fetchClasses().catch(() => {})
    const saved = String(route.query.session || localStorage.getItem(STORAGE_KEY) || '')
    if (!saved) return
    busy.value = true
    try {
      applySession(await api.answerSheet.getSession(saved))
      restoreError.value = route.query.session ? '已打开该条正式记录关联的答题卡原卷。' : '已恢复上次未完成的答题卡会话。'
    } catch {
      if (!route.query.session) localStorage.removeItem(STORAGE_KEY)
      restoreError.value = '答题卡会话无法恢复，可能已删除或不属于当前账号。'
    } finally {
      busy.value = false
    }
  })
  onBeforeUnmount(() => {
    if (availabilityPollTimer.value) window.clearInterval(availabilityPollTimer.value)
    if (answerSheetStreamController.value) answerSheetStreamController.value.abort()
    studentImageRequestId.value += 1
    if (layoutImageUrl.value) URL.revokeObjectURL(layoutImageUrl.value)
    if (studentPageImageUrl.value) URL.revokeObjectURL(studentPageImageUrl.value)
  })

  return {

  }
}
