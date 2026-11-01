// Both ends of the phone capture link.  The desktop keeps a pointer to the
// teacher's session in this tab's sessionStorage, so a reload can show the
// same QR code; the server keeps only a hash of the token.  The QR code
// carries the token in the URL fragment, and the phone moves it into its own
// sessionStorage.
import { withUploadSourcePath } from './uploadSource.js'

const KEY_PREFIX = 'mathagent_phone_capture:'
const PHONE_TOKEN_KEY_PREFIX = 'mathagent_capture_token:'

export function isCaptureSessionId(value) {
  return /^[a-f0-9]{24}$/.test(String(value || ''))
}

function storageKey(teacherId) {
  return teacherId ? `${KEY_PREFIX}${teacherId}` : ''
}

export function loadPhoneCaptureSession(teacherId) {
  const key = storageKey(teacherId)
  if (!key) return null
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null')
    if (isCaptureSessionId(saved?.session_id) && typeof saved.token === 'string' && saved.token) return saved
  } catch {
    // Corrupt or blocked storage simply means "no saved session".
  }
  return null
}

export function savePhoneCaptureSession(teacherId, sessionId, token) {
  const key = storageKey(teacherId)
  if (!key) return
  try {
    sessionStorage.setItem(key, JSON.stringify({ session_id: sessionId, token }))
  } catch {
    // Without storage the QR code is re-issued (token rotated) on next open.
  }
}

export function forgetPhoneCaptureSession(teacherId) {
  const key = storageKey(teacherId)
  if (!key) return
  try {
    sessionStorage.removeItem(key)
  } catch {
    // Nothing to clean up.
  }
}

// The address the phone opens.  The desktop app runs on tauri.localhost, so
// it must point the phone at the web origin the API lives on.
export function phoneCaptureOrigin(env = import.meta.env, location = window.location) {
  const apiOrigin = String(env?.VITE_API_ORIGIN || '').trim().replace(/\/$/, '')
  return apiOrigin || location.origin
}

export function phoneCaptureUrl(origin, sessionId, token) {
  // The token rides in the fragment, which browsers never send to the server.
  return `${origin}/capture/${encodeURIComponent(sessionId)}#t=${encodeURIComponent(token)}`
}

// Phone side: read the token from the fragment, keep it in this tab's
// sessionStorage so a reload still works, and clear it from the address bar.
export function takePhoneCaptureToken(sessionId, location = window.location, history = window.history) {
  const key = `${PHONE_TOKEN_KEY_PREFIX}${sessionId}`
  const fromLink = new URLSearchParams(location.hash.slice(1)).get('t') || ''
  if (fromLink) history.replaceState(history.state, '', location.pathname)
  try {
    if (fromLink) sessionStorage.setItem(key, fromLink)
    return fromLink || sessionStorage.getItem(key) || ''
  } catch {
    // Blocked storage: the token lasts until the page is reloaded.
    return fromLink
  }
}

export function phoneCaptureOriginWarning(origin) {
  let url
  try {
    url = new URL(origin)
  } catch {
    return '无法确定手机访问地址，请用正式域名打开本页。'
  }
  if (['localhost', '127.0.0.1', '[::1]', '0.0.0.0'].includes(url.hostname)) {
    return '当前是本机地址，手机无法打开。请在电脑上改用局域网 IP 或正式域名打开本页后再扫码。'
  }
  if (url.protocol !== 'https:') {
    return '当前不是 HTTPS 地址：手机浏览器不允许实时取景，只能用系统相机逐张拍照。'
  }
  return ''
}

// Photos are named by server sequence so the grading page sorts them in
// shooting order (identity inheritance depends on it).  ASCII names cannot
// collide with a student name in the roster.
export function phoneCaptureFileName(index, type = 'image/jpeg') {
  const extension = { 'image/png': '.png', 'image/webp': '.webp' }[type] || '.jpg'
  return `capture-${String(index + 1).padStart(4, '0')}${extension}`
}

export function phoneCaptureFiles(entries) {
  return [...entries]
    .sort((a, b) => a.seq - b.seq)
    .map((entry, index) => {
      const type = entry.blob.type || 'image/jpeg'
      const name = phoneCaptureFileName(index, type)
      return withUploadSourcePath(new File([entry.blob], name, { type, lastModified: Date.now() }), name)
    })
}
