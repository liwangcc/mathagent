import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const frontendFile = (relativePath) =>
  new URL(`../${relativePath}`, import.meta.url)

test('desktop cold start grades a sample page on the startup plate before Vue mounts', async () => {
  const index = await readFile(frontendFile('index.html'), 'utf8')
  const main = await readFile(frontendFile('src/main.js'), 'utf8')

  assert.match(index, /id="mathagent-startup"/)
  assert.match(index, /role="status" aria-live="polite" aria-label="MathAgent 正在启动"/)
  // The showcase specimen in miniature: phases, a scanned page, marks and the review stamp.
  assert.match(index, /<ol class="startup-phases"><li>识别<\/li><li>判分<\/li><li>复核<\/li><\/ol>/)
  assert.match(index, /class="sheet-scan"/)
  assert.match(index, /class="sheet-mark is-correct m1" pathLength="1"/)
  assert.match(index, /<strong>已复核<\/strong><span>可入档<\/span>/)
  assert.match(index, /animation: startup-stamp \.5s cubic-bezier\(\.16, 1, \.3, 1\) 1\.6s both/)
  assert.match(index, /正在准备批阅环境/)
  // Reduced motion shows the finished, stamped page at rest.
  assert.match(index, /prefers-reduced-motion: reduce\) \{\s*#mathagent-startup \*, #mathagent-startup \*::before \{ animation: none !important; \}/)

  assert.match(main, /document\.getElementById\('mathagent-startup'\)/)
  assert.match(main, /const STARTUP_PLATE_MS = 2100/)
  assert.match(main, /Math\.max\(0, STARTUP_PLATE_MS - performance\.now\(\)\)/)
  assert.match(main, /startupScreen\.classList\.add\('is-leaving'\)/)
  assert.match(main, /window\.setTimeout\(\(\) => startupScreen\.remove\(\), 400\)/)
})
