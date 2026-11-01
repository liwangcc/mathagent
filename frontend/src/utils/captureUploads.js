// Upload queue for the phone capture page.
//
// Strictly first-in-first-out: the server numbers photos in arrival order and
// the desktop imports them in that order, which identity inheritance relies
// on.  A failed photo therefore holds the queue until it is retried or
// deleted.  Each photo carries a client id, so a retry of an upload that did
// reach the server is not stored twice.
import { computed, ref } from 'vue'

const MAX_ATTEMPTS = 4
// The link no longer works: a wrong or replaced token, or an ended session.
const SESSION_GONE = new Set([403, 410])
// Retrying cannot help: a rejected photo, a full session, a photo too large.
const PERMANENT = new Set([400, 409, 413])

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// The server accepts 8-64 characters of [A-Za-z0-9_-].  randomUUID needs a
// secure context, and the system-camera fallback also runs over plain HTTP.
function newClientId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

// `upload(blob, clientId)` and `remove(seq)` talk to the server; errors carry
// the HTTP `status`.  `onSessionEnded(message)` is called once the link stops
// working, `onFailed()` whenever a photo gives up.
export function createCaptureUploads({ upload, remove, onSessionEnded, onFailed, wait = sleep }) {
  const shots = ref([])
  // The server had no room at the last try; clears once a photo fits or is deleted there.
  const sessionFull = ref(false)
  let ended = false
  let pumping = false

  const uploadedCount = computed(() => shots.value.filter((shot) => shot.status === 'done').length)
  const hasFailed = computed(() => shots.value.some((shot) => shot.status === 'failed'))
  const hasUnsent = computed(() => shots.value.some((shot) => shot.status !== 'done'))

  // `photo` is { blob, thumbUrl, info, mode }; the queue owns thumbUrl from now on.
  function add(photo) {
    shots.value.push({ ...photo, clientId: newClientId(), status: 'pending', attempts: 0, seq: null, error: '' })
    pump()
  }

  function retry(shot) {
    Object.assign(shot, { status: 'pending', attempts: 0, error: '' })
    pump()
  }

  // Resolves to false when the photo stays (still uploading, or the server
  // refused; shot.error then says why).
  async function discard(shot) {
    if (shot.status === 'uploading') return false
    if (shot.status === 'done') {
      try {
        await remove(shot.seq)
      } catch (err) {
        if (err.status === 403) {
          endSession(err.message)
          return false
        }
        // 410: already gone on the server.  Anything else keeps the photo.
        if (err.status !== 410) {
          shot.error = err.message
          return false
        }
      }
      sessionFull.value = false
    }
    URL.revokeObjectURL(shot.thumbUrl)
    shots.value = shots.value.filter((item) => item !== shot)
    pump()
    return true
  }

  function dispose() {
    for (const shot of shots.value) URL.revokeObjectURL(shot.thumbUrl)
  }

  // The oldest photo not yet on the server, unless it failed and holds the queue.
  function nextToSend() {
    const shot = shots.value.find((item) => item.status !== 'done')
    return shot?.status === 'pending' ? shot : null
  }

  async function pump() {
    if (pumping) return
    pumping = true
    try {
      while (!ended) {
        const shot = nextToSend()
        if (!shot) break
        await send(shot)
      }
    } finally {
      pumping = false
    }
  }

  async function send(shot) {
    shot.status = 'uploading'
    try {
      const result = await upload(shot.blob, shot.clientId)
      Object.assign(shot, { status: 'done', seq: result.image.seq, blob: null, error: '' })
      // It fit, so the teacher may have made room on the desktop.
      sessionFull.value = false
    } catch (err) {
      if (SESSION_GONE.has(err.status)) {
        shot.status = 'failed'
        endSession(err.message)
        return
      }
      shot.attempts += 1
      if (err.status === 409) sessionFull.value = true
      if (PERMANENT.has(err.status) || shot.attempts >= MAX_ATTEMPTS) {
        shot.status = 'failed'
        shot.error = err.status === 400 ? `${err.message}（请删除后重拍）` : err.message
        onFailed()
        return
      }
      shot.status = 'pending'
      await wait(1000 * 2 ** shot.attempts)
    }
  }

  function endSession(message) {
    ended = true
    onSessionEnded(message)
  }

  return { shots, sessionFull, uploadedCount, hasFailed, hasUnsent, add, retry, discard, dispose }
}
