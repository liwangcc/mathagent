/**
 * The 批改 page's state: every ref and reactive object the page's parts share, the values derived from them
 * alone, and the page's constants.
 *
 * - The workflow state (files, template, recognition, grading, review, report, dialogs) and the request
 *   controllers and run tokens several parts read (shallowRef, so a part can replace them).
 * - Derived values that need nothing else: service blocks, the hero copy, the workflow steps, recognition
 *   counts, whether the template locks the mode, whether the batch dialog may be confirmed.
 * - The teacher's "inherit identity" choice, remembered per teacher in localStorage.
 *
 * Moved from views/Upload.vue; the page passes its props in ctx (see Upload.vue).
 */
import { ref, reactive, computed, watch, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { countGradablePapers, isExcludedPaper } from '@/utils/batchReadiness'
import { gradingPhases } from '@/utils/gradingPhases'
import { useAuthStore } from '@/stores/auth'
import { paperOcrFailed, paperOcrNeedsReview, paperOcrSucceeded } from '@/utils/recognitionStatus'

export function useUploadState(ctx) {
  const {
    props,
  } = ctx

  const anonymous = computed(() => props.anonymous)
  const auth = useAuthStore()
  const router = useRouter()
  const REVIEW_BATCH_STORAGE_PREFIX = 'mathagent_review_batch_id'
  const ORIENTATION_BATCH_STORAGE_PREFIX = 'mathagent_orientation_batch_id'
  const TEMPLATE_RECOGNITION_STORAGE_PREFIX = 'mathagent_template_recognition'
  const INHERIT_IDENTITY_STORAGE_PREFIX = 'mathagent_inherit_identity'

  // Opt-in per batch (frozen by the server at upload); the last choice is a
  // per-teacher browser convenience only.
  const inheritIdentity = ref(false)
  function inheritIdentityStorageKey() {
    const teacherId = auth.user?.id
    return teacherId ? `${INHERIT_IDENTITY_STORAGE_PREFIX}:${teacherId}` : ''
  }
  watch(() => auth.user?.id, () => {
    const key = inheritIdentityStorageKey()
    try {
      inheritIdentity.value = Boolean(key) && localStorage.getItem(key) === 'true'
    } catch {
      inheritIdentity.value = false
    }
  }, { immediate: true })
  watch(inheritIdentity, (value) => {
    const key = inheritIdentityStorageKey()
    try {
      if (key) localStorage.setItem(key, value ? 'true' : 'false')
    } catch {
      // Storage may be unavailable; the checkbox still applies to this upload.
    }
  })

  const fileInputRef = ref(null)
  const folderInputRef = ref(null)
  const templateInputRef = ref(null)
  const fileList = ref([])
  const phoneCaptureVisible = ref(false)
  const studentSubmissionVisible = ref(false)

  const imagePreprocessing = ref(false)
  const imageExtracting = ref(false)
  const extractionProgress = ref({ current: 0, total: 0, message: '' })
  const isDragover = ref(false)
  const showNameGuide = ref(false)
  const batchDialogVisible = ref(false)

  // 流程步骤：0 上传 / 1 识别（批改结果展示在独立进度卡片）
  const activeStep = ref(0)
  const recognizing = ref(false)
  const grading = ref(false)
  // Live grading stopwatch. Progress events re-anchor it to the server's elapsed
  // seconds (first paper graded -> now), so client clock skew does not matter.
  const gradingClockStart = ref(0)
  const gradingClockNow = ref(0)

  const gradingElapsedSeconds = computed(() => (
    gradingClockStart.value ? Math.max(0, (gradingClockNow.value - gradingClockStart.value) / 1000) : 0
  ))
  const dailyUsage = ref(null)
  const serviceState = ref({
    status: 'checking',
    label: '检测中',
    message: '正在检查视觉识别、数学批改和任务协调链路。',
  })
  const SERVICE_FAULT_HOLD_MS = 60_000
  const TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE = '模板识别链路当前不可用，请稍后再试。'
  const ACCELERATOR_AUTO_RESUME_DELAY_MS = 3000
  const ACCELERATOR_AUTO_RESUME_LIMIT = 6
  // Transient OCR failures (provider capacity, malformed output) keep one durable
  // server-side retry. Spend it automatically after a short cool-down instead of
  // leaving the teacher at an incomplete batch.
  const OCR_RETRY_AUTO_RESUME_DELAY_MS = 15000
  const availabilityPollTimer = shallowRef(null)

  const serviceFaultHoldUntil = shallowRef(0)

  const serviceBlocksNewWork = computed(() => (
    ['checking', 'unavailable', 'fault'].includes(serviceState.value.status)
  ))
  const templateServiceState = ref({
    status: 'checking',
    label: '检测中',
    message: '正在检查模板版面识别链路。',
    blocksNewWork: true,
  })
  const templateServiceBlocksNewWork = computed(() => (
    templateServiceState.value.blocksNewWork !== false
  ))
  const registrationHardwareState = ref({
    status: 'checking',
    blocksNewWork: true,
    advisory: false,
  })
  const registrationHardwareBlocksNewWork = computed(() => (
    // Only the server's explicit instruction blocks: an advisory node state
    // never blocks, and a missing flag is not a block either.
    registrationHardwareState.value.advisory !== true
    && registrationHardwareState.value.blocksNewWork === true
  ))
  const recognitionBlocksNewWork = computed(() => (
    serviceBlocksNewWork.value
    || registrationHardwareBlocksNewWork.value
  ))

  // ── 多页一次性模板（本次批改绝对权威，下次重新导入）──
  const templateMode = ref('marked')        // 统一模板合同：密集手写卷走有痕迹安全路径
  const templateColumnMode = ref('double')   // single/double/triple：模板栏布局
  const templateFiles = ref([])             // 待识别的 N 页模板图
  const templatePreprocessing = ref(false)
  const recognizingTemplate = ref(false)
  const templateRecognizeMessage = ref('')
  const templateRecognizeError = ref('')
  const confirmedTemplateId = ref('')       // 已确认模板 id（本次批改权威）
  const templatePageCount = ref(0)
  const templateTeachingContext = ref(null)
  const templateStageLabel = computed(() => {
    const stage = pendingTemplateId.value || confirmedTemplateId.value
      ? templateTeachingContext.value?.education_stage
      : auth.user?.education_stage
    return stage === 'primary' ? '小学数学' : stage === 'junior' ? '初中数学' : stage === 'senior' ? '高中数学' : '历史模板 · 学段未标记'
  })
  const templateDialogVisible = ref(false)   // 模板确认弹窗
  const confirmingTemplate = ref(false)
  const importingTemplateBank = ref(false)
  const templateBankImported = ref(false)
  const templatePages = ref([])              // 弹窗内逐页题目（可编辑）
  const templateCorrectionCandidatesLoading = ref(false)

  const templateCutBusy = ref(false)
  const templateCutResult = ref(null)
  const templateCutProgress = ref({ completed: 0, total: 0, remaining: 0 })
  const templateCutActionId = ref('')
  const templateCutImageType = ref('photo')
  const templateCutPreviewPages = ref([])
  const templateCutPreviewError = ref('')
  const templateCropGesture = ref(null)
  const templateReferenceEditorInstances = new Map()
  const templateActivePreviewPage = ref(0)
  const TEMPLATE_GEOMETRY_TOOLS = [
    { key: 'question', label: '红色题号' },
    { key: 'answer', label: '蓝色作答框' },
    { key: 'drawing', label: '绿色画图区' },
    { key: 'drawing-label', label: '作图题题号' },
  ]
  const templateEditorBusy = computed(() => confirmingTemplate.value || importingTemplateBank.value
    || templateQuestionEditing.value || templateCutBusy.value
    || templateCutPreviewPages.value.some((page) => page.saving || page.geometryQuestionPrompting))
  const templateQuestionEditing = ref(false)
  const templateChoiceScore = ref(5)         // 考试：选择题统一分值
  const templateFillScore = ref(5)           // 考试：填空题统一分值（多空自定义除外）
  const pendingTemplateId = ref('')          // recognize 返回、待确认的 template_id

  // OCR 身份不确定时只收集教师决策；真正建学生延迟到“全部入学生档案”。
  const identityDialogVisible = ref(false)
  const identityResolutions = ref([])
  const identityOptionsLoading = ref(false)
  const identityOptionsError = ref('')

  const form = reactive({
    category: 'daily',
    exam_name: '',
    mode: 'precise',
  })

  // 识别阶段状态
  const batchId = ref('')
  const papers = ref([])
  const recognized = reactive({ current: 0, total: 0 })
  const recognitionDone = ref(false)
  const recognitionAutoArmed = ref(true)
  const recognitionCanGrade = ref(false)
  const recognitionError = ref('')
  const recognitionRecoveryState = ref({ status: 'none', message: '' })
  const recognitionStats = ref({ uploaded: 0, succeeded: 0, manualReview: 0, failed: 0, excluded: 0, unresolved: 0 })
  const excludingPaperId = ref('')
  const currentMessage = ref('')
  const questions = ref([])
  const orientationDialogVisible = ref(false)
  const orientationPaper = ref(null)
  const orientationImageUrl = ref('')
  const orientationImageLoading = ref(false)
  const orientationImageError = ref('')
  const orientationQuarterTurns = ref(0)
  const orientationSaving = ref(false)
  const orientationActionId = ref('')

  // 批改阶段状态
  const progressList = ref([])
  const gradeTotal = ref(0)
  const gradeError = ref('')
  const summary = ref(null)
  const reviewRestoredLabel = ref('')
  const coursewareRecommendations = ref([])
  const coursewareRecommendationState = ref('idle')
  const coursewareRecommendationQuery = ref({})
  const coursewareRecommendationKey = shallowRef('')
  const batchReport = ref({
    status: 'not_started',
    text: '',
    stats: {},
    error: '',
    updated_at: '',
  })

  const reviewBusy = ref(false)
  const reviewImagesDownloading = ref(false)
  const hasReviewImageRows = computed(() => progressList.value.some((row) => row.disposition !== 'voided' && !row.error))
  const reviewArchived = ref(false)
  // Hero copy follows the current workflow step (display only).
  const gradeHero = computed(() => {
    const current = workflowSteps.value.findIndex((step) => step.state === 'current')
    const copy = [
      ['导入答案依据', '先确认批次类型与试卷栏数，再导入模板；教师确认后的标准答案才是本次批改依据。'],
      [anonymous.value ? '上传无身份页面' : '上传学生试卷', '支持图片、文件夹与 ZIP / RAR 压缩包；每张图片独立识别，身份不确定时由老师确认。'],
      ['识别与确认', '逐张核对识别结果，确认无误后开始批改。'],
      [reviewArchived.value ? '本批审阅已结束' : '审阅批改结果', '逐份查看原卷与逐题判定，先处理待复核题，再把已完成的结果归档。'],
    ]
    const [title, lead] = copy[current < 0 ? 3 : current]
    return { title, lead }
  })
  // Read-only position in the grading flow for the page stepper; derived purely from existing state.
  const workflowSteps = computed(() => {
    const reviewStarted = grading.value || Boolean(summary.value) || progressList.value.length > 0
    const confirmStarted = activeStep.value >= 1
    const current = reviewStarted ? 3 : confirmStarted ? 2 : confirmedTemplateId.value ? 1 : 0
    return [
      { key: 'template', label: '答案依据', hint: '导入并确认模板' },
      { key: 'papers', label: anonymous.value ? '页面上传' : '学生试卷', hint: '选择图片或压缩包' },
      { key: 'confirm', label: '识别与确认', hint: '核对识别结果' },
      { key: 'review', label: '批改审阅', hint: '复核、作废或归档' },
    ].map((step, index) => ({
      ...step,
      state: index < current || (index === 3 && reviewArchived.value) ? 'done' : index === current ? 'current' : 'todo',
    }))
  })
  // The grading card appears once grading starts and stays for the review it produces.
  const progressCardVisible = computed(() => (
    grading.value || progressList.value.length > 0 || Boolean(summary.value) || Boolean(gradeError.value)
  ))
  const gradingPhaseState = computed(() => gradingPhases({
    anonymous: anonymous.value,
    identityPending: identityDialogVisible.value,
    grading: grading.value || Boolean(gradeError.value) || (progressList.value.length > 0 && !summary.value),
    reviewing: Boolean(summary.value),
    archived: reviewArchived.value,
  }))
  const dailyUsagePercent = computed(() => {
    const limit = Number(dailyUsage.value?.limit || 0)
    return limit > 0 ? Math.min(100, Math.round((Number(dailyUsage.value?.current || 0) / limit) * 100)) : 0
  })
  const reviewRevision = ref(0)
  const manualDecisionDialogVisible = ref(false)
  const manualDecisionRow = ref(null)
  const manualDecisionDrafts = ref([])
  const manualDecisionSaving = ref(false)
  const manualDecisionImageUrl = ref('')
  const manualDecisionImageLoading = ref(false)
  const manualDecisionImageError = ref('')
  const manualDecisionPages = ref([])
  const manualDecisionPageIndex = ref(0)
  const manualDecisionShowPaper = ref(true)

  const templateRecognizeController = shallowRef(null)
  const templateConfirmController = shallowRef(null)
  const templateCutController = shallowRef(null)

  // Run token whose finished state was already applied.  Polling can observe the
  // completion before the POST resolves; rendering it again would rebuild every
  // page preview (refetching images and revoking blobs) on each tick.
  const templateCutCompletedRunToken = shallowRef(null)
  const templateCutRunToken = shallowRef(0)

  const recognizeController = shallowRef(null)
  const gradeController = shallowRef(null)

  const recognizeSucceededCount = computed(() => recognitionDone.value
    ? recognitionStats.value.succeeded
    : papers.value.filter(paperOcrSucceeded).length)
  const recognizeFailureCount = computed(() => recognitionDone.value
    ? recognitionStats.value.failed
    : papers.value.filter(paperOcrFailed).length)
  const recognizeManualReviewCount = computed(() => recognitionDone.value
    ? recognitionStats.value.manualReview
    : papers.value.filter(paperOcrNeedsReview).length)
  const recognizeCompletedCount = computed(() => (
    recognizeSucceededCount.value
    + recognizeFailureCount.value
    + recognizeManualReviewCount.value
    + (recognitionDone.value ? Number(recognitionStats.value.excluded || 0) : papers.value.filter(isExcludedPaper).length)
  ))
  const recognizeUnresolvedCount = computed(() => recognitionDone.value
    ? recognitionStats.value.unresolved
    : Math.max(0, Number(recognized.total || 0) - recognizeCompletedCount.value))
  const recognitionResumeVisible = computed(() => (
    ['available', 'service_fault', 'waiting'].includes(recognitionRecoveryState.value.status)
    && !!batchId.value
  ))
  const recognitionRecoveryIsError = computed(() => (
    ['exhausted', 'unrecoverable', 'service_fault'].includes(recognitionRecoveryState.value.status)
  ))
  const gradablePaperCount = computed(() => countGradablePapers(papers.value))

  // 有模板走确认权威路后，识别阶段锁定模式选择（避免中途切换）
  const templateLocked = computed(() => recognizing.value || grading.value || recognizingTemplate.value || templateDialogVisible.value || !!pendingTemplateId.value)

  // 批次确认弹窗校验：批次类型必填；考试须填名称；模板在前一步已确认
  const batchConfirmValid = computed(() => {
    if (!form.category) return false
    if (form.category === 'exam' && !form.exam_name.trim()) return false
    return Boolean(confirmedTemplateId.value)
  })

  return {
    anonymous, auth, router, REVIEW_BATCH_STORAGE_PREFIX, ORIENTATION_BATCH_STORAGE_PREFIX,
    TEMPLATE_RECOGNITION_STORAGE_PREFIX, INHERIT_IDENTITY_STORAGE_PREFIX, inheritIdentity,
    inheritIdentityStorageKey, fileInputRef, folderInputRef, templateInputRef, fileList,
    phoneCaptureVisible, studentSubmissionVisible, imagePreprocessing, imageExtracting,
    extractionProgress, isDragover, showNameGuide, batchDialogVisible, activeStep, recognizing,
    grading, gradingClockStart, gradingClockNow, gradingElapsedSeconds, dailyUsage, serviceState,
    SERVICE_FAULT_HOLD_MS, TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE, ACCELERATOR_AUTO_RESUME_DELAY_MS,
    ACCELERATOR_AUTO_RESUME_LIMIT, OCR_RETRY_AUTO_RESUME_DELAY_MS, availabilityPollTimer,
    serviceFaultHoldUntil, serviceBlocksNewWork, templateServiceState, templateServiceBlocksNewWork,
    registrationHardwareState, registrationHardwareBlocksNewWork, recognitionBlocksNewWork,
    templateMode, templateColumnMode, templateFiles, templatePreprocessing, recognizingTemplate,
    templateRecognizeMessage, templateRecognizeError, confirmedTemplateId, templatePageCount,
    templateTeachingContext, templateStageLabel, templateDialogVisible, confirmingTemplate,
    importingTemplateBank, templateBankImported, templatePages, templateCorrectionCandidatesLoading,
    templateCutBusy, templateCutResult, templateCutProgress, templateCutActionId,
    templateCutImageType, templateCutPreviewPages, templateCutPreviewError, templateCropGesture,
    templateReferenceEditorInstances, templateActivePreviewPage, TEMPLATE_GEOMETRY_TOOLS,
    templateEditorBusy, templateQuestionEditing, templateChoiceScore, templateFillScore,
    pendingTemplateId, identityDialogVisible, identityResolutions, identityOptionsLoading,
    identityOptionsError, form, batchId, papers, recognized, recognitionDone, recognitionAutoArmed,
    recognitionCanGrade, recognitionError, recognitionRecoveryState, recognitionStats,
    excludingPaperId, currentMessage, questions, orientationDialogVisible, orientationPaper,
    orientationImageUrl, orientationImageLoading, orientationImageError, orientationQuarterTurns,
    orientationSaving, orientationActionId, progressList, gradeTotal, gradeError, summary,
    reviewRestoredLabel, coursewareRecommendations, coursewareRecommendationState,
    coursewareRecommendationQuery, coursewareRecommendationKey, batchReport, reviewBusy,
    reviewImagesDownloading, hasReviewImageRows, reviewArchived, gradeHero, workflowSteps,
    progressCardVisible, gradingPhaseState, dailyUsagePercent, reviewRevision,
    manualDecisionDialogVisible, manualDecisionRow, manualDecisionDrafts, manualDecisionSaving,
    manualDecisionImageUrl, manualDecisionImageLoading, manualDecisionImageError,
    manualDecisionPages, manualDecisionPageIndex, manualDecisionShowPaper,
    templateRecognizeController, templateConfirmController, templateCutController,
    templateCutCompletedRunToken, templateCutRunToken, recognizeController, gradeController,
    recognizeSucceededCount, recognizeFailureCount, recognizeManualReviewCount,
    recognizeCompletedCount, recognizeUnresolvedCount, recognitionResumeVisible,
    recognitionRecoveryIsError, gradablePaperCount, templateLocked, batchConfirmValid,
  }
}
