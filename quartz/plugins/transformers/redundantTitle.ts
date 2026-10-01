import { QuartzTransformerPlugin } from "../types"
import { toString } from "mdast-util-to-string"

/**
 * Drop a leading body heading that only repeats the page title.
 *
 * Quartz already renders `title:` from the frontmatter as the page's `<h1>`. Most pages
 * in this wiki *also* open with `# Same Title`, because that is how the ingest pipeline
 * writes them — so the title is printed twice, one line apart, and the document has two
 * competing `<h1>`s. The audit counted 141 of 360 pages doing this.
 *
 * That is not only untidy. A screen reader announces the page heading twice, and the
 * document outline has two level-one headings where the standard expects one, so every
 * real section lands a level deeper than it reads.
 *
 * The fix is at build time rather than in the source files, because the ingest pipeline
 * will keep writing that heading and 141 hand-edits would be undone by the next run.
 *
 * Deliberately narrow. The heading is removed only when it is the very first node in the
 * document, is level one, and its text matches the frontmatter title once case and
 * punctuation are normalised away. A page whose opening heading genuinely differs from
 * its title keeps it, and an `<h1>` further down the page is never touched — that is a
 * content problem, and silently deleting content is not this plugin's job.
 */

// Case, surrounding whitespace, and the punctuation that drifts between a YAML title and
// a markdown heading (EXAMPLE CO. vs EXAMPLE CO) are all noise for this comparison.
const normalise = (s: string): string =>
  s
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[‘’“”]/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim()

export const RedundantTitle: QuartzTransformerPlugin = () => ({
  name: "RedundantTitle",
  markdownPlugins() {
    return [
      () => (tree: any, file: any) => {
        const title = file.data?.frontmatter?.title
        if (!title) return

        // The *first heading*, not the first node. Two earlier versions of this got the
        // position wrong and silently did nothing: index 0 is the parsed `yaml` frontmatter
        // block, and on 55 pages a confidentiality callout or lede sits above the heading.
        // What matters is that no heading precedes it, not that no content does.
        const at = tree.children?.findIndex((n: any) => n.type === "heading")
        if (at === undefined || at < 0) return

        const first = tree.children[at]
        if (first.depth !== 1) return

        const heading = normalise(toString(first))
        if (!heading || heading !== normalise(String(title))) return

        tree.children.splice(at, 1)
      },
    ]
  },
})
