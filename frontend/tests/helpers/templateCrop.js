// The template editor (编辑本次模板) of the real 批改 page, opened the way a
// teacher reaches it after reloading the page with an unfinished template: the
// recognition checkpoint in localStorage, the server reporting the template
// OCR as done (GET .../recognition) and its automatic crop (GET
// .../cut-candidates) as complete. Every page image comes from the
// authenticated image route; saving goes to the real layout-overrides and
// confirm routes, answered like a server that accepts the request.
//
//   import { cutPage, openTemplateEditor, question } from './helpers/templateCrop.js'
//   const editor = await openTemplateEditor(t, {
//     questions: [[question('1')]],                      // per page, what OCR recognized
//     cut: { pages: [cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] })] },
//   })
//   await editor.drag(editor.card().byClass('tpl-column-divider'), [500, 0], [550, 0])
//   await editor.save()                                 // 保存模板（全部页）
//   editor.layoutSaves()[0]                             // the PUT layout-overrides body
//
// Options (the rest goes to helpers/uploadPage.js's mountUpload: responses,
// streams, timers, messageBox ...):
//   questions    per page, the questions template OCR returned
//   recognition  more fields of the recognition state (template_mode, ...)
//   checkpoint   more fields of the stored checkpoint (category, ...)
//   cut          the crop state GET cut-candidates answers ({ status:
//                'complete', template_id } are added; by default page 1 with
//                the default question's number box), or (request) =>
//                Response for a crop that is not there yet
//   resume       false: mount without the checkpoint (a fresh page)
//
// The editor's page stage is laid out as a browser would at 1000 x 1000 px
// from the viewport origin, so pointer coordinates equal the page's own
// 0..1000 coordinates; Element.closest, which the gestures call, is provided
// for the in-memory tree.
//
// Returns the mountUpload result plus:
//   dialog()            the 编辑本次模板 <el-dialog>
//   card(pageIndex)     the page's preview card (#template-reference-page-N)
//   preview(pageIndex)  the page's editor state (templateCutPreviewPages)
//   alert()             the 模板定位仍需教师复核 alert text, or ''
//   layoutSaves(), questionEdits(), confirms()   request bodies, in order
//   warnings()          ElMessage warnings shown
//   prompt(...values)   answers for the next ElMessageBox.prompt calls
//   choose(select, v)   pick an option of a <select> with v-model and @change
//   drag(el, from, to)  press on el at from, move and release at to
//   draw(tool, number, box)  pick a 新增标记 tool, give its question number,
//                       then draw box [x1, y1, x2, y2] on the page
//   save()              click 保存模板（全部页） and settle
import { MElement } from './memoryDom.js'
import { jsonResponse } from './memoryFetch.js'
import { mountUpload } from './uploadPage.js'

export const TEMPLATE_ID = 'tpl-1'
export const CHECKPOINT_KEY = 'mathagent_template_recognition:7'
const TEMPLATE_API = `/api/batch-grade/template/${TEMPLATE_ID}`
export const ROUTES = Object.freeze({
  recognition: `GET ${TEMPLATE_API}/recognition`,
  cutState: `GET ${TEMPLATE_API}/cut-candidates`,
  cutRun: `POST ${TEMPLATE_API}/cut-candidates`,
  layout: `PUT ${TEMPLATE_API}/layout-overrides`,
  questions: `PUT ${TEMPLATE_API}/questions`,
  confirm: 'POST /api/batch-grade/template/confirm',
  bankImport: `POST ${TEMPLATE_API}/bank-import`,
  correction: 'POST /api/batch-grade/template/correction-candidates',
  recognize: 'POST /api/batch-grade/template/recognize',
  image: pageIndex => `GET ${TEMPLATE_API}/pages/${pageIndex}/image`,
})

// A recognized template question, complete enough to be saved as it is.
export function question(number, fields = {}) {
  return { question_no: String(number), question_type: 'fill', question_text: `第 ${number} 题题干`, standard_answer: `答案${number}`, ...fields }
}

export function anchor(number, bbox, fields = {}) {
  return { question_number: String(number), question_number_bbox: bbox, ...fields }
}

export function regions(number, boxes, fields = {}) {
  return { question_number: String(number), regions: boxes, ...fields }
}

export const TWO_COLUMNS = Object.freeze([{ x1_1000: 0, x2_1000: 500 }, { x1_1000: 500, x2_1000: 1000 }])

// One page of the crop result: a confirmed two-column crop plan unless the
// fields say otherwise.
export function cutPage(pageIndex, fields = {}) {
  return {
    page_index: pageIndex,
    page_label: `page-${pageIndex}`,
    anchor_crop_plan: { parameters: { columns: TWO_COLUMNS.map(column => ({ ...column })), column_count: 2 }, blocks: [] },
    modes: { question: {} },
    ...fields,
  }
}

export const parsed = config => (typeof config.data === 'string' ? JSON.parse(config.data) : config.data)

// What a server that accepts a page save answers: the saved geometry.
export function acceptedLayout(config) {
  const body = parsed(config)
  return {
    status: 200,
    data: {
      success: true,
      template_id: TEMPLATE_ID,
      page_index: body.page_index,
      columns: body.columns,
      question_anchors: body.question_anchors,
      question_schema: body.question_schema,
      template_answer_regions: body.template_answer_regions,
      template_handwriting_regions: body.template_handwriting_regions,
    },
  }
}

function installClosest(t) {
  if (MElement.prototype.closest) return
  // Element.closest for the class selectors the page's gestures use.
  MElement.prototype.closest = function closest(selector) {
    const name = String(selector).replace(/^\./, '')
    for (let node = this; node; node = node.parentNode) {
      if (node instanceof MElement && node.classList.contains(name)) return node
    }
    return null
  }
  t.after(() => { delete MElement.prototype.closest })
}

function layOut(root) {
  for (const stage of root.allByClass('tpl-cut-preview-stage')) {
    for (const [name, value] of Object.entries({ clientWidth: 1000, clientHeight: 1000, offsetWidth: 1000, offsetHeight: 1000 })) {
      Object.defineProperty(stage, name, { value, configurable: true })
    }
    stage.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 1000, width: 1000, height: 1000 })
  }
}

export async function openTemplateEditor(t, {
  questions = [[question('1')]],
  recognition = {},
  checkpoint = {},
  cut = { pages: [cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] })] },
  resume = true,
  responses = {},
  streams = {},
  localStorage = {},
  messageBox,
  ...options
} = {}) {
  installClosest(t)
  const pages = questions.map((items, pageIndex) => ({ page_index: pageIndex, page_label: `page-${pageIndex}`, questions: items }))
  const cutState = typeof cut === 'function' ? cut : () => jsonResponse({ status: 'complete', template_id: TEMPLATE_ID, ...cut })
  const imageRoutes = Object.fromEntries(Array.from({ length: 8 }, (_, pageIndex) => [
    ROUTES.image(pageIndex), () => ({ status: 200, data: new Blob([`page-${pageIndex}`], { type: 'image/png' }) }),
  ]))
  const promptAnswers = []
  const upload = await mountUpload(t, {
    localStorage: {
      ...(resume ? {
        [CHECKPOINT_KEY]: JSON.stringify({ template_id: TEMPLATE_ID, category: 'daily', exam_name: '', template_mode: 'marked', column_mode: 'double', ...checkpoint }),
      } : {}),
      ...localStorage,
    },
    streams: {
      [ROUTES.recognition]: () => jsonResponse({ status: 'ocr_done', template_id: TEMPLATE_ID, category: checkpoint.category ?? 'daily', pages, ...recognition }),
      [ROUTES.cutState]: cutState,
      ...streams,
    },
    responses: {
      ...imageRoutes,
      [ROUTES.correction]: { candidates: [] },
      [ROUTES.layout]: acceptedLayout,
      [ROUTES.confirm]: config => ({ status: 200, data: { success: true, page_count: parsed(config).pages.length, template_mode: 'marked', column_mode: 'double' } }),
      ...responses,
    },
    messageBox: messageBox ?? (box => (box.kind === 'prompt' ? { value: promptAnswers.shift() ?? '' } : 'confirm')),
    ...options,
  })
  const { page, settle } = upload
  const root = page.root
  const bodies = route => upload.requests(route).map(parsed)
  const card = (pageIndex = 0) => root.find(el => el.id === `template-reference-page-${pageIndex}`)

  async function drag(el, [x0, y0], [x1, y1]) {
    layOut(root)
    const editor = (() => {
      for (let node = el; node; node = node.parentNode) if (node.classList?.contains('drawing-reference-editor')) return node
      return null
    })()
    if (!editor) throw new Error('drag: the element is not on a template page stage')
    const pointer = { pointerId: 1, button: 0 }
    await el.dispatch('pointerdown', { ...pointer, clientX: x0, clientY: y0 })
    await settle()
    await editor.dispatch('pointermove', { ...pointer, clientX: x1, clientY: y1 })
    await editor.dispatch('pointerup', { ...pointer, clientX: x1, clientY: y1 })
    await settle()
  }

  return {
    ...upload,
    dialog: () => root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板'),
    card,
    preview: (pageIndex = 0) => page.setup.templateCutPreviewPages.find(item => Number(item.pageIndex) === pageIndex),
    alert: () => root.find(el => el.classList.contains('tpl-layout-review-alert') && el.text.startsWith('模板定位仍需教师复核'))?.text ?? '',
    layoutSaves: () => bodies(ROUTES.layout),
    questionEdits: () => bodies(ROUTES.questions),
    confirms: () => bodies(ROUTES.confirm),
    warnings: () => upload.client.messages.filter(message => message.type === 'warning').map(message => message.message),
    prompt: (...values) => { promptAnswers.push(...values.map(String)) },
    layOut: () => layOut(root),
    // Picks an option of a <select> bound with both v-model and @change in the
    // order a browser runs them: Vue attaches v-model's change listener (the
    // directive's created hook) before the element's own @change listener, so
    // the handler already sees the new value. (helpers/memoryDom.js runs an
    // element's on* handlers before its listeners.)
    async choose(el, value) {
      if (el.disabled === true || el.hasAttribute('disabled')) throw new Error('the select is disabled')
      for (const option of el.options) option.selected = String(option._value ?? option.value) === String(value)
      const event = { type: 'change', target: el, currentTarget: el, preventDefault() {}, stopPropagation() {} }
      for (const fn of el.listeners.get('change') ?? []) fn(event)
      await el.handlers.change?.(event)
      await settle()
    },
    drag,
    async draw(tool, number, [x1, y1, x2, y2], pageIndex = 0) {
      promptAnswers.push(String(number))
      const button = card(pageIndex).find(el => el.tagName === 'button' && el.classList.contains('tpl-tool-btn') && el.text === tool)
      if (!button) throw new Error(`no 新增标记 tool "${tool}"`)
      await button.dispatch('click')
      await settle()
      const surface = card(pageIndex).byClass('tpl-draw-surface')
      if (!surface) throw new Error(`the ${tool} tool did not open the drawing surface`)
      await drag(surface, [x1, y1], [x2, y2])
    },
    async save() {
      await upload.click('保存模板（全部页）')
      await settle()
    },
  }
}
