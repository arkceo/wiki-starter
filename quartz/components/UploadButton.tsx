import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
import style from "./styles/upload.scss"
import { classNames } from "../util/lang"

/**
 * UploadButton — "Ask" and "Upload", top right of every page.
 *
 * Both open the Upload & Logs page served by the local viewer (scripts/wiki_server.py, page
 * in engine/upload/upload.html): "Upload" for drag-and-drop into the queue, per-file
 * progress and the live log; "Ask" at its Ask tab, where a question about the business is
 * answered from the wiki's own pages. Asking is there, not on the page itself: only the
 * Upload app can start work that costs money, and a page (written from a document) never
 * can. Plain links, so they work without any script on the page, and they opt out of
 * Quartz's in-page navigation (data-router-ignore): /upload is not a Quartz page, so
 * swapping it in would leave its own script unrun.
 */
const UploadButton: QuartzComponent = ({ displayClass }: QuartzComponentProps) => {
  return (
    <div class={classNames(displayClass, "wiki-links")}>
      <a class="ask-link" href="/upload#ask" data-router-ignore title="Ask a question about the business">
        <svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
          stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z" />
        </svg>
        <span>Ask</span>
      </a>
      <a class="upload-link" href="/upload" data-router-ignore title="Add documents and see what the wiki is doing">
        <svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
          stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 16V4M6 10l6-6 6 6M4 20h16" />
        </svg>
        <span>Upload</span>
      </a>
    </div>
  )
}

UploadButton.css = style

export default (() => UploadButton) satisfies QuartzComponentConstructor
