import test from 'node:test'
import assert from 'node:assert/strict'

import {
  cleanExerciseText,
  normalizeLatexSource,
  renderAnswerLatex,
  renderLatex,
} from '../src/utils/latex.js'

test('exercise preview recovers flattened table cells and decodes entities', () => {
  const text = cleanExerciseText(
    '**17.&#x20;**题干\n\n'
      + '|   |\n| - |\n**—**\n'
      + '|   |\n| - |\n**一级品**\n'
      + '|   |\n| - |\n**二级品**\n'
      + '|   |\n| - |\n**合计**',
  )

  assert.match(text, /17\. 题干/)
  assert.match(text, /— \| 一级品 \| 二级品 \| 合计/)
  assert.doesNotMatch(text, /\| - \||\*\*/)
})

test('exercise preview removes damaged latex table syntax without losing values', () => {
  const text = cleanExerciseText(
    '附：\n(|l|l|l|l|)\nhline & 0.050 & 0.010 & 0.001 \\\\\\\\ hline',
  )

  assert.match(text, /0\.050 \| 0\.010 \| 0\.001/)
  assert.doesNotMatch(text, /\(\|l\|l\|l\|l\|\)|hline/)
})

test('math rendering removes empty root degrees and repairs safe escape damage', () => {
  const source = String.raw`$\sqrt [ ] {30}$，\(2\sqrt[]{5}\)，\[\sqrt[3]{8}\]`
  const normalized = normalizeLatexSource(source)

  assert.doesNotMatch(normalized, /\\sqrt\s*\[\s*\]/)
  const html = renderLatex(normalized)
  assert.doesNotMatch(html, /katex-error|\\sqrt\s*\[\s*\]/)
  assert.equal((html.match(/<msqrt>/g) || []).length, 2)
  assert.equal((html.match(/<mroot>/g) || []).length, 1)

  const brokenFraction = `$${String.fromCharCode(12)}rac{1}{2}$`
  const repairedHtml = renderLatex(brokenFraction)
  assert.match(repairedHtml, /<mfrac>/)
  assert.doesNotMatch(repairedHtml, /katex-error/)
})

test('renders bare OCR formula answers without changing plain text', () => {
  const formula = renderAnswerLatex('4\\sqrt{5}')
  assert.match(formula, /katex/)
  assert.match(formula, /<msqrt>/)
  assert.equal(renderAnswerLatex('未识别到作答'), '未识别到作答')
})
