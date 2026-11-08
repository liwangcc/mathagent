/**
 * Importing exam scores from Excel: the dialog, a dry-run preview that is dropped when the file, the exam
 * name or the date changes, and the import itself.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'

export function useExamImport(ctx) {
  const {
    captureClassScope, loadStudents, showStringList,
  } = ctx

  const examDialogVisible = ref(false)
  const examName = ref('')
  const examFile = ref(null)
  const examDate = ref('')
  const examPreview = ref(null)
  const importingExam = ref(false)
  // A preview only describes the exact file, name and date it was built from.
  watch([examName, examDate], () => { examPreview.value = null })

  const examIssues = computed(() => {
    const preview = examPreview.value
    if (!preview) return []
    return [...(preview.unmatched || []), ...(preview.skipped || []), ...(preview.duplicates || [])].slice(0, 50)
  })

  function onExamFileChange(file) {
    examFile.value = file.raw
    examPreview.value = null
  }

  async function importExamScores() {
    if (!examFile.value) {
      ElMessage.warning('请先选择成绩 Excel')
      return
    }
    importingExam.value = true
    const { classId, current } = captureClassScope()
    try {
      const fd = new FormData()
      fd.append('file', examFile.value)
      if (examName.value.trim()) fd.append('exam_name', examName.value.trim())
      if (examDate.value) fd.append('exam_date', examDate.value)
      if (!examPreview.value) {
        // First pass is a dry run so the teacher can check matches before writing.
        fd.append('dry_run', 'true')
        const preview = await api.classroom.importExamScores(classId, fd)
        if (current()) examPreview.value = preview
        return
      }
      const res = await api.classroom.importExamScores(classId, fd)
      if (!current()) return
      const examLabel = (res.exam_names || []).join('、') || res.exam_name || '考试'
      ElMessage.success(`导入完成：${examLabel} 共 ${res.matched || 0} 人`)
      showStringList([...(res.unmatched || []), ...(res.skipped || []), ...(res.duplicates || [])], '请核对以下行')
      examDialogVisible.value = false
      examFile.value = null
      examName.value = ''
      examDate.value = ''
      examPreview.value = null
      await loadStudents()
    } catch {
      // handled by interceptor
    } finally {
      if (current()) importingExam.value = false
    }
  }

  return {
    examDialogVisible, examName, examFile, examDate, examPreview, importingExam, examIssues,
    onExamFileChange, importExamScores,
  }
}
