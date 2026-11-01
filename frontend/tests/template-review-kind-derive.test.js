// The teacher's per-region marks are the single source of truth for the
// answer kind (incident a5d95c2a: a stale model answer_kind blocked a teacher
// correction "因为跟模型的错误层级不匹配"). serializeTemplateDrawingReview must
// re-derive the kind from region_kinds exactly like updateSlotStructure and
// the server contract (R4).
import test from 'node:test'
import assert from 'node:assert/strict'
import { serializeTemplateDrawingReview } from '../src/utils/templateReview.js'

test('stale model answer_kind re-derives from the teacher region marks', () => {
  const schema = [{
    question_number: '1',
    answer_kind: 'drawing', // stale model kind
    region_kinds: ['answer'], // the teacher's fix
    drawing_regions: [[0, 0, 10, 10]],
    label_anchor: [150, 205],
  }]
  const [row] = serializeTemplateDrawingReview(schema, [], [])
  assert.equal(row.answer_kind, 'non_graphic')
  assert.deepEqual(row.drawing_regions, [])
  assert.equal(row.label_anchor, null)
})

test('drawing region marks keep the drawing kind and its geometry', () => {
  const schema = [{
    question_number: '1',
    answer_kind: 'non_graphic',
    region_kinds: ['drawing'],
    drawing_regions: [[10, 0, 20, 10]],
    label_anchor: [1, 2],
  }]
  const [row] = serializeTemplateDrawingReview(
    schema,
    [{ question_number: '1', bbox: [10, 0, 20, 10] }],
    [{ question_number: '1', label_anchor: [1, 2] }],
  )
  assert.equal(row.answer_kind, 'drawing')
  assert.deepEqual(row.drawing_regions, [[10, 0, 20, 10]])
  assert.deepEqual(row.label_anchor, [1, 2])
})

test('rows without region marks keep their explicit answer_kind', () => {
  const schema = [{
    question_number: '1',
    answer_kind: 'drawing',
    drawing_regions: [[0, 0, 10, 10]],
  }]
  const [row] = serializeTemplateDrawingReview(
    schema,
    [{ question_number: '1', bbox: [0, 0, 10, 10] }],
    [],
  )
  assert.equal(row.answer_kind, 'drawing')
  assert.deepEqual(row.drawing_regions, [[0, 0, 10, 10]])
})
