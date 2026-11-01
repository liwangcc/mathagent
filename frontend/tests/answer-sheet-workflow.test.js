import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const view = read('../src/views/AnswerSheet.vue')
const api = read('../src/api/index.js')
const router = read('../src/router/index.js')
const layout = read('../src/views/Layout.vue')

test('答题卡批阅是详细批改同级入口并使用独立懒加载路由', () => {
  assert.match(router, /path: 'answer-sheet'.*import\('@\/views\/AnswerSheet\.vue'\).*title: '答题卡批阅'/)
  const uploadIndex = layout.indexOf('index="/upload"')
  const answerSheetIndex = layout.indexOf('index="/answer-sheet"')
  const studentsIndex = layout.indexOf('index="/students"')
  assert.ok(uploadIndex >= 0 && uploadIndex < answerSheetIndex && answerSheetIndex < studentsIndex)
  assert.match(layout, /<Tickets \/>/)
})

test('API 客户端覆盖当前后端已开放的会话、语义与版面合同', () => {
  for (const method of [
    'createSession', 'getSession', 'deleteSession', 'uploadSource', 'recognizeSource',
    'confirmAnswers', 'uploadLayout', 'saveLayout', 'freezeLayout', 'layoutPageImage',
    'startStudentUpload', 'uploadStudents', 'processStudentUploadStream', 'discardStudentUpload',
  ]) {
    assert.match(api, new RegExp(`${method}:`))
  }
  assert.match(api, /source\/upload/)
  assert.match(api, /answers\/confirm/)
  assert.match(api, /layout\/pages\/\$\{encodeURIComponent\(pageId\)\}\/image/)
  assert.match(api, /responseType: 'blob'/)
})

test('五阶段工作流持久化 session id 并从 GET 会话刷新恢复', () => {
  for (const heading of ['原始试卷与答案来源', '教师确认题目与答案', '上传空白答题卡', '版面绑定与冻结', '学生答题卡与批阅']) {
    assert.match(view, new RegExp(heading))
  }
  assert.match(view, /mathagent_answer_sheet_session_id/)
  assert.match(view, /localStorage\.setItem\(STORAGE_KEY, data\.session_id\)/)
  assert.match(view, /api\.answerSheet\.getSession\(saved\)/)
  assert.match(view, /已恢复上次未完成的答题卡会话/)
  assert.match(view, /statusOrder/)
})

test('前端明确分离三层材料并保留三种答案来源', () => {
  for (const source of ['embedded', 'separate', 'ai_generated']) assert.match(view, new RegExp(`value="${source}"`))
  assert.match(view, /原始试卷提供题目语义，空白答题卡提供版面，学生答题卡才是批阅对象/)
  assert.match(view, /form\.append\('source_files'/)
  assert.match(view, /form\.append\('answer_files'/)
  assert.match(view, /form\.append\('answer_source'/)
})

test('答题卡可选复用模板码，新建模板仍兼容外部版式', () => {
  assert.match(view, /可直接复用共享模板码，也可上传新的空白答题卡/)
  assert.match(view, /或首次建立模板/)
  assert.match(view, /支持自制及其他平台版式；上传后自动粗定位，按页面顺序选择/)
})

test('模板码输入框显示当前教师历史并且复用路径不再定位', () => {
  for (const method of ['locateLayout', 'templateHistory', 'useTemplateCode', 'publishTemplateCode']) {
    assert.match(api, new RegExp(`${method}:`))
  }
  assert.match(view, /v-for="item in templateCodeHistory"/)
  assert.match(view, /输入模板码，或从历史记录中选择/)
  assert.match(view, /api\.answerSheet\.templateHistory\(\)/)
  assert.match(view, /api\.answerSheet\.useTemplateCode/)
  assert.match(view, /模板码已套用，无需模型重新定位/)
  assert.match(view, /provider_route !== 'qwen3\.8max'/)
  assert.match(view, /确认本页定位/)
  assert.match(view, /function confirmCurrentPageRegions\(\)/)
})

test('模板页和学生答题卡使用各自的文件数量上限', () => {
  assert.equal((view.match(/:max-files="20"/g) || []).length, 3)
  assert.match(view, /:max-files="200"/)
  assert.match(view, /selected\.slice\(0, props\.maxFiles\)/)
  assert.match(view, /单次最多选择 \$\{props\.maxFiles\} 个文件/)
})

test('答题卡页面失败关闭整链路状态并在故障时保留已有证据查看', () => {
  assert.match(api, /availability: \(\) => http\.get\('\/answer-sheet\/availability', \{\s*timeout: AVAILABILITY_TIMEOUT_MS,\s*suppressErrorToast: true,/)
  for (const label of ['检测中', '可用', '需排队', '暂不可用', '链路故障']) assert.match(view, new RegExp(label))
  assert.match(view, /window\.setInterval\(refreshAvailability, 15000\)/)
  assert.match(view, /\['checking', 'unavailable', 'fault'\]\.includes\(serviceState\.value\.status\)/)
  assert.match(view, /workflowReadonly = computed\(\(\) => serviceBlocksNewWork\.value && session\.value\?\.status !== 'archived'\)/)
  assert.match(view, /canDelete = computed\(\(\) => !serviceBlocksNewWork\.value/)
  assert.match(view, /:disabled="!canCreate \|\| serviceBlocksNewWork"/)
  assert.match(view, /\.workflow-readonly \.review-student-tabs/)
  assert.match(view, /\.workflow-readonly \.student-paper-pane/)
  assert.doesNotMatch(api, /active_teachers|queue_length|free_slots|queue_position/)
})

test('答题卡可用性轮询单飞并保持运行期链路故障，未知状态也失败关闭', () => {
  assert.match(view, /let availabilityCheckPending = false/)
  assert.match(view, /if \(availabilityCheckPending\) return/)
  assert.match(view, /availabilityCheckPending = true/)
  assert.match(view, /finally \{\s*availabilityCheckPending = false\s*\}/)
  assert.match(view, /const SERVICE_FAULT_HOLD_MS = 60_000/)
  assert.match(view, /Date\.now\(\) < serviceFaultHoldUntil/)
  assert.match(view, /if \(!labels\[state\?\.status\]\) \{\s*markServiceFault/)
  assert.match(view, /if \(effectiveStatus === 'fault'\) \{\s*markServiceFault/)
})

test('答题卡页面消费机会式硬件状态，等待节点不锁死恢复，故障节点关闭新图像工作', () => {
  for (const status of ['ready', 'waiting_accelerator', 'unavailable', 'fault', 'bypass']) {
    assert.match(api, new RegExp(`'${status}'`))
  }
  assert.match(api, /waiting_accelerator: 'queued'/)
  assert.match(api, /message: message \|\| HARDWARE_STATUS_FALLBACK_MESSAGES\[status\] \|\| ''/)
  assert.match(view, /normalizeHardwareAvailability\(state\)/)
  assert.match(view, /mergeHardwareAvailabilityStatus\(state\.status, hardware\.status\)/)
  assert.match(view, /hardware\.message \|\| state\.message/)
  assert.match(view, /\['unavailable', 'fault'\]\.includes\(hardwareState\.value\.status\)/)
  assert.match(view, /workflowReadonly = computed\(\(\) => serviceBlocksNewWork\.value && session\.value\?\.status !== 'archived'\)/)
  assert.match(view, /const hardwareServiceStatus = mergeHardwareAvailabilityStatus\('available', hardware\.status\)/)
  assert.match(view, /hardwareServiceStatus === effectiveStatus[\s\S]*hardware\.message \|\| state\.message[\s\S]*state\.message \|\| hardware\.message/)
})

test('答题卡新任务有函数级硬件门禁，不仅依赖鼠标样式', () => {
  assert.match(view, /function requireAnswerSheetService\(\)/)
  for (const functionName of [
    'runAnswerSheetStream',
    'createSession',
    'uploadSource',
    'uploadLayout',
    'uploadStudents',
    'restartStudentUpload',
    'confirmOrientation',
  ]) {
    assert.match(view, new RegExp(`(?:async )?function ${functionName}\\([^)]*\\) \\{\\s*if \\(!requireAnswerSheetService\\(\\)\\) return`))
  }
})

test('答题卡写操作把网络、5xx 与长请求超时升级为链路故障', () => {
  assert.match(view, /isSseServiceFault,/)
  assert.match(view, /error\?\.response\?\.status \|\| error\?\.status/)
  for (const code of ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK']) assert.match(view, new RegExp(code))
  assert.match(view, /isSseServiceFault\(normalizeServiceError\(error\)\)/)
  assert.match(view, /markServiceFault\('答题卡批阅链路中断/)
  assert.match(api, /const GRADING_OPERATION_TIMEOUT_MS = 660000/)
  assert.ok((api.match(/timeout: GRADING_OPERATION_TIMEOUT_MS/g) || []).length >= 7)
})

test('三条长链路使用有心跳的 SSE 并可从持久化检查点恢复', () => {
  for (const method of ['recognizeSourceStream', 'recognizeStudentsStream', 'gradingDraftStream']) {
    assert.match(api, new RegExp(`${method}:`))
    assert.match(view, new RegExp(`api\\.answerSheet\\.${method}`))
  }
  for (const endpoint of [
    '/source/recognize/stream',
    '/students/recognize/stream',
    '/grading/draft/stream',
  ]) assert.match(api, new RegExp(endpoint))
  assert.match(view, /consumeSse\(response/)
  assert.match(view, /new SseTransportError/)
  assert.match(view, /answerSheetStreamController\.abort\(\)/)
  assert.match(view, /api\.answerSheet\.getSession\(session\.value\.session_id\)/)
  assert.match(view, /recognition_checkpoint/)
  assert.match(view, /grading_draft_checkpoint/)
  assert.match(view, /进度会自动保存/)
})

test('SSE 加速节点状态区分等待、不可用与故障，并保留已保存进度', () => {
  for (const code of ['ACCELERATOR_WAITING', 'ACCELERATOR_UNAVAILABLE', 'ACCELERATOR_FAULT']) {
    assert.match(view, new RegExp(`${code}:`))
  }
  assert.match(view, /ACCELERATOR_WAITING:[\s\S]*hardwareStatus: 'waiting_accelerator'[\s\S]*serviceStatus: 'queued'[\s\S]*label: '等待加速节点'/)
  assert.match(view, /ACCELERATOR_UNAVAILABLE:[\s\S]*hardwareStatus: 'unavailable'[\s\S]*serviceStatus: 'unavailable'[\s\S]*label: '加速节点不可用'/)
  assert.match(view, /ACCELERATOR_FAULT:[\s\S]*hardwareStatus: 'fault'[\s\S]*serviceStatus: 'fault'[\s\S]*label: '加速节点故障'/)
  assert.match(view, /applyAcceleratorStreamError\(normalizedError\)/)
  assert.match(view, /if \(!acceleratorHandled && isSseServiceFault/)
  assert.match(view, /serviceStatus === 'queued'\) ElMessage\.warning/)
  assert.match(view, /if \(!acceleratorHandled\) notifyProcessingError\(error,/)
  assert.match(view, /已完成进度保留在当前会话/)

  const streamCatch = view.slice(
    view.indexOf('const normalizedError = normalizeServiceError(error)'),
    view.indexOf('return null', view.indexOf('const normalizedError = normalizeServiceError(error)')),
  )
  assert.ok(streamCatch.indexOf('applyAcceleratorStreamError') < streamCatch.indexOf('isSseServiceFault'))
})

test('链路探测不阻塞已保存会话恢复', () => {
  const availabilityIndex = view.indexOf('void refreshAvailability()')
  const restoreIndex = view.indexOf('api.answerSheet.getSession(saved)')
  assert.ok(availabilityIndex >= 0 && availabilityIndex < restoreIndex)
  assert.doesNotMatch(view, /await refreshAvailability\(\).*api\.answerSheet\.getSession\(saved\)/s)
})

test('版面表单支持归一化矩形、题号绑定、身份区域、保存与冻结门禁', () => {
  for (const type of ['choice_mark', 'choice_handwritten', 'fill', 'solve']) assert.match(view, new RegExp(type))
  for (const type of ['name', 'student_no', 'exam_no_text', 'qr', 'barcode', 'exam_no_marks', 'page_number']) assert.match(view, new RegExp(`${type}:`))
  assert.match(view, /min: 0, max: 1000/)
  assert.match(view, /answer_regions: answerRegions\.value, identity_regions: identityRegions\.value/)
  assert.match(view, /:disabled="!layoutSummary\.can_freeze"/)
  assert.match(view, /URL\.revokeObjectURL\(layoutImageUrl\.value\)/)
})

test('学生答题卡阶段调用真实上传、方向、识别、复核、判分与归档接口', () => {
  for (const method of ['uploadStudents', 'confirmOrientation', 'recognizeStudents', 'confirmStudentReview', 'gradingDraft', 'archive']) {
    assert.match(api, new RegExp(`${method}:`))
  }
  assert.match(view, /api\.answerSheet\.uploadStudents/)
  assert.match(view, /api\.answerSheet\.confirmOrientation/)
  assert.match(view, /api\.answerSheet\.recognizeStudents/)
  assert.match(view, /api\.answerSheet\.confirmStudentReview/)
  assert.match(view, /api\.answerSheet\.gradingDraft/)
  assert.match(view, /api\.answerSheet\.archive/)
  assert.doesNotMatch(view, /后端接口待开放/)
})

test('学生答题卡按单文件请求持久上传并可恢复处理', () => {
  assert.match(api, /students\/upload\/start/)
  assert.match(api, /\{ restart: Boolean\(restart\) \}/)
  assert.match(api, /students\/upload\/\$\{encodeURIComponent\(uploadId\)\}\/process\/stream/)
  assert.match(api, /discardStudentUpload: \(sessionId, uploadId\) => http\.delete/)

  const apiUploadStart = api.indexOf('uploadStudents: (sessionId, uploadId, file, sourcePath')
  const apiUploadEnd = api.indexOf('processStudentUploadStream:', apiUploadStart)
  assert.ok(apiUploadStart >= 0 && apiUploadEnd > apiUploadStart)
  const apiUpload = api.slice(apiUploadStart, apiUploadEnd)
  assert.equal((apiUpload.match(/formData\.append\('files', file\)/g) || []).length, 1)
  assert.doesNotMatch(apiUpload, /forEach|for \(/)

  const viewUploadStart = view.indexOf('async function uploadStudents()')
  const viewUploadEnd = view.indexOf('async function restartStudentUpload()', viewUploadStart)
  assert.ok(viewUploadStart >= 0 && viewUploadEnd > viewUploadStart)
  const viewUpload = view.slice(viewUploadStart, viewUploadEnd)
  assert.match(viewUpload, /for \(const \[index, file\] of files\.entries\(\)\)/)
  assert.match(viewUpload, /api\.answerSheet\.uploadStudents\([\s\S]*durableUploadId,[\s\S]*file,[\s\S]*\)/)
  assert.doesNotMatch(viewUpload, /new FormData|append\('files'/)
  assert.match(viewUpload, /startStudentUpload\(session\.value\.session_id, false\)/)

  assert.match(view, /api\.answerSheet\.processStudentUploadStream/)
  assert.match(view, /runAnswerSheetStream\([\s\S]*processStudentUploadStream/)
  assert.match(view, /startStudentUpload\(session\.value\.session_id, true\)/)
  assert.match(view, /:disabled="(?:busy \|\| )?studentUploadActive/)
  for (const field of ['uploaded_count', 'processed_count', 'total_bytes']) {
    assert.match(view, new RegExp(field))
  }
})
