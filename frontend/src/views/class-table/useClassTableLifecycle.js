/**
 * The page's lifecycle: following the route's class id and its ?add=1 query, and reloading everything when
 * the signed-in account or the education stage changes (reloadProfileScope, also run on mount).
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { onMounted, watch } from 'vue'

export function useClassTableLifecycle(ctx) {
  const {
    route, auth, profileScope, activeClassId, loading, classDialogVisible, savingClass,
    studentDialogVisible, recognizing, savingStudents, editStudentVisible, savingEdit,
    examDialogVisible, examName, examFile, importingExam, heatmapVisible, heatmapLoading,
    heatmapTree, studentsGeneration, heatmapGeneration, classScopeGeneration, resolveInitialClass,
    loadStudents, loadAll, resetStudents, consumeAddClassQuery, resetStudentDialog,
  } = ctx

  watch(() => route.params.classId, async () => {
    const scope = profileScope.value
    const id = Number(route.params.classId)
    if (id && id !== activeClassId.value) {
      activeClassId.value = id
      await auth.switchClass(id)
      if (scope !== profileScope.value) return
      await loadStudents()
    } else if (!id && route.path === '/students') {
      activeClassId.value = resolveInitialClass()
      await loadStudents()
    }
  })

  watch(() => route.query.add, (value) => {
    if (value === '1') {
      consumeAddClassQuery()
    }
  })

  async function reloadProfileScope() {
    classScopeGeneration.value += 1
    studentsGeneration.value += 1
    heatmapGeneration.value += 1
    resetStudents()
    activeClassId.value = null
    loading.value = false
    heatmapTree.value = []
    heatmapVisible.value = false
    heatmapLoading.value = false
    classDialogVisible.value = false
    studentDialogVisible.value = false
    editStudentVisible.value = false
    examDialogVisible.value = false
    recognizing.value = false
    savingClass.value = false
    savingStudents.value = false
    savingEdit.value = false
    importingExam.value = false
    examFile.value = null
    examName.value = ''
    resetStudentDialog()
    const scope = profileScope.value
    await loadAll()
    if (scope !== profileScope.value) return
    consumeAddClassQuery()
  }

  watch(profileScope, reloadProfileScope, { flush: 'sync' })
  onMounted(reloadProfileScope)

  return {
    reloadProfileScope,
  }
}
