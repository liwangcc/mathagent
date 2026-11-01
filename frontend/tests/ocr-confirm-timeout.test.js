import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const apiSource = fs.readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8')

test('orientation OCR retry outlives the full server phase budget', () => {
  assert.match(apiSource, /const OCR_CONFIRM_TIMEOUT_MS = 600000/)
  assert.match(
    apiSource,
    /orientation-confirm`,\s*formData,\s*\{ timeout: OCR_CONFIRM_TIMEOUT_MS \}/s,
  )
})
