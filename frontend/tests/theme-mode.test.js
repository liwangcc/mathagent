import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const theme = fs.readFileSync(new URL('../src/composables/useTheme.js', import.meta.url), 'utf8')
const profile = fs.readFileSync(new URL('../src/views/Profile.vue', import.meta.url), 'utf8')
const globalCss = fs.readFileSync(new URL('../src/styles/global.css', import.meta.url), 'utf8')
const login = fs.readFileSync(new URL('../src/views/Login.vue', import.meta.url), 'utf8')
const index = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8')

test('明暗模式支持浅色、深色和跟随系统并持久化', () => {
  assert.match(theme, /COLOR_MODE_STORAGE_KEY = 'mathagent_color_mode'/)
  assert.match(theme, /const COLOR_MODES = \['light', 'dark', 'system'\]/)
  assert.match(theme, /export function getColorMode\(\)/)
  assert.match(theme, /export function setColorMode\(name\)/)
  assert.match(theme, /root\.setAttribute\('data-color-mode', mode\)/)
  assert.match(theme, /mode === 'system' \? 'light dark' : mode/)
  assert.match(theme, /applyColorMode\(getColorMode\(\)\)/)
})

test('个人资料页提供三种明暗模式选择，并支持键盘切换', () => {
  assert.match(profile, /明暗模式/)
  assert.match(profile, /跟随系统会随操作系统的深浅色设置自动变化/)
  assert.match(profile, /value: 'light', label: '浅色'/)
  assert.match(profile, /value: 'dark', label: '深色'/)
  assert.match(profile, /value: 'system', label: '跟随系统'/)
  assert.match(profile, /chooseColorMode\(item\.value\)/)
  assert.match(profile, /onColorModeKeydown\(\$event, item\.value\)/)
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
