#!/usr/bin/env python3
"""Tiny web front-end for scanning on the HP DeskJet 1515 via SANE (scanimage)."""
import os, re, struct, subprocess, tempfile, threading, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8080
# Lineart only works as raw PNM output from scanimage (its PDF/PNG writers fail in that mode),
# so for Lineart we scan PNM and build the PDF/PNG ourselves.
MODES = {"Color": "Colour", "Gray": "Greyscale", "Lineart": "Black & white (smallest files)",
         "GrayBW": "Black & white – enhanced (keeps light text)"}
# GrayBW: scan in greyscale and threshold here. The scanner's own Lineart cut-off drops
# light-grey text; anything darker than this (paper scans as 255) becomes black.
BW_THRESHOLD = 200
RESOLUTIONS = ["75", "100", "200", "300", "600"]   # 1200+ would exhaust the Pi's RAM
FORMATS = {"pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png"}
PAPER = {"a4": ("210", "297"), "letter": ("215.9", "279.4"), "full": ("215.9", "297.011")}
SCAN_TIMEOUT = 600

scan_lock = threading.Lock()
device = None


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


def gray_to_bits(data, threshold):
    """Threshold a P5 greyscale PNM to packed 1-bit rows (bit 1 = black).
    Uses translate + int(..., 2) so the per-pixel work runs at C speed on the Pi."""
    (w, h, _), off = read_pnm_header(data, b"P5", 3)
    # hpaio sometimes delivers fewer rows than the header announces (seen at 600 dpi)
    got = (len(data) - off) // w
    if got < h:
        print(f"greyscale scan short: header {h} rows, received {got}", flush=True)
        h = got
    lut = bytes(0x31 if v < threshold else 0x30 for v in range(256))   # b'1' / b'0'
    s = data[off:off + w * h].translate(lut)
    rowb = (w + 7) // 8
    pad = b"0" * (rowb * 8 - w)
    bits = b"".join(int(s[r * w:(r + 1) * w] + pad, 2).to_bytes(rowb, "big") for r in range(h))
    return w, h, bits


def read_pbm(data):
    """Parse a binary PBM (P4): return (width, height, packed rows; bit 1 = black)."""
    (w, h), off = read_pnm_header(data, b"P4", 2)
    rowb = (w + 7) // 8
    h = min(h, (len(data) - off) // rowb)   # tolerate short scans, as in gray_to_bits
    return w, h, data[off:off + rowb * h]


def pbm_to_png(w, h, bits):
    """Encode packed 1-bit rows as a 1-bit greyscale PNG (PNG uses 1 = white, so invert)."""
    rowb = (w + 7) // 8
    inv = bits.translate(bytes(255 - b for b in range(256)))
    raw = b"".join(b"\x00" + inv[r * rowb:(r + 1) * rowb] for r in range(h))
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def jpeg_to_pdf(jpeg, page_w_mm, page_h_mm):
    """Wrap a JPEG in a single-page PDF (scanimage's own PDF writer leaves lengths/xref as 0)."""
    w, h, comps = jpeg_info(jpeg)
    cs = "/DeviceGray" if comps == 1 else "/DeviceRGB"
    return image_pdf(f"/Width {w} /Height {h} /ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode",
                     jpeg, page_w_mm, page_h_mm)


def pbm_to_pdf(w, h, bits, page_w_mm, page_h_mm):
    """Single-page PDF holding a 1-bit image; /Decode [1 0] because PBM uses 1 = black."""
    return image_pdf(f"/Width {w} /Height {h} /ColorSpace /DeviceGray /BitsPerComponent 1 "
                     f"/Decode [1 0] /Filter /FlateDecode", zlib.compress(bits, 6), page_w_mm, page_h_mm)


def image_pdf(image_dict, stream, page_w_mm, page_h_mm):
    """Build a one-page PDF that stretches a single image over the whole page."""
    pw, ph = float(page_w_mm) * 72 / 25.4, float(page_h_mm) * 72 / 25.4
    content = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im1 Do Q".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] "
        f"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>".encode(),
        f"<< /Type /XObject /Subtype /Image {image_dict} /Length {len(stream)} >>\nstream\n".encode()
        + stream + b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def find_device():
    out = subprocess.run(["scanimage", "-L"], capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"device `(hpaio:[^']+)'", out)
    return m.group(1) if m else None


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanner</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5d6675; --accent:#1a73e8; --border:#d9dde3; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181d; --card:#20232a; --text:#e8eaed; --muted:#9aa0a6; --accent:#8ab4f8; --border:#3a3f47; }
}
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--text); }
main { max-width: 420px; margin: 40px auto; padding: 0 16px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:24px; }
h1 { font-size: 1.3rem; margin: 0 0 4px; }
p.sub { color:var(--muted); margin: 0 0 20px; font-size: .9rem; }
label { display:block; font-weight:600; font-size:.9rem; margin: 14px 0 6px; }
select { width:100%%; padding:10px; border-radius:8px; border:1px solid var(--border);
         background:var(--card); color:var(--text); font-size:1rem; }
button { width:100%%; margin-top:22px; padding:14px; font-size:1.05rem; font-weight:600;
         border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
@media (prefers-color-scheme: dark) { button { color:#16181d; } }
button:disabled { opacity:.6; cursor:wait; }
#status { margin-top:16px; color:var(--muted); font-size:.9rem; min-height:1.2em; }
.err { color:#d93025 !important; }
</style></head><body><main><div class="card">
<h1>HP DeskJet 1515 scanner</h1>
<p class="sub">Place the page face-down on the glass, top-left corner.</p>
<form id="f" action="/scan" method="get">
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
</select>
<button id="b" type="submit">Scan</button>
<div id="status">%(status)s</div>
</form></div></main>
<script>
const f = document.getElementById('f'), b = document.getElementById('b'), s = document.getElementById('status');
const FIELDS = ['mode', 'res', 'paper', 'fmt'];

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
loadPrefs();
f.addEventListener('change', savePrefs);

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  b.disabled = true; s.className = ''; s.textContent = 'Scanning… this can take up to a minute.';
  const t0 = Date.now();
  try {
    const r = await fetch('/scan?' + new URLSearchParams(new FormData(f)));
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    const name = (cd.match(/filename="([^"]+)"/) || [, 'scan'])[1];
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    s.textContent = 'Done: ' + name + ' (' + Math.round((Date.now() - t0) / 1000) + ' s)';
  } catch (err) {
    s.className = 'err'; s.textContent = 'Scan failed: ' + err.message;
  } finally { b.disabled = false; }
});
</script></body></html>"""


def render_page(status=""):
    modes = "".join(f'<option value="{k}"{" selected" if k == "Color" else ""}>{v}</option>'
                    for k, v in MODES.items())
    res = "".join(f'<option value="{r}"{" selected" if r == "200" else ""}>{r} dpi</option>'
                  for r in RESOLUTIONS)
    return PAGE % {"modes": modes, "res": res, "status": status}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def send_text(self, code, text, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self.send_text(200, render_page(), "text/html; charset=utf-8")
        if url.path == "/scan":
            return self.scan(parse_qs(url.query))
        self.send_text(404, "Not found")

    def scan(self, q):
        global device
        mode = q.get("mode", ["Color"])[0]
        res = q.get("res", ["200"])[0]
        fmt = q.get("fmt", ["pdf"])[0]
        paper = q.get("paper", ["a4"])[0]
        if mode not in MODES or res not in RESOLUTIONS or fmt not in FORMATS or paper not in PAPER:
            return self.send_text(400, "Invalid scan settings")
        if not scan_lock.acquire(blocking=False):
            return self.send_text(409, "The scanner is busy with another scan. Try again shortly.")
        try:
            if not device:
                device = find_device()
            if not device:
                return self.send_text(503, "Scanner not found. Is the printer on and connected to the Pi?")
            w, h = PAPER[paper]
            lineart = mode == "Lineart"
            enhanced = mode == "GrayBW"
            if (lineart or enhanced) and fmt == "jpeg":
                fmt = "png"   # JPEG is unsuitable for pure black & white
            # scanimage's PDF output is malformed (shows blank), and its PDF/PNG writers fail
            # for Lineart: scan JPEG (colour/grey) or raw PNM (both B&W modes) and convert here
            scan_fmt = "pnm" if (lineart or enhanced) else ("jpeg" if fmt == "pdf" else fmt)
            cmd = ["scanimage", "-d", device, "--mode", "Gray" if enhanced else mode,
                   "--resolution", res, "-x", w, "-y", h, f"--format={scan_fmt}"]
            if not lineart:
                cmd += ["--compression", "JPEG"]   # much faster USB transfer
            with tempfile.NamedTemporaryFile(suffix="." + scan_fmt, delete=True) as out:
                for attempt in range(4):
                    p = subprocess.run(cmd + ["-o", out.name], capture_output=True, text=True,
                                       timeout=SCAN_TIMEOUT)
                    # The scanner reports "Device busy" for a few seconds after each scan
                    if "Device busy" not in p.stderr:
                        break
                    time.sleep(5)
                size = os.path.getsize(out.name)
                if p.returncode != 0 or size == 0:
                    device = None   # re-detect next time (printer may have been unplugged)
                    msg = (p.stderr.strip().splitlines() or ["unknown error"])[-1]
                    return self.send_text(500, msg)
                out.seek(0)
                data = out.read()
                if lineart or enhanced:
                    t0 = time.time()
                    bw, bh, bits = gray_to_bits(data, BW_THRESHOLD) if enhanced else read_pbm(data)
                    del data   # free the raw scan before encoding (matters at 600 dpi)
                    data = pbm_to_pdf(bw, bh, bits, w, h) if fmt == "pdf" else pbm_to_png(bw, bh, bits)
                    print(f"B&W conversion ({mode}, {res} dpi): {time.time() - t0:.1f}s", flush=True)
                elif fmt == "pdf":
                    data = jpeg_to_pdf(data, w, h)
                name = time.strftime("scan-%Y%m%d-%H%M%S.") + ("jpg" if fmt == "jpeg" else fmt)
                self.send_response(200)
                self.send_header("Content-Type", FORMATS[fmt])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                self.wfile.write(data)
        except subprocess.TimeoutExpired:
            device = None
            self.send_text(504, "The scan took too long and was stopped.")
        except Exception as e:   # report instead of dropping the connection
            print(f"scan error: {e!r}", flush=True)
            self.send_text(500, f"Could not process the scan: {e}")
        finally:
            scan_lock.release()


if __name__ == "__main__":
    print(f"Scan server on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
