/**
 * Click-to-copy for the canonical fact card.
 *
 * Handlers are delegated from the document rather than bound per row, so a card cloned
 * into the Copy sheet works without being re-wired, and nothing has to be torn down when
 * Quartz morphs the body on navigation.
 *
 * The clipboard path has a fallback. `navigator.clipboard` needs a secure context and is
 * absent in enough real situations — an older iOS, a plain-http preview — that failing
 * silently would be the worst outcome for the one feature this build exists to deliver.
 */

async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through
  }
  try {
    const ta = document.createElement("textarea")
    ta.value = text
    ta.setAttribute("readonly", "")
    ta.style.position = "fixed"
    ta.style.top = "-1000px"
    ta.style.opacity = "0"
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, ta.value.length)
    const ok = document.execCommand("copy")
    ta.remove()
    return ok
  } catch {
    return false
  }
}

function announce(scope: Element | null, message: string) {
  const live = scope?.querySelector<HTMLElement>(".wiki-facts-live")
  if (live) live.textContent = message
}

function flash(el: Element, cls: string) {
  el.classList.add(cls)
  window.setTimeout(() => el.classList.remove(cls), 1400)
}

async function onCopyClick(e: Event) {
  const target = e.target as HTMLElement

  const one = target.closest<HTMLElement>("[data-copy]")
  if (one) {
    const row = one.closest<HTMLElement>(".wiki-fact")
    const value = row?.dataset.value
    if (!value) return
    const card = one.closest(".wiki-facts")
    const ok = await writeClipboard(value)
    if (row && ok) flash(row, "copied")
    announce(card, ok ? `${row?.dataset.label ?? "Value"} copied` : "Copy failed")
    return
  }

  const all = target.closest<HTMLElement>("[data-copy-all]")
  if (all) {
    const card = all.closest<HTMLElement>(".wiki-facts")
    const block = card?.dataset.block
    if (!block) return
    const ok = await writeClipboard(block)
    if (ok) flash(all, "copied")
    announce(card, ok ? "All facts copied" : "Copy failed")
  }
}

document.addEventListener("click", onCopyClick)
