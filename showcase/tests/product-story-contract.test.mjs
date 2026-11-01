import assert from "node:assert/strict"
import test from "node:test"

import { allSource as source, read } from "./source.mjs"

test("showcase presents both teacher grading modes", () => {
  assert.match(source, /有身份批阅/)
  assert.match(source, /无身份批阅/)
  assert.match(source, /不绑定学生、不写入学生档案/)
})

test("showcase foregrounds review and batch insight instead of obsolete setup choices", () => {
  assert.match(source, /原卷与批阅结果/)
  assert.match(source, /高频错题/)
  assert.match(source, /判定结果与知识点/)
  assert.doesNotMatch(source, /错误类型与知识点|计算失误|方法不完整/)
  assert.doesNotMatch(source, /无模板（自动聚类）/)
  assert.doesNotMatch(source, /无答案（AI 生成）/)
})

test("the web workspace is the primary hero action", () => {
  const hero = read("src/components/Hero.tsx")
  const heroStart = hero.indexOf('id="top"')
  const heroEnd = hero.indexOf("</section>", heroStart)
  const heroMarkup = hero.slice(heroStart, heroEnd)

  assert.ok(heroStart >= 0 && heroEnd > heroStart)
  assert.ok(heroMarkup.indexOf("进入教师工作台") < heroMarkup.indexOf("查看两种批阅方式"))
  assert.doesNotMatch(heroMarkup, /下载 Windows 客户端/)
})

test("public privacy copy matches the current retention boundary", () => {
  assert.match(source, /终态原图按 36 小时保留策略自动清理/)
  assert.match(source, /错题、得分和正确率继续保留/)
})

test("the insight mock adds up", () => {
  const insights = read("src/components/Insights.tsx")
  const count = (key) => Number(insights.match(new RegExp(`key: "${key}", label: "[^"]+", count: ([0-9]+)`))[1])
  const judged = 768 - 16
  assert.equal(count("correct") + count("partial") + count("wrong"), judged)
  assert.equal(count("partial") + count("wrong"), 182)
})
