import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const homePath = fileURLToPath(new URL('../src/views/Home.vue', import.meta.url))
const guidePath = fileURLToPath(new URL('../src/components/UsageGuideDialog.vue', import.meta.url))
const manualPath = fileURLToPath(
  new URL('../src/assets/docs/MathAgent_产品说明书与公告文本.docx', import.meta.url),
)
const homeSource = readFileSync(homePath, 'utf8')
const guideSource = readFileSync(guidePath, 'utf8')
const { buildUsageTourSteps } = await import('../src/utils/usageTour.js')

test('首页公告保持简短并准确说明100页处理时间', () => {
  assert.match(homeSource, /主要时间通常花在教师拍照、检查和上传/)
  assert.match(homeSource, /100 页以内，系统流程通常 20 分钟内完成/)
  assert.doesNotMatch(homeSource, /50至140分钟|25至70分钟/)
  assert.equal((homeSource.match(/date: '2026-07-30'/g) || []).length, 3)
})

test('首页公告说明模型服务长期不可用的可能原因', () => {
  assert.match(homeSource, /阿里云和小米的模型接口欠费后会自动停用/)
  assert.match(homeSource, /连续超过 24 小时不可用，大概率是管理员暂时没钱续费维护/)
})

test('统一使用引导按正常业务顺序指向操作位置，首页保留详细Word下载', () => {
  assert.match(guideSource, /buildUsageTourSteps\(\{ classes: auth\.classes, currentClassId: auth\.currentClassId \}\)/)
  const titles = buildUsageTourSteps({ classes: [{ id: 1, class_name: '一班', student_count: 3 }], currentClassId: 1 })
    .map((step) => step.title)
  for (const label of ['先确认当前班级', '名单有变化，点这里补充', '点这里导入答案模板', '把学生试卷放进来', '材料齐了，点「开始批阅」', '批改完，就在这一页复核归档']) {
    assert.ok(titles.includes(label), label)
  }
  assert.match(homeSource, /title: '使用引导', desc: '高亮带你找到每一步该点的位置'/)
  assert.match(homeSource, /download="MathAgent_产品说明书与公告文本\.docx"/)
  assert.match(homeSource, /MathAgent_产品说明书与公告文本\.docx\?url/)
})

test('详细说明书资源是可发布的非空DOCX容器', () => {
  const bytes = readFileSync(manualPath)
  assert.ok(statSync(manualPath).size > 40_000)
  assert.equal(bytes.subarray(0, 2).toString('ascii'), 'PK')
})
