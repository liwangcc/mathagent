/**
 * The order of student answer-sheet files: capture names, timestamps and sequence numbers, sorting flat
 * and folder captures into page order, and how a file's side and path are shown.
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { pairFilesByFolder, pairFilesBySide, parseSideFilename, sourceDirectory } from '@/utils/answerSheetPairing'
import { getUploadSourcePath } from '@/utils/uploadSource'

export function useStudentFileOrder(ctx) {
  const {
    studentPairingMode,
  } = ctx

  function captureFileName(file) {
    const raw = file?.raw || file
    return String(raw?.name || file?.name || '')
  }

  function captureFileBaseName(file) {
    return captureFileName(file)
      .replace(/\\/g, '/')
      .split('/')
      .pop()
      .replace(/\.ocr-ready-v1\.jpg$/i, '')
      .replace(/\.[^.]+$/, '')
  }

  function captureFileTimestamp(file) {
    const name = captureFileBaseName(file)
    const match = name.match(/(?:^|[^0-9])(20[0-9]{2})[-_.]?([0-1][0-9])[-_.]?([0-3][0-9])[T _-]?([0-2][0-9])[-_.]?([0-5][0-9])[-_.]?([0-5][0-9])(?:[-_.]([0-9]{1,6}))?(?:[^0-9]|$)/)
    if (!match) return null
    const [, year, month, day, hour, minute, second, fraction = ''] = match
    const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)))
    if (
      date.getUTCFullYear() !== Number(year)
      || date.getUTCMonth() !== Number(month) - 1
      || date.getUTCDate() !== Number(day)
      || date.getUTCHours() !== Number(hour)
      || date.getUTCMinutes() !== Number(minute)
      || date.getUTCSeconds() !== Number(second)
    ) return null
    return `${year}${month}${day}${hour}${minute}${second}${fraction.padEnd(6, '0')}`
  }

  function captureFileSequence(file) {
    const name = captureFileBaseName(file)
    const matches = [...name.matchAll(/[0-9]+/g)]
    if (!matches.length) return null
    return matches[matches.length - 1][0]
  }

  function compareNumericStrings(left, right) {
    const a = String(left).replace(/^0+(?=\d)/, '')
    const b = String(right).replace(/^0+(?=\d)/, '')
    if (a.length !== b.length) return a.length - b.length
    return a < b ? -1 : (a > b ? 1 : 0)
  }

  function sortByCaptureKey(files, getKey, compareKey) {
    const entries = files.map((file, index) => ({ file, index, key: getKey(file) }))
    if (entries.some((entry) => entry.key == null)) return null
    const keys = entries.map((entry) => String(entry.key))
    if (new Set(keys).size !== entries.length) return null
    return entries
      .sort((left, right) => compareKey(left.key, right.key) || left.index - right.index)
      .map((entry) => entry.file)
  }

  function studentFileSideLabel(file, index) {
    if (studentPairingMode.value === 'folder') return '按页型区分'
    if (studentPairingMode.value === 'side') {
      return parseSideFilename(captureFileName(file))?.side === 'back' ? '背面' : '正面'
    }
    return index % 2 ? '背面' : '正面'
  }

  // Import path (folder/archive member) for display and identity only.
  function captureSourcePath(file) {
    try {
      return getUploadSourcePath(file)
    } catch {
      return ''
    }
  }

  function studentFileDisplayPath(file) {
    return captureSourcePath(file) || captureFileName(file)
  }

  function sortCaptureFiles(files) {
    const byFolder = pairFilesByFolder(files, captureSourcePath)
    if (byFolder) {
      return {
        files: byFolder,
        source: 'folder_pairs',
        label: '文件夹配对',
        message: '系统已按文件夹排序。',
      }
    }
    const byName = pairFilesBySide(files, captureFileName)
    if (byName) {
      return {
        files: byName,
        source: 'filename_side_pairs',
        label: '文件名正反面',
        message: '系统已按文件名中的正面/反面标记排序。',
      }
    }
    if (files.some((file) => sourceDirectory(captureSourcePath(file)))) {
      const folders = new Map()
      for (const file of files) {
        const directory = sourceDirectory(captureSourcePath(file))
        folders.set(directory, [...(folders.get(directory) || []), file])
      }
      return {
        files: [...folders.keys()]
          .sort((left, right) => left.localeCompare(right, 'zh-CN', { numeric: true }))
          .flatMap((directory) => sortFlatCaptureFiles(folders.get(directory)).files),
        source: 'folder_order',
        label: '文件夹分组',
        message: '系统已按文件夹分组，文件夹内按文件序号或时间排序；请核对下面列表。',
      }
    }
    return sortFlatCaptureFiles(files)
  }

  function sortFlatCaptureFiles(files) {
    const byTimestamp = sortByCaptureKey(files, captureFileTimestamp, (left, right) => left < right ? -1 : (left > right ? 1 : 0))
    if (byTimestamp) {
      return {
        files: byTimestamp,
        source: 'filename_timestamp',
        label: '文件名时间戳',
        message: '系统已按文件名中的拍摄时间戳自动排序。',
      }
    }

    const bySequence = sortByCaptureKey(files, captureFileSequence, compareNumericStrings)
    if (bySequence) {
      return {
        files: bySequence,
        source: 'filename_sequence',
        label: '文件名序号',
        message: '系统已按文件名中的连续序号自动排序。',
      }
    }

    const byModifiedTime = sortByCaptureKey(
      files,
      (file) => {
        const raw = file?.raw || file
        const value = Number(raw?.lastModified)
        return Number.isFinite(value) && value > 0 ? String(value).padStart(16, '0') : null
      },
      (left, right) => left < right ? -1 : (left > right ? 1 : 0),
    )
    if (byModifiedTime) {
      return {
        files: byModifiedTime,
        source: 'file_modified_time',
        label: '文件时间',
        message: '系统已按文件修改时间自动排序；如文件被复制或批量导出，请核对下面列表。',
      }
    }

    return {
      files: [...files],
      source: 'input_order',
      label: '选择顺序',
      message: '未找到可靠的文件序号或时间戳，暂保留本次选择顺序；请检查下面列表。',
    }
  }

  return {
    captureFileName, captureFileBaseName, captureFileTimestamp, captureFileSequence,
    compareNumericStrings, sortByCaptureKey, studentFileSideLabel, captureSourcePath,
    studentFileDisplayPath, sortCaptureFiles, sortFlatCaptureFiles,
  }
}
