import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

// 已确认模板是本次批阅的唯一权威：识别完成后直接批阅，页面上没有"确认参考答案"步骤。
// 这些断言防止后续改动重新引入客户端答案确认页或无模板分支。
test('the upload flow has no answer-confirmation step or no-template branch', () => {
  assert.doesNotMatch(upload, /activeStep\.value = 2/)
  assert.doesNotMatch(upload, /activeStep === 2/)
  assert.doesNotMatch(upload, /isConfirmedTemplateBatch|baselineDialogVisible|onConfirmBaselines/)
})

test('recognition completion routes straight to grading', () => {
  const runRecognize = upload.match(
    /async function runRecognize\(\) \{([\s\S]*?)\n\}\n\nasync function loadDailyUsage/,
  )
  assert.ok(runRecognize, 'runRecognize function should be present')
  assert.match(runRecognize[1], /if \(!recognitionCanGrade\.value\) \{\s*activeStep\.value = 1/)
  assert.match(runRecognize[1], /activeStep\.value = 1\s*await onConfirmGrade\(\)/)
  assert.doesNotMatch(runRecognize[1], /continueAfterRecognition/)
})

test('recovered and re-checked batches go straight to grading', () => {
  // 恢复识别完成后直接批阅。
  assert.match(
    upload,
    /if \(!questions\.value\.length\) throw new Error\('恢复识别后未得到可批阅题目'\)\s*await onConfirmGrade\(\)/,
  )
  // 单卷方向确认成功后直接批阅。
  assert.match(
    upload,
    /if \(payload\.can_grade\) \{\s*await onConfirmGrade\(\)\s*return\s*\}/,
  )
  // 方向复核批次恢复后直接批阅。
  assert.match(
    upload,
    /if \(recognitionCanGrade\.value\) \{\s*await onConfirmGrade\(\)\s*\}/,
  )
})

test('recognition exposes new-batch and continuation controls before the long paper list', () => {
  const step = upload.slice(
    upload.indexOf('<template v-else-if="activeStep === 1">'),
    upload.indexOf('v-model="orientationDialogVisible"'),
  )
  const table = step.indexOf('<table v-if="papers.length"')
  assert.ok(step.indexOf('@click="startNewBatch"') >= 0)
  assert.ok(step.indexOf('@click="startNewBatch"') < table)
  assert.ok(step.indexOf('@click="continueAfterRecognition"') >= 0)
  assert.ok(step.indexOf('@click="continueAfterRecognition"') < table)
  assert.match(step, /v-if="!recognizing" class="actions"/)
  assert.match(step, /开始新批次/)
  assert.match(upload, /async function continueAfterRecognition\(\) \{[\s\S]*?return onConfirmGrade\(\)\n\}/)
})

test('template grading submits no client answers', () => {
  const confirmGrade = upload.match(
    /async function onConfirmGrade\(skipIdentityGate = false\) \{([\s\S]*?)\n\}\n/,
  )
  assert.ok(confirmGrade, 'onConfirmGrade function should be present')
  // 冻结答案由服务端从模板读取，前端不回传答案。
  assert.doesNotMatch(confirmGrade[1], /confirmed_answers|canConfirm/)
})

test('identity confirmation is the only dialog a template batch can still stop on', () => {
  const confirmGrade = upload.match(
    /async function onConfirmGrade\(skipIdentityGate = false\) \{([\s\S]*?)\n\}\n/,
  )
  assert.ok(confirmGrade, 'onConfirmGrade function should be present')
  // 身份闸门只打开身份弹窗，不切换步骤。
  assert.match(
    confirmGrade[1],
    /if \(!skipIdentityGate && needsIdentityResolution\.value\) \{\s*identityDialogVisible\.value = true\s*if \(!identityResolutions\.value\.length\) await prepareIdentityResolutions\(\)\s*return false\s*\}/,
  )
  // 身份确认后走同一条批阅入口（跳过重复的身份闸门）。
  assert.match(
    upload,
    /async function onConfirmIdentitiesAndGrade\(\) \{[\s\S]*?await onConfirmGrade\(true\)/,
  )
})

test('mode selection is independent of the template authority path', () => {
  const confirmGrade = upload.match(
    /async function onConfirmGrade\(skipIdentityGate = false\) \{([\s\S]*?)\n\}\n/,
  )
  assert.ok(confirmGrade, 'onConfirmGrade function should be present')
  // 简单与精准模式共用同一批阅提交。
  assert.match(confirmGrade[1], /mode: form\.mode/)
  assert.doesNotMatch(confirmGrade[1], /form\.mode === 'simple'[\s\S]*?activeStep/)
})
