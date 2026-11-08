/**
 * Two small render-function components the page's template uses: FilePicker (choosing files, a folder or
 * an archive for one stage, keeping each image's relative path) and BboxFields (one region's bounding box).
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { defineComponent, h, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { compressImageFiles, validateUploadList } from '@/utils/uploadValidation'
import { extractImagesFromDrop, extractImagesFromFileList } from '@/utils/fileExtraction'

export function useAnswerSheetPickers(ctx) {
  const {
    fileSelectionProcessingCount, sortCaptureFiles,
  } = ctx

  const FilePicker = defineComponent({
    props: {
      modelValue: Array,
      title: String,
      note: String,
      disabled: Boolean,
      autoOrder: Boolean,
      // Folder, drag-and-drop and ZIP/RAR import through the shared extractor,
      // which keeps each image's relative path for pairing and identity.
      importArchives: Boolean,
      maxFiles: { type: Number, default: 200 },
    },
    emits: ['update:modelValue', 'order-detected'],
    setup(props, { emit }) {
      const selecting = ref(false)
      const dragover = ref(false)
      async function selectFiles(event) {
        const selected = [...(event.target.files || [])]
        event.target.value = ''
        if (selecting.value || props.disabled || !selected.length) return
        await acceptFiles(selected)
      }
      async function importFiles(load) {
        if (selecting.value || props.disabled) return
        selecting.value = true
        fileSelectionProcessingCount.value += 1
        let extracted
        try {
          extracted = await load()
        } catch (error) {
          ElMessage.error(error?.message || '文件导入失败')
          return
        } finally {
          fileSelectionProcessingCount.value = Math.max(0, fileSelectionProcessingCount.value - 1)
          selecting.value = false
        }
        if (!extracted.length) {
          ElMessage.warning('未找到任何图片文件')
          return
        }
        await acceptFiles(extracted)
      }
      function importInput(event) {
        const selected = [...(event.target.files || [])]
        event.target.value = ''
        if (selected.length) importFiles(() => extractImagesFromFileList(selected, { maxFiles: props.maxFiles }))
      }
      function importDrop(event) {
        event.preventDefault()
        dragover.value = false
        importFiles(() => extractImagesFromDrop(event, { maxFiles: props.maxFiles }))
      }
      async function acceptFiles(selected) {
        if (selected.length > props.maxFiles) {
          ElMessage.warning(`单次最多选择 ${props.maxFiles} 个文件`)
        }
        const supported = selected.slice(0, props.maxFiles).filter((file) => (
          ['image/jpeg', 'image/png'].includes(String(file?.type || '').toLowerCase())
        ))
        if (supported.length < Math.min(selected.length, props.maxFiles)) {
          ElMessage.warning('仅支持 JPG、JPEG 和 PNG 图片')
        }
        const result = validateUploadList(supported, { maxFiles: props.maxFiles })
        if (result.errors.length) ElMessage.warning(result.errors[0])
        if (!result.files.length) {
          emit('update:modelValue', [])
          return
        }

        const ordered = props.autoOrder
          ? sortCaptureFiles(result.files)
          : { files: result.files, source: 'input_order', label: '选择顺序', message: '' }
        if (props.autoOrder) {
          result.files = ordered.files
          emit('order-detected', ordered)
        }

        selecting.value = true
        fileSelectionProcessingCount.value += 1
        let failedCount = 0
        try {
          const processed = await compressImageFiles(result.files, 1920, 0.88, {
            onFileError: () => { failedCount += 1 },
          })
          emit('update:modelValue', processed)
          if (failedCount) {
            ElMessage.warning(`${failedCount} 张图片预处理失败，已保留原图；建议重新选择后再试`)
          }
        } catch {
          ElMessage.error('图片预处理未完成，请重新选择')
        } finally {
          fileSelectionProcessingCount.value = Math.max(0, fileSelectionProcessingCount.value - 1)
          selecting.value = false
        }
      }
      return () => props.importArchives ? h('div', {
        class: ['file-picker', 'file-picker-import', (props.disabled || selecting.value) && 'disabled', selecting.value && 'processing', props.modelValue?.length && 'has-files', dragover.value && 'is-dragover'],
        'aria-disabled': (props.disabled || selecting.value) ? 'true' : undefined,
        'aria-busy': selecting.value ? 'true' : undefined,
        onDragover: (event) => { event.preventDefault(); dragover.value = !props.disabled && !selecting.value },
        onDragleave: () => { dragover.value = false },
        onDrop: importDrop,
      }, [
        h(UploadFilled, { class: 'file-picker-icon', 'aria-hidden': 'true' }),
        h('strong', selecting.value ? '正在导入并处理图片' : (props.modelValue?.length ? `已选择 ${props.modelValue.length} 个文件` : props.title)),
        h('span', selecting.value ? '完成后即可上传' : props.note),
        h('div', { class: 'file-picker-actions' }, [
          h('label', { class: 'file-picker-button' }, ['选择文件', h('input', { type: 'file', accept: '.jpg,.jpeg,.png,.zip,.rar', multiple: true, disabled: props.disabled || selecting.value, onChange: importInput })]),
          h('label', { class: 'file-picker-button' }, ['选择文件夹', h('input', { type: 'file', webkitdirectory: true, disabled: props.disabled || selecting.value, onChange: importInput })]),
        ]),
      ]) : h('label', {
        class: ['file-picker', (props.disabled || selecting.value) && 'disabled', selecting.value && 'processing', props.modelValue?.length && 'has-files'],
        'aria-disabled': (props.disabled || selecting.value) ? 'true' : undefined,
        'aria-busy': selecting.value ? 'true' : undefined,
      }, [
        h('input', { type: 'file', accept: 'image/jpeg,image/png', multiple: true, disabled: props.disabled || selecting.value, onChange: selectFiles }),
        h(UploadFilled, { class: 'file-picker-icon', 'aria-hidden': 'true' }),
        h('strong', selecting.value ? '正在逐张处理图片' : (props.modelValue?.length ? `已选择 ${props.modelValue.length} 个文件` : props.title)),
        h('span', selecting.value ? '完成后即可上传' : props.note),
      ])
    },
  })

  const BboxFields = defineComponent({
    props: { modelValue: Array, disabled: Boolean }, emits: ['update:modelValue'],
    setup(props, { emit }) {
      const labels = ['左', '上', '右', '下']
      return () => h('div', { class: 'bbox-fields' }, labels.map((label, index) => h('label', [
        h('span', label), h('input', { type: 'number', min: 0, max: 1000, value: props.modelValue?.[index], disabled: props.disabled, onInput: (event) => { const next = [...props.modelValue]; next[index] = Number(event.target.value); emit('update:modelValue', next) } }),
      ])))
    },
  })

  return {
    FilePicker, BboxFields,
  }
}
