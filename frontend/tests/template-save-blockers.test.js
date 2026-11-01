// The editor names every problem a save would hit before the teacher saves:
// oversized boxes (a model box larger than a quarter of the page still opens
// in the editor and is flagged), standard figures outside their drawing areas,
// and every reason a choice question is unresolved.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  TEMPLATE_MAX_BOX_AREA, templateBoxSizeProblems, templateChoiceConflictReasons, templateReferenceLeavesRegions,
} from '../src/utils/templateReview.js'

test('boxes over a quarter of the page are named by slot and kind; normal boxes are not', () => {
  const answer = { question_number: '1', slot_id: '1[1]', bbox: [40, 60, 700, 520] }
  const drawing = { question_number: '2', bbox: [100, 600, 900, 980] }
  const normal = { question_number: '3', bbox: [100, 100, 300, 200] }
  const problems = templateBoxSizeProblems([
    { box: answer, kind: 'answer' }, { box: drawing, kind: 'drawing' }, { box: normal, kind: 'answer' },
  ], 2)
  assert.deepEqual(problems.map((item) => item.box), [answer, drawing, null])
  assert.match(problems[0].message, /^Q1\[1\] 的作答框超过整页的四分之一.*缩小到实际作答范围/)
  assert.match(problems[1].message, /^Q2 的画图区超过整页的四分之一.*缩小到实际作图范围/)
  assert.match(problems[2].message, /^第 2 页的作答框和画图区合计覆盖超过整页的六成/)
  // Exactly the server limit is accepted.
  assert.deepEqual(templateBoxSizeProblems([{ box: { question_number: '4', bbox: [0, 0, 500, TEMPLATE_MAX_BOX_AREA / 500] }, kind: 'answer' }]), [])
  // Pending positions without geometry are not sized.
  assert.deepEqual(templateBoxSizeProblems([{ box: { question_number: '5', bbox: [] }, kind: 'answer' }]), [])
})

test('a fixed standard figure is flagged only when its centre line leaves every drawing area', () => {
  const figure = (...strokes) => ({ comparison_mode: 'fixed', variants: [{ strokes }] })
  const regions = [[100, 100, 400, 400], [400, 100, 700, 400]]
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'polyline', points: [[150, 150], [650, 350]] }), regions), false,
    'a line across two touching areas stays inside their union')
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'point', point: [400, 400] }), regions), false, 'the edge counts as inside')
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'point', point: [750, 200] }), regions), true)
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'polyline', points: [[150, 150], [150, 450]] }), regions), true)
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'polygon', points: [[150, 150], [350, 150], [350, 350]] }), regions), false)
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'ellipse', center: [250, 250], radii: [100, 100], fill: false }), regions), false)
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'ellipse', center: [250, 250], radii: [200, 100], fill: true }), regions), true)
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'brush', points: [[690, 200], [720, 200]], width: 8 }), regions), true)
  // Only fixed figures are compared by position; nothing to compare without areas.
  assert.equal(templateReferenceLeavesRegions({ comparison_mode: 'manual', variants: [{ strokes: [{ kind: 'point', point: [900, 900] }] }] }, regions), false)
  assert.equal(templateReferenceLeavesRegions(figure({ kind: 'point', point: [900, 900] }), []), false)
  assert.equal(templateReferenceLeavesRegions(null, regions), false)
})

test('every reason a choice question is unresolved is listed together', () => {
  const question = { question_no: '3', question_type: 'choice', choice_mode: 'unknown', standard_answer: '', slot_ids: ['3[1]', '3[2]', '3[3]'], region_kinds: ['answer', 'answer', 'answer'] }
  const reasons = templateChoiceConflictReasons(question)
  assert.equal(reasons.length, 3)
  assert.match(reasons[0], /Q3 是选择题，但有 3 个作答框/)
  assert.match(reasons[1], /Q3 请在选择题答题卡中选择“单选”或“多选”/)
  assert.match(reasons[2], /Q3 请在选择题答题卡中点选标准答案/)
  // Mode chosen, answer already valid for it: only the extra boxes remain.
  assert.deepEqual(templateChoiceConflictReasons({ ...question, choice_mode: 'single', standard_answer: 'B' }).length, 1)
  assert.deepEqual(templateChoiceConflictReasons({ question_no: '4', question_type: 'choice', choice_mode: 'single', standard_answer: 'C' }), [])
  assert.deepEqual(templateChoiceConflictReasons({ question_no: '5', question_type: 'fill' }), [])
})
