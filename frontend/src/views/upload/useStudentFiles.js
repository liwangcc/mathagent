/**
 * Where the student papers come from: the file and folder pickers, drag and drop, phone photos and pages
 * students submitted through a class link.
 *
 * - processFiles validates, extracts and prepares the chosen files; onFileInputChange, onFolderInputChange
 *   and onDrop feed it; resetFiles clears the list.
 * - Phone capture and student submissions are imported into the same list; a capture session is released
 *   once a grading batch has stored its own copies.
 * - triggerFileUpload, triggerFolderUpload and triggerTemplateUpload open the hidden file inputs.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import { forgetPhoneCaptureSession, loadPhoneCaptureSession } from '@/utils/phoneCaptureSession'
import api from '@/api'
import { validateUploadList, compressImageFiles } from '@/utils/uploadValidation'
import { extractImagesFromDrop, extractImagesFromFileList } from '@/utils/fileExtraction'

export function useStudentFiles(ctx) {
  const {
    auth, fileInputRef, folderInputRef, templateInputRef, fileList, phoneCaptureVisible,
    studentSubmissionVisible, imagePreprocessing, imageExtracting, extractionProgress, isDragover,
    templatePreprocessing, recognitionAutoArmed, requireRecognitionService, requireTemplateService,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const clearReviewDisplay = (...args) => ctx.clearReviewDisplay(...args)

  // Capture session whose photos make up fileList, released once grading starts.
  let phoneCaptureSessionForFiles = ''

  function triggerFileUpload() {
    if (imagePreprocessing.value || imageExtracting.value) {
      ElMessage.warning('图片正在处理中，请稍候')
      return
    }
    if (requireRecognitionService()) fileInputRef.value?.click()
  }

  function triggerFolderUpload() {
    if (imagePreprocessing.value || imageExtracting.value) {
      ElMessage.warning('图片正在处理中，请稍候')
      return
    }
    if (requireRecognitionService()) folderInputRef.value?.click()
  }

  function triggerTemplateUpload() {
    if (templatePreprocessing.value) {
      ElMessage.warning('模板图片正在处理中，请稍候')
      return
    }
    if (requireTemplateService()) templateInputRef.value?.click()
  }

  async function processFiles(rawFiles, { fromExtraction = false } = {}) {
    if (imagePreprocessing.value || (imageExtracting.value && !fromExtraction)) {
      ElMessage.warning('图片正在处理中，请稍候')
      return
    }
    imagePreprocessing.value = true
    let failedCount = 0
    const failedFiles = []
    try {
      const imageFiles = rawFiles.filter((file) => file?.type?.startsWith('image/'))
      if (imageFiles.length !== rawFiles.length) {
        throw new Error('[IMPORT_UNSUPPORTED_FILE] 批改仅支持图片，请先完成文件提取')
      }
      const fakeList = imageFiles.map((f) => ({ raw: f, name: f.name, size: f.size }))
      const result = validateUploadList(fakeList, { maxFiles: 200 })
      if (result.errors.length) throw new Error(result.errors.join('；'))
      if (!result.files.length) return
      const preparedFiles = await compressImageFiles(result.files, 1920, 0.88, {
        onFileError: ({ file }) => { failedCount += 1; failedFiles.push(file.name) },
      })
      if (failedCount) {
        fileList.value = []
        ElMessage.error(`[IMPORT_IMAGE_INVALID] ${failedFiles.join('、')}：${failedCount} 张学生试卷图片损坏或尺寸超限，本次选择已清空，请检查原图后重新选择`)
        return
      }
      recognitionAutoArmed.value = true
      fileList.value = preparedFiles
      phoneCaptureSessionForFiles = ''
      clearReviewDisplay()
      return true
    } catch (err) {
      ElMessage.error(err.message || '学生试卷预处理未完成，请重新选择')
    } finally {
      imagePreprocessing.value = false
    }
  }

  async function onFileInputChange(e) {
    const files = Array.from(e.target.files || [])
    e.target.value = ''
    if (imagePreprocessing.value || imageExtracting.value) return
    if (!files.length || !requireRecognitionService()) return

    // All sources use byte detection; neither MIME nor the suffix selects a decoder.
    {
      imageExtracting.value = true
      try {
        const extractedFiles = await extractImagesFromFileList(files, {
          onProgress: (current, total, message) => {
            extractionProgress.value = { current, total, message }
          }
        })

        if (extractedFiles.length === 0) {
          ElMessage.warning('未找到任何图片文件')
          return
        }

        if (await processFiles(extractedFiles, { fromExtraction: true })) ElMessage.success(`成功导入 ${extractedFiles.length} 张图片`)
      } catch (err) {
        ElMessage.error(err.message || '文件提取失败')
      } finally {
        imageExtracting.value = false
        extractionProgress.value = { current: 0, total: 0, message: '' }
      }
    }
  }

  async function onFolderInputChange(e) {
    const files = Array.from(e.target.files || [])
    e.target.value = ''
    if (imagePreprocessing.value || imageExtracting.value) return
    if (!files.length || !requireRecognitionService()) return

    imageExtracting.value = true
    try {
      const extractedFiles = await extractImagesFromFileList(files, {
        onProgress: (current, total, message) => {
          extractionProgress.value = { current, total, message }
        }
      })

      if (extractedFiles.length === 0) {
        ElMessage.warning('文件夹中未找到任何图片文件')
        return
      }

      if (await processFiles(extractedFiles, { fromExtraction: true })) ElMessage.success(`成功导入 ${extractedFiles.length} 张图片`)
    } catch (err) {
      ElMessage.error(err.message || '文件提取失败')
    } finally {
      imageExtracting.value = false
      extractionProgress.value = { current: 0, total: 0, message: '' }
    }
  }

  async function onDrop(e) {
    isDragover.value = false
    if (imagePreprocessing.value || imageExtracting.value) return
    if (!requireRecognitionService()) return

    imageExtracting.value = true
    try {
      const extractedFiles = await extractImagesFromDrop(e, {
        onProgress: (current, total, message) => {
          extractionProgress.value = { current, total, message }
        }
      })

      if (extractedFiles.length === 0) {
        ElMessage.warning('未找到任何图片文件，请拖入图片、文件夹或支持的压缩包')
        return
      }

      if (await processFiles(extractedFiles, { fromExtraction: true })) ElMessage.success(`成功导入 ${extractedFiles.length} 张图片`)
    } catch (err) {
      ElMessage.error(err.message || '文件提取失败')
    } finally {
      imageExtracting.value = false
      extractionProgress.value = { current: 0, total: 0, message: '' }
    }
  }

  function resetFiles() {
    fileList.value = []
    phoneCaptureSessionForFiles = ''
    recognitionAutoArmed.value = true
  }

  function openPhoneCapture() {
    if (imagePreprocessing.value || imageExtracting.value) {
      ElMessage.warning('图片正在处理中，请稍候')
      return
    }
    if (requireRecognitionService()) phoneCaptureVisible.value = true
  }

  async function importPhoneCaptures(files, { sessionId = '' } = {}) {
    if (imagePreprocessing.value || imageExtracting.value) {
      ElMessage.warning('图片正在处理中，请稍候')
      return false
    }
    if (!requireRecognitionService()) return false
    if (!(await processFiles(files))) return false
    phoneCaptureSessionForFiles = sessionId
    ElMessage.success(`已导入 ${files.length} 张手机照片`)
    return true
  }

  function openStudentSubmissions() {
    if (imagePreprocessing.value || imageExtracting.value) {
      ElMessage.warning('图片正在处理中，请稍候')
      return
    }
    if (requireRecognitionService()) studentSubmissionVisible.value = true
  }

  // Pages students uploaded through a class link arrive as
  // 学生提交/<姓名>_学号<学号>/第N页.jpg; the identity gate reads the folder name
  // and still checks it against the name written on the paper.
  async function importStudentSubmissions(files) {
    if (imagePreprocessing.value || imageExtracting.value) {
      ElMessage.warning('图片正在处理中，请稍候')
      return false
    }
    if (!requireRecognitionService()) return false
    if (!(await processFiles(files))) return false
    ElMessage.success(`已导入 ${files.length} 张学生上传的照片`)
    return true
  }

  // Phone photos stay on the server until a grading batch has stored its own
  // copies; then the capture session is deleted.
  function releasePhoneCaptureSession() {
    const sessionId = phoneCaptureSessionForFiles
    phoneCaptureSessionForFiles = ''
    if (!sessionId) return
    if (loadPhoneCaptureSession(auth.user?.id)?.session_id === sessionId) forgetPhoneCaptureSession(auth.user?.id)
    api.capture.close(sessionId, { suppressErrorToast: true }).catch(() => {})
  }

  return {
    phoneCaptureSessionForFiles, triggerFileUpload, triggerFolderUpload, triggerTemplateUpload,
    processFiles, onFileInputChange, onFolderInputChange, onDrop, resetFiles, openPhoneCapture,
    importPhoneCaptures, openStudentSubmissions, importStudentSubmissions,
    releasePhoneCaptureSession,
  }
}
