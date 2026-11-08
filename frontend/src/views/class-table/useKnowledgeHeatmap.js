/**
 * The class's knowledge-point heatmap: loading it (an older request's answer is dropped once a newer one is
 * sent or the account changes), how a chapter's priority is shown, and opening the courseware matched to a
 * topic.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { ref, shallowRef } from 'vue'
import api from '@/api'

export function useKnowledgeHeatmap(ctx) {
  const {
    router, auth, educationStage, profileScope, activeClassId,
  } = ctx

  const heatmapVisible = ref(false)
  const heatmapLoading = ref(false)
  const heatmapTree = ref([])

  const heatmapGeneration = shallowRef(0)

  function priorityMeta(priority) {
    const priorityMap = {
      high: { label: '优先讲评', type: 'danger' },
      medium: { label: '适时讲评', type: 'warning' },
      low: { label: '个别辅导', type: 'info' },
    }
    return priorityMap[priority] || priorityMap.low
  }

  async function openHeatmap() {
    const scope = profileScope.value
    const classId = activeClassId.value
    const generation = ++heatmapGeneration.value
    const current = () => scope === profileScope.value && classId === activeClassId.value && generation === heatmapGeneration.value
    if (!classId || !auth.token || !educationStage.value) return
    heatmapTree.value = []
    heatmapVisible.value = true
    heatmapLoading.value = true
    try {
      const data = await api.classroom.heatmap(classId, { education_stage: educationStage.value })
      if (!current()) return
      heatmapTree.value = data.tree || []
    } catch {
      if (current()) heatmapTree.value = []
    } finally {
      if (current()) heatmapLoading.value = false
    }
  }

  function openCoursewareRecommendation(chapter, knowledgePoint) {
    router.push({ path: '/courseware', query: { chapter: chapter || undefined, knowledge_point: knowledgePoint || undefined } })
  }

  return {
    heatmapVisible, heatmapLoading, heatmapTree, heatmapGeneration, priorityMeta, openHeatmap,
    openCoursewareRecommendation,
  }
}
