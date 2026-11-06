import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { createPinia, setActivePinia } from 'pinia'
import { assertAbsent, dialogTitled, isOpen, signedInTeacher, teleportToBody, tourCard } from './helpers/homeGuideLayout.js'
import { mountComponent } from './helpers/mountComponent.js'

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

const { useAuthStore } = await import('../src/stores/auth.js')

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

// The usage guide as the header opens it on a web browser, for `user`; the
// teacher skips the tour it shows.
async function skipTourAs(t, user) {
  const client = await signedInTeacher({ user, storage: { disclaimer_agreed: 'true' } })
  client.respond(() => ({ data: {} }))
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = user
  const { default: UsageGuideDialog } = await import('../src/components/UsageGuideDialog.vue?sfc')
  const page = await mountComponent(t, UsageGuideDialog, { path: '/upload', pinia })
  await page.vm.exposed.open()
  await page.flush()
  await tourCard().byText('el-button', '跳过引导').dispatch('click')
  await page.flush()
  assertAbsent(tourCard())
  page.unmount()
  return client.localStorage
}

test('公告的账号标识和引导完成标记与使用引导组件保持一致', async (t) => {
  assert.equal(accountIdentity({ id: 7, username: 'lin' }), 7)
  assert.equal(accountIdentity({ username: 'lin' }), 'lin')
  assert.equal(accountIdentity(null), 'current-account')
  assert.equal(usageGuideStorageKey({ id: 7 }), 'mathagent_usage_guide_v3:7')

  // What the home page reads is what the guide writes when the tour ends,
  // for an account with an id, one with only a username, and no account.
  teleportToBody(t)
  for (const user of [{ id: 7, username: 'lin', education_stage: 'senior', profile_complete: true }, { username: 'lin' }, null]) {
    const storage = await skipTourAs(t, user)
    assert.equal(readFlag(usageGuideStorageKey(user), storage), true, JSON.stringify(user))
    const written = [...Array(storage.length).keys()].map(index => storage.key(index)).filter(key => key.startsWith('mathagent_usage_guide_v3'))
    assert.deepEqual(written, [usageGuideStorageKey(user)], 'one flag, under the account it belongs to')
  }
})

// The home page as it opens for teacher 7 with `storage`.
async function openHome(t, storage) {
  const client = await signedInTeacher({ storage })
  client.respond(() => ({ data: { items: [] } }))
  const { default: Home } = await import('../src/views/Home.vue?sfc')
  const page = await mountComponent(t, Home, { path: '/' })
  const view = {
    notice: () => dialogTitled(page.root, '系统公告'),
    disclaimer: () => dialogTitled(page.root, '用户协议与免责声明'),
    badge: () => page.root.byClass('command-row__badge'),
    newItems: () => page.root.allByClass('notice-item').filter(item => item.classList.contains('is-new')).map(item => item.byTag('h3')[0].text),
    newTags: () => page.root.findAll(el => el.tagName === 'el-tag' && el.text === '新'),
  }
  return { client, page, view }
}

const HOME_NOTICES = 5
const seenTwoOld = {
  'mathagent_system_notice_seen_v1:7': JSON.stringify([
    '2026-07-30-先确认班级，每一页都填写姓名和学号',
    '2026-07-30-页面完整、文字正立、光线均匀',
    '2026-07-30-100 页以内，系统流程通常 20 分钟内完成',
  ]),
}

test('首页进入时自动弹出新公告，并在入口卡片和公告列表上标记', async (t) => {
  const { client, page, view } = await openHome(t, {
    disclaimer_agreed: 'true',
    'mathagent_usage_guide_v3:7': 'true',
    ...seenTwoOld,
  })
  assert.equal(isOpen(view.notice()), true, 'new notices pop up on entry')
  assert.equal(view.badge().text, '2 条新公告')
  assert.deepEqual(view.newItems(), ['管理员暂时无力维护', '批改服务连续超过 24 小时不可用'])
  assert.equal(view.newTags().length, 2)

  // Closing the dialog marks every notice as read for this account.
  await view.notice().handlers['update:modelValue'](false)
  await page.flush()
  assert.equal(isOpen(view.notice()), false)
  assertAbsent(view.badge())
  const seen = JSON.parse(client.localStorage.getItem('mathagent_system_notice_seen_v1:7'))
  assert.equal(seen.length, HOME_NOTICES)
  assert.ok(seen.includes('2026-10-28-管理员暂时无力维护'))
  page.unmount()

  // The next visit shows nothing new.
  const { view: again } = await openHome(t, {
    disclaimer_agreed: 'true',
    'mathagent_usage_guide_v3:7': 'true',
    'mathagent_system_notice_seen_v1:7': JSON.stringify(seen),
  })
  assert.equal(isOpen(again.notice()), false)
  assertAbsent(again.badge())
})

test('首页在免责声明或首次引导未完成时不自动弹出公告，但仍标记新公告', async (t) => {
  const firstVisit = await openHome(t, {})
  assert.equal(isOpen(firstVisit.view.disclaimer()), true)
  assert.equal(isOpen(firstVisit.view.notice()), false)
  assert.equal(firstVisit.view.badge().text, `${HOME_NOTICES} 条新公告`)
  firstVisit.page.unmount()

  // The tour is done but the disclaimer has not been agreed to: the
  // disclaimer alone holds the notice back.
  const disclaimerPending = await openHome(t, { 'mathagent_usage_guide_v3:7': 'true', ...seenTwoOld })
  assert.equal(isOpen(disclaimerPending.view.disclaimer()), true)
  assert.equal(isOpen(disclaimerPending.view.notice()), false, 'the disclaimer comes first')
  assert.equal(disclaimerPending.view.badge().text, '2 条新公告')
  disclaimerPending.page.unmount()

  const tourPending = await openHome(t, { disclaimer_agreed: 'true', ...seenTwoOld })
  assert.equal(isOpen(tourPending.view.disclaimer()), false)
  assert.equal(isOpen(tourPending.view.notice()), false, 'the first-run tour comes first')
  assert.equal(tourPending.view.badge().text, '2 条新公告')
  tourPending.page.unmount()

  // Another account's tour does not count for this one.
  const otherAccount = await openHome(t, { disclaimer_agreed: 'true', 'mathagent_usage_guide_v3:8': 'true', ...seenTwoOld })
  assert.equal(isOpen(otherAccount.view.notice()), false)
})
