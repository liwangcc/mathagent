function nonNegativeInteger(value, fallback = 0) {
  const number = Number(value)
  return Number.isInteger(number) && number >= 0 ? number : fallback
}

// A teacher removed this unrecoverable page from the batch: it is resolved,
// listed, and never graded.
export function isExcludedPaper(paper) {
  return String(paper?.ocr_status || '').toLowerCase() === 'excluded'
}

export function isGradablePaper(paper) {
  return Boolean(paper && !isExcludedPaper(paper) && (!paper.error || paper.requires_manual_review))
}

export function countGradablePapers(papers = []) {
  return (Array.isArray(papers) ? papers : []).filter(isGradablePaper).length
}

// A `paper` SSE event is the server's current recognition outcome for that
// paper. Successful outcomes omit the failure fields instead of sending them
// empty, so outcome fields from an earlier snapshot (for example an
// accelerator-waiting row from the orientation state) must not survive.
const RECOGNITION_OUTCOME_FIELDS = ['error', 'error_info', 'requires_manual_review', 'ocr_status']

export function mergeRecognitionPaper(previous = {}, payload = {}) {
  const merged = { ...previous }
  for (const field of RECOGNITION_OUTCOME_FIELDS) {
    if (!Object.hasOwn(payload, field)) delete merged[field]
  }
  return { ...merged, ...payload }
}

export function normalizeBatchReadiness(done = {}, { expectedTotal = 0, papers = [] } = {}) {
  const paperList = Array.isArray(papers) ? papers : []
  const fallbackExpected = nonNegativeInteger(expectedTotal)
  const uploaded = nonNegativeInteger(done.uploaded_papers, fallbackExpected)
  const represented = nonNegativeInteger(done.total_papers, paperList.length)
  const inferredExcluded = paperList.filter(isExcludedPaper).length
  const inferredManualReview = paperList.filter((paper) => paper?.error && paper.requires_manual_review).length
  const inferredFailed = paperList.filter((paper) => paper?.error && !paper.requires_manual_review && !isExcludedPaper(paper)).length
  const inferredSucceeded = paperList.filter((paper) => paper && !paper.error).length
  const succeeded = nonNegativeInteger(done.succeeded_papers, inferredSucceeded)
  const manualReview = nonNegativeInteger(done.manual_review_papers, inferredManualReview)
  const failed = nonNegativeInteger(done.failed_papers, inferredFailed)
  const excluded = nonNegativeInteger(done.excluded_papers, inferredExcluded)
  const gradable = Math.max(0, uploaded - excluded)
  const unresolved = nonNegativeInteger(
    done.unresolved_papers,
    Math.max(0, uploaded - succeeded - manualReview - failed - excluded),
  )
  // An SSE `done` counts recognized papers (`total_papers`); a snapshot lists
  // one row per upload, including teacher-excluded pages. Without exclusions
  // both are the uploaded count.
  const structurallyComplete = uploaded > 0
    && gradable > 0
    && (!fallbackExpected || uploaded === fallbackExpected)
    && (represented === gradable || represented === uploaded)
    && paperList.length === uploaded
    && succeeded + manualReview === gradable
    && failed === 0
    && unresolved === 0
  const serverAllowsGrade = typeof done.can_grade === 'boolean' ? done.can_grade : structurallyComplete

  return {
    uploaded,
    succeeded,
    manualReview,
    failed,
    excluded,
    unresolved,
    canGrade: serverAllowsGrade && structurallyComplete,
  }
}

export function validateGradeCompletion({ expectedTotal = 0, reportedTotal, receivedTotal = 0 } = {}) {
  const expected = nonNegativeInteger(expectedTotal)
  const reported = nonNegativeInteger(reportedTotal, -1)
  const received = nonNegativeInteger(receivedTotal)
  const complete = expected > 0 && reported === expected && received === expected

  return {
    complete,
    message: complete
      ? ''
      : `批阅结果不完整：预期 ${expected} 张，服务端报告 ${reported < 0 ? '未知' : reported} 张，实际收到 ${received} 张。请重试批阅。`,
  }
}
