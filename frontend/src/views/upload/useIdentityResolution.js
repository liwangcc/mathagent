/**
 * Resolving student identities the OCR was unsure about, before anyone is added to the class.
 *
 * - The identity crop images (loadIdentityImages, openIdentityImage, openRecognizePaperImage,
 *   revokeIdentityImageUrls) and the hints shown beside them (identityReasonHint, identityClueLabel).
 * - The teacher's decisions (prepareIdentityResolutions, refreshIdentityOptions, identityResolutionPayload,
 *   needsIdentityResolution, identityCanConfirm); students are only created when all of them are filed.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import {
  buildIdentityResolutionItems,
  identityResolutionsReady,
  mergeRosterIntoIdentityItems,
  serializeIdentityResolutions,
} from '@/utils/identityResolution'

export function useIdentityResolution(ctx) {
  const {
    anonymous, identityResolutions, identityOptionsLoading, identityOptionsError, batchId, papers,
  } = ctx

  const needsIdentityResolution = computed(() => papers.value.some((paper) => {
    if (anonymous.value) return false
    if (paper.error && !paper.requires_manual_review) return false
    const identity = paper.identity || {}
    return identity.status !== 'matched' || !identity.student_id
  }))
  const identityCanConfirm = computed(() => identityResolutionsReady(identityResolutions.value))

  function revokeIdentityImageUrls() {
    identityResolutions.value.forEach((item) => {
      if (item.image_url) URL.revokeObjectURL(item.image_url)
    })
  }

  async function loadIdentityImages() {
    await Promise.all(identityResolutions.value.map(async (item) => {
      try {
        item.image_url = await api.grade.paperImage(batchId.value, item.paper_id)
      } catch {
        item.image_url = ''
      }
    }))
  }

  function openIdentityImage(item) {
    if (item.image_url) window.open(item.image_url, '_blank', 'noopener,noreferrer')
  }

  async function openRecognizePaperImage(paper, variant = 'active') {
    const targetBatchId = paper.batch_id || batchId.value
    if (!targetBatchId || !paper.paper_id) {
      ElMessage.error('原图标识缺失，请重新上传后再试')
      return
    }
    const imageWindow = window.open('about:blank', '_blank')
    try {
      const imageUrl = await api.grade.paperImage(targetBatchId, paper.paper_id, variant)
      if (imageWindow) {
        imageWindow.opener = null
        imageWindow.location.href = imageUrl
      } else {
        window.open(imageUrl, '_blank', 'noopener,noreferrer')
      }
      window.setTimeout(() => URL.revokeObjectURL(imageUrl), 60_000)
    } catch (err) {
      imageWindow?.close()
      notifyProcessingError(err, err.message || '原图加载失败')
    }
  }

  function identityReasonHint(reason) {
    const hints = {
      name_ambiguous: '当前班级存在同名学生，学号线索不足以区分，请确认学生身份。',
      identity_name_conflict: '图片、文件名或文件夹中的姓名指向不同学生，请结合原图确认，系统不会自动覆盖。',
      identity_number_conflict: '学号线索对应了多名学生，可能包含重名编号或收集工具后缀，请确认后再归属。',
      roster_name_ambiguous: 'AI 在当前班级名单中给出了多个相近候选，请结合原图选择。',
      roster_name_low_confidence: 'AI 只给出了低置信候选，请结合原图确认后再选择。',
      roster_name_not_found: 'AI 未在当前班级名单中找到可靠候选。正常情况下请先维护名单；确认无误时仍可例外新建。',
    }
    return hints[reason] || ''
  }

  function identityClueLabel(source) {
    const labels = { ocr: '图片识别', filename: '图片文件名', folder: '文件夹名' }
    return String(source || '').split('+').map(key => labels[key]).filter(Boolean).join('、') || '待老师确认'
  }

  async function prepareIdentityResolutions() {
    revokeIdentityImageUrls()
    identityOptionsError.value = ''
    identityResolutions.value = buildIdentityResolutionItems(papers.value)
    await refreshIdentityOptions()
    loadIdentityImages()
  }

  async function refreshIdentityOptions() {
    if (!batchId.value || !identityResolutions.value.length || identityOptionsLoading.value) return
    identityOptionsError.value = ''
    identityOptionsLoading.value = true
    try {
      const data = await api.grade.identityOptions(batchId.value)
      identityResolutions.value = mergeRosterIntoIdentityItems(
        identityResolutions.value,
        data.students || [],
      )
    } catch (err) {
      identityOptionsError.value = '未能刷新本批次班级名单，请检查网络后重新加载。'
    } finally {
      identityOptionsLoading.value = false
    }
  }

  function identityResolutionPayload() {
    if (anonymous.value) return []
    return serializeIdentityResolutions(identityResolutions.value)
  }

  return {
    needsIdentityResolution, identityCanConfirm, revokeIdentityImageUrls, loadIdentityImages,
    openIdentityImage, openRecognizePaperImage, identityReasonHint, identityClueLabel,
    prepareIdentityResolutions, refreshIdentityOptions, identityResolutionPayload,
  }
}
