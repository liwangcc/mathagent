import { createExtractorFromData } from 'node-unrar-js'
import wasmUrl from 'node-unrar-js/esm/js/unrar.wasm?url'
import { boundArchiveMemory } from './archiveMemory.js'
import { extractArchive } from './archiveExtractionCore.js'
import { importError } from './fileImportPolicy.js'

self.onmessage = async ({ data: { file, limits } }) => {
  try {
    const createRarExtractor = async (data) => {
      try {
        const response = await fetch(wasmUrl)
        if (!response.ok) throw new Error(`WASM HTTP ${response.status}`)
        return await createExtractorFromData({ data, wasmBinary: boundArchiveMemory(await response.arrayBuffer()) })
      } catch (error) {
        throw importError('IMPORT_DECODER_UNAVAILABLE', file.name, `解包组件加载失败（${error.message}）`,
          '请刷新后重试；管理员需完整部署前端 Worker 和 WASM 静态资源。')
      }
    }
    const files = await extractArchive(file, createRarExtractor, limits)
    self.postMessage({ files })
  } catch (error) {
    self.postMessage({ error: { message: error.message, code: error.code, source: error.source, stage: 'file_import' } })
  }
}
