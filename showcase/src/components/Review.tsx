import { useRef, useState, type PointerEvent } from "react"
import { RotateCcw, ZoomIn } from "lucide-react"

import { SectionHead } from "@/components/SectionHead"
import { cn } from "@/lib/utils"

type Verdict = "correct" | "partial" | "wrong"

const VERDICTS: Array<{ value: Verdict; label: string }> = [
  { value: "correct", label: "正确" },
  { value: "partial", label: "部分正确" },
  { value: "wrong", label: "错误" },
]

const LOUPE_RADIUS = 78
const LOUPE_ZOOM = 2.1

function PaperSheet() {
  return (
    <div className="paper-sheet">
      <div className="paper-title">高一数学 · 函数与数列练习</div>
      <div className="paper-meta">
        <span>姓名：<b className="hand">学生 A</b></span>
        <span>班级：<b className="hand">高一 3 班</b></span>
      </div>
      <div className="paper-question">
        <strong>16.</strong>
        <p>已知函数 f(x) = x² − 2x − 3，求函数的单调区间。</p>
        <div className="hand paper-hand">
          f'(x) = 2x − 2<br />
          当 x ＜ 1 时，f'(x) ＜ 0；当 x ＞ 1 时，f'(x) ＞ 0<br />
          减区间 (−∞, 1)，增区间 (1, +∞)
        </div>
      </div>
      <div className="paper-question is-focused">
        <strong>17.</strong>
        <p>等差数列 {"{aₙ}"} 中，a₁ = 2，d = 3，求 S₁₀。</p>
        <div className="hand paper-hand">
          a₁₀ = 2 + 9 × 3 = 29<br />
          S₁₀ = (2 + 29) × 10 ÷ 2 = <span className="smudge">155</span>
        </div>
        <span className="paper-focus-label">当前复核</span>
      </div>
      <div className="paper-question is-muted">
        <strong>18.</strong>
        <p>已知向量 a = (1, 2)，b = (3, −1)，求 a · b。</p>
        <div className="hand paper-hand">a · b = 1 × 3 + 2 × (−1) = 1</div>
      </div>
    </div>
  )
}

export function Review() {
  const stageRef = useRef<HTMLDivElement>(null)
  const loupeRef = useRef<HTMLDivElement>(null)
  const loupeContentRef = useRef<HTMLDivElement>(null)
  const [verdict, setVerdict] = useState<Verdict | null>(null)
  const [saved, setSaved] = useState(false)

  const moveLoupe = (event: PointerEvent<HTMLDivElement>) => {
    const stage = stageRef.current
    const loupe = loupeRef.current
    const content = loupeContentRef.current
    if (!stage || !loupe || !content || event.pointerType !== "mouse") return
    const box = stage.getBoundingClientRect()
    const x = event.clientX - box.left
    const y = event.clientY - box.top
    content.style.width = `${box.width}px`
    loupe.style.transform = `translate(${x - LOUPE_RADIUS}px, ${y - LOUPE_RADIUS}px)`
    content.style.transform = `translate(${LOUPE_RADIUS - x * LOUPE_ZOOM}px, ${LOUPE_RADIUS - y * LOUPE_ZOOM}px) scale(${LOUPE_ZOOM})`
    loupe.classList.add("is-visible")
  }

  const hideLoupe = () => loupeRef.current?.classList.remove("is-visible")

  const chooseVerdict = (next: Verdict) => {
    setVerdict(next)
    setSaved(false)
  }

  const reset = () => {
    setVerdict(null)
    setSaved(false)
  }

  const verdictLabel = VERDICTS.find((item) => item.value === verdict)?.label

  return (
    <section id="review" className="section" aria-labelledby="review-title">
      <div className="wrap">
        <SectionHead
          index="03"
          label="人工复核"
          titleId="review-title"
          title="原卷放大看。"
          accent="判断逐题核。"
          lead="试卷原图与结构化批阅结果同屏出现。老师不需要在弹窗和表格之间来回切换，临界题由教师完成最终判定。下面的界面可以直接试一试。"
        />

        <div className="review-window" data-reveal>
          <div className="review-top">
            <div>
              <span className="logo-mark is-small" aria-hidden="true">M</span>
              <strong>原卷与批阅结果 · 学生 A</strong>
            </div>
            <span>模型原始结论保留在审计记录中</span>
          </div>

          <div className="review-workspace">
            <section className="paper-panel" aria-label="原卷图像示意">
              <div className="paper-toolbar">
                <span>上一页</span>
                <strong>1 / 2</strong>
                <span>下一页</span>
              </div>
              <div
                ref={stageRef}
                className="paper-stage"
                onPointerMove={moveLoupe}
                onPointerLeave={hideLoupe}
              >
                <PaperSheet />
                <div ref={loupeRef} className="loupe" aria-hidden="true">
                  <div ref={loupeContentRef} className="loupe-content">
                    <PaperSheet />
                  </div>
                </div>
              </div>
              <p className="paper-hint">
                <ZoomIn aria-hidden="true" />
                <span className="hint-hover">悬停原卷查看局部放大；</span>工作台中点击原卷可在新窗口放大查看
              </p>
            </section>

            <section className="decision-panel" aria-label="逐题结果">
              <div className="decision-head">
                <div>
                  <span>逐题结果</span>
                  <strong>3 道题</strong>
                </div>
                <span className={cn("chip", verdict ? "is-good" : "is-warn")} aria-live="polite">
                  {verdict ? "已全部判定" : "待复核 1 题"}
                </span>
              </div>

              <article className="decision-card">
                <div className="decision-card-head">
                  <strong>第 16 题</strong>
                  <span className="chip is-good">系统：正确</span>
                </div>
                <dl>
                  <div>
                    <dt>学生答案</dt>
                    <dd>减区间 (−∞, 1)，增区间 (1, +∞)</dd>
                  </div>
                  <div>
                    <dt>参考答案</dt>
                    <dd>与学生结论一致</dd>
                  </div>
                </dl>
              </article>

              <article className={cn("decision-card is-pending", verdict && "is-resolved")}>
                <div className="decision-card-head">
                  <strong>第 17 题</strong>
                  <span className={cn("chip", verdict ? "is-good" : "is-warn")}>
                    {verdict ? `教师：${verdictLabel}` : "系统：待复核"}
                  </span>
                </div>
                <dl>
                  <div>
                    <dt>OCR 学生答案</dt>
                    <dd>
                      S₁₀ = (2 + 29) × 10 ÷ 2 = 155 <em className="ocr-doubt">末位字迹不清，也可能是 135</em>
                    </dd>
                  </div>
                  <div>
                    <dt>参考答案</dt>
                    <dd>S₁₀ = 155，步骤完整</dd>
                  </div>
                </dl>
                <fieldset className="teacher-decision">
                  <legend>最终判定</legend>
                  <div className="segmented">
                    {VERDICTS.map((item) => (
                      <label key={item.value} className={cn(verdict === item.value && "is-checked")}>
                        <input
                          type="radio"
                          name="q17-verdict"
                          value={item.value}
                          checked={verdict === item.value}
                          onChange={() => chooseVerdict(item.value)}
                        />
                        {item.label}
                      </label>
                    ))}
                  </div>
                </fieldset>
              </article>

              <article className="decision-card is-compact">
                <div className="decision-card-head">
                  <strong>第 18 题</strong>
                  <span className="chip is-good">系统：正确</span>
                </div>
              </article>

              <div className="decision-actions">
                <button
                  type="button"
                  className={cn("btn btn-solid btn-md save-decision", saved && "is-saved")}
                  disabled={!verdict || saved}
                  onClick={() => setSaved(true)}
                >
                  {saved ? "已保存（演示，不提交数据）" : verdict ? "保存该学生判定" : "先完成第 17 题判定"}
                </button>
                {verdict && (
                  <button type="button" className="btn btn-ghost btn-sm" onClick={reset}>
                    <RotateCcw aria-hidden="true" />
                    重置演示
                  </button>
                )}
              </div>
            </section>
          </div>
        </div>
      </div>
    </section>
  )
}
