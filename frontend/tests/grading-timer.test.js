import assert from 'node:assert/strict'
import test from 'node:test'

import { formatGradingDuration } from '../src/utils/gradingTimer.js'

test('grading durations read as m:ss, or h:mm:ss past an hour', () => {
  assert.equal(formatGradingDuration(0), '0:00')
  assert.equal(formatGradingDuration(42.9), '0:42')
  assert.equal(formatGradingDuration(125), '2:05')
  assert.equal(formatGradingDuration(3725), '1:02:05')
})

test('a missing or invalid duration shows a placeholder instead of NaN', () => {
  for (const value of [null, undefined, -1, 'soon', Number.NaN]) {
    assert.equal(formatGradingDuration(value), '--:--')
  }
})
