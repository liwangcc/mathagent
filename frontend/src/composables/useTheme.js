// Theme system — 松间绿 song / 青墨 qing / 黛砚 dai
// Color mode is independent from the brand palette: light / dark / system.

const STORAGE_KEY = 'mathagent_theme'
const COLOR_MODE_STORAGE_KEY = 'mathagent_color_mode'
const THEMES = ['song', 'qing', 'dai']
const COLOR_MODES = ['light', 'dark', 'system']
const DEFAULT_THEME = 'song'
const DEFAULT_COLOR_MODE = 'light'

function normalize(name) {
  return THEMES.includes(name) ? name : DEFAULT_THEME
}

function normalizeColorMode(name) {
  return COLOR_MODES.includes(name) ? name : DEFAULT_COLOR_MODE
}

function readStorage(key) {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** Read the persisted brand palette (falls back to song when absent/invalid). */
export function getTheme() {
  return normalize(readStorage(STORAGE_KEY))
}

/** Apply the brand palette to the document root without persisting. */
export function applyTheme(name) {
  document.documentElement.setAttribute('data-theme', normalize(name))
}

/** Set + persist a brand palette, and apply it immediately. */
export function setTheme(name) {
  const theme = normalize(name)
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // persistence best-effort only
  }
  applyTheme(theme)
  return theme
}

/** Read the persisted color mode (light, dark, or system). */
export function getColorMode() {
  return normalizeColorMode(readStorage(COLOR_MODE_STORAGE_KEY))
}

/** Apply color mode; CSS resolves system mode through prefers-color-scheme. */
export function applyColorMode(name) {
  const mode = normalizeColorMode(name)
  const root = document.documentElement
  root.setAttribute('data-color-mode', mode)
  root.style.colorScheme = mode === 'system' ? 'light dark' : mode
}

/** Set + persist the color mode, and apply it immediately. */
export function setColorMode(name) {
  const mode = normalizeColorMode(name)
  try {
    localStorage.setItem(COLOR_MODE_STORAGE_KEY, mode)
  } catch {
    // persistence best-effort only
  }
  applyColorMode(mode)
  return mode
}

/** Boot init — apply persisted palette and color mode before Vue mounts. */
export function initTheme() {
  applyTheme(getTheme())
  applyColorMode(getColorMode())
}

export {
  THEMES,
  DEFAULT_THEME,
  COLOR_MODES,
  DEFAULT_COLOR_MODE,
}
