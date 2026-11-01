import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

test('教师OCR状态只显示行动结论，不暴露人数、槽位或队列位置', async () => {
  const upload = await readFile(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
  const api = await readFile(new URL('../src/api/index.js', import.meta.url), 'utf8')
  const sse = await readFile(new URL('../src/utils/sse.js', import.meta.url), 'utf8')

  assert.match(api, /const AVAILABILITY_TIMEOUT_MS = 10000/)
  assert.match(api, /availability: \(\) => http\.get\('\/batch-grade\/availability', \{[\s\S]*?timeout: AVAILABILITY_TIMEOUT_MS/)
  assert.match(api, /suppressErrorToast: true/)
  assert.match(api, /!err\.config\?\.suppressErrorToast/)
  assert.match(upload, /serviceState\.label/)
  for (const label of ['可用', '需排队', '暂不可用']) {
    assert.match(upload, new RegExp(label))
  }
  assert.match(upload, /链路故障/)
  assert.match(upload, /serviceBlocksNewWork/)
  assert.match(upload, /reportSseServiceFault/)
  assert.match(sse, /GRADING_CHAIN_FAILED/)
  assert.match(upload, /SERVICE_FAULT_HOLD_MS = 60_000/)
  assert.match(upload, /Date\.now\(\) < serviceFaultHoldUntil/)
  assert.match(upload, /if \(availabilityCheckPending\) return/)
  assert.match(upload, /批改链路状态响应异常/)
  assert.match(api, /normalizeHardwareAvailability/)
  assert.match(api, /waiting_accelerator: '等待教师客户端加速节点/)
  assert.match(api, /unavailable: '本地加速节点不可用/)
  assert.match(upload, /const ACCELERATOR_UNAVAILABLE_MESSAGE = '加速节点不可用，请使用客户端'/)
  assert.match(upload, /\['waiting_accelerator', 'pending', 'unavailable'\]\.includes\(status\)/)
  assert.match(upload, /message: advisory \? '[^']+' : ACCELERATOR_UNAVAILABLE_MESSAGE/)
  assert.match(upload, /serviceState\.status !== 'available'/)
  assert.match(upload, /const serviceBlocksNewWork = computed\(\(\) => \(\s*\['checking', 'unavailable', 'fault'\]\.includes\(serviceState\.value\.status\)\s*\)\)/)
  // 全局链路保持硬门禁；只有后端显式 advisory=true 才豁免额外硬件门禁。
  assert.match(upload, /serviceBlocksNewWork\.value\s*\|\| registrationHardwareBlocksNewWork\.value/)
  assert.match(upload, /const advisory = hardware\?\.advisory === true/)
  assert.match(upload, /async function onRunTemplateCut[\s\S]*?if \(!requireTemplateService\(\)\) return/)
  assert.match(upload, /async function confirmOrientationAndRetry[\s\S]*?if \(!requireRecognitionService\(\)\) return/)
  assert.match(upload, /const registrationHardware = requiredHardwareState\(templateState\.registration_hardware\)/)
  assert.doesNotMatch(upload, /const templateHardware = requiredHardwareState\(templateState\.hardware\)/)
  assert.match(upload, /const templateModelStatus = labels\[templateState\.service_status\]/)
  assert.match(upload, /v-if="templateServiceState\.status !== 'available'"/)
  assert.match(upload, /:role="templateServiceBlocksNewWork \? 'alert' : 'status'"/)
  for (const forbidden of ['活跃教师', '排队人数', '队列位置', '空闲槽', '剩余请求槽']) {
    assert.doesNotMatch(upload, new RegExp(forbidden))
  }
})
