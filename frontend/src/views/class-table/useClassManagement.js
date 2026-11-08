/**
 * Creating, renaming and deleting classes: the class dialog, the ?add=1 query that opens it, and moving to
 * another class after one is created or deleted.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

export function useClassManagement(ctx) {
  const {
    route, router, auth, profileScope, activeClassId, className, classScopeGeneration,
    captureClassScope, loadStudents,
  } = ctx

  const classDialogVisible = ref(false)
  const classFormName = ref('')
  const editingClassId = ref(null)
  const savingClass = ref(false)

  function openCreateClass() {
    editingClassId.value = null
    classFormName.value = ''
    classDialogVisible.value = true
  }

  function consumeAddClassQuery() {
    if (route.query.add !== '1') return
    openCreateClass()
    router.replace({ path: route.path, query: { ...route.query, add: undefined } })
  }

  function openEditClass(cls) {
    editingClassId.value = cls.id
    classFormName.value = cls.class_name || ''
    classDialogVisible.value = true
  }

  function openEditCurrentClass() {
    const cls = auth.classes.find((item) => item.id === activeClassId.value)
    if (cls) {
      openEditClass(cls)
    }
  }

  async function saveClass() {
    const scope = profileScope.value
    const generation = classScopeGeneration.value
    const current = () => scope === profileScope.value && generation === classScopeGeneration.value
    const name = classFormName.value.trim()
    if (!name) {
      ElMessage.warning('请输入班级名称')
      return
    }
    savingClass.value = true
    try {
      if (editingClassId.value) {
        await auth.updateClass(editingClassId.value, name)
        if (!current()) return
        ElMessage.success('班级已保存')
        if (editingClassId.value === activeClassId.value) {
          className.value = name
        }
      } else {
        const res = await auth.createClass(name)
        if (!current()) return
        ElMessage.success('班级已添加')
        const createdId = res?.class_id || res?.id || auth.classes.find((c) => c.class_name === name)?.id
        if (createdId) {
          activeClassId.value = createdId
          await auth.switchClass(createdId)
          if (!current()) return
          router.replace(`/class/${createdId}/students`)
          await loadStudents()
        }
      }
      classDialogVisible.value = false
      await auth.fetchClasses()
    } catch {
      // handled by interceptor
    } finally {
      if (current()) savingClass.value = false
    }
  }

  async function deleteClass(cls) {
    const { current } = captureClassScope()
    try {
      await auth.deleteClass(cls.id)
      if (!current()) return
      ElMessage.success('班级已删除')
      await auth.fetchClasses()
      if (!current()) return
      if (activeClassId.value === cls.id) {
        activeClassId.value = auth.classes[0]?.id || null
        if (activeClassId.value) {
          await auth.switchClass(activeClassId.value)
          router.replace(`/class/${activeClassId.value}/students`)
        } else {
          router.replace('/students')
        }
        await loadStudents()
      }
    } catch {
      // handled by interceptor
    }
  }

  async function deleteCurrentClass() {
    const cls = auth.classes.find((item) => item.id === activeClassId.value)
    if (cls) {
      await deleteClass(cls)
    }
  }

  return {
    classDialogVisible, classFormName, editingClassId, savingClass, openCreateClass,
    consumeAddClassQuery, openEditClass, openEditCurrentClass, saveClass, deleteClass,
    deleteCurrentClass,
  }
}
