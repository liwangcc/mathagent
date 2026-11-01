// Bank knowledge labels: what to show when a question has none, and the
// two-level catalog picker teachers use to choose one by hand.
const UNCLASSIFIED = new Set(['', '未知', '未指定', '未标注', '未分类', '综合'])

export function hasKnowledge(row) {
  return !UNCLASSIFIED.has(String(row?.chapter ?? '').trim())
    && !UNCLASSIFIED.has(String(row?.topic ?? '').trim())
}

// A missing label is either still being classified or failed; "未分类" is
// never shown as if it were a knowledge point.
export function classificationState(row) {
  if (hasKnowledge(row)) return 'classified'
  return ['', 'pending'].includes(String(row?.classification_status ?? '')) ? 'pending' : 'failed'
}

export const CLASSIFICATION_LABELS = Object.freeze({
  pending: '分类中',
  failed: '分类失败',
})

export function catalogOptions(catalog) {
  return (Array.isArray(catalog) ? catalog : [])
    .filter((entry) => entry?.chapter)
    .map((entry) => ({
      value: entry.chapter,
      label: entry.chapter,
      children: (entry.topics || []).map((topic) => ({ value: topic, label: topic })),
    }))
}
