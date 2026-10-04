import type { ContentDetails } from "../plugins/emitters/contentIndex"
import type { FileTrieNode } from "./fileTrie"

/**
 * The wiki's menu: sections in a fixed order, each with its categories, above the folder
 * tree in the Explorer.
 *
 * Pages stay where they are on disk. A page joins the menu through the `menu:` field of
 * its frontmatter, a list of "section/category" keys (`["company-profile/suppliers"]`),
 * and appears under every one of them. The sections and categories themselves come from
 * the layout (the wiki's engine/menu.json), so a key the menu does not know, or a field
 * that is not a list of keys, simply leaves the page in its folder.
 */

export interface MenuCategory {
  key: string
  title: string
}

export interface MenuSection {
  key: string
  title: string
  categories: MenuCategory[]
}

export interface MenuBranch {
  // where the branch's open or closed state is saved, next to the folders' own
  path: string
  title: string
}

export interface MenuCategoryBranch extends MenuBranch {
  key: string
  pages: FileTrieNode<ContentDetails>[]
}

export interface MenuSectionBranch extends MenuBranch {
  key: string
  categories: MenuCategoryBranch[]
}

export interface PlacedMenu {
  // populated sections only, in order, each with its populated categories in order
  sections: MenuSectionBranch[]
  // every menu path each placed page sits under, by slug
  pathsOf: Map<string, string[]>
  // the pages taken out of the folder tree
  removed: Set<string>
}

const KEY = /^[a-z0-9-]+$/
const MENU_PATH = /^[a-z0-9-]+\/[a-z0-9-]+$/

/**
 * The `menu:` field of a page's frontmatter as the content index keeps it: a list of
 * "section/category" keys, trimmed and without repeats. A single string counts as a list
 * of one; anything else, and any entry that is not shaped like a key, is dropped.
 * Undefined when nothing is left, so the index carries no empty field.
 */
export function menuPaths(value: unknown): string[] | undefined {
  const raw = Array.isArray(value) ? value : typeof value === "string" ? [value] : []
  const out: string[] = []
  for (const entry of raw) {
    if (typeof entry !== "string") continue
    const path = entry.trim()
    if (MENU_PATH.test(path) && !out.includes(path)) out.push(path)
  }
  return out.length > 0 ? out : undefined
}

/**
 * The menu as the Explorer reads it back from the shared script. A section or category
 * without a proper key and title is left out, and anything unreadable means no menu at
 * all: the Explorer then shows the folder tree alone, as it always did.
 */
export function readMenu(value: unknown): MenuSection[] {
  if (!Array.isArray(value)) return []
  const named = (v: unknown): v is { key: string; title: string } =>
    typeof v === "object" &&
    v !== null &&
    typeof (v as { key?: unknown }).key === "string" &&
    KEY.test((v as { key: string }).key) &&
    typeof (v as { title?: unknown }).title === "string"
  const sections: MenuSection[] = []
  const seen = new Set<string>()
  for (const s of value) {
    if (!named(s) || seen.has(s.key)) continue
    seen.add(s.key)
    const raw = (s as { categories?: unknown }).categories
    const keys = new Set<string>()
    const categories: MenuCategory[] = []
    for (const c of Array.isArray(raw) ? raw : []) {
      if (!named(c) || keys.has(c.key)) continue
      keys.add(c.key)
      categories.push({ key: c.key, title: c.title })
    }
    sections.push({ key: s.key, title: s.title, categories })
  }
  return sections
}

const byTitle = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" })

/**
 * Places the pages of a (filtered and mapped) file trie into the menu.
 *
 * Every page whose `menu` names a known category goes under that category, once per
 * category, sorted by title. Those pages leave the folder tree, and a folder that loses
 * all its children that way goes too, even when it has an index page (a folder that was
 * empty to begin with stays, as before). One pass over the trie, so thousands of pages
 * cost next to nothing.
 */
export function placeInMenu(
  sections: MenuSection[],
  trie: FileTrieNode<ContentDetails>,
): PlacedMenu {
  const slots = new Map<string, FileTrieNode<ContentDetails>[]>()
  for (const section of sections) {
    for (const category of section.categories) slots.set(`${section.key}/${category.key}`, [])
  }
  const pathsOf = new Map<string, string[]>()
  const removed = new Set<string>()
  // No menu: the folder tree is left exactly as it was.
  if (slots.size === 0) return { sections: [], pathsOf, removed }

  const place = (node: FileTrieNode<ContentDetails>): boolean => {
    const data = node.data
    if (!data || !Array.isArray(data.menu)) return false
    const known: string[] = []
    for (const path of data.menu) {
      if (slots.has(path) && !known.includes(path)) known.push(path)
    }
    if (known.length === 0) return false
    for (const path of known) slots.get(path)!.push(node)
    pathsOf.set(data.slug, known)
    return true
  }

  // Returns whether the folder should stay in the tree.
  const visit = (folder: FileTrieNode<ContentDetails>): boolean => {
    // A folder's own index page is the folder itself: it can be in the menu too, but the
    // folder stays for whatever else it holds.
    place(folder)
    const before = folder.children.length
    folder.children = folder.children.filter((child) => {
      if (child.isFolder) return visit(child)
      if (!place(child)) return true
      removed.add(child.data!.slug)
      return false
    })
    return before === 0 || folder.children.length > 0
  }
  visit(trie)

  const placed: MenuSectionBranch[] = []
  for (const section of sections) {
    const categories: MenuCategoryBranch[] = []
    for (const category of section.categories) {
      const pages = slots.get(`${section.key}/${category.key}`)!
      if (pages.length === 0) continue
      pages.sort(
        (a, b) =>
          byTitle.compare(a.displayName, b.displayName) ||
          (a.data!.slug < b.data!.slug ? -1 : a.data!.slug > b.data!.slug ? 1 : 0),
      )
      categories.push({
        key: category.key,
        title: category.title,
        path: `menu/${section.key}/${category.key}`,
        pages,
      })
    }
    if (categories.length > 0) {
      placed.push({
        key: section.key,
        title: section.title,
        path: `menu/${section.key}`,
        categories,
      })
    }
  }
  return { sections: placed, pathsOf, removed }
}
