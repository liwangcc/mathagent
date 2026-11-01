import { useCallback, useEffect, useRef, useState } from "react"

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)"
const DARK_QUERY = "(prefers-color-scheme: dark)"

function subscribeMedia(query: string, onChange: (matches: boolean) => void) {
  if (typeof window === "undefined" || !window.matchMedia) return () => {}
  const media = window.matchMedia(query)
  const handler = () => onChange(media.matches)
  media.addEventListener("change", handler)
  return () => media.removeEventListener("change", handler)
}

function mediaMatches(query: string) {
  return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(query).matches
}

export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(() => mediaMatches(REDUCED_MOTION_QUERY))
  useEffect(() => subscribeMedia(REDUCED_MOTION_QUERY, setReduced), [])
  return reduced
}

/** Reports whether the element has entered the viewport; with `once` it stays true afterwards. */
export function useInView<T extends Element>({
  once = true,
  rootMargin = "0px 0px -12% 0px",
  threshold = 0.15,
}: { once?: boolean; rootMargin?: string; threshold?: number } = {}) {
  const ref = useRef<T>(null)
  const [inView, setInView] = useState(false)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    if (typeof IntersectionObserver === "undefined") {
      setInView(true)
      return
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true)
          if (once) observer.disconnect()
        } else if (!once) {
          setInView(false)
        }
      },
      { rootMargin, threshold },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [once, rootMargin, threshold])

  return [ref, inView] as const
}

/** Marks every `[data-reveal]` element with `data-revealed` the first time it scrolls into view.
 * A data attribute, not a class, so React re-rendering `className` never clears it. */
export function useRevealOnScroll() {
  useEffect(() => {
    const elements = Array.from(document.querySelectorAll<HTMLElement>("[data-reveal]"))
    if (typeof IntersectionObserver === "undefined") {
      elements.forEach((element) => element.setAttribute("data-revealed", ""))
      return
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          entry.target.setAttribute("data-revealed", "")
          observer.unobserve(entry.target)
        }
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.12 },
    )
    elements.forEach((element) => observer.observe(element))
    return () => observer.disconnect()
  }, [])
}

/** The id of the last navigation target whose top has passed the upper third of the viewport. */
export function useScrollSpy(ids: string[]) {
  const [active, setActive] = useState<string | null>(null)

  useEffect(() => {
    let frame = 0
    const update = () => {
      frame = 0
      const line = window.innerHeight * 0.34
      let current: string | null = null
      for (const id of ids) {
        const element = document.getElementById(id)
        if (element && element.getBoundingClientRect().top <= line) current = id
      }
      const download = document.getElementById("download")
      if (download && download.getBoundingClientRect().top <= line) current = null
      setActive(current)
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update)
    }
    update()
    window.addEventListener("scroll", schedule, { passive: true })
    window.addEventListener("resize", schedule)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener("scroll", schedule)
      window.removeEventListener("resize", schedule)
    }
  }, [ids])

  return active
}

export type ColorMode = "system" | "light" | "dark"
export type AccentTheme = "song" | "qing" | "dai"

const COLOR_MODE_KEY = "mathagent-showcase-color-mode"
const THEME_KEY = "mathagent-showcase-theme"

function readStored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key)
    return value && (allowed as readonly string[]).includes(value) ? (value as T) : fallback
  } catch {
    return fallback
  }
}

function writeStored(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* Storage can be unavailable (private mode, blocked site data); the choice still applies for this visit. */
  }
}

/** Applies the stored appearance before the first render so an explicit dark choice never flashes light. */
export function applyStoredAppearance() {
  const root = document.documentElement
  root.dataset.colorMode = readStored<ColorMode>(COLOR_MODE_KEY, ["system", "light", "dark"], "system")
  root.dataset.theme = readStored<AccentTheme>(THEME_KEY, ["song", "qing", "dai"], "song")
}

/** Mirrors the workspace's appearance model: `data-color-mode` (system/light/dark) and `data-theme` accents. */
export function useAppearance() {
  const [mode, setModeState] = useState<ColorMode>(() =>
    readStored<ColorMode>(COLOR_MODE_KEY, ["system", "light", "dark"], "system"),
  )
  const [theme, setThemeState] = useState<AccentTheme>(() =>
    readStored<AccentTheme>(THEME_KEY, ["song", "qing", "dai"], "song"),
  )
  const [systemDark, setSystemDark] = useState(() => mediaMatches(DARK_QUERY))

  useEffect(() => subscribeMedia(DARK_QUERY, setSystemDark), [])

  useEffect(() => {
    document.documentElement.dataset.colorMode = mode
  }, [mode])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  const setMode = useCallback((next: ColorMode) => {
    setModeState(next)
    writeStored(COLOR_MODE_KEY, next)
  }, [])

  const setTheme = useCallback((next: AccentTheme) => {
    setThemeState(next)
    writeStored(THEME_KEY, next)
  }, [])

  const resolvedDark = mode === "dark" || (mode === "system" && systemDark)
  return { mode, setMode, theme, setTheme, resolvedDark }
}

export function clamp(value: number, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value))
}
