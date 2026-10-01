# Wiki Intake

Drop source documents here: PDF, Word, Excel, PowerPoint, CSV, images, scans.

The background runner notices them within a minute, waits until they have finished
copying, converts each one to text (with OCR for scans), and has Claude summarise it into
the right wiki pages. The document is then filed under `raw/<project>/` and disappears
from this folder. A notification confirms each update.

- Update Packets (`.md` notes from a Claude conversation) go into **Wiki Inbox** instead.
  A packet dropped here is still processed, just as a document.
- Files still downloading (`.download`, `.part`, `.crdownload`) are left alone until they
  finish.
- Nothing here is published. Everything stays on this Mac; only the text Claude reads is
  sent to Anthropic for processing.
