// Geometry of the outcome donut (the showcase site's report chart): one stroked
// circle per outcome, each arc a share of the counted answers, with a small gap
// between neighbouring arcs. Pending answers are not judged and get no arc.
export const DONUT_RADIUS = 74
export const DONUT_CIRCUMFERENCE = 2 * Math.PI * DONUT_RADIUS
const ARC_GAP = 3

// `total` lets a caller scale the ring to a wider count (for example every
// question a student answered); answers outside the segments stay as bare track.
export function donutArcs(segments, total = 0) {
  const values = segments.map((segment) => Math.max(0, Number(segment.value) || 0))
  const sum = values.reduce((acc, value) => acc + value, 0)
  const denominator = Math.max(sum, Number(total) || 0)
  // A lone outcome closes the ring; a gap there would read as a missing sliver.
  const gap = values.filter((value) => value > 0).length > 1 ? ARC_GAP : 0
  let offset = 0
  return segments.map((segment, index) => {
    const value = values[index]
    const span = denominator ? (value / denominator) * DONUT_CIRCUMFERENCE : 0
    const arc = {
      ...segment,
      value,
      share: denominator ? (value / denominator) * 100 : 0,
      length: Math.max(0, span - gap),
      offset,
    }
    offset += span
    return arc
  })
}
