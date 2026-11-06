// Which frontend/src files a test file names other than as an import, i.e. reads
// as text (see no-source-text.test.js). Reads are reported relative to src/, and
// as * for a path assembled from a bare src directory segment, such as
// path.join(root, frontend, src, name), whose file is only known at run time.

const IMPORT_SPECIFIERS = [
  /\b(?:import|export)\b[^'"`;]*?\bfrom\s*(['"])[^'"\n]*\1/g,
  /\bimport\s*(['"])[^'"\n]*\1/g,
  /\bimport\s*\(\s*(['"`])[^'"`\n]*\1\s*\)/g,
  // The Vite alias of browser tests (alias @ to resolve of src) builds the app.
  /(['"])@\1\s*:\s*\w+\(\s*(['"])src\2\s*\)/g,
]
const STRING_LITERAL = /(['"`])((?:(?!\1)[^\\\n]|\\.)*)\1/g
// src/ then a .vue/.js/.ts path, or a path with an interpolation in it.
const SOURCE_PATH = /(?:^|[/.])src\/((?:[^'"`\n]*\.(?:vue|js|ts))|(?:[^'"`\n]*\$\{[^'"`\n]*))$/
// ../src, frontend/src/, ../src/views/ and the like: a path into src that names
// no file yet. Paths ending in another extension (.css, .json) are not code.
const SOURCE_DIRECTORY = /(?:^|[/.])src(?:\/[^'"`\n]*)?$/
const OTHER_FILE = /\.\w+$/
// A bare src segment counts as a later argument of a call joining a path; as a
// first argument it is usually something else, like an HTML attribute name.
const LATER_ARGUMENT = /,\s*$/

export function sourceTextReads(text) {
  let code = text
  for (const specifier of IMPORT_SPECIFIERS) code = code.replace(specifier, '')
  const reads = new Set()
  for (const match of code.matchAll(STRING_LITERAL)) {
    const value = match[2]
    const path = SOURCE_PATH.exec(value)
    if (path) reads.add(path[1])
    else if (SOURCE_DIRECTORY.test(value) && !OTHER_FILE.test(value) && (value !== 'src' || LATER_ARGUMENT.test(code.slice(Math.max(0, match.index - 16), match.index)))) {
      reads.add('*')
    }
  }
  return [...reads].sort()
}

// Relative module specifiers a file imports (static, re-export or dynamic).
const LOCAL_IMPORTS = [
  /\b(?:import|export)\b[^'"`;]*?\bfrom\s*(['"])(\.{1,2}\/[^'"\n]*)\1/g,
  /\bimport\s*(['"])(\.{1,2}\/[^'"\n]*)\1/g,
  /\bimport\s*\(\s*(['"`])(\.{1,2}\/[^'"`\n]*)\1\s*\)/g,
]

function localImports(text) {
  return LOCAL_IMPORTS.flatMap(pattern => [...text.matchAll(pattern)].map(match => match[2]))
}

// Reads per file of a set of test files ({ 'dir/name.js': text }, paths
// relative to tests/), counting what a file reads itself and what every test
// file it imports reads, transitively: a helper that reads source text for a
// test is the same reading. `exempt` files are imported but never read from.
export function sourceTextReadsOf(files, { exempt = [] } = {}) {
  const direct = Object.fromEntries(Object.entries(files).map(([name, text]) => [
    name, exempt.includes(name) ? [] : sourceTextReads(text),
  ]))
  const imports = Object.fromEntries(Object.entries(files).map(([name, text]) => {
    const base = new URL(name, 'file:///tests/')
    const targets = localImports(text)
      .map(specifier => new URL(specifier.split('?')[0], base).pathname)
      .filter(path => path.startsWith('/tests/'))
      .map(path => decodeURIComponent(path.slice('/tests/'.length)))
      .filter(target => Object.hasOwn(files, target))
    return [name, targets]
  }))
  const readsOf = (name) => {
    const seen = new Set([name])
    const pending = [name]
    while (pending.length) {
      for (const target of imports[pending.pop()]) {
        if (!seen.has(target)) {
          seen.add(target)
          pending.push(target)
        }
      }
    }
    return [...new Set([...seen].flatMap(file => direct[file]))].sort()
  }
  return Object.fromEntries(Object.keys(files).sort()
    .map(name => [name, readsOf(name)])
    .filter(([, reads]) => reads.length))
}
