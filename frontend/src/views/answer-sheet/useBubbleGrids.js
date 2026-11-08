/**
 * Bubble grids on the blank answer sheet (splitting a row into cells, confirming or discarding it) and the
 * recognition evidence viewer.
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { computed, h } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'

export function useBubbleGrids(ctx) {
  const {
    session, gridSplitCount, evidenceDialogVisible, evidenceLoading, evidenceRecords,
    evidencePaperId, evidenceFileText, evidenceImageUrl,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const applySession = (...args) => ctx.applySession(...args)
  const run = (...args) => ctx.run(...args)

  const bubbleGridRows = computed(() => session.value?.bubble_grids || [])
  const bubbleGridsUnconfirmed = computed(() => bubbleGridRows.value.filter(grid => grid.state !== 'confirmed').length)

  function recognitionTagType(answer) {
    const outcome = String(answer.outcome || '')
    if (outcome === 'correct') return 'success'
    if (outcome === 'wrong') return 'danger'
    if (outcome === 'blank') return 'info'
    return 'warning'
  }

  function addGridCell(grid) {
    if (!Array.isArray(grid.cells)) grid.cells = []
    if (grid.cells.length >= 8) return
    grid.cells.push({ option: '', bbox: [...(grid.grid_bbox || [0, 0, 0, 0])] })
  }

  function autoSplitGridCells(grid) {
    const [x, y, w, h] = (grid.grid_bbox || []).map(Number)
    const count = Math.min(8, Math.max(2, Number(gridSplitCount.value) || 4))
    if (!w || !h) {
      ElMessage.warning('请先填写整格范围')
      return
    }
    const letters = 'ABCDEFGH'
    grid.cells = Array.from({ length: count }, (_, index) => {
      const vertical = grid.grid_order === 'top_to_bottom'
      const cellW = vertical ? w : Math.floor(w / count)
      const cellH = vertical ? Math.floor(h / count) : h
      const cellX = vertical ? x : x + Math.floor((w / count) * index)
      const cellY = vertical ? y + Math.floor((h / count) * index) : y
      return { option: letters[index] || String(index + 1), bbox: [cellX, cellY, cellW, cellH] }
    })
  }

  async function confirmBubbleGridRow(grid) {
    const payload = {
      question_no: String(grid.question_no || ''),
      subquestion_no: String(grid.subquestion_no || ''),
      grid_order: grid.grid_order === 'top_to_bottom' ? 'top_to_bottom' : 'left_to_right',
      cells: (grid.cells || []).map(cell => ({
        option: String(cell.option || '').slice(0, 1),
        bbox: (cell.bbox || []).map(value => Math.round(Number(value))),
      })),
    }
    const result = await run(() => api.answerSheet.confirmBubbleGrids(session.value.session_id, [payload]), '涂卡栅格已确认')
    applySession(result?.session ? result.session : result)
  }

  async function discardBubbleGridRow(grid) {
    const payload = {
      question_no: String(grid.question_no || ''),
      subquestion_no: String(grid.subquestion_no || ''),
    }
    const result = await run(() => api.answerSheet.discardBubbleGrids(session.value.session_id, [payload]), '涂卡栅格已丢弃')
    applySession(result?.session ? result.session : result)
  }

  async function openEvidenceViewer(paperId) {
    evidencePaperId.value = paperId
    evidenceDialogVisible.value = true
    evidenceLoading.value = true
    evidenceRecords.value = []
    evidenceFileText.value = ''
    if (evidenceImageUrl.value) { URL.revokeObjectURL(evidenceImageUrl.value); evidenceImageUrl.value = '' }
    try {
      const data = await api.answerSheet.studentEvidenceIndex(session.value.session_id, paperId)
      evidenceRecords.value = data?.records || []
    } catch {
      evidenceRecords.value = []
    } finally {
      evidenceLoading.value = false
    }
  }

  async function openEvidenceFile(record, file) {
    const questionNo = (record.question_nos || [])[0] || ''
    try {
      const value = await api.answerSheet.studentEvidenceFile(session.value.session_id, evidencePaperId.value, questionNo, file.file)
      if (/\.png$/i.test(file.file)) {
        if (evidenceImageUrl.value) URL.revokeObjectURL(evidenceImageUrl.value)
        evidenceFileText.value = ''
        evidenceImageUrl.value = value
      } else {
        if (evidenceImageUrl.value) { URL.revokeObjectURL(evidenceImageUrl.value); evidenceImageUrl.value = '' }
        evidenceFileText.value = value
      }
    } catch {
      ElMessage.error('证据文件读取失败')
    }
  }

  return {
    bubbleGridRows, bubbleGridsUnconfirmed, recognitionTagType, addGridCell, autoSplitGridCells,
    confirmBubbleGridRow, discardBubbleGridRow, openEvidenceViewer, openEvidenceFile,
  }
}
