import assert from 'node:assert/strict'
import test from 'node:test'

import { compareStudentNumbers } from '../src/utils/studentSort.js'

test('学号按自然数顺序排序，不把 11 排在 2 前面', () => {
  const values = ['11', '2', '1', '10', '003']

  values.sort(compareStudentNumbers)

  assert.deepEqual(values, ['1', '2', '003', '10', '11'])
})

test('学号排序保留字母数字编号的自然顺序', () => {
  const values = ['A11', 'A2', 'A1']

  values.sort(compareStudentNumbers)

  assert.deepEqual(values, ['A1', 'A2', 'A11'])
})