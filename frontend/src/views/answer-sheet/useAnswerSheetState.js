/**
 * The 答题卡批阅 page's state: the session and its stage, the service and hardware state, the files chosen
 * for each stage, the layout and review drafts, the stage descriptions, and the values derived from them
 * alone (which stage is open, what is locked, what can resume).
 *
 * Moved from views/AnswerSheet.vue; the page passes nothing in (see AnswerSheet.vue).
 */
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { normalizeHardwareAvailability } from '@/api'
import { pairFilesByFolder, pairFilesBySide } from '@/utils/answerSheetPairing'
import { useAuthStore } from '@/stores/auth'

export function useAnswerSheetState(ctx) {
  // Defined by later parts of the page; looked up when called.
  const captureFileName = (...args) => ctx.captureFileName(...args)
  const captureSourcePath = (...args) => ctx.captureSourcePath(...args)

  const STORAGE_KEY = 'mathagent_answer_sheet_session_id'
  const auth = useAuthStore()
  const route = useRoute()
  const busy = ref(false)
  const operationMessage = ref('')
  // { current, total } while a stream reports countable progress; drives the busy overlay's page counter.
  const operationProgress = ref(null)
  const serviceState = ref({ status: 'checking', label: '检测中', message: '正在检查答题卡批阅整条处理链路…' })
  const hardwareState = ref(normalizeHardwareAvailability())
  const SERVICE_FAULT_HOLD_MS = 60_000
  const ACCELERATOR_STREAM_ERROR_STATES = {
    ACCELERATOR_WAITING: {
      hardwareStatus: 'waiting_accelerator',
      serviceStatus: 'queued',
      label: '等待加速节点',
      message: '正在等待客户端或机会式加速节点；已完成进度保留在当前会话，可继续处理。',
    },
    ACCELERATOR_UNAVAILABLE: {
      hardwareStatus: 'unavailable',
      serviceStatus: 'unavailable',
      label: '加速节点不可用',
      message: '加速节点当前不可用，已停止新的图像处理；已完成进度保留在当前会话。',
    },
    ACCELERATOR_FAULT: {
      hardwareStatus: 'fault',
      serviceStatus: 'fault',
      label: '加速节点故障',
      message: '加速节点处理发生故障，批改已停止；已完成进度保留在当前会话。',
    },
  }

  const restoreError = ref('')
  const session = ref(null)
  const activeStage = ref(0)
  const answerSource = ref('embedded')
  const sourceFiles = ref([])
  const answerFiles = ref([])
  const layoutFiles = ref([])
  const templateCodeInput = ref('')
  const templateCodeHistory = ref([])
  const batchSetup = ref({ paper_template_code: '', answer_sheet_template_code: '' })
  const paperTemplates = ref([])
  const paperTemplateSources = ref([])
  const paperRegistration = ref({ template_id: '', alias: '' })
  const batches = ref([])
  const studentFiles = ref([])
  const studentOrderSource = ref({
    source: 'input_order',
    label: '选择顺序',
    message: '未找到可靠的文件序号或时间戳，暂保留本次选择顺序；请检查下面列表。',
  })
  // The server pairs by file name whenever every file is paired, whatever the
  // list order, so the tags and note follow the names rather than the sort.
  const studentPairingMode = computed(() => {
    if (pairFilesByFolder(studentFiles.value, captureSourcePath)) return 'folder'
    if (pairFilesBySide(studentFiles.value, captureFileName)) return 'side'
    return 'order'
  })
  const studentPairingNote = computed(() => ({
    folder: '每个文件夹恰有两张图片，系统按文件夹配对正反面（按模板页型区分）；文件夹名或文件名能在当前班级名单中唯一对应学生时直接确认身份，否则沿用同组已识别的一面。',
    side: '文件名均带正面/反面标记且两两成对，系统按文件名配对正反面；文件名能在当前班级名单中唯一对应学生时直接确认身份，否则沿用同组已识别的一面。',
    order: '若每位学生的文件名或文件夹名都能在当前班级名单中唯一对应且恰有两页，系统按学生配对；否则按相邻页建立正反面页对，正面身份确认后传给同组背面。文件名不能确定身份时，请按顺序上传同一学生的正面和背面（正面在前）。',
  }[studentPairingMode.value]))
  const fileSelectionProcessingCount = ref(0)
  const classStudents = ref([])
  const gradingDrafts = ref([])
  const questionDrafts = ref([])
  const answerRegions = ref([])
  const identityRegions = ref([])
  const layoutSummary = ref({})
  const currentPageId = ref('')
  const layoutImageUrl = ref('')
  const layoutImageReady = ref(false)
  const layoutImageError = ref('')
  const activeDraftIndex = ref(0)
  const gridSplitCount = ref(4)
  const evidenceDialogVisible = ref(false)
  const evidenceLoading = ref(false)
  const evidenceRecords = ref([])
  const evidencePaperId = ref('')
  const evidenceFileText = ref('')
  const evidenceImageUrl = ref('')

  const currentReviewPaperId = ref('')
  const studentPageImageUrl = ref('')
  const studentPageImageError = ref('')

  const stages = [
    { title: '原始试卷', caption: '语义与答案来源' },
    { title: '教师确认', caption: '题目与评分规则' },
    { title: '空白答题卡', caption: '导入版面依据' },
    { title: '绑定冻结', caption: '区域与页型' },
    { title: '学生答题卡', caption: '识别与批阅' },
  ]
  // Template-code batches take questions and answers from a frozen paper
  // template, so the first two stages only show that binding.
  const batchStages = [
    { title: '试卷模板', caption: '已冻结题目与答案' },
    { title: '题目答案', caption: '只读核对' },
    ...stages.slice(2),
  ]
  const statusOrder = { draft: 0, source_uploaded: 0, failed: 0, source_recognized: 1, answers_confirmed: 2, layout_draft: 3, frozen: 4, recognizing: 4, review: 4, archived: 4 }
  const statusLabels = { draft: '新建草稿', source_uploaded: '材料已上传', source_recognized: '题目已识别', answers_confirmed: '答案已确认', layout_draft: '版面编辑中', frozen: '版面已冻结', recognizing: '学生答题卡识别中', review: '等待复核', archived: '已归档', failed: '处理失败' }
  const identityLabels = { name: '姓名', student_no: '学号', exam_no_text: '准考证号文字', qr: '二维码', barcode: '条形码', exam_no_marks: '准考证号涂格', page_number: '页码' }

  const completedStage = computed(() => statusOrder[session.value?.status] ?? 0)
  const isBatch = computed(() => Boolean(session.value?.batch_contract_version))
  const visibleStages = computed(() => (isBatch.value ? batchStages : stages))
  const canCreate = computed(() => !!auth.currentClassId && !!batchSetup.value.paper_template_code)
  const hardwareBlocksNewImageWork = computed(() => ['unavailable', 'fault'].includes(hardwareState.value.status))
  const serviceBlocksNewWork = computed(() => (
    ['checking', 'unavailable', 'fault'].includes(serviceState.value.status)
    || hardwareBlocksNewImageWork.value
  ))
  const workflowReadonly = computed(() => serviceBlocksNewWork.value && session.value?.status !== 'archived')
  const canDelete = computed(() => !serviceBlocksNewWork.value && ['draft', 'failed'].includes(session.value?.status))
  const statusLabel = computed(() => statusLabels[session.value?.status] || session.value?.status)
  const sourceLocked = computed(() => !['draft', 'failed'].includes(session.value?.status))
  const answersLocked = computed(() => !['source_recognized', 'failed'].includes(session.value?.status))
  const layoutFrozen = computed(() => ['frozen', 'recognizing', 'review', 'archived'].includes(session.value?.status))
  const canUploadSource = computed(() => sourceFiles.value.length && (answerSource.value !== 'separate' || answerFiles.value.length))
  const fileSelectionProcessing = computed(() => fileSelectionProcessingCount.value > 0)
  const answerSourceLabel = computed(() => ({ embedded: '试卷内含答案', separate: '另传答案文件', ai_generated: 'AI 生成候选' })[answerSource.value])
  const answerSpecs = computed(() => session.value?.answer_spec_snapshot?.length ? session.value.answer_spec_snapshot : questionDrafts.value)
  const currentPage = computed(() => session.value?.answer_sheet_pages?.find(page => page.page_id === currentPageId.value))
  const visibleAnswerRegions = computed(() => answerRegions.value.filter(region => region.page_id === currentPageId.value))
  const visibleIdentityRegions = computed(() => identityRegions.value.filter(region => region.page_id === currentPageId.value))
  const currentPageHasUnconfirmedRegions = computed(() => (
    !layoutFrozen.value
    && session.value?.layout_localization?.provider_route === 'qwen3.8max'
    && [...visibleAnswerRegions.value, ...visibleIdentityRegions.value].some(region => !region.teacher_confirmed)
  ))
  const currentDraft = computed(() => gradingDrafts.value[activeDraftIndex.value] || {})
  const currentDraftResult = computed(() => currentDraft.value.result || {})
  const currentDraftPages = computed(() => (currentDraft.value.paper_ids || []).map((paperId) => (
    session.value?.student_pages?.find(page => page.paper_id === paperId) || { paper_id: paperId }
  )))
  const currentReviewPageIndex = computed(() => {
    const index = currentDraftPages.value.findIndex(page => page.paper_id === currentReviewPaperId.value)
    return index >= 0 ? index : 0
  })
  const currentReviewPage = computed(() => currentDraftPages.value[currentReviewPageIndex.value] || null)
  const canArchiveDrafts = computed(() => gradingDrafts.value.length > 0 && gradingDrafts.value.every(
    draft => Number(draft.result?.needs_review_count || 0) === 0,
  ))
  const sourceRecognitionResumable = computed(() => [
    ...(session.value?.source_exam_pages || []),
    ...(session.value?.answer_pages || []),
  ].some(page => page?.recognition_checkpoint))
  const studentRecognitionResumable = computed(() => (session.value?.student_pages || []).some(
    page => page?.recognition_checkpoint,
  ))
  const gradingDraftResumable = computed(() => Boolean(
    session.value?.asset_lifecycle?.grading_draft_checkpoint?.drafts?.length,
  ))
  const layoutAcceleration = computed(() => {
    const state = session.value?.asset_lifecycle?.layout_acceleration
    return state && typeof state === 'object' ? state : null
  })
  const layoutAccelerationMessage = computed(() => String(
    session.value?.error
    || ({
      waiting_accelerator: '页面已保存，正在等待客户端或机会式加速节点。',
      unavailable: '当前没有可用加速节点，服务器不会回退处理图像。',
      fault: '加速图像处理发生故障，页面仍已保存。',
    }[layoutAcceleration.value?.status])
    || '页面图像处理尚未完成。',
  ))
  const studentUpload = computed(() => session.value?.student_upload || {})
  const studentUploadActive = computed(() => ['uploading', 'processing'].includes(
    String(studentUpload.value.status || ''),
  ))
  const studentUploadProcessing = computed(() => studentUpload.value.status === 'processing')
  const studentUploadCanProcess = computed(() => studentUploadActive.value && (
    studentUploadProcessing.value || Number(studentUpload.value.uploaded_count || 0) > 0
  ))

  return {
    STORAGE_KEY, auth, route, busy, operationMessage, operationProgress, serviceState,
    hardwareState, SERVICE_FAULT_HOLD_MS, ACCELERATOR_STREAM_ERROR_STATES, restoreError, session,
    activeStage, answerSource, sourceFiles, answerFiles, layoutFiles, templateCodeInput,
    templateCodeHistory, batchSetup, paperTemplates, paperTemplateSources, paperRegistration,
    batches, studentFiles, studentOrderSource, studentPairingMode, studentPairingNote,
    fileSelectionProcessingCount, classStudents, gradingDrafts, questionDrafts, answerRegions,
    identityRegions, layoutSummary, currentPageId, layoutImageUrl, layoutImageReady,
    layoutImageError, activeDraftIndex, gridSplitCount, evidenceDialogVisible, evidenceLoading,
    evidenceRecords, evidencePaperId, evidenceFileText, evidenceImageUrl, currentReviewPaperId,
    studentPageImageUrl, studentPageImageError, stages, batchStages, statusOrder, statusLabels,
    identityLabels, completedStage, isBatch, visibleStages, canCreate, hardwareBlocksNewImageWork,
    serviceBlocksNewWork, workflowReadonly, canDelete, statusLabel, sourceLocked, answersLocked,
    layoutFrozen, canUploadSource, fileSelectionProcessing, answerSourceLabel, answerSpecs,
    currentPage, visibleAnswerRegions, visibleIdentityRegions, currentPageHasUnconfirmedRegions,
    currentDraft, currentDraftResult, currentDraftPages, currentReviewPageIndex, currentReviewPage,
    canArchiveDrafts, sourceRecognitionResumable, studentRecognitionResumable,
    gradingDraftResumable, layoutAcceleration, layoutAccelerationMessage, studentUpload,
    studentUploadActive, studentUploadProcessing, studentUploadCanProcess,
  }
}
