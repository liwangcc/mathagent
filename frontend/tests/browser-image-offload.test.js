import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const answerSheet = read('../src/views/AnswerSheet.vue')
const upload = read('../src/views/Upload.vue')
const preprocessing = read('../src/utils/uploadValidation.js')

test('AnswerSheet FilePicker serially prepares every selected image and blocks duplicate selection', () => {
  assert.match(answerSheet, /import \{ compressImageFiles, validateUploadList \} from '@\/utils\/uploadValidation'/)
  assert.equal((answerSheet.match(/<FilePicker/g) || []).length, 4)
  assert.match(answerSheet, /const selecting = ref\(false\)/)
  assert.match(answerSheet, /if \(selecting\.value \|\| props\.disabled \|\| !selected\.length\) return/)
  assert.match(answerSheet, /disabled: props\.disabled \|\| selecting\.value/)
  assert.match(answerSheet, /await compressImageFiles\(result\.files, 1920, 0\.88/)
  assert.match(answerSheet, /fileSelectionProcessingCount\.value \+= 1/)
  assert.match(answerSheet, /:disabled="sourceLocked \|\| fileSelectionProcessing"/)
  assert.match(answerSheet, /:disabled="layoutFrozen \|\| fileSelectionProcessing"/)
  assert.match(answerSheet, /studentUploadProcessing \|\| fileSelectionProcessing/)
})

test('Upload prepares both template and student images before either recognition path', () => {
  const templateStart = upload.indexOf('async function onTemplateFileChange(e)')
  const templateEnd = upload.indexOf('function templateRecognitionStorageKey()', templateStart)
  const studentStart = upload.indexOf('async function processFiles(')
  const studentEnd = upload.indexOf('async function onFileInputChange(e)', studentStart)
  assert.ok(templateStart >= 0 && templateEnd > templateStart)
  assert.ok(studentStart >= 0 && studentEnd > studentStart)

  const templatePath = upload.slice(templateStart, templateEnd)
  const studentPath = upload.slice(studentStart, studentEnd)
  assert.match(templatePath, /templatePreprocessing\.value = true/)
  assert.match(templatePath, /compressImageFiles\(result\.files, 1920, 0\.88/)
  assert.match(studentPath, /imagePreprocessing\.value = true/)
  assert.match(studentPath, /const preparedFiles = await compressImageFiles\(result\.files, 1920, 0\.88/)
  assert.match(studentPath, /if \(failedCount\)[\s\S]*fileList\.value = \[\]/)
  assert.doesNotMatch(studentPath, /fileList\.value = result\.files/)
  assert.match(upload, /:disabled="imagePreprocessing \|\| imageExtracting \|\| recognitionBlocksNewWork"/)
  assert.match(upload, /:disabled="templatePreprocessing \|\| templateServiceBlocksNewWork"/)
})

test('the shared OCR-ready contract is globally serial and cannot label failed originals ready', () => {
  assert.match(preprocessing, /const DEFAULT_QUALITY = 0\.88/)
  assert.match(preprocessing, /const MAX_DECODED_SOURCE_PIXELS = 16_000_000/)
  assert.match(preprocessing, /assertDecodedImageWithinPixelBudget\(width, height\)[\s\S]*document\.createElement\('canvas'\)/)
  assert.match(preprocessing, /const OCR_READY_SUFFIX = '\.ocr-ready-v1\.jpg'/)
  assert.match(preprocessing, /let imagePreprocessTail = Promise\.resolve\(\)/)
  assert.match(preprocessing, /await previousBatch/)
  assert.match(preprocessing, /for \(const \[index, file\] of files\.entries\(\)\)/)
  assert.doesNotMatch(preprocessing, /originalFallbackFile/)
  assert.match(preprocessing, /export async function createRotatedOcrReadyFile/)
  assert.match(preprocessing, /canvas\.width = 0[\s\S]*canvas\.height = 0/)
  assert.match(preprocessing, /decoded\?\.close\(\)/)
})
