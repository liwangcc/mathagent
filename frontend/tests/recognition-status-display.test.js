import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

function pendingLabel(p) {
  const body = upload.match(/<span v-else-if="paperOcrPending\(p\)"[^>]*>([\s\S]*?)<\/span>/)[1]
  const helper = upload.match(/function paperOcrPendingLabel\(paper\) \{[\s\S]*?\n\}/)?.[0] || ''
  return body.replace(/\{\{([\s\S]*?)\}\}/g, (_, expression) => new Function('p', `${helper}; return (${expression})`)(p)).trim()
}

function reviewDraft(detail) {
  const row = { paper_id: 'synthetic-paper', disposition: 'active', details: [detail] }
  const deps = {
    progressList: { value: [row] },
    reviewPaperId: (value) => value.paper_id,
    manualDecisionRow: { value: null }, manualDecisionPages: { value: [] },
    manualDecisionPageIndex: { value: 0 }, manualDecisionDrafts: { value: [] },
    manualDecisionShowPaper: { value: false }, manualDecisionDialogVisible: { value: false },
    ElMessage: { error: (message) => assert.fail(message) }, loadManualDecisionImage: () => {},
  }
  const source = upload.match(/function openManualDecision\(row\) \{[\s\S]*?\n\}/)[0]
  new Function(...Object.keys(deps), `${source}; return openManualDecision`)(...Object.values(deps))(row)
  return deps.manualDecisionDrafts.value[0]
}

function diagnostic(detail) {
  const item = reviewDraft(JSON.parse(JSON.stringify(detail)))
  const row = upload.split('<span>学生答案</span>')[1].split('</div>')[0]
  for (const [, condition, text] of row.matchAll(/<small v-(?:else-)?if="([^"]+)" class="manual-review-diagnostic">([^<]+)<\/small>/g)) {
    if (new Function('item', `return (${condition})`)(item)) return text
  }
  return ''
}

for (const [status, label] of [['queued', '等待识别处理'], ['running', '正在识别处理']]) {
  test(`${status} describes the actual OCR stage rather than assuming accelerator work`, () => {
    assert.equal(pendingLabel({ ocr_status: status }), label)
  })
}

test('an explicit accelerator diagnostic is preserved without inferring it from queued', () => {
  assert.equal(pendingLabel({ ocr_status: 'queued', error: '等待教师客户端加速节点完成图片处理' }), '等待教师客户端加速节点完成图片处理')
})

for (const detail of [
  { error_type: 'ocr_slot_identity_invalid' },
  { error_type: 'ocr_failed', slot_identity_error: 'slot_contract_missing' },
  { error_type: 'ocr_unreadable', slot_identity_error: 'slot_contract_missing' },
]) {
  test(`slot evidence failure takes precedence over unreadable: ${detail.error_type}`, () => {
    const text = diagnostic({ ...detail, answer_kind: 'fill', answer_state: 'unreadable' })
    assert.match(text, /模板.*作答位/)
    assert.doesNotMatch(text, /笔迹未能可靠识别/)
  })
}

test('an OCR processing failure never masquerades as unreadable handwriting', () => {
  const text = diagnostic({ error_type: 'ocr_failed', answer_state: 'unreadable', answer_kind: 'fill' })
  assert.match(text, /识别处理未完成/)
  assert.doesNotMatch(text, /笔迹未能可靠识别/)
})

test('genuine unreadable handwriting still describes the original-image review path', () => {
  assert.match(diagnostic({ error_type: 'ocr_unreadable', answer_state: 'unreadable', answer_kind: 'fill' }), /原卷笔迹未能可靠识别/)
})
