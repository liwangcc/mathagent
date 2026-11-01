// Short synthesized cues for the phone capture page.  Phone speakers are weak
// below ~1 kHz, so the shutter cue is a quick rising two-note "di-ding" in
// the 1.5-4 kHz band, triangle waves with a little square-wave edge, pushed
// through a compressor for loudness without clipping.  It stays ~160 ms so it
// is not tiresome when repeated.  The warning cue is lower, falling and
// rate-limited.  No vibration: a phone on a stand would shake the frame.

const WARN_INTERVAL_MS = 4000

let context = null
let output = null
let lastWarnAt = 0

function audioContext() {
  if (context) return context
  const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext
  if (!AudioContextClass) return null
  try {
    context = new AudioContextClass()
  } catch {
    context = null
  }
  return context
}

// Loudness chain: master gain into a fast compressor.  Exported, with
// scheduleCaptureCue, so the cue can be rendered offline and measured.
export function createCueOutput(ctx, destination = ctx.destination) {
  const compressor = ctx.createDynamicsCompressor()
  compressor.threshold.value = -14
  compressor.knee.value = 6
  compressor.ratio.value = 6
  compressor.attack.value = 0.001
  compressor.release.value = 0.08
  const master = ctx.createGain()
  master.gain.value = 1.6
  master.connect(compressor).connect(destination)
  return master
}

// Must run inside a user gesture (a tap) or browsers keep the audio muted.
export function unlockCaptureSound() {
  // iOS mutes web audio with the ring/silent switch unless the page asks for
  // a playback session (Safari 16.4+); a capture cue must be heard.
  try {
    if (globalThis.navigator?.audioSession) globalThis.navigator.audioSession.type = 'playback'
  } catch {
    // Older Safari: cues follow the silent switch.
  }
  const ctx = audioContext()
  if (ctx && !output) output = createCueOutput(ctx)
  if (ctx?.state === 'suspended') ctx.resume().catch(() => {})
}

function note(ctx, destination, { at, frequency, duration, gain, edge = 0.18 }) {
  const envelope = ctx.createGain()
  envelope.gain.setValueAtTime(0.0001, at)
  envelope.gain.exponentialRampToValueAtTime(gain, at + 0.003)
  envelope.gain.setValueAtTime(gain, at + duration * 0.35)
  envelope.gain.exponentialRampToValueAtTime(0.0001, at + duration)
  envelope.connect(destination)
  for (const [type, level] of [['triangle', 1], ['square', edge]]) {
    const oscillator = ctx.createOscillator()
    const mix = ctx.createGain()
    oscillator.type = type
    oscillator.frequency.setValueAtTime(frequency, at)
    mix.gain.value = level
    oscillator.connect(mix).connect(envelope)
    oscillator.start(at)
    oscillator.stop(at + duration + 0.02)
  }
}

// Schedule a cue on any (offline or live) context.
export function scheduleCaptureCue(ctx, destination, kind = 'shutter', at = ctx.currentTime) {
  if (kind === 'warn') {
    note(ctx, destination, { at, frequency: 880, duration: 0.12, gain: 0.8 })
    note(ctx, destination, { at: at + 0.15, frequency: 659, duration: 0.14, gain: 0.8 })
    return
  }
  note(ctx, destination, { at, frequency: 1568, duration: 0.07, gain: 0.85 })
  note(ctx, destination, { at: at + 0.07, frequency: 2093, duration: 0.09, gain: 0.85 })
}

export function playCaptureCue(kind = 'shutter') {
  // Never create the context here: outside a tap browsers start it muted.
  const ctx = context
  if (!ctx || !output || ctx.state === 'closed') return
  if (kind === 'warn') {
    if (Date.now() - lastWarnAt < WARN_INTERVAL_MS) return
    lastWarnAt = Date.now()
  }
  const play = () => scheduleCaptureCue(ctx, output, kind, ctx.currentTime + 0.005)
  // A context unlocked by the last tap may still be resuming.
  if (ctx.state === 'suspended') ctx.resume().then(play, () => {})
  else play()
}
