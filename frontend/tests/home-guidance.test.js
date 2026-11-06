import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import './helpers/srcModules.js'
import { assertAbsent, dialogTitled, isOpen, signedInTeacher, teleportToBody, tourCard } from './helpers/homeGuideLayout.js'
import { mountComponent } from './helpers/mountComponent.js'

const manualPath = fileURLToPath(
  new URL('../src/assets/docs/MathAgent_产品说明书与公告文本.docx', import.meta.url),
)
const { buildUsageTourSteps } = await import('../src/utils/usageTour.js')

// The home page of a teacher who has agreed to the disclaimer and finished
// the first-run tour.
async function mountHome(t) {
  const client = await signedInTeacher({ storage: { disclaimer_agreed: 'true', 'mathagent_usage_guide_v3:7': 'true' } })
  client.respond(() => ({ data: { items: [] } }))
  const { default: Home } = await import('../src/views/Home.vue?sfc')
  const page = await mountComponent(t, Home, { path: '/' })
  return { client, page }
}

// The notices the 系统公告 dialog lists, opened from its entry on the page.
async function openNotices(page) {
  const dialog = dialogTitled(page.root, '系统公告')
  if (!isOpen(dialog)) {
    await page.root.find(el => el.classList.contains('command-row') && el.text.startsWith('系统公告')).dispatch('click')
    await page.flush()
  }
  assert.ok(isOpen(dialog), 'the notice dialog is open')
  return dialog.byClass('notice-list')
}

const noticesOf = list => list.allByClass('notice-item').map(item => ({
  date: item.byTag('time')[0].text,
  title: item.byTag('h3')[0].text,
  content: item.byTag('p')[0].text,
}))

test('首页公告保持简短并准确说明100页处理时间', async (t) => {
  const { page } = await mountHome(t)
  const notices = noticesOf(await openNotices(page))
  const timing = notices.find(notice => notice.title === '100 页以内，系统流程通常 20 分钟内完成')
  assert.ok(timing, 'the processing-time notice is listed')
  assert.match(timing.content, /主要时间通常花在教师拍照、检查和上传/)
  for (const notice of notices) {
    assert.doesNotMatch(`${notice.title}${notice.content}`, /50至140分钟|25至70分钟/)
  }
  assert.equal(notices.filter(notice => notice.date === '2026-07-30').length, 3)
})

test('首页公告说明模型服务长期不可用的可能原因', async (t) => {
  const { page } = await mountHome(t)
  const notices = noticesOf(await openNotices(page))
  const outage = notices.find(notice => notice.title === '批改服务连续超过 24 小时不可用')
  assert.ok(outage)
  assert.match(outage.content, /阿里云和小米的模型接口欠费后会自动停用/)
  assert.match(outage.content, /连续超过 24 小时不可用，大概率是管理员暂时没钱续费维护/)
})

test('统一使用引导按正常业务顺序指向操作位置，首页保留详细Word下载', async (t) => {
  const titles = buildUsageTourSteps({ classes: [{ id: 1, class_name: '一班', student_count: 3 }], currentClassId: 1 })
    .map((step) => step.title)
  for (const label of ['先确认当前班级', '名单有变化，点这里补充', '点这里导入答案模板', '把学生试卷放进来', '材料齐了，点「开始批阅」', '批改完，就在这一页复核归档']) {
    assert.ok(titles.includes(label), label)
  }

  // Home offers the guide as an entry of its own and keeps the Word manual
  // as a download in the notice dialog.
  const { page } = await mountHome(t)
  const entry = page.root.find(el => el.classList.contains('command-row') && el.text.startsWith('使用引导'))
  assert.ok(entry, 'a 使用引导 entry')
  assert.equal(entry.find(el => el.tagName === 'small').text, '高亮带你找到每一步该点的位置')
  const list = await openNotices(page)
  const manual = list.byClass('document-download')
  assert.equal(manual.tagName, 'a')
  assert.equal(manual.getAttribute('download'), 'MathAgent_产品说明书与公告文本.docx')
  assert.ok(manual.getAttribute('href').endsWith('assets/docs/MathAgent_产品说明书与公告文本.docx'), 'links the bundled manual')
  assert.match(manual.text, /下载 Word 详细版/)
  page.unmount()

  // The guide the header opens walks the teacher's own classes, as loaded
  // from the server.
  teleportToBody(t)
  const client = await signedInTeacher({ storage: { disclaimer_agreed: 'true', 'mathagent_usage_guide_v3:7': 'true' } })
  client.respond(({ url }) => (url === '/auth/classes'
    ? { data: { classes: [{ id: 1, class_name: '一班', student_count: 3 }], current_class_id: 1 } }
    : { data: {} }))
  const { default: Layout } = await import('../src/views/Layout.vue?sfc')
  const layout = await mountComponent(t, Layout, { path: '/upload' })
  assertAbsent(tourCard())
  await layout.root.find(el => el.getAttribute('aria-label') === '打开使用引导').dispatch('click')
  await layout.flush()
  const card = tourCard()
  assert.equal(card.byClass('guided-tour__count').text, `第 1 / ${titles.length} 步`)
  assert.equal(card.byClass('guided-tour__title').text, '先确认当前班级')
  assert.match(card.byClass('guided-tour__body').text, /现在是「一班」/)
})

test('详细说明书资源是可发布的非空DOCX容器', () => {
  const bytes = readFileSync(manualPath)
  assert.ok(statSync(manualPath).size > 40_000)
  assert.equal(bytes.subarray(0, 2).toString('ascii'), 'PK')
})
