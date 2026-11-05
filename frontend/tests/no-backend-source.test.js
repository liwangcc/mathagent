import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import test from 'node:test'

// Frontend tests verify the frontend. A backend contract is tested in tests/
// against behaviour; matching Python source text here would make a pure
// backend refactor fail the frontend gate.
const BACKEND_SOURCE = /['"`][^'"`\n]*(?:\.\.\/)+(?:routers|services|worker|tests)\/|['"`][^'"`\n]*\.py['"`]/

test('frontend tests do not read backend source files', () => {
  const here = new URL('./', import.meta.url)
  const offenders = readdirSync(here, { recursive: true })
    .filter((name) => /\.m?js$/.test(name) && !name.endsWith('no-backend-source.test.js'))
    .filter((name) => BACKEND_SOURCE.test(readFileSync(new URL(name.replaceAll('\\', '/'), here), 'utf8')))
  assert.deepEqual(offenders, [])
})
