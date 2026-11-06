import assert from 'node:assert/strict'
import test from 'node:test'
import { toRaw } from 'vue'
import { completeCut, deferred, openTemplateEditor } from './helpers/templateGeometryCut.js'

// The template's automatic cut on the real 批改 page: the cut state the page
// reads (when a template opens, and by polling a run), the cut run it starts
// (重新自动裁剪, or by itself for an uncut template), and the page images it
// loads for the editor. Every answer is held until the test releases it, so
// each test can deliver a late answer after the template or the run changed.

// The recognised template the teacher comes back to (see openTemplateEditor).
const RECOGNISED = [{
  page_index: 0,
  page_label: 'page-0',
  questions: [{ question_no: '1', question_text: '合成题干', standard_answer: '合成答案', question_type: 'fill' }],
}]
const CORRECTIONS = 'POST /api/batch-grade/template/correction-candidates'
const never = () => new Promise(() => {})

// The 重新自动裁剪 button (it reads 自动裁剪中... while a run is in flight).
const cutButton = editor => editor.upload.button('自动裁剪')
const rerun = editor => editor.upload.click('重新自动裁剪')
const footerButton = (editor, text) => editor.dialog()
  .find(el => el.tagName === 'el-slot' && el.attributes.name === 'footer').byText('button', text)
// The page images the editor shows.
const shownImages = editor => (editor.dialog()?.findAll(el => el.tagName === 'img' && el.attributes.alt === '教师模板原图') ?? [])
  .map(el => el.attributes.src)
const messagesOf = (editor, type, from = 0) => editor.upload.client.messages.slice(from)
  .filter(message => message.type === type).map(message => message.message)

// The page with no template to resume, then given one as the editor would hold it.
async function emptyEditor(t, options = {}) {
  const editor = await openTemplateEditor(t, { pages: null, ...options })
  editor.setup.pendingTemplateId = 'template-a'
  editor.setup.templateDialogVisible = true
  await editor.settle()
  return editor
}

test('first complete result at run zero loads the real preview once and unlocks the editor', async (t) => {
  const editor = await emptyEditor(t)
  const { setup } = editor
  setup.templateCutBusy = true
  await editor.settle()
  assert.equal(setup.templateCutRunToken, 0, 'no cut run has started on this page')
  assert.equal(cutButton(editor).text, '自动裁剪中...')
  const data = completeCut()
  setup.applyTemplateCutState(data)
  await editor.settle()
  assert.equal(toRaw(setup.templateCutResult), data)
  assert.equal(setup.templateCutPreviewPages.length, 1)
  assert.equal(editor.preview(0).imageUrl, 'blob:template-a-0')
  assert.deepEqual(shownImages(editor), ['blob:template-a-0'])
  assert.equal(setup.templateCutBusy, false)
  assert.equal(cutButton(editor).text, '重新自动裁剪')
  setup.applyTemplateCutState(completeCut())
  setup.applyTemplateCutState({ status: 'running', progress: { completed: 0 } })
  await editor.settle()
  assert.equal(toRaw(setup.templateCutResult), data, 'late nonterminal state cannot regress a finished run')
  assert.equal(editor.imageRequests().length, 1)
  assert.deepEqual(shownImages(editor), ['blob:template-a-0'])
})

test('recognition results get distinct runs and a late initial state cannot attach to another template', async (t) => {
  const old = deferred()
  const current = deferred()
  const editor = await openTemplateEditor(t, {
    pages: null,
    templateIds: ['old', 'new'],
    cut: id => (id === 'old' ? old.promise : current.promise),
  })
  const { setup } = editor
  // Two templates recognised back to back; each asks for its existing cut.
  const first = setup.applyTemplateRecognitionResult('old', [], 'blank', 'single')
  const second = setup.applyTemplateRecognitionResult('new', [], 'marked', 'single')
  assert.equal(editor.cutStateRequests('old').length, 1)
  assert.equal(editor.cutStateRequests('new').length, 1)
  old.resolve(completeCut())
  await first
  await editor.settle()
  assert.equal(setup.templateCutResult, null)
  assert.equal(editor.imageRequests().length + editor.cutRuns('old').length + editor.cutRuns('new').length, 0)
  current.resolve(completeCut())
  await second
  await editor.settle()
  assert.equal(setup.pendingTemplateId, 'new')
  assert.equal(editor.preview(0).imageUrl, 'blob:new-0')
  assert.deepEqual(shownImages(editor), ['blob:new-0'])
  const corrections = editor.upload.requests(CORRECTIONS)
  assert.equal(corrections.length, 1)
  assert.equal(JSON.parse(corrections[0].data).template_id, 'new')
})

test('cancelled recognition-state lookup cannot restart a cut request or restore a discarded template', async (t) => {
  const state = deferred()
  // The teacher comes back to the unconfirmed template; its cut state is still on its way.
  const editor = await openTemplateEditor(t, { pages: RECOGNISED, cut: () => state.promise })
  assert.equal(editor.dialog().attributes['data-open'], 'true')
  assert.equal(editor.setup.pendingTemplateId, 'template-a')
  assert.equal(editor.cutStateRequests().length, 1)
  await editor.click(footerButton(editor, '取消'))
  assert.ok(messagesOf(editor, 'info').includes('已取消本次模板识别，当前模板已清空'))
  state.resolve({ status: 'not_run' })
  await editor.settle()
  assert.equal(editor.setup.pendingTemplateId, '')
  assert.equal(editor.dialog().attributes['data-open'], 'false')
  assert.equal(editor.cutRuns().length, 0)
})

test('an older preview failure cannot clear a newer run or cancel its drawing request', async (t) => {
  const old = deferred()
  let reads = 0
  const editor = await openTemplateEditor(t, {
    pages: RECOGNISED,
    timers: true,
    image: () => (++reads === 1 ? old.promise : 'blob:new'),
    receipt: never,
  })
  // The resumed template's page image is still loading when the teacher re-runs the cut.
  assert.equal(editor.setup.templateCutPreviewPages.length, 0)
  await rerun(editor)
  await editor.upload.tick(300) // the new run's poll finds it complete
  assert.equal(editor.preview(0).imageUrl, 'blob:new')
  old.reject(new Error('old image failed'))
  await editor.settle()
  assert.equal(editor.preview(0).imageUrl, 'blob:new')
  assert.deepEqual(shownImages(editor), ['blob:new'])
  assert.equal(editor.errorText(), '')
  assert.deepEqual(editor.urls.revoked, [])
  assert.equal(editor.cutRuns()[0].init.signal.aborted, false, 'the newer run keeps its request')
})

test('overlapping preview reloads within the same run keep the newest images', async (t) => {
  const old = deferred()
  let reads = 0
  const editor = await emptyEditor(t, { image: () => (++reads === 1 ? old.promise : 'blob:new') })
  const { setup } = editor
  const previous = setup.loadTemplateCutPreviews(completeCut())
  await setup.loadTemplateCutPreviews(completeCut())
  old.resolve('blob:old')
  await previous
  await editor.settle()
  assert.equal(editor.preview(0).imageUrl, 'blob:new')
  assert.deepEqual(shownImages(editor), ['blob:new'])
  assert.deepEqual(editor.urls.revoked, ['blob:old'])
})

test('one failed page releases both already-loaded and later-successful images and allows retry', async (t) => {
  const images = [deferred(), deferred(), deferred()]
  const editor = await emptyEditor(t, { image: (_id, index) => images[index].promise })
  const { setup } = editor
  const loading = setup.loadTemplateCutPreviews(completeCut(3))
  images[0].resolve('blob:first')
  await editor.settle()
  images[1].reject(new Error('broken image'))
  await loading
  images[2].resolve('blob:last')
  await editor.settle()
  assert.deepEqual([...editor.urls.revoked].sort(), ['blob:first', 'blob:last'])
  assert.equal(setup.templateCutPreviewPages.length, 0)
  assert.deepEqual(shownImages(editor), [])
  assert.match(editor.errorText(), /读取失败/)
  editor.control.image = async () => 'blob:retry'
  await setup.loadTemplateCutState('template-a')
  await editor.settle()
  assert.equal(editor.errorText(), '')
  assert.equal(editor.preview(0).imageUrl, 'blob:retry')
  assert.deepEqual(shownImages(editor), ['blob:retry'])
})

test('cancelled preview reads revoke their own late blob and cannot repopulate the editor', async (t) => {
  const image = deferred()
  // The resumed template's page image is still loading when the teacher cancels the draft.
  const editor = await openTemplateEditor(t, { pages: RECOGNISED, image: () => image.promise })
  assert.equal(editor.imageRequests().length, 1)
  await editor.click(footerButton(editor, '取消'))
  image.resolve('blob:cancelled')
  await editor.settle()
  assert.equal(editor.setup.templateCutPreviewPages.length, 0)
  assert.equal(editor.setup.templateCutPreviewError, '')
  assert.equal(editor.dialog().attributes['data-open'], 'false')
  assert.deepEqual(editor.urls.revoked, ['blob:cancelled'])
})

test('existing-template and polling state responses cannot cross a template or run boundary', async (t) => {
  for (const method of ['loadTemplateCutState', 'pollTemplateCutState']) {
    for (const change of ['template', 'run']) {
      const state = deferred()
      const firstRun = deferred()
      const editor = await emptyEditor(t, { timers: true, cut: () => state.promise, receipt: () => firstRun.promise })
      const { setup } = editor
      let pending = Promise.resolve()
      if (method === 'loadTemplateCutState') {
        pending = setup.loadTemplateCutState('template-a')
      } else {
        // A cut run polls its state.
        await rerun(editor)
        await editor.upload.tick(300)
      }
      assert.equal(editor.cutStateRequests().length, 1, `${method}/${change}`)
      if (change === 'template') {
        setup.pendingTemplateId = 'template-b'
      } else {
        if (method === 'pollTemplateCutState') {
          // That run's request fails, so the teacher can start another.
          firstRun.reject(new Error('cut request failed'))
          await editor.settle()
          assert.equal(cutButton(editor).text, '重新自动裁剪')
        }
        editor.control.receipt = never
        await rerun(editor)
      }
      state.resolve(completeCut())
      await pending
      await editor.settle()
      assert.equal(setup.templateCutResult, null, `${method}/${change}`)
      assert.equal(editor.imageRequests().length, 0)
      assert.deepEqual(shownImages(editor), [])
      editor.close()
    }
  }
})

test('poll completion unlocks before the POST settles; a late error cannot erase the success', async (t) => {
  const post = deferred()
  const editor = await openTemplateEditor(t, { pages: RECOGNISED, timers: true, receipt: () => post.promise })
  const { setup } = editor
  await rerun(editor)
  assert.equal(setup.templateCutBusy, true)
  assert.equal(cutButton(editor).text, '自动裁剪中...')
  await editor.upload.tick(300) // the run's poll finds it complete
  assert.equal(setup.templateCutBusy, false)
  assert.equal(cutButton(editor).text, '重新自动裁剪')
  const preview = editor.preview(0)
  // The teacher already adjusts the fresh preview.
  await editor.drag(editor.anchors()[0], [40, 50], [80, 90])
  assert.equal(preview.dirty, true)
  const from = editor.upload.client.messages.length
  post.reject(new Error('late POST timeout'))
  await editor.settle()
  assert.equal(setup.templateCutBusy, false)
  assert.equal(toRaw(editor.preview(0)), toRaw(preview))
  assert.equal(preview.dirty, true)
  assert.match(editor.article().text, /已调整 · 随模板保存/)
  assert.deepEqual(messagesOf(editor, 'error', from), [])
  // Nothing polls any more.
  const reads = editor.cutStateRequests().length
  await editor.upload.tick(5000)
  assert.equal(editor.cutStateRequests().length, reads)
})

test('a failed run exposes recovery and an older POST cannot unlock or overwrite the retry', async (t) => {
  const first = deferred()
  const second = deferred()
  let runs = 0
  const editor = await openTemplateEditor(t, {
    pages: RECOGNISED,
    timers: true,
    receipt: () => (++runs === 1 ? first.promise : second.promise),
  })
  const { setup } = editor
  const shownBefore = editor.imageRequests().length
  await rerun(editor)
  const oldToken = setup.templateCutRunToken
  // The run's poll reports the failure: the editor unlocks and says why.
  editor.control.cut = () => ({ status: 'failed', message: 'synthetic failed' })
  await editor.upload.tick(300)
  assert.equal(setup.templateCutBusy, false)
  assert.equal(editor.errorText(), 'synthetic failed')
  const from = editor.upload.client.messages.length
  await rerun(editor)
  assert.equal(editor.cutRuns()[0].init.signal.aborted, true)
  assert.equal(JSON.parse(editor.cutRuns()[1].init.body).force, true)
  assert.equal(editor.errorText(), '')
  first.resolve(completeCut())
  await editor.settle()
  assert.equal(setup.templateCutBusy, true)
  assert.equal(cutButton(editor).text, '自动裁剪中...')
  // A late state of the older run cannot unlock the retry or replace what it shows.
  const resultBefore = toRaw(setup.templateCutResult)
  setup.applyTemplateCutState(completeCut(), oldToken)
  await editor.settle()
  assert.equal(setup.templateCutBusy, true)
  assert.equal(toRaw(setup.templateCutResult), resultBefore)
  assert.equal(editor.imageRequests().length, shownBefore)
  second.resolve(completeCut())
  await editor.settle()
  assert.equal(setup.templateCutBusy, false)
  assert.equal(setup.templateCutPreviewPages.length, 1)
  assert.equal(editor.imageRequests().length, shownBefore + 1)
  assert.equal(messagesOf(editor, 'success', from).length, 1)
  assert.match(messagesOf(editor, 'success', from)[0], /模板自动裁剪完成/)
})

test('malformed or mismatched POST receipts do not overwrite an existing result or report success', async (t) => {
  for (const response of [{}, { status: 'running' }, completeCut(1, { action_id: 'different-run' })]) {
    const editor = await openTemplateEditor(t, { pages: RECOGNISED, timers: true, receipt: () => response })
    const { setup } = editor
    const previous = toRaw(setup.templateCutResult)
    assert.equal(previous.status, 'complete')
    const from = editor.upload.client.messages.length
    await rerun(editor)
    assert.equal(toRaw(setup.templateCutResult), previous)
    assert.equal(setup.templateCutBusy, false)
    assert.equal(cutButton(editor).text, '重新自动裁剪')
    assert.deepEqual(messagesOf(editor, 'error', from), ['模板裁剪结果不完整或不属于本次任务，请重试。'])
    assert.equal(messagesOf(editor, 'success', from).length, 0)
    editor.close()
  }
})

test('failed POST receipt displays the backend recovery message without a success notification', async (t) => {
  // A template that was never cut starts its cut by itself when it opens.
  const editor = await openTemplateEditor(t, {
    pages: RECOGNISED,
    timers: true,
    cut: () => ({ status: 'not_run' }),
    receipt: () => ({ status: 'failed', error: { message: 'recoverable synthetic failure' } }),
  })
  await editor.settle()
  const { setup } = editor
  assert.equal(editor.cutRuns().length, 1)
  assert.equal(setup.templateCutResult.status, 'failed')
  assert.equal(setup.templateCutPreviewError, 'recoverable synthetic failure')
  assert.equal(editor.errorText(), 'recoverable synthetic failure')
  assert.equal(setup.templateCutBusy, false)
  assert.equal(cutButton(editor).text, '重新自动裁剪')
  // The only success notice is the template coming back, never the cut.
  assert.deepEqual(messagesOf(editor, 'success'), ['已恢复服务器完成的模板识别结果'])
})
