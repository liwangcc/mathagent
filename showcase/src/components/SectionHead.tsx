import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

export function SectionHead({
  index,
  label,
  title,
  accent,
  lead,
  titleId,
  className,
}: {
  index: string
  label: string
  title: ReactNode
  accent?: ReactNode
  lead?: ReactNode
  titleId: string
  className?: string
}) {
  return (
    <div className={cn("section-head", className)} data-reveal>
      <p className="kicker">
        <span className="kicker-num">{index}</span>
        <span>{label}</span>
      </p>
      <div>
        <h2 id={titleId} className="display">
          {title}
          {accent && <em>{accent}</em>}
        </h2>
        {lead && <p className="lead">{lead}</p>}
      </div>
    </div>
  )
}
