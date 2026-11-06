import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import test from 'node:test'
import { sourceTextReads, sourceTextReadsOf } from './helpers/sourceTextReads.js'

// Frontend tests check behaviour: import the module under test and call it
// (helpers/srcModules.js loads modules that use the `@/` alias), or drive it.
// Matching the text of frontend/src pins how the code is written, keeps
// passing when what it does breaks, and fails a refactor that keeps it.
//
// A test file reads source text when it names a frontend/src .vue, .js or .ts
// file anywhere other than as an import specifier (readFileSync, readFile, a
// `read()` helper, a URL, a path), or builds a path from a bare src directory
// segment (listed as `*`), or imports a test helper that does either: a helper
// reading source on a test's behalf is the same reading, and both are listed.
// KNOWN_SOURCE_TEXT_READS lists what each existing file still reads. It may
// only shrink: a new read fails, and so does a listed read that a file no
// longer has.
const KNOWN_SOURCE_TEXT_READS = {
  'answer-sheet-workflow.test.js': ['views/AnswerSheet.vue'],
  'batch-report.test.js': ['views/Upload.vue'],
  'class-table-render-contract.test.js': ['views/ClassTable.vue'],
  'courseware-covers.test.js': ['components/CoursewareCover.vue'],
  'drawing-reference.test.mjs': ['components/DrawingReferenceEditor.vue'],
  'grading-display.test.js': ['components/InkSpinner.vue', 'components/SheetLoader.vue', 'views/Upload.vue'],
  'ordinary-paper-review-workbench.test.js': ['views/Upload.vue'],
  'theme-mode.test.js': ['views/Login.vue'],
}

// Points at src/ only to resolve and load src modules the way the build does
// (the `@/` alias, import.meta.env, .vue files): Node's loader hands it a
// module's text, which it turns into the module and never gives to a test.
const MODULE_RESOLUTION = ['helpers/srcModuleHooks.js']

const here = new URL('./', import.meta.url)

function testFiles() {
  return Object.fromEntries(
    readdirSync(here, { recursive: true })
      .map(name => name.split('\\').join('/'))
      .filter(name => /\.m?js$/.test(name) && name !== 'no-source-text.test.js')
      .sort()
      .map(name => [name, readFileSync(new URL(name, here), 'utf8')]),
  )
}

function testFileReads() {
  return sourceTextReadsOf(testFiles(), { exempt: MODULE_RESOLUTION })
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

test('a test reads what the test helpers it imports read, transitively', () => {
  const files = {
    'uses-reader.test.js': "import { view } from './helpers/views.js'\nview('Upload')",
    'helpers/views.js': "import { read } from './read.js'\nexport const view = name => read(`views/${name}.vue`)",
    'helpers/read.js': "export const read = rel => readFileSync(new URL('../../src/' + rel, import.meta.url), 'utf8')",
    'uses-loader.test.js': "import './helpers/loader.js'\nconst { default: Upload } = await import('../src/views/Upload.vue?sfc')",
    'helpers/loader.js': "register('./hooks.js', import.meta.url)",
    'helpers/hooks.js': "const SRC = new URL('../../src/', import.meta.url)",
    'plain.test.js': "import { messages } from './helpers/elementPlus.js'",
    'helpers/elementPlus.js': 'export const messages = []',
  }
  assert.deepEqual(sourceTextReadsOf(files, { exempt: ['helpers/hooks.js'] }), {
    'helpers/read.js': ['*'],
    'helpers/views.js': ['*'],
    'uses-reader.test.js': ['*'],
  })
  // Only the exemption keeps the hooks' own src URL from counting as a read.
  assert.deepEqual(sourceTextReadsOf(files)['helpers/hooks.js'], ['*'])
})

test('the module-resolution hooks get src modules from the loader chain, never from the disk', () => {
  for (const name of MODULE_RESOLUTION) {
    const text = readFileSync(new URL(name, here), 'utf8')
    assert.doesNotMatch(text, /\b(?:readFile|readFileSync|createReadStream|openSync|readSync|readdir|readdirSync)\b/, name)
    assert.doesNotMatch(text, /\bfrom\s*['"](?:node:)?fs\/promises['"]/, name)
  }
})

test('source text detection tells reads from imports', () => {
  const samples = {
    "const { default: Upload } = await import('../src/views/Upload.vue?sfc')": [],
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
    "fs.readFileSync(path.resolve(here, '..', 'src', rel), 'utf8')": ['*'],
    "path.join(root, 'frontend', 'src', 'views', 'Courseware.vue')": ['*'],
    "new URL('../src/', import.meta.url)": ['*'],
    "readFileSync(new URL('../src/views/' + name, import.meta.url))": ['*'],
    "resolve: { alias: { '@': resolve('src') } }": [],
    "await img.getAttribute('src')": [],
  }
  for (const [text, expected] of Object.entries(samples)) {
    assert.deepEqual(sourceTextReads(text), expected, text)
  }
})
