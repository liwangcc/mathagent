import { clamp } from "@/lib/hooks"

/* Every ink point has one position per stage; scrolling moves the points from one stage's layout to the next. */
export const STAGE = {
  hand: 0, // the student's handwriting on the page
  type: 1, // the same answer, transcribed
  judge: 2, // verdicts; the illegible answer goes to the teacher
  atlas: 3, // a class's mistakes as a star chart of knowledge points
  ledger: 4, // 50 students x 600 open wrong records
  clusters: 5, // the same records merged by question fingerprint
  finale: 6, // the tagline
} as const

/* Captions: 读 判 汇 追 见. */
export const ACT_COUNT = 5

interface Segment {
  start: number
  end: number
  from: number
  to: number
  act: number
}

/* Section progress windows. Holds are long enough to read the caption before the next move starts. */
const SEGMENTS: Segment[] = [
  { start: 0.0, end: 0.12, from: STAGE.hand, to: STAGE.hand, act: 0 }, // writing
  { start: 0.12, end: 0.16, from: STAGE.hand, to: STAGE.hand, act: 0 },
  { start: 0.16, end: 0.26, from: STAGE.hand, to: STAGE.type, act: 0 }, // scan: ink leaves the page
  { start: 0.26, end: 0.3, from: STAGE.type, to: STAGE.type, act: 0 },
  { start: 0.3, end: 0.37, from: STAGE.type, to: STAGE.judge, act: 1 },
  { start: 0.37, end: 0.43, from: STAGE.judge, to: STAGE.judge, act: 1 },
  { start: 0.43, end: 0.53, from: STAGE.judge, to: STAGE.atlas, act: 2 },
  { start: 0.53, end: 0.61, from: STAGE.atlas, to: STAGE.atlas, act: 2 },
  { start: 0.61, end: 0.68, from: STAGE.atlas, to: STAGE.ledger, act: 3 },
  { start: 0.68, end: 0.71, from: STAGE.ledger, to: STAGE.ledger, act: 3 },
  { start: 0.71, end: 0.79, from: STAGE.ledger, to: STAGE.clusters, act: 3 },
  { start: 0.79, end: 0.88, from: STAGE.clusters, to: STAGE.clusters, act: 3 }, // threads to the new paper
  { start: 0.88, end: 0.95, from: STAGE.clusters, to: STAGE.finale, act: 4 },
  { start: 0.95, end: 1.0001, from: STAGE.finale, to: STAGE.finale, act: 4 },
]

/* Where each act is fully told; the rail jumps here and reduced motion shows only these frames. */
export const ACT_REST = [0.14, 0.4, 0.57, 0.85, 0.98]

export interface Frame {
  progress: number
  from: number
  to: number
  /** Eased 0..1 between the two stage layouts. */
  mix: number
  /** Share of the handwriting already on the page. */
  write: number
  act: number
  /** 0..1 inside the current segment, for overlay timing. */
  local: number
}

const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

export function frameAt(progress: number): Frame {
  const p = clamp(progress)
  const segment = SEGMENTS.find((item) => p >= item.start && p < item.end) ?? SEGMENTS[SEGMENTS.length - 1]
  const local = clamp((p - segment.start) / (segment.end - segment.start))
  const moving = segment.from !== segment.to
  return {
    progress: p,
    from: segment.from,
    to: segment.to,
    mix: moving ? easeInOut(local) : 0,
    write: p < SEGMENTS[0].end ? easeInOut(local) : 1,
    act: segment.act,
    local,
  }
}

/** Reduced motion: every scroll position shows the finished frame of its act, never a transition. */
export function restingFrame(progress: number): Frame {
  return frameAt(ACT_REST[frameAt(progress).act])
}

/** How far a given stage is shown in a frame, for overlay layers that belong to one stage. */
export function stageWeight(frame: Frame, stage: number) {
  if (frame.from === stage && frame.to === stage) return 1
  if (frame.from === stage) return 1 - frame.mix
  if (frame.to === stage) return frame.mix
  return 0
}
