import assert from 'node:assert/strict'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'
import { anchor, cutPage, openTemplateEditor, question, regions, ROUTES, TWO_COLUMNS } from './helpers/templateCrop.js'

// After template recognition the real 批改 page (Upload.vue) crops the
// template by itself and shows every page with its columns, question-number
// boxes and answer boxes over the authenticated page image.

const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
const crop = (pages, fields = {}) => ({ status: 'complete', template_id: 'tpl-1', pages, ...fields })

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

const boxStyles = (card, className) => card.allByClass(className).map(el => [el.style.left, el.style.top, el.style.width, el.style.height])
const columnOverlays = card => card.allByClass('tpl-column-overlay').map(el => [el.text, el.style.left, el.style.width])
const columnSelect = card => card.find(el => el.tagName === 'select' && el.parentNode?.classList.contains('tpl-column-count'))
const selectedText = select => select.options.find(option => option.selected)?.text

test('模板识别完成后自动裁剪并显示受鉴权模板覆盖框', async (t) => {
  const created = []
  t.mock.method(URL, 'createObjectURL', (blob) => {
    created.push(blob)
    return `blob:template-page-${created.length - 1}`
  })
  t.mock.method(URL, 'revokeObjectURL', () => {})
  const pages = [
    // Under geometry review: the approved fields are empty, the candidates seed the editor.
    cutPage(0, {
      geometry_review_pending: true,
      question_anchors: [],
      question_anchor_candidates: [anchor('1', [10, 10, 40, 30])],
      template_answer_region_candidates: [regions('1', [[100, 100, 300, 200]])],
      template_handwriting_region_candidates: [regions('1', [[100, 250, 300, 300]])],
      anchor_crop_plan: { parameters: { columns: TWO_COLUMNS, column_count: 2 }, blocks: [{}, {}] },
    }),
    // An older page artifact, with the older field names for its boxes.
    cutPage(1, {
      artifact: {
        question_anchors: [{ question_number: '2', bbox: [10, 100, 40, 130] }],
        template_answer_regions: [{ question_number: '2', answer_bbox: [100, 100, 300, 200] }],
        template_handwriting_regions: [{ question_number: '2', student_answer_bbox: [100, 250, 300, 300] }],
      },
      anchor_crop_plan: { parameters: { columns: TWO_COLUMNS, column_count: 2 }, blocks: [{}] },
    }),
    cutPage(2, {
      question_anchors: [{ question_number: '3', question_bbox: [10, 200, 40, 230] }],
      template_answer_regions: [{ question_number: '3', answer_regions: [[100, 400, 300, 500]] }],
    }),
    // Under review, but the candidates hold nothing usable: the approved geometry stays.
    cutPage(3, {
      geometry_review_pending: true,
      question_anchors: [anchor('4', [10, 300, 40, 330])],
      question_anchor_candidates: [],
      template_answer_regions: [regions('4', [[100, 600, 300, 700]])],
      template_answer_region_candidates: [regions('4', [])],
    }),
  ]
  const editor = await openTemplateEditor(t, {
    timers: true,
    questions: [[question('1')], [question('2')], [question('3')], [question('4')]],
    // Nothing cropped yet when the editor opens.
    cut: () => json({ status: 'not_run' }),
    streams: { [ROUTES.cutRun]: () => json(crop(pages)) },
  })

  // The crop starts by itself.
  const [run] = editor.fetches(ROUTES.cutRun)
  const body = JSON.parse(run.init.body)
  assert.deepEqual([body.cut_types, body.image_type, body.force], [['question'], 'photo', false])
  assert.ok(body.action_id)
  assert.equal(run.init.headers.Authorization, 'Bearer teacher-token')
  assert.ok(editor.client.messages.some(message => message.message === '模板自动裁剪完成：已生成 3 个连续裁剪块'))

  const dialog = editor.dialog()
  assert.equal(dialog.attributes['data-open'], 'true')
  assert.match(dialog.byClass('tpl-cut-panel').text, /模板分栏与框选定位/)
  assert.equal(dialog.allByClass('tpl-cut-preview-card').length, 4)

  // Each page image comes from the authenticated image route.
  const images = editor.client.requests.filter(config => config.url.includes('/pages/'))
  assert.deepEqual(images.map(config => [config.method, config.url, config.responseType, config.headers.Authorization]), [0, 1, 2, 3].map(index => (
    ['get', `/batch-grade/template/tpl-1/pages/${index}/image`, 'blob', 'Bearer teacher-token']
  )))
  assert.ok(created.every(blob => blob instanceof Blob))
  for (const index of [0, 1, 2, 3]) {
    assert.equal(editor.card(index).find(el => el.tagName === 'img').attributes.src, `blob:template-page-${index}`)
  }

  // Page 1: the crop plan's columns, the candidate boxes (answer and legacy handwriting regions merged).
  assert.deepEqual(columnOverlays(editor.card(0)), [['第1栏', '0%', '50%'], ['第2栏', '50%', '50%']])
  assert.deepEqual(boxStyles(editor.card(0), 'tpl-question-anchor'), [['1%', '1%', '3%', '2%']])
  assert.deepEqual(boxStyles(editor.card(0), 'tpl-handwriting-region'), [['10%', '10%', '20%', '10%'], ['10%', '25%', '20%', '5%']])
  // Page 2: from the artifact, `bbox` / `answer_bbox` / `student_answer_bbox` accepted.
  assert.deepEqual(boxStyles(editor.card(1), 'tpl-question-anchor'), [['1%', '10%', '3%', '3%']])
  assert.deepEqual(boxStyles(editor.card(1), 'tpl-handwriting-region'), [['10%', '10%', '20%', '10%'], ['10%', '25%', '20%', '5%']])
  // Page 3: `question_bbox` and `answer_regions`.
  assert.deepEqual(boxStyles(editor.card(2), 'tpl-question-anchor'), [['1%', '20%', '3%', '3%']])
  assert.deepEqual(boxStyles(editor.card(2), 'tpl-handwriting-region'), [['10%', '40%', '20%', '10%']])
  // Page 4: empty candidates do not replace the approved boxes.
  assert.deepEqual(boxStyles(editor.card(3), 'tpl-question-anchor'), [['1%', '30%', '3%', '3%']])
  assert.deepEqual(boxStyles(editor.card(3), 'tpl-handwriting-region'), [['10%', '60%', '20%', '10%']])
  // The older names become the canonical question_number_bbox the server expects.
  assert.deepEqual(editor.preview(1).questionAnchors[0].question_number_bbox, [10, 100, 40, 130])
  editor.unmount()

  // A template whose crop already finished opens on it without cropping again.
  const reopened = await openTemplateEditor(t, { timers: true, cut: crop([pages[2]]), questions: [[question('3')]] })
  assert.equal(reopened.fetches(ROUTES.cutRun).length, 0)
  assert.equal(reopened.dialog().allByClass('tpl-cut-preview-card').length, 1)
  assert.deepEqual(boxStyles(reopened.card(2), 'tpl-handwriting-region'), [['10%', '40%', '20%', '10%']])
})

test('模板页图以带登录令牌的 blob 请求取回，并返回由该图生成的对象 URL', async (t) => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'tok-page' } })
  const image = new Blob(['page-image'], { type: 'image/png' })
  client.respond(() => ({ data: image, headers: { 'content-type': 'image/png' } }))
  const createObjectURL = t.mock.method(URL, 'createObjectURL', () => 'blob:template-page-3')

  const objectUrl = await client.api.grade.templatePageImage('tpl 1/2', 3)

  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'get')
  assert.equal(request.url, '/batch-grade/template/tpl%201%2F2/pages/3/image')
  assert.equal(request.responseType, 'blob')
  assert.equal(request.headers.Authorization, 'Bearer tok-page')
  assert.equal(createObjectURL.mock.callCount(), 1)
  assert.equal(createObjectURL.mock.calls[0].arguments[0], image)
  assert.equal(objectUrl, 'blob:template-page-3')
})

test('手动按钮只用于重新自动裁剪，不是首次裁剪门禁', async (t) => {
  const runs = []
  const page = cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] })
  const editor = await openTemplateEditor(t, {
    timers: true,
    cut: () => json({ status: 'not_run' }),
    streams: {
      [ROUTES.cutRun]: (request) => {
        const run = { request, ...deferred() }
        runs.push(run)
        // The first, automatic crop answers at once; the rerun waits for the test.
        return runs.length === 1 ? json(crop([page])) : run.promise
      },
    },
  })
  // The first crop needed no click.
  assert.equal(runs.length, 1)
  assert.equal(JSON.parse(runs[0].request.init.body).force, false)
  assert.equal(editor.page.root.allByClass('tpl-cut-preview-card').length, 1)
  assert.equal(editor.button('生成题块候选') === null, true, 'no 生成题块候选 button')
  assert.doesNotMatch(editor.text(), /生成题块候选/)

  const rerun = editor.button('重新自动裁剪')
  assert.equal(rerun.hasAttribute('disabled'), false)
  await editor.click('重新自动裁剪')
  assert.equal(runs.length, 2)
  const body = JSON.parse(runs[1].request.init.body)
  assert.equal(body.force, true, 'the button forces a fresh crop')
  assert.notEqual(body.action_id, JSON.parse(runs[0].request.init.body).action_id)
  const busy = editor.button('自动裁剪中...')
  assert.ok(busy.hasAttribute('disabled'))
  assert.match(editor.dialog().byClass('tpl-cut-status').text, /进度 0\/1/)

  runs[1].resolve(json(crop([page])))
  await editor.settle()
  assert.equal(editor.button('重新自动裁剪').hasAttribute('disabled'), false)
})

test('轮询先看到完成时只渲染一次并立即解锁', async (t) => {
  // The POST may stay open after the server already finished; polling observes
  // that completion, so it must render once and release the editor lock instead
  // of rebuilding every preview on each tick while the POST hangs.
  const page = cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] })
  const states = [json({ status: 'not_run' }), json({ status: 'running', progress: { completed: 0, total: 1, remaining: 1 } })]
  const post = deferred()
  const editor = await openTemplateEditor(t, {
    timers: true,
    cut: () => states.shift() ?? json(crop([page], { progress: { completed: 1, total: 1, remaining: 0 } })),
    streams: { [ROUTES.cutRun]: () => post.promise },
  })
  const polls = () => editor.fetches(ROUTES.cutState).length
  const imageRequests = () => editor.client.requests.filter(config => config.url.includes('/pages/')).length
  assert.ok(editor.button('自动裁剪中...').hasAttribute('disabled'))

  await editor.tick(300)
  assert.equal(polls(), 2)
  assert.ok(editor.button('自动裁剪中...'), 'still cropping')
  await editor.tick(800)
  assert.equal(polls(), 3)
  // Completion seen by the poll: the editor is unlocked and the pages render once.
  assert.equal(editor.button('重新自动裁剪').hasAttribute('disabled'), false)
  assert.equal(editor.page.root.allByClass('tpl-cut-preview-card').length, 1)
  assert.equal(imageRequests(), 1)
  const rendered = editor.preview()

  await editor.tick(5000)
  assert.equal(polls(), 3, 'polling stops once the crop is finished')
  post.resolve(json(crop([page])))
  await editor.settle()
  assert.equal(imageRequests(), 1, 'the late POST answer does not rebuild the previews')
  assert.ok(editor.preview() === rendered, 'the rendered preview is kept')
  assert.equal(editor.button('重新自动裁剪').hasAttribute('disabled'), false)
})

test('轮询先看到失败时同样立即解锁、说明原因并停止轮询', async (t) => {
  // A failed crop is finished too: the hanging POST must not keep the editor
  // locked or the polling running after the server reported the failure.
  const states = [json({ status: 'not_run' }), json({ status: 'running', progress: { completed: 0, total: 1, remaining: 1 } })]
  const post = deferred()
  const editor = await openTemplateEditor(t, {
    timers: true,
    cut: () => states.shift() ?? json({ status: 'failed', template_id: 'tpl-1', message: '模型裁剪失败' }),
    streams: { [ROUTES.cutRun]: () => post.promise },
  })
  const polls = () => editor.fetches(ROUTES.cutState).length
  assert.ok(editor.button('自动裁剪中...').hasAttribute('disabled'))

  await editor.tick(300)
  assert.equal(polls(), 2)
  assert.ok(editor.button('自动裁剪中...'), 'still cropping')
  await editor.tick(800)
  assert.equal(polls(), 3)
  // The failure seen by the poll: the teacher can rerun the crop at once and reads why it failed.
  const rerun = editor.button('重新自动裁剪')
  assert.ok(rerun, 'the crop is no longer running')
  assert.equal(rerun.hasAttribute('disabled'), false)
  assert.ok(editor.dialog().text.includes('模型裁剪失败'), editor.dialog().byClass('tpl-cut-panel').text)
  assert.equal(editor.page.root.allByClass('tpl-cut-preview-card').length, 0, 'a failed crop renders no pages')

  await editor.tick(5000)
  assert.equal(polls(), 3, 'polling stops once the crop has failed')
  post.resolve(json({ status: 'failed', template_id: 'tpl-1', message: '模型裁剪失败' }))
  await editor.settle()
  assert.equal(editor.button('重新自动裁剪').hasAttribute('disabled'), false)
  assert.equal(polls(), 3)
})

test('crop plan 为空时回退读取 modes.question 候选列布局且从不自动确认', async (t) => {
  const candidateLayout = { column_layout: { columns: [{ x1_1000: 0, x2_1000: 480 }, { x1_1000: 480, x2_1000: 1000 }], column_count: 2 } }
  const noPlan = { parameters: { columns: [] }, blocks: [] }
  const editor = await openTemplateEditor(t, {
    questions: [0, 1, 2, 3, 4].map(index => [question(String(index + 1))]),
    cut: crop([
      cutPage(0),
      cutPage(1, { anchor_crop_plan: noPlan, modes: { question: candidateLayout } }),
      cutPage(2, { anchor_crop_plan: noPlan, modes: { question: { column_layout: { columns: [] } } } }),
      // The crop plan of a page the model asks to review, and a candidate on a page the server lists for review.
      cutPage(3, { layout_review_pending: true }),
      cutPage(4, { anchor_crop_plan: noPlan, modes: { question: candidateLayout } }),
    ].map((page, index) => ({ ...page, question_anchors: [anchor(String(index + 1), [10, 10, 40, 30])] })), { layout_review_pending_pages: [4] }),
  })
  const state = index => {
    const page = editor.preview(index)
    return [page.columnsSource, page.columnsConfirmed, page.layoutReviewPending, page.columnCount]
  }

  assert.deepEqual(columnOverlays(editor.card(0)), [['第1栏', '0%', '50%'], ['第2栏', '50%', '50%']])
  assert.deepEqual(state(0), ['model', true, false, 2])
  assert.deepEqual(columnOverlays(editor.card(1)), [['第1栏', '0%', '48%'], ['第2栏', '48%', '52%']])
  assert.deepEqual(state(1), ['model_candidate', false, true, 2])
  assert.deepEqual(columnOverlays(editor.card(2)), [])
  assert.equal(selectedText(columnSelect(editor.card(2))), '选择栏数')
  assert.deepEqual(state(2), ['teacher_draft', false, true, 0])
  assert.deepEqual(columnOverlays(editor.card(3)).length, 2)
  assert.deepEqual(state(3), ['model', false, true, 2])
  assert.deepEqual(state(4), ['model_candidate', false, true, 2])
  // Only the confirmed plan passes the review gate.
  assert.equal(editor.alert(), '模板定位仍需教师复核：栏边界 第2页、第3页、第4页、第5页')
  assert.equal(editor.page.setup.templateCanConfirm, false)
})

test('候选列布局接入页面对象：dirty 与复核算子随候选开启，默认值不被冒充', async (t) => {
  const noPlan = { parameters: { columns: [] }, blocks: [] }
  const columns = count => Array.from({ length: count }, (_, index) => ({ x1_1000: Math.round(index * 1000 / count), x2_1000: Math.round((index + 1) * 1000 / count) }))
  const editor = await openTemplateEditor(t, {
    questions: [[question('1')], [question('2')], [question('3')], [question('4')]],
    cut: crop([
      cutPage(0),
      cutPage(1, { anchor_crop_plan: noPlan, modes: { question: { column_layout: { columns: columns(3) } } } }),
      cutPage(2, { anchor_crop_plan: noPlan, modes: { question: { column_layout: { columns: columns(2), column_count: 3 } } } }),
      cutPage(3, { anchor_crop_plan: noPlan }),
    ].map((page, index) => ({ ...page, question_anchors: [anchor(String(index + 1), [10, 10, 40, 30])] }))),
  })
  // A confirmed plan is not an unsaved decision.
  assert.doesNotMatch(editor.card(0).text, /已调整|请核对本页定位/)
  assert.equal(editor.preview(0).dirty, false)
  // A candidate layout is: it is shown as adjusted and saved with the template.
  assert.match(editor.card(1).text, /已调整 · 随模板保存/)
  assert.equal(editor.preview(1).dirty, true)
  // Its count is the candidate's own column count, or how many columns it has.
  assert.equal(selectedText(columnSelect(editor.card(1))), '三栏')
  assert.equal(editor.preview(1).columnCount, 3)
  assert.equal(selectedText(columnSelect(editor.card(2))), '三栏')
  assert.equal(editor.preview(2).columnCount, 3)
  assert.deepEqual(editor.page.setup.templateGeometryReview.invalidColumnPages, [2], 'a count that disagrees with the columns is flagged')
  // No columns: not dirty (nothing to save yet), but the page still needs the teacher.
  assert.equal(editor.preview(3).dirty, false)
  assert.equal(editor.preview(3).columnCount, 0)
  assert.match(editor.card(3).text, /请核对本页定位/)

  await editor.save()
  assert.deepEqual(editor.layoutSaves(), [], 'a page without columns stops the save')
  assert.ok(editor.warnings().includes('第 4 页尚未设置栏数，请先选择并核对栏边界'))
})
