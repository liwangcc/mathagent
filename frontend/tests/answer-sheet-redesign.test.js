import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { computed, defineComponent, effectScope, h, ref, watch } from 'vue'

const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const view = read('../src/views/AnswerSheet.vue')
const api = read('../src/api/index.js')

test('批次不再选择批阅类型或班级，班级来自全局当前班级', () => {
  assert.doesNotMatch(view, /批阅类型/)
  assert.doesNotMatch(view, /setup\.category|setup\.value\.category/)
  assert.doesNotMatch(view, /label="考试名称"/)
  assert.doesNotMatch(view, /createSession\(\{[^}]*class_id/)
  assert.doesNotMatch(view, /createSession\(\{[^}]*category/)
  assert.match(view, /auth\.currentClassName/)
  assert.match(view, /批次自动继承/)
})

test('批次列表接口按答题卡模板号加时间戳展示', () => {
  assert.match(api, /listSessions: \(\) => http\.get\('\/answer-sheet\/sessions'\)/)
  assert.match(view, /item\.batch_label \|\| '待绑定答题卡模板'/)
  assert.match(view, /批次 \{\{ session\.batch_label \}\}/)
})

test('建批次向导使用试卷模板选择与登记，不再提交空请求', () => {
  assert.doesNotMatch(view, /createSession\(\{\}\)/)
  assert.match(view, /v-for="item in paperTemplates"/)
  assert.match(view, /v-for="item in paperTemplateSources"/)
  assert.match(view, /登记试卷模板/)
  assert.match(view, /api\.answerSheet\.registerPaperTemplate\(templateId/)
})

// Execute the real <script setup> with mocked dependencies so the creation
// flow is checked against the backend request contract, not just its text.
const script = view
  .split('<script setup>')[1]
  .split('</script>')[0]
  .replace(/^import[\s\S]*?from\s+'[^']+'\s*$/gm, '')
const exported = [
  'batchSetup', 'paperTemplates', 'paperTemplateSources', 'paperRegistration', 'batches',
  'canCreate', 'createSession', 'registerPaperTemplate', 'loadBatchChoices', 'openBatch',
  'closeSession', 'session', 'activeStage', 'isBatch', 'visibleStages', 'serviceState', 'statusLabels',
]
const parameters = [
  'computed', 'defineComponent', 'h', 'onBeforeUnmount', 'onMounted', 'ref', 'watch',
  'ElMessage', 'ElMessageBox', 'notifyProcessingError', 'UploadFilled', 'useRoute', 'api',
  'mergeHardwareAvailabilityStatus', 'normalizeHardwareAvailability',
  'appendDesktopCanonicalCandidates', 'compressImageFiles', 'validateUploadList', 'useAuthStore',
  'renderLatex', 'HelpButton', 'consumeSse', 'createSseEventError', 'createSseResponseError',
  'isSseRequestCancelled', 'isSseServiceFault', 'SseTransportError', 'localStorage',
]
const factory = new Function(...parameters, `${script}\nreturn { ${exported.join(', ')} }`)

const PAPER = 'PT-AAAA-BBBB-CCCC-DDDD'
const SHEET = 'AS-1111-2222-3333-4444'

function harness(t, { createResponse } = {}) {
  const calls = { create: [], register: [], getSession: [], listSessions: 0, deleteSession: 0 }
  const stored = new Map()
  let registered = []
  const answerSheet = {
    paperTemplates: async () => ({ templates: registered }),
    paperTemplateSources: async () => ({
      sources: [{ template_id: 'tpl-1', exam_name: '期中考试', question_count: 4, confirmed_at: '2026-09-27T06:00:00+00:00' }],
    }),
    listSessions: async () => {
      calls.listSessions += 1
      return { batches: [{ session_id: 'b1', batch_label: `${SHEET}-20260927-143000`, paper_template_code: PAPER, status: 'frozen', created_at: '2026-09-27T06:30:00+00:00' }] }
    },
    registerPaperTemplate: async (templateId, alias) => {
      calls.register.push([templateId, alias])
      registered = [{ template_code: PAPER, alias, exam_name: '期中考试', question_count: 4 }]
      return { template: registered[0] }
    },
    createSession: async (payload) => {
      calls.create.push(payload)
      return createResponse(payload)
    },
    getSession: async (sessionId) => {
      calls.getSession.push(sessionId)
      return batchSession({ session_id: sessionId, status: 'frozen', batch_label: `${SHEET}-20260927-143000` })
    },
    deleteSession: async () => { calls.deleteSession += 1 },
    templateHistory: async () => ({ templates: [] }),
  }
  const auth = { currentClassId: 10, currentClassName: '一班', classes: [] }
  const message = { success() {}, warning() {}, error() {}, info() {} }
  const storage = {
    getItem: key => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, String(value)),
    removeItem: key => stored.delete(key),
  }
  const scope = effectScope()
  t.after(() => scope.stop())
  const state = scope.run(() => factory(
    computed, defineComponent, h, () => {}, () => {}, ref, watch,
    message, { confirm: async () => {} }, () => {}, {}, () => ({ query: {} }), { answerSheet },
    () => ({}), () => ({ status: 'ready' }),
    async () => {}, async files => files, () => ({ ok: true }), () => auth,
    text => String(text || ''), {}, async () => {}, () => new Error('sse'), async () => new Error('sse'),
    () => false, () => false, Error, storage,
  ))
  state.serviceState.value = { status: 'available', label: '可用', message: '' }
  return { state, calls, stored }
}

function batchSession(overrides = {}) {
  return {
    session_id: 's-new',
    teacher_id: 1,
    class_id: 10,
    status: 'answers_confirmed',
    batch_contract_version: 'answer-sheet-batch-v2',
    batch_label: '',
    answer_sheet_template_code: '',
    paper_template_code: PAPER,
    answer_spec_snapshot: [{ question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: 'A' }],
    answer_sheet_pages: [],
    answer_regions: [],
    identity_regions: [],
    grouping_decisions: [],
    ...overrides,
  }
}

test('创建批次必须先选试卷模板，请求只携带模板号（无班级/类型字段）', async (t) => {
  const { state, calls, stored } = harness(t, { createResponse: () => batchSession() })
  await state.loadBatchChoices()
  assert.equal(state.canCreate.value, false)

  state.batchSetup.value.paper_template_code = PAPER
  assert.equal(state.canCreate.value, true)
  await state.createSession()

  assert.deepEqual(calls.create, [{ paper_template_code: PAPER }])
  assert.equal(state.isBatch.value, true)
  assert.deepEqual(state.visibleStages.value.slice(0, 2).map(item => item.title), ['试卷模板', '题目答案'])
  // Answers come from the frozen paper template: continue at the answer-sheet stage.
  assert.equal(state.activeStage.value, 2)
  assert.equal(stored.get('mathagent_answer_sheet_session_id'), 's-new')
  assert.deepEqual(state.batchSetup.value, { paper_template_code: '', answer_sheet_template_code: '' })
})

test('建批次时选择答题卡模板号：规范为大写提交，批次获得模板号加时间戳标签', async (t) => {
  const label = `${SHEET}-20260927-143000`
  const { state, calls } = harness(t, {
    createResponse: payload => batchSession({
      status: 'frozen',
      answer_sheet_template_code: payload.answer_sheet_template_code,
      batch_label: label,
    }),
  })
  state.batchSetup.value = { paper_template_code: PAPER, answer_sheet_template_code: ` ${SHEET.toLowerCase()} ` }
  await state.createSession()

  assert.deepEqual(calls.create, [{ paper_template_code: PAPER, answer_sheet_template_code: SHEET }])
  assert.equal(state.session.value.batch_label, label)
  assert.equal(state.activeStage.value, 4)
})

test('登记试卷模板后刷新列表并自动选中新模板号', async (t) => {
  const { state, calls } = harness(t, { createResponse: () => batchSession() })
  await state.loadBatchChoices()
  assert.equal(state.paperTemplates.value.length, 0)
  assert.equal(state.paperTemplateSources.value[0].template_id, 'tpl-1')

  state.paperRegistration.value = { template_id: 'tpl-1', alias: ' 期中A卷 ' }
  await state.registerPaperTemplate()

  assert.deepEqual(calls.register, [['tpl-1', '期中A卷']])
  assert.deepEqual(state.paperTemplates.value.map(item => item.template_code), [PAPER])
  assert.equal(state.batchSetup.value.paper_template_code, PAPER)
  assert.deepEqual(state.paperRegistration.value, { template_id: '', alias: '' })
})

test('批次列表按标签重新打开批次；返回列表不删除批次', async (t) => {
  const { state, calls, stored } = harness(t, { createResponse: () => batchSession() })
  await state.loadBatchChoices()
  assert.equal(state.batches.value[0].batch_label, `${SHEET}-20260927-143000`)

  await state.openBatch('b1')
  assert.deepEqual(calls.getSession, ['b1'])
  assert.equal(state.session.value.session_id, 'b1')
  assert.equal(stored.get('mathagent_answer_sheet_session_id'), 'b1')

  const listedBefore = calls.listSessions
  state.closeSession()
  assert.equal(state.session.value, null)
  assert.equal(stored.has('mathagent_answer_sheet_session_id'), false)
  assert.equal(calls.deleteSession, 0)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls.listSessions, listedBefore + 1)
})
