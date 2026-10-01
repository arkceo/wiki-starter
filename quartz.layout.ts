import { PageLayout, SharedLayout } from "./quartz/cfg"
import * as Component from "./quartz/components"

const explorer = () =>
  Component.Explorer({
    filterFn: (node) => !["tags", "_review"].includes(node.slugSegment),
    sortFn: (a, b) => {
      const pin = "ingestion-register"
      if (a.slugSegment === pin) return -1
      if (b.slugSegment === pin) return 1
      if ((!a.isFolder && !b.isFolder) || (a.isFolder && b.isFolder)) {
        return a.displayName.localeCompare(b.displayName, undefined, {
          numeric: true,
          sensitivity: "base",
        })
      }
      return !a.isFolder && b.isFolder ? 1 : -1
    },
  })

// components shared across all pages
export const sharedPageComponents: SharedLayout = {
  head: Component.Head(),
  // "Upload", top right: opens the Upload & Logs page served by the local viewer.
  header: [Component.UploadButton()],
  // No repoUrl: this wiki has no remote. Source links open through the local viewer.
  afterBody: [Component.Sources()],
  footer: Component.Footer({ links: {} }),
}

// components for pages that display a single page (e.g. a single note)
export const defaultContentPageLayout: PageLayout = {
  beforeBody: [
    Component.ConditionalRender({
      component: Component.Breadcrumbs(),
      condition: (page) => page.fileData.slug !== "index",
    }),
    Component.ArticleTitle(),
    Component.ContentMeta(),
    Component.TagList(),
    // Canonical facts card. Renders only on pages that declare `facts:` in frontmatter.
    Component.Facts(),
  ],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Flex({
      components: [
        { Component: Component.Search(), grow: true },
        { Component: Component.Darkmode() },
        { Component: Component.ReaderMode() },
      ],
    }),
    explorer(),
  ],
  right: [
    Component.Graph(),
    Component.DesktopOnly(Component.TableOfContents()),
    Component.Backlinks(),
  ],
}

// components for pages that display lists of pages (e.g. tags or folders)
export const defaultListPageLayout: PageLayout = {
  beforeBody: [Component.Breadcrumbs(), Component.ArticleTitle(), Component.ContentMeta()],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Flex({
      components: [{ Component: Component.Search(), grow: true }, { Component: Component.Darkmode() }],
    }),
    explorer(),
  ],
  right: [],
}
