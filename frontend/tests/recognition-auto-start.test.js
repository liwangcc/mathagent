import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, nextTick, reactive, ref, watch } from 'vue'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
function actualFunction(name) {
  const start = source.indexOf('function ' + name + '(')
  assert.ok(start >= 0, name)
  const end = source.indexOf('\n}', start) + 2
  return source.slice(source.slice(start - 6, start) === 'async ' ? start - 6 : start, end)
}
const validity = source.match(/const batchConfirmValid = computed\(\(\) => \{[^]*?\n\}\)/)[0]
const autoWatch = source.match(/watch\(\n  \(\) => \[\n    confirmedTemplateId\.value,[^]*?\n  maybeAutoStartRecognition,\n\)/)[0]
const functions = ['maybeAutoStartRecognition', 'onBatchConfirm', 'resetFiles', 'processFiles'].map(actualFunction).join('\n')

function harness(t) {
  const calls = []
  const messages = []
  const deps = {
    computed, watch,
    recognitionAutoArmed: ref(true), confirmedTemplateId: ref(''), fileList: ref([]),
    recognizing: ref(false), grading: ref(false), imagePreprocessing: ref(false),
    imageExtracting: ref(false), recognitionBlocksNewWork: ref(false),
    templateDialogVisible: ref(false), batchDialogVisible: ref(false), anonymous: ref(false),
    auth: reactive({ currentClassId: 1 }),
    form: reactive({ category: 'daily', exam_name: '', mode: 'precise' }),
    clearReviewDisplay() {},
    ElMessage: { warning: (message) => messages.push(message), error: (message) => messages.push(message) },
    validateUploadList: (files) => ({ files: files.map((file) => file.raw), errors: [] }),
    compressImageFiles: async (files) => files,
  }
  deps.requireRecognitionService = () => !deps.recognitionBlocksNewWork.value
  deps.runRecognize = () => {
    calls.push({ armed: deps.recognitionAutoArmed.value, mode: deps.form.mode, files: deps.fileList.value.length })
    deps.recognizing.value = true
    return Promise.resolve()
  }
  const code = validity + '\n' + functions + '\nconst stop = ' + autoWatch
    + '\nreturn { stop, maybeAutoStartRecognition, onBatchConfirm, resetFiles, processFiles }'
  const methods = new Function(...Object.keys(deps), code)(...Object.values(deps))
  t.after(methods.stop)
  return { ...deps, ...methods, calls, messages }
}

for (const order of ['template-first', 'files-first']) {
  test('starts exactly once when both inputs become ready: ' + order, async (t) => {
    const h = harness(t)
    if (order === 'template-first') h.confirmedTemplateId.value = 'synthetic-template'
    else h.fileList.value = [{}]
    await nextTick()
    assert.equal(h.calls.length, 0)
    if (order === 'template-first') h.fileList.value = [{}]
    else h.confirmedTemplateId.value = 'synthetic-template'
    await nextTick()
    assert.deepEqual(h.calls, [{ armed: false, mode: 'precise', files: 1 }])
    h.recognizing.value = false
    h.grading.value = true
    await nextTick()
    h.grading.value = false
    h.recognitionBlocksNewWork.value = true
    await nextTick()
    h.recognitionBlocksNewWork.value = false
    await nextTick()
    assert.equal(h.calls.length, 1, 'completion and readiness polling must not restart the same upload')
  })
}

test('prepared student pages and a closed correction dialog still wait for saved template confirmation', async (t) => {
  const h = harness(t)
  h.templateDialogVisible.value = true
  h.fileList.value = [{}]
  await nextTick()
  h.templateDialogVisible.value = false
  await nextTick()
  assert.equal(h.calls.length, 0, 'finishing correction alone must not start comparison or grading')
  h.confirmedTemplateId.value = 'answers-and-geometry-confirmed'
  await nextTick()
  assert.equal(h.calls.length, 1)
})

for (const gate of ['templateDialogVisible', 'imagePreprocessing', 'imageExtracting', 'recognizing', 'grading', 'recognitionBlocksNewWork']) {
  test('waits silently for ' + gate + ', then starts', async (t) => {
    const h = harness(t)
    h[gate].value = true
    h.confirmedTemplateId.value = 'synthetic-template'
    h.fileList.value = [{}]
    await nextTick()
    assert.equal(h.calls.length, 0)
    assert.deepEqual(h.messages, [])
    h[gate].value = false
    await nextTick()
    assert.equal(h.calls.length, 1)
  })
}

test('waits for class and exam settings, preserving the selected grading mode', async (t) => {
  const h = harness(t)
  h.auth.currentClassId = null
  h.form.category = 'exam'
  h.form.mode = 'simple'
  h.confirmedTemplateId.value = 'synthetic-template'
  h.fileList.value = [{}]
  await nextTick()
  h.auth.currentClassId = 2
  await nextTick()
  assert.equal(h.calls.length, 0)
  h.form.exam_name = 'Synthetic exam'
  await nextTick()
  assert.deepEqual(h.calls, [{ armed: false, mode: 'simple', files: 1 }])
})

test('anonymous mode can start without assigning a class', async (t) => {
  const h = harness(t)
  h.auth.currentClassId = null
  h.confirmedTemplateId.value = 'synthetic-template'
  h.fileList.value = [{}]
  await nextTick()
  assert.equal(h.calls.length, 0)
  h.anonymous.value = true
  await nextTick()
  assert.equal(h.calls.length, 1)
})

test('manual confirmation and the queued watcher share one launch', async (t) => {
  const h = harness(t)
  h.confirmedTemplateId.value = 'synthetic-template'
  h.fileList.value = [{}]
  h.onBatchConfirm()
  await nextTick()
  assert.equal(h.calls.length, 1)
  assert.equal(h.recognitionAutoArmed.value, false)
})

test('a new prepared upload rearms once and waits for archive extraction', async (t) => {
  const h = harness(t)
  h.confirmedTemplateId.value = 'synthetic-template'
  h.fileList.value = [{}]
  await nextTick()
  h.recognizing.value = false
  h.imageExtracting.value = true
  await h.processFiles([{ name: 'synthetic.jpg', type: 'image/jpeg' }], { fromExtraction: true })
  await nextTick()
  assert.equal(h.calls.length, 1)
  h.imageExtracting.value = false
  await nextTick()
  assert.equal(h.calls.length, 2)
  h.recognizing.value = false
  h.resetFiles()
  await nextTick()
  assert.equal(h.calls.length, 2)
  assert.equal(h.recognitionAutoArmed.value, true)
})

test('a per-paper result appears while recognition is still in progress', () => {
  const recognize = actualFunction('runRecognize')
  const callback = recognize.match(/await consumeSse\(response, \(evt, data\) => \{([^]*?)\n    \}\)/)
  assert.ok(callback)
  const progressList = ref([])
  const consumeEvent = new Function('progressList', 'return (evt, data) => {' + callback[1] + '\n}')(progressList)
  consumeEvent('result', { paper_id: 'fast-paper', correct: 1 })
  assert.deepEqual(progressList.value, [{ paper_id: 'fast-paper', correct: 1 }])
})
