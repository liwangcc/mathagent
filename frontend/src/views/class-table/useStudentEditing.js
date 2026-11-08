/**
 * Actions on one student: opening the detail page, the edit dialog with the student's submission password
 * (read, set, clear), and removing the student from the class.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'

export function useStudentEditing(ctx) {
  const {
    router, auth, captureClassScope, loadStudents,
  } = ctx

  const editStudentVisible = ref(false)
  const editStudentForm = ref({ id: null, name: '', student_no: '', exam_no: '', phone: '' })
  const savingEdit = ref(false)
  const submitPassword = reactive({ studentId: null, set: false, loading: false, saving: false, value: '' })

  function openStudent(row) {
    router.push({ path: `/student/${row.id}/detail` })
  }

  function editStudent(row) {
    editStudentForm.value = {
      id: row.id,
      name: row.name || '',
      student_no: row.student_no || '',
      exam_no: row.exam_no || '',
      phone: row.phone || '',
    }
    editStudentVisible.value = true
    loadSubmitPassword(row.id)
  }

  async function loadSubmitPassword(studentId) {
    const { classId, current } = captureClassScope()
    Object.assign(submitPassword, { studentId, set: false, loading: true, saving: false, value: '' })
    try {
      const state = await api.submissions.passwordState(classId, studentId)
      if (current() && submitPassword.studentId === studentId) submitPassword.set = Boolean(state.set)
    } catch {
      // The API layer already reported the failure; the field stays usable.
    } finally {
      if (submitPassword.studentId === studentId) submitPassword.loading = false
    }
  }

  async function saveSubmitPassword() {
    const { classId, current } = captureClassScope()
    const studentId = submitPassword.studentId
    const password = submitPassword.value.trim()
    if (password.length < 4) {
      ElMessage.warning('提交密码至少 4 位')
      return
    }
    submitPassword.saving = true
    try {
      await api.submissions.setPassword(classId, studentId, password)
      if (!current() || submitPassword.studentId !== studentId) return
      Object.assign(submitPassword, { set: true, value: '' })
      ElMessage.success('提交密码已设置，请单独告诉该学生')
    } catch {
      // The API layer already reported the failure.
    } finally {
      submitPassword.saving = false
    }
  }

  async function clearSubmitPassword() {
    const { classId, current } = captureClassScope()
    const studentId = submitPassword.studentId
    submitPassword.saving = true
    try {
      await api.submissions.clearPassword(classId, studentId)
      if (!current() || submitPassword.studentId !== studentId) return
      Object.assign(submitPassword, { set: false, value: '' })
      ElMessage.success('已清除提交密码，该学生提交时不再需要密码')
    } catch {
      // The API layer already reported the failure.
    } finally {
      submitPassword.saving = false
    }
  }

  async function saveEditStudent() {
    const { classId, current } = captureClassScope()
    const form = editStudentForm.value
    if (!form.name.trim()) {
      ElMessage.warning('姓名不能为空')
      return
    }
    savingEdit.value = true
    try {
      await api.classroom.updateStudent(classId, form.id, {
        name: form.name.trim(),
        student_no: form.student_no.trim(),
        exam_no: form.exam_no.trim(),
        phone: form.phone.trim(),
      })
      if (!current()) return
      ElMessage.success('保存成功')
      editStudentVisible.value = false
      await loadStudents()
    } catch (e) {
      if (current()) ElMessage.error(e.message || '保存失败')
    } finally {
      if (current()) savingEdit.value = false
    }
  }

  async function removeStudent(row) {
    const { classId, current } = captureClassScope()
    try {
      await api.classroom.removeStudent(classId, row.id)
      if (!current()) return
      ElMessage.success('已移出班级')
      await auth.fetchClasses()
      if (!current()) return
      await loadStudents()
    } catch {
      // handled by interceptor
    }
  }

  return {
    editStudentVisible, editStudentForm, savingEdit, submitPassword, openStudent, editStudent,
    loadSubmitPassword, saveSubmitPassword, clearSubmitPassword, saveEditStudent, removeStudent,
  }
}
