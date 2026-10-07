import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"
import site from "./wiki.config.json"

/**
 * Quartz configuration for a wiki that lives on one Mac.
 *
 * The site is built into public/ by the background runner and served only to this
 * machine by scripts/wiki_server.py. Nothing here reaches out to a third party while
 * you read: no analytics, system fonts instead of a font CDN, no maths CDN.
 *
 * Your settings (title, company, port) live in wiki.config.json, which engine updates
 * never overwrite. This file is an engine file.
 */

/**
 * The site's colours. "engine" is the wiki's own; "oeru" matches oeru.com, for wikis oeru
 * keeps in the cloud (WIKI_THEME=oeru, scripts/wiki_cloud.py). A Mac wiki never sets it.
 */
const THEMES = {
  engine: {
    typography: {
      header: "Avenir Next",
      body: "Helvetica Neue",
      code: "Menlo",
    },
    colors: {
      lightMode: {
        light: "#faf8f8",
        lightgray: "#e5e5e5",
        gray: "#b8b8b8",
        darkgray: "#4e4e4e",
        dark: "#2b2b2b",
        secondary: "#284b63",
        tertiary: "#84a59d",
        highlight: "rgba(143, 159, 169, 0.15)",
        textHighlight: "#fff23688",
      },
      darkMode: {
        light: "#161618",
        lightgray: "#393639",
        gray: "#646464",
        darkgray: "#d4d4d4",
        dark: "#ebebec",
        secondary: "#7b97aa",
        tertiary: "#84a59d",
        highlight: "rgba(143, 159, 169, 0.15)",
        textHighlight: "#b3aa0288",
      },
    },
  },
  oeru: {
    typography: {
      header: "Georgia",
      body: "Helvetica Neue",
      code: "Menlo",
    },
    colors: {
      lightMode: {
        light: "#F3F2ED",
        lightgray: "#E2DFD6",
        gray: "#C5C2B8",
        darkgray: "#5A5449",
        dark: "#1F1D1A",
        secondary: "#1C4970",
        tertiary: "#C39143",
        highlight: "rgba(28, 73, 112, 0.08)",
        textHighlight: "#DDE4EC",
      },
      darkMode: {
        light: "#171614",
        lightgray: "#2E2B27",
        gray: "#6B655C",
        darkgray: "#D9D5CC",
        dark: "#F3F2ED",
        secondary: "#8FB3D6",
        tertiary: "#C39143",
        highlight: "rgba(143, 179, 214, 0.12)",
        textHighlight: "#1C497088",
      },
    },
  },
} as const

const config: QuartzConfig = {
  configuration: {
    pageTitle: site.title || "Wiki",
    pageTitleSuffix: "",
    enableSPA: true,
    enablePopovers: true,
    analytics: null,
    locale: "en-US",
    baseUrl: `127.0.0.1:${site.port || 8765}`,
    ignorePatterns: ["private", "templates", ".obsidian"],
    defaultDateType: "modified",
    theme: {
      fontOrigin: "local",
      cdnCaching: false,
      ...THEMES[(site as { theme?: string }).theme === "oeru" ? "oeru" : "engine"],
    },
  },
  plugins: {
    transformers: [
      Plugin.FrontMatter(),
      Plugin.LinkSources(),
      // After FrontMatter, so the title it compares against is populated.
      Plugin.RedundantTitle(),
      Plugin.CreatedModifiedDate({
        priority: ["frontmatter", "git", "filesystem"],
      }),
      Plugin.SyntaxHighlighting({
        theme: {
          light: "github-light",
          dark: "github-dark",
        },
        keepBackground: false,
      }),
      Plugin.ObsidianFlavoredMarkdown({ enableInHtmlEmbed: false }),
      Plugin.GitHubFlavoredMarkdown(),
      Plugin.TableOfContents(),
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest" }),
      Plugin.Description(),
    ],
    filters: [Plugin.RemoveDrafts()],
    emitters: [
      Plugin.AliasRedirects(),
      Plugin.ComponentResources(),
      Plugin.ContentPage(),
      Plugin.FolderPage(),
      Plugin.TagPage(),
      Plugin.ContentIndex({
        enableSiteMap: false,
        enableRSS: false,
      }),
      Plugin.Assets(),
      Plugin.Static(),
      Plugin.Favicon(),
      Plugin.NotFoundPage(),
    ],
  },
}

export default config
