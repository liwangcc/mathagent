import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { openTemplateEditor } from './helpers/templateHierarchyEditor.js'

// Slots saved by ID only (slot_ids, no region_kinds) in the real template
// editor: each round opens what was saved last time, shows it, confirms it.
for (const question_type of ['fill', 'solve']) test(`${question_type}: ID-only saved slots preserve multiline values across reopen`, async (t) => {
  const original = { question_no: '7', directory_node_id: 'n007', question_type, slot_ids: ['7[2]', '7[5]'], blank_count: 2, answer_slots: ['a\nb', 'c'], standard_answer: 'a; b; c', question_text: '按序作答' }
  let current = structuredClone(original)
  for (let round = 0; round < 2; round++) {
    const editor = await openTemplateEditor(t, { pages: [[current]] })
    const slots = editor.field('7', '各空标准答案').findAll(el => el.classList.contains('tpl-fill-slot'))
    assert.deepEqual(slots.map(slot => slot.find(el => el.tagName === 'span').text), ['Q7[2]', 'Q7[5]'])
    assert.deepEqual(slots.map(slot => slot.find(el => el.tagName === 'input').value), ['a\nb', 'c'])
    assert.deepEqual(Array.from(editor.page.setup.templatePages[0].questions[0].answer_slots_text), ['a\nb', 'c'])
    // The slots score as they are: nothing holds the template back.
    assert.equal(editor.bankEnabled(), true, `round ${round}`)
    await editor.save()
    assert.equal(editor.confirms().length, 1, `round ${round}`)
    current = editor.sentQuestions(editor.confirms()[0])['7']
    assert.deepEqual(current.answer_slots, ['a\nb', 'c'])
    assert.deepEqual(current.slot_ids, ['7[2]', '7[5]'])
    editor.upload.unmount()
  }
})
