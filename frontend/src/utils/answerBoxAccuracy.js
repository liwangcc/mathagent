// Answer-box counts and accuracy: every answer box the grader judged
// counts once, so a fill question with seven right blanks and one wrong reads
// 7 correct, 1 wrong, 87.5% — not 0% as its whole-question "partial" would.
// The server counts boxes too; counting from the details also reads reviews
// saved before it did. Exam scores stay separate.

const VERDICTS = ['correct', 'partial', 'wrong', 'needs_review']

// A folded fill question keeps one verdict per box in grading_units; any other
// detail is one judged box. Rows without details keep their own counts and total.
export function answerBoxTally(row) {
  const tally = { correct: 0, partial: 0, wrong: 0, needs_review: 0 }
  const details = Array.isArray(row?.details) ? row.details : []
  if (!details.length) {
    for (const key of VERDICTS) tally[key] = Math.max(0, Number(row?.[key]) || 0)
    return { ...tally, total: Math.max(0, Number(row?.total) || 0) }
  }
  for (const detail of details) {
    if (!detail || detail.result === 'void') continue
    const units = Array.isArray(detail.grading_units) && detail.grading_units.length ? detail.grading_units : [detail]
    for (const unit of units) tally[VERDICTS.includes(unit?.result) ? unit.result : 'needs_review'] += 1
  }
  return { ...tally, total: tally.correct + tally.partial + tally.wrong + tally.needs_review }
}

// Correct boxes over judged boxes; none while any box still waits for review.
export function answerBoxRate(tally) {
  const judged = tally.correct + tally.partial + tally.wrong
  return tally.needs_review || !judged ? null : tally.correct / judged
}

// 0.875 -> 87.5, 1 -> 100: one decimal, no trailing zero.
export function percentNumber(rate) {
  return Number((rate * 100).toFixed(1))
}
