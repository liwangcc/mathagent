import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const anonymousView = readFileSync(new URL('../src/views/AnonymousGrade.vue', import.meta.url), 'utf8')
const globalStyles = readFileSync(new URL('../src/styles/global.css', import.meta.url), 'utf8')

test('template column hint uses a full-width row in both grading modes', () => {
  const layoutRule = globalStyles.match(/\.upload-page \.template-card \.tpl-column-mode\s*\{([^}]*)\}/)?.[1] || ''
  const hintRule = globalStyles.match(/\.upload-page \.template-card \.tpl-column-mode-hint\s*\{([^}]*)\}/)?.[1] || ''

  assert.match(globalStyles, /@media\s*\(min-width:\s*641px\)/)
  assert.match(layoutRule, /grid-template-columns:\s*auto minmax\(0,\s*1fr\) auto/)
  assert.match(hintRule, /grid-column:\s*1\s*\/\s*-1/)
  assert.match(hintRule, /white-space:\s*normal/)
  assert.match(source, /anonymous:\s*\{\s*type:\s*Boolean,\s*default:\s*false\s*\}/)
  assert.match(anonymousView, /<Upload\s+anonymous\s*\/>/)
})

test('ordinary paper column help does not advertise answer-sheet grading', () => {
  const columnSection = source.slice(source.indexOf('<div class="tpl-column-mode">'), source.indexOf('<!-- 已保存：'))

  assert.match(columnSection, /id="template-column-mode-help"[^>]*>默认双栏。<\/span>/)
  assert.doesNotMatch(columnSection, /答题卡/)
  assert.match(columnSection, /<option value="triple">三栏<\/option>/)
})
