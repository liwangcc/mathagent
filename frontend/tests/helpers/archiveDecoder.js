import { readFileSync } from 'node:fs'
import { createExtractorFromData } from 'node-unrar-js'
import { boundArchiveMemory } from '../../src/utils/archiveMemory.js'
import { extractArchive } from '../../src/utils/archiveExtractionCore.js'

export async function decodeArchive(file, limits) {
  const binary = readFileSync(new URL('../../node_modules/node-unrar-js/dist/js/unrar.wasm', import.meta.url))
  return extractArchive(file, data => createExtractorFromData({ data, wasmBinary: boundArchiveMemory(binary) }), limits)
}

// Only adapts the browser Worker transport. Uses the real bounded WASM decoder.
export class NodeArchiveWorker {
  terminated = false
  postMessage({ file, limits }) {
    decodeArchive(file, limits).then(files => {
      if (!this.terminated) this.onmessage({ data: { files } })
    }, error => {
      if (!this.terminated) this.onmessage({ data: { error: { code: error.code, source: error.source, message: error.message } } })
    })
  }
  terminate() { this.terminated = true }
}
