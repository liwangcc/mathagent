import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const apiSource = fs.readFileSync(
  new URL('../src/api/index.js', import.meta.url),
  'utf8',
)

test('blob JSON failures preserve the backend service detail for the page', () => {
  assert.match(apiSource, /responseData instanceof Blob/)
  assert.match(apiSource, /JSON\.parse\(await responseData\.text\(\)\)/)
  assert.match(apiSource, /err\.message = detail/)
})
