import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

// The real component, compiled as the development build compiles it; a
// compile error fails this import. Rendering it to HTML also checks that
// text is escaped (a v-html would not be).
const { default: component } = await import('../src/components/DrawingAssessment.vue?sfc')
const image = 'data:image/png;base64,aGVsbG8='
const render = (assessment, textAssessment = null) => renderToString(createSSRApp(component, { assessment, textAssessment }))
const gate = (variants, threshold = 80) => ({ version: 'drawing-pixel-coarse-v1', threshold, variants })
// The scoped style adds its data-v-* attribute after the bound ones.
const variantHtml = (html, index) => html.match(new RegExp(`<figure data-variant-index="${index}"[^>]*>([\\s\\S]*?)</figure>`))?.[1] || ''

test('all pixel-rejected candidates render wrong without invented model confidence', async () => {
  const html = await render({ decision: 'wrong', reason: '全部完整答案均低于像素门槛',
    pixel_gate: gate([{ variant_index: 0, status: 'fail', score: 0, reason: '没有有效笔迹' }]),
    checks: [{ variant_index: 0, result: 'pixel_rejected', p_correct: null, reason: '粗筛不通过', model_called: false }],
    evidence: { student_image: image, overlays: [image] } })
  assert.match(html, /画图核对：判错/)
  assert.match(html, /data-decision="wrong"/)
  assert.match(html, /像素粗筛 ≥ 80%/)
  const row = variantHtml(html, 0)
  assert.match(row, /像素粗筛：0% · 拦截/)
  assert.match(row, /未调用模型/)
  assert.doesNotMatch(row, /p_correct=/)
})

test('shuffled candidate checks and pixel scores bind by variant_index, not list position', async () => {
  const html = await render({ decision: 'correct', reason: '完整标准2通过双门',
    pixel_gate: gate([
      { variant_index: 1, status: 'pass', score: 96.78, reason: '第二套像素通过' },
      { variant_index: 0, status: 'fail', score: 12, reason: '第一套像素拦截' },
    ]),
    checks: [
      { variant_index: 1, result: 'correct', p_correct: 97, reason: '第二套模型一致', model_called: true },
      { variant_index: 0, result: 'pixel_rejected', p_correct: null, reason: '第一套不调用模型', model_called: false },
    ], evidence: { overlays: [image, image] } })
  assert.match(html, /画图核对：达到自动通过标准/)
  assert.match(html, /同一个完整标准/)
  const first = variantHtml(html, 0), second = variantHtml(html, 1)
  assert.match(first, /像素粗筛：12% · 拦截/)
  assert.match(first, /未调用模型/)
  assert.doesNotMatch(first, /第二套|p_correct=/)
  assert.match(second, /像素粗筛：96.78% · 通过/)
  assert.match(second, /已调用模型/)
  assert.match(second, /p_correct=97%/)
  assert.doesNotMatch(second, /第一套/)
})

test('unavailable gate stays review and unknown score is not presented as zero', async () => {
  const html = await render({ decision: 'needs_review', reason: '图像检测失败',
    pixel_gate: gate([{ variant_index: 2, status: 'unavailable', score: null, reason: '无法可靠提取笔迹' }]),
    checks: [{ variant_index: 2, result: 'needs_review', p_correct: null, reason: '等待老师', model_called: false }] })
  assert.match(html, /画图核对：待老师复核/)
  assert.match(variantHtml(html, 2), /完整标准 3/)
  assert.match(variantHtml(html, 2), /像素粗筛：未知 · 不可用，需复核/)
  assert.match(variantHtml(html, 2), /未调用模型/)
  assert.doesNotMatch(variantHtml(html, 2), /0%|p_correct=/)
})

test('legacy evidence retains old 95 rule and never claims a pixel gate ran', async () => {
  const html = await render({ decision: 'correct', reason: '旧判分',
    checks: [{ variant_index: 0, result: 'correct', p_correct: 95, reason: '旧标准一致', model: 'qwen3.8-flash' }],
    evidence: { overlays: [image] } })
  assert.match(html, /本记录无像素粗筛结果/)
  assert.match(html, /原自动通过标准为模型判对且 p_correct ≥ 95%/)
  assert.doesNotMatch(html, /像素粗筛 ≥|像素粗筛：|全部完整标准均/)
  assert.match(variantHtml(html, 0), /已调用模型/)
  assert.match(variantHtml(html, 0), /p_correct=95%/)
})

test('no images still displays all available indexed results and preserves text assessment', async () => {
  const html = await render({ decision: 'needs_review', reason: '混合题待复核',
    checks: [{ variant_index: 1, result: 'needs_review', p_correct: 80, reason: '模型证据不足', model_called: true }],
    pixel_gate: gate([{ variant_index: 1, status: 'pass', score: 83, reason: '像素通过' }]) },
  { result: 'partial', explanation: '计算部分有误' })
  assert.match(html, /文字／计算：partial · 计算部分有误/)
  assert.match(variantHtml(html, 1), /像素粗筛：83% · 通过/)
  assert.match(variantHtml(html, 1), /需复核.*p_correct=80%/)
  assert.doesNotMatch(html, /<img/)
})

test('image and reason rendering remains safe and invalid percentages do not look valid', async () => {
  const html = await render({ decision: 'wrong', reason: '<script>alert(1)</script>',
    pixel_gate: gate([{ variant_index: 0, status: 'unavailable', score: '80', reason: '<b>bad</b>' }], NaN),
    checks: [{ variant_index: 0, result: 'needs_review', p_correct: 101, reason: 'invalid' }],
    evidence: { student_image: 'javascript:alert(1)', overlays: ['https://example.com/private.png'] } })
  assert.doesNotMatch(html, /<img|<script>|<b>/)
  assert.match(html, /&lt;script&gt;/)
  assert.match(html, /像素粗筛 ≥ 未知/)
  assert.match(variantHtml(html, 0), /像素粗筛：未知/)
  assert.match(variantHtml(html, 0), /p_correct=未知/)
  assert.match(variantHtml(html, 0), /模型调用状态未记录/)
})

test('percentages below either threshold never round up to the passing threshold', async () => {
  for (const [threshold, pixel, probability, displayed] of [
    [90, 79.99, 89.99, '89.99'], [90, 79.999, 89.999, '89.99'],
    [95, 79.99, 94.99, '94.99'], [95, 79.999, 94.999, '94.99'],
  ]) {
    const html = await render({ decision: 'needs_review', threshold, reason: '边界显示检查',
      pixel_gate: gate([
        { variant_index: 0, status: 'fail', score: pixel, reason: '像素不足80' },
        { variant_index: 1, status: 'pass', score: 80, reason: '像素通过' },
      ]), checks: [
        { variant_index: 0, result: 'pixel_rejected', p_correct: null, reason: '拦截', model_called: false },
        { variant_index: 1, result: 'correct', p_correct: probability, reason: '概率低于本记录门槛', model_called: true },
      ] })
    assert.match(variantHtml(html, 0), /像素粗筛：79.99% · 拦截/)
    assert.doesNotMatch(variantHtml(html, 0), /像素粗筛：80%/)
    assert.match(variantHtml(html, 1), /像素粗筛：80% · 通过/)
    assert.ok(variantHtml(html, 1).includes(`p_correct=${displayed}%`))
    assert.ok(!variantHtml(html, 1).includes(`p_correct=${threshold}%`))
    assert.ok(html.includes(`p_correct ≥ ${threshold}%`))
  }
})

test('new dual-gate record uses its 90 model threshold and unchanged 80 pixel threshold', async () => {
  const html = await render({ decision: 'correct', threshold: 90, reason: '按本记录两关通过',
    pixel_gate: gate([{ variant_index: 0, status: 'pass', score: 94.41, reason: '像素通过' }]),
    checks: [{ variant_index: 0, result: 'correct', p_correct: 92, reason: '模型一致', model_called: true }] })
  assert.match(html, /画图核对：达到自动通过标准/)
  assert.match(html, /像素粗筛 ≥ 80%，且模型判对、p_correct ≥ 90%/)
  assert.match(variantHtml(html, 0), /p_correct=92%/)
  assert.doesNotMatch(html, /p_correct ≥ 95%/)
})

test('historical recorded thresholds are displayed rather than rewritten as current 90', async () => {
  for (const pixelGate of [undefined, gate([{ variant_index: 0, status: 'pass', score: 94, reason: '旧粗筛通过' }])]) {
    const html = await render({ decision: 'needs_review', threshold: 95, reason: '历史95规则下未通过',
      pixel_gate: pixelGate, checks: [{ variant_index: 0, result: 'correct', p_correct: 92, reason: '旧模型结果', model_called: true }] })
    assert.match(html, /p_correct ≥ 95%/)
    assert.doesNotMatch(html, /p_correct ≥ 90%/)
    assert.match(html, /画图核对：待老师复核/)
  }
})

test('manual comparison explains human review without implying fixed pixel or confidence grading', async () => {
  const html = await render({ decision: 'needs_review', comparison_mode: 'manual',
    reason: '自选比例尺，保留人工核对', checks: [], evidence: { student_image: image, overlays: [image] } },
  { result: 'partial', explanation: '计算部分有误' })
  assert.match(html, /画图核对：待老师复核/)
  assert.match(html, /自定比例作图：按题干核对学生原图/)
  assert.match(html, /不与固定参考坐标比较/)
  assert.match(html, /文字／计算：partial · 计算部分有误/)
  assert.match(variantHtml(html, 0), /参考图 1 · 仅供老师核对/)
  assert.doesNotMatch(html, /原自动通过标准|本记录无像素粗筛结果|自动通过需|像素粗筛：|p_correct=|模型调用状态未记录/)
})

test('manual mode does not present old automated candidate results as current grading evidence', async () => {
  const html = await render({ decision: 'needs_review', comparison_mode: 'manual', reason: '改为人工核对',
    pixel_gate: gate([{ variant_index: 0, status: 'pass', score: 100, reason: '旧粗筛' }]),
    checks: [{ variant_index: 0, result: 'correct', p_correct: 99, reason: '旧模型', model_called: true }],
    evidence: { overlays: [image] } })
  assert.match(html, /自定比例作图/)
  assert.doesNotMatch(html, /原自动通过标准|自动通过需|像素粗筛：|p_correct=|已调用模型|旧粗筛|旧模型/)
})
