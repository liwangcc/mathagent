import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  accountIdentity,
  loadSeenNotices,
  noticeKey,
  parseSeenNotices,
  readFlag,
  saveSeenNotices,
  seenNoticesStorageKey,
  shouldAutoOpenNotice,
  unseenNotices,
  usageGuideStorageKey,
} from '../src/utils/systemNotice.js'

const source = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')
const home = source('../src/views/Home.vue')
const guide = source('../src/components/UsageGuideDialog.vue')

const items = [
  { date: '2026-10-28', title: '管理员暂时无力维护' },
  { date: '2026-08-21', title: '批改服务连续超过 24 小时不可用' },
]

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    map,
  }
}

test('未关闭过的公告按日期加标题识别为新公告', () => {
  assert.equal(noticeKey(items[0]), '2026-10-28-管理员暂时无力维护')
  const seen = new Set([noticeKey(items[1])])
  assert.deepEqual(unseenNotices(items, seen), [items[0]])
  assert.deepEqual(unseenNotices(items, new Set()), items)
})

test('已读记录缺失或损坏时按全部未读处理', () => {
  for (const raw of [null, undefined, '', 'garbage', '{"a":1}', '[1,2]']) {
    assert.equal(parseSeenNotices(raw).size, 0, String(raw))
  }
  assert.deepEqual([...parseSeenNotices('["a",2,"b"]')], ['a', 'b'])
})

test('关闭公告后按账号记住已读，换账号互不影响', () => {
  const storage = fakeStorage()
  const teacher = { id: 7 }
  assert.equal(loadSeenNotices(teacher, storage).size, 0)
  const seen = saveSeenNotices(teacher, items, storage)
  assert.equal(seen.size, 2)
  assert.deepEqual([...loadSeenNotices(teacher, storage)], items.map(noticeKey))
  assert.equal(loadSeenNotices({ id: 8 }, storage).size, 0)
  assert.equal(seenNoticesStorageKey(teacher), 'mathagent_system_notice_seen_v1:7')
})

test('存储不可用时读写都不抛错', () => {
  const broken = {
    getItem() { throw new Error('blocked') },
    setItem() { throw new Error('blocked') },
  }
  assert.equal(loadSeenNotices({ id: 1 }, broken).size, 0)
  assert.equal(saveSeenNotices({ id: 1 }, items, broken).size, 2)
  assert.equal(readFlag('x', broken), false)
  assert.equal(readFlag('x', fakeStorage({ x: 'true' })), true)
})

test('只有在免责声明和首次引导都完成后才自动弹出新公告', () => {
  const ready = { unseenCount: 1, disclaimerAgreed: true, usageGuideDone: true }
  assert.equal(shouldAutoOpenNotice(ready), true)
  assert.equal(shouldAutoOpenNotice({ ...ready, unseenCount: 0 }), false)
  assert.equal(shouldAutoOpenNotice({ ...ready, disclaimerAgreed: false }), false)
  assert.equal(shouldAutoOpenNotice({ ...ready, usageGuideDone: false }), false)
})

test('公告的账号标识和引导完成标记与使用引导组件保持一致', () => {
  assert.equal(accountIdentity({ id: 7, username: 'lin' }), 7)
  assert.equal(accountIdentity({ username: 'lin' }), 'lin')
  assert.equal(accountIdentity(null), 'current-account')
  assert.match(guide, /auth\.user\?\.id \?\? auth\.user\?\.username \?\? 'current-account'/)
  assert.match(guide, /const WEB_GUIDE_KEY = 'mathagent_usage_guide_v3'/)
  assert.equal(usageGuideStorageKey({ id: 7 }), 'mathagent_usage_guide_v3:7')
})

test('首页进入时自动弹出新公告，并在入口卡片和公告列表上标记', () => {
  assert.match(home, /seenNoticeKeys\.value = loadSeenNotices\(auth\.user\)/)
  assert.match(home, /shouldAutoOpenNotice\(\{/)
  assert.match(home, /usageGuideDone: readFlag\(usageGuideStorageKey\(auth\.user\)\)/)
  assert.match(home, /@closed="acknowledgeNotices"/)
  assert.match(home, /saveSeenNotices\(auth\.user, noticeItems\)/)
  assert.match(home, /class="command-row__badge">\{\{ newNoticeCount \}\} 条新公告/)
  assert.match(home, /:class="\{ 'is-new': isNewNotice\(item\) \}"/)
  assert.match(home, /v-if="isNewNotice\(item\)"[^>]*>新<\/el-tag>/)
})
