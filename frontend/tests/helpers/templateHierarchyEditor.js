// The teacher's template editor ("编辑本次模板") on the real 批改 page
// (helpers/uploadPage.js), opened the way a teacher opens it: pick the
// template pages, click 识别模板, and the page loads what the services
// return — the recognized questions, the cut result with every page's
// located questions, and each page's image.
//
//   const editor = await openTemplateEditor(t, {
//     category: 'exam',
//     pages: [[{ question_no: '1', question_type: 'solve', question_text: '…', standard_answer: '…' }]],
//   })
//   editor.question('1')                  // the question's card in the answer editor
//   await editor.save()                   // click 保存模板（全部页）
//   editor.layoutSaves()[0]               // the page geometry the save sent (PUT layout-overrides)
//   editor.confirms()[0]                  // the template the save confirmed (POST confirm)
//
// Options:
//   pages       the recognized questions, one list per template page
//   recognized  more fields of each recognized page (e.g. question_anchors)
//   cut         the cut result's pages (default: locatedPage(i, questions)
//               for every page: one column, a printed number and an answer
//               box per question, the question schema), or the whole state
//               ({ status, pages, ... })
//   category    'daily' (the page's default) or 'exam' (chosen with an exam name)
//   templateMode  the template_mode recognition reports ('marked' or 'blank')
//   responses, streams   more routes for helpers/uploadPage.js (they win)
//   ...the rest goes to mountUpload (timers, ...)
//
// A browser loads each page image: by default every template image on the
// editor reports `load` once the previews are shown (imagesLoad: false keeps
// them loading).
import { jsonResponse } from './memoryFetch.js'
import { mountUpload, paperFile } from './uploadPage.js'

export const TEMPLATE_ID = 'tpl-hierarchy'
const base = `/api/batch-grade/template/${TEMPLATE_ID}`
export const ROUTES = Object.freeze({
  recognize: 'POST /api/batch-grade/template/recognize',
  cutState: `GET ${base}/cut-candidates`,
  cutRun: `POST ${base}/cut-candidates`,
  layout: `PUT ${base}/layout-overrides`,
  questions: `PUT ${base}/questions`,
  correction: 'POST /api/batch-grade/template/correction-candidates',
  confirm: 'POST /api/batch-grade/template/confirm',
  image: pageIndex => `GET ${base}/pages/${pageIndex}/image`,
})

const METADATA = ['directory_node_id', 'directory_parent_id', 'printed_path', 'answer_kind', 'region_kinds', 'slot_ids']
const pick = (source, keys) => Object.fromEntries(keys.filter(key => Object.hasOwn(source, key)).map(key => [key, structuredClone(source[key])]))
// Typed answer positions decide whether a question is drawn, as on the server.
const isDrawing = question => (Array.isArray(question.region_kinds) && question.region_kinds.length
  ? question.region_kinds.includes('drawing')
  : ['drawing', 'mixed'].includes(question.answer_kind))
const isPureDrawing = question => (Array.isArray(question.region_kinds) && question.region_kinds.length
  ? question.region_kinds.every(kind => kind === 'drawing')
  : question.answer_kind === 'drawing')

// A template page the cut service has located completely: one column, the
// printed number of every question, an answer box per answer position (a
// drawing position is its drawing area; a pure drawing without positions has
// none), a drawing area beside every drawing question and the question
// schema. Rows are 90 units apart from the top.
export function locatedPage(pageIndex, questions, extra = {}) {
  const rows = questions.map((question, row) => ({ question, top: 40 + row * 90 }))
  const drawingArea = top => [600, top, 900, top + 80]
  return {
    page_index: pageIndex,
    page_label: `p${pageIndex + 1}`,
    anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }], column_count: 1 } },
    question_anchors: rows.map(({ question, top }) => ({
      question_number: question.question_no,
      question_number_bbox: [20, top, 60, top + 30],
    })),
    template_answer_regions: rows.filter(({ question }) => question.slot_ids || !isPureDrawing(question)).map(({ question, top }) => ({
      question_number: question.question_no,
      regions: (question.slot_ids ?? [null]).map((_, slot) => (question.region_kinds?.[slot] === 'drawing'
        ? drawingArea(top) : [100 + slot * 150, top, 230 + slot * 150, top + 60])),
      ...(question.slot_ids ? { slot_ids: [...question.slot_ids] } : {}),
    })),
    question_schema: rows.map(({ question, top }) => ({
      question_number: question.question_no,
      answer_kind: 'non_graphic',
      ...pick(question, METADATA),
      ...(isDrawing(question) ? { drawing_regions: [drawingArea(top)], label_anchor: [585, top + 40] } : {}),
    })),
    ...extra,
  }
}

const parse = config => (typeof config.data === 'string' ? JSON.parse(config.data) : config.data)

// The server accepting a page save as sent.
export function acceptLayout(config) {
  const payload = parse(config)
  return {
    status: 200,
    data: {
      success: true,
      template_id: TEMPLATE_ID,
      page_index: payload.page_index,
      columns: payload.columns,
      question_anchors: payload.question_anchors,
      question_schema: payload.question_schema,
      ...(payload.template_answer_regions === undefined ? {} : { template_answer_regions: payload.template_answer_regions }),
      template_handwriting_regions: payload.template_handwriting_regions,
    },
  }
}

// Prompts answer with their suggested value (the teacher accepts it), other
// boxes are confirmed, unless the test says otherwise.
const acceptPrompts = entry => (entry.kind === 'prompt' ? { value: entry.options?.inputValue ?? '' } : 'confirm')

export async function openTemplateEditor(t, {
  pages,
  recognized: recognizedExtra = [],
  cut,
  category = 'daily',
  examName = '期中考试',
  templateMode = 'marked',
  imagesLoad = true,
  responses = {},
  streams = {},
  ...options
} = {}) {
  const recognized = pages.map((questions, pageIndex) => ({
    page_index: pageIndex,
    page_label: `p${pageIndex + 1}`,
    questions: structuredClone(questions),
    ...structuredClone(recognizedExtra[pageIndex] ?? {}),
  }))
  const cutState = Array.isArray(cut) || cut === undefined
    ? { status: 'complete', pages: cut ?? pages.map((questions, pageIndex) => locatedPage(pageIndex, questions)) }
    : cut
  const images = Object.fromEntries(pages.map((_, pageIndex) => [ROUTES.image(pageIndex), new Blob([`page ${pageIndex}`], { type: 'image/png' })]))
  const upload = await mountUpload(t, {
    messageBox: acceptPrompts,
    ...options,
    responses: {
      ...images,
      [ROUTES.layout]: acceptLayout,
      [ROUTES.confirm]: { success: true, page_count: pages.length, template_mode: templateMode, column_mode: 'single' },
      ...responses,
    },
    streams: {
      [ROUTES.recognize]: [['done', { template_id: TEMPLATE_ID, template_mode: templateMode, column_mode: 'single', pages: recognized }]],
      // The page edits the result it reads; the service answers afresh each time.
      [ROUTES.cutState]: () => jsonResponse(structuredClone(cutState)),
      ...streams,
    },
  })
  const { page } = upload

  if (category !== 'daily') {
    const setup = page.root.byClass('batch-setup')
    await upload.select(setup.find(el => el.tagName === 'select'), category)
    if (category === 'exam') await upload.type(setup.find(el => el.tagName === 'input'), examName)
  }
  const input = page.root.byClass('template-card').find(el => el.tagName === 'input' && el.type === 'file')
  input.files = pages.map((_, pageIndex) => paperFile(`template-${pageIndex + 1}.png`))
  await input.dispatch('change')
  await upload.settle()
  await upload.click('识别模板')
  await upload.settle()

  const dialog = () => page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
  const parsed = route => upload.requests(route).map(parse)

  const editor = {
    upload,
    page,
    dialog,
    // The template images a browser has finished loading.
    async loadImages() {
      for (const img of dialog().findAll(el => el.tagName === 'img' && el.attributes.alt === '教师模板原图')) {
        await img.dispatch('load')
      }
      await upload.settle()
    },
    // One template page's preview card (第 N 页 with its canvas and tools).
    preview: (pageIndex = 0) => dialog().find(el => el.id === `template-reference-page-${pageIndex}`),
    // The image the page shows (each drawing question has an editor of its
    // own; one canvas is visible at a time and carries the page's markers).
    canvas: (pageIndex = 0) => editor.preview(pageIndex).findAll(el => el.classList.contains('drawing-reference-canvas'))
      .find(el => el.style.display !== 'none') ?? null,
    // A question's card in the answer editor, by its number.
    question: number => dialog().findAll(el => el.classList.contains('tpl-q'))
      .find(card => card.byClass('tpl-q-identity')?.text.split(' · ')[0] === `Q${number}`) ?? null,
    // A labelled field of a question's card (题干, 填空数量, 各小问分值, ...).
    field: (number, label) => editor.question(number)?.findAll(el => el.classList.contains('ci-field'))
      .find(field => field.byClass('form-label')?.text.startsWith(label)) ?? null,
    // The 作图属性 select of a question's card.
    drawingKind: number => editor.question(number)?.find(el => el.tagName === 'select' && el.attributes['aria-label'] === `第 ${number} 题作图属性`) ?? null,
    // The legend beside a page's image: its entries as the teacher reads them.
    legend: (pageIndex = 0) => editor.preview(pageIndex).find(el => el.attributes['aria-label'] === '图外题号与作答框对应表'),
    legendItems: (pageIndex = 0) => editor.legend(pageIndex).byClass('legend-items').children.map(el => el.text),
    counts: (pageIndex = 0) => editor.preview(pageIndex).byClass('tpl-page-counts').text,
    // Click as a user would (a disabled control cannot be clicked) and wait
    // for what the click does, including the requests it sends.
    async press(el) {
      if (!el) throw new Error('nothing to press')
      if (el.hasAttribute('disabled')) throw new Error(`"${el.text}" is disabled`)
      await el.dispatch('click')
      await upload.settle()
    },
    bankButton: () => upload.button('写入私库'),
    bankEnabled: () => !editor.bankButton().hasAttribute('disabled'),
    reviewAlert: () => dialog().findAll(el => el.classList.contains('tpl-layout-review-alert') && el.text.startsWith('模板定位仍需教师复核'))[0] ?? null,
    async save() { await upload.click('保存模板（全部页）') },
    layoutSaves: () => parsed(ROUTES.layout),
    confirms: () => parsed(ROUTES.confirm),
    corrections: () => parsed(ROUTES.correction),
    questionEdits: () => parsed(ROUTES.questions),
    // The questions a payload carries, by number.
    sentQuestions: payload => Object.fromEntries(payload.pages.flatMap(item => item.questions).map(question => [question.question_no, question])),
    messages: type => upload.client.messages.filter(message => !type || message.type === type).map(message => message.message),
  }
  if (imagesLoad) await editor.loadImages()
  return editor
}

// Choose a value of a native <select> whose @change reads $event.target.value
// (the 作图属性 select), as a browser sets it before the change event.
export async function chooseValue(upload, select, value) {
  if (select.hasAttribute('disabled')) throw new Error('the select is disabled')
  select.value = value
  await select.dispatch('change')
  await upload.settle()
}

// Drag a pointer from an element of a template preview by (dx, dy) pixels.
// The in-memory DOM has no layout: the preview canvas is given the size a
// browser would lay it out at (1000 x 1000 px, one pixel per page unit) and
// the pressed element the closest() lookup the page's handlers use to find it.
export async function drag(upload, el, { dx, dy }) {
  let stage = el.parentNode
  while (stage && !stage.classList?.contains('tpl-cut-preview-stage')) stage = stage.parentNode
  if (!stage) throw new Error('the element is not on a template preview canvas')
  Object.defineProperty(stage, 'clientWidth', { value: 1000, configurable: true })
  Object.defineProperty(stage, 'clientHeight', { value: 1000, configurable: true })
  el.closest = selector => (selector === '.tpl-cut-preview-stage' ? stage : null)
  await el.dispatch('pointerdown', { pointerId: 1, button: 0, clientX: 0, clientY: 0 })
  await el.dispatch('pointermove', { pointerId: 1, clientX: dx, clientY: dy })
  await el.dispatch('pointerup', { pointerId: 1, clientX: dx, clientY: dy })
  await upload.settle()
}
