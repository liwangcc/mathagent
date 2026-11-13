// Standard answers the server finishes after the template editor has opened.
//
// A page is delivered while its slowest question is still being solved: that
// question arrives with `answer_pending: true` and an empty answer. A later
// read of the same template carries the answer, or `answer_failed` with the
// reason when none will come. takeTemplateAnswers moves what has arrived into
// the questions the teacher is editing, and only into those.
import { normalizeAnswerSpecForEditor } from './answerSpecEditor.js'
import { templateAnswerPending } from './templateReview.js'

// What an arrived answer brings, in the editor's own fields.
const ANSWER_FIELDS = [
  'standard_answer', 'answer_slots', 'answer_variants', 'answer_variants_text', 'solution_steps',
  'solution_steps_text', 'solve_visual_context',
]

const hasText = value => (Array.isArray(value) ? value.some(hasText) : String(value ?? '').trim() !== '')

// A question keeps its directory node through renumbering; a page-local number is the fallback.
const identityOf = (page, question) => question.directory_node_id
  || `${Number(page.page_index)}:${String(question.question_no ?? question.question_number ?? '')}`

/** The same slots, in the same shape the editor holds them: an array per typed slot, or joined text. */
const sameSlotShape = (mine, theirs) => (Array.isArray(mine)
  ? Array.isArray(theirs) && theirs.length === mine.length
  : typeof theirs === 'string')

function take(question, server) {
  if (typeof server.answer_failed === 'string' && server.answer_failed) {
    question.answer_failed = server.answer_failed
    delete question.answer_pending
    return
  }
  const arrived = normalizeAnswerSpecForEditor(server)
  // Whatever the teacher typed while waiting stays: their answer is the one they will confirm.
  if (!hasText(question.standard_answer) && !hasText(question.answer_slots_text)) {
    for (const field of ANSWER_FIELDS) {
      if (field in arrived) question[field] = arrived[field]
      else delete question[field]
    }
    if (sameSlotShape(question.answer_slots_text, arrived.answer_slots_text)) question.answer_slots_text = arrived.answer_slots_text
  }
  // A report the solver made about a position blocks saving until the teacher has dealt with it.
  if (Array.isArray(arrived.max_flags) && arrived.max_flags.length) question.max_flags = arrived.max_flags
  delete question.answer_pending
}

/**
 * Move every answer the server has finished into the question still waiting for it.
 * `pages` are the editor's pages (changed in place), `serverPages` a fresh read of the template.
 * Returns how many questions are still waiting.
 */
export function takeTemplateAnswers(pages, serverPages) {
  const fresh = new Map()
  for (const page of serverPages || []) {
    for (const question of page.questions || []) fresh.set(identityOf(page, question), question)
  }
  let waiting = 0
  for (const page of pages || []) {
    for (const question of page.questions || []) {
      if (!templateAnswerPending(question)) continue
      const server = fresh.get(identityOf(page, question))
      if (server && !templateAnswerPending(server)) take(question, server)
      else waiting += 1
    }
  }
  return waiting
}
