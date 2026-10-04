import test, { describe } from "node:test"
import assert from "node:assert"
import { FileTrieNode } from "./fileTrie"
import { menuPaths, readMenu, placeInMenu, MenuSection } from "./menu"
import { ContentDetails } from "../plugins/emitters/contentIndex"
import { FilePath, FullSlug } from "./path"

const MENU: MenuSection[] = [
  {
    key: "company-profile",
    title: "Company Profile",
    categories: [
      { key: "company", title: "The company itself" },
      { key: "customers", title: "Customers" },
      { key: "suppliers", title: "Suppliers" },
      { key: "auditor", title: "Auditor" },
    ],
  },
  { key: "marketing", title: "Marketing", categories: [{ key: "brand", title: "Brand and logo" }] },
  {
    key: "account",
    title: "Account",
    categories: [
      { key: "receivables", title: "Customer invoices and receivables" },
      { key: "tax", title: "Tax filings" },
    ],
  },
]

function page(slug: string, title: string, menu?: unknown): ContentDetails {
  return {
    slug: slug as FullSlug,
    filePath: `${slug}.md` as FilePath,
    title,
    links: [],
    tags: [],
    content: "",
    ...(menuPaths(menu) ? { menu: menuPaths(menu) } : {}),
  }
}

function trieOf(pages: ContentDetails[]) {
  return FileTrieNode.fromEntries(pages.map((p) => [p.slug, p] as [FullSlug, ContentDetails]))
}

const names = (nodes: FileTrieNode<ContentDetails>[]) => nodes.map((n) => n.displayName)

describe("menuPaths", () => {
  test("keeps keys, trimmed and once each", () => {
    assert.deepStrictEqual(
      menuPaths([" company-profile/suppliers ", "account/tax", "company-profile/suppliers"]),
      ["company-profile/suppliers", "account/tax"],
    )
  })

  test("a single string is a list of one", () => {
    assert.deepStrictEqual(menuPaths("account/tax"), ["account/tax"])
  })

  test("drops anything not shaped like a key", () => {
    assert.deepStrictEqual(
      menuPaths([
        "Company Profile/Suppliers",
        "company-profile › suppliers",
        "account",
        "a/b/c",
        42,
        null,
        { key: "x" },
        "account/tax",
      ]),
      ["account/tax"],
    )
  })

  test("nothing left means no field at all", () => {
    for (const value of [undefined, null, "", [], ["Nonsense"], 7, { a: 1 }, true]) {
      assert.strictEqual(menuPaths(value), undefined)
    }
  })
})

describe("readMenu", () => {
  test("reads the layout's menu back", () => {
    assert.deepStrictEqual(readMenu(JSON.parse(JSON.stringify(MENU))), MENU)
  })

  test("unreadable means no menu", () => {
    for (const value of [undefined, null, "", "[]", { key: "a" }, 42]) {
      assert.deepStrictEqual(readMenu(value), [])
    }
  })

  test("leaves out malformed and repeated entries", () => {
    const value = [
      { key: "sales", title: "Sales", categories: [{ key: "quotations", title: "Quotations" }] },
      { key: "sales", title: "Sales again", categories: [] },
      { key: "Bad Key", title: "Bad", categories: [] },
      { title: "No key" },
      {
        key: "account",
        title: "Account",
        categories: [{ key: "tax", title: "Tax filings" }, { key: "tax", title: "Twice" }, "x"],
      },
      { key: "finance", title: "Finance" },
    ]
    assert.deepStrictEqual(readMenu(value), [
      { key: "sales", title: "Sales", categories: [{ key: "quotations", title: "Quotations" }] },
      { key: "account", title: "Account", categories: [{ key: "tax", title: "Tax filings" }] },
      { key: "finance", title: "Finance", categories: [] },
    ])
  })
})

describe("placeInMenu", () => {
  const wiki = () =>
    trieOf([
      page("index", "Home"),
      page("overview", "Overview"),
      page("companies/index", "Companies"),
      page("companies/harbourline-foods", "Harbourline Foods", ["company-profile/company"]),
      page("companies/northwind-trading", "Northwind Trading", [
        "company-profile/suppliers",
        "company-profile/customers",
      ]),
      page("companies/kopi-lestari", "Kopi Lestari", "company-profile/suppliers"),
      page("finance-legal/index", "Finance and legal", ["account/tax"]),
      page("finance-legal/sst", "SST", ["account/tax"]),
      page("finance-legal/loans", "Loans", ["finance/loans"]),
      page("sources/invoice-10", "Invoice 10", ["account/receivables"]),
      page("sources/invoice-9", "Invoice 9", ["account/receivables"]),
      page("sources/menu-photo", "Menu photo"),
      page("decisions/index", "Decisions"),
    ])

  test("sections and categories in order, only those with pages", () => {
    const trie = wiki()
    const menu = placeInMenu(MENU, trie)
    assert.deepStrictEqual(
      menu.sections.map((s) => [s.title, s.categories.map((c) => c.title)]),
      [
        ["Company Profile", ["The company itself", "Customers", "Suppliers"]],
        ["Account", ["Customer invoices and receivables", "Tax filings"]],
      ],
    )
    assert.deepStrictEqual(
      menu.sections.map((s) => [s.path, s.categories.map((c) => c.path)]),
      [
        [
          "menu/company-profile",
          [
            "menu/company-profile/company",
            "menu/company-profile/customers",
            "menu/company-profile/suppliers",
          ],
        ],
        ["menu/account", ["menu/account/receivables", "menu/account/tax"]],
      ],
    )
  })

  test("a page under every category it names, sorted by title", () => {
    const menu = placeInMenu(MENU, wiki())
    const [profile, account] = menu.sections
    assert.deepStrictEqual(names(profile.categories[1].pages), ["Northwind Trading"])
    assert.deepStrictEqual(names(profile.categories[2].pages), [
      "Kopi Lestari",
      "Northwind Trading",
    ])
    assert.deepStrictEqual(names(account.categories[0].pages), ["Invoice 9", "Invoice 10"])
    assert.deepStrictEqual(menu.pathsOf.get("companies/northwind-trading"), [
      "company-profile/suppliers",
      "company-profile/customers",
    ])
  })

  test("placed pages leave the folder tree; emptied folders go, index page or not", () => {
    const trie = wiki()
    const menu = placeInMenu(MENU, trie)
    const top = trie.children.map((c) => c.slugSegment)
    assert.deepStrictEqual(top, ["overview", "finance-legal", "sources", "decisions"])
    // finance-legal keeps the page with an unknown category, and its own index page, which
    // is in the menu as well
    const financeLegal = trie.findNode(["finance-legal"])!
    assert.deepStrictEqual(names(financeLegal.children), ["Loans"])
    assert.deepStrictEqual(names(menu.sections[1].categories[1].pages), [
      "Finance and legal",
      "SST",
    ])
    assert.deepStrictEqual(names(trie.findNode(["sources"])!.children), ["Menu photo"])
    // an empty folder that was empty to begin with stays, as before
    assert.ok(trie.findNode(["decisions"]))
    assert.deepStrictEqual([...menu.removed].sort(), [
      "companies/harbourline-foods",
      "companies/kopi-lestari",
      "companies/northwind-trading",
      "finance-legal/sst",
      "sources/invoice-10",
      "sources/invoice-9",
    ])
  })

  test("nested folders emptied by the menu all go", () => {
    const trie = trieOf([
      page("a/b/c/deep", "Deep", ["account/tax"]),
      page("a/b/index", "B"),
      page("keep", "Keep"),
    ])
    placeInMenu(MENU, trie)
    assert.deepStrictEqual(
      trie.children.map((c) => c.slugSegment),
      ["keep"],
    )
  })

  test("no menu, or no page in it: the tree is untouched", () => {
    for (const sections of [[], [{ key: "x", title: "X", categories: [] }]]) {
      const trie = wiki()
      const before = trie.entries().map(([slug]) => slug)
      const menu = placeInMenu(sections, trie)
      assert.deepStrictEqual(menu.sections, [])
      assert.deepStrictEqual(
        trie.entries().map(([slug]) => slug),
        before,
      )
    }
  })

  test("thousands of pages in one pass", () => {
    const pages: ContentDetails[] = []
    const keys = ["company-profile/suppliers", "account/receivables", "account/tax"]
    for (let i = 0; i < 6000; i++) {
      const menu = i % 3 === 0 ? undefined : [keys[i % keys.length], keys[(i + 1) % keys.length]]
      pages.push(page(`sources/doc-${i}`, `Document ${i}`, menu))
    }
    const trie = trieOf(pages)
    const started = performance.now()
    const menu = placeInMenu(MENU, trie)
    const took = performance.now() - started
    assert.strictEqual(menu.removed.size, 4000)
    assert.strictEqual(trie.findNode(["sources"])!.children.length, 2000)
    const counts = menu.sections.flatMap((s) => s.categories.map((c) => c.pages.length))
    assert.strictEqual(
      counts.reduce((a, b) => a + b, 0),
      8000,
    )
    assert.strictEqual(menu.sections[1].categories[0].pages[0].displayName, "Document 1")
    assert.ok(took < 1000, `placing 6,000 pages took ${Math.round(took)} ms`)
  })
})
