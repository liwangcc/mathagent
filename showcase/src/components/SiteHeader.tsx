import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { ArrowRight, ArrowUpRight, Menu, Moon, Sun, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { WEB_APP_URL, navigation } from "@/config"
import { useScrollSpy, type useAppearance } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const SECTION_IDS = navigation.map((item) => item.href.slice(1))

type Appearance = ReturnType<typeof useAppearance>

export function SiteHeader({ appearance }: { appearance: Appearance }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [scrolled, setScrolled] = useState(false)
  const progressRef = useRef<HTMLSpanElement>(null)
  const linksRef = useRef<HTMLDivElement>(null)
  const [indicator, setIndicator] = useState({ left: 0, width: 0, visible: false })
  const active = useScrollSpy(SECTION_IDS)

  useEffect(() => {
    let frame = 0
    const update = () => {
      frame = 0
      const scrollable = document.documentElement.scrollHeight - window.innerHeight
      const progress = scrollable > 0 ? window.scrollY / scrollable : 0
      progressRef.current?.style.setProperty("--progress", progress.toFixed(4))
      setScrolled(window.scrollY > 8)
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
  }, [])

  // The moss underline slides to the active link instead of jumping.
  useLayoutEffect(() => {
    const container = linksRef.current
    const link = active ? container?.querySelector<HTMLAnchorElement>(`a[href="#${active}"]`) : null
    if (!container || !link) {
      setIndicator((value) => ({ ...value, visible: false }))
      return
    }
    setIndicator({ left: link.offsetLeft + 12, width: link.offsetWidth - 24, visible: true })
  }, [active])

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false)
    }
    window.addEventListener("keydown", closeOnEscape)
    return () => window.removeEventListener("keydown", closeOnEscape)
  }, [])

  const { resolvedDark, setMode } = appearance
  const toggleLabel = resolvedDark ? "切换为浅色" : "切换为深色"

  return (
    <header className={cn("site-nav", (scrolled || menuOpen) && "is-scrolled")}>
      <div className="wrap nav-inner">
        <a href="#top" className="nav-brand" aria-label="MathAgent 首页">
          <span className="logo-mark" aria-hidden="true">M</span>
          <span className="wordmark">
            MathAgent<i>.</i>
          </span>
        </a>

        <nav className="nav-links" aria-label="主导航">
          <div ref={linksRef} className="nav-links-track">
            {navigation.map((item) => (
              <a key={item.href} href={item.href} aria-current={active === item.href.slice(1) ? "true" : undefined}>
                {item.label}
              </a>
            ))}
            <span
              className="nav-indicator"
              aria-hidden="true"
              style={{
                transform: `translateX(${indicator.left}px)`,
                width: indicator.width,
                opacity: indicator.visible ? 1 : 0,
              }}
            />
          </div>
        </nav>

        <div className="nav-actions">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => setMode(resolvedDark ? "light" : "dark")}
            aria-label={toggleLabel}
            title={toggleLabel}
          >
            {resolvedDark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
          </Button>
          <a href="#download" className="nav-text-link">
            Windows
          </a>
          <Button asChild size="sm" className="nav-cta">
            <a href={WEB_APP_URL} target="_blank" rel="noreferrer">
              进入教师工作台
              <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="nav-menu-button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label={menuOpen ? "关闭导航" : "打开导航"}
            aria-expanded={menuOpen}
            aria-controls="mobile-menu"
          >
            {menuOpen ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
          </Button>
        </div>
      </div>

      <div id="mobile-menu" className={cn("mobile-menu", menuOpen && "is-open")} hidden={!menuOpen}>
        <nav className="wrap" aria-label="移动端导航">
          {navigation.map((item, index) => (
            <a key={item.href} href={item.href} onClick={() => setMenuOpen(false)}>
              <span className="mobile-menu-index">{String(index + 1).padStart(2, "0")}</span>
              {item.label}
              <ArrowRight aria-hidden="true" />
            </a>
          ))}
          <a href="#download" onClick={() => setMenuOpen(false)}>
            <span className="mobile-menu-index">—</span>
            Windows 客户端
            <ArrowRight aria-hidden="true" />
          </a>
          <a href={WEB_APP_URL} target="_blank" rel="noreferrer" className="mobile-menu-primary">
            进入教师工作台
            <ArrowUpRight aria-hidden="true" />
          </a>
        </nav>
      </div>

      <span ref={progressRef} className="nav-progress" aria-hidden="true" />
    </header>
  )
}
