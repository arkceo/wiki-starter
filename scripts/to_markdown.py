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
  - Never stuck on one file: the tools it runs (OCR, sips) have time limits, OCR's growing
    with the page count, and the conversion itself may take --file-seconds per file (15
    minutes; WIKI_CONVERT_FILE_SECONDS). A file over its limit gets an ERROR mirror ("took
    too long") and the next file starts. While a tool runs, a "still working" line every
    minute shows the conversion is alive (the runner stops one that falls silent). A tool's
    time is measured on a clock that stops while the Mac sleeps, so closing the lid never
    makes a tool look too slow
  - Never ended by one file: a mirror that cannot be written (a source name too long for
    "<name>.md", a full disk) is reported on stderr ("could not write the mirror") and
    the next file starts
  - Stopped (SIGTERM, as the runner stops a converter that hangs), it stops the tool it is
    running with everything that tool started, and removes its temporary files
  - Folders whose name starts with a dot are skipped, as the runner skips them

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
import argparse, contextlib, datetime, hashlib, logging, os, re, shutil, signal, subprocess, sys, tempfile
import time

logging.disable(logging.CRITICAL)  # silence pypdf's malformed-xref chatter

TEXT_NATIVE = {".md", ".markdown", ".txt"}
SKIP_EXT = {".svg", ".ai", ".crdownload", ".textclipping", ".mp4", ".mov"}
SCAN_THRESHOLD = 40  # < this many chars on sampled pages => treat the PDF as scanned
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
HEIC_EXT = {".heic", ".heif"}
FILE_SECONDS = 900          # converting one file, outside tools apart (--file-seconds)
OCR_SECONDS = 120           # ocrmypdf: this much, plus OCR_PAGE_SECONDS a page, at most
OCR_PAGE_SECONDS = 60       #   OCR_MAX_SECONDS: a long scan may take its time, a stuck
OCR_MAX_SECONDS = 3 * 3600  #   one never holds the queue for ever
TESSERACT_SECONDS = 300     # one photo, turned one way
SIPS_SECONDS = 120          # one HEIC photo to JPEG
HEARTBEAT_SECONDS = 60      # a "still working" line this often while a tool runs


class FileTimeout(BaseException):
    """One file's conversion went over its time limit. A BaseException, so that a
    library's own `except Exception` cannot swallow it."""


def ocr_jobs():
    """Cores for one OCR: the Mac's, shared among the conversions running side by side
    (WIKI_CONVERT_JOBS, set by the runner), so that together they do not overload it."""
    try:
        side_by_side = max(1, int(os.environ.get("WIKI_CONVERT_JOBS") or 1))
    except ValueError:
        side_by_side = 1
    return max(1, (os.cpu_count() or 1) // side_by_side)


def span(seconds):
    """900 -> "15 min", 3 -> "3 s"."""
    seconds = int(seconds)
    return f"{round(seconds / 60)} min" if seconds >= 60 else f"{seconds} s"


def _alarm(signum, frame):
    signal.alarm(30)   # should something swallow this one anyway, it comes back
    raise FileTimeout()


def start_clock(seconds):
    """Start one file's clock (SIGALRM, where the system has it)."""
    if seconds > 0 and hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(int(seconds))


def stop_clock():
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)


_TOOL_PID = None   # the outside tool running now (its own process group), for _on_term


def _on_term(signum, frame):
    """SIGTERM (the runner stopping a converter that hangs): the tool at work goes too, with
    everything it started (OCR's workers), instead of running on with no parent and no
    time limit. SystemExit is no Exception: the file's own `finally` still runs and removes
    its temporary folder."""
    if _TOOL_PID:
        try:
            os.killpg(_TOOL_PID, signal.SIGKILL)
        except OSError:
            pass
    raise SystemExit(128 + signum)


def run_tool(cmd, seconds, what, text=False):
    """Run an outside tool (OCR, a photo conversion) for at most `seconds` and return its
    CompletedProcess (output captured). The file's own clock stops meanwhile, since the
    tool has its own limit; every HEARTBEAT_SECONDS a line says it is still at work. A
    tool over its limit is stopped with everything it started, and raises RuntimeError.
    The time is the Mac's awake time: a monotonic clock (on a Mac it stops during sleep),
    and a pass of the loop is never charged more than it waited, should a clock go on
    while the Mac sleeps."""
    global _TOOL_PID
    left = signal.alarm(0) if hasattr(signal, "SIGALRM") else 0
    took, mark = 0.0, time.monotonic()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, text=text, errors="replace" if text else None)
        _TOOL_PID = p.pid
        while True:
            wait = max(1, min(HEARTBEAT_SECONDS, seconds - took))
            try:
                out, err = p.communicate(timeout=wait)
                return subprocess.CompletedProcess(cmd, p.returncode, out, err)
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                took += min(now - mark, wait + 1)
                mark = now
                if took >= seconds:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except OSError:
                        p.kill()
                    p.communicate()
                    raise RuntimeError(f"{what} took longer than {span(seconds)}")
                print(f"    still working: {what}, {span(took)}", file=sys.stderr, flush=True)
    finally:
        _TOOL_PID = None
        if left:
            signal.alarm(left)   # the file's clock goes on where it stopped


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
        try:
            r = run_tool(["sips", "-s", "format", "jpeg", src, "--out", dest], SIPS_SECONDS, "sips")
            if r.returncode == 0 and os.path.exists(dest):
                return True
        except RuntimeError:
            pass   # too slow: Pillow may still manage
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
        r = run_tool(["tesseract", img, "stdout", "-l", "eng"], TESSERACT_SECONDS, "tesseract", text=True)
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
    ap.add_argument("--file-seconds", type=int,
                    default=int(os.environ.get("WIKI_CONVERT_FILE_SECONDS") or FILE_SECONDS),
                    help="time one file's conversion may take, outside tools apart; 0 = no limit "
                         f"(default {FILE_SECONDS}, or WIKI_CONVERT_FILE_SECONDS)")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _on_term)
    wanted = None
    if a.list:
        root = os.path.abspath(a.root)
        with open(a.list, encoding="utf-8") as f:
            # One path per line, exactly as listed: a name may end in a space.
            wanted = {os.path.relpath(os.path.abspath(p.rstrip("\n")), root) for p in f if p.rstrip("\n")}

    from markitdown import MarkItDown
    try:  # markitdown tells file types apart with onnxruntime, whose telemetry can abort Python
        import onnxruntime
        onnxruntime.disable_telemetry_events()
    except Exception:
        pass
    md = MarkItDown(enable_plugins=False)
    has_ocr = shutil.which("ocrmypdf") is not None
    has_tesseract = shutil.which("tesseract") is not None
    if a.ocr == "auto" and not has_ocr:
        print("[warn] ocrmypdf not found - scanned PDFs will be flagged, not OCR'd "
              "(install: tesseract + ghostscript + `pip install ocrmypdf`)")

    done = skipped = ocred = flagged = errors = idx = deferred = 0
    for dirpath, dirs, files in os.walk(a.root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))   # as the runner's listing
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
            try:
                os.makedirs(os.path.dirname(outp), exist_ok=True)
            except OSError:
                pass   # writing the mirror fails too, and says so

            if not a.force and os.path.exists(outp) and os.path.getmtime(outp) >= os.path.getmtime(src):
                skipped += 1
                continue
            idx += 1
            print(f"[{idx}] {rel}", file=sys.stderr, flush=True)
            if is_lfs_pointer(src):
                errors += 1
                _write_error(outp, rel, "git-lfs pointer not materialized (run `git lfs pull`)")
                continue

            method, target, note, tmpdir, preview_rel, taken = "markitdown", src, "", None, "", ""
            try:
                start_clock(a.file_seconds)
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
                                r = run_tool(["ocrmypdf", "--force-ocr", "--output-type", "pdf",
                                              "--jobs", str(ocr_jobs()), "-l", "eng", src, ocr_pdf],
                                             min(OCR_MAX_SECONDS, OCR_SECONDS + OCR_PAGE_SECONDS * max(1, npages or 1)),
                                             "OCR")
                                if r.returncode != 0:
                                    said = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
                                    raise RuntimeError(f"ocrmypdf exited {r.returncode}"
                                                       + (f": {said[-1][:150]}" if said else ""))
                                target, method = ocr_pdf, "ocr+markitdown"
                                ocred += 1
                    text = md.convert(target).text_content
                stop_clock()
                _write(outp, rel, method, text=text, src=src, note=note, preview=preview_rel, taken=taken)
                done += 1
            except FileTimeout:
                stop_clock()
                errors += 1
                print(f"    stopped: the conversion took longer than {span(a.file_seconds)}",
                      file=sys.stderr, flush=True)
                _write_error(outp, rel, f"conversion took too long (over {span(a.file_seconds)})")
            except Exception as e:
                stop_clock()
                errors += 1
                _write_error(outp, rel, str(e)[:200])
            finally:
                stop_clock()
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


def _write_error(outp, rel, note):
    """An ERROR mirror, from inside the loop: one that cannot be written either (the name
    too long for "<name>.md", a full disk) is reported, and the next file starts."""
    try:
        _write(outp, rel, "ERROR", note=note)
    except OSError as w:
        print(f"    could not write the mirror: {w}", file=sys.stderr, flush=True)


def write_error(outp, rel, note):
    """An ERROR mirror for one source, written from outside (the runner, when this script
    died or hung on that file): the file then counts as converted and unreadable, and is
    not converted again until it changes."""
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    _write(outp, rel, "ERROR", note=note)


def _summary(done, ocred, flagged, skipped, errors, deferred, out, limit=False):
    tag = "[limit reached] " if limit else ""
    print(f"{tag}converted={done} (ocr={ocred}, flagged-scanned={flagged}, deferred-large={deferred})  "
          f"skipped(up-to-date)={skipped}  errors={errors}")
    print(f"cache: {out}")


if __name__ == "__main__":
    main()
    # Done: leave without the native libraries' teardown. onnxruntime can abort as Python
    # exits (a Mac then says "Python quit unexpectedly"), after every mirror is written.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
