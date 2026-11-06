// The real 批改 page (helpers/uploadPage.js) with its 编辑本次模板 editor open
// on a recognised template, and a pointer to drive the editor's canvas.
//
//   import { completeCut, openTemplateEditor } from './helpers/templateGeometryCut.js'
//   const editor = await openTemplateEditor(t, {
//     pages: [{ page_index: 0, questions: [{ question_no: '2.1', ... }] }],
//     cut: () => completeCut(1),               // the template's saved cut state
//   })
//   await editor.tool('蓝色作答框')               // arm a toolbar tool (answers its 题号 prompt)
//   await editor.drag(editor.drawSurface(), [120, 170], [320, 330])
//   editor.preview().answerBoxes                  // the page the editor shows
//
// How the editor opens: the teacher comes back to the page while a recognised
// template is still unconfirmed. Upload finds its checkpoint in localStorage,
// asks the server for the recognition state (`pages`), opens the editor and
// asks for the template's cut state; a complete cut loads every page image
// and shows it. `pages: null` mounts the page with no template to resume.
//
// Options (the rest goes to mountUpload: timers, responses, ...):
//   templateId     'template-a', the template the checkpoint names
//   templateIds    other template ids the page may talk about
//   pages          the recognition state's pages (the template's questions)
//   cut            GET .../cut-candidates: (templateId, request) => the state
//                  (or a promise of it: a deferred().promise holds it back)
//   receipt        POST .../cut-candidates: (templateId, request) => the run's
//                  JSON receipt (or a promise; a rejection is a network failure)
//   image          (templateId, pageIndex) => the object URL name of that page
//                  image (or a promise of it; a rejection is a failed read)
//   prompt         answer to the 题号 prompt of a toolbar tool: a number, or
//                  box => number; default: accept the number it suggests
// editor.control holds cut / receipt / image: replace them to change answers.
//
// Returns { upload, page, setup, ... }:
//   urls           { created, revoked }: object URLs the page made / revoked
//   imageRequests(), cutStateRequests(id), cutRuns(id): requests sent so far
//   preview(i)     the editor's page i (default: the one shown), live
//   question(no)   the template question `no` of page 0 (templatePages), live
//   dialog(), article() the editor dialog and its shown page's <article>
//   stage()        the shown page's canvas, laid out as STAGE
//   toolButton(label) a toolbar button;  tool(label) click it (answering its
//                  题号 prompt), then settle
//   deleteButton() 删除所选;  legend(text) the legend entry including text
//   drawSurface(), anchors(), answerRegions(), drawingRegions(), labels(),
//   dividers(), emptyLayer(): what the canvas shows, in page order
//   down / move / up / cancel(el, [clientX, clientY], extra): one pointer event
//   drag(el, from, to, extra) down, move, up;  tap(el, at) down, up
//   click(el)      click an enabled button, then settle
//   hint()         the pointer hint;  errorText() the cut / preview error
//   settle(), close() (unmount; also done after the test). Closing fails if
//                  Vue caught an error in a handler or render (page.errors)
//
// The canvas is STAGE (1000 x 800 px at 20, 10), so client point [x, y] is
// page point [x - 20, (y - 10) * 1.25] of the 0..1000 template space.
//
// memoryDom has no layout, and the editor's pointer handlers measure their
// canvas: stage() gives the canvas its box.
import { jsonResponse } from './memoryFetch.js'
import { mountUpload, TEACHER } from './uploadPage.js'

export const STAGE = Object.freeze({ left: 20, top: 10, width: 1000, height: 800 })

export function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

// A finished cut of `count` pages, each with one Q anchor and one column.
export function completeCut(count = 1, extras = {}) {
  return {
    status: 'complete',
    image_type: 'scan',
    progress: { completed: count, total: count, remaining: 0 },
    pages: Array.from({ length: count }, (_, index) => ({
      page_index: index,
      page_label: `page-${index}`,
      question_anchors: [{ question_number: `${index + 1}`, question_number_bbox: [20, 30, 40, 50] }],
      anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }] } },
    })),
    ...extras,
  }
}

// URL.createObjectURL / revokeObjectURL for the page images: each image blob
// becomes the URL its responder named; every revocation is recorded.
function installObjectUrls() {
  const names = new WeakMap()
  const created = []
  const revoked = []
  const previous = ['createObjectURL', 'revokeObjectURL']
    .map(name => [name, Object.getOwnPropertyDescriptor(URL, name)])
  let counter = 0
  URL.createObjectURL = (blob) => {
    const url = names.get(blob) ?? `blob:memory-${++counter}`
    created.push(url)
    return url
  }
  URL.revokeObjectURL = (url) => { revoked.push(url) }
  let restored = false
  return {
    created,
    revoked,
    blob(url) {
      const blob = new Blob(['image'], { type: 'image/png' })
      names.set(blob, url)
      return blob
    },
    restore() {
      if (restored) return
      restored = true
      for (const [name, descriptor] of previous) Object.defineProperty(URL, name, descriptor)
    },
  }
}

const isShown = el => el.style.display !== 'none'
const isDisabled = el => el.disabled === true || el.hasAttribute('disabled')
const templatePath = id => `/api/batch-grade/template/${encodeURIComponent(id)}`

export async function openTemplateEditor(t, {
  templateId = 'template-a',
  templateIds = [],
  pages = [{ page_index: 0, questions: [] }],
  cut = () => completeCut(),
  receipt = () => completeCut(),
  image = (id, index) => `blob:${id}-${index}`,
  prompt,
  responses = {},
  streams = {},
  localStorage = {},
  ...options
} = {}) {
  const urls = installObjectUrls()
  const control = { cut, receipt, image }
  const answerPrompt = (box) => {
    const value = typeof prompt === 'function' ? prompt(box) : prompt ?? box.options?.inputValue
    return { value: String(value ?? '') }
  }
  const checkpoint = pages
    ? {
        [`mathagent_template_recognition:${TEACHER.id}`]: JSON.stringify({
          template_id: templateId, category: 'daily', exam_name: '', template_mode: 'marked', column_mode: 'double',
        }),
      }
    : {}
  const imageOf = config => /^\/batch-grade\/template\/([^/]+)\/pages\/([^/]+)\/image$/.exec(config.url)
  const ids = [...new Set([templateId, ...templateIds])]
  const imageRoutes = {}
  const cutRoutes = {}
  for (const id of ids) {
    for (let index = 0; index < 8; index++) {
      imageRoutes[`GET ${templatePath(id)}/pages/${index}/image`] = async () => {
        try {
          return { status: 200, data: urls.blob(await control.image(id, index)) }
        } catch (error) {
          return { status: 500, data: { detail: error?.message || '图片读取失败' } }
        }
      }
    }
    cutRoutes[`GET ${templatePath(id)}/cut-candidates`] = async request => jsonResponse(await control.cut(id, request))
    cutRoutes[`POST ${templatePath(id)}/cut-candidates`] = async request => jsonResponse(await control.receipt(id, request))
  }
  let upload
  try {
    upload = await mountUpload(t, {
      localStorage: { ...checkpoint, ...localStorage },
      messageBox: box => (box.kind === 'prompt' ? answerPrompt(box) : 'confirm'),
      ...options,
      responses: { ...imageRoutes, ...responses },
      streams: {
        [`GET ${templatePath(templateId)}/recognition`]: () => jsonResponse({
          status: 'ocr_done', template_id: templateId, category: 'daily', pages: pages ?? [],
        }),
        ...cutRoutes,
        ...streams,
      },
    })
  } catch (error) {
    urls.restore()
    throw error
  }
  // A handler or render failure Vue caught fails the test when the page closes.
  let closed = false
  const close = () => {
    if (closed) return
    closed = true
    upload.unmount()
    urls.restore()
    const [failure] = upload.page.errors
    if (failure) throw Object.assign(new Error(`the page failed in ${failure.info}: ${failure.err?.message ?? failure.err}`), { cause: failure.err })
  }
  t.after(close)
  const { page } = upload
  const setup = page.setup
  const settle = () => upload.settle()

  const article = () => page.root.findAll(el => el.tagName === 'article'
    && el.classList.contains('tpl-cut-preview-card') && isShown(el))[0] ?? null
  const stage = () => {
    const shown = article()?.findAll(el => el.classList.contains('tpl-cut-preview-stage') && isShown(el))[0]
    if (!shown) throw new Error('the template editor shows no page canvas')
    const { left, top, width, height } = STAGE
    shown.getBoundingClientRect = () => ({ left, top, width, height, x: left, y: top, right: left + width, bottom: top + height })
    Object.defineProperty(shown, 'clientWidth', { value: width, configurable: true })
    Object.defineProperty(shown, 'clientHeight', { value: height, configurable: true })
    return shown
  }
  const within = className => stage().findAll(el => el.classList.contains(className))
  const pointer = type => async (el, [clientX, clientY] = [0, 0], extra = {}) => {
    stage()
    await el.dispatch(type, { button: 0, pointerId: 7, pointerType: 'mouse', clientX, clientY, ...extra })
  }
  const down = pointer('pointerdown')
  const move = pointer('pointermove')
  const up = pointer('pointerup')
  const cancel = pointer('pointercancel')

  const editor = {
    upload,
    page,
    setup,
    urls,
    control,
    settle,
    close,
    imageRequests: () => upload.client.requests.filter(config => imageOf(config)),
    cutStateRequests: (id = templateId) => upload.fetches(`GET ${templatePath(id)}/cut-candidates`),
    cutRuns: (id = templateId) => upload.fetches(`POST ${templatePath(id)}/cut-candidates`),
    preview: index => setup.templateCutPreviewPages
      .find(item => Number(item.pageIndex) === (index ?? setup.templatePreviewPageIndex())),
    question: (number, pageIndex = 0) => setup.templatePages
      .find(item => Number(item.page_index) === pageIndex)?.questions
      .find(item => String(item.question_no) === String(number)),
    dialog: () => page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板'),
    article,
    stage,
    drawSurface: () => within('tpl-draw-surface')[0] ?? null,
    anchors: () => within('tpl-question-anchor'),
    answerRegions: () => within('tpl-handwriting-region'),
    drawingRegions: () => within('tpl-drawing-region'),
    labels: () => within('tpl-drawing-label'),
    dividers: () => within('tpl-column-divider'),
    emptyLayer: () => within('tpl-reference-geometry')[0] ?? null,
    toolButton: label => article()?.byClass('tpl-add-tools')
      ?.find(el => el.tagName === 'button' && el.text === label) ?? null,
    deleteButton: () => article()?.byClass('tpl-delete-selected') ?? null,
    legend: text => article()?.byClass('template-geometry-legend')
      ?.find(el => el.tagName === 'button' && el.text.includes(text)) ?? null,
    hint: () => article()?.byClass('tpl-pointer-hint')?.text ?? '',
    errorText: () => page.root.byClass('tpl-cut-preview-error')?.text ?? '',
    async click(el) {
      if (!el) throw new Error('nothing to click')
      if (isDisabled(el)) throw new Error(`"${el.text}" is disabled`)
      await el.dispatch('click')
      await settle()
    },
    async tool(label) {
      const button = editor.toolButton(label)
      if (!button) throw new Error(`no toolbar tool "${label}"`)
      await editor.click(button)
    },
    down,
    move,
    up,
    cancel,
    async drag(el, from, to, extra = {}) {
      await down(el, from, extra)
      await move(el, to, extra)
      await up(el, to, extra)
      await settle()
    },
    async tap(el, at, extra = {}) {
      await down(el, at, extra)
      await up(el, at, extra)
      await settle()
    },
  }
  return editor
}
