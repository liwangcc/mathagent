import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { ArrowDown, Pause, Play } from "lucide-react"

import { drawOverlay, type OverlayPalette } from "@/lib/atlas/overlay"
import { createPointRenderer, type Palette, type PointRenderer, type RGB } from "@/lib/atlas/points"
import { buildScene, POINT_COUNT, type Fonts, type Layout, type Scene } from "@/lib/atlas/targets"
import { ACT_REST, frameAt, restingFrame } from "@/lib/atlas/timeline"
import { clamp, usePrefersReducedMotion } from "@/lib/hooks"
import { cn } from "@/lib/utils"

/* 墨迹星图: one answer sheet's journey told with 30,000 ink points. Scrolling moves the same
 * points from a student's handwriting to its transcription and verdicts, out into a star chart of
 * a class's mistakes, into a 50 x 600 ledger of open wrong records, and into the 2,000 questions
 * they merge into when a new paper arrives. */

const ACTS = [
  {
    mark: "读",
    title: ["字迹离开纸面，", "只变成它本来的样子。"],
    body: "识别只转录、不改写：学生写 2/4 就记 2/4，写错的步骤原样保留，对错留给判分。",
  },
  {
    mark: "判",
    title: ["逐题判定，", "看不清的不猜。"],
    body: "选择题机械比对，其余交给数学判分。字迹模糊、证据不足的题停下来，连同原卷交给老师确认。",
  },
  {
    mark: "汇",
    title: ["一个班的错题，", "汇成一张星图。"],
    body: "每颗星是高中课程目录里的一个知识点，越亮错得越多。星图里的错题分布为示意数据。",
  },
  {
    mark: "追",
    title: ["新试卷到来，", "按题目指纹反查到人。"],
    body: "30,000 条未纠正错题先按题目归并成约 2,000 道，再与新卷 25 题一次比对，不必逐人逐条扫描。",
    metrics: ["30,000 条 → 约 2,000 道 → 25 题", "模拟规模实测 0.63 秒 · 原做法约 1.5 分钟"],
  },
  {
    mark: "见",
    title: ["AI 给建议，", "老师做决定。"],
    body: "重复的步骤交给系统，需要判断的那几题留给老师。",
  },
]

/* Auto-play crosses the whole section in this many seconds — long enough to read every caption. */
const TOUR_SECONDS = 42

function cssColor(probe: CanvasRenderingContext2D, value: string): RGB {
  probe.fillStyle = "#000"
  probe.fillStyle = value.trim() || "#000"
  const normalized = String(probe.fillStyle)
  if (normalized.startsWith("#")) {
    const hex = normalized.slice(1)
    return [0, 2, 4].map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255) as RGB
  }
  const channels = normalized.match(/[\d.]+/g)?.slice(0, 3).map(Number) ?? [0, 0, 0]
  return channels.map((channel) => channel / 255) as RGB
}

function readPalettes(section: HTMLElement) {
  const style = getComputedStyle(section)
  const value = (name: string) => style.getPropertyValue(name).trim()
  const probe = document.createElement("canvas").getContext("2d")
  const overlay: OverlayPalette = {
    night: value("--atlas-bg"),
    paper: value("--atlas-paper"),
    paperInk: value("--atlas-paper-ink"),
    paperRule: value("--atlas-paper-rule"),
    pen: value("--atlas-pen"),
    ink: value("--atlas-ink"),
    muted: value("--atlas-muted"),
    rule: value("--atlas-rule"),
    accent: value("--atlas-accent"),
    correct: value("--atlas-correct"),
    partial: value("--atlas-partial"),
    wrong: value("--atlas-wrong"),
    pending: value("--atlas-pending"),
  }
  const rgb = (name: string) => (probe ? cssColor(probe, value(name)) : ([1, 1, 1] as RGB))
  const points: Palette = {
    pen: rgb("--atlas-pen"),
    ink: rgb("--atlas-ink"),
    dim: rgb("--atlas-dim"),
    correct: rgb("--atlas-correct"),
    partial: rgb("--atlas-partial"),
    wrong: rgb("--atlas-wrong"),
    pending: rgb("--atlas-pending"),
    accent: rgb("--atlas-accent"),
  }
  return { overlay, points }
}

function readFonts(): Fonts {
  const style = getComputedStyle(document.documentElement)
  const font = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback
  return { hand: font("--font-hand", "serif"), serif: font("--font-serif", "serif"), body: font("--font-body", "sans-serif") }
}

/** The plate sits beside the captions on wide screens and under them on narrow ones. */
function measureLayout(stage: HTMLElement, copy: HTMLElement): Layout {
  const stageBox = stage.getBoundingClientRect()
  const copyBox = copy.getBoundingClientRect()
  const compact = stageBox.width < 900
  const nav = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--nav-h")) || 68
  const gutter = compact ? 18 : Math.max(28, stageBox.width * 0.035)
  if (compact) {
    const y = copyBox.bottom - stageBox.top + 18
    return { width: stageBox.width, height: stageBox.height, compact, plate: { x: gutter, y, w: stageBox.width - gutter * 2, h: Math.max(160, stageBox.height - y - 46) } }
  }
  const x = copyBox.right - stageBox.left + 48
  const y = nav + 30
  return { width: stageBox.width, height: stageBox.height, compact, plate: { x, y, w: stageBox.width - x - gutter, h: stageBox.height - y - 58 } }
}

export function InkAtlas() {
  const reduced = usePrefersReducedMotion()
  const sectionRef = useRef<HTMLElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const copyRef = useRef<HTMLDivElement>(null)
  const underRef = useRef<HTMLCanvasElement>(null)
  const overRef = useRef<HTMLCanvasElement>(null)
  const pointsRef = useRef<HTMLCanvasElement>(null)
  const fpsRef = useRef<HTMLSpanElement>(null)
  const tourRef = useRef<{ start: () => void; stop: () => void } | null>(null)
  const actRef = useRef(0)
  const [act, setAct] = useState(0)
  const [touring, setTouring] = useState(false)
  const [supported, setSupported] = useState(true)
  const [showFps, setShowFps] = useState(false)

  useLayoutEffect(() => {
    const section = sectionRef.current
    const stage = stageRef.current
    const copy = copyRef.current
    const under = underRef.current
    const over = overRef.current
    const canvas = pointsRef.current
    const underCtx = under?.getContext("2d")
    const overCtx = over?.getContext("2d")
    if (!section || !stage || !copy || !under || !over || !canvas || !underCtx || !overCtx) return
    const layers = { under: underCtx, over: overCtx }

    let renderer: PointRenderer | null = null
    try {
      renderer = createPointRenderer(canvas)
    } catch {
      renderer = null
    }
    if (!renderer) {
      setSupported(false)
      return
    }
    const gl = renderer

    let scene: Scene | null = null
    let palettes = readPalettes(section)
    let ratio = 1
    let drawnProgress = -1
    let frameRequest = 0
    let inView = false
    let pageVisible = !document.hidden
    let touringNow = false
    let tourClock = 0
    let fpsFrames = 0
    let fpsSince = performance.now()
    const pointer = { x: -1e4, y: -1e4, strength: 0, target: 0 }

    const progress = () => {
      const box = section.getBoundingClientRect()
      const travel = box.height - window.innerHeight
      return travel > 0 ? clamp(-box.top / travel) : 0
    }

    const render = (now: number) => {
      if (!scene) return
      const frame = reduced ? restingFrame(progress()) : frameAt(progress())
      if (frame.progress !== drawnProgress) {
        drawnProgress = frame.progress
        drawOverlay(layers, scene, frame, palettes.overlay, ratio)
        section.style.setProperty("--p", frame.progress.toFixed(4))
        if (frame.act !== actRef.current) {
          actRef.current = frame.act
          setAct(frame.act)
        }
      }
      pointer.strength += (pointer.target - pointer.strength) * 0.12
      gl.draw(
        {
          frame,
          time: now / 1000,
          plate: scene.layout.plate,
          pointer,
          motion: reduced ? 0 : 1,
          pointSize: scene.layout.compact ? 1.35 : 1.55,
        },
        palettes.points,
      )
      fpsFrames += 1
      if (now - fpsSince > 500) {
        if (fpsRef.current) fpsRef.current.textContent = ` · ${Math.round((fpsFrames * 1000) / (now - fpsSince))} fps`
        fpsFrames = 0
        fpsSince = now
      }
    }

    const sectionTop = () => section.getBoundingClientRect().top + window.scrollY
    const travel = () => section.offsetHeight - window.innerHeight

    const advanceTour = (now: number) => {
      const elapsed = tourClock ? Math.min(0.05, (now - tourClock) / 1000) : 0
      tourClock = now
      const end = sectionTop() + travel()
      const next = Math.min(end, window.scrollY + (travel() / TOUR_SECONDS) * elapsed)
      window.scrollTo({ top: next, behavior: "instant" })
      if (next >= end - 0.5) stopTour()
    }

    const running = () => inView && pageVisible && !reduced
    const loop = (now: number) => {
      frameRequest = 0
      if (touringNow) advanceTour(now)
      render(now)
      if (running()) frameRequest = requestAnimationFrame(loop)
    }
    const paintOnce = (now: number) => {
      frameRequest = 0
      render(now)
    }
    const wake = () => {
      if (frameRequest) return
      // Reduced motion paints only when the scroll position asks for a new frame.
      frameRequest = requestAnimationFrame(reduced ? paintOnce : loop)
    }

    const rebuild = () => {
      const box = stage.getBoundingClientRect()
      if (box.width < 10 || box.height < 10) return
      ratio = Math.min(window.devicePixelRatio || 1, 2)
      for (const layer of [under, over]) {
        layer.width = Math.round(box.width * ratio)
        layer.height = Math.round(box.height * ratio)
      }
      gl.resize(box.width, box.height, ratio)
      const built = buildScene(measureLayout(stage, copy), readFonts())
      scene = built.scene
      gl.upload(built.buffers)
      drawnProgress = -1
      render(performance.now())
    }

    function stopTour() {
      if (!touringNow) return
      touringNow = false
      setTouring(false)
    }
    tourRef.current = {
      start() {
        if (progress() > 0.97) window.scrollTo({ top: sectionTop(), behavior: "instant" })
        touringNow = true
        tourClock = 0
        setTouring(true)
        wake()
      },
      stop: stopTour,
    }

    // The 30,000-point layout is built only when the section comes near, after fonts are ready,
    // so visitors who never scroll this far pay nothing for it.
    let disposed = false
    const approach = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return
        approach.disconnect()
        const fontsReady = document.fonts?.ready ?? Promise.resolve()
        fontsReady.then(() => {
          if (!disposed) rebuild()
        })
      },
      { rootMargin: "150% 0px 150% 0px" },
    )
    approach.observe(section)

    let resizeTimer = 0
    const resizeObserver = new ResizeObserver(() => {
      if (!scene) return
      window.clearTimeout(resizeTimer)
      resizeTimer = window.setTimeout(rebuild, 140)
    })
    resizeObserver.observe(stage)

    const visibility = new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting
      if (inView) wake()
      else stopTour()
    })
    visibility.observe(section)

    const onVisibility = () => {
      pageVisible = !document.hidden
      if (pageVisible) wake()
    }
    document.addEventListener("visibilitychange", onVisibility)
    const onScroll = () => wake()
    window.addEventListener("scroll", onScroll, { passive: true })

    // Any reading gesture hands the page back to the reader.
    const interrupt = () => stopTour()
    const onKey = (event: KeyboardEvent) => {
      if (["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End", " "].includes(event.key)) stopTour()
      const typing = event.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)
      if (inView && !typing && !event.metaKey && !event.ctrlKey && !event.altKey && event.key.toLowerCase() === "f") {
        setShowFps((value) => !value)
      }
    }
    window.addEventListener("wheel", interrupt, { passive: true })
    window.addEventListener("touchstart", interrupt, { passive: true })
    window.addEventListener("keydown", onKey)

    const onPointerMove = (event: PointerEvent) => {
      const box = stage.getBoundingClientRect()
      pointer.x = event.clientX - box.left
      pointer.y = event.clientY - box.top
      pointer.target = reduced || event.pointerType === "touch" ? 0 : 1
    }
    const onPointerLeave = () => {
      pointer.target = 0
    }
    stage.addEventListener("pointermove", onPointerMove)
    stage.addEventListener("pointerleave", onPointerLeave)

    // Accent theme and colour mode change the inks; re-read them and repaint.
    const themeObserver = new MutationObserver(() => {
      palettes = readPalettes(section)
      drawnProgress = -1
      wake()
    })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-color-mode"] })

    const onContextLost = (event: Event) => {
      event.preventDefault()
      setSupported(false)
    }
    canvas.addEventListener("webglcontextlost", onContextLost)

    return () => {
      disposed = true
      approach.disconnect()
      tourRef.current = null
      cancelAnimationFrame(frameRequest)
      window.clearTimeout(resizeTimer)
      resizeObserver.disconnect()
      visibility.disconnect()
      themeObserver.disconnect()
      document.removeEventListener("visibilitychange", onVisibility)
      window.removeEventListener("scroll", onScroll)
      window.removeEventListener("wheel", interrupt)
      window.removeEventListener("touchstart", interrupt)
      window.removeEventListener("keydown", onKey)
      stage.removeEventListener("pointermove", onPointerMove)
      stage.removeEventListener("pointerleave", onPointerLeave)
      canvas.removeEventListener("webglcontextlost", onContextLost)
      gl.dispose()
    }
  }, [reduced])

  const jumpTo = useCallback(
    (index: number) => {
      const section = sectionRef.current
      if (!section) return
      tourRef.current?.stop()
      const travel = section.offsetHeight - window.innerHeight
      const top = section.getBoundingClientRect().top + window.scrollY + ACT_REST[index] * travel
      window.scrollTo({ top, behavior: reduced ? "auto" : "smooth" })
    },
    [reduced],
  )

  useEffect(() => {
    if (!supported) setAct(0)
  }, [supported])

  return (
    <section
      ref={sectionRef}
      id="atlas"
      className={cn("atlas", reduced && "is-static", !supported && "is-fallback")}
      aria-labelledby="atlas-title"
    >
      <div ref={stageRef} className="atlas-stage">
        <canvas ref={underRef} className="atlas-canvas" aria-hidden="true" />
        <canvas ref={pointsRef} className="atlas-canvas" aria-hidden="true" />
        <canvas ref={overRef} className="atlas-canvas" aria-hidden="true" />

        <div className="wrap atlas-layout">
          <div ref={copyRef} className="atlas-copy">
            <p className="kicker atlas-kicker">
              <span className="kicker-num">图</span>
              <span id="atlas-title">墨迹星图 · 一份作答的去处</span>
            </p>

            <ol className="atlas-steps">
              {ACTS.map((item, index) => (
                <li
                  key={item.mark}
                  className={cn("atlas-step", act === index && "is-current")}
                  aria-current={act === index ? "step" : undefined}
                >
                  <span className="atlas-mark" aria-hidden="true">
                    {item.mark}
                  </span>
                  <h2 className="atlas-title">
                    {item.title.map((line) => (
                      <span key={line}>{line}</span>
                    ))}
                  </h2>
                  <p className="atlas-body">{item.body}</p>
                  {item.metrics && (
                    <p className="atlas-metrics">
                      {item.metrics.map((metric) => (
                        <span key={metric}>{metric}</span>
                      ))}
                    </p>
                  )}
                </li>
              ))}
            </ol>

            <nav className="atlas-rail" aria-label="墨迹星图章节">
              {ACTS.map((item, index) => (
                <button
                  key={item.mark}
                  type="button"
                  className={cn(act === index && "is-current")}
                  onClick={() => jumpTo(index)}
                  aria-label={`跳到「${item.mark}」：${item.title.join("")}`}
                >
                  {item.mark}
                </button>
              ))}
              <i aria-hidden="true">
                <b />
              </i>
            </nav>

            <div className="atlas-controls">
              {!reduced && (
                <button
                  type="button"
                  className="atlas-tour"
                  onClick={() => (touring ? tourRef.current?.stop() : tourRef.current?.start())}
                  aria-pressed={touring}
                >
                  {touring ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
                  {touring ? "停止自动演示" : "自动演示"}
                </button>
              )}
              <a className="atlas-skip" href="#flow">
                跳过
                <ArrowDown aria-hidden="true" />
              </a>
            </div>
          </div>
        </div>

        <p className="atlas-hud" aria-hidden="true">
          {POINT_COUNT.toLocaleString("en-US")} 个墨点 · WebGL 实时绘制
          {showFps && <span ref={fpsRef}> · — fps</span>}
        </p>
      </div>
    </section>
  )
}
