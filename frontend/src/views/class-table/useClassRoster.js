/**
 * The class page's roster: the route, the router and the signed-in teacher, the current class with its
 * students and their coverage figures, and loading them (resolveInitialClass, loadClassesIfNeeded,
 * loadStudents, loadAll). captureClassScope lets a request check, when it answers, that the class and the
 * account are still the ones it was sent for; toggleObservation turns a student's observation mode on or off.
 *
 * Moved from views/ClassTable.vue; the page passes nothing in (see ClassTable.vue).
 */
import { computed, reactive, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { normalizeEducationStage } from '@/utils/educationStage'
import { useAuthStore } from '@/stores/auth'

export function useClassRoster(ctx) {
  const route = useRoute()
  const router = useRouter()
  const auth = useAuthStore()
  const educationStage = computed(() => normalizeEducationStage(auth.user?.education_stage))
  const profileScope = computed(() => JSON.stringify([auth.user?.id, auth.token, educationStage.value]))

  const activeClassId = ref(null)
  const className = ref('未选择班级')
  const students = ref([])
  const coverage = reactive({ class_size: 0, effective_students: 0, observation_students: 0 })
  const loading = ref(false)

  const studentsGeneration = shallowRef(0)

  const classScopeGeneration = shallowRef(0)

  function captureClassScope() {
    const scope = profileScope.value
    const classId = activeClassId.value
    const generation = classScopeGeneration.value
    return { classId, current: () => scope === profileScope.value && classId === activeClassId.value && generation === classScopeGeneration.value }
  }

  function resolveInitialClass() {
    const routeClassId = Number(route.params.classId)
    if (routeClassId) return routeClassId
    if (auth.currentClassId) return auth.currentClassId
    return auth.classes[0]?.id || null
  }

  async function loadClassesIfNeeded() {
    const scope = profileScope.value
    if (!auth.classes.length) {
      await auth.fetchClasses()
    }
    if (scope !== profileScope.value) return
    if (!activeClassId.value) {
      activeClassId.value = resolveInitialClass()
    }
    if (activeClassId.value && auth.currentClassId !== activeClassId.value) {
      await auth.switchClass(activeClassId.value)
    }
  }

  async function loadStudents() {
    const scope = profileScope.value
    const classId = activeClassId.value
    const generation = ++studentsGeneration.value
    const current = () => scope === profileScope.value && classId === activeClassId.value && generation === studentsGeneration.value
    resetStudents()
    if (!classId || !auth.token || !educationStage.value) {
      loading.value = false
      return
    }
    loading.value = true
    try {
      const stage = educationStage.value
      const data = await api.classroom.students(classId, { education_stage: stage })
      if (!current()) return
      students.value = data.students || []
      Object.assign(coverage, data.coverage || {
        class_size: students.value.length,
        effective_students: students.value.filter((item) => !item.observation_mode).length,
        observation_students: students.value.filter((item) => item.observation_mode).length,
      })
      className.value = data.class_name || auth.classes.find((c) => c.id === classId)?.class_name || '当前班级'
    } catch {
      if (current()) resetStudents()
    } finally {
      if (current()) loading.value = false
    }
  }

  async function loadAll() {
    const scope = profileScope.value
    if (!auth.token || !educationStage.value) return
    await loadClassesIfNeeded()
    if (scope !== profileScope.value) return
    await loadStudents()
  }

  function resetStudents() {
    students.value = []
    className.value = '未选择班级'
    Object.assign(coverage, { class_size: 0, effective_students: 0, observation_students: 0 })
  }

  async function toggleObservation(row) {
    const { classId, current } = captureClassScope()
    try {
      if (row.observation_mode) {
        await ElMessageBox.confirm(
          '恢复后仅新产生的数据重新纳入统计，历史观察期数据不会回填。',
          `恢复 ${row.name} 的统计`,
          { type: 'warning', confirmButtonText: '确认恢复' },
        )
        if (!current()) return
        await api.classroom.setObservation(classId, row.id, {
          enabled: false,
          reason: row.observation_reason || '教师结束观察',
          note: '教师从班级表恢复统计',
        })
        if (!current()) return
        ElMessage.success('已恢复；后续新数据重新纳入统计')
      } else {
        const { value } = await ElMessageBox.prompt(
          '观察期仍正常批阅并保留结果，但不进入班级统计、待订正队列或家长报告。请输入原因。',
          `将 ${row.name} 设为观察模式`,
          { inputPattern: /\S+/, inputErrorMessage: '必须填写原因', confirmButtonText: '启用观察' },
        )
        if (!current()) return
        await api.classroom.setObservation(classId, row.id, {
          enabled: true,
          reason: value.trim(),
          note: '教师从班级表启用',
        })
        if (!current()) return
        ElMessage.success('观察模式已启用')
      }
      await loadStudents()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close') throw error
    }
  }

  return {
    route, router, auth, educationStage, profileScope, activeClassId, className, students, coverage,
    loading, studentsGeneration, classScopeGeneration, captureClassScope, resolveInitialClass,
    loadClassesIfNeeded, loadStudents, loadAll, resetStudents, toggleObservation,
  }
}
