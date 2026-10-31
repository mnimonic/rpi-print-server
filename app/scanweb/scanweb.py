#!/usr/bin/env python3
"""Tiny web front-end for scanning on the HP DeskJet 1515 via SANE (scanimage).

Each scan runs as a background job and is saved on the Pi, so it survives a closed browser
or a dropped connection: the page polls /scans and lists the saved files for download."""
import json, os, re, shutil, struct, subprocess, threading, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

PORT = 8080
# Saved scans and work files live on the SD card: /tmp is a small RAM disk on the Pi, and a
# 1200 dpi colour page is ~420 MB raw. systemd's StateDirectory= sets STATE_DIRECTORY.
STATE_DIR = os.environ.get("STATE_DIRECTORY", "/var/lib/scanweb")
SCAN_DIR = os.path.join(STATE_DIR, "scans")
WORK_DIR = os.path.join(STATE_DIR, "work")
MIN_FREE = 500 << 20   # keep this much of the SD card free besides the scan itself
# Lineart only works as raw PNM output from scanimage (its PDF/PNG writers fail in that mode),
# so for Lineart we scan PNM and build the PDF/PNG ourselves.
MODES = {"Color": "Colour", "Gray": "Greyscale", "Lineart": "Black & white (smallest files)",
         "GrayBW": "Black & white – enhanced (keeps light text)"}
# GrayBW: scan in greyscale and threshold here. The scanner's own Lineart cut-off drops
# light-grey text; anything darker than this (paper scans as 255) becomes black.
BW_THRESHOLD = 200
# 1200 dpi is the scanner's optical limit; 2400 is interpolated by the driver. Above 600 dpi
# everything is converted in blocks of rows, so even a 1.7 GB raw page fits in the Pi's RAM.
RESOLUTIONS = {"75": "75 dpi", "100": "100 dpi", "200": "200 dpi", "300": "300 dpi",
               "600": "600 dpi", "1200": "1200 dpi (slow)", "2400": "2400 dpi (upscaled, very slow)"}
FORMATS = {"pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png",
           "ocrpdf": "application/pdf", "txt": "text/plain; charset=utf-8"}   # last two: OCR
EXTENSIONS = {"jpeg": "jpg", "ocrpdf": "pdf"}
JPEG_QUALITY = 90   # for cjpeg (above 600 dpi)
# Text recognition (Tesseract). A 300 dpi A4 page takes ~2 min with English + Greek on the
# Pi 1 (~1.5 min with one language); higher resolutions don't read better, only slower.
OCR_LANGS = {"eng+ell": "English + Greek", "eng": "English", "ell": "Greek"}
OCR_MAX_RES = 300
OCR_TIMEOUT = 900
PAPER = {"a4": ("210", "297"), "letter": ("215.9", "279.4"), "full": ("215.9", "297.011")}
# A4 grey at 1200 dpi took 4.5 min of scanner time
SCAN_TIMEOUTS = {"1200": 1800, "2400": 3600}
SCAN_TIMEOUT = 600
BLOCK = 4 << 20   # bytes of raw image converted at a time

scan_lock = threading.Lock()   # held from the request that starts a job until the job ends
state_lock = threading.Lock()  # guards `scans`
scans = {}                     # id -> record, also saved as SCAN_DIR/<id>.json
device = None


class ScanError(Exception):
    """A failure with a message meant for the user."""


def jpeg_info(data):
    """Return (width, height, components) from a JPEG's SOF header."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h, data[i + 9]
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("not a valid JPEG")


def read_pnm_header(data, magic, nvals):
    """Parse a binary PNM header: return (header values, offset of pixel data)."""
    if data[:2] != magic:
        raise ValueError(f"expected a {magic.decode()} PNM image")
    vals, i = [], 2
    while len(vals) < nvals:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            i = data.index(b"\n", i) + 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        vals.append(int(data[i:j]))
        i = j
    return vals, i + 1


def open_pnm(path):
    """Open a raw P4/P5/P6 scan: return (file positioned at the pixels, magic, width, rows,
    bytes per row). hpaio sometimes delivers fewer rows than the header announces (seen at
    600 and 1200 dpi), so rows is what the file really holds."""
    f = open(path, "rb")
    head = f.read(512)
    magic = head[:2]
    if magic not in (b"P4", b"P5", b"P6"):
        f.close()
        raise ValueError("expected a P4/P5/P6 PNM image")
    vals, off = read_pnm_header(head, magic, 2 if magic == b"P4" else 3)
    w, h = vals[0], vals[1]
    rowb = {b"P4": (w + 7) // 8, b"P5": w, b"P6": 3 * w}[magic]
    got = (os.path.getsize(path) - off) // rowb
    if got < h:
        print(f"scan short: header {h} rows, received {got}", flush=True)
        h = got
    f.seek(off)
    return f, magic, w, h, rowb


def pnm_blocks(f, rowb, rows):
    """Yield the pixel data in blocks of whole rows."""
    n = max(1, BLOCK // rowb)
    for r in range(0, rows, n):
        yield f.read(rowb * min(n, rows - r))


def bw_image(path, threshold):
    """Return (width, rows, blocks of packed 1-bit rows; bit 1 = black) from a P4 scan, or
    from a P5 scan thresholded here. translate + int(..., 2) keep the per-pixel work at C
    speed on the Pi; working in blocks keeps 1200/2400 dpi pages within its RAM."""
    f, magic, w, h, rowb = open_pnm(path)
    if magic == b"P6":
        f.close()
        raise ValueError("expected a black & white or greyscale scan")

    def blocks():
        with f:
            if magic == b"P4":
                yield from pnm_blocks(f, rowb, h)
                return
            lut = bytes(0x31 if v < threshold else 0x30 for v in range(256))   # b'1' / b'0'
            outb = (w + 7) // 8
            pad = b"0" * (outb * 8 - w)
            for block in pnm_blocks(f, rowb, h):
                s = block.translate(lut)
                yield b"".join(int(s[i:i + w] + pad, 2).to_bytes(outb, "big")
                               for i in range(0, len(s), w))
    return w, h, blocks()


def write_png(path, w, h, color_type, bit_depth, rowb, blocks, invert=False):
    """Stream rows into a PNG (filter 0, one IDAT chunk per compressed piece) so large scans
    need little RAM. invert: for 1-bit rows with 1 = black (PNG greyscale uses 1 = white)."""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    inv = bytes(255 - b for b in range(256)) if invert else None
    z = zlib.compressobj(6)
    with open(path, "wb") as out:
        out.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth,
                                                                    color_type, 0, 0, 0)))
        for block in blocks:
            if inv:
                block = block.translate(inv)
            c = z.compress(b"".join(b"\x00" + block[i:i + rowb] for i in range(0, len(block), rowb)))
            if c:
                out.write(chunk(b"IDAT", c))
        out.write(chunk(b"IDAT", z.flush()) + chunk(b"IEND", b""))


def write_image_pdf(path, image_dict, stream_path, page_w_mm, page_h_mm):
    """Write a one-page PDF that stretches a single image over the whole page. The image
    stream is copied from stream_path, so large scans need little RAM. (scanimage's own PDF
    writer leaves the lengths and xref as 0.)"""
    pw, ph = float(page_w_mm) * 72 / 25.4, float(page_h_mm) * 72 / 25.4
    content = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im1 Do Q".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] "
        f"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>".encode(),
        None,   # the image, written from stream_path
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    with open(path, "wb") as out:
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(objs, 1):
            offsets.append(out.tell())
            out.write(f"{n} 0 obj\n".encode())
            if body is None:
                out.write(f"<< /Type /XObject /Subtype /Image {image_dict} "
                          f"/Length {os.path.getsize(stream_path)} >>\nstream\n".encode())
                with open(stream_path, "rb") as s:
                    shutil.copyfileobj(s, out, 1 << 20)
                out.write(b"\nendstream")
            else:
                out.write(body)
            out.write(b"\nendobj\n")
        xref = out.tell()
        out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
        out.write(b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets))
        out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())


def jpeg_to_pdf(jpeg_path, path, page_w_mm, page_h_mm):
    with open(jpeg_path, "rb") as f:
        w, h, comps = jpeg_info(f.read(1 << 20))
    cs = "/DeviceGray" if comps == 1 else "/DeviceRGB"
    write_image_pdf(path, f"/Width {w} /Height {h} /ColorSpace {cs} /BitsPerComponent 8 "
                    f"/Filter /DCTDecode", jpeg_path, page_w_mm, page_h_mm)


def bw_to_pdf(w, h, blocks, path, page_w_mm, page_h_mm):
    """PDF holding a 1-bit image; /Decode [1 0] because the rows use 1 = black."""
    z, zpath = zlib.compressobj(6), path + ".z"
    try:
        with open(zpath, "wb") as out:
            for block in blocks:
                out.write(z.compress(block))
            out.write(z.flush())
        write_image_pdf(path, f"/Width {w} /Height {h} /ColorSpace /DeviceGray /BitsPerComponent 1 "
                        f"/Decode [1 0] /Filter /FlateDecode", zpath, page_w_mm, page_h_mm)
    finally:
        remove(zpath)


def pnm_to_jpeg(src, path):
    """Encode a raw P5/P6 scan with cjpeg, which streams (scanimage's own JPEG writer fails
    above 600 dpi). The header is rewritten with the rows really received: cjpeg stops at a
    short file."""
    f, magic, w, h, rowb = open_pnm(src)
    with f, open(path, "wb") as out:
        p = subprocess.Popen(["cjpeg", "-quality", str(JPEG_QUALITY)], stdin=subprocess.PIPE, stdout=out)
        try:
            p.stdin.write(b"%s\n%d %d\n255\n" % (magic, w, h))
            for block in pnm_blocks(f, rowb, h):
                p.stdin.write(block)
            p.stdin.close()
        except BrokenPipeError:
            pass
        if p.wait() != 0:
            raise ScanError("Could not encode the scan as JPEG.")


def ocr(src, out_base, res, lang, fmt):
    """Run Tesseract on a scanned image, writing out_base + ".pdf" (searchable PDF) or ".txt".
    For a JPEG, Tesseract's PDF embeds the original data unchanged; 1-bit images become G4."""
    kind = "pdf" if fmt == "ocrpdf" else "txt"
    t0 = time.time()
    # One OpenMP thread: on the single-core Pi more threads only add overhead (3.5 -> 2 min)
    try:
        p = subprocess.run(["tesseract", src, out_base, "-l", lang, "--dpi", res, kind],
                           capture_output=True, text=True, timeout=OCR_TIMEOUT,
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except subprocess.TimeoutExpired:
        raise ScanError("Reading the text took too long and was stopped.")
    if p.returncode != 0:
        raise ScanError("Text recognition failed: " + (p.stderr.strip().splitlines() or ["?"])[-1])
    print(f"OCR ({lang}, {res} dpi, {kind}): {time.time() - t0:.1f}s", flush=True)


def find_device():
    out = subprocess.run(["scanimage", "-L"], capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"device `(hpaio:[^']+)'", out)
    return m.group(1) if m else None


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ saved scans and jobs

def save(rec):
    """Write a scan's record next to its file (atomically)."""
    path = os.path.join(SCAN_DIR, rec["id"] + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(path + ".tmp", path)


def update(rec, **changes):
    with state_lock:
        rec.update(changes)
        save(rec)


def load_scans():
    """Read the saved records at startup. Jobs cut short by a restart are marked failed."""
    os.makedirs(SCAN_DIR, exist_ok=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR)
    for name in os.listdir(SCAN_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SCAN_DIR, name)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec["state"] not in ("done", "failed"):
            rec.update(state="failed", error="Interrupted: the scan server restarted.")
            save(rec)
        scans[rec["id"]] = rec


def raw_size(rec):
    """Estimated size of the raw scan in bytes."""
    w, h = PAPER[rec["paper"]]
    px = (float(w) / 25.4 * int(rec["res"])) * (float(h) / 25.4 * int(rec["res"]))
    return int(px * {"Color": 3, "Gray": 1, "GrayBW": 1, "Lineart": 1 / 8}[rec["mode"]])


def run_scanimage(rec, cmd, path, timeout):
    """Run scanimage with progress reports (-p) going into the record. Returns (rc, error lines)."""
    for attempt in range(4):
        p = subprocess.Popen(cmd + ["-p", "-o", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        killed = []
        timer = threading.Timer(timeout, lambda: (killed.append(1), p.kill()))
        timer.start()
        errors, buf, last = [], b"", 0
        while True:
            data = p.stderr.read1(4096)
            if not data:
                break
            *lines, buf = re.split(rb"[\r\n]", buf + data)
            for line in lines:
                m = re.match(rb"Progress: ([\d.]+)%", line)
                if m:
                    pct = float(m.group(1))
                    if pct - last >= 1 or pct >= 100:   # don't rewrite the record too often
                        last = pct
                        update(rec, progress=round(pct))
                elif line.strip() and b"musb.c" not in line and b"hpmud" not in line:
                    errors.append(line.decode(errors="replace").strip())
        rc = p.wait()
        timer.cancel()
        if killed:
            raise subprocess.TimeoutExpired(cmd, timeout)
        # The scanner reports "Device busy" for a few seconds after each scan
        if not any("Device busy" in e for e in errors):
            break
        time.sleep(5)
    return rc, errors


def run_job(rec):
    """Scan and convert in the background; the result goes to SCAN_DIR/<rec['file']>."""
    global device
    t0 = time.time()
    mode, res, fmt, paper, lang = rec["mode"], rec["res"], rec["fmt"], rec["paper"], rec["lang"]
    work = []   # temporary files to remove at the end
    try:
        if not device:
            device = find_device()
        if not device:
            raise ScanError("Scanner not found. Is the printer on and connected to the Pi?")
        w, h = PAPER[paper]
        lineart, enhanced = mode == "Lineart", mode == "GrayBW"
        ocr_fmt = fmt in ("ocrpdf", "txt")
        hires = int(res) > 600
        # scanimage's PDF output is malformed (shows blank), its PDF/PNG writers fail for
        # Lineart, and its JPEG/PNG writers above 600 dpi: scan JPEG/PNG for colour/grey up to
        # 600 dpi, otherwise raw PNM, and convert here
        raw = lineart or enhanced or hires
        scan_fmt = "pnm" if raw else ("png" if fmt == "png" else "jpeg")
        scan_path = os.path.join(WORK_DIR, f"{rec['id']}.{scan_fmt}")
        work.append(scan_path)
        cmd = ["scanimage", "-d", device, "--mode", "Gray" if enhanced else mode,
               "--resolution", res, "-x", w, "-y", h, f"--format={scan_fmt}"]
        if not lineart:
            cmd += ["--compression", "JPEG"]   # much faster USB transfer
        update(rec, state="scanning", progress=0)
        try:
            rc, errors = run_scanimage(rec, cmd, scan_path, SCAN_TIMEOUTS.get(res, SCAN_TIMEOUT))
        except subprocess.TimeoutExpired:
            device = None
            raise ScanError("The scan took too long and was stopped.")
        if rc != 0 or not os.path.exists(scan_path) or os.path.getsize(scan_path) == 0:
            device = None   # re-detect next time (printer may have been unplugged)
            raise ScanError((errors or ["Scanning failed (unknown error)."])[-1])

        update(rec, state="processing", step="Reading the text" if ocr_fmt else "Converting",
               progress=None)
        out = os.path.join(WORK_DIR, rec["file"])
        work.append(out)
        t1 = time.time()
        if lineart or enhanced:
            bw, bh, blocks = bw_image(scan_path, BW_THRESHOLD)
            if ocr_fmt:
                src = os.path.join(WORK_DIR, rec["id"] + ".pbm")
                work.append(src)
                with open(src, "wb") as f:
                    f.write(b"P4\n%d %d\n" % (bw, bh))
                    for block in blocks:
                        f.write(block)
            elif fmt == "pdf":
                bw_to_pdf(bw, bh, blocks, out, w, h)
            else:
                write_png(out, bw, bh, 0, 1, (bw + 7) // 8, blocks, invert=True)
        elif hires and fmt == "png":
            f, magic, pw, ph, rowb = open_pnm(scan_path)
            with f:
                write_png(out, pw, ph, 2 if magic == b"P6" else 0, 8, rowb, pnm_blocks(f, rowb, ph))
        else:   # colour/grey as JPEG (or PNG from scanimage up to 600 dpi)
            src = scan_path
            if hires:
                src = os.path.join(WORK_DIR, rec["id"] + ".jpg")
                work.append(src)
                pnm_to_jpeg(scan_path, src)
            if fmt == "pdf":
                jpeg_to_pdf(src, out, w, h)
            elif fmt in ("jpeg", "png"):
                os.replace(src, out)
        if raw:
            print(f"conversion ({mode}, {res} dpi, {fmt}): {time.time() - t1:.1f}s", flush=True)
        if ocr_fmt:
            if src != scan_path:
                remove(scan_path)   # free the SD card early
            ocr(src, os.path.splitext(out)[0], res, lang, fmt)
        final = os.path.join(SCAN_DIR, rec["file"])
        os.replace(out, final)
        update(rec, state="done", size=os.path.getsize(final), seconds=round(time.time() - t0))
    except ScanError as e:
        update(rec, state="failed", error=str(e), seconds=round(time.time() - t0))
    except Exception as e:   # report in the list instead of losing the job
        print(f"scan error: {e!r}", flush=True)
        update(rec, state="failed", error=f"Could not process the scan: {e}",
               seconds=round(time.time() - t0))
    finally:
        for path in work:
            remove(path)
        scan_lock.release()


def start_job(q):
    """Validate the settings and start a job. Returns (HTTP code, record or error message)."""
    mode = q.get("mode", ["Color"])[0]
    res = q.get("res", ["200"])[0]
    fmt = q.get("fmt", ["pdf"])[0]
    paper = q.get("paper", ["a4"])[0]
    lang = q.get("lang", ["eng+ell"])[0]
    if (mode not in MODES or res not in RESOLUTIONS or fmt not in FORMATS or paper not in PAPER
            or lang not in OCR_LANGS):
        return 400, "Invalid scan settings"
    if fmt in ("ocrpdf", "txt") and int(res) > OCR_MAX_RES:
        res = str(OCR_MAX_RES)
    if mode in ("Lineart", "GrayBW") and fmt == "jpeg":
        fmt = "png"   # JPEG is unsuitable for pure black & white
    if not scan_lock.acquire(blocking=False):
        return 409, "The scanner is busy with another scan. Wait for it to finish (see the list below)."
    try:
        sid = time.strftime("%Y%m%d-%H%M%S")
        rec = {"id": sid, "file": f"scan-{sid}.{EXTENSIONS.get(fmt, fmt)}", "created": time.time(),
               "mode": mode, "res": res, "fmt": fmt, "paper": paper,
               "lang": lang if fmt in ("ocrpdf", "txt") else None,
               "state": "starting", "progress": None, "step": None, "size": None, "error": None}
        need = raw_size(rec) * 2 + MIN_FREE   # raw scan + converted file
        free = shutil.disk_usage(STATE_DIR).free
        if free < need:
            scan_lock.release()
            return 507, (f"Not enough space on the Pi for this scan ({need >> 20} MB needed, "
                         f"{free >> 20} MB free). Delete some saved scans first.")
        with state_lock:
            if sid in scans:   # a second scan within the same second
                scan_lock.release()
                return 409, "The scanner is busy. Try again shortly."
            scans[sid] = rec
            save(rec)
        threading.Thread(target=run_job, args=(rec,), daemon=True).start()
        return 202, rec
    except Exception:
        scan_lock.release()
        raise


def drop(rec):
    """Delete a finished scan's file and record (caller holds state_lock)."""
    del scans[rec["id"]]
    remove(os.path.join(SCAN_DIR, rec["file"]))
    remove(os.path.join(SCAN_DIR, rec["id"] + ".json"))


def delete_scan(sid):
    with state_lock:
        rec = scans.get(sid)
        if not rec:
            return 404, "No such scan"
        if rec["state"] not in ("done", "failed"):
            return 409, "This scan is still running."
        drop(rec)
    return 200, "Deleted"


# Clean-up choices: keep the newest N scans, or the scans of the last N days
CLEANUP = {"n10": ("count", 10), "n20": ("count", 20), "n30": ("count", 30), "d30": ("days", 30)}


def cleanup(keep):
    """Delete the finished scans outside the kept set. Returns (count, bytes freed)."""
    kind, n = CLEANUP[keep]
    with state_lock:
        items = sorted(scans.values(), key=lambda r: r["id"], reverse=True)
        if kind == "count":
            old = items[n:]
        else:
            old = [r for r in items if r["created"] < time.time() - n * 86400]
        old = [r for r in old if r["state"] in ("done", "failed")]
        freed = sum(r.get("size") or 0 for r in old)
        for rec in old:
            drop(rec)
    print(f"clean-up ({keep}): deleted {len(old)} scans, {freed >> 20} MB", flush=True)
    return len(old), freed


# ------------------------------------------------------------------ the page

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanner</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5d6675; --accent:#1a73e8; --border:#d9dde3;
        --err:#d93025; --track:#e8eaed; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181d; --card:#20232a; --text:#e8eaed; --muted:#9aa0a6; --accent:#8ab4f8; --border:#3a3f47;
          --err:#f28b82; --track:#3a3f47; }
}
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--text); }
main { max-width: 460px; margin: 40px auto; padding: 0 16px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:16px; }
h1 { font-size: 1.3rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 0 0 12px; display:flex; justify-content:space-between; align-items:baseline; }
h2 small { font-weight:400; color:var(--muted); font-size:.8rem; }
a.home { display:inline-block; margin-bottom: 12px; color:var(--accent); text-decoration:none; font-size:.9rem; }
a.home:hover { text-decoration:underline; }
p.sub { color:var(--muted); margin: 0 0 20px; font-size: .9rem; }
label { display:block; font-weight:600; font-size:.9rem; margin: 14px 0 6px; }
select { width:100%%; padding:10px; border-radius:8px; border:1px solid var(--border);
         background:var(--card); color:var(--text); font-size:1rem; }
select:disabled { opacity:.45; cursor:not-allowed; }
label.off { color:var(--muted); }
button#b { width:100%%; margin-top:22px; padding:14px; font-size:1.05rem; font-weight:600;
           border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
@media (prefers-color-scheme: dark) { button#b { color:#16181d; } }
button#b:disabled { opacity:.75; cursor:wait; }
.spin { display:inline-block; width:1em; height:1em; margin-right:10px; vertical-align:-.15em;
        border:2.5px solid currentColor; border-right-color:transparent; border-radius:50%%;
        animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation-duration: 2.4s; } }
#status { margin-top:16px; color:var(--muted); font-size:.9rem; min-height:1.2em; }
.err { color:var(--err) !important; }
ul { list-style:none; margin:0; padding:0; }
li { padding:12px 0; border-top:1px solid var(--border); }
li:first-child { border-top:0; padding-top:0; }
.row { display:flex; align-items:center; gap:8px; }
.name { flex:1; min-width:0; overflow-wrap:anywhere; font-weight:600; font-size:.95rem; color:var(--text); }
a.name { color:var(--accent); text-decoration:none; }
a.name:hover { text-decoration:underline; }
.meta { color:var(--muted); font-size:.82rem; margin-top:3px; }
.del { border:1px solid var(--border); background:transparent; color:var(--muted); border-radius:6px;
       padding:4px 10px; font-size:.82rem; cursor:pointer; }
.del:hover { color:var(--err); border-color:var(--err); }
.bar { height:6px; background:var(--track); border-radius:3px; margin-top:8px; overflow:hidden; }
.bar i { display:block; height:100%%; background:var(--accent); transition: width .5s; }
.bar.indet i { width:30%%; animation: slide 1.4s ease-in-out infinite; }
@keyframes slide { from { margin-left:-30%%; } to { margin-left:100%%; } }
#empty { color:var(--muted); font-size:.9rem; margin:0; }
[hidden] { display:none !important; }
.confirm { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin-top:8px; font-size:.85rem; }
.confirm span { flex:1; min-width:10em; }
.del.danger { background:var(--err); border-color:var(--err); color:#fff; }
.del.danger:hover { color:#fff; }
@media (prefers-color-scheme: dark) { .del.danger, .del.danger:hover { color:#16181d; } }
.cleanup { display:flex; gap:8px; align-items:center; margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }
.cleanup select { flex:1; width:auto; padding:6px 8px; font-size:.85rem; }
#cleanmsg { color:var(--muted); font-size:.85rem; }
#cleanmsg:not(:empty) { margin-top:8px; }
</style></head><body><main><div class="card">
<a class="home" id="home" href="/">&larr; Print server home</a>
<h1>HP DeskJet 1515 scanner</h1>
<p class="sub">Place the page face-down on the glass, top-left corner.</p>
<form id="f" action="/scan" method="post">
<label for="mode">Colour</label>
<select id="mode" name="mode">%(modes)s</select>
<label for="res">Resolution</label>
<select id="res" name="res">%(res)s</select>
<label for="paper">Paper size</label>
<select id="paper" name="paper">
<option value="a4" selected>A4</option><option value="letter">Letter</option><option value="full">Whole glass</option>
</select>
<label for="fmt">File type</label>
<select id="fmt" name="fmt">
<option value="pdf" selected>PDF</option><option value="jpeg">JPEG</option><option value="png">PNG</option>
<option value="ocrpdf">PDF with searchable text (OCR)</option><option value="txt">Text only (OCR)</option>
</select>
<label for="lang" id="langlabel">Text language (OCR)</label>
<select id="lang" name="lang">%(langs)s</select>
<button id="b" type="submit">Scan</button>
<div id="status"></div>
</form></div>
<div class="card">
<h2>Saved scans <small id="free"></small></h2>
<ul id="list"></ul>
<p id="empty" hidden>No saved scans yet.</p>
<div id="cleanup" class="cleanup" hidden>
<select id="keep" aria-label="Which scans to keep">%(keep)s</select>
<button type="button" id="clean" class="del">Clean up…</button>
</div>
<div id="cleanmsg"></div>
</div></main>
<script>
const f = document.getElementById('f'), b = document.getElementById('b'), s = document.getElementById('status');
const FIELDS = ['mode', 'res', 'paper', 'fmt', 'lang'];
const isOcr = () => ['ocrpdf', 'txt'].includes(f.elements.fmt.value);
const LABEL = {Color: 'Colour', Gray: 'Greyscale', Lineart: 'B&W', GrayBW: 'B&W enhanced',
               a4: 'A4', letter: 'Letter', full: 'Whole glass',
               pdf: 'PDF', jpeg: 'JPEG', png: 'PNG', ocrpdf: 'Searchable PDF', txt: 'Text',
               'eng+ell': 'English + Greek', eng: 'English', ell: 'Greek'};

// The welcome page is on port 80 of this same host
document.getElementById('home').href = location.protocol + '//' + location.host.replace(/:\\d+$/, '') + '/';

// Remember selections in a cookie for a year
function savePrefs() {
  const v = new URLSearchParams();
  FIELDS.forEach(n => v.set(n, f.elements[n].value));
  document.cookie = 'scanprefs=' + encodeURIComponent(v.toString()) +
                    '; max-age=31536000; path=/; SameSite=Lax';
}
function loadPrefs() {
  const m = document.cookie.match(/(?:^|; )scanprefs=([^;]*)/);
  if (!m) return;
  const v = new URLSearchParams(decodeURIComponent(m[1]));
  FIELDS.forEach(n => {
    const sel = f.elements[n], val = v.get(n);
    // only restore values that are still offered
    if (val && [...sel.options].some(o => o.value === val)) sel.value = val;
  });
}
// Grey out what doesn't apply: the language without OCR, and with OCR the resolutions above
// OCR_MAX_RES (the server caps them). A resolution changed for OCR comes back without it.
const OCR_MAX_RES = %(ocr_max_res)s;
let resBeforeOcr = null;
function updateOcr() {
  const ocr = isOcr(), res = f.elements.res;
  f.elements.lang.disabled = !ocr;
  document.getElementById('langlabel').classList.toggle('off', !ocr);
  for (const o of res.options) o.disabled = ocr && +o.value > OCR_MAX_RES;
  if (ocr && +res.value > OCR_MAX_RES) {
    resBeforeOcr = res.value;
    res.value = String(OCR_MAX_RES);
  } else if (!ocr && resBeforeOcr) {
    res.value = resBeforeOcr;
    resBeforeOcr = null;
  }
}
loadPrefs();
updateOcr();
f.addEventListener('change', (e) => {
  if (e.target.name === 'res') resBeforeOcr = null;
  updateOcr();
  savePrefs();
});

// ---- saved scans: the server keeps them, this page polls the list
const el = (tag, cls, text) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const size = n => n >= 1e9 ? (n / 1e9).toFixed(2) + ' GB' : n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB'
                                                                     : Math.max(1, Math.round(n / 1e3)) + ' kB';
const clock = t => Math.floor(t / 60) + ':' + String(Math.floor(t %% 60)).padStart(2, '0');
const running = r => !['done', 'failed'].includes(r.state);
const fileUrl = r => '/scans/' + encodeURIComponent(r.file);
let mine = null, active = null, fetchedAt = 0, poll = null, lastData = null;
let confirmDel = null, confirmClean = false;   // open confirmations survive the re-render on each poll
const KEEP = %(keep_rules)s;
const keepSel = document.getElementById('keep'), cleanMsg = document.getElementById('cleanmsg');

// An in-page "Are you sure?" row (works the same on every browser and phone)
function confirmBox(text, onYes, onNo) {
  const box = el('div', 'confirm'), yes = el('button', 'del danger', 'Yes, delete'), no = el('button', 'del', 'Cancel');
  yes.type = no.type = 'button';
  yes.onclick = onYes; no.onclick = onNo;
  box.append(el('span', null, text), yes, no);
  return box;
}

function stepText(r) {
  if (r.state === 'starting') return 'Starting…';
  if (r.state === 'scanning') return 'Scanning… ' + (r.progress || 0) + '%%';
  return (r.step || 'Converting') + '…';
}
function render(data) {
  const list = document.getElementById('list');
  list.replaceChildren();
  document.getElementById('empty').hidden = data.scans.length > 0;
  document.getElementById('free').textContent = size(data.free) + ' free';
  for (const r of data.scans) {
    const li = el('li'), row = el('div', 'row');
    if (r.state === 'done') {
      const a = el('a', 'name', r.file); a.href = fileUrl(r); a.download = r.file; row.append(a);
    } else row.append(el('span', 'name', r.file));
    if (!running(r) && confirmDel !== r.id) {
      const d = el('button', 'del', 'Delete'); d.type = 'button';
      d.onclick = () => { confirmDel = r.id; render(lastData); };
      row.append(d);
    }
    li.append(row);
    const parts = [LABEL[r.mode], r.res + ' dpi', LABEL[r.paper], LABEL[r.fmt]];
    if (r.lang) parts.push(LABEL[r.lang]);
    if (r.size != null) parts.push(size(r.size));
    parts.push(new Date(r.created * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}));
    li.append(el('div', 'meta', parts.join(' · ')));
    if (running(r)) {
      const bar = el('div', 'bar'), i = el('i');
      if (r.state === 'scanning') i.style.width = (r.progress || 0) + '%%'; else bar.classList.add('indet');
      bar.append(i); li.append(bar, el('div', 'meta', stepText(r)));
    } else if (r.state === 'failed') li.append(el('div', 'meta err', r.error));
    if (confirmDel === r.id) {
      li.append(confirmBox('Delete this scan?', async () => {
        confirmDel = null;
        await fetch('/scans/' + encodeURIComponent(r.id), {method: 'DELETE'});
        refresh();
      }, () => { confirmDel = null; render(lastData); }));
    }
    list.append(li);
  }
  document.getElementById('cleanup').hidden = data.scans.length === 0;
  if (confirmClean) renderClean();
}

// ---- clean-up: delete everything except the newest N scans / the last N days
function toClean() {
  const [kind, n] = KEEP[keepSel.value], items = lastData ? lastData.scans : [];   // newest first
  const old = kind === 'count' ? items.slice(n) : items.filter(r => r.created < Date.now() / 1000 - n * 86400);
  return old.filter(r => !running(r));
}
function renderClean() {
  cleanMsg.replaceChildren();
  if (!confirmClean) return;
  const old = toClean();
  if (!old.length) {
    confirmClean = false;
    cleanMsg.textContent = 'Nothing to delete: all saved scans are kept.';
    return;
  }
  const bytes = old.reduce((a, r) => a + (r.size || 0), 0);
  cleanMsg.append(confirmBox('Delete ' + old.length + ' older scan' + (old.length > 1 ? 's' : '') +
                             (bytes ? ' (' + size(bytes) + ')' : '') + '?', async () => {
    confirmClean = false;
    cleanMsg.textContent = 'Deleting…';
    try {
      const r = await fetch('/scans/cleanup', {method: 'POST', body: new URLSearchParams({keep: keepSel.value})});
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      cleanMsg.textContent = 'Deleted ' + d.deleted + ' scan' + (d.deleted === 1 ? '' : 's') +
                             (d.freed ? ', freed ' + size(d.freed) : '') + '.';
    } catch (err) { cleanMsg.textContent = 'Clean-up failed: ' + err.message; }
    refresh();
  }, () => { confirmClean = false; renderClean(); }));
}
document.getElementById('clean').onclick = () => { confirmClean = true; renderClean(); };
keepSel.onchange = () => { if (confirmClean) renderClean(); else cleanMsg.textContent = ''; };
function tick() {   // the Scan button while any scan (from any device) is running
  if (!active) { b.disabled = false; b.textContent = 'Scan'; return; }
  const t = active.elapsed + (Date.now() - fetchedAt) / 1000;
  b.disabled = true;
  b.innerHTML = '<span class="spin"></span>';
  b.append(stepText(active) + ' ' + clock(t));
}
async function refresh() {
  clearTimeout(poll);
  try {
    const r = await fetch('/scans', {cache: 'no-store'});
    const data = await r.json();
    fetchedAt = Date.now();
    lastData = data;
    render(data);
    active = data.scans.find(running) || null;
    const m = mine && data.scans.find(x => x.id === mine);
    if (m && m.state === 'done') {
      mine = null;
      const a = el('a'); a.href = fileUrl(m); a.download = m.file; a.click();
      s.className = ''; s.textContent = 'Done: ' + m.file + ' (' + clock(m.seconds) + ')';
    } else if (m && m.state === 'failed') {
      mine = null; s.className = 'err'; s.textContent = 'Scan failed: ' + m.error;
    }
  } catch (err) { /* network hiccup: try again on the next poll */ }
  tick();
  poll = setTimeout(refresh, active ? 2000 : 15000);
}
setInterval(() => { if (active) tick(); }, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  b.disabled = true; s.className = ''; s.textContent = 'Starting…';
  try {
    const r = await fetch('/scan', {method: 'POST', body: new URLSearchParams(new FormData(f))});
    const t = await r.text();
    if (!r.ok) throw new Error(t);
    mine = JSON.parse(t).id;
    s.textContent = 'The scan is saved on the Pi when done (see below), so you can close this page.';
  } catch (err) {
    s.className = 'err'; s.textContent = 'Scan failed: ' + err.message;
  }
  refresh();
});
refresh();
</script></body></html>"""


def render_page():
    modes = "".join(f'<option value="{k}"{" selected" if k == "Color" else ""}>{v}</option>'
                    for k, v in MODES.items())
    res = "".join(f'<option value="{k}"{" selected" if k == "200" else ""}>{v}</option>'
                  for k, v in RESOLUTIONS.items())
    langs = "".join(f'<option value="{k}">{v}</option>' for k, v in OCR_LANGS.items())
    keep = "".join(f'<option value="{k}">Keep the {"newest " + str(n) if kind == "count" else f"last {n} days"}'
                   f'</option>' for k, (kind, n) in CLEANUP.items())
    return PAGE % {"modes": modes, "res": res, "langs": langs, "ocr_max_res": OCR_MAX_RES,
                   "keep": keep, "keep_rules": json.dumps(CLEANUP)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.path.startswith("/scans") and self.command == "GET" and args[1:2] == ("200",):
            return   # the page polls this every few seconds
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def send_text(self, code, text, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_text(code, json.dumps(obj), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self.send_text(200, render_page(), "text/html; charset=utf-8")
        if url.path == "/scans":
            now = time.time()
            with state_lock:
                items = sorted(scans.values(), key=lambda r: r["id"], reverse=True)
                items = [dict(r, elapsed=round(now - r["created"])) for r in items]
            return self.send_json(200, {"scans": items, "free": shutil.disk_usage(STATE_DIR).free})
        if url.path.startswith("/scans/"):
            return self.download(unquote(url.path[len("/scans/"):]))
        self.send_text(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/scan", "/scans/cleanup"):
            return self.send_text(404, "Not found")
        n = int(self.headers.get("Content-Length") or 0)
        q = parse_qs(self.rfile.read(min(n, 4096)).decode(errors="replace"))
        if path == "/scans/cleanup":
            keep = q.get("keep", [""])[0]
            if keep not in CLEANUP:
                return self.send_text(400, "Invalid clean-up choice")
            count, freed = cleanup(keep)
            return self.send_json(200, {"deleted": count, "freed": freed})
        code, result = start_job(q)
        if code == 202:
            return self.send_json(202, result)
        self.send_text(code, result)

    def do_DELETE(self):
        url = urlparse(self.path)
        if not url.path.startswith("/scans/"):
            return self.send_text(404, "Not found")
        self.send_text(*delete_scan(unquote(url.path[len("/scans/"):])))

    def download(self, name):
        with state_lock:
            rec = next((r for r in scans.values() if r["file"] == name and r["state"] == "done"), None)
        path = rec and os.path.join(SCAN_DIR, rec["file"])
        if not rec or not os.path.exists(path):
            return self.send_text(404, "No such scan")
        self.send_response(200)
        self.send_header("Content-Type", FORMATS[rec["fmt"]])
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition", f"attachment; filename=\"{rec['file']}\"; "
                                                f"filename*=UTF-8''{quote(rec['file'])}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the browser went away; the file stays in the list


if __name__ == "__main__":
    load_scans()
    print(f"Scan server on port {PORT}, saving scans in {SCAN_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
