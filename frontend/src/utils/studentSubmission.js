// Student self-upload: link addresses, page slots on the student page, and
// the files the grading page imports from a class link.
import { withUploadSourcePath } from './uploadSource.js'

// The folder every imported page sits in.  As the first path component it is
// only a container; the student folder below it carries the identity clue
// (services/collection_identity.py).
export const SUBMISSION_IMPORT_ROOT = '学生提交'

export function studentSubmitUrl(origin, token) {
  return `${origin}/submit/${encodeURIComponent(token)}`
}

export function studentSubmitOriginWarning(origin) {
  try {
    const { hostname } = new URL(origin)
    if (['localhost', '127.0.0.1', '[::1]', '0.0.0.0'].includes(hostname)) {
      return '当前是本机地址，学生的手机打不开这个链接。请用局域网 IP 或正式域名打开本页后再复制链接。'
    }
    return ''
  } catch {
    return '无法确定学生访问地址，请用正式域名打开本页。'
  }
}

// Slots 1..N where N is the highest uploaded page, so a gap left by a deleted
// page stays visible until it is filled.
export function pageSlots(pages) {
  const byNumber = new Map((pages || []).map((page) => [Number(page.page_no), page]))
  const last = Math.max(0, ...byNumber.keys())
  return Array.from({ length: last }, (_, index) => ({ pageNo: index + 1, page: byNumber.get(index + 1) || null }))
}

// Page numbers for newly picked photos: after the last uploaded page, in the
// order picked, never beyond the limit.
export function nextPageNumbers(pages, count, maxPages) {
  const last = Math.max(0, ...(pages || []).map((page) => Number(page.page_no)))
  const available = Math.max(0, maxPages - last)
  return Array.from({ length: Math.min(count, available) }, (_, index) => last + index + 1)
}

function pathPart(value) {
  // Path separators, colons and control characters are not allowed in
  // upload source paths (uploadSource.js); a roster name never needs them.
  return String(value || '').replace(/[\\/:\x00-\x1f\x7f]+/g, '_').trim()
}

// "学生提交/张三_学号2024001/第1页.jpg": the explicit 学号 label keeps short or
// zero-padded numbers from being read as page counters.
export function submissionSourcePath(student, pageNo) {
  const number = pathPart(student.student_no)
  const folder = number ? `${pathPart(student.name)}_学号${number}` : pathPart(student.name)
  return `${SUBMISSION_IMPORT_ROOT}/${folder || '未命名'}/第${Number(pageNo)}页.jpg`
}

// Pages to import, student by student (in the given order) and page by page.
export function submissionImportEntries(students) {
  return (students || []).flatMap((student) => [...(student.pages || [])]
    .sort((a, b) => a.page_no - b.page_no)
    .map((page) => ({ pageId: page.id, path: submissionSourcePath(student, page.page_no) })))
}

export function submissionFile(blob, path) {
  const type = blob.type || 'image/jpeg'
  const name = path.split('/').pop()
  return withUploadSourcePath(new File([blob], name, { type, lastModified: Date.now() }), path)
}

export function formatSubmissionTime(value) {
  const time = Date.parse(value || '')
  if (!Number.isFinite(time)) return ''
  return new Date(time).toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(value / 1024))} KB`
}
