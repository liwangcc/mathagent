import { ChartColumn, FileCheck2, FolderUp, PenLine, UserCheck } from "lucide-react"

import { SectionHead } from "@/components/SectionHead"
import { useInView } from "@/lib/hooks"
import { cn, cssVars } from "@/lib/utils"

const STEPS = [
  {
    icon: FileCheck2,
    title: "答案依据",
    body: "上传模板与参考答案，系统转录题目、建立题目目录；参考答案经老师确认后才作为判分依据。",
  },
  {
    icon: FolderUp,
    title: "学生试卷",
    body: "拍照上传，或直接导入文件夹与 ZIP / RAR 压缩包；也可以用“姓名＋学号”给图片或文件夹命名。",
  },
  {
    icon: UserCheck,
    title: "识别确认",
    body: "逐页核验姓名与学号，后一页不会自动继承前一页；低置信度的身份保留给老师确认。",
  },
  {
    icon: PenLine,
    title: "批改审阅",
    body: "选择题机械比对，解答题逐题判分；部分正确与待复核的题目由老师对照原卷定夺。",
  },
  {
    icon: ChartColumn,
    title: "学情洞察",
    body: "高频错题、判定分布与薄弱知识点一屏可读；确认后的结果才进入学生档案与班级统计。",
  },
]

export function Workflow() {
  const [flowRef, inView] = useInView<HTMLOListElement>({ threshold: 0.3 })

  return (
    <section id="flow" className="section" aria-labelledby="flow-title">
      <div className="wrap">
        <SectionHead
          index="01"
          label="批阅流程"
          titleId="flow-title"
          title="从一摞纸，"
          accent="到一份能用的学情。"
          lead="五个环节，一条主线：AI 承担识别、比对与汇总这些重复步骤；每一处需要判断的地方，都会停下来交给老师。"
        />

        <div className={cn("flow", inView && "is-drawn")}>
          <svg className="flow-line" viewBox="0 0 1000 24" preserveAspectRatio="none" aria-hidden="true">
            <path className="flow-line-track" d="M0 12 C 120 4, 220 20, 340 12 S 560 4, 680 12 S 880 20, 1000 12" />
            <path className="flow-line-ink" pathLength={1} d="M0 12 C 120 4, 220 20, 340 12 S 560 4, 680 12 S 880 20, 1000 12" />
          </svg>
          <ol ref={flowRef} className="flow-steps">
            {STEPS.map(({ icon: Icon, title, body }, index) => (
              <li key={title} className="flow-step" style={cssVars({ "--i": index })}>
                <span className="flow-marks" aria-hidden="true">
                  <span className="flow-node">{String(index + 1).padStart(2, "0")}</span>
                  <span className="flow-icon">
                    <Icon />
                  </span>
                </span>
                <h3>{title}</h3>
                <p>{body}</p>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  )
}
