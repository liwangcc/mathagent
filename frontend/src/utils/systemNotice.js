// 系统公告 acknowledgement. Each teacher account keeps, in this browser, the
// keys of the notices it has already closed. Anything not in that list is new:
// it pops up once when the home page opens and carries a 新 mark until the
// dialog is closed again.

const SEEN_PREFIX = 'mathagent_system_notice_seen_v1'
// Same key and account rule as UsageGuideDialog.vue. The first-run tour must
// have finished for this account before a notice may pop up, otherwise the two
// overlays would stack on a teacher's very first visit.
const USAGE_GUIDE_PREFIX = 'mathagent_usage_guide_v3'

export function noticeKey(item) {
  return `${item.date}-${item.title}`
}

export function accountIdentity(user) {
  return user?.id ?? user?.username ?? 'current-account'
}

export function seenNoticesStorageKey(user) {
  return `${SEEN_PREFIX}:${accountIdentity(user)}`
}

export function usageGuideStorageKey(user) {
  return `${USAGE_GUIDE_PREFIX}:${accountIdentity(user)}`
}

export function parseSeenNotices(raw) {
  try {
    const parsed = JSON.parse(raw || '[]')
    return new Set(Array.isArray(parsed) ? parsed.filter((key) => typeof key === 'string') : [])
  } catch {
    return new Set()
  }
}

export function unseenNotices(items, seenKeys) {
  return items.filter((item) => !seenKeys.has(noticeKey(item)))
}

export function readFlag(key, storage = globalThis.localStorage) {
  try {
    return storage?.getItem(key) === 'true'
  } catch {
    return false
  }
}

export function loadSeenNotices(user, storage = globalThis.localStorage) {
  try {
    return parseSeenNotices(storage?.getItem(seenNoticesStorageKey(user)))
  } catch {
    return new Set()
  }
}

export function saveSeenNotices(user, items, storage = globalThis.localStorage) {
  const keys = items.map(noticeKey)
  try {
    storage?.setItem(seenNoticesStorageKey(user), JSON.stringify(keys))
  } catch {
    // Without storage the notice simply pops up again on the next visit.
  }
  return new Set(keys)
}

export function shouldAutoOpenNotice({ unseenCount, disclaimerAgreed, usageGuideDone }) {
  return unseenCount > 0 && disclaimerAgreed && usageGuideDone
}
