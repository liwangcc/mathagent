import assert from "node:assert/strict"
import test from "node:test"

import { ENGAGE_FRACTION, RESUME_AFTER_MS, ScrollPlayer } from "../src/lib/scrollPlayer.ts"

// A pinned section 2,000 px of scroll long, starting at 1,000 px, on an 800 px viewport.
const START = 1000
const TRAVEL = 2000
const VIEWPORT = 800
const at = (scrollY) => ({ start: START, travel: TRAVEL, scrollY, viewport: VIEWPORT })

// Runs the player the way the page does: a frame every 16 ms, moving the page to what it asks.
function play(player, { from, now = 0, frames, round = value => value }) {
  let scrollY = from
  for (let i = 0; i < frames; i++, now += 16) {
    const next = player.step(now, at(scrollY))
    if (next !== null) {
      scrollY = round(next)
      player.scrolled(now, scrollY) // the page's scroll event for the player's own move
    }
  }
  return { scrollY, now }
}

test("once pinned it plays the whole section in `seconds`, then stops at its end", () => {
  const player = new ScrollPlayer(18)
  const half = play(player, { from: START, frames: Math.round(9000 / 16) })
  assert.ok(Math.abs(half.scrollY - (START + TRAVEL / 2)) < 10, `halfway after 9 s, at ${half.scrollY}`)
  const done = play(player, { from: half.scrollY, now: half.now, frames: Math.round(9500 / 16) })
  assert.equal(done.scrollY, START + TRAVEL, "it ends exactly at the end of the pinned travel")
  assert.equal(player.step(done.now, at(done.scrollY)), null, "and stays there")
})

test("it waits until the section reaches the upper part of the viewport", () => {
  const player = new ScrollPlayer(18)
  const below = START - VIEWPORT * ENGAGE_FRACTION - 40
  assert.equal(play(player, { from: below, frames: 120 }).scrollY, below, "a section still low on the screen is left alone")
  const near = START - VIEWPORT * ENGAGE_FRACTION + 40
  assert.ok(play(player, { from: near, frames: 120 }).scrollY > near, "one in the upper quarter starts playing")
})

test("the wheel takes over, and the play resumes from wherever the reader left it", () => {
  const player = new ScrollPlayer(18)
  let { scrollY, now } = play(player, { from: START, frames: 200 })
  // The reader wheels back to the start of the section.
  player.yieldTo(now)
  scrollY = START + 100
  player.scrolled(now, scrollY)
  const still = play(player, { from: scrollY, now: now + 16, frames: Math.floor((RESUME_AFTER_MS - 32) / 16) })
  assert.equal(still.scrollY, START + 100, "nothing moves while the reader is in charge")
  const resumed = play(player, { from: still.scrollY, now: now + RESUME_AFTER_MS + 16, frames: 60 })
  assert.ok(resumed.scrollY > START + 100 && resumed.scrollY < START + 300, `it plays on from ${START + 100}, now at ${resumed.scrollY}`)
})

test("a scroll the player did not make (the scrollbar, an anchor jump) also hands the page over", () => {
  const player = new ScrollPlayer(18)
  const { scrollY, now } = play(player, { from: START, frames: 30 })
  player.scrolled(now, scrollY + 1) // its own move, as the browser rounded it
  assert.notEqual(player.step(now + 16, at(scrollY + 1)), null)
  player.scrolled(now + 32, scrollY + 600) // the reader dragged the scrollbar
  assert.equal(player.step(now + 48, at(scrollY + 600)), null)
  assert.notEqual(player.step(now + 48 + RESUME_AFTER_MS, at(scrollY + 600)), null)
})

test("a long gap between frames (a background tab) moves the page by one short step only", () => {
  const player = new ScrollPlayer(18)
  player.step(0, at(START))
  const next = player.step(60_000, at(START))
  assert.ok(next - START <= (TRAVEL / 18) * 0.05 + 1e-9, `moved ${next - START} px`)
})

test("a slow play still advances on a browser that rounds scroll positions to whole pixels", () => {
  const player = new ScrollPlayer(120) // about 0.27 px per frame
  const { scrollY } = play(player, { from: START, frames: Math.round(3000 / 16), round: Math.round })
  assert.ok(Math.abs(scrollY - (START + (TRAVEL / 120) * 3)) <= 2, `moved to ${scrollY}`)
})

test("a section with nothing to scroll through never plays", () => {
  const player = new ScrollPlayer(18)
  for (const now of [0, 16, 32]) assert.equal(player.step(now, { start: START, travel: 0, scrollY: START, viewport: VIEWPORT }), null)
})
