import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { Pause, Play } from "lucide-react"

import { useInView, usePrefersReducedMotion } from "@/lib/hooks"
import { cn, cssVars } from "@/lib/utils"

type Verdict = "correct" | "partial" | "wrong"

interface SheetVariant {
  student: string
  page: number
  q1: { answer: string; verdict: Verdict }
  q2: { answer: string; verdict: Verdict }
  q3: { lines: string[]; verdict: Verdict }
}

/* Three desensitised pages from one batch; question 4 is always smudged and always goes to the teacher. */
const VARIANTS: SheetVariant[] = [
  {
    student: "学生 A",
    page: 12,
    q1: { answer: "B", verdict: "correct" },
    q2: { answer: "4/5", verdict: "correct" },
    q3: { lines: ["f'(x) = 2x − 2", "x > 1 时 f'(x) > 0", "增区间 (1, +∞)"], verdict: "partial" },
  },
  {
    student: "学生 B",
    page: 13,
    q1: { answer: "B", verdict: "correct" },
    q2: { answer: "3/4", verdict: "wrong" },
    q3: { lines: ["f'(x) = 2x − 2", "x < 1 时递减，x > 1 时递增", "减区间 (−∞, 1)，增区间 (1, +∞)"], verdict: "correct" },
  },
  {
    student: "学生 C",
    page: 14,
    q1: { answer: "C", verdict: "wrong" },
    q2: { answer: "4/5", verdict: "correct" },
    q3: { lines: ["f'(x) = 2x − 2", "x < 1 时递减，x > 1 时递增", "减区间 (−∞, 1)，增区间 (1, +∞)"], verdict: "correct" },
  },
]

const TOTAL_PAGES = 48

/* One beat per state change; CSS transitions animate between beats. */
const STEP_MS = [900, 1800, 900, 650, 650, 750, 1000, 1000, 950, 650, 2300, 700]
const STEP = {
  enter: 0,
  scan: 1,
  identity: 2,
  q1: 3,
  q2: 4,
  q3: 5,
  q4: 6,
  ask: 7,
  aim: 8,
  click: 9,
  done: 10,
  leave: 11,
} as const
const LAST_STEP = STEP_MS.length - 1

const PHASES = ["识别", "身份", "判分", "复核", "归档"]

function phaseOf(step: number) {
  if (step <= STEP.scan) return 0
  if (step === STEP.identity) return 1
  if (step <= STEP.q4) return 2
  if (step <= STEP.click) return 3
  return 4
}

const VERDICT_LABEL: Record<Verdict, string> = { correct: "正确", partial: "部分正确", wrong: "错误" }

function Mark({ verdict, on, pending = false }: { verdict: Verdict; on: boolean; pending?: boolean }) {
  if (pending) {
    return (
      <svg className={cn("mark mark-ask", on && "is-on")} viewBox="0 0 40 32" aria-hidden="true">
        <path pathLength={1} d="M20 3.5 C 28 3.2, 33.5 9, 33.2 16 C 32.9 23.5, 27 28.6, 19.6 28.4 C 12 28.2, 6.6 22.6, 6.9 15.6 C 7.2 8.8, 12.5 3.8, 21.5 3.6" />
        <text x="20" y="21.5" textAnchor="middle">?</text>
      </svg>
    )
  }
  return (
    <svg className={cn("mark", `mark-${verdict}`, on && "is-on")} viewBox="0 0 40 32" aria-hidden="true">
      {verdict === "wrong" ? (
        <>
          <path pathLength={1} d="M9 6 C 15 12, 22 19, 31 27" />
          <path pathLength={1} d="M31.5 5.5 C 24 12, 16 19.5, 8.5 27" className="mark-second" />
        </>
      ) : (
        <>
          <path pathLength={1} d="M5 17.5 C 8.5 20, 12 23.5, 14.5 27 C 19.5 18, 26 10, 35 3.5" />
          {verdict === "partial" && <path pathLength={1} d="M19.5 9 C 22 13, 24.5 16.5, 27.5 20" className="mark-second" />}
        </>
      )}
    </svg>
  )
}

export function GradingSpecimen() {
  const reduced = usePrefersReducedMotion()
  const [figureRef, inView] = useInView<HTMLElement>({ once: false, threshold: 0.2, rootMargin: "0px" })
  const stageRef = useRef<HTMLDivElement>(null)
  const targetRef = useRef<HTMLSpanElement>(null)
  const [step, setStep] = useState<number>(STEP.enter)
  const [cycle, setCycle] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [pageVisible, setPageVisible] = useState(true)
  const [cursor, setCursor] = useState({ x: 0, y: 0 })

  // Reduced motion: show the finished page at rest; the visitor can still start the sequence.
  useEffect(() => {
    if (!reduced) return
    setPlaying(false)
    setStep(STEP.done)
  }, [reduced])

  useEffect(() => {
    const onVisibility = () => setPageVisible(!document.hidden)
    document.addEventListener("visibilitychange", onVisibility)
    return () => document.removeEventListener("visibilitychange", onVisibility)
  }, [])

  useEffect(() => {
    if (!playing || !inView || !pageVisible) return
    const timer = window.setTimeout(() => {
      if (step === LAST_STEP) {
        setCycle((value) => value + 1)
        setStep(STEP.enter)
      } else {
        setStep(step + 1)
      }
    }, STEP_MS[step])
    return () => window.clearTimeout(timer)
  }, [step, playing, inView, pageVisible])

  const placeCursor = useCallback(() => {
    const stage = stageRef.current
    const target = targetRef.current
    if (!stage) return
    const stageBox = stage.getBoundingClientRect()
    if (step >= STEP.aim && step <= STEP.click && target) {
      const box = target.getBoundingClientRect()
      setCursor({ x: box.left - stageBox.left + box.width * 0.55, y: box.top - stageBox.top + box.height * 0.6 })
    } else {
      setCursor({ x: stageBox.width * 0.9, y: stageBox.height * 0.93 })
    }
  }, [step])

  useLayoutEffect(() => {
    placeCursor()
  }, [placeCursor])

  useEffect(() => {
    window.addEventListener("resize", placeCursor)
    return () => window.removeEventListener("resize", placeCursor)
  }, [placeCursor])

  const sheet = VARIANTS[cycle % VARIANTS.length]
  const at = (value: number) => step >= value
  const phase = phaseOf(step)
  const asking = step >= STEP.ask && step <= STEP.click
  const resolved = at(STEP.click)
  const verdicts: Verdict[] = [sheet.q1.verdict, sheet.q2.verdict, sheet.q3.verdict, "correct"]
  const tally = (kind: Verdict) => verdicts.filter((verdict) => verdict === kind).length

  return (
    <figure
      ref={figureRef}
      className="specimen hero-rise"
      aria-label="批阅流程示意：逐页识别、核验身份、逐题判分，字迹不清的题目交给教师复核后归档"
    >
      <div className="specimen-window" aria-hidden="true">
        <div className="specimen-chrome">
          <span className="logo-mark is-small">M</span>
          <strong>详细批改</strong>
          <span className="specimen-class">高一 · 3 班</span>
          <ol className="specimen-phases">
            {PHASES.map((label, index) => (
              <li key={label} className={cn(index < phase && "is-done", index === phase && "is-current")}>
                {label}
              </li>
            ))}
          </ol>
        </div>
        <div className="specimen-progress">
          <span>第 {sheet.page} / {TOTAL_PAGES} 页</span>
          <i>
            <b style={{ transform: `scaleX(${(sheet.page - (at(STEP.done) ? 0 : 1)) / TOTAL_PAGES})` }} />
          </i>
        </div>

        <div ref={stageRef} className="specimen-stage" data-step={step}>
          <div className="sheet-pile" />
          <article key={cycle} className={cn("sheet", step === STEP.leave && "is-leaving")}>
            <header className="sheet-head">
              <p className="sheet-title">高一数学 · 函数与数列练习</p>
              <div className={cn("sheet-fields", at(STEP.scan) && "ocr-on", at(STEP.identity) && "is-verified")}>
                <span>
                  姓名 <b className="hand">{sheet.student}</b>
                </span>
                <span>班级 <b className="hand">高一 3 班</b></span>
                <em className="id-chip">身份已核验</em>
              </div>
            </header>

            <ol className="sheet-questions">
              <li className={cn("q", at(STEP.scan) && "ocr-on", at(STEP.q1) && "ocr-off")} style={cssVars({ "--i": 0 })}>
                <p>
                  <span className="q-no">1.</span>函数 y = log₂(x − 1) 的定义域为（<b className="hand q-inline">{sheet.q1.answer}</b>）
                </p>
                <p className="q-options">A. (0, +∞)　B. (1, +∞)　C. [1, +∞)　D. ℝ</p>
                <Mark verdict={sheet.q1.verdict} on={at(STEP.q1)} />
                <span className={cn("q-tag", at(STEP.q1) && "is-on")}>选择题 · 机械比对</span>
              </li>
              <li className={cn("q", at(STEP.scan) && "ocr-on", at(STEP.q2) && "ocr-off")} style={cssVars({ "--i": 1 })}>
                <p>
                  <span className="q-no">2.</span>已知 sin α = 3/5，α 为锐角，则 cos α = <b className="hand q-blank">{sheet.q2.answer}</b>
                </p>
                <Mark verdict={sheet.q2.verdict} on={at(STEP.q2)} />
              </li>
              <li className={cn("q", at(STEP.scan) && "ocr-on", at(STEP.q3) && "ocr-off")} style={cssVars({ "--i": 2 })}>
                <p>
                  <span className="q-no">3.</span>求函数 f(x) = x² − 2x − 3 的单调区间。
                </p>
                <div className="hand hand-block">
                  {sheet.q3.lines.map((line) => (
                    <span key={line}>{line}</span>
                  ))}
                </div>
                <Mark verdict={sheet.q3.verdict} on={at(STEP.q3)} />
                {sheet.q3.verdict === "partial" && (
                  <span className={cn("q-tag is-partial", at(STEP.q3) && "is-on")}>漏写减区间</span>
                )}
              </li>
              <li
                className={cn(
                  "q q-last",
                  at(STEP.scan) && "ocr-on",
                  at(STEP.q4) && !resolved && "is-flagged",
                  resolved && "ocr-off",
                )}
                style={cssVars({ "--i": 3 })}
              >
                <p>
                  <span className="q-no">4.</span>等差数列 {"{aₙ}"} 中，a₁ = 2，d = 3，求 S₁₀。
                </p>
                <div className="hand hand-block">
                  <span>a₁₀ = 2 + 9 × 3 = 29</span>
                  <span>
                    S₁₀ = (2 + 29) × 10 ÷ 2 = <span className="smudge">155</span>
                  </span>
                </div>
                <Mark verdict="correct" on={at(STEP.q4) && !resolved} pending />
                <Mark verdict="correct" on={resolved} />
                <span className={cn("q-tag is-pending", at(STEP.q4) && !resolved && "is-on")}>字迹不清 · 待复核</span>
                <span className={cn("q-tag is-teacher", resolved && "is-on")}>教师确认</span>

                <div className={cn("review-pop", asking && "is-open")}>
                  <p className="review-pop-title">第 4 题 · 待教师判定</p>
                  <p className="review-pop-sub">识别结果：155（末位也可能是 135）</p>
                  <div className="review-pop-actions">
                    <span ref={targetRef} className={cn("is-target", step === STEP.click && "is-pressed")}>正确</span>
                    <span>部分正确</span>
                    <span>错误</span>
                  </div>
                </div>
              </li>
            </ol>

            <footer className={cn("sheet-tally", at(STEP.done) && "is-on")}>
              <span>正确 {tally("correct")}</span>
              <span>部分正确 {tally("partial")}</span>
              <span>错误 {tally("wrong")}</span>
              <strong>本页已全部判定</strong>
            </footer>

            <div className={cn("sheet-stamp", at(STEP.done) && "is-on")}>
              <strong>已复核</strong>
              <span>可入档</span>
            </div>

            <div className={cn("scan-line", step === STEP.scan && "is-scanning")} />
          </article>

          <svg
            className={cn("specimen-cursor", asking && "is-visible", step === STEP.click && "is-clicking")}
            style={{ transform: `translate(${cursor.x}px, ${cursor.y}px)` }}
            viewBox="0 0 24 24"
          >
            <path d="M5 3 L5 19.5 L9.4 15.4 L12.4 21.6 L15.2 20.3 L12.3 14.2 L18.4 14.2 Z" />
          </svg>
        </div>
      </div>

      <figcaption className="specimen-caption">
        <span>
          示意动画 · 当前：<b>{PHASES[phase]}</b>
          <span className="sr-only">。第 4 题字迹不清，系统不猜测，交给教师判定为{VERDICT_LABEL.correct}。</span>
        </span>
        <button
          type="button"
          className="specimen-toggle"
          onClick={() => setPlaying((value) => !value)}
        >
          {playing ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
          {playing ? "暂停演示" : "播放演示"}
        </button>
      </figcaption>
    </figure>
  )
}
