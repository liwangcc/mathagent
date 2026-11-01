import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

test('template recognition button lets click validation explain missing fields', async () => {
  const source = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
  const button = source.match(
    /<button\s+type="button"\s+class="btn btn-primary"\s+:disabled="([^"]+)"\s+@click="onRecognizeTemplate"/,
  )

  assert.ok(button, 'template recognition button must remain wired to its click handler')
  assert.equal(
    button[1],
    '!templateFiles.length || recognizingTemplate || templatePreprocessing || templateServiceBlocksNewWork',
  )
  assert.match(source, /if \(!requireTemplateService\(\)\) return/)
  assert.match(source, /templateServiceState\.value\.blocksNewWork !== false/)
  assert.match(source, /const registrationHardware = requiredHardwareState\(templateState\.registration_hardware\)/)
  assert.doesNotMatch(source, /const templateHardware = requiredHardwareState\(templateState\.hardware\)/)
  assert.match(source, /const templateModelStatus = labels\[templateState\.service_status\]/)
  assert.doesNotMatch(
    source,
    /function requireTemplateService\(\) \{\s*if \(!requireBatchService\(\)\)/,
  )
  assert.match(source, /api\.grade\.templateAvailability\(\)/)
  assert.match(source, /const ACCELERATOR_UNAVAILABLE_MESSAGE = '加速节点不可用，请使用客户端'/)
  assert.match(source, /:disabled="templatePreprocessing \|\| templateServiceBlocksNewWork"/)
  assert.match(source, /模板识别链路当前不可用/)
  assert.match(source, /ElMessage\.warning\('请先导入模板图片'\)/)
  assert.match(source, /ElMessage\.warning\('请先选择本次批阅类型'\)/)
  assert.match(source, /ElMessage\.warning\('请先填写考试名称'\)/)
})

test('template import branch is rendered directly instead of hidden in a native template element', async () => {
  const source = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

  assert.match(
    source,
    /<div v-if="confirmedTemplateId" class="tpl-confirmed"[^>]*>[\s\S]*?<\/div>\s*<!--[\s\S]*?未确认：导入 N 页[\s\S]*?-->/,
  )
  assert.match(source, /<!--[\s\S]*?未确认：导入 N 页[\s\S]*?-->\s*<template v-else>/)
  assert.doesNotMatch(source, /<template>\s*<!--[\s\S]*?已保存：可在学生试卷开始识别前再次编辑/)
})

test('student recognition requires the confirmed template and registration hardware', async () => {
  const source = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

  assert.match(source, /const registrationHardware = requiredHardwareState\(templateState\.registration_hardware\)/)
  assert.match(source, /serviceBlocksNewWork\.value\s*\|\| registrationHardwareBlocksNewWork\.value/)
  assert.match(source, /function requireRecognitionService\(\) \{[\s\S]*if \(!registrationHardwareBlocksNewWork\.value\) return true/)
  assert.match(source, /async function runRecognize\(\) \{\s*if \(!requireRecognitionService\(\)\) return/)
  assert.match(source, /:disabled="!fileList\.length \|\| !confirmedTemplateId \|\| recognizing \|\| imagePreprocessing \|\| recognitionBlocksNewWork"/)
  assert.doesNotMatch(source, /registration-service-alert/)
  assert.doesNotMatch(source, /配准提示|加速节点状态仅供参考，不影响本次识别/)
})
