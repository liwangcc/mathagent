// Standard answers still being generated while the teacher reviews the template.
//
// The server delivers a page before its slowest question is solved. While any
// question of the open template waits for its answer, the page reads the
// template again every few seconds and takes what has arrived; saving the
// template is held back until nothing is waiting (the server refuses it too).
import { computed } from 'vue'
import api from '@/api'
import { templateAnswerPending } from '@/utils/templateReview'
import { takeTemplateAnswers } from '@/utils/templateLateAnswers'

const READ_EVERY_MS = 3000
// The template is gone or no longer this teacher's: reading it again cannot help.
const GIVE_UP_STATUSES = [401, 403, 404]

export function useTemplateLateAnswers(ctx) {
  const { templatePages, pendingTemplateId } = ctx

  const templatePendingAnswerEntries = computed(() => templatePages.value.flatMap((page, pageIndex) =>
    (page.questions || []).map((question, questionIndex) => ({ pageIndex, questionIndex, question }))
      .filter(({ question }) => !question._pendingRemoval && templateAnswerPending(question))))
  const templateAnswersPending = computed(() => templatePendingAnswerEntries.value.length > 0)

  let timer = null
  let following = 0

  function stopFollowingTemplateAnswers() {
    following += 1
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  }

  /** Read the open template again while any of its answers is still being generated. */
  function followTemplateAnswers() {
    stopFollowingTemplateAnswers()
    if (!templateAnswersPending.value) return
    const templateId = pendingTemplateId.value
    const mine = following
    const current = () => mine === following && templateId === pendingTemplateId.value
    const later = () => { timer = window.setTimeout(read, READ_EVERY_MS) }
    async function read() {
      timer = null
      let state = null
      try {
        state = await api.grade.templateRecognitionState(templateId)
      } catch (error) {
        if (GIVE_UP_STATUSES.includes(error?.status)) return
      }
      if (!current()) return
      if (Array.isArray(state?.pages)) takeTemplateAnswers(templatePages.value, state.pages)
      if (templateAnswersPending.value && state?.status !== 'failed') later()
    }
    later()
  }

  return {
    templatePendingAnswerEntries, templateAnswersPending, followTemplateAnswers, stopFollowingTemplateAnswers,
  }
}
