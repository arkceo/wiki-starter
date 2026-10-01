import { QuartzTransformerPlugin } from "../types"
import { visit, SKIP } from "unist-util-visit"
import fs from "fs"

// Turn inline code mentions of a source document (e.g. `supplier agreement.pdf`)
// into a link that opens the document itself. Filenames are resolved to their repo
// path by scanning raw/ at build time, so the markdown stays clean — just the
// filename — and the reader still gets one click to the original.
//
// The link is site-relative (/raw/...), served by whatever serves the site: the
// document route online, or the local viewer on a single machine. No machine path
// is baked into a built page.
const DOC_RE = /\.(pdf|xlsx?|csv|docx?|pptx?)$/i
const enc = (p: string): string => p.split("/").map(encodeURIComponent).join("/")

function buildIndex(): Map<string, string> {
  const m = new Map<string, string>()
  const walk = (dir: string) => {
    let entries: fs.Dirent[]
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const p = `${dir}/${e.name}`
      if (e.isDirectory()) {
        walk(p)
      } else if (DOC_RE.test(e.name) && !m.has(e.name)) {
        m.set(e.name, p)
      }
    }
  }
  walk("raw")
  return m
}

export const LinkSources: QuartzTransformerPlugin = () => {
  const index = buildIndex()
  return {
    name: "LinkSources",
    markdownPlugins() {
      return [
        () => (tree: any) => {
          if (index.size === 0) {
            return
          }
          visit(tree, "inlineCode", (node: any, i: any, parent: any) => {
            if (parent == null || i == null) {
              return
            }
            const repoPath = index.get(node.value)
            if (!repoPath) {
              return
            }
            // repoPath already starts with "raw/", which is exactly the route.
            parent.children[i] = {
              type: "link",
              url: `/${enc(repoPath)}`,
              title: "Open this source document",
              children: [node],
            }
            return [SKIP, i + 1]
          })
        },
      ]
    },
  }
}
