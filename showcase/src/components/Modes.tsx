import { Check, ChevronDown, FileScan, Minus, ShieldCheck, Users } from "lucide-react"

import { SectionHead } from "@/components/SectionHead"
import { cn, cssVars } from "@/lib/utils"

const STUDENTS = [
  { index: "01", name: "学生 A", state: "已完成", tone: "is-good", score: "86%" },
  { index: "02", name: "学生 B", state: "待复核 2 题", tone: "is-warn", score: "—" },
  { index: "03", name: "学生 C", state: "可入档", tone: "is-good", score: "92%" },
  { index: "04", name: "学生 D?", state: "身份待老师确认", tone: "is-warn", score: "—" },
]

const PAGES = [
  { label: "第 06 页", state: "已判 16 题" },
  { label: "第 07 页", state: "页面方向异常 · 教师已排除", excluded: true },
  { label: "第 08 页", state: "已判 16 题" },
]

const COMPARISON: Array<{ label: string; identified: string; anonymous: string; both?: boolean }> = [
  { label: "班级名单", identified: "需要，先选择班级", anonymous: "不需要" },
  { label: "身份核验", identified: "逐页核验姓名与学号", anonymous: "不识别身份" },
  { label: "学生档案", identified: "教师确认后写入", anonymous: "不写入" },
  { label: "原卷复核", identified: "逐题对照原卷", anonymous: "按页面排除异常结果", both: true },
  { label: "整批洞察", identified: "高频错题与知识点", anonymous: "高频错题与知识点", both: true },
]

export function Modes() {
  return (
    <section id="modes" className="section" aria-labelledby="modes-title">
      <div className="wrap">
        <SectionHead
          index="02"
          label="两种批阅方式"
          titleId="modes-title"
          title="有名字，看见每个学生。"
          accent="没名字，也能读懂这一批。"
          lead="不要求老师为一次体验先整理完整名单。根据教学场景选择入口，两种方式都保留原卷复核和整批统计。"
        />

        <div className="mode-grid">
          <article className="mode-card" data-reveal>
            <div className="mode-copy">
              <p className="mode-label">
                <Users aria-hidden="true" /> 有身份批阅
              </p>
              <h3>批阅结果跟着学生走</h3>
              <p>逐页核验姓名与学号，人工确认后写入学生档案与班级统计，适合日常作业和考试。</p>
              <ul>
                <li><Check aria-hidden="true" />确认身份后按学生归组</li>
                <li><Check aria-hidden="true" />待复核结果不提前入档</li>
                <li><Check aria-hidden="true" />保留错题、得分与正确率</li>
              </ul>
            </div>
            <div className="mode-preview" aria-label="有身份批阅界面示意" role="img">
              <div className="mini-bar">
                <span className="logo-mark is-small" aria-hidden="true">M</span>
                <strong>详细批改</strong>
                <span className="mini-pill">
                  高一 · 3 班 <ChevronDown aria-hidden="true" />
                </span>
              </div>
              <div className="mini-summary">
                <span>本批结果</span>
                <strong>
                  48<small> 份试卷</small>
                </strong>
                <em className="chip is-warn">待复核 16 题</em>
              </div>
              <ol className="mini-students">
                {STUDENTS.map((student, index) => (
                  <li key={student.index} style={cssVars({ "--i": index })}>
                    <span className="mini-index">{student.index}</span>
                    <span className="mini-name">
                      <strong>{student.name}</strong>
                      <small className={student.tone}>{student.state}</small>
                    </span>
                    <b>{student.score}</b>
                  </li>
                ))}
              </ol>
              <div className="mini-action">归档已完成结果</div>
            </div>
          </article>

          <article className="mode-card is-anonymous" data-reveal style={cssVars({ "--reveal-delay": 120 })}>
            <div className="mode-copy">
              <p className="mode-label">
                <FileScan aria-hidden="true" /> 无身份批阅
              </p>
              <h3>不建名单，也不只给一个错题率</h3>
              <p>每张图片独立批阅，不绑定学生、不写入学生档案；直接查看高频错题、判定结果和知识点。</p>
              <ul>
                <li><Check aria-hidden="true" />无需学生姓名与班级名单</li>
                <li><Check aria-hidden="true" />按页面人工排除异常结果</li>
                <li><Check aria-hidden="true" />整批洞察完成即看即用</li>
              </ul>
            </div>
            <div className="mode-preview" aria-label="无身份批阅界面示意：已判正确率百分之七十六，第 12 题错误最多" role="img">
              <div className="mini-bar">
                <span className="logo-mark is-small" aria-hidden="true">M</span>
                <strong>无身份批阅</strong>
                <span className="mini-pill is-quiet">脱敏示意</span>
              </div>
              <div className="mini-overview">
                <svg className="mini-ring" viewBox="0 0 120 120" aria-hidden="true">
                  <circle className="mini-ring-track" cx="60" cy="60" r="48" />
                  <circle className="mini-ring-value" cx="60" cy="60" r="48" pathLength={100} />
                  <text x="60" y="60" textAnchor="middle" className="mini-ring-number">76%</text>
                  <text x="60" y="80" textAnchor="middle" className="mini-ring-label">已判正确率</text>
                </svg>
                <dl className="mini-metrics">
                  <div>
                    <dt>批阅范围</dt>
                    <dd>48<small> 页</small></dd>
                  </div>
                  <div>
                    <dt>错题总量</dt>
                    <dd>182<small> 题</small></dd>
                  </div>
                </dl>
              </div>
              <div className="mini-highlight">
                <span>优先讲解</span>
                <strong>第 12 题</strong>
                <em>18 次错误 · 错题率 38%</em>
              </div>
              <ol className="mini-pages">
                {PAGES.map((page) => (
                  <li key={page.label} className={cn(page.excluded && "is-excluded")}>
                    <span>{page.label}</span>
                    <em>{page.state}</em>
                  </li>
                ))}
              </ol>
              <div className="mini-knowledge">
                <span>薄弱知识点</span>
                <b>函数单调性</b>
                <b>数列求和</b>
                <b>空间向量</b>
              </div>
            </div>
          </article>
        </div>

        <div className="compare" data-reveal>
          <table>
            <caption className="sr-only">有身份批阅与无身份批阅对比</caption>
            <thead>
              <tr>
                <th scope="col"><span className="sr-only">对比项</span></th>
                <th scope="col">有身份批阅</th>
                <th scope="col">无身份批阅</th>
              </tr>
            </thead>
            <tbody>
              {COMPARISON.map((row) => (
                <tr key={row.label}>
                  <th scope="row">{row.label}</th>
                  <td>
                    <Check aria-hidden="true" className="compare-yes" />
                    {row.identified}
                  </td>
                  <td>
                    {row.both ? (
                      <Check aria-hidden="true" className="compare-yes" />
                    ) : (
                      <Minus aria-hidden="true" className="compare-no" />
                    )}
                    {row.anonymous}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="demo-note">
          <ShieldCheck aria-hidden="true" /> 界面和数据均为脱敏展示，不含真实学生信息
        </p>
      </div>
    </section>
  )
}
