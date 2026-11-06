import './helpers/srcModules.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { COLOR_MODES, getColorMode, initTheme, setColorMode } from '../src/composables/useTheme.js'
import { loadApiClient } from './helpers/apiClient.js'
import { memoryStorage } from './helpers/browser.js'
import { mountComponent } from './helpers/mountComponent.js'

const { default: Profile } = await import('../src/views/Profile.vue?sfc')
const globalCss = fs.readFileSync(new URL('../src/styles/global.css', import.meta.url), 'utf8')
const login = fs.readFileSync(new URL('../src/views/Login.vue', import.meta.url), 'utf8')
const index = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8')

// useTheme touches only localStorage and document.documentElement's
// setAttribute/style, so the test provides just those two globals.
function installThemeGlobals(entries = {}) {
  const previous = { localStorage: globalThis.localStorage, document: globalThis.document }
  const attributes = {}
  const root = { attributes, style: {}, setAttribute(name, value) { attributes[name] = String(value) } }
  const define = (name, value) => Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  define('localStorage', memoryStorage(entries))
  define('document', { documentElement: root })
  const restore = () => {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else define(name, value)
    }
  }
  return { root, storage: globalThis.localStorage, restore }
}

test('明暗模式支持浅色、深色和跟随系统并持久化', () => {
  assert.deepEqual(COLOR_MODES, ['light', 'dark', 'system'])
  const env = installThemeGlobals()
  try {
    for (const mode of ['light', 'dark', 'system']) {
      assert.equal(setColorMode(mode), mode)
      assert.equal(env.storage.getItem('mathagent_color_mode'), mode)
      assert.equal(getColorMode(), mode)
      assert.equal(env.root.attributes['data-color-mode'], mode)
      assert.equal(env.root.style.colorScheme, mode === 'system' ? 'light dark' : mode)
    }
    assert.equal(setColorMode('sepia'), 'light', 'only the three modes are accepted')
    assert.equal(env.storage.getItem('mathagent_color_mode'), 'light')
  } finally {
    env.restore()
  }

  for (const [stored, colorScheme] of [['dark', 'dark'], ['system', 'light dark']]) {
    const boot = installThemeGlobals({ mathagent_color_mode: stored })
    try {
      assert.equal(getColorMode(), stored)
      initTheme()
      assert.equal(boot.root.attributes['data-color-mode'], stored, 'boot applies the persisted mode')
      assert.equal(boot.root.style.colorScheme, colorScheme)
    } finally {
      boot.restore()
    }
  }
})

test('个人资料页提供三种明暗模式选择，并支持键盘切换', async (t) => {
  const teacher = { id: 1, role: 'teacher', name: '王老师', education_stage: 'junior', profile_complete: true }
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(teacher) },
  })
  const page = await mountComponent(t, Profile, { path: '/profile' })
  const heading = page.root.byClass('color-mode-heading')
  assert.equal(heading.byTag('strong')[0].text, '明暗模式')
  assert.equal(heading.byTag('small')[0].text, '跟随系统会随操作系统的深浅色设置自动变化')

  const group = page.root.find(el => el.getAttribute('role') === 'radiogroup' && el.getAttribute('aria-label') === '明暗模式')
  const radios = () => group.findAll(el => el.getAttribute('role') === 'radio')
  const choices = () => radios().map(radio => [radio.byTag('strong')[0].text, radio.props['aria-checked'], radio.getAttribute('tabindex')])
  // The page shows the saved mode (none yet: light), focusable as one radio group.
  assert.deepEqual(choices(), [['浅色', true, '0'], ['深色', false, '-1'], ['跟随系统', false, '-1']])

  const shownMode = () => ({
    saved: client.localStorage.getItem('mathagent_color_mode'),
    applied: document.documentElement.getAttribute('data-color-mode'),
    checked: radios().filter(radio => radio.props['aria-checked'] === true).map(radio => radio.byTag('strong')[0].text),
  })
  await radios()[1].dispatch('click')
  await page.flush()
  assert.deepEqual(shownMode(), { saved: 'dark', applied: 'dark', checked: ['深色'] })
  assert.deepEqual(choices().map(([, , tabindex]) => tabindex), ['-1', '0', '-1'])

  // Arrow keys move to the next / previous mode and wrap around; Home and End
  // jump to the first and last. The page keeps those keys from scrolling.
  const press = async (index, key) => {
    let prevented = false
    await radios()[index].dispatch('keydown', { key, preventDefault() { prevented = true } })
    await page.flush()
    return prevented
  }
  for (const [index, key, mode, label] of [
    [1, 'ArrowRight', 'system', '跟随系统'],
    [2, 'ArrowDown', 'light', '浅色'],
    [0, 'ArrowLeft', 'system', '跟随系统'],
    [2, 'ArrowUp', 'dark', '深色'],
    [1, 'Home', 'light', '浅色'],
    [0, 'End', 'system', '跟随系统'],
  ]) {
    assert.equal(await press(index, key), true, key)
    assert.deepEqual(shownMode(), { saved: mode, applied: mode, checked: [label] }, key)
  }
  // Other keys leave the choice alone.
  assert.equal(await press(2, 'Enter'), false)
  assert.deepEqual(shownMode(), { saved: 'system', applied: 'system', checked: ['跟随系统'] })
})

test('深色与跟随系统共用暖炭色表面、植物绿和可读文字', () => {
  assert.match(globalCss, /:root\[data-color-mode='dark'\]/)
  const explicit = globalCss.match(/:root\[data-color-mode='dark'\] \{([^}]+)\}/)[1]
  const system = globalCss.match(/@media \(prefers-color-scheme: dark\) \{\s*:root\[data-color-mode='system'\] \{([^}]+)\}/)[1]
  const declarations = block => Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(([,key,value]) => [key,value.trim()]))
  assert.deepEqual(declarations(system), declarations(explicit), 'OS dark mode has exactly the explicit dark tokens')
  assert.equal(declarations(explicit)['--paper'], '#20231f')
  assert.equal(declarations(explicit)['--primary'], '#9bb393')
  assert.equal(declarations(explicit)['--on-primary'], '#20231f')
  assert.doesNotMatch(explicit, /#000000|#42c98a/)
  assert.match(globalCss, /--el-color-primary: var\(--primary\)/)
  assert.match(globalCss, /@media \(prefers-color-scheme: dark\)/)
  assert.match(login, /登录页始终保持浅色/)
  assert.match(login, /background: #eef1f0; color: #1c1c1e/)
  assert.match(login, /--page: #eef1f0; --card: #ffffff/)
  assert.doesNotMatch(login, /html\[data-color-mode='dark'\]/)
})

test('启动页在主应用脚本加载前也能读取明暗模式', () => {
  assert.match(index, /localStorage\.getItem\('mathagent_color_mode'\)/)
  assert.match(index, /document\.documentElement\.dataset\.colorMode = mode/)
  assert.match(index, /document\.documentElement\.style\.colorScheme/)
  assert.match(index, /:root\[data-color-mode='dark'\] \{ --startup-paper: #20231f/)
  assert.match(index, /:root\[data-color-mode='system'\] \{ --startup-paper: #20231f/)
  assert.match(index, /location\.pathname === '\/login'/)
  assert.match(index, /data-startup-login='true'\]:has\(#mathagent-startup\)/)
  assert.doesNotMatch(index, /radial-gradient|linear-gradient/)
})
