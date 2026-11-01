import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = (path) => new URL(`../src/${path}`, import.meta.url)

test('report overview is scoped by current class and period', async () => {
  const view = await readFile(source('views/Reports.vue'), 'utf8')

  assert.match(view, /api\.report\.summary\(\{[\s\S]*period: period\.value,[\s\S]*class_id: classId,[\s\S]*type: 'all'/)
  assert.match(view, /metrics\.questions/)
  assert.doesNotMatch(view, /classParentLink/)
  assert.doesNotMatch(view, /班级统一入口/)
})

test('parent letter links are reusable, refreshable and manageable without raw HTML dialogs', async () => {
  const [view, api] = await Promise.all([
    readFile(source('views/Reports.vue'), 'utf8'),
    readFile(source('api/index.js'), 'utf8'),
  ])

  assert.match(view, /generateBatchReport\(false\)/)
  assert.match(view, /refresh,/)
  assert.match(view, /loadLetterShares/)
  assert.match(view, /revokeShare/)
  assert.match(view, /复制全部有效链接/)
  assert.doesNotMatch(view, /dangerouslyUseHTMLString/)
  assert.match(api, /letterShares: \(classId, params\) => http\.get\(`\/class\/\$\{classId\}\/letter-shares`, \{ params \}\)/)
  assert.match(api, /revokeLetterShare: \(shareId\)/)
  assert.doesNotMatch(api, /classParentLink:/)
})

test('generated letter links keep all fields within the desktop width budget', async () => {
  const view = await readFile(source('views/Reports.vue'), 'utf8')
  const table = view.match(/<el-table\b[^>]*v-if="letterShares\.length"[\s\S]*?<\/el-table>/)?.[0]
  assert.ok(table, 'generated links table is present')
  const columns = [...table.matchAll(/<el-table-column\b([^>]+)>/g)].map(([, attrs]) => ({
    label: attrs.match(/label="([^"]+)"/)?.[1],
    width: Number(attrs.match(/(?:min-)?width="(\d+)"/)?.[1]),
  }))

  assert.deepEqual(columns.map(column => column.label), ['学生', '状态', '查看次数', '学段', '有效期', '操作'])
  const minimumWidth = columns.reduce((sum, column) => sum + column.width, 0)
  assert.ok(minimumWidth <= 558, `column widths ${minimumWidth}px exceed the 1280px desktop table budget`)
  assert.match(table, /label="操作"[^>]*fixed="right"/)
  assert.match(table, /@click="copyShare\(row\)"/)
  assert.match(table, /@click="revokeShare\(row\)"/)
})
