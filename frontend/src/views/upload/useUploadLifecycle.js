/**
 * The 批改 page's lifecycle: guarding a reload or navigation that would interrupt work, the watchers
 * that start recognition automatically, and what mounting and unmounting do.
 *
 * - leaveInterruptsWork marks the route busy; onBeforeRouteLeave and the beforeunload handler ask before
 *   leaving, and abortActiveRequests cancels every running request.
 * - onMounted loads availability and usage, polls availability, and resumes a template recognition, a
 *   review or an orientation batch; onBeforeUnmount releases timers, images and requests.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { setRouteBusy } from '@/utils/routeBusy'

export function useUploadLifecycle(ctx) {
  const {
    anonymous, auth, fileList, imagePreprocessing, imageExtracting, recognizing, grading,
    availabilityPollTimer, recognitionBlocksNewWork, recognizingTemplate, confirmedTemplateId,
    templateDialogVisible, confirmingTemplate, templateCutBusy, form, templateRecognizeController,
    templateConfirmController, templateCutController, templateCutRunToken, recognizeController,
    gradeController, clearRecognitionAutoResume, revokeIdentityImageUrls,
    resumeTemplateRecognitionCheckpoint, stopTemplateCutPolling, revokeTemplateCutPreviews,
    resetOrientationReview, maybeAutoStartRecognition, loadDailyUsage, stopGradingClock,
    resumeOrientationBatch, stopBatchReportPolling, resumeReviewBatch, resetManualDecisionImage,
    refreshAvailability,
  } = ctx

  // Leaving now would abort an in-flight request (guard below); the usage tour
  // reads this so it never triggers the leave-confirm on the teacher's behalf.
  const leaveInterruptsWork = computed(() => recognizing.value || grading.value || recognizingTemplate.value || confirmingTemplate.value || templateCutBusy.value)
  watch(leaveInterruptsWork, (busy) => setRouteBusy('upload', busy), { immediate: true })

  onBeforeRouteLeave((to, from, next) => {
    if (leaveInterruptsWork.value) {
      if (confirm('任务正在处理中，离开会中断当前请求。确定离开吗？')) {
        abortActiveRequests()
        next()
      } else next(false)
    } else next()
  })

  function onBeforeUnload(event) {
    if (!leaveInterruptsWork.value) return
    event.preventDefault()
    event.returnValue = ''
  }

  function abortActiveRequests() {
    clearRecognitionAutoResume()
    templateRecognizeController.value?.abort()
    templateConfirmController.value?.abort()
    templateCutController.value?.abort()
    stopTemplateCutPolling()
    templateCutRunToken.value += 1
    templateCutBusy.value = false
    recognizeController.value?.abort()
    gradeController.value?.abort()
  }

  watch(
    () => [
      confirmedTemplateId.value,
      fileList.value.length,
      imagePreprocessing.value,
      imageExtracting.value,
      recognizing.value,
      grading.value,
      auth.currentClassId,
      form.category,
      form.exam_name,
      templateDialogVisible.value,
      recognitionBlocksNewWork.value,
      anonymous.value,
    ],
    maybeAutoStartRecognition,
  )

  watch(() => form.category, (category) => {
    if (category !== 'exam') form.exam_name = ''
  })

  onMounted(async () => {
    window.addEventListener('beforeunload', onBeforeUnload)
    await Promise.all([refreshAvailability(), loadDailyUsage()])
    availabilityPollTimer.value = window.setInterval(refreshAvailability, 15000)
    const resumedTemplate = await resumeTemplateRecognitionCheckpoint()
    if (!resumedTemplate) {
      const resumedReview = await resumeReviewBatch()
      if (!resumedReview) await resumeOrientationBatch()
    }
  })
  onBeforeUnmount(() => {
    setRouteBusy('upload', false)
    window.removeEventListener('beforeunload', onBeforeUnload)
    if (availabilityPollTimer.value) window.clearInterval(availabilityPollTimer.value)
    stopGradingClock()
    stopBatchReportPolling()
    revokeIdentityImageUrls()
    resetManualDecisionImage()
    resetOrientationReview()
    revokeTemplateCutPreviews()
    abortActiveRequests()
  })

  return {
    leaveInterruptsWork, onBeforeUnload, abortActiveRequests,
  }
}
