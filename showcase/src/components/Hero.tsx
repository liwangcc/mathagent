import { ArrowDown, ArrowUpRight } from "lucide-react"

import { Button } from "@/components/ui/button"
import { GradingSpecimen } from "@/components/GradingSpecimen"
import { WEB_APP_URL } from "@/config"
import { cssVars } from "@/lib/utils"

export function Hero() {
  return (
    <section id="top" className="hero" aria-labelledby="hero-title">
      <div className="hero-paper" aria-hidden="true" />
      <svg className="hero-plot" viewBox="0 0 1440 520" preserveAspectRatio="none" aria-hidden="true">
        <line className="axis" x1="0" y1="496" x2="1440" y2="496" />
        <path className="curve is-faint" pathLength={1} d="M0 470 C 320 470, 560 440, 760 420 S 1160 300, 1440 330" />
        <path className="curve" pathLength={1} d="M0 508 C 420 508, 700 480, 900 380 S 1240 110, 1440 60" />
        <circle className="dot" cx="900" cy="380" r="5" />
      </svg>

      <div className="wrap hero-layout">
        <div className="hero-copy">
          <p className="eyebrow hero-rise" style={cssVars({ "--d": 0 })}>
            面向中小学数学教师的 AI 批阅与学情工作台
          </p>
          <h1 id="hero-title" className="hero-title hero-rise" style={cssVars({ "--d": 1 })}>
            <span className="hero-title-line">让每一份作答，</span>
            <span className="hero-title-accent">
              被清晰看见。
              <svg viewBox="0 0 300 18" preserveAspectRatio="none" aria-hidden="true">
                <path pathLength={1} d="M4 12 C 60 5, 120 4, 170 8 S 260 14, 296 6" />
              </svg>
            </span>
          </h1>
          <p className="hero-lead hero-rise" style={cssVars({ "--d": 2 })}>
            有名单，沉淀学生档案；没名单，也能看懂整批学情。AI 完成重复步骤，教师对照原卷确认最终判断。
          </p>
          <div className="hero-actions hero-rise" style={cssVars({ "--d": 3 })}>
            <Button asChild size="lg">
              <a href={WEB_APP_URL} target="_blank" rel="noreferrer">
                进入教师工作台
                <ArrowUpRight aria-hidden="true" />
              </a>
            </Button>
            <Button asChild size="lg" variant="outline">
              <a href="#modes">
                查看两种批阅方式
                <ArrowDown aria-hidden="true" />
              </a>
            </Button>
          </div>
          <dl className="hero-ledger hero-rise" style={cssVars({ "--d": 4 })}>
            <div>
              <dt>学段</dt>
              <dd className="ledger-value">3<small>个</small></dd>
              <dd className="ledger-note">小学 · 初中 · 高中</dd>
            </div>
            <div>
              <dt>批阅方式</dt>
              <dd className="ledger-value">2<small>种</small></dd>
              <dd className="ledger-note">有身份 · 无身份</dd>
            </div>
            <div>
              <dt>系统流程</dt>
              <dd className="ledger-value">20<small>分钟内</small></dd>
              <dd className="ledger-note">100 页以内通常可完成</dd>
            </div>
          </dl>
        </div>

        <GradingSpecimen />
      </div>
    </section>
  )
}
