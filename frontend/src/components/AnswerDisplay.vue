<template>
  <div class="answer-display">
    <div v-if="questionHtml" class="question-block" v-html="questionHtml" />
    <div class="answer-block" v-html="answerHtml" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { cleanExerciseText, renderLatexLines } from '@/utils/latex'

const props = defineProps({
  text: { type: String, default: '' },
  question: { type: String, default: '' },
})

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function processInline(raw) {
  if (!raw) return []
  return renderLatexLines(cleanExerciseText(raw))
}

function isFinalAnswerLine(line) {
  const t = line.trim()
  return /^(答案|结果|所以|故|∴|因此)[：:]/.test(t) || /^答[：:]/.test(t)
}

function renderLines(text) {
  const processed = processInline(text)
  return processed.map(line => {
    const cls = isFinalAnswerLine(line) ? 'step-line final-answer' : 'step-line'
    return `<div class="${cls}">${line}</div>`
  }).join('')
}

const questionHtml = computed(() => {
  const q = cleanExerciseText(props.question || '').trim()
  if (!q) return ''
  const lines = processInline(q)
  return lines.join('<br>')
})

function stripMathForSubQ(text) {
  // Remove $$...$$, $...$, \[...\], \(...\) so (N) inside math won't match
  return text
    .replace(/\$\$[\s\S]+?\$\$/g, '')
    .replace(/\$[^\$\n]+?\$/g, '')
    .replace(/\\\[[\s\S]+?\\\]/g, '')
    .replace(/\\\([\s\S]+?\\\)/g, '')
}

function findSubQuestions(raw) {
  // 捕获组1=纯标号数字；前缀(行首/换行/冒号)仅用于判定语境，不计入 label
  const subQPattern = /(?:^|[\r\n]|[：:]\s*)([（(]\s*[1-9]\s*[）)])\s*/g
  const stripped = stripMathForSubQ(raw)
  const matches = [...stripped.matchAll(subQPattern)]
  if (matches.length < 2) return null
  const nums = matches.map(m => parseInt(m[1].replace(/[^\d]/g, '')))
  if (nums[0] !== 1) return null
  for (let i = 1; i < nums.length; i++) {
    if (nums[i] <= nums[i - 1]) return null
  }
  // 在 raw 中搜索每个纯标号 (N)，避免 stripped/raw 位置偏移；
  // label 只存纯括号标号，绝不含冒号/换行等前缀。
  const result = []
  let searchFrom = 0
  for (let i = 0; i < matches.length; i++) {
    const label = matches[i][1].trim()   // 纯 "(N)"，不含前缀
    const idx = raw.indexOf(label, searchFrom)
    if (idx < 0) return null
    result.push({ label, start: idx, end: raw.length })
    searchFrom = idx + label.length
  }
  // 设置每个子问题的结束位置 = 下一个的开始
  for (let i = 0; i < result.length - 1; i++) {
    result[i].end = result[i + 1].start
  }
  return result
}

const answerHtml = computed(() => {
  const raw = cleanExerciseText(props.text || '')
  if (!raw) return ''

  const subQs = findSubQuestions(raw)

  if (subQs && subQs.length >= 2) {
    let html = '<div class="sub-questions">'
    for (let i = 0; i < subQs.length; i++) {
      const chunk = raw.slice(subQs[i].start, subQs[i].end).trim()
      const body = chunk.replace(/^[（(]\s*\d+\s*[）)]\s*/, '').trim()
      html += `<div class="sub-q">`
      html += `<div class="sub-q-label">${escapeHtml(subQs[i].label)}</div>`
      html += `<div class="sub-q-body">${renderLines(body)}</div>`
      html += `</div>`
    }
    html += '</div>'
    return html
  }

  return `<div class="single-q">${renderLines(raw)}</div>`
})
</script>

<style scoped>
.answer-display {
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.6;
  color: var(--ink-soft);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  padding: 14px 16px;
}

.answer-display .question-block {
  font-size: 14px;
  line-height: 1.5;
  color: var(--ink);
  padding-bottom: 10px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
}

.answer-display .answer-block {
  font-size: 13.5px;
}

/* A question shown on its own should not draw a divider over an empty answer. */
.answer-display .answer-block:empty {
  display: none;
}

.answer-display .question-block:has(+ .answer-block:empty) {
  margin-bottom: 0;
  padding-bottom: 0;
  border-bottom: 0;
}

.answer-display .sub-questions {
  display: flex;
  gap: 28px;
  align-items: flex-start;
}

.answer-display .sub-q {
  flex: 1;
  min-width: 0;
}

.answer-display .sub-q + .sub-q {
  padding-left: 28px;
  border-left: 1px solid var(--border);
}

.answer-display .sub-q-label {
  font-weight: 700;
  font-size: 14px;
  color: var(--primary-dark);
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}

.answer-display .sub-q-body,
.answer-display .single-q {
  line-height: 1.35;
}

.answer-display .step-line {
  margin: 0;
  padding: 1px 0;
  line-height: 1.35;
  white-space: pre-wrap;
  word-break: break-word;
}

.answer-display .final-answer {
  font-weight: 700;
  color: var(--primary-dark);
  margin-top: 4px;
  padding: 3px 8px;
  border-radius: var(--radius-xs);
  background: var(--primary-soft);
  display: inline-block;
}

.answer-display :deep(.katex) {
  font-size: 1.02em;
  line-height: 1.2;
}

.answer-display :deep(.katex-display) {
  margin: 4px 0;
  text-align: left;
}

.answer-display :deep(.katex-error) {
  color: var(--ink-soft);
  font-style: italic;
}

@media (max-width: 640px) {
  .answer-display .sub-questions {
    flex-direction: column;
    gap: 16px;
  }

  .answer-display .sub-q + .sub-q {
    width: 100%;
    padding-top: 16px;
    padding-left: 0;
    border-top: 1px solid var(--border);
    border-left: 0;
  }
}
</style>
