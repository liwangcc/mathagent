import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import * as review from '../src/utils/templateReview.js'
import * as slots from '../src/utils/templateSlots.js'
const source=readFileSync(new URL('../src/views/Upload.vue',import.meta.url),'utf8')
const names=['normalizeAnswerSpecForEditor','answerSpecFields','questionScoringValid','defaultGradingMethod','toTextLines','toStringList','inferSubquestionCount','inferBlankCount','defaultSubquestionScores','equalBlankScores','normalizedBlankScores']
const functions=names.map(name=>{const m=source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`));assert.ok(m,name);return m[0]}).join('\n')
const deps={...review,...slots,form:{category:'daily'}}
const api=new Function(...Object.keys(deps),functions+`\nreturn {${names.join(',')}}`)(...Object.values(deps))
for(const question_type of ['fill','solve'])test(`${question_type}: ID-only saved slots preserve multiline values across reopen`,()=>{
 const original={question_no:'7',directory_node_id:'n007',question_type,slot_ids:['7[2]','7[5]'],blank_count:2,answer_slots:['a\nb','c'],standard_answer:'a; b; c'}
 let current=structuredClone(original)
 for(let round=0;round<2;round++){
  const normalized=api.normalizeAnswerSpecForEditor(current)
  assert.deepEqual(normalized.answer_slots_text,['a\nb','c'])
  assert.equal(api.questionScoringValid(normalized),true)
  current={...normalized,...api.answerSpecFields(normalized)}
  delete current.answer_slots_text
  assert.deepEqual(current.answer_slots,['a\nb','c'])
  assert.deepEqual(current.slot_ids,['7[2]','7[5]'])
 }
})
