import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  CLASSIFICATION_LABELS, catalogOptions, classificationState, hasKnowledge,
} from '../src/utils/knowledgeClassification.js'

const bank = readFileSync(new URL('../src/views/Bank.vue', import.meta.url), 'utf8')
const pending = readFileSync(new URL('../src/views/PendingAnswers.vue', import.meta.url), 'utf8')

test('placeholder labels are not knowledge points', () => {
  assert.equal(hasKnowledge({ chapter: '函数', topic: '定义域' }), true)
  for (const topic of ['', '未分类', '未知', '综合']) {
    assert.equal(hasKnowledge({ chapter: '函数', topic }), false)
  }
})

test('missing labels read as in progress or failed, never 未分类', () => {
  assert.equal(classificationState({ chapter: '函数', topic: '定义域', classification_status: 'manual' }), 'classified')
  assert.equal(classificationState({ classification_status: 'pending' }), 'pending')
  assert.equal(classificationState({ classification_status: '' }), 'pending')
  for (const status of ['manual', 'failed', 'confirmed', 'suggested']) {
    assert.equal(classificationState({ topic: '未分类', classification_status: status }), 'failed')
  }
  assert.equal(CLASSIFICATION_LABELS.failed, '分类失败')
  assert.doesNotMatch(JSON.stringify(CLASSIFICATION_LABELS), /未分类/)
})

test('catalog becomes a two-level chapter/topic picker', () => {
  assert.deepEqual(catalogOptions([{ chapter: '函数', topics: ['定义域', '值域'] }, { chapter: '' }]), [{
    value: '函数', label: '函数',
    children: [{ value: '定义域', label: '定义域' }, { value: '值域', label: '值域' }],
  }])
  assert.deepEqual(catalogOptions(null), [])
})

test('bank and pending pages offer a manual pick when classification is missing', () => {
  assert.match(bank, /CLASSIFICATION_LABELS\[classificationState\(row\)\]/)
  assert.match(bank, /api\.bank\.update\(row\.id, \{ chapter: value\[0\], topic: value\[1\] \}\)/)
  assert.match(pending, /CLASSIFICATION_LABELS\.failed/)
  assert.match(pending, /\.\.\.knowledgePayload\(row\)/)
  assert.match(pending, /\.\.\.knowledgePayload\(r\)/)
})
