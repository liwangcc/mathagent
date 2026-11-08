/**
 * The 添加学生 dialog: typing students in, importing an Excel roster (a dry-run preview first) or recognizing
 * a roster image, then saving. columnText describes the columns an import recognized; showStringList shows
 * the rows the server reported back.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'

export function useStudentImport(ctx) {
  const {
    auth, activeClassId, captureClassScope, loadStudents,
  } = ctx

  const studentDialogVisible = ref(false)
  const studentImportMode = ref('manual')
  const manualStudents = ref([{ student_no: '', exam_no: '', name: '', phone: '' }])
  const studentExcelFile = ref(null)
  const studentExcelPreview = ref(null)
  const studentImageFile = ref(null)
  const recognizedStudents = ref([])
  const recognizing = ref(false)
  const savingStudents = ref(false)

  const COLUMN_LABELS = { no: '学号', exam_no: '准考证号', name: '姓名', phone: '家长手机', score: '成绩', exam: '考试名' }
  function columnText(columns) {
    const entries = Object.entries(columns || {})
    return entries.length
      ? entries.map(([key, header]) => `${COLUMN_LABELS[key] || key}←“${header}”`).join('，')
      : '未识别'
  }

  function onStudentExcelChange(file) {
    studentExcelFile.value = file.raw
    studentExcelPreview.value = null
  }

  function onStudentImageChange(file) {
    studentImageFile.value = file.raw
    recognizedStudents.value = []
  }

  async function recognizeImage() {
    if (recognizing.value) return
    if (!studentImageFile.value) {
      ElMessage.warning('请先选择名单图片')
      return
    }
    recognizing.value = true
    const { classId, current } = captureClassScope()
    try {
      const fd = new FormData()
      fd.append('file', studentImageFile.value)
      const data = await api.classroom.importStudentsImage(classId, fd)
      if (!current()) return
      recognizedStudents.value = data.recognized || []
      if (!recognizedStudents.value.length) {
        ElMessage.warning('未识别到学生')
      }
    } catch {
      if (current()) recognizedStudents.value = []
    } finally {
      if (current()) recognizing.value = false
    }
  }

  async function saveStudents() {
    if (!activeClassId.value) return
    const { classId, current } = captureClassScope()
    savingStudents.value = true
    try {
      if (studentImportMode.value === 'excel') {
        if (!studentExcelFile.value) {
          ElMessage.warning('请先选择 Excel 文件')
          return
        }
        const fd = new FormData()
        fd.append('file', studentExcelFile.value)
        if (!studentExcelPreview.value) {
          // First pass is a dry run: show column mapping and per-row results.
          fd.append('dry_run', 'true')
          const preview = await api.classroom.importStudentsExcel(classId, fd)
          if (current()) studentExcelPreview.value = preview
          return
        }
        const res = await api.classroom.importStudentsExcel(classId, fd)
        if (!current()) return
        ElMessage.success(`导入完成：新增 ${res.added || 0}，跳过 ${res.skipped || 0}`)
        showStringList([...(res.errors || []), ...(res.warnings || [])], '导入提示')
      } else {
        const list = studentImportMode.value === 'image'
          ? recognizedStudents.value
          : manualStudents.value
        const clean = list
          .map((item) => ({
            student_no: item.student_no?.trim() || undefined,
            exam_no: item.exam_no?.trim() || undefined,
            name: item.name?.trim(),
            phone: item.phone?.trim() || undefined,
          }))
          .filter((item) => item.name)  // 只要求姓名，学号选填
        if (!clean.length) {
          ElMessage.warning('请至少填写一个学生的姓名')
          return
        }
        const res = await api.classroom.addStudents(classId, clean)
        if (!current()) return
        ElMessage.success(`保存完成：新增 ${res.added || 0}，跳过 ${res.skipped || 0}`)
        showStringList(res.errors, '保存错误')
      }
      studentDialogVisible.value = false
      resetStudentDialog()
      await auth.fetchClasses()
      if (!current()) return
      await loadStudents()
    } catch {
      // handled by interceptor
    } finally {
      if (current()) savingStudents.value = false
    }
  }

  function resetStudentDialog() {
    studentImportMode.value = 'manual'
    manualStudents.value = [{ student_no: '', exam_no: '', name: '', phone: '' }]
    studentExcelFile.value = null
    studentExcelPreview.value = null
    studentImageFile.value = null
    recognizedStudents.value = []
  }

  function showStringList(list, title) {
    if (!Array.isArray(list) || !list.length) return
    ElMessageBox.alert(
      `<div class="message-list">${list.map((item) => `<p>${escapeHtml(item)}</p>`).join('')}</div>`,
      title,
      { dangerouslyUseHTMLString: true, type: 'warning', confirmButtonText: '知道了' }
    )
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
  }

  return {
    studentDialogVisible, studentImportMode, manualStudents, studentExcelFile, studentExcelPreview,
    studentImageFile, recognizedStudents, recognizing, savingStudents, COLUMN_LABELS, columnText,
    onStudentExcelChange, onStudentImageChange, recognizeImage, saveStudents, resetStudentDialog,
    showStringList, escapeHtml,
  }
}
