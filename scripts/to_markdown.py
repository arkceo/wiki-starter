#!/usr/bin/env python3
"""
to_markdown.py - Convert raw/ sources to a cached Markdown mirror for the wiki.

Why: reading binaries directly (esp. PDFs as page-images) is token-expensive and
can't open Office files at all. Converting to Markdown once, then reading the .md,
is ~3-7x cheaper on digital PDFs and unlocks docx/xlsx/pptx entirely.

What it does:
  - Digital docs (PDF with a text layer, docx, xlsx, pptx, html, csv, json, epub, ...) -> MarkItDown
  - Scanned PDFs (no text layer)  -> ocrmypdf (Tesseract) adds a text layer, then MarkItDown
  - Plain text/markdown            -> copied through
  - Output mirrors raw/ structure under the cache dir, one <name>.md per source
  - Idempotent: skips a source whose cache .md is newer than the source (unless --force)
  - Each .md carries frontmatter: source path, sha256 (short), bytes, method, date

Run anywhere (a sandbox, CI, or a Mac). Requirements:
  pip install 'markitdown[all]' pypdf
  OCR (optional but recommended): system 'tesseract' + 'ghostscript', plus  pip install ocrmypdf

Examples:
  python3 scripts/to_markdown.py                          # convert all of raw/ -> cache/md/
  python3 scripts/to_markdown.py --only _intake           # just one subtree
  python3 scripts/to_markdown.py --out /tmp/md --limit 20 # dry-ish trial elsewhere
  python3 scripts/to_markdown.py --ocr off                # skip OCR (digital + Office only)
"""
import argparse, contextlib, datetime, hashlib, logging, os, shutil, subprocess, sys, tempfile

logging.disable(logging.CRITICAL)  # silence pypdf's malformed-xref chatter

TEXT_NATIVE = {".md", ".markdown", ".txt"}
SKIP_EXT = {".svg", ".ai", ".crdownload", ".textclipping", ".mp4", ".mov"}
SCAN_THRESHOLD = 40  # < this many chars on sampled pages => treat the PDF as scanned


def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def pdf_sample_has_text(path):
    """Cheap check: does the first and middle page carry a real text layer?"""
    from pypdf import PdfReader
    with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn):
        r = PdfReader(path)
        n = len(r.pages)
        best = ""
        for i in sorted({0, n // 2 if n > 1 else 0}):
            t = r.pages[i].extract_text() or ""
            if len(t) > len(best):
                best = t
    return len(best.strip()) >= SCAN_THRESHOLD, n


def is_lfs_pointer(path):
    try:
        if os.path.getsize(path) < 300:
            with open(path, "rb") as f:
                return f.read(40).startswith(b"version https://git-lfs")
    except OSError:
        pass
    return False


def main():
    ap = argparse.ArgumentParser(description="Convert raw/ sources to a Markdown cache.")
    ap.add_argument("--root", default="raw", help="source tree to walk (default: raw)")
    ap.add_argument("--out", default="cache/md", help="cache output dir (default: cache/md)")
    ap.add_argument("--ocr", choices=["auto", "off"], default="auto", help="OCR scanned PDFs")
    ap.add_argument("--force", action="store_true", help="reconvert even if cache is up to date")
    ap.add_argument("--limit", type=int, default=0, help="stop after N conversions (testing)")
    ap.add_argument("--only", default="", help="only process source paths containing this substring")
    ap.add_argument("--max-ocr-pages", type=int, default=0,
                    help="defer (skip, don't write) scanned PDFs with more than N pages; 0 = no limit")
    a = ap.parse_args()

    from markitdown import MarkItDown
    md = MarkItDown(enable_plugins=False)
    has_ocr = shutil.which("ocrmypdf") is not None
    if a.ocr == "auto" and not has_ocr:
        print("[warn] ocrmypdf not found - scanned PDFs will be flagged, not OCR'd "
              "(install: tesseract + ghostscript + `pip install ocrmypdf`)")

    done = skipped = ocred = flagged = errors = idx = deferred = 0
    for dirpath, _, files in os.walk(a.root):
        for fn in sorted(files):
            if fn.startswith(".") or fn == ".gitkeep":
                continue
            src = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXT or (a.only and a.only not in src):
                continue
            rel = os.path.relpath(src, a.root)
            outp = os.path.join(a.out, rel + ".md")
            os.makedirs(os.path.dirname(outp), exist_ok=True)

            if not a.force and os.path.exists(outp) and os.path.getmtime(outp) >= os.path.getmtime(src):
                skipped += 1
                continue
            idx += 1
            print(f"[{idx}] {rel}", file=sys.stderr, flush=True)
            if is_lfs_pointer(src):
                errors += 1
                _write(outp, rel, "ERROR", note="git-lfs pointer not materialized (run `git lfs pull`)")
                continue

            method, target, note, tmpdir = "markitdown", src, "", None
            try:
                if ext in TEXT_NATIVE:
                    text = open(src, encoding="utf-8", errors="replace").read()
                    method = "copy"
                else:
                    if ext == ".pdf":
                        try:
                            has_text, npages = pdf_sample_has_text(src)
                        except Exception:
                            has_text, npages = True, 0  # let MarkItDown try
                        if not has_text:
                            # Scanned PDF (no text layer). Never write an empty .md —
                            # that would mark it "done" and block a later OCR pass.
                            if a.ocr == "off":
                                print(f"    deferred: scanned, --ocr off ({npages}pg)",
                                      file=sys.stderr, flush=True)
                                deferred += 1
                                continue
                            if not has_ocr:
                                note = "scanned-no-ocr"
                                flagged += 1
                            elif a.max_ocr_pages and (npages or 0) > a.max_ocr_pages:
                                print(f"    deferred: scanned {npages}pg > max-ocr-pages {a.max_ocr_pages}",
                                      file=sys.stderr, flush=True)
                                deferred += 1
                                continue
                            else:
                                print(f"    ocr: {npages} pages", file=sys.stderr, flush=True)
                                tmpdir = tempfile.mkdtemp()
                                ocr_pdf = os.path.join(tmpdir, "ocr.pdf")
                                subprocess.run(
                                    ["ocrmypdf", "--force-ocr", "--output-type", "pdf",
                                     "-l", "eng", src, ocr_pdf],
                                    check=True, capture_output=True)
                                target, method = ocr_pdf, "ocr+markitdown"
                                ocred += 1
                    text = md.convert(target).text_content
                _write(outp, rel, method, text=text, src=src, note=note)
                done += 1
            except Exception as e:
                errors += 1
                _write(outp, rel, "ERROR", note=str(e)[:200])
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)

            if a.limit and done >= a.limit:
                _summary(done, ocred, flagged, skipped, errors, deferred, a.out, limit=True)
                return
    _summary(done, ocred, flagged, skipped, errors, deferred, a.out)


def _write(outp, rel, method, text=None, src=None, note=""):
    fm = [f"source: {rel}", f"method: {method}", f"converted: {datetime.date.today().isoformat()}"]
    if src and method != "ERROR":
        fm.insert(1, f"sha256: {sha16(src)}")
        fm.insert(2, f"bytes: {os.path.getsize(src)}")
    if note:
        fm.append(f"note: {note}")
    body = text if text else ""
    with open(outp, "w", encoding="utf-8") as g:
        g.write("---\n" + "\n".join(fm) + "\n---\n\n" + body)


def _summary(done, ocred, flagged, skipped, errors, deferred, out, limit=False):
    tag = "[limit reached] " if limit else ""
    print(f"{tag}converted={done} (ocr={ocred}, flagged-scanned={flagged}, deferred-large={deferred})  "
          f"skipped(up-to-date)={skipped}  errors={errors}")
    print(f"cache: {out}")


if __name__ == "__main__":
    main()
