import katex from 'katex'

function decodeHtmlEntities(value) {
  let text = String(value ?? '')
  if (typeof document === 'undefined') {
    return text
      .replace(/&#x20;|&#32;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
  }
  const node = document.createElement('textarea')
  for (let index = 0; index < 3; index += 1) {
    node.innerHTML = text
    const decoded = node.value
    if (decoded === text) break
    text = decoded
  }
  return text
}

const brokenJsonLatexEscapes = [
  [`${String.fromCharCode(8)}egin`, '\\begin'],
  [`${String.fromCharCode(8)}eta`, '\\beta'],
  [`${String.fromCharCode(8)}inom`, '\\binom'],
  [`${String.fromCharCode(12)}rac`, '\\frac'],
  [`${String.fromCharCode(13)}ight`, '\\right'],
  [`${String.fromCharCode(13)}ho`, '\\rho'],
  [`${String.fromCharCode(9)}heta`, '\\theta'],
  [`${String.fromCharCode(9)}imes`, '\\times'],
  [`${String.fromCharCode(9)}ext`, '\\text'],
  [`${String.fromCharCode(9)}an`, '\\tan'],
  [`${String.fromCharCode(10)}eq`, '\\neq'],
  [`${String.fromCharCode(10)}otin`, '\\notin'],
]

/** Repair renderer-safe mathematical source shared by previews and records. */
export function normalizeLatexSource(value) {
  let text = decodeHtmlEntities(value)
    .replace(/[\u200B-\u200D\uFEFF]/g, '')
    .replace(/[＼﹨](?=[A-Za-z])/g, '\\')
  for (const [damaged, command] of brokenJsonLatexEscapes) {
    text = text.split(damaged).join(command)
  }
  return text.replace(/\\sqrt\s*\[\s*\]/g, '\\sqrt')
}

function isFlatTableMarker(line) {
  return /^\|\s*(?:(?::?-+:?)\s*)?\|$/.test(line.trim())
}

function inferFlatTableColumns(count) {
  for (const preferred of [4, 3, 2, 5, 6, 7, 8]) {
    if (count >= preferred && count % preferred === 0) return preferred
  }
  for (let candidate = Math.min(12, count); candidate >= 2; candidate -= 1) {
    if (count % candidate === 0) return candidate
  }
  return Math.min(4, count)
}

function recoverFlatTables(text) {
  const lines = text.split(/\r?\n/)
  const result = []
  let index = 0
  while (index < lines.length) {
    if (!isFlatTableMarker(lines[index])) {
      result.push(lines[index])
      index += 1
      continue
    }
    const start = index
    const values = []
    let markerCount = 0
    while (index < lines.length && isFlatTableMarker(lines[index])) {
      markerCount += 1
      index += 1
      while (index < lines.length && !lines[index].trim()) index += 1
      if (index >= lines.length || isFlatTableMarker(lines[index])) continue
      values.push(lines[index].trim())
      index += 1
      while (index < lines.length && !lines[index].trim()) index += 1
      if (index >= lines.length || !isFlatTableMarker(lines[index])) break
    }
    const columns = inferFlatTableColumns(values.length)
    if (markerCount >= 4 && values.length >= 4 && markerCount >= values.length && columns >= 2 && values.length % columns === 0) {
      const rows = []
      for (let offset = 0; offset < values.length; offset += columns) {
        rows.push(values.slice(offset, offset + columns).join(' | '))
      }
      result.push(rows.join('\n'))
    } else {
      result.push(lines[start])
      index = start + 1
    }
  }
  return result.join('\n')
}

function recoverDamagedLatexTables(text) {
  const marker = /\(\s*\|[clr](?:\|[clr])+\|\s*\)/gi
  const matches = [...text.matchAll(marker)]
  if (!matches.length) return text
  let result = text.slice(0, matches[0].index)
  for (let index = 0; index < matches.length; index += 1) {
    const current = matches[index]
    const bodyStart = current.index + current[0].length
    const nextStart = matches[index + 1]?.index ?? text.length
    const body = text.slice(bodyStart, nextStart)
    const normalized = body
      .replace(/\\{2,}/g, '\n')
      .replace(/(^|[^A-Za-z])\\?hline\b/gi, '$1\n')
    const rows = normalized.split(/\r?\n/)
      .map(line => line.trim())
      .filter(line => line.includes('&'))
      .map(line => line.split('&').map(cell => cell.trim()).join(' | '))
    if (rows.length && result && !result.endsWith('\n')) result += '\n'
    result += rows.length ? rows.join('\n') : text.slice(current.index, nextStart)
    if (index + 1 === matches.length) {
      const hlines = [...body.matchAll(/(?<![A-Za-z])\\?hline\b/gi)]
      if (hlines.length) {
        const consumed = bodyStart + hlines[hlines.length - 1].index + hlines[hlines.length - 1][0].length
        result += text.slice(consumed)
      }
    }
  }
  return result
}

function normalizeMarkdownTables(text) {
  const lines = text.split(/\r?\n/)
  const result = []
  let index = 0
  while (index < lines.length) {
    const current = lines[index].trim()
    const next = lines[index + 1]?.trim() || ''
    const isRow = current.split('|').length >= 3
    const isSeparator = /^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?$/.test(next)
    if (!isRow || !isSeparator) {
      result.push(lines[index])
      index += 1
      continue
    }
    const pushRow = (line) => {
      const cells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|')
      result.push(cells.map(cell => cell.trim()).join(' | '))
    }
    pushRow(current)
    index += 2
    while (index < lines.length) {
      const row = lines[index].trim()
      if (!row || row.split('|').length < 3 || /^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?$/.test(row)) break
      pushRow(row)
      index += 1
    }
  }
  return result.join('\n')
}

/** Remove source-format debris from generated exercise preview text. */
export function cleanExerciseText(value) {
  let text = normalizeLatexSource(value)
    .replace(/<br\s*\/?>(?=\s*)/gi, '\n')
    .replace(/<\/?(?:p|div|span|strong|em|b|i)\b[^>]*>/gi, '')
    .replace(/\*\*/g, '')
    .replace(/^\s*#{1,6}\s*/gm, '')
  text = recoverFlatTables(text)
  text = recoverDamagedLatexTables(text)
  text = normalizeMarkdownTables(text)
  text = text
    .replace(/\(\s*\|[clr](?:\|[clr])+\|\s*\)/gi, '')
    .replace(/(^|[^A-Za-z])\\?hline\b/gi, '$1')
    .replace(/^\s*\|\s*(?:[-: ]*)\|\s*$/gm, '')
    .replace(/\n{3,}/g, '\n\n')
  return text.trim()
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

/**
 * 将文本中的LaTeX公式渲染为HTML
 * 支持：行内公式 $...$ 和独立公式 $$...$$
 * 安全：公式以外的纯文本一律 HTML 转义，防止存储型 XSS（如 <img onerror=...>）；
 *       KaTeX trust=false，禁用 \href{javascript:} 等危险命令。
 * 语法错误时降级为转义后的纯文本显示。
 */
export function renderLatex(text) {
  if (!text || typeof text !== 'string') return text ? escapeHtml(text) : ''

  const normalizedText = normalizeLatexSource(text)

  // 统一匹配 $$...$$ / $...$，并兼容 OCR 常见的 \[...\] / \(...\)。
  const formulaRe = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)/g
  let result = ''
  let lastIndex = 0
  let m
  while ((m = formulaRe.exec(normalizedText)) !== null) {
    // 公式前的纯文本 → 转义
    result += escapeHtml(normalizedText.slice(lastIndex, m.index))
    const isDisplay = m[1] !== undefined || m[3] !== undefined
    const formula = (m[1] ?? m[2] ?? m[3] ?? m[4]).trim()
    try {
      result += katex.renderToString(formula, {
        displayMode: isDisplay,
        throwOnError: false,
        trust: false,
      })
    } catch (e) {
      console.warn('KaTeX formula error:', e)
      result += `<span class="katex-error">${escapeHtml(m[0])}</span>`
    }
    lastIndex = formulaRe.lastIndex
  }
  // 尾部剩余纯文本 → 转义
  result += escapeHtml(normalizedText.slice(lastIndex))
  return result
}

/**
 * 按行渲染LaTeX（供AnswerDisplay.vue使用）
 * 返回HTML字符串数组，每行一个元素
 */
export function renderLatexLines(text) {
  if (!text || typeof text !== 'string') return [text || '']
  const lines = text.split(/\r?\n/)
  return lines.map(line => renderLatex(line))
}

/** Render an OCR answer, including bare formula source such as `4\\sqrt{5}`. */
export function renderAnswerLatex(text) {
  if (!text || typeof text !== 'string') return text ? escapeHtml(text) : ''
  return text.split(/\r?\n/).map((line) => {
    const trimmed = line.trim()
    const isDelimited = /\$\$|\$[^$\n]+\$|\\\[|\\\(|\\\]|\\\)/.test(trimmed)
    const isBareFormula = trimmed.length > 0
      && /^[0-9A-Za-z\\{}[\]()+\-*/=.,\s_^]+$/.test(trimmed)
      && (/\\[A-Za-z]+/.test(trimmed) || /[_^]/.test(trimmed))
    return renderLatex(isDelimited || !isBareFormula ? line : `$${line}$`)
  }).join('<br>')
}
