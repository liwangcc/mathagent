import { readFileSync, readdirSync, statSync } from "node:fs"
import { join } from "node:path"
import { fileURLToPath } from "node:url"

const root = fileURLToPath(new URL("../", import.meta.url))

function collect(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    if (statSync(path).isDirectory()) return collect(path)
    return /\.(tsx?|css)$/.test(name) ? [path] : []
  })
}

export const read = (relative) => readFileSync(join(root, relative), "utf8")

/** Every TS/TSX/CSS source under src/, concatenated, for page-wide copy assertions. */
export const allSource = collect(join(root, "src"))
  .map((path) => readFileSync(path, "utf8"))
  .join("\n")
