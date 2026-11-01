import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = (name) => new URL(`../src/views/${name}`, import.meta.url)

test('model-triggering buttons expose a busy state and guard duplicate calls', async () => {
  const [upload, answerSheet, exercise, reports, studentDetail, bankImport, classTable, settings] = await Promise.all([
    readFile(source('Upload.vue'), 'utf8'),
    readFile(source('AnswerSheet.vue'), 'utf8'),
    readFile(source('Exercise.vue'), 'utf8'),
    readFile(source('Reports.vue'), 'utf8'),
    readFile(source('StudentDetail.vue'), 'utf8'),
    readFile(source('BankPhotoImport.vue'), 'utf8'),
    readFile(source('ClassTable.vue'), 'utf8'),
    readFile(source('Settings.vue'), 'utf8'),
  ])

  assert.match(upload, /if \(recognizingTemplate\.value\) return/)
  assert.match(upload, /if \(recognizing\.value\) return/)
  assert.match(upload, /if \(grading\.value\) return false/)
  assert.match(upload, /recognizingTemplate \? '识别中\.\.\.'/)
  assert.match(upload, /grading \? '批改中\.\.\.'/)

  assert.match(answerSheet, /if \(busy\.value\) return null/)
  assert.match(answerSheet, /:disabled="busy \|\| studentUploadActive"/)
  assert.match(answerSheet, /busy \? '生成中\.\.\.'/)

  assert.match(exercise, /:disabled="generating"/)
  assert.match(exercise, /if \(generating\.value\) return/)
  assert.match(reports, /:disabled="generating \|\| !auth\.currentClassId"/)
  assert.match(reports, /if \(generating\.value\) return/)
  assert.match(studentDetail, /:disabled="reporting"/)
  assert.match(studentDetail, /if \(reporting\.value\) return/)
  assert.match(studentDetail, /label="原题号"/)
  assert.match(studentDetail, /学生详情加载失败，当前数据可能不是最新，请重试。/)
  assert.match(studentDetail, /loadError\.value = ''/)
  assert.match(bankImport, /:disabled="recognizing \|\| !canRecognize"/)
  assert.match(bankImport, /if \(recognizing\.value\) return/)
  assert.match(classTable, /:disabled="recognizing \|\| !studentImageFile"/)
  assert.match(classTable, /if \(recognizing\.value\) return/)
  assert.match(classTable, /command="reports"/)
  assert.match(classTable, /router\.push\('\/reports'\)/)
  assert.match(settings, /:disabled="testing"/)
  assert.match(settings, /if \(testing\.value\) return/)
})
