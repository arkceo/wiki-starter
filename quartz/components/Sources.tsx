import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
import { classNames } from "../util/lang"

// Render a page's `sources:` frontmatter as links to the actual documents.
//  - A file under raw/ links to /raw/... , which the site serves straight from the
//    source tree (the document route online, the local viewer on one machine). It
//    opens in the browser with no account needed.
//  - With `repoUrl` set, folders and packets under archive/ link to that repository,
//    and each served file also gets a small link to its repository copy.
//  - Without `repoUrl` (a wiki that lives on one machine), packets under archive/ are
//    served like raw/ files, and folders are listed as plain text.
// Deliberately no local filesystem path: a built page must not carry a machine path.
interface Options {
  repoUrl?: string
}

const enc = (p: string) => p.split("/").map(encodeURIComponent).join("/")

const css = `
.sources {
  margin-top: 2rem;
  padding-top: 1rem;
  border-top: 1px solid var(--lightgray);
  font-size: 0.9rem;
}
.sources h3 {
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
  color: var(--gray);
}
.sources ul {
  list-style: none;
  padding-left: 0;
  margin: 0;
}
.sources li {
  margin: 0.3rem 0;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.sources a {
  font-family: var(--codeFont);
  word-break: break-all;
}
.sources a.src-gh {
  font-family: var(--bodyFont);
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--secondary);
  border: 1px solid var(--lightgray);
  border-radius: 6px;
  padding: 0.05rem 0.4rem;
  text-decoration: none;
  white-space: nowrap;
}
.sources a.src-gh:hover {
  background: var(--lightgray);
}
`

export default ((opts?: Options) => {
  const repo = opts?.repoUrl?.replace(/\/+$/, "")

  const Sources: QuartzComponent = ({ fileData, displayClass }: QuartzComponentProps) => {
    const raw = (fileData.frontmatter as Record<string, unknown> | undefined)?.sources
    const list: string[] = Array.isArray(raw)
      ? (raw as unknown[]).map((s) => String(s).trim()).filter((s) => s.length > 0)
      : typeof raw === "string" && raw.trim().length > 0
        ? [raw.trim()]
        : []
    if (list.length === 0) {
      return null
    }
    return (
      <div class={classNames(displayClass, "sources")}>
        <h3>Sources</h3>
        <ul>
          {list.map((path) => {
            const isFolder = path.endsWith("/")
            const ghUrl = repo ? `${repo}/${isFolder ? "tree" : "blob"}/main/${enc(path)}` : null
            // Only a file under raw/ (and, on a single-machine wiki, a packet under
            // archive/) has something the document route can serve.
            const servable =
              !isFolder && (path.startsWith("raw/") || (!repo && path.startsWith("archive/")))
            const href = servable ? `/${enc(path)}` : ghUrl
            return (
              <li>
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {path}
                  </a>
                ) : (
                  <code>{path}</code>
                )}
                {servable && ghUrl && (
                  <a
                    class="src-gh"
                    href={ghUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="View this file on GitHub"
                  >
                    GitHub
                  </a>
                )}
              </li>
            )
          })}
        </ul>
      </div>
    )
  }

  Sources.css = css
  return Sources
}) satisfies QuartzComponentConstructor<Options>
