import assert from 'node:assert/strict'
import test from 'node:test'

import {
  countGradablePapers,
  mergeRecognitionPaper,
  normalizeBatchReadiness,
  validateGradeCompletion,
} from '../src/utils/batchReadiness.js'

function successfulPapers(count) {
  return Array.from({ length: count }, (_, index) => ({ paper_id: `paper-${index + 1}` }))
}

test('complete OCR batch is ready for grading', () => {
  const papers = successfulPapers(10)
  const readiness = normalizeBatchReadiness({
    uploaded_papers: 10,
    succeeded_papers: 10,
    manual_review_papers: 0,
    failed_papers: 0,
    unresolved_papers: 0,
    total_papers: 10,
    can_grade: true,
  }, { expectedTotal: 10, papers })

  assert.equal(readiness.canGrade, true)
  assert.equal(countGradablePapers(papers), 10)
})

test('partial OCR batch cannot grade even when three successful papers were returned', () => {
  const papers = successfulPapers(3)
  const readiness = normalizeBatchReadiness({
    uploaded_papers: 10,
    succeeded_papers: 3,
    failed_papers: 7,
    total_papers: 3,
    can_grade: false,
  }, { expectedTotal: 10, papers })

  assert.equal(readiness.canGrade, false)
  assert.equal(readiness.failed, 7)
})

test('recoverable OCR placeholders remain gradable and count as manual review', () => {
  const papers = [
    ...successfulPapers(3),
    ...Array.from({ length: 7 }, (_, index) => ({
      paper_id: `review-${index + 1}`,
      error: 'OCR 识别失败',
      requires_manual_review: true,
    })),
  ]
  const readiness = normalizeBatchReadiness({
    uploaded_papers: 10,
    succeeded_papers: 3,
    manual_review_papers: 7,
    failed_papers: 0,
    unresolved_papers: 0,
    total_papers: 10,
    can_grade: true,
  }, { expectedTotal: 10, papers })

  assert.equal(readiness.canGrade, true)
  assert.equal(countGradablePapers(papers), 10)
})

test('grading completion requires expected, reported, and received counts to match', () => {
  assert.equal(validateGradeCompletion({ expectedTotal: 10, reportedTotal: 10, receivedTotal: 10 }).complete, true)

  const incomplete = validateGradeCompletion({ expectedTotal: 10, reportedTotal: 3, receivedTotal: 3 })
  assert.equal(incomplete.complete, false)
  assert.match(incomplete.message, /预期 10 张/)
  assert.match(incomplete.message, /实际收到 3 张/)
})

test('resumed success events clear accelerator-waiting snapshot rows before grading', () => {
  // Orientation snapshot after ACCELERATOR_WAITING: 15 recognized papers plus
  // 8 queued rows whose job last_error is the waiting message.
  const waitingRow = (index) => ({
    paper_id: `waiting-${index + 1}`,
    error: '等待教师客户端加速节点完成图片处理',
    requires_manual_review: false,
    ocr_status: 'queued',
    orientation_status: 'auto_upright',
  })
  const snapshot = [
    ...successfulPapers(15).map((paper) => ({ ...paper, error: '', ocr_status: 'succeeded' })),
    ...Array.from({ length: 8 }, (_, index) => waitingRow(index)),
  ]
  // The resume `done` path re-emits every paper; successful rows omit failure fields.
  const papers = snapshot.map((row) => mergeRecognitionPaper(row, {
    paper_id: row.paper_id,
    questions_count: 5,
    orientation_status: row.orientation_status || 'auto_upright',
  }))

  const readiness = normalizeBatchReadiness({
    uploaded_papers: 23,
    succeeded_papers: 23,
    manual_review_papers: 0,
    failed_papers: 0,
    unresolved_papers: 0,
    total_papers: 23,
    can_grade: true,
  }, { expectedTotal: 23, papers })
  assert.equal(readiness.canGrade, true)
  assert.equal(countGradablePapers(papers), 23)
  assert.equal(papers.some((paper) => 'ocr_status' in paper || 'error' in paper), false)
  assert.equal(validateGradeCompletion({
    expectedTotal: countGradablePapers(papers),
    reportedTotal: 23,
    receivedTotal: 23,
  }).complete, true)
})

test('a later failure event still replaces an earlier successful row', () => {
  const failed = mergeRecognitionPaper(
    { paper_id: 'p-1', questions_count: 5 },
    { paper_id: 'p-1', error: 'OCR 识别失败' },
  )
  assert.equal(countGradablePapers([failed]), 0)

  const needsReview = mergeRecognitionPaper(
    { paper_id: 'p-2', error: '等待加速节点', ocr_status: 'queued' },
    { paper_id: 'p-2', error: 'OCR 识别失败', requires_manual_review: true },
  )
  assert.equal(countGradablePapers([needsReview]), 1)
  assert.equal('ocr_status' in needsReview, false)
})

test('a teacher-excluded page is resolved, listed, and never counted as gradable', () => {
  const papers = [
    ...successfulPapers(9),
    { paper_id: 'excluded-1', error: '已由教师排除，不参与本批批阅', requires_manual_review: false, ocr_status: 'excluded' },
  ]
  // Snapshot shape: one row per upload, no `total_papers`.
  const snapshot = normalizeBatchReadiness({
    uploaded_papers: 10,
    succeeded_papers: 9,
    manual_review_papers: 0,
    failed_papers: 0,
    excluded_papers: 1,
    unresolved_papers: 0,
    can_grade: true,
  }, { expectedTotal: 10, papers })
  assert.equal(snapshot.canGrade, true)
  assert.equal(snapshot.excluded, 1)
  assert.equal(countGradablePapers(papers), 9)

  // SSE `done` shape: `total_papers` counts only recognized papers.
  const done = normalizeBatchReadiness({
    uploaded_papers: 10,
    succeeded_papers: 9,
    failed_papers: 0,
    excluded_papers: 1,
    unresolved_papers: 0,
    total_papers: 9,
    can_grade: true,
  }, { expectedTotal: 10, papers })
  assert.equal(done.canGrade, true)

  // Excluding every page leaves nothing to grade.
  const allExcluded = normalizeBatchReadiness({
    uploaded_papers: 2, succeeded_papers: 0, excluded_papers: 2, unresolved_papers: 0, can_grade: true,
  }, {
    expectedTotal: 2,
    papers: [{ ocr_status: 'excluded', error: 'x' }, { ocr_status: 'excluded', error: 'x' }],
  })
  assert.equal(allExcluded.canGrade, false)
})
