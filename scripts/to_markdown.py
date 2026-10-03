#!/usr/bin/env python3
"""
to_markdown.py - Convert raw/ sources to a cached Markdown mirror for the wiki.

Why: reading binaries directly (esp. PDFs as page-images) is token-expensive and
can't open Office files at all. Converting to Markdown once, then reading the .md,
is ~3-7x cheaper on digital PDFs and unlocks docx/xlsx/pptx entirely.

What it does:
  - Digital docs (PDF with a text layer, docx, xlsx, pptx, html, csv, json, epub, ...) -> MarkItDown
  - Scanned PDFs (no text layer)  -> ocrmypdf (Tesseract) adds a text layer, then MarkItDown
  - Photos and images              -> Tesseract OCR, turned upright first (Pillow, if installed),
                                      and the date the photo was taken (never its location);
                                      HEIC/HEIF become a JPEG first (macOS sips, or pillow-heif),
                                      kept next to the mirror as <name>.jpg so a reader that
                                      can see images can look at it
  - Plain text/markdown            -> copied through
  - Output mirrors raw/ structure under the cache dir, one <name>.md per source
  - Idempotent: skips a source whose cache .md is newer than the source (unless --force)
  - Each .md carries frontmatter: source path, sha256 (short), bytes, method, date

Run anywhere (a sandbox, CI, or a Mac). Requirements:
  pip install 'markitdown[all]' pypdf
  OCR (optional but recommended): system 'tesseract' + 'ghostscript', plus  pip install ocrmypdf
  (images need only 'tesseract'; Pillow straightens rotated phone photos)

Examples:
  python3 scripts/to_markdown.py                          # convert all of raw/ -> cache/md/
  python3 scripts/to_markdown.py --only _intake           # just one subtree
  python3 scripts/to_markdown.py --out /tmp/md --limit 20 # dry-ish trial elsewhere
  python3 scripts/to_markdown.py --ocr off                # skip OCR (digital + Office only)
  python3 scripts/to_markdown.py --list batch.txt         # only the sources named in a file
"""
import argparse, contextlib, datetime, hashlib, logging, os, re, shutil, subprocess, sys, tempfile

logging.disable(logging.CRITICAL)  # silence pypdf's malformed-xref chatter

TEXT_NATIVE = {".md", ".markdown", ".txt"}
SKIP_EXT = {".svg", ".ai", ".crdownload", ".textclipping", ".mp4", ".mov"}
SCAN_THRESHOLD = 40  # < this many chars on sampled pages => treat the PDF as scanned
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
HEIC_EXT = {".heic", ".heif"}


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


def heic_to_jpeg(src, dest):
    """Convert a HEIC/HEIF photo (an iPhone's default) to JPEG. True if it worked."""
    if shutil.which("sips"):
        r = subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", dest],
                           capture_output=True)
        if r.returncode == 0 and os.path.exists(dest):
            return True
    try:
        import pillow_heif
        from PIL import Image
        pillow_heif.register_heif_opener()
        Image.open(src).convert("RGB").save(dest, "JPEG", quality=90)
        return True
    except Exception:
        return False


def upright_copy(src, tmpdir, turn=0):
    """A copy turned the way the camera held it (phones store a rotation flag that OCR
    ignores), then by `turn` degrees, as PNG. The original path if Pillow is missing or
    cannot open it."""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(src))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if turn:
            im = im.rotate(turn, expand=True)
        out = os.path.join(tmpdir, f"upright-{turn}.png")
        im.save(out)
        return out
    except Exception:
        return src


def photo_taken(path):
    """When the photo was taken, from its own data ("" if it does not say). The camera's
    location is never read: a photo's GPS position stays out of the text."""
    try:
        from PIL import Image
        exif = Image.open(path).getexif()
        when = exif.get_ifd(0x8769).get(36867) or exif.get(306) or ""   # DateTimeOriginal, DateTime
    except Exception:
        return ""
    when = str(when).strip()
    if len(when) >= 16 and when[4] == ":" and when[7] == ":":
        return when[:10].replace(":", "-") + " " + when[11:16]
    return ""


def words(text):
    """How much of the text reads as words (text OCR'd upside down mostly does not)."""
    return len(re.findall(r"[A-Za-z]{4,}", text))


def ocr_image(path, tmpdir):
    """The text Tesseract finds in an image ("" if none). A photo with little readable
    text is also tried turned a quarter, a half and three quarters, in case it lost its
    rotation flag; the most readable result wins."""
    best = None
    for turn in (0, 90, 180, 270):
        img = upright_copy(path, tmpdir, turn)
        if turn and img == path:
            break   # no Pillow: nothing to turn
        r = subprocess.run(["tesseract", img, "stdout", "-l", "eng"],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise RuntimeError("tesseract: " + (r.stderr or "failed").strip()[:150])
        text = r.stdout.strip()
        if best is None or words(text) > words(best):
            best = text
        if words(best) >= 8:
            break
    return best or ""


def main():
    ap = argparse.ArgumentParser(description="Convert raw/ sources to a Markdown cache.")
    ap.add_argument("--root", default="raw", help="source tree to walk (default: raw)")
    ap.add_argument("--out", default="cache/md", help="cache output dir (default: cache/md)")
    ap.add_argument("--ocr", choices=["auto", "off"], default="auto", help="OCR scanned PDFs and images")
    ap.add_argument("--force", action="store_true", help="reconvert even if cache is up to date")
    ap.add_argument("--limit", type=int, default=0, help="stop after N conversions (testing)")
    ap.add_argument("--only", default="", help="only process source paths containing this substring")
    ap.add_argument("--max-ocr-pages", type=int, default=0,
                    help="defer (skip, don't write) scanned PDFs with more than N pages; 0 = no limit")
    ap.add_argument("--list", default="",
                    help="only process the sources named in this file, one path per line")
    a = ap.parse_args()
    wanted = None
    if a.list:
        root = os.path.abspath(a.root)
        with open(a.list, encoding="utf-8") as f:
            wanted = {os.path.relpath(os.path.abspath(p.strip()), root) for p in f if p.strip()}

    from markitdown import MarkItDown
    md = MarkItDown(enable_plugins=False)
    has_ocr = shutil.which("ocrmypdf") is not None
    has_tesseract = shutil.which("tesseract") is not None
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
            if wanted is not None and rel not in wanted:
                continue
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

            method, target, note, tmpdir, preview_rel, taken = "markitdown", src, "", None, "", ""
            try:
                if ext in TEXT_NATIVE:
                    text = open(src, encoding="utf-8", errors="replace").read()
                    method = "copy"
                elif ext in IMAGE_EXT:
                    # A photo or image: its words are only in the pixels. Never write an
                    # empty .md for one OCR was not asked for, as with scanned PDFs.
                    if a.ocr == "off":
                        print("    deferred: image, --ocr off", file=sys.stderr, flush=True)
                        deferred += 1
                        continue
                    tmpdir = tempfile.mkdtemp()
                    target, text, method = src, "", "image"
                    if ext in HEIC_EXT:
                        preview = os.path.splitext(outp)[0] + ".jpg"   # <name>.heic.jpg
                        if heic_to_jpeg(src, preview):
                            target = preview
                        else:
                            note = "heic-not-converted"
                    if note:
                        flagged += 1
                    elif not has_tesseract:
                        note = "image-no-ocr"
                        flagged += 1
                    else:
                        print("    ocr: 1 page (image)", file=sys.stderr, flush=True)
                        text = ocr_image(target, tmpdir)
                        method = "ocr-image"
                        ocred += 1
                        if len(text) < SCAN_THRESHOLD:
                            note = "image-little-text"
                    if target != src:
                        preview_rel = os.path.relpath(target, a.out)
                    taken = photo_taken(target)
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
                _write(outp, rel, method, text=text, src=src, note=note, preview=preview_rel, taken=taken)
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


def _write(outp, rel, method, text=None, src=None, note="", preview="", taken=""):
    fm = [f"source: {rel}", f"method: {method}", f"converted: {datetime.date.today().isoformat()}"]
    if src and method != "ERROR":
        fm.insert(1, f"sha256: {sha16(src)}")
        fm.insert(2, f"bytes: {os.path.getsize(src)}")
    if note:
        fm.append(f"note: {note}")
    if preview:
        fm.append(f"preview: {preview}")
    if taken:
        fm.append(f"taken: {taken}")
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
