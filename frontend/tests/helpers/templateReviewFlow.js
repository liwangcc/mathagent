// The template editor (编辑本次模板) of the real 批改 page, frontend/src/views/
// Upload.vue, mounted through helpers/uploadPage.js and opened the way the
// page opens it after a reload: the teacher's checkpoint names a template the
// server has finished recognizing, the page reads that job back and opens the
// editor with the recognized pages.
//
//   import { mountTemplateEditor } from './helpers/templateReviewFlow.js'
//   const editor = await mountTemplateEditor(t, { pages, category: 'exam', examName: '期中' })
//   editor.dialog()          // the open <el-dialog title="编辑本次模板">
//   editor.choiceCell('3')   // the 选择题答题卡 cell of question 3
//   await editor.save()      // click 保存模板（全部页）
//   editor.confirmed()       // template confirm payloads sent (parsed JSON), in order
//
// The server side (templateServer) answers like a finished import: the
// recognition job with the given pages, a complete one-column cut for every
// page with a question-number anchor and an answer box per question (so the
// geometry needs no teacher review), the page images, the correction
// discovery (`discovery`, default: nothing found) and the template confirm.
//
// mountTemplateEditor options:
//   pages               the recognized pages: [{ page_index, questions: [...] }]
//   templateId          default 'tpl-review'
//   category, examName  the batch type in the checkpoint and the job
//   discovery           the correction-candidates answer: its data, or
//                       config => ({ status, data }) (may return a promise)
//   ...the rest goes to mountUpload (responses, streams, localStorage, timers;
//   timers default to true)
//
// Returns mountUpload's upload object plus:
//   templateId, dialog(), isOpen(), choiceCell(no), fillCell(no), question(no)
//   (the detail block in 第 N 页), save(), confirmed(), discoveries()
//
// brief(el) describes an element in a few words (null for none), to assert
// that something is absent: assert.equal(brief(el), null).
import { jsonResponse } from './memoryFetch.js'
import { mountUpload, TEACHER } from './uploadPage.js'

export const TEMPLATE_CONFIRM = 'POST /api/batch-grade/template/confirm'
export const TEMPLATE_DISCOVERY = 'POST /api/batch-grade/template/correction-candidates'
export const templateJobRoute = templateId => `GET /api/batch-grade/template/${templateId}/recognition`
export const templateCutRoute = templateId => `GET /api/batch-grade/template/${templateId}/cut-candidates`
export const templateImageRoute = (templateId, pageIndex) => `GET /api/batch-grade/template/${templateId}/pages/${pageIndex}/image`
export const templateCheckpointKey = (teacher = TEACHER) => `mathagent_template_recognition:${teacher.id}`

// The checkpoint the page keeps while a template import is unfinished.
export function templateCheckpoint(templateId, { category = 'daily', examName = '', columnMode = 'single' } = {}) {
  return JSON.stringify({ template_id: templateId, category, exam_name: examName, template_mode: 'marked', column_mode: columnMode })
}

// The recognition job state of a finished import.
export function templateJob(templateId, pages, { category = 'daily', examName = '', status = 'ocr_done' } = {}) {
  return { status, template_id: templateId, category, exam_name: examName, template_mode: 'marked', column_mode: 'single', pages }
}

// A complete automatic cut: one column per page, every question with its
// printed-number anchor and one answer box, nothing left for review.
export function templateCut(pages) {
  return {
    status: 'complete',
    pages: pages.map((page) => {
      const numbers = (page.questions || []).map(question => String(question.question_no))
      return {
        page_index: page.page_index,
        page_label: `page-${page.page_index}`,
        anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }], column_count: 1 }, blocks: [] },
        question_anchors: numbers.map((number, i) => ({ question_number: number, question_number_bbox: [10, 40 + i * 90, 60, 80 + i * 90] })),
        template_answer_regions: numbers.map((number, i) => ({ question_number: number, bbox: [100, 40 + i * 90, 400, 110 + i * 90] })),
        question_schema: numbers.map(number => ({ question_number: number, answer_kind: 'non_graphic' })),
      }
    }),
  }
}

// What the server answers while the teacher reviews a recognized template.
export function templateServer(templateId, pages, { category = 'daily', examName = '', discovery = { candidates: [] } } = {}) {
  const imageAnswers = Object.fromEntries(pages.map(page => [
    templateImageRoute(templateId, page.page_index),
    () => ({ status: 200, data: new Blob(['page image'], { type: 'image/jpeg' }) }),
  ]))
  return {
    streams: {
      [templateJobRoute(templateId)]: () => jsonResponse(templateJob(templateId, pages, { category, examName })),
      [templateCutRoute(templateId)]: () => jsonResponse(templateCut(pages)),
    },
    responses: {
      ...imageAnswers,
      [TEMPLATE_DISCOVERY]: discovery,
      [TEMPLATE_CONFIRM]: () => ({ status: 200, data: { success: true, page_count: pages.length, template_mode: 'marked', column_mode: 'single' } }),
    },
  }
}

// An element as a short string, null when there is none, for asserting that
// something is not on the page: a failing assert.equal(element, null) makes
// the test runner print the element, i.e. the whole mounted tree around it.
export const brief = el => (el ? `<${el.tagName}${el.className ? ` class="${el.className}"` : ''}>${el.text.slice(0, 60)}` : null)

const identityOf = el => el?.text.split(' ')[0] ?? ''

export async function mountTemplateEditor(t, {
  pages,
  templateId = 'tpl-review',
  category = 'daily',
  examName = '',
  discovery,
  responses = {},
  streams = {},
  localStorage = {},
  timers = true,
  ...options
} = {}) {
  const server = templateServer(templateId, pages, { category, examName, ...(discovery === undefined ? {} : { discovery }) })
  const upload = await mountUpload(t, {
    timers,
    ...options,
    localStorage: { [templateCheckpointKey(options.user)]: templateCheckpoint(templateId, { category, examName }), ...localStorage },
    streams: { ...server.streams, ...streams },
    responses: { ...server.responses, ...responses },
  })
  const dialog = () => upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
  const cell = (kind, no) => dialog()?.allByClass(`tpl-${kind}-answer-cell`)
    .find(el => identityOf(el.byClass('tpl-answer-card-focus')) === `Q${no}`) ?? null
  return Object.assign(upload, {
    templateId,
    dialog,
    isOpen: () => dialog()?.attributes['data-open'] === 'true',
    choiceCell: no => cell('choice', no),
    fillCell: no => cell('fill', no),
    question: no => dialog()?.allByClass('tpl-q').find(el => identityOf(el.byClass('tpl-q-identity')) === `Q${no}`) ?? null,
    save: () => upload.click('保存模板（全部页）'),
    confirmed: () => upload.requests(TEMPLATE_CONFIRM).map(config => JSON.parse(config.data)),
    discoveries: () => upload.requests(TEMPLATE_DISCOVERY).map(config => JSON.parse(config.data)),
  })
}
