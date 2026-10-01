import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
import style from "./styles/upload.scss"
import { classNames } from "../util/lang"

/**
 * UploadButton — "Upload", top right of every page.
 *
 * Opens /upload, the Upload & Logs page served by the local viewer (scripts/wiki_server.py,
 * page in engine/upload/upload.html): drag-and-drop into the queue, per-file progress and
 * the live log. It is a plain link, so it works without any script on the page, and it
 * opts out of Quartz's in-page navigation (data-router-ignore): /upload is not a Quartz
 * page, so swapping it in would leave its own script unrun.
 */
const UploadButton: QuartzComponent = ({ displayClass }: QuartzComponentProps) => {
  return (
    <a
      class={classNames(displayClass, "upload-link")}
      href="/upload"
      data-router-ignore
      title="Add documents and see what the wiki is doing"
    >
      <svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
        stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 16V4M6 10l6-6 6 6M4 20h16" />
      </svg>
      <span>Upload</span>
    </a>
  )
}

UploadButton.css = style

export default (() => UploadButton) satisfies QuartzComponentConstructor
