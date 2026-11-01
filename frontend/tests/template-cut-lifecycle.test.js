import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { prepareTemplateSlots, serializeTemplateSlots, templateSlotBoxes } from '../src/utils/templateSlots.js'
import vm from 'node:vm'
import { createErrorNotifier } from '../src/utils/processingError.js'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const names = [
  'initializeTemplatePageSlots', 'stopTemplateCutPolling', 'releaseFinishedTemplateCut', 'applyTemplateCutState',
  'revokeTemplateCutPreviews', 'loadTemplateCutPreviews', 'pollTemplateCutState',
  'loadTemplateCutState', 'templateCutErrorMessage', 'onRunTemplateCut',
  'applyTemplateRecognitionResult', 'clearTemplateDraft',
]
const functions = names.map((name) => {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))
  assert.ok(match, `${name} must be present in Upload.vue`)
  return match[0]
}).join('\n')
const variableNames = ['templateCutRunToken', 'templateCutCompletedRunToken', 'templateCutPreviewLoadToken']
const variables = variableNames.map((name) => {
  const match = source.match(new RegExp(`let ${name} = [^\\r\\n]+`))
  assert.ok(match, `${name} initializer must be present in Upload.vue`)
  return match[0]
}).join('\n')
const ref = (value) => ({ value })
const clone = (value) => JSON.parse(JSON.stringify(value))
const flush = () => new Promise((resolve) => setImmediate(resolve))
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function complete(count = 1, extras = {}) {
  return {
    status: 'complete', image_type: 'scan', progress: { completed: count, total: count, remaining: 0 },
    pages: Array.from({ length: count }, (_, index) => ({
      page_index: index, page_label: `page-${index}`, question_anchors: [{ question_number: `${index + 1}`, question_number_bbox: [20, 30, 40, 50] }],
      anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }] } },
    })), ...extras,
  }
}
function harness(options = {}) {
  const calls = [], revoked = [], messages = [], timers = new Map()
  let timerId = 0
  const context = {
    recognitionAutoArmed: ref(true),
    AbortController, console, prepareTemplateSlots,
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id },
    clearTimeout: (id) => timers.delete(id),
    URL: { revokeObjectURL: (url) => revoked.push(url) },
    pendingTemplateId: ref('template-a'), confirmedTemplateId: ref(''),
    templateCutPreviewPages: ref([]), templateCutResult: ref(null), templateCutPreviewError: ref(''),
    templateCutProgress: ref({ completed: 0, total: 0, remaining: 0 }), templateCutImageType: ref('photo'),
    templateCutBusy: ref(false), templateCutActionId: ref(''),
    templatePages: ref([{ page_index: 0, questions: [] }]), templateDrawingRequests: new Map(),
    templateTeachingContext: ref({}), templateMode: ref('blank'), templateColumnMode: ref('single'),
    templateBankImported: ref(false), templateDialogVisible: ref(false), templateFiles: ref([]),
    templatePageCount: ref(0), templateChoiceScore: ref(5), templateFillScore: ref(5),
    recognizingTemplate: ref(false), confirmingTemplate: ref(false), importingTemplateBank: ref(false),
    templatePreprocessing: ref(false), templateRecognizeMessage: ref(''), templateRecognizeError: ref(''),
    templateReviewPendingPageIndexes: (data) => new Set(data.geometry_review_pending_pages || []),
    templatePageReviewPending: (page) => page.geometry_review_pending === true,
    cloneTemplateColumns: (items) => clone(items || []), cloneQuestionAnchors: (items) => clone(items || []),
    templateRegionCandidateUsable: (items) => Array.isArray(items) && items.length > 0,
    flattenTemplateHandwritingRegions: (items) => clone(items),
    mergeTemplateAnswerRegionSources: (answerRegions, legacyHandwritingRegions) => [
      ...clone(answerRegions || []),
      ...clone(legacyHandwritingRegions || []),
    ],
    mergeTemplateDrawingCandidates: (schema) => clone(schema), templateDrawingReview: () => ({}),
    forgetTemplateRecognition() {}, syncTemplateBatchScores() {},
    normalizeTemplateMode: (mode) => mode, normalizeTemplateColumnMode: (mode) => mode,
    normalizeAnswerSpecForEditor: (question) => ({ ...question }), requireTemplateService: () => true,
    loadTemplateCorrectionCandidates: async () => calls.push({ kind: 'corrections' }),
    ElMessage: Object.fromEntries(['success', 'error'].map((kind) => [kind, (text) => messages.push({ kind, text })])),
    notifyProcessingError: createErrorNotifier(text => messages.push({ kind: 'error', text })),
    api: { grade: {
      templatePageImage: (id, index) => { calls.push({ kind: 'image', id, index }); return options.image ? options.image(id, index) : Promise.resolve(`blob:${id}-${index}`) },
      templateCutCandidatesState: (id) => { calls.push({ kind: 'state', id }); return options.state ? options.state(id) : Promise.resolve(complete()) },
      templateCutCandidates: (id, payload, signal) => { calls.push({ kind: 'post', id, payload, signal }); return options.post ? options.post(id, payload, signal) : Promise.resolve({ ok: true, json: async () => complete() }) },
    } },
  }
  context.window = { clearTimeout: context.clearTimeout }
  vm.createContext(context)
  vm.runInContext(`${variables}
    let templateCutController = null, templateCutPollTimer = null
    let templateRecognizeController = null, templateConfirmController = null
    ${functions}`, context)
  return {
    ...context, calls, revoked, messages, timers,
    token: () => vm.runInContext('templateCutRunToken', context),
    advanceRun: () => vm.runInContext('++templateCutRunToken', context),
  }
}

test('first complete result at run zero loads the real preview once and unlocks the editor', async () => {
  const h = harness(), data = complete()
  h.templateCutBusy.value = true
  assert.equal(h.token(), 0)
  h.applyTemplateCutState(data)
  await flush()
  assert.equal(h.templateCutResult.value, data)
  assert.equal(h.templateCutPreviewPages.value.length, 1)
  assert.equal(h.templateCutPreviewPages.value[0].imageUrl, 'blob:template-a-0')
  assert.equal(h.templateCutBusy.value, false)
  h.applyTemplateCutState(complete())
  h.applyTemplateCutState({ status: 'running', progress: { completed: 0 } })
  assert.equal(h.templateCutResult.value, data, 'late nonterminal state cannot regress a finished run')
  assert.equal(h.calls.filter((call) => call.kind === 'image').length, 1)
})

test('recognition results get distinct runs and a late initial state cannot attach to another template', async () => {
  const old = deferred(), current = deferred()
  const h = harness({ state: (id) => id === 'old' ? old.promise : current.promise })
  const first = h.applyTemplateRecognitionResult('old', [], 'blank', 'single')
  const second = h.applyTemplateRecognitionResult('new', [], 'marked', 'single')
  old.resolve(complete())
  await first
  assert.equal(h.templateCutResult.value, null)
  assert.equal(h.calls.filter((call) => call.kind === 'image' || call.kind === 'post').length, 0)
  current.resolve(complete())
  await second
  await flush()
  assert.equal(h.pendingTemplateId.value, 'new')
  assert.equal(h.templateCutPreviewPages.value[0].imageUrl, 'blob:new-0')
  assert.equal(h.calls.filter((call) => call.kind === 'corrections').length, 1)
})

test('cancelled recognition-state lookup cannot restart a cut request or restore a discarded template', async () => {
  const state = deferred(), h = harness({ state: () => state.promise })
  const pending = h.applyTemplateRecognitionResult('old', [], 'blank', 'single')
  h.clearTemplateDraft()
  state.resolve({ status: 'not_run' })
  await pending
  assert.equal(h.pendingTemplateId.value, '')
  assert.equal(h.templateDialogVisible.value, false)
  assert.equal(h.calls.filter((call) => call.kind === 'post').length, 0)
})

test('an older preview failure cannot clear a newer run or cancel its drawing request', async () => {
  const old = deferred()
  let imageCall = 0
  const h = harness({ image: () => ++imageCall === 1 ? old.promise : Promise.resolve('blob:new') })
  const previous = h.loadTemplateCutPreviews(complete())
  h.advanceRun()
  await h.loadTemplateCutPreviews(complete())
  const drawing = new AbortController()
  h.templateDrawingRequests.set('new-drawing', drawing)
  old.reject(new Error('old image failed'))
  await previous
  assert.equal(h.templateCutPreviewPages.value[0].imageUrl, 'blob:new')
  assert.equal(h.templateCutPreviewError.value, '')
  assert.deepEqual(h.revoked, [])
  assert.equal(drawing.signal.aborted, false)
})

test('overlapping preview reloads within the same run keep the newest images', async () => {
  const old = deferred()
  let imageCall = 0
  const h = harness({ image: () => ++imageCall === 1 ? old.promise : Promise.resolve('blob:new') })
  const previous = h.loadTemplateCutPreviews(complete())
  await h.loadTemplateCutPreviews(complete())
  old.resolve('blob:old')
  await previous
  assert.equal(h.templateCutPreviewPages.value[0].imageUrl, 'blob:new')
  assert.deepEqual(h.revoked, ['blob:old'])
})

test('one failed page releases both already-loaded and later-successful images and allows retry', async () => {
  const images = [deferred(), deferred(), deferred()]
  const h = harness({ image: (_id, index) => images[index].promise })
  const loading = h.loadTemplateCutPreviews(complete(3))
  images[0].resolve('blob:first')
  await flush()
  images[1].reject(new Error('broken image'))
  await loading
  images[2].resolve('blob:last')
  await flush()
  assert.deepEqual(h.revoked.sort(), ['blob:first', 'blob:last'])
  assert.equal(h.templateCutPreviewPages.value.length, 0)
  assert.match(h.templateCutPreviewError.value, /读取失败/)
  h.api.grade.templatePageImage = async () => 'blob:retry'
  await h.loadTemplateCutState('template-a')
  await flush()
  assert.equal(h.templateCutPreviewError.value, '')
  assert.equal(h.templateCutPreviewPages.value[0].imageUrl, 'blob:retry')
})

test('cancelled preview reads revoke their own late blob and cannot repopulate the editor', async () => {
  const image = deferred(), h = harness({ image: () => image.promise })
  const loading = h.loadTemplateCutPreviews(complete())
  h.clearTemplateDraft()
  image.resolve('blob:cancelled')
  await loading
  assert.equal(h.templateCutPreviewPages.value.length, 0)
  assert.equal(h.templateCutPreviewError.value, '')
  assert.deepEqual(h.revoked, ['blob:cancelled'])
})

test('existing-template and polling state responses cannot cross a template or run boundary', async () => {
  for (const method of ['loadTemplateCutState', 'pollTemplateCutState']) {
    for (const change of ['template', 'run']) {
      const state = deferred(), h = harness({ state: () => state.promise })
      h.templateCutBusy.value = true
      const pending = h[method]('template-a')
      if (change === 'template') h.pendingTemplateId.value = 'template-b'
      else h.advanceRun()
      state.resolve(complete())
      await pending
      assert.equal(h.templateCutResult.value, null, `${method}/${change}`)
      assert.equal(h.calls.filter((call) => call.kind === 'image').length, 0)
    }
  }
})

test('poll completion unlocks before the POST settles; a late error cannot erase the success', async () => {
  const post = deferred(), h = harness({ post: () => post.promise })
  const pending = h.onRunTemplateCut()
  assert.equal(h.templateCutBusy.value, true)
  h.applyTemplateCutState(complete())
  await flush()
  const preview = h.templateCutPreviewPages.value[0]
  preview.dirty = true
  post.reject(new Error('late POST timeout'))
  await pending
  assert.equal(h.templateCutBusy.value, false)
  assert.equal(h.templateCutPreviewPages.value[0], preview)
  assert.equal(preview.dirty, true)
  assert.equal(h.messages.filter((message) => message.kind === 'error').length, 0)
  assert.equal(h.timers.size, 0)
})

test('a failed run exposes recovery and an older POST cannot unlock or overwrite the retry', async () => {
  const first = deferred(), second = deferred()
  let count = 0
  const h = harness({ post: () => ++count === 1 ? first.promise : second.promise })
  const old = h.onRunTemplateCut()
  const oldToken = h.token()
  h.applyTemplateCutState({ status: 'failed', message: 'synthetic failed' })
  assert.equal(h.templateCutBusy.value, false)
  assert.equal(h.templateCutPreviewError.value, 'synthetic failed')
  const retry = h.onRunTemplateCut({ force: true })
  assert.equal(h.calls[0].signal.aborted, true)
  assert.equal(h.templateCutPreviewError.value, '')
  first.resolve({ ok: true, json: async () => complete() })
  await old
  assert.equal(h.templateCutBusy.value, true)
  h.applyTemplateCutState(complete(), oldToken)
  assert.equal(h.calls.filter((call) => call.kind === 'image').length, 0)
  second.resolve({ ok: true, json: async () => complete() })
  await retry
  await flush()
  assert.equal(h.templateCutBusy.value, false)
  assert.equal(h.templateCutPreviewPages.value.length, 1)
  assert.equal(h.messages.filter((message) => message.kind === 'success').length, 1)
})

test('malformed or mismatched POST receipts do not overwrite an existing result or report success', async () => {
  for (const response of [{}, { status: 'running' }, complete(1, { action_id: 'different-run' })]) {
    const h = harness({ post: async () => ({ ok: true, json: async () => response }) })
    const previous = complete()
    h.templateCutResult.value = previous
    await h.onRunTemplateCut({ force: true })
    assert.equal(h.templateCutResult.value, previous)
    assert.equal(h.templateCutBusy.value, false)
    assert.equal(h.messages.filter((message) => message.kind === 'error').length, 1)
    assert.equal(h.messages.filter((message) => message.kind === 'success').length, 0)
  }
})

test('failed POST receipt displays the backend recovery message without a success notification', async () => {
  const h = harness({ post: async () => ({ ok: true, json: async () => ({ status: 'failed', error: { message: 'recoverable synthetic failure' } }) }) })
  await h.onRunTemplateCut()
  assert.equal(h.templateCutResult.value.status, 'failed')
  assert.equal(h.templateCutPreviewError.value, 'recoverable synthetic failure')
  assert.equal(h.templateCutBusy.value, false)
  assert.equal(h.messages.filter((message) => message.kind === 'success').length, 0)
})
