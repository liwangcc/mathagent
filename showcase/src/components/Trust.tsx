import { usePrefersReducedMotion } from "@/lib/hooks"
import { cssVars } from "@/lib/utils"

const PRINCIPLES = [
  { title: "不确定，就停下来确认", body: "身份低置信、方向异常或判断临界时，不静默猜测。" },
  { title: "原卷与结果同屏复核", body: "老师始终能回到作答证据，系统结论不会遮住原始试卷。" },
  { title: "确认后才进入正式档案", body: "待复核结果不会冒充最终成绩，也不会提前写入学生成长记录。" },
  { title: "原图短期保留，结果持续可用", body: "终态原图按 36 小时保留策略自动清理；错题、得分和正确率继续保留。" },
]

const UPPER = "M40 110 H176 C 226 110, 236 52, 292 52 H418"
const LOWER = "M40 110 H176 C 226 110, 236 168, 292 168 H418"

export function Trust() {
  const reduced = usePrefersReducedMotion()

  return (
    <section id="trust" className="section trust" aria-labelledby="trust-title">
      <div className="wrap trust-grid">
        <div data-reveal>
          <p className="kicker trust-kicker">
            <span className="kicker-num">06</span>
            <span>可信边界</span>
          </p>
          <h2 id="trust-title" className="display trust-title">
            AI 可以很快。
            <em>教育判断必须稳。</em>
          </h2>
          <p className="lead trust-lead">系统给出证据和建议，但最终判断、归档与异常处理仍由教师完成。</p>

          <figure className="fork">
            <svg viewBox="0 0 540 220" role="img" aria-label="证据充分时进入正式结果；证据不足时停下，交给老师确认">
              <path className="fork-path is-pass" d={UPPER} />
              <path className="fork-path is-hold" d={LOWER} />
              {/* Each dot waits hidden under a node while the other travels. */}
              {!reduced && (
                <>
                  <circle className="fork-dot is-pass" r="5">
                    <animateMotion dur="5.6s" repeatCount="indefinite" path={UPPER} keyPoints="0;1;1" keyTimes="0;0.5;1" calcMode="linear" />
                  </circle>
                  <circle className="fork-dot is-hold" r="5">
                    <animateMotion dur="5.6s" repeatCount="indefinite" path={LOWER} keyPoints="0;0;1" keyTimes="0;0.5;1" calcMode="linear" />
                  </circle>
                </>
              )}
              <circle className="fork-start" cx="40" cy="110" r="9" />
              <text className="fork-label" x="40" y="142" textAnchor="middle">
                系统结论
              </text>
              <text className="fork-note is-pass" x="300" y="36">
                证据充分
              </text>
              <text className="fork-note is-hold" x="300" y="198">
                证据不足
              </text>
              <rect className="fork-end is-pass" x="404" y="34" width="124" height="36" rx="8" />
              <text className="fork-end-text" x="466" y="57" textAnchor="middle">
                进入正式结果
              </text>
              <rect className="fork-end is-hold" x="404" y="150" width="124" height="36" rx="8" />
              <text className="fork-end-text" x="466" y="173" textAnchor="middle">
                停下，交给老师
              </text>
            </svg>
          </figure>
        </div>

        <ol className="trust-list">
          {PRINCIPLES.map((item, index) => (
            <li key={item.title} data-reveal style={cssVars({ "--reveal-delay": index * 90 })}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{item.title}</strong>
                <p>{item.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
