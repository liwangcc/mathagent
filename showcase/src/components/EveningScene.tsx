import { useEffect, useLayoutEffect, useRef, useState } from "react"

import { clamp, usePrefersReducedMotion } from "@/lib/hooks"
import { cn } from "@/lib/utils"

/* The old hero video — teachers working through the night under the stars, glowing laptops among
 * books and flowers — redrawn as an ink plate. Scrolling turns the clock back from 23:40 to 18:10:
 * the pile flies page by page into the "done" tray, the stars fade and the night sky becomes dusk. */

const SHEETS = 12
const PAGES = 48
const FLAGGED = new Set([4, 9])
const START_MINUTES = 23 * 60 + 40
const END_MINUTES = 18 * 60 + 10

const STEPS = [
  {
    time: "23:40",
    title: ["夜里十一点四十，", "桌上还剩 48 份。"],
    body: "对答案、数错题、登分、写评语——重复步骤占去的，往往是老师本该休息的夜晚。",
  },
  {
    time: "交给 MathAgent",
    title: ["逐页识别，逐题批阅。"],
    body: "100 页以内，系统流程通常 20 分钟内完成；更多时间花在拍照、检查与上传。",
  },
  {
    time: "18:10",
    title: ["留给老师的，", "是真正需要判断的那几题。"],
    body: "字迹不清、身份存疑、判分临界的题目会停下来等老师确认；其余结果已整理成这一批的学情。",
  },
]

const STARS: Array<[number, number, number]> = [
  [140, 104, 4.5],
  [214, 92, 3],
  [318, 112, 4],
  [374, 150, 3],
  [128, 178, 3],
  [298, 168, 3.5],
  [196, 146, 2.5],
  [352, 226, 2.5],
  [164, 236, 3],
]

const SKY_WINDOWS: Array<[number, number]> = [
  [142, 278],
  [204, 270],
  [218, 284],
  [278, 286],
  [336, 278],
]

const smooth = (from: number, to: number, value: number) => {
  const t = clamp((value - from) / (to - from))
  return t * t * (3 - 2 * t)
}

const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

/* Deterministic jitter so the pile looks hand-stacked but never reshuffles between frames. */
const jitter = (index: number, salt: number) => {
  const value = Math.sin(index * 12.9898 + salt * 78.233) * 43758.5453
  return value - Math.floor(value) - 0.5
}

function sheetTransform(index: number, p: number) {
  const start = 0.16 + index * 0.05
  const q = clamp((p - start) / 0.12)
  const stackX = 300 + jitter(index, 1) * 6
  const stackY = 464 - (SHEETS - 1 - index) * 4.4
  const trayX = 740 + jitter(index, 2) * 6
  const trayY = 464 - index * 3.4
  if (q <= 0) return { transform: `translate(${stackX} ${stackY}) rotate(${jitter(index, 3) * 3}) scale(1 0.035)`, q }
  if (q >= 1) return { transform: `translate(${trayX} ${trayY}) rotate(${jitter(index, 4) * 3}) scale(1 0.035)`, q }
  const e = easeInOut(q)
  const cx = 520
  const cy = 60
  const x = (1 - e) * (1 - e) * stackX + 2 * (1 - e) * e * cx + e * e * trayX
  const y = (1 - e) * (1 - e) * stackY + 2 * (1 - e) * e * cy + e * e * trayY
  const lift = Math.sin(Math.PI * e)
  const rotate = -6 + 10 * e - lift * 8
  return { transform: `translate(${x.toFixed(1)} ${y.toFixed(1)}) rotate(${rotate.toFixed(2)}) scale(1 ${Math.max(0.035, lift).toFixed(3)})`, q }
}

const formatTime = (minutes: number) => {
  const whole = Math.round(minutes)
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`
}

export function EveningScene() {
  const reduced = usePrefersReducedMotion()
  const sectionRef = useRef<HTMLElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const sheetRefs = useRef<Array<SVGGElement | null>>([])
  const [step, setStep] = useState(0)
  const [active, setActive] = useState(false)
  const stepRef = useRef(0)

  useLayoutEffect(() => {
    const section = sectionRef.current
    const svg = svgRef.current
    if (!section || !svg) return
    const pick = <T extends Element>(selector: string) => svg.querySelector<T>(selector)
    const nodes = {
      dim: pick<SVGPathElement>("[data-part=dim]"),
      nightSky: pick<SVGRectElement>("[data-part=night-sky]"),
      nightCity: pick<SVGPathElement>("[data-part=night-city]"),
      stars: pick<SVGGElement>("[data-part=stars]"),
      lights: pick<SVGGElement>("[data-part=city-lights]"),
      moon: pick<SVGGElement>("[data-part=moon]"),
      sun: pick<SVGCircleElement>("[data-part=sun]"),
      cone: pick<SVGPathElement>("[data-part=cone]"),
      screen: pick<SVGRectElement>("[data-part=screen]"),
      hour: pick<SVGLineElement>("[data-part=hour]"),
      minute: pick<SVGLineElement>("[data-part=minute]"),
      clock: pick<SVGTextElement>("[data-part=clock-text]"),
      bar: pick<SVGRectElement>("[data-part=bar]"),
      laptopText: pick<SVGTextElement>("[data-part=laptop-text]"),
      pending: pick<SVGTextElement>("[data-part=pending]"),
      done: pick<SVGTextElement>("[data-part=done]"),
    }

    const render = (p: number) => {
      section.style.setProperty("--p", p.toFixed(4))
      const night = 1 - smooth(0.42, 0.84, p)
      nodes.dim?.setAttribute("opacity", (night * 0.5).toFixed(3))
      nodes.nightSky?.setAttribute("opacity", night.toFixed(3))
      nodes.nightCity?.setAttribute("opacity", night.toFixed(3))
      nodes.stars?.setAttribute("opacity", (1 - smooth(0.36, 0.74, p)).toFixed(3))
      nodes.lights?.setAttribute("opacity", (1 - smooth(0.5, 0.86, p)).toFixed(3))
      nodes.moon?.setAttribute("transform", `translate(0 ${(smooth(0.26, 0.9, p) * 230).toFixed(1)})`)
      nodes.sun?.setAttribute("transform", `translate(0 ${((1 - smooth(0.55, 0.96, p)) * 96).toFixed(1)})`)
      nodes.cone?.setAttribute("opacity", ((1 - smooth(0.62, 0.88, p)) * 0.36).toFixed(3))
      nodes.screen?.setAttribute("opacity", night.toFixed(3))

      const minutes = START_MINUTES - (START_MINUTES - END_MINUTES) * easeInOut(clamp((p - 0.1) / 0.8))
      nodes.minute?.setAttribute("transform", `rotate(${((minutes % 60) * 6).toFixed(2)} 700 150)`)
      nodes.hour?.setAttribute("transform", `rotate(${(((minutes / 60) % 12) * 30).toFixed(2)} 700 150)`)
      if (nodes.clock) nodes.clock.textContent = formatTime(minutes)

      let landed = 0
      sheetRefs.current.forEach((sheet, index) => {
        if (!sheet) return
        const { transform, q } = sheetTransform(index, p)
        sheet.setAttribute("transform", transform)
        sheet.classList.toggle("is-checked", q > 0.42)
        if (q >= 1) landed += 1
        else if (q > 0) landed += q
      })
      const fraction = landed / SHEETS
      const pagesDone = Math.round(fraction * PAGES)
      nodes.bar?.setAttribute("width", (144 * fraction).toFixed(1))
      if (nodes.laptopText) nodes.laptopText.textContent = fraction >= 1 ? "已完成 · 待复核交给老师" : `批阅中 · ${pagesDone} / ${PAGES}`
      if (nodes.pending) nodes.pending.textContent = `待批 ${PAGES - pagesDone}`
      if (nodes.done) nodes.done.textContent = `已批阅 ${pagesDone}`

      const nextStep = p < 0.3 ? 0 : p < 0.72 ? 1 : 2
      if (nextStep !== stepRef.current) {
        stepRef.current = nextStep
        setStep(nextStep)
      }
    }

    if (reduced) {
      render(1)
      return
    }

    let frame = 0
    const update = () => {
      frame = 0
      const box = section.getBoundingClientRect()
      const travel = box.height - window.innerHeight
      render(travel > 0 ? clamp(-box.top / travel) : 0)
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update)
    }

    update()
    let listening = false
    const listen = (on: boolean) => {
      if (on === listening) return
      listening = on
      if (on) {
        window.addEventListener("scroll", schedule, { passive: true })
        window.addEventListener("resize", schedule)
        schedule()
      } else {
        window.removeEventListener("scroll", schedule)
        window.removeEventListener("resize", schedule)
      }
    }
    const observer =
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver(([entry]) => {
            listen(entry.isIntersecting)
            setActive(entry.isIntersecting)
          })
    if (observer) observer.observe(section)
    else listen(true)

    return () => {
      observer?.disconnect()
      listen(false)
      cancelAnimationFrame(frame)
    }
  }, [reduced])

  useEffect(() => {
    if (reduced) setStep(2)
  }, [reduced])

  return (
    <section
      ref={sectionRef}
      id="evening"
      className={cn("evening", reduced && "is-static", active && "is-active")}
      aria-labelledby="evening-title"
    >
      <div className="evening-sticky">
        <div className="wrap evening-layout">
          <div className="evening-copy">
            <p className="kicker evening-kicker">
              <span className="kicker-num">序</span>
              <span id="evening-title">把夜晚还给老师</span>
            </p>

            <ol className="evening-steps">
              {STEPS.map((item, index) => (
                <li key={item.time} className={cn("evening-step", step === index && "is-current")} aria-current={step === index ? "step" : undefined}>
                  <span className={cn("evening-time", !/^\d/.test(item.time) && "is-word")}>{item.time}</span>
                  <h2 className="evening-title">
                    {item.title.map((line) => (
                      <span key={line}>{line}</span>
                    ))}
                  </h2>
                  <p className="evening-body">{item.body}</p>
                </li>
              ))}
            </ol>

            <div className="evening-rail" aria-hidden="true">
              <span>23:40</span>
              <i>
                <b />
              </i>
              <span>18:10</span>
            </div>
          </div>

          <div className="evening-plate">
            <svg ref={svgRef} viewBox="0 0 960 640" role="img" aria-label="插画：深夜书桌上的一摞试卷被逐页批阅，时钟从 23:40 回到 18:10，窗外由星夜变为黄昏">
              <defs>
                <clipPath id="pane-clip">
                  <rect x="106" y="74" width="292" height="248" />
                </clipPath>
                <mask id="moon-cut">
                  <rect x="0" y="0" width="960" height="640" fill="#fff" />
                  <circle cx="182" cy="120" r="21" fill="#000" />
                </mask>
              </defs>

              <rect className="ev-plate" x="0" y="0" width="960" height="640" />

              {/* Window: dusk underneath, night painted over it */}
              <g clipPath="url(#pane-clip)">
                <rect className="ev-dusk" x="106" y="74" width="292" height="248" />
                <rect className="ev-dusk-low" x="106" y="236" width="292" height="86" />
                <circle data-part="sun" className="ev-sun" cx="300" cy="292" r="30" />
                <rect data-part="night-sky" className="ev-night-sky" x="106" y="74" width="292" height="248" />
                <path className="ev-city" d="M106 322 V288 H136 V270 H160 V292 H192 V262 L212 248 L232 262 V296 H266 V278 H300 V300 H326 V266 H352 V290 H398 V322 Z" />
                <path data-part="night-city" className="ev-night-city" d="M106 322 V288 H136 V270 H160 V292 H192 V262 L212 248 L232 262 V296 H266 V278 H300 V300 H326 V266 H352 V290 H398 V322 Z" />
              </g>
              <rect className="ev-line ev-sill" x="82" y="332" width="340" height="10" rx="2" />

              {/* Wall clock */}
              <circle className="ev-line ev-face" cx="700" cy="150" r="58" />
              {Array.from({ length: 12 }, (_, index) => (
                <line
                  key={index}
                  className={cn("ev-tick", index % 3 === 0 && "is-major")}
                  x1="700"
                  y1={index % 3 === 0 ? 100 : 103}
                  x2="700"
                  y2="108"
                  transform={`rotate(${index * 30} 700 150)`}
                />
              ))}
              <line data-part="hour" className="ev-hand is-hour" x1="700" y1="154" x2="700" y2="120" />
              <line data-part="minute" className="ev-hand" x1="700" y1="156" x2="700" y2="104" />
              <circle className="ev-pin" cx="700" cy="150" r="4.5" />
              <text data-part="clock-text" className="ev-clock-text" x="700" y="240" textAnchor="middle">
                23:40
              </text>

              {/* Pinned graph-paper note */}
              <g transform="rotate(3 864 160)">
                <rect className="ev-note" x="812" y="98" width="104" height="124" rx="2" />
                <path className="ev-note-grid" d="M812 122 H916 M812 146 H916 M812 170 H916 M812 194 H916 M838 98 V222 M864 98 V222 M890 98 V222" />
                <path className="ev-note-curve" d="M820 112 C 840 196, 888 196, 908 112" />
                <circle className="ev-pin is-note" cx="864" cy="104" r="4" />
              </g>

              {/* Desk */}
              <line className="ev-floor" x1="0" y1="630" x2="960" y2="630" />
              <rect className="ev-line ev-desk" x="40" y="470" width="880" height="16" rx="2" />
              <rect className="ev-line ev-apron" x="60" y="486" width="840" height="34" />
              <rect className="ev-line ev-leg" x="78" y="520" width="14" height="110" />
              <rect className="ev-line ev-leg" x="868" y="520" width="14" height="110" />
              <text data-part="pending" className="ev-label" x="300" y="508" textAnchor="middle">
                待批 48
              </text>
              <text data-part="done" className="ev-label" x="740" y="508" textAnchor="middle">
                已批阅 0
              </text>

              {/* Book stack */}
              <rect className="ev-line ev-book bk-1" x="56" y="446" width="106" height="24" rx="2" />
              <rect className="ev-line ev-book bk-2" x="64" y="426" width="92" height="20" rx="2" />
              <rect className="ev-line ev-book bk-3" x="58" y="410" width="98" height="16" rx="2" />
              <rect className="ev-line ev-book bk-4" x="70" y="392" width="84" height="18" rx="2" />
              <path className="ev-line ev-thin" d="M70 446 V470 M78 426 V446 M72 410 V426 M84 392 V410" />

              {/* Lamp */}
              <path className="ev-line ev-lamp-base" d="M180 470 C 182 460, 238 460, 240 470 Z" />
              <path className="ev-line ev-arm" d="M210 462 L236 362 L316 318" />
              <circle className="ev-line ev-joint" cx="236" cy="362" r="4.5" />
              <path className="ev-line ev-shade" d="M300 312 C 312 296, 334 294, 344 304 L364 340 C 344 356, 322 356, 312 350 Z" />

              {/* Laptop */}
              <rect className="ev-line ev-laptop" x="440" y="338" width="180" height="124" rx="6" />
              <rect className="ev-screen-day" x="448" y="346" width="164" height="108" rx="2" />
              <path className="ev-line ev-laptop-base" d="M420 462 H640 L646 470 H414 Z" />

              {/* Paper stack, done tray */}
              <path className="ev-line ev-tray" d="M680 446 L686 470 H794 L800 446" />

              {/* The room dims at night except the lamp's pool of light. */}
              <path
                data-part="dim"
                className="ev-dim"
                fillRule="evenodd"
                d="M0 0 H960 V640 H0 Z M314 350 L364 340 L480 470 L238 470 Z"
              />
              <path data-part="cone" className="ev-cone" d="M314 350 L364 340 L480 470 L238 470 Z" />

              {/* Lit things sit above the dimming: stars, moon, windows across the street, the laptop screen. */}
              <g clipPath="url(#pane-clip)">
                <g data-part="stars" className="ev-stars">
                  {STARS.map(([x, y, r], index) => (
                    <path
                      key={index}
                      className="ev-star"
                      style={{ animationDelay: `${index * 0.37}s` }}
                      d={`M${x} ${y - r * 2} Q${x} ${y} ${x + r * 2} ${y} Q${x} ${y} ${x} ${y + r * 2} Q${x} ${y} ${x - r * 2} ${y} Q${x} ${y} ${x} ${y - r * 2} Z`}
                    />
                  ))}
                </g>
                <g data-part="moon">
                  <circle className="ev-moon" cx="170" cy="128" r="22" mask="url(#moon-cut)" />
                </g>
                <g data-part="city-lights" className="ev-city-lights">
                  {SKY_WINDOWS.map(([x, y]) => (
                    <rect key={`${x}-${y}`} x={x} y={y} width="6" height="7" />
                  ))}
                </g>
              </g>
              <rect className="ev-line" x="96" y="64" width="312" height="268" rx="3" />
              <rect className="ev-line" x="106" y="74" width="292" height="248" />
              <line className="ev-line" x1="252" y1="74" x2="252" y2="322" />
              <line className="ev-line" x1="106" y1="198" x2="398" y2="198" />

              {/* Sill plant with small flowers, for the video's flower field; they stay lit like the flowers there */}
              <g className="ev-plant" transform="translate(-232 0)">
                <path className="ev-leaf" d="M376 306 C 362 296, 356 282, 360 268 C 372 276, 378 290, 376 306 Z" />
                <path className="ev-leaf" d="M378 306 C 388 292, 400 286, 410 288 C 404 300, 392 306, 378 306 Z" />
                <path className="ev-leaf" d="M377 306 C 374 290, 378 274, 388 264 C 392 280, 386 296, 377 306 Z" />
                <line className="ev-line ev-thin" x1="369" y1="300" x2="356" y2="262" />
                <line className="ev-line ev-thin" x1="386" y1="298" x2="398" y2="258" />
                <circle className="ev-flower" cx="356" cy="258" r="5" />
                <circle className="ev-flower is-alt" cx="398" cy="254" r="5" />
                <circle className="ev-flower" cx="390" cy="262" r="3.5" />
                <path className="ev-pot" d="M362 306 H392 L387 332 H367 Z" />
              </g>

              <rect data-part="screen" className="ev-screen-night" x="448" y="346" width="164" height="108" rx="2" />
              <g className="ev-ui">
                <rect className="ev-ui-logo" x="458" y="356" width="12" height="12" rx="3" />
                <text className="ev-ui-title" x="476" y="366">
                  MathAgent
                </text>
                <rect className="ev-ui-row" x="458" y="380" width="120" height="5" rx="2.5" />
                <rect className="ev-ui-row" x="458" y="392" width="96" height="5" rx="2.5" />
                <text data-part="laptop-text" className="ev-ui-label" x="458" y="416">
                  批阅中 · 0 / 48
                </text>
                <rect className="ev-ui-track" x="458" y="426" width="144" height="5" rx="2.5" />
                <rect data-part="bar" className="ev-ui-bar" x="458" y="426" width="0" height="5" rx="2.5" />
              </g>

              {/* Mug with steam */}
              <path className="ev-line ev-mug-handle" d="M882 440 C 898 440, 898 462, 882 462" />
              <rect className="ev-line ev-mug" x="846" y="430" width="36" height="40" rx="4" />
              <path className="ev-steam" d="M856 420 C 850 410, 862 402, 856 390" />
              <path className="ev-steam is-late" d="M870 420 C 864 410, 876 402, 870 390" />

              {/* Sheets fly last so they pass in front of everything. */}
              {Array.from({ length: SHEETS }, (_, index) => (
                <g
                  key={index}
                  ref={(node) => {
                    sheetRefs.current[index] = node
                  }}
                  className={cn("ev-sheet", FLAGGED.has(index) && "is-flagged")}
                >
                  <rect x="-48" y="-62" width="96" height="124" rx="2" />
                  <path className="ev-sheet-lines" d={`M-36 -46 H${10 + jitter(index, 5) * 16} M-36 -30 H${28 + jitter(index, 6) * 14} M-36 -18 H${20 + jitter(index, 7) * 18} M-36 -6 H${30 + jitter(index, 8) * 10} M-36 6 H${8 + jitter(index, 9) * 18}`} />
                  {FLAGGED.has(index) ? (
                    <g className="ev-sheet-mark">
                      <circle cx="28" cy="36" r="11" />
                      <text x="28" y="41" textAnchor="middle">
                        ?
                      </text>
                    </g>
                  ) : (
                    <path className="ev-sheet-mark" d="M14 34 L22 43 L40 22" />
                  )}
                </g>
              ))}
            </svg>
          </div>
        </div>
      </div>
    </section>
  )
}
