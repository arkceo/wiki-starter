import { PageLayout, SharedLayout } from "./quartz/cfg"
import * as Component from "./quartz/components"
import menu from "./engine/menu.json"

// The left menu: the sections and categories of engine/menu.json above the folders. A page
// appears under each "section/category" its frontmatter `menu:` lists; titles only, since
// the hints are for whoever files the pages.
const explorer = () =>
  Component.Explorer({
    menu: menu.sections.map(({ key, title, categories }) => ({
      key,
      title,
      categories: categories.map(({ key, title }) => ({ key, title })),
    })),
    filterFn: (node) => !["tags", "_review"].includes(node.slugSegment),
    // A folder with no index page of its own shows a readable name rather than its folder
    // name. Rebuilt in the browser from its text, so it uses nothing from outside itself.
    mapFn: (node) => {
      const folder = node.slugSegment
      if (!node.isFolder || !folder || node.displayName !== folder) return
      const names: Record<string, string> = {
        sources: "Documents",
        companies: "Companies and people",
        "finance-legal": "Finance and legal",
        "how-it-runs": "How it runs",
        decisions: "Decisions",
        products: "Products",
        projects: "Projects",
        updates: "Updates",
      }
      const plain = folder.replace(/-/g, " ")
      node.displayName = Object.prototype.hasOwnProperty.call(names, folder)
        ? names[folder]
        : plain.charAt(0).toUpperCase() + plain.slice(1)
    },
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
      components: [
        { Component: Component.Search(), grow: true },
        { Component: Component.Darkmode() },
      ],
    }),
    explorer(),
  ],
  right: [],
}
