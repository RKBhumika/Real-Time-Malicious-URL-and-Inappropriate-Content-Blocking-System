import { promises as fs } from "fs"
import path from "path"

const ROOT = process.cwd()
const IGNORE_DIRS = new Set(["node_modules", ".git", ".next", ".vercel"])

function shouldIgnoreDir(dir) {
  const base = path.basename(dir)
  return IGNORE_DIRS.has(base)
}

async function* walk(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true })
  for (const e of entries) {
    const full = path.join(dir, e.name)
    if (e.isDirectory()) {
      if (shouldIgnoreDir(full)) continue
      yield* walk(full)
    } else {
      yield full
    }
  }
}

async function main() {
  const tsFiles = []
  for await (const f of walk(ROOT)) {
    if (f.endsWith(".ts") || f.endsWith(".tsx") || f.endsWith(".d.ts") || f.endsWith(".d.tsx")) {
      tsFiles.push(f)
    }
  }
  if (tsFiles.length === 0) {
    console.log("[v0] ✅ No TypeScript files found. Project is pure JavaScript.")
  } else {
    console.log("[v0] ❌ Found remaining TypeScript files:")
    for (const f of tsFiles) console.log(" -", f)
    process.exitCode = 1
  }
}

main().catch((e) => {
  console.error("[v0] Error verifying TS files:", e)
  process.exit(1)
})
