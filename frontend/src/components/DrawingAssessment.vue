<script setup>
import { computed } from 'vue'

const props = defineProps({ assessment: { type: Object, required: true }, textAssessment: { type: Object, default: null } })
const isManual = computed(() => props.assessment.comparison_mode === 'manual')
const hasPixelGate = computed(() => !isManual.value && props.assessment.pixel_gate?.version === 'drawing-pixel-coarse-v1')
const modelThreshold = computed(() => props.assessment.threshold ?? 95)
const decisionLabel = computed(() => ({ correct: '达到自动通过标准', wrong: '判错', needs_review: '待老师复核' })[props.assessment.decision] || '待老师复核')
const candidates = computed(() => {
  const rows = new Map()
  const rowFor = (index) => {
    if (!rows.has(index)) rows.set(index, { index })
    return rows.get(index)
  }
  for (const [index, overlay] of (props.assessment.evidence?.overlays || []).entries()) rowFor(index).overlay = overlay
  for (const check of props.assessment.checks || []) {
    if (Number.isInteger(check?.variant_index) && check.variant_index >= 0) rowFor(check.variant_index).check = check
  }
  if (hasPixelGate.value) {
    for (const gate of props.assessment.pixel_gate.variants || []) {
      if (Number.isInteger(gate?.variant_index) && gate.variant_index >= 0) rowFor(gate.variant_index).gate = gate
    }
  }
  return [...rows.values()].sort((a, b) => a.index - b.index)
})

function percentage(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) return '未知'
  // Truncate decimal digits so a value below either threshold never appears to pass.
  // String slicing also avoids 79.99 * 100 floating-point underflow to 7998.999.
  const [whole, fraction = ''] = String(value).split('.')
  return `${value < 0.01 ? 0 : Number(`${whole}.${fraction.slice(0, 2)}`)}%`
}
function gateLabel(value) {
  return ({ pass: '通过', fail: '拦截', unavailable: '不可用，需复核' })[value] || '未记录'
}
function checkLabel(value) {
  return ({ correct: '一致', wrong: '不一致', incorrect: '不一致', needs_review: '需复核', pixel_rejected: '像素粗筛拦截' })[value] || value || '未记录'
}
function modelLabel(check) {
  if (check?.model_called === false) return '未调用模型'
  if (check?.model_called === true || check?.model) return '已调用模型'
  return '模型调用状态未记录'
}
function safeImage(value) {
  return typeof value === 'string' && value.length < 5600000 && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(value) ? value : ''
}
</script>

<template>
  <section class="drawing-assessment" :data-decision="assessment.decision" @click.stop>
    <strong>画图核对：{{ decisionLabel }}</strong>
    <p>{{ assessment.reason }}</p>
    <p v-if="isManual">自定比例作图：按题干核对学生原图，不与固定参考坐标比较。</p>
    <template v-else-if="hasPixelGate">
      <p>自动通过需同一个完整标准的像素粗筛 ≥ {{ percentage(assessment.pixel_gate.threshold) }}，且模型判对、p_correct ≥ {{ percentage(modelThreshold) }}。</p>
      <p>全部完整标准均被像素粗筛拦截时直接判错；检测异常仍待老师复核。像素粗筛不统计白底；p_correct 不是像素重合率或已校准准确率。</p>
    </template>
    <p v-else>本记录无像素粗筛结果。原自动通过标准为模型判对且 p_correct ≥ {{ percentage(modelThreshold) }}；此数值不是像素重合率或已校准准确率。</p>
    <p v-if="textAssessment">文字／计算：{{ textAssessment.result }} · {{ textAssessment.explanation }}</p>
    <details>
      <summary>查看实际核对图与分项结果</summary>
      <figure v-if="safeImage(assessment.evidence?.student_image)">
        <figcaption>实际学生图（未叠加标准）</figcaption>
        <img :src="safeImage(assessment.evidence.student_image)" alt="实际学生作图证据" />
      </figure>
      <figure v-for="candidate in candidates" :key="candidate.index" :data-variant-index="candidate.index">
        <figcaption v-if="isManual">参考图 {{ candidate.index + 1 }} · 仅供老师核对</figcaption>
        <figcaption v-else>完整标准 {{ candidate.index + 1 }} · 半透明青蓝参考层</figcaption>
        <img v-if="safeImage(candidate.overlay)" :src="safeImage(candidate.overlay)" :alt="`标准 ${candidate.index + 1} 与学生作图叠加`" />
        <template v-if="!isManual">
          <p v-if="candidate.gate">像素粗筛：{{ percentage(candidate.gate.score) }} · {{ gateLabel(candidate.gate.status) }} · {{ candidate.gate.reason }}</p>
          <p v-else-if="hasPixelGate">像素粗筛：未记录</p>
          <p>{{ modelLabel(candidate.check) }}</p>
          <p v-if="candidate.check">{{ checkLabel(candidate.check.result) }}<template v-if="candidate.check.model_called !== false"> · p_correct={{ percentage(candidate.check.p_correct) }}</template> · {{ candidate.check.reason }}</p>
        </template>
      </figure>
    </details>
  </section>
</template>

<style scoped>
.drawing-assessment { background: var(--surface-subtle); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin: 10px 0; }
.drawing-assessment[data-decision="wrong"] { background: var(--danger-soft); border-color: var(--danger); }
p { margin: 6px 0; font-size: 13px; }
figure { margin: 12px 0; }
img { max-width: 100%; max-height: 640px; object-fit: contain; background: white; }
summary { cursor: pointer; }
</style>
