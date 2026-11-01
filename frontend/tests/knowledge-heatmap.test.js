import test from 'node:test'
import assert from 'node:assert/strict'
import { knowledgeTopicStudents } from '../src/utils/knowledgeHeatmap.js'

test('knowledge heatmap accepts current students and deduplicates legacy groups', () => {
  assert.deepEqual(knowledgeTopicStudents({ students: ['甲', '乙', '甲'] }), ['甲', '乙'])
  assert.deepEqual(knowledgeTopicStudents({ errors: [{ students: ['甲'] }, { students: ['乙', '甲'] }] }), ['甲', '乙'])
  assert.deepEqual(knowledgeTopicStudents({ students: [], errors: [{ students: ['旧'] }] }), [])
})

test('knowledge heatmap handles incomplete records without losing valid names', () => {
  assert.deepEqual(knowledgeTopicStudents(null), [])
  assert.deepEqual(knowledgeTopicStudents({ errors: [null, {}, { students: [' 甲 ', null, '', 12] }] }), ['甲'])
})
