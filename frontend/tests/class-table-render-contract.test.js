import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/views/ClassTable.vue', import.meta.url), 'utf8')

test('班级管理页导入并使用 reactive，避免 setup 初始化时整页白屏', () => {
  assert.match(source, /import\s*\{[^}]*\breactive\b[^}]*\}\s*from\s*['"]vue['"]/)
  assert.match(source, /const coverage = reactive\(/)
})

test('添加学生时提示同名学生填写学号确认身份', () => {
  assert.match(source, /如果班级中有同名学生，请填写学号，以便准确确定身份。/)
})

test('操作列给四个按钮预留完整宽度并消除 Element Plus 的重复左边距', () => {
  assert.match(source, /<el-table-column label="操作" width="300" fixed="right">/)
  assert.match(source, /\.action-btns\s*\{[^}]*white-space:\s*nowrap;/s)
  assert.match(source, /\.action-btns :deep\(\.el-button\)\s*\{[^}]*flex:\s*0 0 auto;[^}]*margin-left:\s*0;/s)
})
