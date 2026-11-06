import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { chooseValue, openTemplateEditor } from './helpers/templateHierarchyEditor.js'

// The confirmation gate as the teacher meets it in the real template editor:
// 写入私库 is offered only while the template can be confirmed, and 保存模板
// confirms it only then.

const statement = (editor, number) => editor.field(number, '题干').find(el => el.tagName === 'textarea')

test('actual confirmation gate accepts deliberately untranscribed choice text', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [[{ question_no: '3', question_type: 'choice', choice_mode: 'single', question_text: '', standard_answer: 'D' }]] })
  assert.equal(editor.bankEnabled(), true)
  await editor.save()
  assert.equal(editor.confirms().length, 1)
  assert.equal(editor.sentQuestions(editor.confirms()[0])['3'].question_text, '')
})

test('actual confirmation gate still requires non-choice text and all reference answers', async (t) => {
  for (const question_type of ['fill', 'solve']) {
    const editor = await openTemplateEditor(t, { pages: [[{ question_no: '2', question_type, question_text: '', standard_answer: '8' }]] })
    assert.equal(editor.bankEnabled(), false, question_type)
    await editor.save()
    assert.equal(editor.confirms().length, 0, question_type)
    assert.match(editor.messages('warning').join(' '), /第 2 题缺少题干/, question_type)
    await editor.upload.type(statement(editor, '2'), '求 x 的值')
    assert.equal(editor.bankEnabled(), true, `${question_type}: the statement was all it lacked`)
    editor.upload.unmount()
  }
  const editor = await openTemplateEditor(t, { pages: [[{ question_no: '3', question_type: 'choice', question_text: '', standard_answer: '' }]] })
  assert.equal(editor.bankEnabled(), false)
  // Answering it on the answer card opens the gate.
  const cell = editor.dialog().find(el => el.classList.contains('tpl-choice-answer-cell'))
  await editor.press(cell.byText('button', '单选'))
  await editor.press(cell.find(el => el.classList.contains('tpl-choice-option') && el.text === 'D'))
  assert.equal(editor.bankEnabled(), true)
})

test('pure drawing may save an empty standard but must retain the problem statement', async (t) => {
  for (const question_type of ['fill', 'solve', 'choice']) {
    const editor = await openTemplateEditor(t, { pages: [[{ question_no: '2', question_type, answer_kind: 'drawing', question_text: '按条件画图', standard_answer: '' }]] })
    assert.equal(editor.bankEnabled(), true, question_type)
    await editor.upload.type(statement(editor, '2'), '')
    assert.equal(editor.bankEnabled(), false, `${question_type}: without its statement`)
    await editor.upload.type(statement(editor, '2'), '按条件画图')
    assert.equal(editor.bankEnabled(), true, question_type)
    await chooseValue(editor.upload, editor.drawingKind('2'), 'mixed')
    assert.equal(editor.bankEnabled(), false, `${question_type}: a mixed question needs its standard answer`)
    editor.upload.unmount()
  }
})
