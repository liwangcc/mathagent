import assert from "node:assert/strict"
import test from "node:test"

import { allSource, read } from "./source.mjs"

const html = read("index.html")

test("the page loads no third-party video or font hosts", () => {
  for (const source of [allSource, html]) {
    assert.doesNotMatch(source, /<video/)
    assert.doesNotMatch(source, /cloudfront\.net/)
    assert.doesNotMatch(source, /fonts\.googleapis\.com|fonts\.gstatic\.com/)
  }
})

test("the hero grading demo can be paused and stops when unseen", () => {
  const specimen = read("src/components/GradingSpecimen.tsx")
  assert.match(specimen, /暂停演示/)
  assert.match(specimen, /播放演示/)
  assert.match(specimen, /if \(!playing \|\| !inView \|\| !pageVisible\) return/)
  assert.match(specimen, /document\.addEventListener\("visibilitychange"/)
})

test("reduced motion gets finished, static states instead of animation", () => {
  const specimen = read("src/components/GradingSpecimen.tsx")
  const evening = read("src/components/EveningScene.tsx")
  assert.match(specimen, /if \(!reduced\) return\s+setPlaying\(false\)\s+setStep\(STEP\.done\)/)
  assert.match(evening, /if \(reduced\) \{\s+render\(1\)\s+return/)
  assert.match(evening, /reduced && "is-static"/)
  assert.match(read("src/index.css"), /@media \(prefers-reduced-motion: reduce\)/)
})

test("the night-work story survives as the evening plate", () => {
  const evening = read("src/components/EveningScene.tsx")
  assert.match(evening, /23:40/)
  assert.match(evening, /18:10/)
  assert.match(evening, /data-part="stars"/)
  assert.match(evening, /100 页以内，系统流程通常 20 分钟内完成/)
})

test("unsigned beta metadata matches the current release", () => {
  const config = read("src/config.ts")
  assert.match(config, /const BETA_VERSION = "0\.1\.7"/)
  assert.match(
    config,
    /const BETA_SHA256 = "a27149a1f305a95d5b7160c1dbe9afad031043084c1e3eecf0fc6238625377ca"/,
  )
  assert.match(read("src/components/Download.tsx"), /未签名 Windows 内测版 · \{BETA_VERSION\}/)
})
