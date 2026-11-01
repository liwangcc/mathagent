import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const viewUrl = new URL('../src/views/Upload.vue', import.meta.url)

async function source() {
  return readFile(viewUrl, 'utf8')
}

test('ordinary paper review uses authenticated full-page workbench', async () => {
  const text = await source()
  assert.match(text, /原卷与批阅结果/)
  assert.match(text, /manualDecisionPages/)
  assert.match(text, /manualDecisionPageIndex/)
  assert.match(text, /api\.grade\.reviewImage\(batchId\.value, paperId\)/)
  assert.doesNotMatch(text, /manual-review-mark-layer|manualDecisionImageMarks/)
  assert.match(text, /api\.grade\.reviewImage\(batchId\.value, paperId, decisions\)/)
  assert.match(text, /下载批阅痕迹图/)
  assert.match(text, /URL\.revokeObjectURL\(manualDecisionImageUrl\.value\)/)
  assert.match(text, /上一页/)
  assert.match(text, /下一页/)
})

test('desktop workbench enlarges the paper and keeps preview scrolling separate from review cards', async () => {
  const text = await source()
  assert.match(text, /:class="\['manual-review-dialog', \{ 'is-paper-hidden': !manualDecisionShowPaper \}\]"/)
  assert.match(text, /:global\(\.manual-review-dialog \.el-dialog__body\).*overflow: hidden/)
  assert.match(text, /:global\(\.manual-review-dialog \.el-dialog__body\).*max-height: none/)
  assert.match(text, /\.manual-review-paper \{[^}]*overflow: hidden/)
  assert.match(text, /\.manual-review-image-stage \{[^}]*align-items: center[^}]*justify-content: center[^}]*overflow: hidden/)
  assert.match(text, /\.manual-review-paper img \{[^}]*width: auto[^}]*height: auto[^}]*max-width: 100%[^}]*max-height: 100%[^}]*object-fit: contain/)
  assert.match(text, /\.manual-review-list \{[^}]*overflow-y: auto/)
})

test('batch review image export remains available after archive and outside precise mode', async () => {
  const text = await source()
  assert.match(text, /@click="downloadAllReviewImages"/)
  assert.match(text, /批量下载批阅痕迹图/)
  assert.match(text, /reviewImagesDownloading \? '正在打包批阅图…'/)
  assert.match(text, /api\.grade\.reviewDownload\('\/batch-grade\/review\/export\/images'/)
  assert.match(text, /link\.href = manualDecisionImageUrl\.value/)
  const button = text.match(/<button[^>]*@click="downloadAllReviewImages"[^>]*>/)?.[0] || ''
  assert.doesNotMatch(button, /reviewArchived|anonymous|form\.mode/)
})

test('review cards omit confirmed question text and enlarge the paper workspace', async () => {
  const text = await source()
  assert.doesNotMatch(text, /<div><span>题目<\/span><p>\{\{ item\.question/)
  assert.match(text, /<span>学生答案<\/span><p v-html="renderAnswerLatex\(/)
  assert.match(text, /<span>参考答案<\/span><p v-html="renderAnswerLatex\(/)
  assert.match(text, /width: min\(1920px, calc\(100vw - 16px\)\) !important/)
  assert.match(text, /grid-template-columns: minmax\(0, 2\.25fr\) minmax\(360px, 0\.85fr\)/)
})

test('review cards use compact choice/fill rows and can hide the paper for a full-width review', async () => {
  const text = await source()
  assert.match(text, /manualDecisionShowPaper/)
  assert.match(text, /关闭原卷/)
  assert.match(text, /显示原卷/)
  assert.match(text, /manualDecisionIsCompact\(item\)/)
  assert.match(text, /class="manual-review-item is-compact"|is-compact/)
  assert.match(text, /grid-template-columns: 62px minmax\(0, 1fr\) minmax\(0, 1fr\) 116px/)
  assert.match(text, /question_type: detail\.question_type \|\| 'solve'/)
  assert.match(text, /\.manual-review-workspace\.is-paper-hidden \.manual-review-list \{[^}]*display: block/)
  assert.match(text, /\.manual-review-sheet-grid \{[^}]*width: max-content;[^}]*repeat\(var\(--sheet-columns\), minmax\(52px, max-content\)/)
  assert.match(text, /\.manual-review-sheet-answer \{ max-width: 112px; overflow-wrap: anywhere; white-space: normal; \}/)
  assert.match(text, /:global\(\.manual-review-dialog\.is-paper-hidden\) \{ height: auto !important; \}/)
  assert.match(text, /manual-review-sheet-grid/)
  assert.match(text, /<strong>选择 \/ 填空<\/strong>/)
  assert.match(text, /判定符号说明/)
  assert.match(text, /<b class="manual-review-legend-symbol is-unread">·<\/b>未识别/)
  assert.match(text, /<b class="manual-review-legend-symbol is-correct">✓<\/b>对/)
  assert.match(text, /<b class="manual-review-legend-symbol is-partial">△<\/b>半对/)
  assert.match(text, /<b class="manual-review-legend-symbol is-wrong">✗<\/b>错/)
  assert.match(text, /\.manual-review-sheet-cell\.is-current-page \{ background: var\(--primary-soft\); \}/)
  assert.match(text, /\.manual-review-list-toolbar \.manual-review-legend > span \{ display: inline-flex; margin-top: 0; \}/)
  assert.match(text, /<option value="correct">✓<\/option>/)
  assert.match(text, /<option value="wrong">✗<\/option>/)
})

test('question cards jump only by explicit backend page reference', async () => {
  const text = await source()
  assert.match(text, /jumpToManualDecisionPage\(item\.page_index\)/)
  assert.match(text, /paper_id: String\(detail\.paper_id/)
  assert.match(text, /page_index: Number\(detail\.page_index/)
  assert.doesNotMatch(text, /question_no\s*[<>]=?\s*\d+\s*\?\s*\d+/)
})

test('fresh grading reloads authoritative persisted review contracts', async () => {
  const text = await source()
  assert.match(text, /const reviewPayload = await api\.grade\.reviewGet\(batchId\.value\)/)
  assert.match(text, /hydrateReviewState\(reviewPayload\)/)
  assert.match(text, /review_group_id/)
})

test('multi-page teacher decisions remain audited per original paper id', async () => {
  const text = await source()
  assert.match(text, /const paperIds = \[\.\.\.new Set/)
  assert.match(text, /paper_id: paperId/)
  assert.match(text, /review_revision: reviewRevision\.value/)
  assert.match(text, /item\.paper_id === paperId/)
})
