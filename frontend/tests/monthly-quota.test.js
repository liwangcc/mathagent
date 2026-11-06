import test from 'node:test'
import assert from 'node:assert/strict'
import { loadApiClient } from './helpers/apiClient.js'
import { textOf } from './helpers/availabilityQuota.js'
import { sseResponse } from './helpers/memoryFetch.js'
import { mountUpload, paperFile } from './helpers/uploadPage.js'

// The real Upload page shows the teacher's daily allowance of original
// student images from GET /usage/stats, loaded with the readiness probes when
// the page opens and again once a recognition batch has been created.

const STATS = 'GET /api/usage/stats'
const RECOGNIZE = 'POST /api/batch-grade/recognize'
const AVAILABILITY = 'GET /api/batch-grade/availability'
const TEMPLATE_AVAILABILITY = 'GET /api/batch-grade/template/availability'

const quota = upload => upload.page.root.byClass('monthly-quota')
// The quota line's parts as the teacher reads them.
const quotaText = upload => quota(upload).children.map(el => el.text).filter(Boolean)
// How much of the meter is filled.
const meterWidth = upload => quota(upload).byClass('quota-meter').children[0].style.width
const daily = (current, limit = 400, remaining = limit - current) => ({ daily_images: { current, limit, remaining, unlimited: false } })

test('批改页显示每日400张原始学生图片额度并在创建批次后刷新', async (t) => {
  // What the page asks for: the JSON API and the recognition stream.
  const calls = []
  let stats = daily(12)
  // The recognition request stays unanswered until the test answers it.
  let answerBatch
  const upload = await mountUpload(t, {
    responses: {
      [STATS]: () => { calls.push('stats'); return { data: stats } },
      [AVAILABILITY]: () => { calls.push('availability'); return { data: { status: 'available' } } },
      [TEMPLATE_AVAILABILITY]: () => { calls.push('template availability'); return { data: { status: 'available', service_status: 'available', registration_hardware: { status: 'ready' } } } },
    },
    streams: {
      [RECOGNIZE]: () => {
        calls.push('recognize')
        return new Promise((resolve) => { answerBatch = resolve })
      },
    },
  })
  // Opening the page probes readiness and loads the allowance together.
  assert.deepEqual([...calls].sort(), ['availability', 'stats', 'template availability'])
  assert.deepEqual(quotaText(upload), ['今日学生图片', '12 / 400 张', '剩余 388 张'])
  assert.equal(quota(upload).byClass('quota-now').text, '12')
  // 12 / 400 is 3 %.
  assert.equal(meterWidth(upload), '3%')

  // Saving the template with papers chosen starts recognition by itself.
  upload.page.setup.confirmedTemplateId = 'tpl-quota'
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.equal(upload.requests(STATS).length, 1, 'no reload while the batch request is unanswered')
  assert.deepEqual(quotaText(upload), ['今日学生图片', '12 / 400 张', '剩余 388 张'])

  // The server creates the batch and counts the new image; only then does the
  // page read the allowance again.
  stats = daily(13)
  answerBatch(sseResponse([['done', { batch_id: 'b-quota', can_grade: false, uploaded_papers: 1, total_papers: 0, unresolved_papers: 1 }]]))
  await upload.settle()
  assert.deepEqual(calls.slice(3), ['recognize', 'stats'], 'the allowance is reloaded once the batch request is answered')
  assert.equal(upload.requests(STATS).length, 2)
  assert.deepEqual(quotaText(upload), ['今日学生图片', '13 / 400 张', '剩余 387 张'])
  // 13 / 400 is 3.25 %: rounded, not up to 4 %.
  assert.equal(meterWidth(upload), '3%')
})

test('额度条按已用比例四舍五入，用量超过限额时封顶为满格', async (t) => {
  for (const [stats, shown, width] of [
    // 7 / 400 is 1.75 %: rounded, not down to 1 %.
    [daily(7), ['今日学生图片', '7 / 400 张', '剩余 393 张'], '2%'],
    // 2 / 400 is 0.5 %: half rounds up.
    [daily(2), ['今日学生图片', '2 / 400 张', '剩余 398 张'], '1%'],
    [daily(400), ['今日学生图片', '400 / 400 张', '剩余 0 张'], '100%'],
    // More images than the limit: a full meter, not 125 %.
    [daily(500, 400, 0), ['今日学生图片', '500 / 400 张', '剩余 0 张'], '100%'],
  ]) {
    const name = JSON.stringify(stats.daily_images)
    const upload = await mountUpload(t, { responses: { [STATS]: stats } })
    assert.deepEqual(quotaText(upload), shown, name)
    assert.equal(meterWidth(upload), width, name)
    upload.unmount()
  }
})

test('额度统计缺少每日字段时沿用月度字段，无限额或读取失败时不显示额度', async (t) => {
  const monthly = await mountUpload(t, {
    responses: { [STATS]: { monthly_images: { current: 40, limit: 100, remaining: 60, unlimited: false } } },
  })
  assert.deepEqual(quotaText(monthly), ['今日学生图片', '40 / 100 张', '剩余 60 张'])
  assert.equal(meterWidth(monthly), '40%')
  monthly.unmount()

  for (const answer of [
    { daily_images: { current: 3, limit: 0, remaining: 0, unlimited: true } },
    {},
    () => ({ status: 500, data: { detail: '统计暂不可用' } }),
  ]) {
    const upload = await mountUpload(t, { responses: { [STATS]: answer } })
    assert.equal(upload.requests(STATS).length, 1)
    assert.equal(textOf(quota(upload)), null, JSON.stringify(answer))
    assert.ok(!upload.text().includes('今日学生图片'))
    upload.unmount()
  }
})

test('额度统计接口 GET /usage/stats 并把后端统计原样交给批改页', async () => {
  const client = await loadApiClient()
  const stats = { daily_images: { current: 12, limit: 400, remaining: 388, unlimited: false } }
  client.respond(() => ({ data: stats }))

  assert.deepEqual(await client.api.usage.stats(), stats)
  assert.equal(client.requests.length, 1)
  assert.equal(client.requests[0].method, 'get')
  assert.equal(client.requests[0].url, '/usage/stats')
})
