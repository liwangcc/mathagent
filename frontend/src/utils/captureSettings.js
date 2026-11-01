// Phone capture preferences, remembered on this phone.
import { normalizeRotation } from './captureVision.js'

const SETTINGS_KEY = 'mathagent_capture_prefs_v1'
// Seconds the teacher can choose between two automatic shots.
export const COOLDOWN_CHOICES = [1, 2, 3, 4, 5]

const DEFAULTS = {
  shootAngle: 0, // interface angle, see shootingLayout
  auto: true, // shoot automatically
  sound: true, // capture cue
  hd: true, // full-resolution stills where the browser offers them
  torch: false, // switch the torch on with the camera
  cooldown: 2, // seconds from one photo to the next automatic shot
}
const SWITCHES = ['auto', 'sound', 'hd', 'torch']

export function loadCaptureSettings() {
  const settings = { ...DEFAULTS }
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')
    if (Number.isFinite(saved.shootAngle)) settings.shootAngle = normalizeRotation(saved.shootAngle)
    for (const key of SWITCHES) {
      if (typeof saved[key] === 'boolean') settings[key] = saved[key]
    }
    if (COOLDOWN_CHOICES.includes(saved.cooldown)) settings.cooldown = saved.cooldown
  } catch {
    // Preferences are a convenience; defaults are safe.
  }
  return settings
}

export function saveCaptureSettings(settings) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings))
  } catch {
    // Private mode or blocked storage: keep the in-memory choice only.
  }
}
