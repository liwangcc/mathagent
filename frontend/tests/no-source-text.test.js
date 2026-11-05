import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import test from 'node:test'

// Frontend tests check behaviour: import the module under test and call it
// (helpers/srcModules.js loads modules that use the `@/` alias), or drive it.
// Matching the text of frontend/src pins how the code is written, keeps
// passing when what it does breaks, and fails a refactor that keeps it.
//
// A test file reads source text when it names a frontend/src .vue, .js or .ts
// file anywhere other than as an import specifier (readFileSync, readFile, a
// `read()` helper, a URL, a path). KNOWN_SOURCE_TEXT_READS lists what each
// existing file still reads. It may only shrink: a new read fails, and so
// does a listed read that a file no longer has.
const KNOWN_SOURCE_TEXT_READS = {
  'anonymous-grade.test.js': ['router/index.js', 'views/AnonymousGrade.vue', 'views/Layout.vue', 'views/Upload.vue'],
  'answer-sheet-pairing.test.js': ['views/AnswerSheet.vue'],
  'answer-sheet-redesign.test.js': ['views/AnswerSheet.vue'],
  'answer-sheet-review-workbench.test.js': ['views/AnswerSheet.vue', 'views/GradeRecords.vue'],
  'answer-sheet-workflow.test.js': ['api/index.js', 'router/index.js', 'views/AnswerSheet.vue', 'views/Layout.vue'],
  'bank-photo-stage-runtime.test.js': ['views/BankPhotoImport.vue'],
  'bank-profile-scope-runtime.test.js': ['views/Bank.vue'],
  'bank-stage-contract.test.js': ['views/Bank.vue', 'views/BankPhotoImport.vue', 'views/PendingAnswers.vue'],
  'batch-report.test.js': ['views/Upload.vue'],
  'browser-image-offload.test.js': ['utils/uploadValidation.js', 'views/AnswerSheet.vue', 'views/Upload.vue'],
  'class-table-render-contract.test.js': ['views/ClassTable.vue'],
  'collection-import.test.js': ['views/Upload.vue'],
  'courseware-covers.test.js': ['components/CoursewareCover.vue', 'views/Courseware.vue'],
  'derived-features.test.js': ['components/ChatBar.vue', 'views/${view}.vue', 'views/Layout.vue'],
  'desktop-auth-contract.test.js': ['views/Settings.vue'],
  'desktop-binding-dialog.test.js': ['${path}', 'components/DesktopBindingDialog.vue', 'components/UsageGuideDialog.vue'],
  'desktop-startup-splash.test.js': ['main.js'],
  'drawing-assessment.test.mjs': ['components/DrawingAssessment.vue'],
  'drawing-reference.test.mjs': ['components/DrawingReferenceEditor.vue'],
  'education-stage-profile.test.js': ['router/index.js', 'stores/auth.js'],
  'exercise-mode-contract.test.js': ['views/Exercise.vue'],
  'exercise-profile-stage.test.js': ['views/Exercise.vue'],
  'exercise-restore-runtime.test.js': ['views/Exercise.vue'],
  'grading-display.test.js': ['components/GradingProgress.vue', 'components/InkSpinner.vue', 'components/SheetLoader.vue', 'views/AnswerSheet.vue', 'views/Stats.vue', 'views/Upload.vue'],
  'grading-image-only-contract.test.js': ['views/Upload.vue'],
  'grading-quick-dock.test.mjs': ['components/GradingQuickDock.vue'],
  'home-guidance.test.js': ['components/UsageGuideDialog.vue', 'views/Home.vue'],
  'knowledge-classification.test.js': ['views/Bank.vue', 'views/PendingAnswers.vue'],
  'local-help.test.js': ['components/HelpButton.vue', 'views/${view}'],
  'math-stage-profile-views.test.js': ['views/${page}.vue'],
  'model-button-single-flight.test.js': ['views/${name}'],
  'monthly-quota.test.js': ['views/Upload.vue'],
  'ocr-availability-privacy.test.js': ['views/Upload.vue'],
  'ordinary-paper-review-workbench.test.js': ['views/Upload.vue'],
  'primary-stage.test.js': ['views/${name}.vue'],
  'recognition-auto-start.test.js': ['views/Upload.vue'],
  'recognition-network-error.test.js': ['views/Upload.vue'],
  'recognition-recovery-runtime.test.js': ['views/Upload.vue'],
  'recognition-resume.test.js': ['views/Upload.vue'],
  'recognition-status-display.test.js': ['views/Upload.vue'],
  'registration-advisory.test.js': ['views/Upload.vue'],
  'report-center.test.js': ['${path}'],
  'stats-contract.test.js': ['views/Stats.vue'],
  'student-detail-error-state.test.js': ['views/StudentDetail.vue'],
  'student-submission.test.js': ['router/index.js'],
  'system-notice.test.js': ['components/UsageGuideDialog.vue', 'views/Home.vue'],
  'template-auto-crop.test.js': ['views/Upload.vue'],
  'template-choice-no-transcription.test.mjs': ['views/Upload.vue'],
  'template-choice-slot-consistency.test.mjs': ['views/Upload.vue'],
  'template-column-layout.test.js': ['views/AnonymousGrade.vue', 'views/Upload.vue'],
  'template-correction-confirmation.test.js': ['views/Upload.vue'],
  'template-crop-editing.test.js': ['views/Upload.vue'],
  'template-cut-lifecycle.test.js': ['views/Upload.vue'],
  'template-drawing-preview.test.mjs': ['components/DrawingReferenceEditor.vue', 'views/Upload.vue'],
  'template-geometry-review-resolve.test.js': ['views/Upload.vue'],
  'template-geometry-tools.test.js': ['views/Upload.vue'],
  'template-hierarchy-review.test.js': ['views/Upload.vue'],
  'template-id-only-multiline.test.mjs': ['views/Upload.vue'],
  'template-layout-save.test.js': ['views/Upload.vue'],
  'template-recognition-button.test.js': ['views/Upload.vue'],
  'template-recognition-network-state.test.js': ['views/Upload.vue'],
  'template-review.test.js': ['views/Upload.vue'],
  'template-skip-answer-confirm.test.js': ['views/Upload.vue'],
  'template-slot-binding.test.mjs': ['views/Upload.vue'],
  'text-grading-method.test.mjs': ['views/AnswerSheet.vue', 'views/Upload.vue'],
  'theme-mode.test.js': ['composables/useTheme.js', 'views/Login.vue', 'views/Profile.vue'],
  'usage-guide-contract.test.js': ['App.vue', 'components/GuidedTour.vue', 'components/UsageGuideDialog.vue', 'composables/useDesktopBindingDialog.js', 'views/ClassTable.vue', 'views/Exercise.vue', 'views/Home.vue', 'views/Layout.vue', 'views/Upload.vue'],
}

const IMPORT_SPECIFIERS = [
  /\b(?:import|export)\b[^'"`;]*?\bfrom\s*(['"])[^'"\n]*\1/g,
  /\bimport\s*(['"])[^'"\n]*\1/g,
  /\bimport\s*\(\s*(['"`])[^'"`\n]*\1\s*\)/g,
]
const STRING_LITERAL = /(['"`])((?:(?!\1)[^\\\n]|\\.)*)\1/g
// `src/<path>.vue|.js|.ts`, or `src/<path with ${…}>` whose file is computed.
const SOURCE_PATH = /(?:^|[/.])src\/((?:[^'"`\n]*\.(?:vue|js|ts))|(?:[^'"`\n]*\$\{[^'"`\n]*))$/

export function sourceTextReads(text) {
  let code = text
  for (const specifier of IMPORT_SPECIFIERS) code = code.replace(specifier, '')
  const reads = new Set()
  for (const [, , value] of code.matchAll(STRING_LITERAL)) {
    const path = SOURCE_PATH.exec(value)
    if (path) reads.add(path[1])
  }
  return [...reads].sort()
}

function testFileReads() {
  const here = new URL('./', import.meta.url)
  return Object.fromEntries(
    readdirSync(here, { recursive: true })
      .map(name => name.split('\\').join('/'))
      .filter(name => /\.m?js$/.test(name) && name !== 'no-source-text.test.js')
      .sort()
      .map(name => [name, sourceTextReads(readFileSync(new URL(name, here), 'utf8'))])
      .filter(([, reads]) => reads.length),
  )
}

test('frontend tests read no frontend source text beyond the shrinking known list', () => {
  const reads = testFileReads()
  const unexpected = {}
  const stale = {}
  for (const [name, files] of Object.entries(reads)) {
    const extra = files.filter(file => !(KNOWN_SOURCE_TEXT_READS[name] ?? []).includes(file))
    if (extra.length) unexpected[name] = extra
  }
  for (const [name, files] of Object.entries(KNOWN_SOURCE_TEXT_READS)) {
    const gone = files.filter(file => !(reads[name] ?? []).includes(file))
    if (gone.length) stale[name] = gone
  }
  assert.deepEqual(unexpected, {}, 'write a behaviour test instead of reading frontend/src')
  assert.deepEqual(stale, {}, 'remove what these tests no longer read from KNOWN_SOURCE_TEXT_READS')
})

test('source text detection tells reads from imports', () => {
  const samples = {
    "readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')": ['views/Upload.vue'],
    "await readFile(new URL('../src/api/index.js', import.meta.url), 'utf8')": ['api/index.js'],
    "const view = read('../src/views/AnswerSheet.vue')": ['views/AnswerSheet.vue'],
    "read('frontend/src/components/CoursewareCover.vue')": ['components/CoursewareCover.vue'],
    "path.resolve(import.meta.dirname, '../src/views/Exercise.vue')": ['views/Exercise.vue'],
    'source(`../src/views/${view}.vue`)': ['views/${view}.vue'],
    "import { consumeSse } from '../src/utils/sse.js'": [],
    "import {\n  a,\n  b,\n} from '../src/utils/templateReview.js'": [],
    "const { default: api } = await import('../src/api/index.js')": [],
    "readFileSync(new URL('../src/styles/global.css', import.meta.url))": [],
    "new URL('../src/assets/docs/manual.docx', import.meta.url)": [],
  }
  for (const [text, expected] of Object.entries(samples)) {
    assert.deepEqual(sourceTextReads(text), expected, text)
  }
})
