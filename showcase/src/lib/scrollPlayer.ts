/* Autoplay for the pinned, scroll-driven sections (把夜晚还给老师, 墨迹星图). Their animation
 * follows the scroll position, so playing one means moving the page through it, at one full
 * travel per `seconds`. The reader stays in charge: a reading gesture (wheel, touch, a scroll
 * key, a pointer press) or any scroll the player did not make itself (the scrollbar, an anchor
 * jump) hands the page back at once, and after `resumeAfter` ms of stillness the play goes on
 * from wherever the reader left it, forwards or backwards. It starts once the section's top is
 * in the upper part of the viewport, stops at the section's end and never scrolls past it.
 *
 * Pure: the caller reports time, gestures, scroll events and the section's geometry, and moves
 * the page to whatever `step` returns. */

export const RESUME_AFTER_MS = 1200

/* Engage when the section's top is within this share of the viewport from the top. */
export const ENGAGE_FRACTION = 0.25

/* Longest step, in seconds: a stalled frame (a background tab) resumes without a jump. */
const MAX_STEP_S = 0.05

/* A scroll this close to where the player put the page is the player's own. */
const OWN_SCROLL_PX = 2

export interface SectionGeometry {
  /** Scroll position at which the section pins (its top in page coordinates). */
  start: number
  /** How far the page scrolls while the section stays pinned. */
  travel: number
  /** The page's current scroll position. */
  scrollY: number
  /** The viewport's height. */
  viewport: number
}

export class ScrollPlayer {
  readonly seconds: number
  readonly resumeAfter: number
  private position: number | null = null
  private lastStep: number | null = null
  private quietUntil = 0

  constructor(seconds: number, resumeAfter = RESUME_AFTER_MS) {
    this.seconds = seconds
    this.resumeAfter = resumeAfter
  }

  /** The reader takes over; the play waits until they have been still for `resumeAfter` ms. */
  yieldTo(now: number) {
    this.quietUntil = now + this.resumeAfter
    this.lastStep = null
    this.position = null
  }

  /** A scroll event: anything but the player's own move is the reader's. */
  scrolled(now: number, scrollY: number) {
    if (this.position === null || Math.abs(scrollY - this.position) > OWN_SCROLL_PX) this.yieldTo(now)
  }

  /** Where to move the page now, or null to leave it where it is. */
  step(now: number, { start, travel, scrollY, viewport }: SectionGeometry): number | null {
    const end = start + travel
    const engaged = travel > 0 && scrollY >= start - viewport * ENGAGE_FRACTION && scrollY < end - 0.5
    if (!engaged || now < this.quietUntil) {
      this.lastStep = null
      this.position = null
      return null
    }
    // Its own last position, not the page's: browsers round scroll positions, and a slow play
    // moves less than a pixel per frame.
    const from = this.position ?? scrollY
    const elapsed = this.lastStep === null ? 0 : Math.min(MAX_STEP_S, (now - this.lastStep) / 1000)
    this.lastStep = now
    // Below the pinned range the page first glides up to it at the same pace.
    const next = Math.min(end, from + (travel / this.seconds) * elapsed)
    this.position = next
    return next
  }
}
