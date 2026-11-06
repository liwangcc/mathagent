import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createPinia } from 'pinia'
import { loadApiClient } from './helpers/apiClient.js'
import { mountComponent } from './helpers/mountComponent.js'
import { formFields, HEALTHY_SERVICE, mountUpload, paperFile, TEACHER } from './helpers/uploadPage.js'

const { default: AnonymousGrade } = await import('../src/views/AnonymousGrade.vue?sfc')
const { useAuthStore } = await import('../src/stores/auth.js')

// The stylesheet is checked as text: grid placement has no observable outcome
// without a browser's layout engine.
const globalStyles = readFileSync(new URL('../src/styles/global.css', import.meta.url), 'utf8')

const TEMPLATE_RECOGNIZE = 'POST /api/batch-grade/template/recognize'

// 无身份批阅 as the router shows it (frontend/src/views/AnonymousGrade.vue),
// for the same signed-in teacher and healthy service as helpers/uploadPage.js.
async function mountAnonymousGrade(t) {
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER) },
  })
  client.respond((config) => {
    const answer = HEALTHY_SERVICE[`${String(config.method).toUpperCase()} /api${config.url.split('?')[0]}`]
    return { status: 200, data: answer ?? {} }
  })
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.currentClassId = TEACHER.current_class_id
  auth.classes = [{ id: TEACHER.current_class_id, class_name: '班级 1' }]
  return mountComponent(t, AnonymousGrade, { path: '/anonymous-grade', pinia })
}

// The column hint inside the elements the stylesheet rule below targets
// (.upload-page .template-card .tpl-column-mode .tpl-column-mode-hint).
function columnHint(root) {
  const page = root.byClass('upload-page')
  const field = page?.byClass('template-card')?.byClass('tpl-column-mode')
  return field?.byClass('tpl-column-mode-hint') ?? null
}

test('template column hint uses a full-width row in both grading modes', async (t) => {
  const layoutRule = globalStyles.match(/\.upload-page \.template-card \.tpl-column-mode\s*\{([^}]*)\}/)?.[1] || ''
  const hintRule = globalStyles.match(/\.upload-page \.template-card \.tpl-column-mode-hint\s*\{([^}]*)\}/)?.[1] || ''

  assert.match(globalStyles, /@media\s*\(min-width:\s*641px\)/)
  assert.match(layoutRule, /grid-template-columns:\s*auto minmax\(0,\s*1fr\) auto/)
  assert.match(hintRule, /grid-column:\s*1\s*\/\s*-1/)
  assert.match(hintRule, /white-space:\s*normal/)

  // Both grading modes are the one Upload page, so the rule lays out the same
  // hint in each: 详细批改 (Upload, not anonymous unless asked) ...
  const ordinary = await mountUpload(t)
  assert.match(ordinary.page.root.byClass('hero-band__eyebrow').text, /^详细批改/)
  assert.equal(columnHint(ordinary.page.root)?.text, '默认双栏。')
  ordinary.unmount()

  // ... and 无身份批阅, which renders Upload in anonymous mode.
  const anonymous = await mountAnonymousGrade(t)
  assert.match(anonymous.root.byClass('hero-band__eyebrow').text, /^无身份批阅/)
  assert.match(anonymous.root.byClass('grade-card').byClass('card-title').text, /无身份试卷页面/)
  assert.equal(columnHint(anonymous.root)?.text, '默认双栏。')
  assert.deepEqual(anonymous.errors, [])
})

test('ordinary paper column help does not advertise answer-sheet grading', async (t) => {
  const upload = await mountUpload(t, { timers: true, streams: { [TEMPLATE_RECOGNIZE]: [['progress', { message: '正在识别模板第 1 页' }]] } })
  const field = upload.page.root.byClass('template-card').byClass('tpl-column-mode')
  const select = field.byTag('select')[0]
  const help = field.find(el => el.id === 'template-column-mode-help')

  assert.equal(select.id, 'template-column-mode')
  assert.equal(select.getAttribute('aria-describedby'), 'template-column-mode-help')
  assert.equal(help.text, '默认双栏。')
  assert.deepEqual(select.options.map(option => [option._value, option.text]), [['single', '单栏'], ['double', '双栏'], ['triple', '三栏']])
  assert.equal(select.options.find(option => option.selected)._value, 'double')
  assert.doesNotMatch(field.text, /答题卡/)

  // 三栏 is a real choice: the template import is recognized as three columns.
  await upload.select(select, 'triple')
  const input = upload.page.root.byClass('template-card').find(el => el.tagName === 'input' && el.type === 'file')
  input.files = [paperFile('template-1.png')]
  await input.dispatch('change')
  await upload.settle()
  await upload.click('识别模板')
  const [sent] = upload.fetches(TEMPLATE_RECOGNIZE)
  assert.equal(formFields(sent.init.body).column_mode, 'triple')
  assert.equal(formFields(sent.init.body).layout_mode, 'triple')
})
