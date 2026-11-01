import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const component = fs.readFileSync(
  new URL('../src/components/HelpButton.vue', import.meta.url),
  'utf8',
)

const teacherViews = [
  'Home.vue',
  'Upload.vue',
  'AnswerSheet.vue',
  'Exercise.vue',
  'ClassTable.vue',
  'Students.vue',
  'Bank.vue',
  'BankPhotoImport.vue',
  'GradeRecords.vue',
  'Reports.vue',
  'Stats.vue',
  'Settings.vue',
  'Profile.vue',
  'PendingAnswers.vue',
  'StudentDetail.vue',
]

test('local help button is accessible and renders a concise popover', () => {
  assert.match(component, /trigger="click"/)
  assert.match(component, /aria-label="`查看\$\{title\}`"/)
  assert.match(component, /class="local-help-content"/)
  assert.match(component, /v-for="item in items"/)
})

for (const view of teacherViews) {
  test(`${view} exposes local help for teacher functions`, () => {
    const source = fs.readFileSync(new URL(`../src/views/${view}`, import.meta.url), 'utf8')
    assert.match(source, /HelpButton/)
  })
}
