/**
 * The 更多 menu: its commands (exam scores, the heatmap, reports, student submissions) and the student
 * submissions dialog it opens.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { ref } from 'vue'

export function useMoreMenu(ctx) {
  const {
    router, examDialogVisible, openHeatmap,
  } = ctx

  const submissionDialogVisible = ref(false)

  function onMoreCommand(command) {
    if (command === 'exam') {
      examDialogVisible.value = true
    } else if (command === 'heatmap') {
      openHeatmap()
    } else if (command === 'reports') {
      router.push('/reports')
    } else if (command === 'submissions') {
      submissionDialogVisible.value = true
    }
  }

  return {
    submissionDialogVisible, onMoreCommand,
  }
}
