// The template editor of the real 批改 page (frontend/src/views/Upload.vue),
// opened the way a teacher opens it: choose 批次类型 (and 试卷栏数), pick a
// template image, click 识别模板. The recognition stream answers with `pages`
// (the questions of each page) and the stored cut result with `cutPages` (each
// page's columns, question anchors, answer regions and drawing schema), so
// what the editor shows is built by the page's own code from server answers.
//
//   import { cutPage, openTemplateEditor } from './helpers/templateDrawingSlots.js'
//   const editor = await openTemplateEditor(t, {
//     pages: [{ page_index: 0, questions: [{ question_no: '2.3', answer_kind: 'drawing', ... }] }],
//     cutPages: [cutPage({ anchors: ['2.3'], schema: [{ question_number: '2.3', answer_kind: 'drawing', ... }] })],
//   })
//   editor.editors(0)              // the DrawingReferenceEditor <section>s of page 0
//   await editor.loadImages()      // the template images finish loading
//   await editor.drawBox(0, [300, 400, 600, 700])   // drag on the armed tool's surface
//   await editor.save()            // click 保存模板（全部页）
//
// Options (the rest goes to helpers/uploadPage.js's mountUpload):
//   pages, cutPages   the two server answers described above
//   templateId        the template the server creates (default 'tpl-1')
//   category          the 批次类型 chosen before recognition (default 'daily')
//   columnMode        the 试卷栏数 chosen before recognition (default: the page's)
//   confirmResponse   templateConfirm's answer: data, or config => ({ status, data })
//                     (default: saved)
//   Layout saves (保存 writes each page that needs it first) answer with what
//   was sent, as a server that accepts the page does.
//
// Returns { upload, page, ... } (upload: mountUpload's result, page: upload.page):
//   dialog()          the 编辑本次模板 <el-dialog>
//   article(i)        the preview <article> of template page i
//   editors(i)        page i's DrawingReferenceEditor <section>s, in order
//   canvas(editor)    an editor's canvas <div> (image, SVG and overlays)
//   shown(el)         el exists and neither it nor an ancestor is hidden (v-show)
//   questionCard(position, number)  the detail card (div.tpl-q) of a question;
//                     position is the page's place in the editor's page list
//   answerPrompts(v)  what the teacher types into the next prompts (a question
//                     number); a prompt without it keeps its prefilled value
//   loadImages()      every template image reports `load`, as the browser does
//   drawBox(i, bbox)  drag a box on page i with the armed geometry tool, on a
//                     stage laid out as 1000 × 1000 (bbox in page units)
//   recut(cutPages)   click 重新自动裁剪; the server answers with cutPages
//   save()            click 保存模板（全部页） and settle
//   reopen()          click 再次编辑模板 (after a successful save) and settle
//   warnings()        ElMessage warnings shown so far
//   confirms()        templateConfirm request bodies sent so far
//   layoutSaves()     layout-overrides request bodies sent so far
//   questionEdits()   templateQuestionEdit request bodies sent so far
import { jsonResponse } from './memoryFetch.js'
import { mountUpload, paperFile } from './uploadPage.js'

export const TEMPLATE_ID = 'tpl-1'

// One stored cut-result page: a single-column plan the server trusts (so the
// page needs no layout review of its own), the printed question numbers
// (strings get a small question-number box each) and the saved regions.
// columnCount: null leaves the plan's explicit count out, as older plans do.
export function cutPage({
  pageIndex = 0,
  anchors = [],
  answerRegions = [],
  schema = [],
  columns = [{ x1_1000: 0, x2_1000: 1000 }],
  columnCount = columns.length,
  ...rest
} = {}) {
  return {
    page_index: pageIndex,
    page_label: `p${pageIndex + 1}`,
    anchor_crop_plan: {
      parameters: { columns, ...(columnCount === null ? {} : { column_count: columnCount }) },
      blocks: [],
    },
    question_anchors: anchors.map((number, index) => (typeof number === 'string'
      ? { question_number: number, question_number_bbox: [20, 40 + index * 120, 60, 70 + index * 120] }
      : number)),
    template_answer_regions: answerRegions,
    question_schema: schema,
    ...rest,
  }
}

const body = config => (typeof config.data === 'string' ? JSON.parse(config.data) : config.data)

export async function openTemplateEditor(t, {
  pages,
  cutPages,
  templateId = TEMPLATE_ID,
  category = 'daily',
  columnMode,
  confirmResponse = { success: true, page_count: pages.length, template_mode: 'marked', column_mode: 'single' },
  responses = {},
  streams = {},
  ...options
} = {}) {
  const base = `/api/batch-grade/template/${templateId}`
  const imageRoutes = Object.fromEntries(cutPages.map(page => [
    `GET ${base}/pages/${page.page_index}/image`,
    () => ({ status: 200, data: new Blob(['template page'], { type: 'image/png' }) }),
  ]))
  let typed = null
  const upload = await mountUpload(t, {
    messageBox: box => (box.kind === 'prompt' ? { value: typed ?? box.options?.inputValue ?? '' } : 'confirm'),
    ...options,
    responses: {
      ...imageRoutes,
      'POST /api/batch-grade/template/confirm': confirmResponse,
      [`PUT ${base}/layout-overrides`]: (config) => {
        const sent = body(config)
        return { status: 200, data: { success: true, template_id: templateId, ...sent } }
      },
      ...responses,
    },
    streams: {
      'POST /api/batch-grade/template/recognize': [['done', { template_id: templateId, pages }]],
      [`GET ${base}/cut-candidates`]: () => jsonResponse({ status: 'complete', pages: cutPages }),
      ...streams,
    },
  })
  const { page } = upload
  // A handler that throws is caught by Vue, not by the test: fail it here.
  t.after(() => {
    if (page.errors.length) throw new Error(`the page failed: ${page.errors.map(({ err, info }) => `${info}: ${err?.stack ?? err}`).join('\n')}`)
  })

  const setup = page.root.byClass('template-card')
  await upload.select(setup.byClass('batch-setup').find(el => el.tagName === 'select'), category)
  if (columnMode) await upload.select(setup.find(el => el.id === 'template-column-mode'), columnMode)
  const templateInput = setup.find(el => el.tagName === 'input' && el.type === 'file')
  templateInput.files = [paperFile('template.png')]
  await templateInput.dispatch('change')
  await upload.settle()
  await upload.click('识别模板')

  const dialog = () => page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
  const article = index => page.root.find(el => el.tagName === 'article' && el.id === `template-reference-page-${index}`)
  const editors = index => article(index)?.allByClass('drawing-reference-editor') ?? []
  const canvas = editor => editor.byClass('drawing-reference-canvas')
  const shown = (el) => {
    if (!el) return false
    for (let node = el; node && node.tagName; node = node.parentNode) if (node.style?.display === 'none') return false
    return true
  }

  return {
    upload,
    page,
    dialog,
    article,
    editors,
    canvas,
    shown,
    questionCard: (position, number) => page.root.allByClass('tpl-page')[position]
      ?.findAll(el => el.classList.contains('tpl-q'))
      .find(card => card.byClass('tpl-q-identity')?.text === `Q${number}`) ?? null,
    answerPrompts(value) { typed = value },
    async loadImages() {
      for (const img of page.root.findAll(el => el.tagName === 'img' && el.attributes.alt === '教师模板原图')) await img.dispatch('load')
      await upload.settle()
    },
    async drawBox(index, [left, top, right, bottom], { pointerId = 1 } = {}) {
      const surface = article(index).find(el => el.classList.contains('tpl-draw-surface') && shown(el))
      if (!surface) throw new Error(`no armed geometry tool on page ${index}`)
      // No layout engine here: the stage the surface measures is 1000 × 1000.
      const stage = surface.closest('.tpl-cut-preview-stage')
      stage.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 1000, width: 1000, height: 1000 })
      const at = (x, y) => ({ button: 0, pointerId, clientX: x, clientY: y })
      await surface.dispatch('pointerdown', at(left, top))
      await upload.settle()
      await surface.dispatch('pointermove', at(right, bottom))
      await surface.dispatch('pointerup', at(right, bottom))
      await upload.settle()
    },
    async recut(nextCutPages) {
      upload.stream(`POST ${base}/cut-candidates`, () => jsonResponse({ status: 'complete', pages: nextCutPages }))
      for (const next of nextCutPages) {
        upload.answer(`GET ${base}/pages/${next.page_index}/image`, () => ({ status: 200, data: new Blob(['template page'], { type: 'image/png' }) }))
      }
      await upload.click('重新自动裁剪')
    },
    // Like upload.click: does not wait for a confirmation the server holds back.
    async save() {
      await upload.click('保存模板（全部页）')
    },
    async reopen() {
      await upload.click('再次编辑模板')
    },
    warnings: () => upload.client.messages.filter(message => message.type === 'warning').map(message => message.message),
    confirms: () => upload.requests('POST /api/batch-grade/template/confirm').map(body),
    layoutSaves: () => upload.requests(`PUT ${base}/layout-overrides`).map(body),
    questionEdits: () => upload.requests(`PUT ${base}/questions`).map(body),
  }
}
