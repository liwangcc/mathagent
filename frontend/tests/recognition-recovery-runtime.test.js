import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'

import {
  countGradablePapers,
  isExcludedPaper,
  mergeRecognitionPaper,
  normalizeBatchReadiness,
  validateGradeCompletion,
} from '../src/utils/batchReadiness.js'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  isSseServiceFault,
  reportSseServiceFault,
  SseTransportError,
} from '../src/utils/sse.js'
import { formatGradingDuration } from '../src/utils/gradingTimer.js'

// Execute the real Upload.vue recognition, grading, and recovery-pointer code
// instead of re-implementing it: each function is cut from the SFC source.
const script = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
  .split('<script setup>')[1]
  .split('</script>')[0]

function sourceOf(name) {
  const start = script.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'))
  assert.ok(start >= 0, `${name} should be defined in Upload.vue`)
  return script.slice(start, script.indexOf('\n}\n', start) + 2)
}

function constOf(name) {
  const match = script.match(new RegExp(`^const ${name} = .*$`, 'm'))
  assert.ok(match, `${name} should be declared in Upload.vue`)
  return match[0]
}

const FUNCTIONS = [
  'orientationStorageKey', 'rememberOrientationBatch', 'forgetOrientationBatch',
  'rememberRecognitionBatch', 'reviewStorageKey', 'rememberReviewBatch', 'forgetReviewBatch',
  'upsertRecognitionPaper', 'recognitionIncompleteMessage', 'applyRecognitionDone',
  'hydrateReviewState', 'resumeReviewBatch', 'onConfirmGrade',
  'startGradingClock', 'anchorGradingClock', 'stopGradingClock',
  'resetRecognitionCompletionState', 'resetRecognitionRecoveryState',
  'updateRecognitionRecoveryState', 'applyOrientationState',
  'refreshRecognitionStateAfterFailure', 'runRecognize', 'resumeRecognitionBatch',
  'releasePhoneCaptureSession',
  ...(script.includes('async function continueRefreshedRecognition(') ? ['continueRefreshedRecognition'] : []),
  'resetFiles', 'clearReviewDisplay',
  ...['continueAfterRecognition', 'startNewBatch'].filter((name) => script.includes(`function ${name}(`)),
  'scheduleRetryableOcrAutoResume', 'paperOcrFailed', 'canExcludeRecognitionPaper',
  'excludeRecognitionPaper', 'reviewErrorMessage',
  'clearRecognitionAutoResume', 'scheduleRecognitionAutoResume', 'resumeOrientationBatch',
  'canRecoverRecognitionConnection', 'recoverRecognitionConnection', 'pollRecognitionRecovery',
]
const CONSTANTS = [
  'REVIEW_BATCH_STORAGE_PREFIX', 'ORIENTATION_BATCH_STORAGE_PREFIX', 'gradablePaperCount',
  'REVIEW_COMPLETE_STATUSES', 'OCR_RETRY_AUTO_RESUME_DELAY_MS',
  'ACCELERATOR_AUTO_RESUME_DELAY_MS', 'ACCELERATOR_AUTO_RESUME_LIMIT',
]
const BATCH_ID = 'c609-current-batch'
const OLDER_REVIEW_ID = '1aba-older-review'
const WAITING = '等待教师客户端加速节点完成图片处理'

function sseResponse(events) {
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } })
}

function createUploadHarness({
  storage, transform = (source) => source, gradeEvents = [], reviews = {},
  recognitionResponse, snapshots = {}, excludeResponse, phoneCaptureSession = '',
}) {
  const calls = { confirm: 0, reviewGet: [], faults: [], messages: [], clockAtReview: [], stateGet: [], notifications: [], resumes: [], timers: [], excluded: [], captureCloses: [] }
  const scope = {
    ref, computed, countGradablePapers, isExcludedPaper, mergeRecognitionPaper, normalizeBatchReadiness,
    validateGradeCompletion, consumeSse, createSseEventError, createSseResponseError,
    isSseRequestCancelled, isSseServiceFault, reportSseServiceFault, SseTransportError, formatGradingDuration,
    window: {
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
      setTimeout: (fn, ms) => calls.timers.push({ fn, ms }),
      clearTimeout: (id) => { if (calls.timers[id - 1]) calls.timers[id - 1].cancelled = true },
      confirm: () => true,
    },
    recognitionAutoResumeTimer: null,
    recognitionAutoResumeAttempts: 0,
    recognitionAutoResumeGeneration: 0,
    serviceFaultHoldUntil: 0,
    recognitionRetryAutoResumedBatchId: '',
    excludingPaperId: ref(''),
    gradingClockStart: ref(0),
    gradingClockNow: ref(0),
    gradingClockTimer: null,
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    auth: { user: { id: 7 } },
    phoneCaptureSessionForFiles: phoneCaptureSession,
    loadPhoneCaptureSession: () => (phoneCaptureSession ? { session_id: phoneCaptureSession, token: 't' } : null),
    forgetPhoneCaptureSession: () => {},
    anonymous: ref(true),
    inheritIdentity: ref(false),
    batchId: ref(''),
    papers: ref([]),
    fileList: ref(Array.from({ length: 23 }, (_, index) => ({ name: `paper-${index}.jpg` }))),
    form: { mode: 'precise', category: 'daily', exam_name: '' },
    questions: ref([{ question_no: '1', content: '1+1=?', answer: '2' }]),
    recognized: { current: 0, total: 0 },
    recognitionStats: ref({}),
    recognitionDone: ref(false),
    recognitionCanGrade: ref(false),
    recognitionError: ref(''),
    grading: ref(false),
    gradeError: ref(''),
    gradeTotal: ref(0),
    progressList: ref([]),
    summary: ref(null),
    reviewBusy: ref(false),
    reviewArchived: ref(false),
    reviewRevision: ref(0),
    reviewRestoredLabel: ref(''),
    activeStep: ref(1),
    currentMessage: ref(''),
    serviceState: ref({ message: '' }),
    needsIdentityResolution: ref(false),
    identityDialogVisible: ref(false),
    identityResolutions: ref([]),
    coursewareRecommendations: ref([]),
    coursewareRecommendationState: ref('idle'),
    coursewareRecommendationQuery: ref({}),
    coursewareRecommendationKey: '',
    manualDecisionDialogVisible: ref(false),
    manualDecisionRow: ref(null),
    manualDecisionDrafts: ref([]),
    gradeController: null,
    requireBatchService: () => calls.faults.length === 0,
    requireRecognitionService: () => true,
    confirmedTemplateId: ref('synthetic-template'),
    templateTeachingContext: ref(null),
    templateMode: ref('marked'),
    templateColumnMode: ref('double'),
    normalizeTemplateMode: (value, fallback) => value || fallback,
    normalizeTemplateColumnMode: (value, fallback) => value || fallback,
    recognizing: ref(false),
    recognizeController: null,
    recognitionRecoveryState: ref({ status: 'none', message: '' }),
    recognitionAutoArmed: ref(false),
    identityOptionsLoading: ref(false),
    identityOptionsError: ref(''),
    appendStudentUploadFiles: () => {},
    loadDailyUsage: async () => {},
    prepareIdentityResolutions: async () => {},
    identityResolutionPayload: () => [],
    resetBatchReport: () => {},
    applyBatchReport: () => {},
    startBatchReportPolling: () => {},
    markServiceFault: (message) => calls.faults.push(message),
    notifyProcessingError: (error, message) => calls.notifications.push({ error, message }),
    ElMessage: { success: (message) => calls.messages.push(message), warning: (message) => calls.messages.push(message), error() {} },
    api: {
      capture: {
        close: async (id) => { calls.captureCloses.push(id) },
      },
      grade: {
        recognize: async (_form, signal) => recognitionResponse(signal),
        recognitionResume: async (id, signal) => {
          calls.resumes.push(id)
          return recognitionResponse(signal)
        },
        orientationState: async (id) => {
          calls.stateGet.push(id)
          const snapshot = typeof snapshots[id] === 'function' ? await snapshots[id]() : snapshots[id]
          if (snapshot instanceof Error) throw snapshot
          if (!snapshot) throw Object.assign(new Error('missing snapshot'), { status: 404 })
          return snapshot
        },
        confirm: async () => {
          calls.confirm += 1
          return sseResponse(gradeEvents)
        },
        excludePaper: async (id, paperId) => {
          calls.excluded.push([id, paperId])
          return excludeResponse()
        },
        reviewGet: async (id) => {
          calls.reviewGet.push(id)
          // Called after the grading stream ends, while the stopwatch still runs.
          if (scope.gradingClockStart.value) {
            calls.clockAtReview.push(scope.gradingClockNow.value - scope.gradingClockStart.value)
          }
          if (!reviews[id]) throw Object.assign(new Error('missing review'), { status: 404 })
          return reviews[id]
        },
      },
    },
  }
  const source = [
    // Execute the actual identity projection, not a permissive mocked gate.
    script.slice(script.indexOf('const needsIdentityResolution ='), script.indexOf('const identityCanConfirm =')),
    ...CONSTANTS.map(constOf),
    ...FUNCTIONS.map((name) => transform(sourceOf(name), name)),
  ].join('\n')
  const exported = `return { ${FUNCTIONS.join(', ')}, gradablePaperCount }`
  const names = Object.keys(scope).filter((name) => !CONSTANTS.includes(name) && name !== 'needsIdentityResolution')
  const fns = new Function(...names, `${source}\n${exported}`)(...names.map((name) => scope[name]))
  return { ...fns, scope, calls }
}

// Row shapes follow routers/grade.py: `_orientation_state_payload` for the
// snapshot restored after ACCELERATOR_WAITING and `_recognition_paper_payload`
// for the resume `paper` events (successful outcomes omit failure fields).
function snapshotRow(index, waiting) {
  return {
    batch_id: BATCH_ID,
    paper_id: `paper-${index}`,
    filename: `匿名页 ${index + 1}`,
    student_name: '',
    student_no: '',
    identity: {},
    identity_resolution: null,
    questions_count: waiting ? 0 : 5,
    matched_count: 0,
    ocr_status: waiting ? 'queued' : 'succeeded',
    error: waiting ? WAITING : '',
    requires_manual_review: false,
    orientation_status: 'auto_passed',
    orientation_reason: 'client_ocr_ready_v1',
  }
}

function resumedPaper(index, extra = {}) {
  return {
    batch_id: BATCH_ID,
    filename: `匿名页 ${index + 1}`,
    student_name: '',
    student_no: '',
    paper_id: `paper-${index}`,
    identity: {},
    questions_count: 5,
    matched_count: 0,
    orientation_status: 'auto_passed',
    orientation_reason: 'client_ocr_ready_v1',
    detected_tilt_degrees: 0,
    ...extra,
  }
}

const SERVER_DONE_23 = {
  batch_id: BATCH_ID,
  total_papers: 23,
  uploaded_papers: 23,
  succeeded_papers: 23,
  manual_review_papers: 0,
  failed_papers: 0,
  unresolved_papers: 0,
  can_grade: true,
}

const GRADE_EVENTS_23 = [
  ...Array.from({ length: 23 }, (_, index) => ['result', { paper_id: `paper-${index}`, score: 5 }]),
  ['done', { total: 23, avg_score: 5, review_revision: 1 }],
]

const REVIEW_23 = {
  batch_id: BATCH_ID,
  identity_mode: 'anonymous',
  results: Array.from({ length: 23 }, (_, index) => ({
    paper_id: `paper-${index}`,
    disposition: 'active',
    category: 'daily',
  })),
  summary: { total: 23, avg_score: 5 },
  review_revision: 1,
}

// Replays the incident: an older review is saved, the new batch checkpoints,
// accelerator waiting restores the 15 + 8 snapshot, and resume re-emits all 23.
function replayIncidentRecognition(harness) {
  harness.rememberRecognitionBatch({ batch_id: BATCH_ID })
  harness.scope.papers.value = Array.from({ length: 23 }, (_, index) => snapshotRow(index, index >= 15))
  for (let index = 0; index < 23; index += 1) harness.upsertRecognitionPaper(resumedPaper(index))
  harness.applyRecognitionDone(SERVER_DONE_23)
}

const REVIEW_KEY = 'mathagent_review_batch_id:anonymous:7'
const ORIENTATION_KEY = 'mathagent_orientation_batch_id:anonymous:7'

test('a completed 23/23 snapshot clears historical partial-failure and retry-exhaustion warnings', () => {
  const harness = createUploadHarness({ storage: new Map() })
  const payload = completedSnapshot({ status: 'ocr_partial_failed', retryable_failed_papers: 0 })
  for (const failureCode of ['', 'OCR_RETRY_EXHAUSTED', 'OCR_NO_RETRYABLE_FAILURES']) {
    harness.scope.recognitionRecoveryState.value = { status: 'exhausted', message: 'stale failure' }
    harness.applyOrientationState(payload, { failureCode })
    assert.equal(harness.scope.recognitionCanGrade.value, true)
    assert.deepEqual(harness.scope.recognitionRecoveryState.value, { status: 'none', message: '' })
  }
})

test('a successful SSE done clears an earlier recovery warning', () => {
  const harness = createUploadHarness({ storage: new Map() })
  harness.scope.recognitionRecoveryState.value = { status: 'unrecoverable', message: 'stale failure' }
  replayIncidentRecognition(harness)
  assert.deepEqual(harness.scope.recognitionRecoveryState.value, { status: 'none', message: '' })
})

test('genuine failed or unresolved papers still retain their recovery warning and block grading', () => {
  for (const extra of [
    { succeeded_papers: 22, failed_papers: 1, can_grade: false },
    { succeeded_papers: 22, unresolved_papers: 1, can_grade: false },
    { papers: [snapshotRow(0, false)], can_grade: true },
  ]) {
    const harness = createUploadHarness({ storage: new Map() })
    harness.applyOrientationState(completedSnapshot({ status: 'ocr_partial_failed', ...extra }), { failureCode: 'OCR_RETRY_EXHAUSTED' })
    assert.equal(harness.scope.recognitionCanGrade.value, false)
    assert.equal(harness.scope.recognitionRecoveryState.value.status, 'exhausted')
  }
})

test('starting a new batch exits recognition and clears only this teacher/mode recovery pointers', () => {
  const otherModeKey = 'mathagent_orientation_batch_id:7'
  const otherTeacherKey = 'mathagent_review_batch_id:anonymous:8'
  const storage = new Map([[REVIEW_KEY, BATCH_ID], [ORIENTATION_KEY, BATCH_ID], [otherModeKey, 'other-mode'], [otherTeacherKey, 'other-teacher']])
  const harness = createUploadHarness({ storage, reviews: { [BATCH_ID]: REVIEW_23 } })
  assert.equal(typeof harness.startNewBatch, 'function')
  harness.applyOrientationState(completedSnapshot())
  harness.scope.summary.value = REVIEW_23.summary
  harness.scope.progressList.value = REVIEW_23.results
  harness.scope.gradeError.value = 'stale grading error'
  harness.scope.identityResolutions.value = [{ paper_id: 'paper-0' }]
  harness.startNewBatch()
  assert.equal(harness.scope.activeStep.value, 0)
  assert.equal(harness.scope.batchId.value, '')
  assert.deepEqual(harness.scope.fileList.value, [])
  assert.deepEqual(harness.scope.papers.value, [])
  assert.deepEqual(harness.scope.questions.value, [])
  assert.deepEqual(harness.scope.progressList.value, [])
  assert.deepEqual(harness.scope.identityResolutions.value, [])
  assert.equal(harness.scope.summary.value, null)
  assert.equal(harness.scope.gradeError.value, '')
  assert.equal(harness.scope.recognitionDone.value, false)
  assert.equal(harness.scope.recognitionCanGrade.value, false)
  assert.deepEqual(harness.scope.recognitionRecoveryState.value, { status: 'none', message: '' })
  assert.deepEqual(harness.scope.recognized, { current: 0, total: 0 })
  assert.equal(harness.scope.recognitionAutoArmed.value, true)
  assert.equal(harness.scope.confirmedTemplateId.value, 'synthetic-template')
  assert.equal(harness.scope.form.category, 'daily')
  assert.equal(storage.has(REVIEW_KEY), false)
  assert.equal(storage.has(ORIENTATION_KEY), false)
  assert.equal(storage.get(otherModeKey), 'other-mode')
  assert.equal(storage.get(otherTeacherKey), 'other-teacher')
  assert.deepEqual(harness.calls.stateGet, [])
  assert.deepEqual(harness.calls.resumes, [])
  assert.equal(harness.calls.confirm, 0)
  assert.equal(harness.scope.api.grade.reviewGet instanceof Function, true)
})

test('new-batch action cannot interrupt recognition or grading', () => {
  for (const busy of ['recognizing', 'grading']) {
    const storage = new Map([[ORIENTATION_KEY, BATCH_ID]])
    const harness = createUploadHarness({ storage })
    assert.equal(typeof harness.startNewBatch, 'function')
    harness.scope.batchId.value = BATCH_ID
    harness.scope[busy].value = true
    harness.startNewBatch()
    assert.equal(harness.scope.batchId.value, BATCH_ID)
    assert.equal(harness.scope.activeStep.value, 1)
    assert.equal(storage.get(ORIENTATION_KEY), BATCH_ID)
  }
})

test('explicit continuation of a complete template batch uses the existing grading path once', async () => {
  const harness = createUploadHarness({ storage: new Map(), gradeEvents: GRADE_EVENTS_23, reviews: { [BATCH_ID]: REVIEW_23 } })
  assert.equal(typeof harness.continueAfterRecognition, 'function')
  harness.applyOrientationState(completedSnapshot())
  assert.equal(await harness.continueAfterRecognition(), true)
  assert.equal(harness.calls.confirm, 1)
  assert.equal(harness.scope.progressList.value.length, 23)
})

test('explicit continuation preserves the teacher identity gate', async () => {
  const harness = createUploadHarness({ storage: new Map() })
  assert.equal(typeof harness.continueAfterRecognition, 'function')
  harness.scope.anonymous.value = false
  harness.applyOrientationState(completedSnapshot())
  assert.equal(await harness.continueAfterRecognition(), false)
  assert.equal(harness.scope.identityDialogVisible.value, true)
  assert.equal(harness.calls.confirm, 0)
})

test('explicit continuation cannot submit incomplete or missing-question recognition', async () => {
  for (const extra of [{ succeeded_papers: 22, failed_papers: 1, can_grade: false }, { questions: [] }]) {
    const harness = createUploadHarness({ storage: new Map() })
    assert.equal(typeof harness.continueAfterRecognition, 'function')
    harness.applyOrientationState(completedSnapshot(extra))
    assert.equal(await harness.continueAfterRecognition(), false)
    assert.equal(harness.calls.confirm, 0)
    assert.ok(harness.scope.recognitionError.value)
  }
})

test('15 recognized + 8 accelerator-waiting rows resume to 23 gradable papers and 23/23 grading', async () => {
  const storage = new Map([[REVIEW_KEY, OLDER_REVIEW_ID]])
  const harness = createUploadHarness({ storage, gradeEvents: GRADE_EVENTS_23, reviews: { [BATCH_ID]: REVIEW_23 } })

  replayIncidentRecognition(harness)
  assert.equal(harness.gradablePaperCount.value, 23)
  assert.equal(harness.scope.recognitionCanGrade.value, true)
  assert.equal(harness.scope.papers.value.some((paper) => 'error' in paper || 'ocr_status' in paper), false)

  assert.equal(await harness.onConfirmGrade(), true)
  assert.equal(harness.scope.gradeError.value, '')
  assert.equal(harness.scope.summary.value.total, 23)
  assert.equal(harness.scope.progressList.value.length, 23)
  assert.equal(storage.get(REVIEW_KEY), BATCH_ID)
  assert.equal(storage.has(ORIENTATION_KEY), false)
})

test('the pre-fix shallow merge reproduces the incident message; the current batch stays restorable', async () => {
  const storage = new Map([[REVIEW_KEY, OLDER_REVIEW_ID]])
  const shallowMerge = (source, name) => {
    if (name !== 'upsertRecognitionPaper') return source
    const old = source.replace(
      'mergeRecognitionPaper(papers.value[index], payload)',
      '{ ...papers.value[index], ...payload }',
    )
    assert.notEqual(old, source, 'the merge call site should be present')
    return old
  }
  const harness = createUploadHarness({
    storage,
    transform: shallowMerge,
    gradeEvents: GRADE_EVENTS_23,
    reviews: { [BATCH_ID]: REVIEW_23, [OLDER_REVIEW_ID]: { ...REVIEW_23, batch_id: OLDER_REVIEW_ID } },
  })

  replayIncidentRecognition(harness)
  assert.equal(harness.gradablePaperCount.value, 15)
  assert.equal(await harness.onConfirmGrade(), false)
  assert.equal(
    harness.scope.gradeError.value,
    '批阅结果不完整：预期 15 张，服务端报告 23 张，实际收到 23 张。请重试批阅。',
  )

  // The server already saved the review; a reload must restore this batch,
  // not the older saved review that used to shadow it.
  assert.equal(storage.get(REVIEW_KEY), BATCH_ID)
  const reopened = createUploadHarness({ storage, reviews: { [BATCH_ID]: REVIEW_23, [OLDER_REVIEW_ID]: { ...REVIEW_23, batch_id: OLDER_REVIEW_ID } } })
  assert.equal(await reopened.resumeReviewBatch(), true)
  assert.deepEqual(reopened.calls.reviewGet, [BATCH_ID])
  assert.equal(reopened.scope.batchId.value, BATCH_ID)
  assert.equal(reopened.scope.progressList.value.length, 23)
})

test('a newer unfinished batch drops an older review pointer but keeps its own', () => {
  const storage = new Map([[REVIEW_KEY, OLDER_REVIEW_ID]])
  const harness = createUploadHarness({ storage })
  harness.rememberRecognitionBatch({ batch_id: BATCH_ID })
  assert.equal(storage.get(ORIENTATION_KEY), BATCH_ID)
  assert.equal(storage.has(REVIEW_KEY), false)

  storage.set(REVIEW_KEY, BATCH_ID)
  harness.rememberOrientationBatch(BATCH_ID)
  assert.equal(storage.get(REVIEW_KEY), BATCH_ID)
})

test('new genuine failures and still-queued papers keep the resumed batch blocked', async () => {
  const storage = new Map()
  const harness = createUploadHarness({ storage, gradeEvents: GRADE_EVENTS_23 })
  harness.rememberRecognitionBatch({ batch_id: BATCH_ID })
  harness.scope.papers.value = Array.from({ length: 23 }, (_, index) => snapshotRow(index, index >= 15))
  // Paper 21 now fails outright; paper 22 has no outcome yet and stays queued.
  for (let index = 0; index < 21; index += 1) harness.upsertRecognitionPaper(resumedPaper(index))
  harness.upsertRecognitionPaper(resumedPaper(21, { error: 'OCR 识别失败', requires_manual_review: false }))
  harness.applyRecognitionDone({
    ...SERVER_DONE_23,
    succeeded_papers: 21,
    failed_papers: 1,
    unresolved_papers: 1,
    can_grade: false,
  })

  assert.equal(harness.gradablePaperCount.value, 21)
  assert.equal(harness.scope.papers.value[22].ocr_status, 'queued')
  assert.equal(harness.scope.recognitionCanGrade.value, false)
  assert.equal(await harness.onConfirmGrade(), false)
  assert.match(harness.scope.gradeError.value, /^批阅未开始：/)
  assert.equal(harness.calls.confirm, 0)
})

test('the grading stopwatch follows the server clock and reports server-measured grading time', async () => {
  const storage = new Map()
  const harness = createUploadHarness({
    storage,
    gradeEvents: [
      // Pipeline grading began during recognition: the server is already 95 s in.
      ['progress', { message: '恢复批改', current: 0, total: 23, grading_elapsed_seconds: 95 }],
      ...GRADE_EVENTS_23.slice(0, -1),
      ['done', { total: 23, avg_score: 5, review_revision: 1, grading_elapsed_seconds: 101.5 }],
    ],
    reviews: {
      [BATCH_ID]: { ...REVIEW_23, summary: { ...REVIEW_23.summary, grading_elapsed_seconds: 101.5 } },
    },
  })
  replayIncidentRecognition(harness)

  assert.equal(await harness.onConfirmGrade(), true)
  const [liveMs] = harness.calls.clockAtReview
  assert.ok(liveMs >= 95_000 && liveMs < 97_000, `live clock should follow the server, got ${liveMs} ms`)
  assert.equal(harness.scope.summary.value.grading_elapsed_seconds, 101.5)
  assert.match(harness.calls.messages.at(-1), /批阅用时 1:41$/)
  // The ticking interval is cleared once grading ends.
  assert.equal(harness.scope.gradingClockStart.value, 0)
})

function interruptedResponse(events = []) {
  const bytes = new TextEncoder().encode(events.map(([event, data]) => (
    `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
  )).join(''))
  let read = false
  return {
    ok: true,
    body: { getReader: () => ({
      async read() {
        if (!read && bytes.length) { read = true; return { done: false, value: bytes } }
        throw new TypeError('NetworkError')
      },
      async cancel() {},
      releaseLock() {},
    }) },
  }
}

function completedSnapshot(extra = {}) {
  return {
    ...SERVER_DONE_23,
    status: 'ocr_done',
    papers: Array.from({ length: 23 }, (_, index) => snapshotRow(index, false)),
    questions: [{ question_no: '1', content: '1+1=?', answer: '2' }],
    source_mode: 'template', template_id: 'synthetic-template',
    ...extra,
  }
}

async function startRecognition(harness, mode) {
  if (mode === 'initial') return harness.runRecognize()
  harness.scope.batchId.value = BATCH_ID
  return harness.resumeRecognitionBatch(BATCH_ID)
}

function pendingSnapshot(status = 'recognition_interrupted') {
  return completedSnapshot({
    status, succeeded_papers: 17, unresolved_papers: 6, can_grade: false,
    papers: Array.from({ length: 23 }, (_, index) => snapshotRow(index, index >= 17)),
    questions: [],
  })
}

for (const mode of ['initial', 'resume']) {
  test(`${mode}: disconnect with reachable pending batch waits for active work then resumes without a service fault`, async () => {
    const snapshots = { [BATCH_ID]: pendingSnapshot('recognizing_remaining') }
    let requests = 0
    const harness = createUploadHarness({
      storage: new Map(), snapshots,
      recognitionResponse: () => ++requests === 1
        ? interruptedResponse([['progress', { batch_id: BATCH_ID }]])
        : sseResponse([...Array.from({ length: 23 }, (_, index) => ['paper', resumedPaper(index)]),
          ['done', { ...SERVER_DONE_23, questions: completedSnapshot().questions }]]),
      gradeEvents: GRADE_EVENTS_23, reviews: { [BATCH_ID]: REVIEW_23 },
    })
    await startRecognition(harness, mode)
    assert.deepEqual(harness.calls.faults, [])
    assert.equal(harness.scope.recognitionError.value, '')
    assert.equal(harness.scope.recognitionRecoveryState.value.status, 'waiting')
    assert.equal(harness.scope.papers.value.length, 23)
    assert.equal(harness.scope.papers.value.filter((paper) => paper.ocr_status === 'succeeded').length, 17)
    assert.equal(harness.calls.timers.length, 1)
    harness.calls.timers[0].fn()
    await flushAsync()
    assert.equal(requests, 1, 'active work must be polled, not resubmitted')
    snapshots[BATCH_ID] = pendingSnapshot()
    harness.calls.timers[1].fn()
    await flushAsync()
    assert.equal(requests, 2)
    assert.equal(harness.calls.confirm, 1)
    assert.equal(harness.scope.summary.value?.total, 23)
    assert.deepEqual(harness.calls.notifications, [])
  })
}

test('page reload of an active saved batch uses the same bounded read-before-resume recovery', async () => {
  const harness = createUploadHarness({
    storage: new Map([[ORIENTATION_KEY, BATCH_ID]]),
    snapshots: { [BATCH_ID]: pendingSnapshot('recognizing_remaining') },
  })
  assert.equal(await harness.resumeOrientationBatch(), true)
  assert.equal(harness.scope.recognitionRecoveryState.value.status, 'waiting')
  assert.equal(harness.calls.timers.length, 1)
  for (let index = 0; index < 6; index += 1) {
    harness.calls.timers[index].fn()
    await flushAsync()
  }
  assert.equal(harness.calls.timers.length, 6, 'active work cannot produce an infinite polling loop')
  assert.equal(harness.scope.recognitionRecoveryState.value.status, 'available')
  assert.match(harness.scope.recognitionRecoveryState.value.message, /上限/)
  assert.deepEqual(harness.calls.resumes, [])
  assert.deepEqual(harness.calls.faults, [])
})

test('a business-failed checkpoint stops connection recovery rather than spending OCR retries', async () => {
  const snapshots = { [BATCH_ID]: pendingSnapshot('recognizing_remaining') }
  const harness = createUploadHarness({
    storage: new Map(), snapshots,
    recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
  })
  await startRecognition(harness, 'resume')
  snapshots[BATCH_ID] = { ...pendingSnapshot('failed'), retryable_failed_papers: 1 }
  harness.calls.timers[0].fn()
  await flushAsync()
  assert.equal(harness.calls.resumes.length, 1, 'a fresh business failure is not a transport retry')
  assert.equal(harness.calls.timers.length, 1)
})

test('an empty checkpoint cannot prove that a batch is recoverable', async () => {
  const harness = createUploadHarness({
    storage: new Map(), snapshots: { [BATCH_ID]: { ...pendingSnapshot(), papers: [] } },
    recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
  })
  await startRecognition(harness, 'resume')
  assert.deepEqual(harness.calls.timers, [])
})

for (const responseKind of ['event', '409']) {
  test(`resume lock busy (${responseKind}) is bounded and never marks the service broken`, async () => {
    const signals = []
    const busy = { code: 'BATCH_RECOGNITION_BUSY', message: '该批次仍在识别，请等待当前任务完成，不要重复提交' }
    const harness = createUploadHarness({
      storage: new Map(), snapshots: { [BATCH_ID]: pendingSnapshot() },
      recognitionResponse: (signal) => {
        signals.push(signal)
        return responseKind === 'event' ? sseResponse([['error', busy]])
          : new Response(JSON.stringify({ detail: busy }), { status: 409 })
      },
    })
    await startRecognition(harness, 'resume')
    for (let index = 0; index < 6; index += 1) {
      harness.calls.timers[index].fn()
      await flushAsync()
    }
    assert.equal(harness.calls.timers.length, 6)
    assert.equal(harness.calls.resumes.length, 7, 'lock races share the same six-attempt budget')
    assert.equal(signals.every((signal) => !signal.aborted), true, 'never cancel server work to clear its lock')
    assert.deepEqual(harness.calls.faults, [])
    assert.deepEqual(harness.calls.notifications, [])
    assert.equal(harness.scope.recognitionRecoveryState.value.status, 'available')
  })
}

for (const status of [401, 403, 422, 409]) {
  for (const complete of [false, true]) {
    test(`HTTP ${status} without an explicit busy code never automatically resubmits (complete=${complete})`, async () => {
      const harness = createUploadHarness({
        storage: new Map(), snapshots: { [BATCH_ID]: complete ? completedSnapshot() : pendingSnapshot() },
        recognitionResponse: () => new Response(JSON.stringify({ detail: 'request rejected' }), { status }),
      })
      await startRecognition(harness, 'resume')
      assert.deepEqual(harness.calls.timers, [])
      assert.equal(harness.calls.resumes.length, 1)
      assert.equal(harness.calls.confirm, 0)
    })
  }
}

for (const code of ['GRADING_CHAIN_FAILED', 'OCR_RETRY_EXHAUSTED', 'OCR_NO_RETRYABLE_FAILURES', 'BATCH_TEMPLATE_REQUIRED']) {
  test(`business failure ${code} never becomes a connection retry even with a reachable snapshot`, async () => {
    const harness = createUploadHarness({
      storage: new Map(), snapshots: { [BATCH_ID]: pendingSnapshot() },
      recognitionResponse: () => sseResponse([['error', { code, batch_id: BATCH_ID, message: 'business failure' }]]),
    })
    await startRecognition(harness, 'resume')
    assert.deepEqual(harness.calls.timers, [])
    assert.equal(harness.calls.confirm, 0)
    assert.equal(harness.calls.resumes.length, 1)
  })
}

test('an unclassified business exception is not a transport recovery signal', async () => {
  const harness = createUploadHarness({
    storage: new Map(), snapshots: { [BATCH_ID]: pendingSnapshot() },
    recognitionResponse: () => { throw new Error('批改处理失败，请稍后重试') },
  })
  await startRecognition(harness, 'resume')
  assert.deepEqual(harness.calls.timers, [])
})

test('explicit cancellation does not refresh or schedule a retry', async () => {
  const harness = createUploadHarness({
    storage: new Map(), snapshots: { [BATCH_ID]: pendingSnapshot() },
    recognitionResponse: () => { throw Object.assign(new Error('cancelled'), { name: 'AbortError' }) },
  })
  await startRecognition(harness, 'resume')
  assert.deepEqual(harness.calls.stateGet, [])
  assert.deepEqual(harness.calls.timers, [])
  assert.deepEqual(harness.calls.faults, [])
})

test('clearing recovery while a checkpoint read is in flight cannot revive or replace the abandoned batch', async () => {
  const snapshots = { [BATCH_ID]: pendingSnapshot() }
  const harness = createUploadHarness({
    storage: new Map(), snapshots,
    recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
  })
  await startRecognition(harness, 'resume')
  let resolveSnapshot
  snapshots[BATCH_ID] = () => new Promise((resolve) => { resolveSnapshot = resolve })
  harness.calls.timers[0].fn()
  harness.startNewBatch()
  resolveSnapshot(pendingSnapshot())
  await flushAsync()
  assert.equal(harness.scope.batchId.value, '')
  assert.deepEqual(harness.scope.papers.value, [])
  assert.equal(harness.calls.timers.length, 1)
  assert.equal(harness.calls.resumes.length, 1)
})

for (const mode of ['initial', 'resume']) {
  test(`${mode}: disconnect + completed server snapshot continues the existing grading path once`, async () => {
    const harness = createUploadHarness({
      storage: new Map(),
      recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
      snapshots: { [BATCH_ID]: completedSnapshot() },
      gradeEvents: GRADE_EVENTS_23, reviews: { [BATCH_ID]: REVIEW_23 },
    })
    await startRecognition(harness, mode)
    assert.deepEqual(harness.calls.stateGet, [BATCH_ID])
    assert.equal(harness.calls.confirm, 1)
    assert.equal(harness.scope.summary.value?.total, 23)
    assert.equal(harness.scope.recognitionCanGrade.value, true)
    assert.equal(harness.scope.recognitionError.value, '')
    assert.deepEqual(harness.calls.notifications, [])
    assert.deepEqual(harness.calls.faults, [])
    assert.equal(harness.scope.recognizing.value, false)
  })

  test(`${mode}: failed refresh blocks grading and retains the durable pointer on a temporary failure`, async () => {
    const storage = new Map([[ORIENTATION_KEY, BATCH_ID]])
    const harness = createUploadHarness({
      storage,
      recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
      snapshots: { [BATCH_ID]: Object.assign(new Error('unavailable'), { status: 503 }) },
    })
    await startRecognition(harness, mode)
    assert.equal(harness.scope.recognitionCanGrade.value, false)
    assert.equal(harness.scope.recognitionDone.value, false)
    assert.equal(harness.calls.confirm, 0)
    assert.equal(storage.get(ORIENTATION_KEY), BATCH_ID)
    assert.equal(harness.calls.notifications.length, 1)
  })

  for (const [label, extra] of [
    ['unresolved papers', { unresolved_papers: 1, succeeded_papers: 22, can_grade: false }],
    ['failed papers', { failed_papers: 1, succeeded_papers: 22, can_grade: false }],
    ['no questions', { questions: [] }],
    ['incomplete papers', { papers: [snapshotRow(0, false)] }],
    ['mismatched batch', { batch_id: 'different-batch' }],
  ]) {
    test(`${mode}: refreshed ${label} never bypass the grading completion gate`, async () => {
      const harness = createUploadHarness({
        storage: new Map(),
        recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
        snapshots: { [BATCH_ID]: completedSnapshot(extra) },
      })
      await startRecognition(harness, mode)
      assert.equal(harness.calls.confirm, 0)
      assert.equal(harness.scope.recognitionCanGrade.value, false)
    })
  }

  test(`${mode}: refreshed unknown identity uses the real teacher-resolution gate`, async () => {
    const harness = createUploadHarness({
      storage: new Map(),
      recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
      snapshots: { [BATCH_ID]: completedSnapshot() },
    })
    harness.scope.anonymous.value = false
    await startRecognition(harness, mode)
    assert.equal(harness.scope.recognitionCanGrade.value, true)
    assert.equal(harness.scope.identityDialogVisible.value, true)
    assert.equal(harness.calls.confirm, 0)
    assert.deepEqual(harness.calls.notifications, [])
  })

  test(`${mode}: accelerator-waiting race with completed snapshot does not reschedule or erase completion`, async () => {
    const harness = createUploadHarness({
      storage: new Map(),
      recognitionResponse: () => sseResponse([
        ['progress', { batch_id: BATCH_ID }],
        ['error', { code: 'ACCELERATOR_WAITING', message: WAITING, batch_id: BATCH_ID }],
      ]),
      snapshots: { [BATCH_ID]: completedSnapshot() },
      gradeEvents: GRADE_EVENTS_23, reviews: { [BATCH_ID]: REVIEW_23 },
    })
    await startRecognition(harness, mode)
    assert.equal(harness.calls.confirm, 1)
    assert.deepEqual(harness.calls.timers, [])
    assert.equal(harness.scope.recognitionCanGrade.value, true)
  })

  test(`${mode}: a grading rejection after snapshot recovery is not re-reported as a recognition failure`, async () => {
    const harness = createUploadHarness({
      storage: new Map(),
      recognitionResponse: () => interruptedResponse([['progress', { batch_id: BATCH_ID }]]),
      snapshots: { [BATCH_ID]: completedSnapshot() },
      gradeEvents: [['error', { code: 'BATCH_GRADING_BUSY', message: '批次正在批阅' }]],
    })
    await startRecognition(harness, mode)
    assert.equal(harness.calls.confirm, 1)
    assert.deepEqual(harness.calls.stateGet, [BATCH_ID])
    assert.equal(harness.scope.gradeError.value, '批次正在批阅')
    assert.equal(harness.scope.recognitionError.value, '')
    assert.equal(harness.scope.recognitionCanGrade.value, true)
    assert.equal(harness.calls.notifications.length, 1)
    assert.equal(harness.calls.notifications[0].message, '批次正在批阅')
  })

  test(`${mode}: normal done grades once without a recovery refresh`, async () => {
    const harness = createUploadHarness({
      storage: new Map(),
      recognitionResponse: () => sseResponse([
        ...Array.from({ length: 23 }, (_, index) => ['paper', resumedPaper(index)]),
        ['done', { ...SERVER_DONE_23, questions: completedSnapshot().questions }],
      ]),
      gradeEvents: GRADE_EVENTS_23, reviews: { [BATCH_ID]: REVIEW_23 },
    })
    await startRecognition(harness, mode)
    assert.equal(harness.calls.confirm, 1)
    assert.deepEqual(harness.calls.stateGet, [])
  })
}

for (const failure of ['422', 'disconnect']) {
  test(`initial ${failure} before the first batch event never reads or overwrites the previous batch`, async () => {
    const storage = new Map([[ORIENTATION_KEY, OLDER_REVIEW_ID], [REVIEW_KEY, OLDER_REVIEW_ID]])
    const harness = createUploadHarness({
      storage,
      recognitionResponse: () => failure === '422'
        ? new Response(JSON.stringify({ detail: 'invalid upload' }), { status: 422 })
        : interruptedResponse(),
      snapshots: { [OLDER_REVIEW_ID]: completedSnapshot({ batch_id: OLDER_REVIEW_ID }) },
    })
    harness.scope.batchId.value = OLDER_REVIEW_ID
    await harness.runRecognize()
    assert.deepEqual(harness.calls.stateGet, [])
    assert.equal(harness.scope.batchId.value, '')
    assert.deepEqual([...storage], [[ORIENTATION_KEY, OLDER_REVIEW_ID], [REVIEW_KEY, OLDER_REVIEW_ID]])
    assert.equal(harness.calls.confirm, 0)
    assert.equal(harness.scope.recognitionCanGrade.value, false)
  })
}

for (const event of ['progress', 'paper', 'error']) {
  test(`initial ${event} identifies the new durable batch instead of the previous batch`, async () => {
    const storage = new Map([[ORIENTATION_KEY, OLDER_REVIEW_ID], [REVIEW_KEY, OLDER_REVIEW_ID]])
    const payload = event === 'paper' ? resumedPaper(0) : { batch_id: BATCH_ID }
    const harness = createUploadHarness({
      storage,
      recognitionResponse: () => interruptedResponse([[event, payload]]),
      snapshots: { [BATCH_ID]: completedSnapshot({ can_grade: false }) },
    })
    harness.scope.batchId.value = OLDER_REVIEW_ID
    await harness.runRecognize()
    assert.deepEqual(harness.calls.stateGet, [BATCH_ID])
    assert.equal(storage.get(ORIENTATION_KEY), BATCH_ID)
    assert.equal(storage.has(REVIEW_KEY), false)
    assert.equal(harness.calls.confirm, 0)
  })
}

async function flushAsync(rounds = 30) {
  for (let index = 0; index < rounds; index += 1) await new Promise((resolve) => setTimeout(resolve, 0))
}

function failedRow(index) {
  return { ...snapshotRow(index, false), questions_count: 0, ocr_status: 'failed', error: '本次模板本地配准失败' }
}

const INCOMPLETE_DONE_23 = {
  ...SERVER_DONE_23,
  status: 'ocr_partial_failed',
  total_papers: 22,
  succeeded_papers: 22,
  failed_papers: 1,
  retryable_failed_papers: 1,
  can_grade: false,
  questions: [],
}

test('an incomplete done with a retryable OCR failure shows recovery and retries it once automatically', async () => {
  const harness = createUploadHarness({
    storage: new Map(),
    recognitionResponse: () => sseResponse([
      ...Array.from({ length: 22 }, (_, index) => ['paper', resumedPaper(index)]),
      ['paper', resumedPaper(22, { questions_count: 0, error: '答案识别暂时失败', requires_manual_review: false })],
      ['done', INCOMPLETE_DONE_23],
    ]),
  })
  await harness.runRecognize()
  assert.equal(harness.calls.confirm, 0)
  assert.equal(harness.scope.recognitionCanGrade.value, false)
  assert.equal(harness.scope.recognitionRecoveryState.value.status, 'waiting')
  assert.match(harness.scope.recognitionRecoveryState.value.message, /自动重试/)
  assert.deepEqual(harness.calls.timers.map((timer) => timer.ms), [15000])
  // A failed page waiting for its automatic retry is not offered for exclusion.
  assert.equal(harness.canExcludeRecognitionPaper(harness.scope.papers.value[22]), false)

  harness.calls.timers[0].fn()
  await flushAsync()
  assert.deepEqual(harness.calls.resumes, [BATCH_ID])
  // The same batch never loops: the server's one extra attempt was spent.
  assert.equal(harness.calls.timers.length, 1)
  assert.equal(harness.scope.recognitionRecoveryState.value.status, 'available')
})

test('an incomplete done without retryable failures says the page is unrecoverable', async () => {
  const harness = createUploadHarness({
    storage: new Map(),
    recognitionResponse: () => sseResponse([
      ...Array.from({ length: 22 }, (_, index) => ['paper', resumedPaper(index)]),
      ['paper', resumedPaper(22, { questions_count: 0, error: '本次模板本地配准失败', requires_manual_review: false })],
      ['done', { ...INCOMPLETE_DONE_23, retryable_failed_papers: 0 }],
    ]),
  })
  await harness.runRecognize()
  assert.equal(harness.scope.recognitionRecoveryState.value.status, 'unrecoverable')
  assert.deepEqual(harness.calls.timers, [])
  assert.equal(harness.canExcludeRecognitionPaper(harness.scope.papers.value[22]), true)
  assert.equal(harness.canExcludeRecognitionPaper(harness.scope.papers.value[0]), false)
})

test('excluding a final OCR failure grades the remaining papers through the normal path once', async () => {
  const kept = Array.from({ length: 22 }, (_, index) => snapshotRow(index, false))
  const excludedSnapshot = completedSnapshot({
    succeeded_papers: 22,
    excluded_papers: 1,
    papers: [...kept, { ...failedRow(22), ocr_status: 'excluded', error: '已由教师排除，不参与本批批阅' }],
  })
  const gradeEvents = [
    ...Array.from({ length: 22 }, (_, index) => ['result', { paper_id: `paper-${index}`, score: 5 }]),
    ['done', { total: 22, avg_score: 5, review_revision: 1 }],
  ]
  const review = { ...REVIEW_23, status: 'review_ready', results: REVIEW_23.results.slice(0, 22), summary: { total: 22, avg_score: 5 } }
  const harness = createUploadHarness({
    storage: new Map(), gradeEvents, reviews: { [BATCH_ID]: review },
    excludeResponse: () => excludedSnapshot,
  })
  harness.applyOrientationState(completedSnapshot({
    status: 'ocr_partial_failed', succeeded_papers: 22, failed_papers: 1, can_grade: false,
    papers: [...kept, failedRow(22)], questions: [],
  }))
  assert.equal(harness.scope.recognitionRecoveryState.value.status, 'unrecoverable')

  assert.equal(await harness.excludeRecognitionPaper(harness.scope.papers.value[22]), true)
  assert.deepEqual(harness.calls.excluded, [[BATCH_ID, 'paper-22']])
  assert.equal(harness.calls.confirm, 1)
  assert.equal(harness.scope.summary.value?.total, 22)
  assert.equal(harness.scope.gradeError.value, '')
})

test('a review pointer to an unfinished batch is not shown as finished; it resumes via recovery', async () => {
  const storage = new Map([[REVIEW_KEY, BATCH_ID]])
  const harness = createUploadHarness({
    storage,
    reviews: { [BATCH_ID]: { ...REVIEW_23, status: 'grading_interrupted' } },
  })
  assert.equal(await harness.resumeReviewBatch(), false)
  assert.equal(harness.scope.summary.value, null)
  assert.deepEqual(harness.scope.progressList.value, [])
  assert.equal(storage.has(REVIEW_KEY), false)
  assert.equal(storage.get(ORIENTATION_KEY), BATCH_ID)
})

test('phone capture photos are released only once the server has created the batch', async () => {
  const created = createUploadHarness({
    storage: new Map(),
    phoneCaptureSession: '0123456789abcdef01234567',
    recognitionResponse: () => sseResponse([
      ['progress', { batch_id: BATCH_ID, current: 0, total: 23 }],
      ['paper', resumedPaper(0)],
      ['error', { message: 'synthetic stop' }],
    ]),
  })
  await created.runRecognize()
  assert.deepEqual(created.calls.captureCloses, ['0123456789abcdef01234567'], 'released exactly once')

  const rejected = createUploadHarness({
    storage: new Map(),
    phoneCaptureSession: '0123456789abcdef01234567',
    recognitionResponse: () => new Response(JSON.stringify({ detail: 'busy' }), { status: 503, headers: { 'Content-Type': 'application/json' } }),
  })
  await rejected.runRecognize()
  assert.deepEqual(rejected.calls.captureCloses, [], 'a rejected upload keeps the phone photos for a retry')
})
