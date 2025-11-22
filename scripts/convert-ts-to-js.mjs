// - Converts .ts → .js and .tsx → .jsx
// - Removes type syntax, handles enums, preserves JSX
// - Updates explicit import specifier extensions (".ts"→".js", ".tsx"→".jsx")
// - Deletes original .ts/.tsx and any .d.ts files
// - Skips node_modules, .git, .next, .vercel, and scripts/ (to avoid self-editing)

import { promises as fs } from "fs"
import path from "path"
import ts from "typescript"

const ROOT = process.cwd()

const IGNORE_DIRS = new Set([
  "node_modules",
  ".git",
  ".next",
  ".vercel",
])

function isTSFile(file) {
  return file.endsWith(".ts") && !file.endsWith(".d.ts")
}
function isTSXFile(file) {
  return file.endsWith(".tsx")
}
function isDTSFile(file) {
  return file.endsWith(".d.ts") || file.endsWith(".d.tsx")
}
function shouldIgnoreDir(dir) {
  const base = path.basename(dir)
  // do not convert files in scripts directory to avoid changing the running script
  if (base === "scripts") return true
  return IGNORE_DIRS.has(base)
}

async function* walk(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true })
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      if (shouldIgnoreDir(fullPath)) continue
      yield* walk(fullPath)
    } else {
      yield fullPath
    }
  }
}

function transpileWithTS(source, filePath) {
  const isTsx = filePath.endsWith(".tsx")
  const result = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2020,
      module: ts.ModuleKind.ESNext,
      jsx: isTsx ? ts.JsxEmit.Preserve : ts.JsxEmit.None,
      esModuleInterop: true,
      allowJs: true,
      removeComments: false,
      // keep the code as JS ESM with JSX preserved for .jsx files
      declaration: false,
    },
    fileName: filePath,
    reportDiagnostics: true,
  })
  if (result.diagnostics?.length) {
    const formatted = ts.formatDiagnosticsWithColorAndContext(
      result.diagnostics,
      {
        getCanonicalFileName: (f) => f,
        getCurrentDirectory: () => ROOT,
        getNewLine: () => "\n",
      }
    )
    console.log("[v0] TypeScript diagnostics during transpile:\n", formatted)
  }
  return result.outputText
}

// Update explicit import/export specifiers if they included extensions.
// Most Next.js imports are extension-less; this safely patches the explicit ones.
function rewriteImportExtensions(js, filePath) {
  // Replace .tsx → .jsx and .ts → .js within string literals following from / import(
  // Handles: import x from './foo.tsx', export * from "./bar.ts"
  const patterns = [
    { find: /\.tsx(['"])/g, replace: ".jsx$1" },
    { find: /\.ts(['"])/g, replace: ".js$1" },
  ]
  let out = js
  for (const { find, replace } of patterns) {
    out = out.replace(find, replace)
  }
  return out
}

async function main() {
  const toConvert = []
  const toDelete = []

  for await (const fullPath of walk(ROOT)) {
    if (isDTSFile(fullPath)) {
      toDelete.push(fullPath)
      continue
    }
    if (isTSXFile(fullPath) || isTSFile(fullPath)) {
      toConvert.push(fullPath)
    }
  }

  console.log(`[v0] Found ${toConvert.length} TypeScript source file(s) to convert.`)
  console.log(`[v0] Found ${toDelete.length} declaration file(s) to delete.`)

  const summary = {
    converted: [],
    deleted: [],
    errors: [],
  }

  for (const srcPath of toConvert) {
    try {
      const src = await fs.readFile(srcPath, "utf8")
      let js = transpileWithTS(src, srcPath)
      js = rewriteImportExtensions(js, srcPath)

      const dir = path.dirname(srcPath)
      const base = path.basename(srcPath)
      const outName = base.endsWith(".tsx")
        ? base.replace(/\.tsx$/, ".jsx")
        : base.replace(/\.ts$/, ".js")
      const outPath = path.join(dir, outName)

      await fs.writeFile(outPath, js, "utf8")
      await fs.unlink(srcPath)

      summary.converted.push({ from: srcPath, to: outPath })
      console.log(`[v0] Converted: ${srcPath} -> ${outPath}`)
    } catch (err) {
      summary.errors.push({ file: srcPath, error: String(err?.stack || err) })
      console.log("[v0] ERROR converting:", srcPath, err)
    }
  }

  for (const dts of toDelete) {
    try {
      await fs.unlink(dts)
      summary.deleted.push(dts)
      console.log(`[v0] Deleted declaration: ${dts}`)
    } catch (err) {
      summary.errors.push({ file: dts, error: String(err?.stack || err) })
      console.log("[v0] ERROR deleting declaration:", dts, err)
    }
  }

  // Print final summary (compact)
  console.log("[v0] Conversion summary:")
  console.log("[v0]   Converted:", summary.converted.length)
  console.log("[v0]   Deleted declarations:", summary.deleted.length)
  console.log("[v0]   Errors:", summary.errors.length)
  if (summary.errors.length) {
    console.log("[v0] Errors detail:", JSON.stringify(summary.errors, null, 2))
  }
}

main().catch((e) => {
  console.error("[v0] Fatal error during TS→JS conversion:", e)
  process.exit(1)
})
