import { useEffect, useState } from "react"
import { Check, CircleDashed, Minus, X } from "lucide-react"

import { SectionHead } from "@/components/SectionHead"
import { useInView, usePrefersReducedMotion } from "@/lib/hooks"
import { cn, cssVars } from "@/lib/utils"

/* One consistent, desensitised batch: 48 pages × 16 questions; 752 judged, 16 still with the teacher. */
const PAGES = 48
const QUESTIONS = 768
const PENDING = 16
const JUDGED = QUESTIONS - PENDING

const OUTCOMES = [
  { key: "correct", label: "正确", count: 570, icon: Check },
  { key: "partial", label: "部分正确", count: 58, icon: Minus },
  { key: "wrong", label: "错误", count: 124, icon: X },
] as const

type OutcomeKey = (typeof OUTCOMES)[number]["key"]

const ERRORS = OUTCOMES[1].count + OUTCOMES[2].count

const TOP_ERRORS = [
  { question: "第 12 题", topic: "函数单调性", count: 18 },
  { question: "第 17 题", topic: "数列求和", count: 14 },
  { question: "第 8 题", topic: "空间向量", count: 11 },
]

const percent = (part: number, whole: number) => (part / whole) * 100

const RADIUS = 74
const CIRCUMFERENCE = 2 * Math.PI * RADIUS
const GAP = 3

function useCountUp(target: number, active: boolean, duration = 1300) {
  const reduced = usePrefersReducedMotion()
  const [value, setValue] = useState(0)

  useEffect(() => {
    if (!active) return
    if (reduced) {
      setValue(target)
      return
    }
    let frame = 0
    const start = performance.now()
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      setValue(target * (1 - Math.pow(1 - t, 3)))
      if (t < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [active, duration, reduced, target])

  return value
}

function Metric({
  label,
  value,
  decimals = 0,
  unit,
  suffix,
  note,
  active,
  tone,
}: {
  label: string
  value: number
  decimals?: number
  unit?: string
  suffix?: string
  note: string
  active: boolean
  tone?: "warm"
}) {
  const shown = useCountUp(value, active)
  return (
    <article className={cn("metric", tone === "warm" && "is-warm")}>
      <span>{label}</span>
      <strong>
        {shown.toFixed(decimals)}
        {unit && <small>{unit}</small>}
        {suffix && <small className="metric-suffix">{suffix}</small>}
      </strong>
      <em>{note}</em>
    </article>
  )
}

export function Insights() {
  const [dashRef, inView] = useInView<HTMLDivElement>({ threshold: 0.25 })
  const [focus, setFocus] = useState<OutcomeKey | null>(null)
  const [hoveredError, setHoveredError] = useState<number | null>(null)

  let offset = 0
  const segments = OUTCOMES.map((outcome) => {
    const length = (outcome.count / JUDGED) * CIRCUMFERENCE
    const segment = { ...outcome, length, offset, share: percent(outcome.count, JUDGED) }
    offset += length
    return segment
  })
  const focused = segments.find((segment) => segment.key === focus)

  return (
    <section id="insights" className="section" aria-labelledby="insights-title">
      <div className="wrap">
        <SectionHead
          index="04"
          label="批后洞察"
          titleId="insights-title"
          title="批完这一批。"
          accent="下一步教什么，也更清楚。"
          lead="不停留在大片文字和单一错题率。先看整体，再定位高频错题、判定结果与薄弱知识点。"
        />

        <div ref={dashRef} className={cn("dashboard", inView && "is-live")} data-reveal>
          <div className="dashboard-head">
            <div>
              <span>本批洞察</span>
              <strong>函数与数列 · 本批批阅报告</strong>
            </div>
            <span className="chip is-plain">脱敏示意数据</span>
          </div>

          <div className="dashboard-overview">
            <article className="donut-card">
              <div className="donut" onPointerLeave={() => setFocus(null)}>
                <svg viewBox="0 0 200 200" role="img" aria-label={`判定结果分布：${segments.map((s) => `${s.label} ${s.count} 题`).join("，")}`}>
                  <defs>
                    <pattern id="hatch-wrong" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
                      <rect width="6" height="6" style={{ fill: "var(--chart-wrong)" }} />
                      <line x1="1.5" y1="0" x2="1.5" y2="6" className="hatch-line" />
                    </pattern>
                  </defs>
                  <circle className="donut-track" cx="100" cy="100" r={RADIUS} />
                  {segments.map((segment, index) => (
                    <circle
                      key={segment.key}
                      className={cn("donut-segment", `is-${segment.key}`, focus && focus !== segment.key && "is-dimmed")}
                      cx="100"
                      cy="100"
                      r={RADIUS}
                      style={{
                        strokeDasharray: inView
                          ? `${Math.max(0, segment.length - GAP)} ${CIRCUMFERENCE}`
                          : `0 ${CIRCUMFERENCE}`,
                        strokeDashoffset: -segment.offset,
                        transitionDelay: `${index * 160}ms`,
                      }}
                      onPointerEnter={() => setFocus(segment.key)}
                    />
                  ))}
                </svg>
                <div className="donut-center" aria-hidden="true">
                  <strong>{Math.round(focused ? focused.share : segments[0].share)}%</strong>
                  <span>{focused ? `${focused.label} · ${focused.count} 题` : "已判正确率"}</span>
                </div>
              </div>

              <ul className="donut-legend">
                {segments.map((segment) => {
                  const Icon = segment.icon
                  return (
                    <li key={segment.key}>
                      <button
                        type="button"
                        className={cn(focus === segment.key && "is-focused")}
                        onPointerEnter={() => setFocus(segment.key)}
                        onPointerLeave={() => setFocus(null)}
                        onFocus={() => setFocus(segment.key)}
                        onBlur={() => setFocus(null)}
                      >
                        <i className={cn("legend-swatch", `is-${segment.key}`)} aria-hidden="true">
                          <Icon />
                        </i>
                        <span>{segment.label}</span>
                        <strong>{segment.count}</strong>
                        <em>{Math.round(segment.share)}%</em>
                      </button>
                    </li>
                  )
                })}
                <li className="legend-pending">
                  <span className="legend-pending-row">
                    <i className="legend-swatch is-pending" aria-hidden="true">
                      <CircleDashed />
                    </i>
                    <span>待复核（未计入）</span>
                    <strong>{PENDING}</strong>
                    <em>—</em>
                  </span>
                </li>
              </ul>
            </article>

            <div className="metric-grid">
              <Metric label="批阅范围" value={PAGES} unit="页" note={`共 ${QUESTIONS} 道题`} active={inView} />
              <Metric label="判定覆盖" value={JUDGED} suffix={` / ${QUESTIONS}`} note={`待复核 ${PENDING} 题`} active={inView} />
              <Metric label="错题总量" value={ERRORS} unit="题" note="错误 + 部分正确" active={inView} tone="warm" />
              <Metric label="全题错题率" value={percent(ERRORS, JUDGED)} decimals={1} unit="%" note="仅统计已判题目" active={inView} />
            </div>
          </div>

          <div className="dashboard-detail">
            <article className="insight-card">
              <div className="insight-card-head">
                <div>
                  <span>优先处理</span>
                  <h3>高频错题</h3>
                  <p>先讲清错误最多、最值得复盘的题目。</p>
                </div>
                <b>TOP 3</b>
              </div>
              <ol className="error-list">
                {TOP_ERRORS.map((item, index) => {
                  const share = percent(item.count, PAGES)
                  return (
                    <li
                      key={item.question}
                      onPointerEnter={() => setHoveredError(index)}
                      onPointerLeave={() => setHoveredError(null)}
                      className={cn(hoveredError === index && "is-hovered")}
                    >
                      <span className="error-rank">{String(index + 1).padStart(2, "0")}</span>
                      <div className="error-copy">
                        <strong>{item.question}</strong>
                        <small>
                          {item.topic} · {item.count} 次
                        </small>
                        <i className="error-track">
                          <span style={cssVars({ "--w": `${share}%`, "--i": index })} />
                          <span className="error-tip" role="tooltip">
                            {item.count} / {PAGES} 份作答出错
                          </span>
                        </i>
                      </div>
                      <b>{Math.round(share)}%</b>
                    </li>
                  )
                })}
              </ol>
            </article>

            <article className="insight-card">
              <div className="insight-card-head">
                <div>
                  <span>学习画像</span>
                  <h3>判定结果与知识点</h3>
                  <p>先确认结果分布，再定位需要复盘的知识点。</p>
                </div>
              </div>
              <div className="distribution">
                <div>
                  <span>错误与部分正确</span>
                  <strong>{ERRORS} 题</strong>
                  <i>
                    <span style={cssVars({ "--w": `${percent(ERRORS, JUDGED)}%`, "--i": 0 })} />
                  </i>
                </div>
                <div>
                  <span>待复核</span>
                  <strong>{PENDING} 题</strong>
                  <i className="is-pending">
                    <span style={cssVars({ "--w": `${percent(PENDING, QUESTIONS)}%`, "--i": 1 })} />
                  </i>
                </div>
              </div>
              <div className="knowledge">
                <span>薄弱知识点</span>
                <div>
                  <b>函数单调性</b>
                  <b>数列求和</b>
                  <b>空间向量</b>
                  <b>分类讨论</b>
                </div>
              </div>
            </article>
          </div>

          <table className="sr-only">
            <caption>本批判定结果分布（已判 {JUDGED} 题）</caption>
            <thead>
              <tr>
                <th scope="col">结果</th>
                <th scope="col">题数</th>
                <th scope="col">占比</th>
              </tr>
            </thead>
            <tbody>
              {segments.map((segment) => (
                <tr key={segment.key}>
                  <th scope="row">{segment.label}</th>
                  <td>{segment.count}</td>
                  <td>{segment.share.toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}
