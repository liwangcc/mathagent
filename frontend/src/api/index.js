import axios from 'axios'
import { ElMessage } from 'element-plus'
import { triggerBlobDownload } from '@/utils/download'
import { safeProfileRedirect } from '@/utils/educationStage'
import { enrichProcessingError } from '@/utils/processingError'
import { notifyProcessingError } from '@/utils/errorNotification'

const TOKEN_KEY = 'mathagent_token'
const USER_KEY = 'mathagent_user'

const apiOrigin = (import.meta.env.VITE_API_ORIGIN || '').trim().replace(/\/$/, '')
const apiBaseUrl = apiOrigin ? `${apiOrigin}/api` : '/api'

function absoluteApiUrl(url) {
  if (!apiOrigin) return url
  if (typeof url !== 'string' || !url.startsWith('/api/') || url.startsWith('/api//')) {
    throw new Error('API 地址无效')
  }
  return `${apiOrigin}${url}`
}

const http = axios.create({
  baseURL: apiBaseUrl,
  timeout: 60000,
})

// Teacher-confirmed orientation retries run OCR synchronously.  Keep this
// request beyond the server-side OCR attempt budget instead of letting the
// generic Axios timeout abandon a still-running retry.
const OCR_CONFIRM_TIMEOUT_MS = 600000
const AVAILABILITY_TIMEOUT_MS = 10000
// Model-backed teacher tools (vision office, roster/question photo OCR, chat
// agent) routinely outlive the generic 60 s budget while the server finishes.
const MODEL_TOOL_TIMEOUT_MS = 300000
const STUDENT_PAGE_SIZE = 100
const STUDENT_PAGE_LIMIT = 30
const GRADING_OPERATION_TIMEOUT_MS = 660000
const HARDWARE_AVAILABILITY_STATUSES = new Set([
  'ready',
  'waiting_accelerator',
  'unavailable',
  'fault',
  'bypass',
])
const HARDWARE_STATUS_FALLBACK_MESSAGES = {
  waiting_accelerator: '等待教师客户端加速节点，已保存进度不会丢失。',
  unavailable: '本地加速节点不可用，已停止新的图像处理任务；已保存进度仍可查看。',
  fault: '本地加速节点故障，已停止新的图像处理任务；已保存进度仍可查看。',
}
const HARDWARE_TO_SERVICE_STATUS = {
  waiting_accelerator: 'queued',
  unavailable: 'unavailable',
  fault: 'fault',
}

export function normalizeHardwareAvailability(payload) {
  const hardware = payload?.hardware
  if (hardware == null) return { status: 'bypass', message: '' }
  if (typeof hardware !== 'object' || Array.isArray(hardware)) {
    return {
      status: 'fault',
      message: HARDWARE_STATUS_FALLBACK_MESSAGES.fault,
    }
  }

  const status = String(hardware.status || '')
  if (!HARDWARE_AVAILABILITY_STATUSES.has(status)) {
    return {
      status: 'fault',
      message: HARDWARE_STATUS_FALLBACK_MESSAGES.fault,
    }
  }
  const message = typeof hardware.message === 'string' ? hardware.message.trim() : ''
  return {
    status,
    message: message || HARDWARE_STATUS_FALLBACK_MESSAGES[status] || '',
  }
}

export function mergeHardwareAvailabilityStatus(serviceStatus, hardwareStatus) {
  const serviceRanks = { available: 0, queued: 1, unavailable: 2, fault: 3 }
  const mappedHardwareStatus = HARDWARE_TO_SERVICE_STATUS[hardwareStatus]
  if (!mappedHardwareStatus) return serviceStatus
  if (!(serviceStatus in serviceRanks)) return mappedHardwareStatus
  return serviceRanks[mappedHardwareStatus] > serviceRanks[serviceStatus]
    ? mappedHardwareStatus
    : serviceStatus
}

function handleUnauthorized(error = new Error('登录已过期，请重新登录')) {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  // A shared office computer may be used by the next teacher; never carry
  // the previous account's chat (student names, scores) into a new login.
  localStorage.removeItem('mathagent_chat_history')
  if (location.pathname !== '/login') {
    location.href = '/login'
  }
  notifyProcessingError(error, '登录已过期，请重新登录')
}

function handleTeachingProfileRequired(detail) {
  if (detail?.code !== 'teaching_profile_required') return false
  if (location.pathname !== '/profile') {
    const redirect = safeProfileRedirect(`${location.pathname}${location.search}${location.hash}`)
    // A reload revalidates /me. Keep the token: a missing teaching profile is not an expired login.
    location.href = `/profile?setup=education-stage&redirect=${encodeURIComponent(redirect)}`
  }
  return true
}

function localApiPath(url) {
  if (typeof url !== 'string' || !url.startsWith('/api/') || url.startsWith('/api//') || url.includes(String.fromCharCode(92))) {
    throw new Error('仅允许下载本站生成的文件')
  }
  const path = url.slice(4)
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new Error('仅允许下载本站生成的文件')
  }
  return path
}

async function authFetch(url, options = {}) {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = {
    ...(options.headers || {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  const resp = await fetch(absoluteApiUrl(url), { ...options, headers })
  if (resp.status === 401) {
    const error = new Error('登录已过期，请重新登录')
    handleUnauthorized(error)
    throw error
  }
  if (resp.status === 428) {
    const data = await resp.clone().json().catch(() => ({}))
    if (handleTeachingProfileRequired(data.detail)) throw new Error(data.detail.message || '请先完善教学学段')
  }
  return resp
}

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (resp) => resp.data,
  async (err) => {
    if (axios.isCancel(err) || err.name === 'AbortError') {
      return Promise.reject(err)
    }
    const responseData = err.response?.data
    if (typeof Blob !== 'undefined' && responseData instanceof Blob) {
      const contentType = String(
        responseData.type || err.response?.headers?.['content-type'] || '',
      ).toLowerCase()
      if (contentType.includes('json')) {
        try {
          err.response.data = JSON.parse(await responseData.text())
        } catch {
          // Preserve the original Axios error when the response body is malformed.
        }
      }
    }
    const status = err.response?.status
    const detail = err.response?.data?.detail || err.response?.data?.message || err.message
    if (typeof detail === 'string' && detail) err.message = detail
    else if (detail?.message) err.message = detail.message
    enrichProcessingError(err, err.response?.data)

    if (status === 401) {
      handleUnauthorized(err)
    } else if (status === 428 && handleTeachingProfileRequired(detail)) {
      ElMessage.warning(detail.message || '请先完善教学学段')
    } else if (detail && !err.config?.suppressErrorToast) {
      notifyProcessingError(err, err.message || '请求失败')
    }
    return Promise.reject(err)
  }
)

const api = {
  auth: {
    sendSms: (phone, captchaTicket) => http.post('/auth/sms/send', { phone, captcha_ticket: captchaTicket }),
    smsLogin: (payload) => http.post('/auth/sms/login', payload),
    me: () => http.get('/auth/me'),
    updateMe: (payload) => http.put('/auth/me', payload),

    verifyCaptcha: (captchaVerifyParam, phone) => http.post('/auth/captcha/verify', { captcha_verify_param: captchaVerifyParam, phone }),
    setPassword: (password, oldPassword) => http.post('/auth/password/set', { password, old_password: oldPassword }),
    passwordLogin: (payload) => http.post('/auth/password/login', payload),
    logout: () => http.post('/auth/logout'),
    // 班级管理
    getClasses: () => http.get('/auth/classes'),
    createClass: (className) => http.post('/auth/class/create', { class_name: className }),
    updateClass: (classId, className) => http.put(`/auth/class/${classId}`, { class_name: className }),
    deleteClass: (classId) => http.delete(`/auth/class/${classId}`),
    switchClass: (classId) => http.post('/auth/class/switch', { class_id: classId }),
    // 登录页能力探测 + 内置算术验证码
    features: () => http.get('/auth/features'),
    captchaChallenge: () => http.get('/auth/captcha/challenge'),
    // 管理员建号（没有短信/微信登录时的开通方式）
    adminCreateUser: (payload) => http.post('/auth/admin/user', payload),
    adminListUsers: () => http.get('/auth/admin/users'),
  },

  classroom: {
    students: (classId, params) => http.get(`/class/${classId}/students`, { params }),
    addStudents: (classId, students) => http.post(`/class/${classId}/students`, { students }),
    importStudentsExcel: (classId, formData) => http.post(`/class/${classId}/students/import-excel`, formData),
    importStudentsImage: (classId, formData) => http.post(`/class/${classId}/students/import-image`, formData, { timeout: MODEL_TOOL_TIMEOUT_MS }),
    removeStudent: (classId, studentId) => http.delete(`/class/${classId}/students/${studentId}`),
    updateStudent: (classId, studentId, data) => http.put(`/class/${classId}/students/${studentId}`, data),
    setObservation: (classId, studentId, data) => http.post(`/class/${classId}/students/${studentId}/observation`, data),
    importExamScores: (classId, formData) => http.post(`/class/${classId}/exam-scores/import`, formData),
    heatmap: (classId, params) => http.get(`/class/${classId}/heatmap`, { params }),
    report: (classId, params) => http.post(`/class/${classId}/report`, null, { params }),
    reportBatch: (classId, payload) => http.post(`/class/${classId}/report/batch`, payload),
    letterShares: (classId, params) => http.get(`/class/${classId}/letter-shares`, { params }),
    revokeLetterShare: (shareId) => http.post(`/letter-share/${shareId}/revoke`),
  },

  grade: {
    availability: () => http.get('/batch-grade/availability', {
      timeout: AVAILABILITY_TIMEOUT_MS,
      suppressErrorToast: true,
    }),
    templateAvailability: () => http.get('/batch-grade/template/availability', {
      timeout: AVAILABILITY_TIMEOUT_MS,
      suppressErrorToast: true,
    }),
    results: (params) => http.get('/grade/results', { params }),
    detail: (id) => http.get(`/grade/result/${id}`),
    delete: (ids) => http.delete('/grade/results', { data: ids }),
    recognize: (formData, signal) => {
      return authFetch('/api/batch-grade/recognize', {
        method: 'POST',
        body: formData,
        signal,
      })
    },
    confirm: (payload, signal) => {
      return authFetch('/api/batch-grade/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal,
      })
    },
    recognitionResume: (batchId, signal) => authFetch(
      `/api/batch-grade/${encodeURIComponent(batchId)}/recognize/resume`,
      { method: 'POST', signal },
    ),
    // 多页一次性模板：recognize（SSE，OCR N 页）→ confirm（JSON，老师确认即权威）
    templateRecognize: (formData, signalOrLegacyHasAnswers, legacySignal) => {
      // The second argument used to be the removed has_answers selector.
      // Accept that call shape for older integrations, but never send the
      // selector because the server now runs one template pipeline.
      const signal = typeof signalOrLegacyHasAnswers === 'boolean'
        ? legacySignal
        : (signalOrLegacyHasAnswers ?? legacySignal)
      return authFetch('/api/batch-grade/template/recognize', {
        method: 'POST',
        body: formData,
        signal,
      })
    },
    templateRecognitionState: async (templateId, signal) => {
      const response = await authFetch(
        `/api/batch-grade/template/${encodeURIComponent(templateId)}/recognition`,
        { signal },
      )
      if (!response.ok) {
        const error = new Error(`模板识别状态读取失败 (${response.status})`)
        error.status = response.status
        error.sseFailureKind = 'http'
        throw error
      }
      return response.json()
    },
    templateConfirm: (payload, signal) => http.post('/batch-grade/template/confirm', payload, { signal }),
    templateCorrectionCandidates: (payload, signal) => http.post('/batch-grade/template/correction-candidates', payload, { signal }),
    templateBankImport: (templateId) => http.post(`/batch-grade/template/${encodeURIComponent(templateId)}/bank-import`),
    templateCutCandidates: (templateId, payload, signal) => authFetch(
      `/api/batch-grade/template/${encodeURIComponent(templateId)}/cut-candidates`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal,
      },
    ),
    templateCutCandidatesState: async (templateId) => {
      const response = await authFetch(
        `/api/batch-grade/template/${encodeURIComponent(templateId)}/cut-candidates`,
      )
      if (!response.ok) throw new Error(`裁题状态读取失败 (${response.status})`)
      return response.json()
    },
    templateLayoutOverrides: (templateId, payload) => http.put(
      `/batch-grade/template/${encodeURIComponent(templateId)}/layout-overrides`,
      payload,
      // The save view renders the server's complete geometry diagnostics in
      // one message. Avoid the interceptor emitting a second generic toast.
      { suppressErrorToast: true },
    ),
    templateQuestionEdit: (templateId, payload) => http.put(
      `/batch-grade/template/${encodeURIComponent(templateId)}/questions`,
      payload,
    ),
    templatePageImage: async (templateId, pageIndex) => {
      const blob = await http.get(
        `/batch-grade/template/${encodeURIComponent(templateId)}/pages/${encodeURIComponent(pageIndex)}/image`,
        { responseType: 'blob' },
      )
      return URL.createObjectURL(blob)
    },
    identityOptions: (batchId) => http.get(`/batch-grade/${encodeURIComponent(batchId)}/identity-options`),
    orientationState: (batchId) => http.get(
      `/batch-grade/${encodeURIComponent(batchId)}/orientation`,
    ),
    orientationConfirm: (batchId, paperId, formData) => http.post(
      `/batch-grade/${encodeURIComponent(batchId)}/paper/${encodeURIComponent(paperId)}/orientation-confirm`,
      formData,
      { timeout: OCR_CONFIRM_TIMEOUT_MS },
    ),
    // Teacher removes one unrecoverable OCR failure so the rest can be graded.
    excludePaper: (batchId, paperId) => http.post(
      `/batch-grade/${encodeURIComponent(batchId)}/paper/${encodeURIComponent(paperId)}/exclude`,
    ),
    paperImage: async (batchId, paperId, variant = 'active') => {
      const response = await http.get(
        `/batch-grade/${encodeURIComponent(batchId)}/paper/${encodeURIComponent(paperId)}/image`,
        { params: { variant }, responseType: 'blob' },
      )
      return URL.createObjectURL(response)
    },
    reviewImage: async (batchId, paperId, decisions) => {
      const response = decisions?.length ? await http.post(
        '/batch-grade/review/image-preview',
        { batch_id: batchId, paper_id: paperId, decisions },
        { responseType: 'blob' },
      ) : await http.get(
        `/batch-grade/${encodeURIComponent(batchId)}/paper/${encodeURIComponent(paperId)}/image`,
        { params: { variant: 'review' }, responseType: 'blob' },
      )
      return URL.createObjectURL(response)
    },
    // 批改结果先暂存于 QuickBatch；老师审阅后再作废/归档/导出。
    reviewVoid: (payload) => http.post('/batch-grade/review/void', payload),
    reviewVoidAll: (payload) => http.post('/batch-grade/review/void-all', payload),
    reviewDecision: (payload) => http.post('/batch-grade/review/decision', payload),
    reviewGet: (batchId) => http.get(`/batch-grade/review/${encodeURIComponent(batchId)}`),
    reportGet: (batchId) => http.get(`/batch-grade/report/${encodeURIComponent(batchId)}`),
    reviewArchiveAll: (payload) => http.post('/batch-grade/review/archive-all', payload),
    reviewDownload: async (endpoint, payload, filename, timeout = 60000) => {
      const resp = await http.request({
        url: endpoint,
        method: 'POST',
        timeout,
        data: payload,
        responseType: 'blob',
        transformResponse: [(d) => d],
        validateStatus: () => true,
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp])
      if (blob.type.includes('application/json')) {
        const text = await blob.text()
        let detail = '导出失败'
        try { detail = JSON.parse(text).detail || detail } catch { /* ignore malformed error */ }
        throw new Error(detail)
      }
      triggerBlobDownload(blob, filename)
    },
  },

  usage: {
    stats: () => http.get('/usage/stats'),
    check: () => http.get('/usage/check'),
  },

  answerSheet: {
    availability: () => http.get('/answer-sheet/availability', {
      timeout: AVAILABILITY_TIMEOUT_MS,
      suppressErrorToast: true,
    }),
    createSession: (payload) => http.post('/answer-sheet/sessions', payload),
    listSessions: () => http.get('/answer-sheet/sessions'),
    paperTemplates: () => http.get('/answer-sheet/paper-templates'),
    paperTemplateSources: () => http.get('/answer-sheet/paper-templates/sources'),
    registerPaperTemplate: (templateId, alias = '') => http.post(
      '/answer-sheet/paper-templates',
      { template_id: templateId, alias },
    ),
    getSession: (sessionId) => http.get(`/answer-sheet/sessions/${encodeURIComponent(sessionId)}`),
    deleteSession: (sessionId) => http.delete(`/answer-sheet/sessions/${encodeURIComponent(sessionId)}`),
    uploadSource: (sessionId, formData) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/source/upload`,
      formData,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    recognizeSource: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/source/recognize`,
      undefined,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    recognizeSourceStream: (sessionId, signal) => authFetch(
      `/api/answer-sheet/sessions/${encodeURIComponent(sessionId)}/source/recognize/stream`,
      { method: 'POST', signal },
    ),
    confirmAnswers: (sessionId, questions) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/answers/confirm`,
      { questions },
    ),
    uploadLayout: (sessionId, formData) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/upload`,
      formData,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    processLayout: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/process`,
      undefined,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    locateLayout: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/locate`,
      undefined,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    templateHistory: () => http.get('/answer-sheet/templates/history'),
    useTemplateCode: (sessionId, templateCode) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/templates/use`,
      { template_code: templateCode },
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    publishTemplateCode: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/templates/publish`,
      undefined,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    saveLayout: (sessionId, payload) => http.put(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout`,
      payload,
    ),
    freezeLayout: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/freeze`,
    ),
    layoutPageImage: async (sessionId, pageId) => {
      const blob = await http.get(
        `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/pages/${encodeURIComponent(pageId)}/image`,
        { responseType: 'blob' },
      )
      return URL.createObjectURL(blob)
    },
    startStudentUpload: (sessionId, restart = false) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/upload/start`,
      { restart: Boolean(restart) },
    ),
    uploadStudents: (sessionId, uploadId, file, sourcePath = '') => {
      const formData = new FormData()
      formData.append('upload_id', uploadId)
      formData.append('files', file)
      // Folder/archive path: display and identity metadata, never storage.
      if (sourcePath) formData.append('source_path', sourcePath)
      return http.post(
        `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/upload`,
        formData,
        { timeout: GRADING_OPERATION_TIMEOUT_MS },
      )
    },
    processStudentUploadStream: (sessionId, uploadId, signal) => authFetch(
      `/api/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/upload/${encodeURIComponent(uploadId)}/process/stream`,
      { method: 'POST', signal },
    ),
    discardStudentUpload: (sessionId, uploadId) => http.delete(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/upload/${encodeURIComponent(uploadId)}`,
    ),
    studentPageImage: async (sessionId, paperId) => {
      const blob = await http.get(
        `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/${encodeURIComponent(paperId)}/image`,
        { responseType: 'blob' },
      )
      return URL.createObjectURL(blob)
    },
    confirmOrientation: (sessionId, paperId, turns) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/${encodeURIComponent(paperId)}/orientation`,
      { quarter_turns_clockwise: turns },
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    recognizeStudents: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/recognize`,
      undefined,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    recognizeStudentsStream: (sessionId, signal) => authFetch(
      `/api/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/recognize/stream`,
      { method: 'POST', signal },
    ),
    confirmStudentReview: (sessionId, paperId, payload) => http.post(`/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/${encodeURIComponent(paperId)}/review-confirm`, payload),
    gradingDraft: (sessionId) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/grading/draft`,
      undefined,
      { timeout: GRADING_OPERATION_TIMEOUT_MS },
    ),
    gradingDraftStream: (sessionId, signal) => authFetch(
      `/api/answer-sheet/sessions/${encodeURIComponent(sessionId)}/grading/draft/stream`,
      { method: 'POST', signal },
    ),
    gradingDecision: (sessionId, payload) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/grading/decision`,
      payload,
    ),
    confirmBubbleGrids: (sessionId, grids) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/bubble-grids/confirm`,
      { grids },
    ),
    discardBubbleGrids: (sessionId, keys) => http.post(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/layout/bubble-grids/discard`,
      { keys },
    ),
    studentEvidenceIndex: (sessionId, paperId) => http.get(
      `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/${encodeURIComponent(paperId)}/evidence`,
    ),
    studentEvidenceFile: async (sessionId, paperId, questionNo, fileName) => {
      const path = `/answer-sheet/sessions/${encodeURIComponent(sessionId)}/students/${encodeURIComponent(paperId)}/evidence/${encodeURIComponent(questionNo)}/${encodeURIComponent(fileName)}`
      if (/\.png$/i.test(fileName)) {
        const blob = await http.get(path, { responseType: 'blob' })
        return URL.createObjectURL(blob)
      }
      return http.get(path, { responseType: 'text' })
    },
    archive: (sessionId) => http.post(`/answer-sheet/sessions/${encodeURIComponent(sessionId)}/grading/archive`),
  },

  student: {
    list: (params) => http.get('/student/list', { params }),
    // Every student visible in the current class scope; /student/list pages at 100.
    listAll: async (params = {}) => {
      const items = []
      for (let page = 1; page <= STUDENT_PAGE_LIMIT; page += 1) {
        const data = await http.get('/student/list', { params: { ...params, page, page_size: STUDENT_PAGE_SIZE } })
        const rows = data?.items || data?.students || []
        items.push(...rows)
        if (!rows.length || items.length >= Number(data?.total ?? items.length)) break
      }
      return { items, students: items, total: items.length }
    },
    classDetail: (id, params) => http.get(`/student/${id}/detail`, { params }),
    correctWeakness: (id, topicKey, status) =>
      http.post(`/student/${id}/weakness/correct`, { status }, { params: { topic_key: topicKey } }),
    report: (id, payload) => http.post(`/student/${id}/report`, payload),
    create: (payload) => http.post('/student/add', payload),
    update: (id, payload) => http.put(`/student/${id}`, payload),
    remove: (id) => http.delete(`/student/${id}`),

  },

  bank: {
    list: (params) => http.get('/teacher/bank', { params }),
    chapters: (params) => http.get('/teacher/chapters', { params }),
    update: (id, payload) => http.put(`/teacher/bank/${id}`, payload),

    remove: (id) => http.delete(`/teacher/bank/${id}`),
    exportPrivate: (params = {}) => authFetch(`/api/bank/export?${new URLSearchParams(params)}`),
    photoRecognize: (formData) => http.post('/bank/photo-import/recognize', formData, { timeout: GRADING_OPERATION_TIMEOUT_MS }),
    photoRestore: (importId) => http.get(`/bank/photo-import/${encodeURIComponent(importId)}`),
    photoSave: (payload) => http.post('/bank/photo-import/save', payload),
    pendingAnswers: (params, config = {}) => http.get('/bank/pending-answers', { ...config, params }),
    confirmAnswers: (payload) => http.post('/bank/confirm-answers', payload),
    deletePendingAnswers: (payload) => http.post('/bank/pending-answers/delete', payload),
  },

  stats: {
    student: (studentId, params) => http.get(`/stats/${studentId}`, { params }),
  },

  report: {
    summary: (payload) => http.post('/reports/summary', payload),
  },

  file: {
    recent: () => http.get('/recent-files'),
    downloadUrl: async (url, filename = 'download') => {
      const path = localApiPath(url)
      const resp = await http.request({
        url: path,
        method: 'GET',
        responseType: 'blob',
        transformResponse: [(d) => d],
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp])
      triggerBlobDownload(blob, filename)
    },
    downloadGenerated: async (id, filename = 'report.docx') => {
      const resp = await http.request({
        url: `/download/${id}`,
        method: 'GET',
        responseType: 'blob',
        transformResponse: [(d) => d],
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp])
      triggerBlobDownload(blob, filename)
    },
  },

  chat: {
    send: (message, history = [], signal) => http.post('/chat', { message, history }, { signal, timeout: MODEL_TOOL_TIMEOUT_MS }),
    // 路线A：执行小M返回的 download 意图，二次请求已鉴权的导出接口，触发浏览器下载
    download: async ({ url, method = 'POST', payload = null, filename = 'download' }) => {
      const path = localApiPath(url)
      const resp = await http.request({
        url: path,
        method,
        data: method.toUpperCase() === 'POST' ? (payload || {}) : undefined,
        params: method.toUpperCase() === 'GET' ? (payload || {}) : undefined,
        responseType: 'blob',
        // 覆盖全局响应拦截：保留完整 response 以便读取 blob 与响应头
        transformResponse: [(d) => d],
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp])
      triggerBlobDownload(blob, filename)
    },
  },

  // 办公助手：上传模板（截图或 Word/Excel/PPT/PDF 文件）+ 指令 → 小M 生成文件，直接下载
  visionOffice: {
    submit: async (file, message = '', format = 'auto', signal) => {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('message', message)
      fd.append('format', format)
      const resp = await http.request({
        url: '/vision-office',
        method: 'POST',
        data: fd,
        responseType: 'blob',
        signal,
        // Parse (≤120 s) + image description (≤120 s) + several agent turns can
        // exceed 5 minutes; wait as long as other long operations (nginx: 720 s).
        timeout: GRADING_OPERATION_TIMEOUT_MS,
        // ChatBar shows one tailored message (including the timeout case).
        suppressErrorToast: true,
        transformResponse: [(d) => d],
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp])
      // 错误时后端返回 JSON(blob)，尝试解析出 detail
      if (blob.type && blob.type.includes('application/json')) {
        const text = await blob.text()
        let detail = '生成失败'
        try { detail = JSON.parse(text).detail || detail } catch { /* ignore */ }
        throw new Error(detail)
      }
      // 文件名：默认值（响应头经 axios 暴露受限，用时间戳兜底）
      const extByType = {
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
      }
      const ext = extByType[blob.type] || 'docx'
      const filename = `小M办公_${Date.now()}.${ext}`
      return { blob, filename }
    },
  },

  settings: {
    get: () => http.get('/settings'),
    save: (payload) => http.post('/settings/save', payload),
    testConnection: (payload) => http.post('/settings/test', payload),
    // 模型密钥职责组：简单模式两槽 / 专家模式五槽
    getModelProfiles: () => http.get('/settings/model-profiles'),
    saveSimpleProfiles: (payload) => http.post('/settings/model-profiles/simple', payload),
    saveExpertProfile: (payload) => http.post('/settings/model-profiles', payload),
    testModelProfile: (payload) => http.post('/settings/model-profiles/test', payload),
  },

  exercise: {
    generate: (payload) => http.post('/exercises/generate', payload),
    exportDocx: (payload) => http.post('/exercise/export-docx', payload),
    exportAnswerSheetDocx: (payload) => http.post('/exercise/export-answer-sheet-docx', payload),
  },

  courseware: {
    list: (params) => http.get('/courseware', { params }),
    recommendations: (params) => http.get('/courseware/recommendations', { params }),
    preview: (id) => http.get(`/courseware/${encodeURIComponent(id)}/preview`, {
      responseType: 'text',
      transformResponse: [(data) => data],
    }),
    download: async (id, filename = '课件.html') => {
      const resp = await http.get(`/courseware/${encodeURIComponent(id)}/download`, {
        responseType: 'blob',
        transformResponse: [(data) => data],
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp], { type: 'text/html;charset=utf-8' })
      triggerBlobDownload(blob, filename)
    },
  },

  pending: {
    list: () => http.get('/teacher/pending'),
  },

  // 整卷真题：只按目录身份（试卷 id + 文件类型）访问，不传任何路径。
  pastPapers: {
    list: (params = {}, config = {}) => http.get('/past-papers', { ...config, params }),
    detail: (paperId) => http.get(`/past-papers/${encodeURIComponent(paperId)}`),
    previewImage: async (paperId, kind, page) => {
      const blob = await http.get(
        `/past-papers/${encodeURIComponent(paperId)}/preview/${encodeURIComponent(kind)}/${Number(page)}`,
        { responseType: 'blob', suppressErrorToast: true },
      )
      return URL.createObjectURL(blob)
    },
    download: async (paperId, kind, filename) => {
      const resp = await http.request({
        url: `/past-papers/${encodeURIComponent(paperId)}/download/${encodeURIComponent(kind)}`,
        method: 'GET',
        responseType: 'blob',
        transformResponse: [(d) => d],
        // PastPapers.vue reports the failure itself; avoid a second toast.
        suppressErrorToast: true,
      })
      const blob = resp instanceof Blob ? resp : new Blob([resp], { type: 'application/pdf' })
      triggerBlobDownload(blob, filename)
    },
  },
  capture: {
    open: () => http.post('/capture/sessions'),
    get: (sessionId, config = {}) => http.get(`/capture/sessions/${encodeURIComponent(sessionId)}`, config),
    image: (sessionId, seq) => http.get(
      `/capture/sessions/${encodeURIComponent(sessionId)}/images/${Number(seq)}`,
      { responseType: 'blob', suppressErrorToast: true },
    ),
    deleteImage: (sessionId, seq) => http.delete(`/capture/sessions/${encodeURIComponent(sessionId)}/images/${Number(seq)}`),
    close: (sessionId, config = {}) => http.delete(`/capture/sessions/${encodeURIComponent(sessionId)}`, config),
  },
  // 学生上传：班级链接（教师端）与学生提交密码
  submissions: {
    list: (classId, config = {}) => http.get('/submission-links', { ...config, params: { class_id: classId } }),
    create: (payload) => http.post('/submission-links', payload),
    update: (linkId, payload) => http.put(`/submission-links/${Number(linkId)}`, payload),
    remove: (linkId) => http.delete(`/submission-links/${Number(linkId)}`),
    status: (linkId, config = {}) => http.get(`/submission-links/${Number(linkId)}/submissions`, config),
    image: (linkId, pageId) => http.get(
      `/submission-links/${Number(linkId)}/pages/${Number(pageId)}/image`,
      { responseType: 'blob', suppressErrorToast: true },
    ),
    clearStudent: (linkId, studentId) => http.delete(`/submission-links/${Number(linkId)}/students/${Number(studentId)}`),
    passwordState: (classId, studentId) => http.get(`/class/${classId}/students/${studentId}/submit-password`),
    setPassword: (classId, studentId, password) => http.put(`/class/${classId}/students/${studentId}/submit-password`, { password }),
    clearPassword: (classId, studentId) => http.delete(`/class/${classId}/students/${studentId}/submit-password`),
  },
}

// The phone capture and student upload pages hold only a link token, never a
// login, so they must bypass the login header and the 401 -> login redirect above.
const publicHttp = axios.create({ baseURL: apiBaseUrl, timeout: 60000 })

// Resolves to the response body; errors carry the server's message and the
// HTTP `status` (0 when offline).
function publicRequest(request) {
  return request.then((resp) => resp.data, (err) => {
    const detail = err.response?.data?.detail
    const error = new Error(typeof detail === 'string' && detail ? detail : '网络异常，请检查手机网络')
    error.status = err.response?.status || 0
    return Promise.reject(error)
  })
}

// The calls the phone makes for one capture session.
export function phoneCaptureClient(sessionId, token) {
  const base = `/capture/phone/${encodeURIComponent(sessionId)}`
  const config = { headers: { 'X-Capture-Token': token } }
  return {
    state: () => publicRequest(publicHttp.get(base, config)),
    upload: (blob, clientId) => {
      const form = new FormData()
      form.append('file', blob, `${clientId}.jpg`)
      form.append('client_id', clientId)
      return publicRequest(publicHttp.post(`${base}/images`, form, config))
    },
    remove: (seq) => publicRequest(publicHttp.delete(`${base}/images/${Number(seq)}`, config)),
  }
}

// The calls a student makes on a class upload link.  Every call carries the
// identity typed on the page; the server checks it against the class roster.
export function studentSubmitClient(token) {
  const base = `/submit/${encodeURIComponent(token)}`
  const identityFields = ({ name, studentNo, password }) => ({
    name: String(name || '').trim(),
    student_no: String(studentNo || '').trim(),
    password: String(password || ''),
  })
  return {
    info: () => publicRequest(publicHttp.get(base)),
    status: (identity) => publicRequest(publicHttp.post(`${base}/status`, identityFields(identity))),
    upload: (identity, pageNo, blob) => {
      const form = new FormData()
      for (const [key, value] of Object.entries(identityFields(identity))) form.append(key, value)
      form.append('page_no', String(pageNo))
      form.append('file', blob, `page-${pageNo}.jpg`)
      return publicRequest(publicHttp.post(`${base}/pages`, form))
    },
    remove: (identity, pageNo) => publicRequest(
      publicHttp.post(`${base}/pages/${Number(pageNo)}/delete`, identityFields(identity)),
    ),
    move: (identity, pageNo, toPageNo) => publicRequest(publicHttp.post(
      `${base}/pages/${Number(pageNo)}/move`,
      { ...identityFields(identity), to_page_no: Number(toPageNo) },
    )),
  }
}

export default api
