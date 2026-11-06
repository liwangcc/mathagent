// The batch report as the grading page (views/Upload.vue) displays it: status
// text and tone, percentages and knowledge-point distribution rows, and the
// statistics fallback built in the browser when the server report is missing.
import { CLASSIFICATION_LABELS } from './knowledgeClassification.js'

export function buildClientBatchReportFallback(rows) {
  const totals = { paper_count: 0, question_count: 0, correct_count: 0, partial_count: 0, wrong_count: 0, needs_review_count: 0 }
  for (const row of rows || []) {
    if (!row || row.disposition === 'voided' || row.error) continue
    const details = Array.isArray(row.details) ? row.details : []
    const detailTotal = details.length
    const total = Math.max(0, Number(row.total) || detailTotal)
    if (!total && !detailTotal) continue
    totals.paper_count += 1
    totals.question_count += total
    for (const key of ['correct', 'partial', 'wrong', 'needs_review']) {
      const value = Math.max(0, Number(row[key]) || details.filter((detail) => detail?.result === key).length)
      totals[`${key}_count`] += value
    }
  }
  const judged = totals.question_count - totals.needs_review_count
  const errors = totals.partial_count + totals.wrong_count
  return {
    ...totals,
    error_rate: judged > 0 ? Number((errors / judged).toFixed(4)) : null,
    all_errors: [],
    top_errors: [],
    knowledge_points: [],
    knowledge_distribution: [],
  }
}

export function batchReportStatusText(status) {
  return ({
    not_started: '等待生成',
    queued: '等待生成',
    generating: '正在生成',
    ready: '已生成',
    failed: '生成失败',
  })[status] || '处理中'
}

export function batchReportStatusTone(status) {
  if (status === 'ready') return 'is-good'
  if (status === 'failed') return 'is-bad'
  return 'is-warn'
}

export function reportPercent(value) {
  return value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`
}

// The report waits for classification, so a bucket still without a knowledge
// point failed to classify; show that instead of the internal "未分类" key.
export function reportKnowledgeName(item) {
  const name = String(item?.topic || item?.name || '').trim()
  return !name || name === '未分类' ? CLASSIFICATION_LABELS.failed : name
}

export function reportKnowledgeChapter(item) {
  const chapter = String(item?.chapter || '').trim()
  return !chapter || chapter === '未分类' ? '未分章' : chapter
}

export function nonNegativeReportCount(value) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : 0
}

export function reportDistributionPercent(value, total) {
  const amount = Number(value)
  const denominator = Number(total)
  if (!Number.isFinite(amount) || !Number.isFinite(denominator) || denominator <= 0) return 0
  return Math.min(100, Math.max(0, (amount / denominator) * 100))
}

export function reportEntries(value) {
  return Object.entries(value || {}).sort((a, b) => Number(b[1]) - Number(a[1]))
}
