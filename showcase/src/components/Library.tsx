import { useId, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"
import { Camera, FileText, Library as LibraryIcon, MessagesSquare, PencilRuler, Presentation } from "lucide-react"

import { SectionHead } from "@/components/SectionHead"
import { cn, cssVars } from "@/lib/utils"

/* ── Parabola lab: the same kind of parameter play the courseware library ships ── */
const X_MIN = -5
const X_MAX = 5
const Y_MIN = -4.5
const Y_MAX = 3
const SCALE = 40
const WIDTH = (X_MAX - X_MIN) * SCALE
const HEIGHT = (Y_MAX - Y_MIN) * SCALE
const toX = (x: number) => (x - X_MIN) * SCALE
const toY = (y: number) => (Y_MAX - y) * SCALE

const round1 = (value: number) => Math.round(value * 10) / 10
const clampTo = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

const formatNumber = (value: number) => {
  const rounded = round1(value) || 0
  const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
  return text.replace("-", "−")
}

/** "+ 3" / "− 3" with a true minus sign; `leading` drops the plus. */
const signed = (value: number, leading = false) => {
  const magnitude = formatNumber(Math.abs(value))
  if (value < 0) return leading ? `−${magnitude}` : ` − ${magnitude}`
  return leading ? magnitude : ` + ${magnitude}`
}

function coefficient(value: number) {
  const rounded = round1(value)
  if (rounded === 1) return ""
  if (rounded === -1) return "−"
  return signed(rounded, true)
}

function vertexForm(a: number, h: number, k: number) {
  const inner = round1(h) === 0 ? "x" : `(x${signed(-h)})`
  const square = `${inner}²`
  const tail = round1(k) === 0 ? "" : signed(k)
  return `y = ${coefficient(a)}${square}${tail}`
}

function standardForm(a: number, h: number, k: number) {
  const b = -2 * a * h
  const c = a * h * h + k
  let text = `y = ${coefficient(a)}x²`
  if (round1(b) !== 0) text += `${signed(b)}x`.replace(/ ([+−]) 1x$/, " $1 x")
  if (round1(c) !== 0) text += signed(c)
  return text
}

function ParabolaLab() {
  const [a, setA] = useState(1)
  const [h, setH] = useState(1)
  const [k, setK] = useState(-4)
  const svgRef = useRef<SVGSVGElement>(null)
  const draggingRef = useRef(false)
  const readoutId = useId()

  const points: string[] = []
  for (let x = X_MIN; x <= X_MAX + 1e-9; x += 0.05) {
    const y = a * (x - h) ** 2 + k
    points.push(`${toX(x).toFixed(1)},${toY(clampTo(y, Y_MIN - 4, Y_MAX + 4)).toFixed(1)}`)
  }

  const discriminant = -k / a
  const roots = discriminant > 0 ? [h - Math.sqrt(discriminant), h + Math.sqrt(discriminant)] : discriminant === 0 ? [h] : []
  const visibleRoots = roots.filter((root) => root >= X_MIN && root <= X_MAX)

  const moveVertex = (event: PointerEvent<SVGElement>) => {
    const svg = svgRef.current
    if (!svg || !draggingRef.current) return
    const box = svg.getBoundingClientRect()
    const x = X_MIN + ((event.clientX - box.left) / box.width) * (X_MAX - X_MIN)
    const y = Y_MAX - ((event.clientY - box.top) / box.height) * (Y_MAX - Y_MIN)
    setH(round1(clampTo(x, -4, 4)))
    setK(round1(clampTo(y, -4, 2.5)))
  }

  const nudgeVertex = (event: KeyboardEvent<SVGGElement>) => {
    const step = event.shiftKey ? 1 : 0.1
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, step],
      ArrowDown: [0, -step],
    }
    const move = moves[event.key]
    if (!move) return
    event.preventDefault()
    setH((value) => round1(clampTo(value + move[0], -4, 4)))
    setK((value) => round1(clampTo(value + move[1], -4, 2.5)))
  }

  const setNonZeroA = (value: number) => setA(round1(value) === 0 ? (a > 0 ? -0.1 : 0.1) : round1(value))

  const sliders = [
    { key: "a", label: "开口系数 a", value: a, min: -2, max: 2, onChange: setNonZeroA },
    { key: "h", label: "水平平移 h", value: h, min: -4, max: 4, onChange: (value: number) => setH(round1(value)) },
    { key: "k", label: "竖直平移 k", value: k, min: -4, max: 2.5, onChange: (value: number) => setK(round1(value)) },
  ]

  return (
    <div className="lab" data-reveal>
      <div className="lab-head">
        <span className="chip is-plain">课件示意 · 二次函数</span>
        <p className="lab-formula" id={readoutId} aria-live="polite">
          {vertexForm(a, h, k)}
        </p>
      </div>

      <div className="lab-plot">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label="可拖动顶点的二次函数图像"
          onPointerMove={moveVertex}
          onPointerUp={() => {
            draggingRef.current = false
          }}
          onPointerCancel={() => {
            draggingRef.current = false
          }}
        >
          <defs>
            <clipPath id="lab-clip">
              <rect width={WIDTH} height={HEIGHT} />
            </clipPath>
          </defs>
          {Array.from({ length: X_MAX - X_MIN + 1 }, (_, index) => X_MIN + index).map((x) => (
            <line key={`gx${x}`} className="lab-grid" x1={toX(x)} y1={0} x2={toX(x)} y2={HEIGHT} />
          ))}
          {Array.from({ length: Math.floor(Y_MAX) - Math.ceil(Y_MIN) + 1 }, (_, index) => Math.ceil(Y_MIN) + index).map((y) => (
            <line key={`gy${y}`} className="lab-grid" x1={0} y1={toY(y)} x2={WIDTH} y2={toY(y)} />
          ))}
          <line className="lab-axis" x1={0} y1={toY(0)} x2={WIDTH} y2={toY(0)} />
          <line className="lab-axis" x1={toX(0)} y1={0} x2={toX(0)} y2={HEIGHT} />
          {[-4, -2, 2, 4].map((x) => (
            <text key={`tx${x}`} className="lab-tick" x={toX(x)} y={toY(0) + 16} textAnchor="middle">
              {x < 0 ? `−${-x}` : x}
            </text>
          ))}
          {[-4, -2, 2].map((y) => (
            <text key={`ty${y}`} className="lab-tick" x={toX(0) - 8} y={toY(y) + 4} textAnchor="end">
              {y < 0 ? `−${-y}` : y}
            </text>
          ))}
          <g clipPath="url(#lab-clip)">
            <line className="lab-symmetry" x1={toX(h)} y1={0} x2={toX(h)} y2={HEIGHT} />
            <polyline className="lab-curve" points={points.join(" ")} />
          </g>
          {visibleRoots.map((root) => (
            <g key={root.toFixed(3)}>
              <circle className="lab-root" cx={toX(root)} cy={toY(0)} r={5} />
              <text className="lab-root-label" x={toX(root)} y={toY(0) - 12} textAnchor="middle">
                {formatNumber(root)}
              </text>
            </g>
          ))}
          <g
            className="lab-vertex"
            tabIndex={0}
            role="button"
            aria-label={`顶点 (${formatNumber(h)}, ${formatNumber(k)})，可拖动或用方向键移动`}
            aria-describedby={readoutId}
            onPointerDown={(event) => {
              draggingRef.current = true
              svgRef.current?.setPointerCapture(event.pointerId)
            }}
            onKeyDown={nudgeVertex}
          >
            <circle className="lab-vertex-halo" cx={toX(h)} cy={toY(k)} r={16} />
            <circle className="lab-vertex-dot" cx={toX(h)} cy={toY(k)} r={7} />
          </g>
        </svg>
      </div>

      <dl className="lab-readout">
        <div>
          <dt>一般式</dt>
          <dd>{standardForm(a, h, k)}</dd>
        </div>
        <div>
          <dt>顶点</dt>
          <dd>
            ({formatNumber(h)}, {formatNumber(k)})
          </dd>
        </div>
        <div>
          <dt>零点</dt>
          <dd>{roots.length === 0 ? "无实根" : roots.map((root) => `x = ${formatNumber(root)}`).join("，")}</dd>
        </div>
      </dl>

      <div className="lab-sliders">
        {sliders.map((slider) => (
          <label key={slider.key}>
            <span>{slider.label}</span>
            <input
              type="range"
              min={slider.min}
              max={slider.max}
              step={0.1}
              value={slider.value}
              onChange={(event) => slider.onChange(Number(event.target.value))}
            />
            <output>{formatNumber(slider.value)}</output>
          </label>
        ))}
      </div>
    </div>
  )
}

/* ── Stages and tools ── */
const STAGES = [
  {
    key: "primary",
    label: "小学数学",
    strands: "数与运算 · 图形与几何 · 统计与概率",
    topics: ["分数的认识与运算", "立体图形与体积", "位置、方向与图形运动", "可能性"],
  },
  {
    key: "junior",
    label: "初中数学",
    strands: "数与代数 · 图形与几何 · 统计与概率",
    topics: ["二次函数", "圆", "锐角三角函数与解直角三角形", "图形变换与相似"],
  },
  {
    key: "senior",
    label: "高中数学",
    strands: "函数与代数 · 几何与向量 · 概率与统计",
    topics: ["函数的概念与性质", "空间向量与立体几何", "圆锥曲线与方程", "导数及其应用"],
  },
]

const TOOLS = [
  { icon: Presentation, title: "课件库", body: "每个课件只讲清一个知识点：参数滑块、可暂停的演示，还能导出带标注的图。" },
  { icon: LibraryIcon, title: "题库", body: "维护真实题目与答案；AI 发现的新题先进入“待入库”，老师确认后才入库。" },
  { icon: Camera, title: "拍照导入", body: "纸质题目拍照即可导入题库，省去逐题录入。" },
  { icon: PencilRuler, title: "出题练习", body: "题库抽题、个人薄弱巩固、原题订正卷、班级共性强化，可导出 A3 双栏 Word。" },
  { icon: FileText, title: "成绩报告", body: "班级报告、学生档案与家长信，按周期汇总作答量与正确率。" },
  { icon: MessagesSquare, title: "小M 助手", body: "用自然语言查询班级、学生和批阅数据，也可以请求生成练习或报告。" },
]

export function Library() {
  const [stage, setStage] = useState(2)
  const tabsId = useId()

  const onTabKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return
    event.preventDefault()
    const next = (stage + (event.key === "ArrowRight" ? 1 : STAGES.length - 1)) % STAGES.length
    setStage(next)
    document.getElementById(`${tabsId}-tab-${next}`)?.focus()
  }

  const current = STAGES[stage]

  return (
    <section id="library" className="section" aria-labelledby="library-title">
      <div className="wrap">
        <SectionHead
          index="05"
          label="课件与题库"
          titleId="library-title"
          title="批完这一批，"
          accent="下一节课也有了着落。"
          lead="把错题变成可操作的讲解。课件、题库、练习与报告都在同一个工作台里，并跟随老师在个人资料里设定的学段。"
        />

        <div className="library-grid">
          <ParabolaLab />

          <div className="library-side" data-reveal style={cssVars({ "--reveal-delay": 120 })}>
            <div className="stage-tabs" role="tablist" aria-label="学段" onKeyDown={onTabKey}>
              {STAGES.map((item, index) => (
                <button
                  key={item.key}
                  id={`${tabsId}-tab-${index}`}
                  type="button"
                  role="tab"
                  aria-selected={stage === index}
                  aria-controls={`${tabsId}-panel`}
                  tabIndex={stage === index ? 0 : -1}
                  className={cn(stage === index && "is-active")}
                  onClick={() => setStage(index)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <div id={`${tabsId}-panel`} role="tabpanel" aria-labelledby={`${tabsId}-tab-${stage}`} className="stage-panel" key={current.key}>
              <p className="stage-strands">{current.strands}</p>
              <ul className="stage-topics">
                {current.topics.map((topic) => (
                  <li key={topic}>{topic}</li>
                ))}
              </ul>
              <p className="stage-note">学段在个人资料中设定；题库、统计、报告与新建任务统一跟随，已有批次不受影响。</p>
            </div>

            <ul className="tool-list">
              {TOOLS.map(({ icon: Icon, title, body }) => (
                <li key={title}>
                  <span className="tool-icon" aria-hidden="true">
                    <Icon />
                  </span>
                  <div>
                    <strong>{title}</strong>
                    <p>{body}</p>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </section>
  )
}
