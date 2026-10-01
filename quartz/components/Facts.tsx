import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
import style from "./styles/facts.scss"
import { styleText } from "util"

// @ts-ignore
import script from "./scripts/facts.inline"

/**
 * Facts — the canonical fact card ("Info") on company and people pages.
 *
 * Renders the recyclable facts a page declares in frontmatter: the things that get
 * retyped into contracts, forms and decks. Every value copies on tap.
 *
 * Frontmatter is the record and the card is a view of it. Nothing here infers a fact
 * from prose: a page either declares its facts or the card does not render. Guessing a
 * registration number from a sentence is exactly the failure this is meant to remove.
 *
 *   facts:
 *     registered_name: EXAMPLE HOLDINGS SDN BHD
 *     registration_no: 000000-X
 *     tin_no: C00000000000
 *
 * Unknown keys are allowed and render with a humanised label, so the schema can grow in
 * the wiki without a code change.
 */

/** Canonical order. Keys not listed here render after these, in declaration order. */
const FIELD_ORDER = [
  "registered_name",
  "former_names",
  "registration_no",
  "ssm_no",
  "incorporation_date",
  "registered_address",
  "business_address",
  "tin_no",
  "sst_no",
  "epf_no",
  "socso_no",
  "eis_ref",
  "licence_no",
  "bank_name",
  "bank_account_no",
  "director_id",
  // people
  "full_name",
  "id_no",
  "passport_no",
  "tax_no",
  "email",
  "phone",
]

const LABELS: Record<string, string> = {
  registered_name: "Registered name",
  former_names: "Former name(s)",
  registration_no: "Registration no.",
  ssm_no: "New SSM no.",
  incorporation_date: "Incorporation date",
  registered_address: "Registered address",
  business_address: "Business address",
  tin_no: "TIN no.",
  sst_no: "SST no.",
  epf_no: "EPF employer no.",
  socso_no: "SOCSO employer no.",
  eis_ref: "EIS reference",
  licence_no: "Licence no.",
  bank_name: "Bank — name & branch",
  bank_account_no: "Bank account no.",
  director_id: "Director / individual ID",
  full_name: "Full name",
  id_no: "Identity no.",
  passport_no: "Passport no.",
  tax_no: "Tax no.",
  email: "Email",
  phone: "Phone",
}

function humanise(key: string): string {
  return (
    LABELS[key] ??
    key
      .replace(/_/g, " ")
      .replace(/\bno\b/i, "no.")
      .replace(/^./, (c) => c.toUpperCase())
  )
}

/** Frontmatter values arrive as strings, numbers, dates or lists. Flatten to one string. */
function toText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  if (Array.isArray(value)) {
    const parts = value.map(toText).filter((v): v is string => !!v)
    return parts.length ? parts.join(", ") : null
  }
  if (value instanceof Date) return value.toISOString().slice(0, 10)
  const s = String(value).trim()
  return s.length ? s : null
}

/**
 * Pages that look like *company* pages but declare nothing are listed once per build, so
 * the gap is visible rather than silently empty. Flag, never guess.
 *
 * The test is the title, not the folder: `companies/` also holds people, and warning on
 * every person page would turn a useful signal into noise everyone learns to ignore.
 */
const COMPANY_SUFFIX =
  /\b(sdn\.?\s*bhd|berhad|pte\.?\s*ltd|ltd\.?|limited|inc\.?|llc|llp|plc|gmbh|b\.?v\.?)\b/i
const flagged = new Set<string>()

export default (() => {
  const Facts: QuartzComponent = ({ fileData }: QuartzComponentProps) => {
    const raw = fileData.frontmatter?.facts
    const slug = fileData.slug ?? ""

    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      const title = String(fileData.frontmatter?.title ?? "")
      if (
        slug.startsWith("companies/") &&
        COMPANY_SUFFIX.test(title) &&
        !flagged.has(slug)
      ) {
        flagged.add(slug)
        console.log(
          styleText("yellow", `Warning: no canonical facts declared on company page ${slug}`),
        )
      }
      return null
    }

    const entries = Object.entries(raw as Record<string, unknown>)
      .map(([k, v]) => [k, toText(v)] as const)
      .filter((e): e is readonly [string, string] => e[1] !== null)

    if (entries.length === 0) return null

    entries.sort((a, b) => {
      const ai = FIELD_ORDER.indexOf(a[0])
      const bi = FIELD_ORDER.indexOf(b[0])
      if (ai === -1 && bi === -1) return 0
      if (ai === -1) return 1
      if (bi === -1) return -1
      return ai - bi
    })

    const title = toText((raw as Record<string, unknown>).registered_name) ?? fileData.frontmatter?.title ?? ""

    // A plain-text block for the whole card, built server-side so "copy all" needs no
    // string assembly in the browser and always matches what is on screen.
    const block = [title, ...entries.filter(([k]) => k !== "registered_name").map(([k, v]) => `${humanise(k)}: ${v}`)]
      .filter(Boolean)
      .join("\n")

    return (
      <section class="wiki-facts" data-wiki-facts="page" data-block={block} aria-labelledby="wiki-facts-h">
        <div class="wiki-facts-head">
          <h2 id="wiki-facts-h">Info</h2>
          <button type="button" class="wiki-facts-all" data-copy-all>
            Copy all
          </button>
        </div>
        <dl class="wiki-facts-list">
          {entries.map(([key, value]) => (
            <div class="wiki-fact" data-key={key} data-label={humanise(key)} data-value={value}>
              <dt>{humanise(key)}</dt>
              <dd>
                <button type="button" class="wiki-fact-value" data-copy title="Copy">
                  <span class="wiki-fact-text">{value}</span>
                  <span class="wiki-fact-mark" aria-hidden="true" />
                </button>
              </dd>
            </div>
          ))}
        </dl>
        <p class="wiki-facts-live" role="status" aria-live="polite" />
      </section>
    )
  }

  Facts.css = style
  Facts.afterDOMLoaded = script

  return Facts
}) satisfies QuartzComponentConstructor
