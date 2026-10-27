#!/usr/bin/env python3
"""Welcome page for the Raspberry Pi print server: printer status, links and hardware health.
Read-only: it queries CUPS (lpstat), vcgencmd, /proc and /sys, and never changes anything.
Also announces itself on the LAN via SSDP/UPnP (Windows File Explorer > Network) and shows
QR codes plus a printable info sheet (/sheet) so phones can find the pages."""
import collections, html, json, os, platform, re, socket, subprocess, threading, time, uuid
from email.utils import formatdate
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Replaced with the release version when packaged (scripts/release.sh) or installed from a
# git checkout (scripts/07-install-welcome.sh)
VERSION = "dev"
PORT = int(os.environ.get("WELCOME_PORT", "80"))
QUEUE = os.environ.get("PRINTER_QUEUE", "DeskJet_1515")
PRINTER_NAME = "HP DeskJet 1515"
HP_USB_VENDOR = "03f0"
SCANNER_PORT = 8080
SAMPLE_EVERY = 5          # seconds between background samples
HISTORY = 60              # samples kept (60 x 5 s = 5 minutes)
HW_CACHE_SECONDS = 10     # vcgencmd results are cached this long

# CUPS printer-state-reasons -> plain language (suffixes -error/-warning/-report stripped)
REASONS = {
    "media-empty": "Out of paper", "media-needed": "Load paper", "media-jam": "Paper jam",
    "marker-supply-low": "Ink low", "marker-supply-empty": "Out of ink",
    "toner-low": "Ink low", "toner-empty": "Out of ink",
    "door-open": "Cover open", "cover-open": "Cover open", "input-tray-missing": "Tray missing",
    "offline": "Printer offline", "connecting-to-device": "Connecting to printer",
    "paused": "Paused", "shutdown": "Shut down", "other": "Printer needs attention",
}

# vcgencmd get_throttled bits
THROTTLE_BITS = {
    0: "Under-voltage", 1: "CPU speed capped", 2: "Throttled", 3: "Soft temperature limit",
}


def run(*cmd, timeout=10):
    env = dict(os.environ, LC_ALL="C")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip().rstrip("\x00")
    except OSError:
        return default


# ---------------------------------------------------------------- printer

# Built-in Pi 1 B+/2/3 chips that don't report a product name over USB
KNOWN_USB = {
    "0424:9514": ("SMSC", "LAN9514 USB hub (built-in)"),
    "0424:ec00": ("SMSC", "LAN9514 Ethernet (built-in)"),
    "0424:9512": ("SMSC", "LAN9512 USB hub (built-in)"),
}


def usb_devices():
    """USB devices from sysfs (vendor id, product name), skipping root hubs."""
    base, devs = "/sys/bus/usb/devices", []
    for dev in sorted(os.listdir(base)):
        vid = read(f"{base}/{dev}/idVendor")
        if not vid or vid == "1d6b":   # 1d6b = Linux Foundation root hub
            continue
        pid = read(f"{base}/{dev}/idProduct")
        known_maker, known_name = KNOWN_USB.get(f"{vid}:{pid}", ("", ""))
        name = read(f"{base}/{dev}/product") or known_name or f"{vid}:{pid}"
        maker = read(f"{base}/{dev}/manufacturer") or known_maker
        devs.append({"vendor": vid, "name": name, "maker": maker,
                     "speed_mbps": read(f"{base}/{dev}/speed")})
    return devs


def printer_status(usb):
    p = run("lpstat", "-p", QUEUE)
    if not p.strip():
        return {"state": "missing", "label": "Not set up", "detail": f"No print queue named {QUEUE}.",
                "jobs": []}
    accepting = "not accepting" not in run("lpstat", "-a", QUEUE)
    m = re.search(r"printer-state-reasons=(\S+)", run("lpoptions", "-p", QUEUE))
    reasons = []
    for r in (m.group(1).split(",") if m else []):
        text = REASONS.get(re.sub(r"-(error|warning|report)$", "", r))
        if text and text not in reasons:
            reasons.append(text)

    jobs = []
    for line in run("lpstat", "-o", QUEUE).splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3:
            jobs.append({"id": parts[0].rsplit("-", 1)[-1], "user": parts[1],
                         "size_kb": max(1, int(parts[2]) // 1024) if parts[2].isdigit() else None})
    # CUPS can't tell an unplugged/switched-off USB printer from an idle one; sysfs can
    connected = any(d["vendor"] == HP_USB_VENDOR for d in usb)
    printing = re.search(r"now printing (\S+?)\.?\s", p)

    if "disabled" in p.split("\n")[0]:
        state, label = "stopped", "Stopped"
        detail = "The print queue is paused. Resume it in the print queue settings."
    elif not connected:
        state, label = "offline", "Turned off or not connected"
        detail = "Turn the printer on. Documents you print now will wait and print automatically."
    elif printing:
        state, label = "printing", "Printing"
        detail = f"Printing job {printing.group(1).rsplit('-', 1)[-1]}."
    elif reasons:
        state, label, detail = "attention", reasons[0], ""
    else:
        state, label, detail = "ready", "Ready", "Connected and ready to print."
    if not accepting:
        detail = (detail + " The queue is not accepting new documents.").strip()
    return {"state": state, "label": label, "detail": detail, "reasons": reasons,
            "connected": connected, "accepting": accepting, "jobs": jobs}


# ---------------------------------------------------------------- hardware

class Sampler(threading.Thread):
    """Samples CPU usage, temperature and clock every few seconds (cheap /proc and /sys reads)."""
    def __init__(self):
        super().__init__(daemon=True)
        self.cpu = collections.deque(maxlen=HISTORY)
        self.temp = collections.deque(maxlen=HISTORY)
        self.clock = collections.deque(maxlen=HISTORY)
        self.last_undervolt = None   # epoch seconds when under-voltage was last seen live
        self._prev = None

    @staticmethod
    def _cpu_times():
        f = read("/proc/stat").split("\n")[0].split()[1:]
        v = [int(x) for x in f]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v), idle

    def run(self):
        while True:
            try:
                total, idle = self._cpu_times()
                if self._prev:
                    dt, di = total - self._prev[0], idle - self._prev[1]
                    self.cpu.append(round(100 * (dt - di) / dt, 1) if dt else 0.0)
                self._prev = (total, idle)
                t = read("/sys/class/thermal/thermal_zone0/temp")
                if t:
                    self.temp.append(round(int(t) / 1000, 1))
                f = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
                if f:
                    self.clock.append(int(f) // 1000)
                # in0_lcrit_alarm = 1 while the supply is below the under-voltage threshold
                for hw in os.listdir("/sys/class/hwmon"):
                    if read(f"/sys/class/hwmon/{hw}/in0_lcrit_alarm") == "1":
                        self.last_undervolt = time.time()
            except Exception as e:   # never let the sampler die
                print(f"sampler: {e!r}", flush=True)
            time.sleep(SAMPLE_EVERY)


sampler = Sampler()
_hw_cache = {"t": 0, "data": {}}
_hw_lock = threading.Lock()


def vcgencmd(*args):
    return run("vcgencmd", *args, timeout=5).strip()


def firmware_stats():
    """Voltage, clocks, throttling and memory split from the VideoCore firmware (cached)."""
    with _hw_lock:
        if time.time() - _hw_cache["t"] < HW_CACHE_SECONDS:
            return _hw_cache["data"]
        d = {}
        for key, rail in (("volt_core", "core"), ("volt_sdram_c", "sdram_c"),
                          ("volt_sdram_i", "sdram_i"), ("volt_sdram_p", "sdram_p")):
            m = re.search(r"volt=([\d.]+)V", vcgencmd("measure_volts", rail))
            if m:
                d[key] = float(m.group(1))
        for key, clk in (("clock_arm_mhz", "arm"), ("clock_core_mhz", "core"), ("clock_sdram_mhz", "sdram")):
            m = re.search(r"=(\d+)", vcgencmd("measure_clock", clk))
            if m and int(m.group(1)):
                d[key] = round(int(m.group(1)) / 1e6)
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", vcgencmd("get_throttled"))
        if m:
            bits = int(m.group(1), 16)
            d["throttled_raw"] = m.group(1)
            d["throttle_now"] = [n for b, n in THROTTLE_BITS.items() if bits & (1 << b)]
            d["throttle_since_boot"] = [n for b, n in THROTTLE_BITS.items() if bits & (1 << (b + 16))]
        for key, part in (("mem_arm_mb", "arm"), ("mem_gpu_mb", "gpu")):
            m = re.search(r"=(\d+)M", vcgencmd("get_mem", part))
            if m:
                d[key] = int(m.group(1))
        _hw_cache.update(t=time.time(), data=d)
        return d


_static = {}


def static_info():
    """Things that don't change while running (computed once)."""
    if _static:
        return _static
    cpuinfo = read("/proc/cpuinfo")
    def ci(key):
        m = re.search(rf"^{key}\s*:\s*(.+)$", cpuinfo, re.M)
        return m.group(1).strip() if m else ""
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', read("/etc/os-release"), re.M))
    fw = vcgencmd("version").splitlines()
    sd_sectors = read("/sys/block/mmcblk0/size")
    # Boot-time clock/voltage settings; the firmware reports 0 for "not set" (= stock)
    cfg = {}
    for key in ("arm_freq", "core_freq", "sdram_freq", "over_voltage", "force_turbo"):
        m = re.search(rf"{key}=(-?\d+)", vcgencmd("get_config", key))
        if m:
            cfg[key] = int(m.group(1))
    _static.update({
        "config": cfg,
        "model": read("/proc/device-tree/model") or platform.machine(),
        "revision": ci("Revision"),
        "cpu": ci("model name") or ci("Processor") or platform.processor(),
        "soc": ci("Hardware") or "BCM2835",
        "cores": os.cpu_count(),
        "cpu_max_mhz": int(read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "0")) // 1000 or None,
        "cpu_min_mhz": int(read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq", "0")) // 1000 or None,
        "governor": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "os": osr.get("PRETTY_NAME", ""),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "firmware": fw[0] if fw else "",
        "sd_name": read("/sys/block/mmcblk0/device/name"),
        "sd_size_gb": round(int(sd_sectors) * 512 / 1e9, 1) if sd_sectors.isdigit() else None,
    })
    return _static


def wifi_stats():
    for line in read("/proc/net/wireless").splitlines()[2:]:
        f = line.split()
        try:
            return {"iface": f[0].rstrip(":"),
                    "quality_pct": min(100, round(float(f[2].rstrip(".")) * 100 / 70)),
                    "signal_dbm": int(float(f[3].rstrip(".")))}
        except (ValueError, IndexError):
            pass
    return None


def pi_status():
    mem = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) // 1024
    st = os.statvfs("/")
    disk_total = st.f_blocks * st.f_frsize
    disk_free = st.f_bavail * st.f_frsize
    load = read("/proc/loadavg").split()
    procs = sum(1 for p in os.listdir("/proc") if p.isdigit())
    ips = run("hostname", "-I").split()
    return {
        "hostname": socket.gethostname(),
        "ip": ips[0] if ips else "",
        "uptime_s": int(float(read("/proc/uptime", "0").split()[0])),
        "load": [float(x) for x in load[:3]] if len(load) >= 3 else [],
        "procs": procs,
        "cpu_pct": sampler.cpu[-1] if sampler.cpu else None,
        "cpu_history": list(sampler.cpu),
        "temp_c": sampler.temp[-1] if sampler.temp else None,
        "temp_history": list(sampler.temp),
        "clock_mhz_now": sampler.clock[-1] if sampler.clock else None,
        "last_undervolt_ago_s": int(time.time() - sampler.last_undervolt) if sampler.last_undervolt else None,
        "mem_total_mb": mem.get("MemTotal"), "mem_avail_mb": mem.get("MemAvailable"),
        "swap_total_mb": mem.get("SwapTotal"), "swap_used_mb": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
        "disk_total_gb": round(disk_total / 1e9, 1), "disk_free_gb": round(disk_free / 1e9, 1),
        "wifi": wifi_stats(),
        "fw": firmware_stats(),
        "static": static_info(),
    }


# ---------------------------------------------------------------- QR code
# Minimal QR encoder (ISO/IEC 18004): byte mode, error correction level M, versions 1-10
# (up to 213 bytes). Hand-written because the page uses only the standard library.

# version -> (EC codewords per block, [(number of blocks, data codewords per block), ...])
QR_M_BLOCKS = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
QR_MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _gf_mul(x, y):
    """Multiply in GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _qr_alignment(ver):
    if ver == 1:
        return []
    num = ver // 7 + 2
    step = (ver * 8 + num * 3 + 5) // (num * 4 - 4) * 2
    return sorted([ver * 4 + 10 - i * step for i in range(num - 1)] + [6])


def _qr_format(mat, mask):
    """Write both copies of the format bits (level M + mask); returns the cells used."""
    size, rem = len(mat), mask   # level M = 00
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (mask << 10 | rem) ^ 0x5412
    cells = ([(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)])
    cells2 = [(size - 1 - i, 8) for i in range(8)] + [(8, size - 15 + i) for i in range(8, 15)]
    for copy in (cells, cells2):
        for i, (x, y) in enumerate(copy):
            mat[y][x] = bool(bits >> i & 1)
    mat[size - 8][8] = True   # the "dark module"
    return cells + cells2 + [(8, size - 8)]


def _qr_penalty(mat):
    rows = ["".join("1" if v else "0" for v in r) for r in mat]
    n, score = len(rows), 0
    for line in rows + ["".join(r[i] for r in rows) for i in range(n)]:
        score += sum(len(run) - 2 for run in re.findall(r"0{5,}|1{5,}", line))
        score += 40 * len(re.findall(r"(?=10111010000|00001011101)", "0000" + line + "0000"))
    for a, b in zip(rows, rows[1:]):
        score += 3 * sum(1 for x in range(n - 1) if a[x] == a[x + 1] == b[x] == b[x + 1])
    dark, total = sum(r.count("1") for r in rows), n * n
    return score + 10 * ((abs(dark * 20 - total * 10) + total - 1) // total - 1)


def qr_matrix(text):
    """QR code for `text` as rows of booleans (True = dark), without the quiet zone."""
    data = text.encode()
    for ver, (ec_len, groups) in QR_M_BLOCKS.items():
        capacity, cc_bits = sum(n * d for n, d in groups), 8 if ver < 10 else 16
        if 4 + cc_bits + 8 * len(data) <= capacity * 8:
            break
    else:
        raise ValueError("text too long for a QR code")
    bits = "0100" + format(len(data), f"0{cc_bits}b") + "".join(format(b, "08b") for b in data)
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[i % 2] for i in range(capacity - len(words))]

    blocks, pos, divisor = [], 0, _rs_divisor(ec_len)
    for count, n in groups:
        for _ in range(count):
            blocks.append(words[pos:pos + n])
            pos += n
    ecs = [_rs_remainder(b, divisor) for b in blocks]
    final = []
    for i in range(max(len(b) for b in blocks)):
        final += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        final += [e[i] for e in ecs]

    # Function patterns
    size = ver * 4 + 17
    mat = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]
    def put(x, y, dark):
        mat[y][x], fn[y][x] = dark, True
    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    align = _qr_alignment(ver)
    last = len(align) - 1
    for i, ax in enumerate(align):
        for j, ay in enumerate(align):
            if (i, j) not in ((0, 0), (0, last), (last, 0)):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    for x, y in _qr_format(mat, 0):
        fn[y][x] = True
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            a, b, dark = size - 11 + i % 3, i // 3, bool(vbits >> i & 1)
            put(a, b, dark)
            put(b, a, dark)

    # Data, in two-column zigzags from the bottom right, skipping the vertical timing column
    i, right, nbits = 0, size - 1, len(final) * 8
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for x in (right, right - 1):
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fn[y][x] and i < nbits:
                    mat[y][x] = bool(final[i >> 3] >> (7 - (i & 7)) & 1)
                    i += 1
        right -= 2

    best = None
    for mask, flip in enumerate(QR_MASKS):
        t = [[v ^ (not fn[y][x] and flip(x, y)) for x, v in enumerate(row)] for y, row in enumerate(mat)]
        _qr_format(t, mask)
        score = _qr_penalty(t)
        if best is None or score < best[0]:
            best = (score, t)
    return best[1]


@lru_cache(maxsize=8)
def qr_svg(text, quiet=4):
    """QR code as inline SVG, black on white (also in dark mode, so phones can read it)."""
    mat = qr_matrix(text)
    n = len(mat) + 2 * quiet
    path = []
    for y, row in enumerate(mat):
        for m in re.finditer(r"1+", "".join("1" if v else "0" for v in row)):
            w = m.end() - m.start()
            path.append(f"M{m.start() + quiet} {y + quiet}h{w}v1h-{w}z")
    return (f'<svg class="qr" viewBox="0 0 {n} {n}" shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code for {html.escape(text)}"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{"".join(path)}" fill="#000"/></svg>')


# ---------------------------------------------------------------- network discovery (SSDP)
# Windows lists UPnP devices in File Explorer > Network; double-clicking one opens its
# presentationURL. Answering SSDP searches (and announcing now and then) is all it takes.

SSDP_GROUP, SSDP_PORT = "239.255.255.250", 1900
SSDP_MAX_AGE = 1800
SSDP_NOTIFY_EVERY = 600
DEVICE_TYPE = "urn:schemas-upnp-org:device:Basic:1"
# Stable per Pi, without publishing the machine id itself
DEVICE_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "rpi-print-server:" + read("/etc/machine-id", socket.gethostname())))


def local_ip_for(peer):
    """Our address on the interface that reaches `peer` (connecting a UDP socket sends nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((peer, 9))
            return s.getsockname()[0]
        except OSError:
            return ""


def device_xml(ip):
    name = html.escape(f"Print & Scan Server ({socket.gethostname()})")
    return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<URLBase>http://{ip}:{PORT}/</URLBase>
<device>
<deviceType>{DEVICE_TYPE}</deviceType>
<friendlyName>{name}</friendlyName>
<manufacturer>Raspberry Pi</manufacturer>
<modelName>Print &amp; scan server</modelName>
<modelDescription>{html.escape(PRINTER_NAME)} print and scan server</modelDescription>
<modelNumber>1</modelNumber>
<serialNumber>{DEVICE_UUID[:8]}</serialNumber>
<UDN>uuid:{DEVICE_UUID}</UDN>
<presentationURL>http://{ip}:{PORT}/</presentationURL>
</device>
</root>
"""


class SSDP(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)

    @staticmethod
    def targets(st):
        u = f"uuid:{DEVICE_UUID}"
        all_ = [("upnp:rootdevice", f"{u}::upnp:rootdevice"), (u, u), (DEVICE_TYPE, f"{u}::{DEVICE_TYPE}")]
        return all_ if st == "ssdp:all" else [t for t in all_ if t[0] == st]

    @staticmethod
    def common(ip):
        return (f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}\r\nLOCATION: http://{ip}:{PORT}/upnp/device.xml\r\n"
                f"SERVER: Linux/{platform.release()} UPnP/1.0 rpi-print-server/1.0\r\n")

    def notify(self, sock):
        ip = local_ip_for(SSDP_GROUP)
        if not ip:
            return
        for nt, usn in self.targets("ssdp:all"):
            msg = (f"NOTIFY * HTTP/1.1\r\nHOST: {SSDP_GROUP}:{SSDP_PORT}\r\n" + self.common(ip) +
                   f"NT: {nt}\r\nNTS: ssdp:alive\r\nUSN: {usn}\r\n\r\n")
            sock.sendto(msg.encode(), (SSDP_GROUP, SSDP_PORT))

    def answer(self, sock, data, addr):
        lines = data.decode("utf-8", "replace").split("\r\n")
        if not lines[0].upper().startswith("M-SEARCH"):
            return
        hdr = {k.strip().upper(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:]) if k}
        if hdr.get("MAN", "").strip('"') != "ssdp:discover":
            return
        ip = local_ip_for(addr[0])
        for st, usn in self.targets(hdr.get("ST", "")):
            msg = ("HTTP/1.1 200 OK\r\n" + self.common(ip) +
                   f"DATE: {formatdate(usegmt=True)}\r\nEXT:\r\nST: {st}\r\nUSN: {usn}\r\n\r\n")
            sock.sendto(msg.encode(), addr)

    def serve(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", SSDP_PORT))
            # Fails (and is retried) while the network is still down at boot
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(SSDP_GROUP) + socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(SSDP_NOTIFY_EVERY)
            self.notify(sock)
            last = time.time()
            while True:
                try:
                    data, addr = sock.recvfrom(2048)
                    self.answer(sock, data, addr)
                except socket.timeout:
                    pass
                if time.time() - last >= SSDP_NOTIFY_EVERY:
                    self.notify(sock)
                    last = time.time()

    def run(self):
        while True:
            try:
                self.serve()
            except Exception as e:   # network down, interface changed, ...: try again
                print(f"ssdp: {e!r}", flush=True)
            time.sleep(30)


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Server</title>
<style>
:root { --bg:#f4f6f8; --card:#fff; --text:#1d2330; --muted:#5d6675; --border:#dde1e6;
        --accent:#1a73e8; --ok:#188038; --warn:#b06000; --bad:#c5221f; --info:#1a73e8; --chip:#eef1f4;
        --warn-bg:#fef7e0; --bad-bg:#fce8e6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161b; --card:#1f2228; --text:#e8eaed; --muted:#9aa0a6; --border:#353a42;
          --accent:#8ab4f8; --ok:#81c995; --warn:#fdd663; --bad:#f28b82; --info:#8ab4f8; --chip:#2a2e35;
          --warn-bg:#3a3220; --bad-bg:#3c2423; }
}
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
main { max-width: 820px; margin: 0 auto; padding: 32px 16px 48px; }
header h1 { font-size: 1.6rem; margin: 0; }
header p { color: var(--muted); margin: 4px 0 24px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 20px; margin-bottom: 16px; }
.card h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 12px; }
.status { display: flex; align-items: center; gap: 14px; }
.dot { width: 14px; height: 14px; border-radius: 50%; flex: none; background: var(--muted); }
.ready .dot { background: var(--ok); } .printing .dot { background: var(--info); animation: pulse 1.2s infinite; }
.offline .dot, .attention .dot { background: var(--warn); } .stopped .dot, .missing .dot { background: var(--bad); }
@keyframes pulse { 50% { opacity: .35; } }
.status .label { font-size: 1.35rem; font-weight: 650; }
.status .name { color: var(--muted); font-size: .9rem; }
.detail { margin: 10px 0 0 28px; color: var(--muted); }
.jobs { margin: 14px 0 0 28px; padding: 0; list-style: none; }
.jobs li { padding: 6px 0; border-top: 1px solid var(--border); font-size: .92rem; display: flex; justify-content: space-between; gap: 12px; }
.jobs .meta { color: var(--muted); white-space: nowrap; }
.actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
a.action { display: block; padding: 16px; border-radius: 12px; border: 1px solid var(--border);
           text-decoration: none; color: var(--text); background: var(--card); }
a.action:hover { border-color: var(--accent); }
a.action.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
@media (prefers-color-scheme: dark) { a.action.primary { color: #14161b; } }
a.action strong { display: block; font-size: 1.05rem; margin-bottom: 3px; }
a.action span { font-size: .85rem; opacity: .8; }
.alert { border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; font-size: .92rem; line-height: 1.45; }
.alert.warn { background: var(--warn-bg); border-left: 4px solid var(--warn); }
.alert.bad { background: var(--bad-bg); border-left: 4px solid var(--bad); }
.alert.ok { background: var(--chip); border-left: 4px solid var(--ok); }
.alert strong { display: block; margin-bottom: 2px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.stat { background: var(--chip); border-radius: 10px; padding: 12px; min-width: 0; }
.stat .v { font-size: 1.2rem; font-weight: 650; font-variant-numeric: tabular-nums; }
.stat .k { font-size: .8rem; color: var(--muted); }
.stat .s { font-size: .78rem; color: var(--muted); margin-top: 2px; }
.stat.wide { grid-column: span 2; }
@media (max-width: 420px) { .stat.wide { grid-column: span 1; } }
.v.ok { color: var(--ok); } .v.warn { color: var(--warn); } .v.bad { color: var(--bad); }
svg.spark { width: 100%; height: 38px; display: block; margin-top: 6px; color: var(--accent); }
.bar { height: 6px; background: var(--border); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.bar > div { height: 100%; background: var(--accent); border-radius: 3px; width: 0; transition: width .4s; }
table.kv { width: 100%; border-collapse: collapse; font-size: .9rem; }
table.kv th { text-align: left; font-weight: 500; color: var(--muted); padding: 7px 12px 7px 0; vertical-align: top; width: 38%; }
table.kv td { padding: 7px 0; border-top: 1px solid var(--border); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
table.kv tr:first-child td { border-top: 0; }
table.kv tr th { border-top: 1px solid var(--border); } table.kv tr:first-child th { border-top: 0; }
details summary { cursor: pointer; font-weight: 600; }
details p { color: var(--muted); font-size: .92rem; line-height: 1.5; }
code { background: var(--chip); padding: 2px 6px; border-radius: 6px; font-size: .85rem; word-break: break-all; }
footer { color: var(--muted); font-size: .8rem; text-align: center; margin-top: 8px; }
.find { display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
.find svg.qr { width: 150px; height: 150px; flex: none; border-radius: 8px; }
.find > div { flex: 1; min-width: 220px; }
.find p { margin: 0 0 8px; color: var(--muted); font-size: .92rem; line-height: 1.45; }
.find ul { margin: 0 0 10px; padding: 0; list-style: none; }
.find li { padding: 3px 0; }
.find li a { color: var(--accent); font-weight: 600; overflow-wrap: anywhere; }
.find li span { color: var(--muted); font-size: .85rem; }
a.button { display: inline-block; padding: 9px 16px; border-radius: 10px; border: 1px solid var(--border);
           color: var(--text); text-decoration: none; font-weight: 600; font-size: .92rem; }
a.button:hover { border-color: var(--accent); }
</style></head><body><main>
<header>
  <h1>Print &amp; scan server</h1>
  <p><span id="host">{{host}}</span> &middot; <span id="ip"></span> &middot; {{printer}}</p>
</header>

<section class="card" aria-live="polite">
  <h2>Printer</h2>
  <div id="printer" class="status missing">
    <div class="dot"></div>
    <div><div class="label" id="p-label">Checking…</div><div class="name">{{printer}}</div></div>
  </div>
  <p class="detail" id="p-detail"></p>
  <ul class="jobs" id="p-jobs"></ul>
</section>

<section class="card">
  <h2>Quick links</h2>
  <div class="actions">
    <a class="action primary" id="l-scan" href="#"><strong>Scan a document</strong><span>Scan to PDF, JPEG or PNG</span></a>
    <a class="action" id="l-queue" href="#"><strong>Print queue</strong><span>Jobs, test page, pause/resume</span></a>
    <a class="action" id="l-cups" href="#"><strong>Printer admin</strong><span>CUPS settings</span></a>
  </div>
</section>

<section class="card">
  <h2>Open on another device</h2>
  <div class="find">
    {{qr_home}}
    <div>
      <p>Scan the code with a phone camera, or type one of these addresses:</p>
      <ul>
        <li><a href="http://{{ip}}/">http://{{ip}}/</a></li>
        <li><a href="http://{{local}}/">http://{{local}}/</a> <span>Windows, Mac, iPhone</span></li>
      </ul>
      <p>On Windows it also shows up in File Explorer under <em>Network</em>.</p>
      <a class="button" href="/sheet" target="_blank">Print an info sheet</a>
    </div>
  </div>
</section>

<section class="card">
  <h2>Raspberry Pi health</h2>
  <div id="power-alert"></div>
  <div class="stats">
    <div class="stat wide"><div class="k">CPU usage</div><div class="v" id="s-cpu">–</div>
      <div class="s" id="s-load"></div><svg class="spark" id="g-cpu" viewBox="0 0 120 38" preserveAspectRatio="none"></svg></div>
    <div class="stat wide"><div class="k">CPU temperature</div><div class="v" id="s-temp">–</div>
      <div class="s" id="s-temp-range"></div><svg class="spark" id="g-temp" viewBox="0 0 120 38" preserveAspectRatio="none"></svg></div>
    <div class="stat"><div class="k">CPU speed</div><div class="v" id="s-clock">–</div><div class="s" id="s-clock-s"></div></div>
    <div class="stat"><div class="k">Core voltage</div><div class="v" id="s-volt">–</div><div class="s" id="s-volt-s"></div></div>
    <div class="stat"><div class="k">Power supply</div><div class="v" id="s-power">–</div><div class="s" id="s-power-s"></div></div>
    <div class="stat"><div class="k">Memory free</div><div class="v" id="s-mem">–</div><div class="bar"><div id="b-mem"></div></div></div>
    <div class="stat"><div class="k">SD card free</div><div class="v" id="s-disk">–</div><div class="bar"><div id="b-disk"></div></div></div>
    <div class="stat"><div class="k">Wi-Fi signal</div><div class="v" id="s-wifi">–</div><div class="s" id="s-wifi-s"></div></div>
    <div class="stat"><div class="k">Up for</div><div class="v" id="s-up">–</div><div class="s" id="s-procs"></div></div>
  </div>
</section>

<section class="card">
  <details id="hw-details">
    <summary>Hardware details</summary>
    <table class="kv" id="hw-table" style="margin-top:12px"></table>
  </details>
</section>

<section class="card">
  <details>
    <summary>Add this printer to a computer or phone</summary>
    <p><strong>Windows 11:</strong> Settings → Bluetooth &amp; devices → Printers &amp; scanners → Add device.
       If it isn't listed: <em>Add manually</em> → <em>Select a shared printer by name</em> → enter
       <code id="ipp-url"></code> and choose the <em>Microsoft IPP Class Driver</em>.</p>
    <p><strong>Mac, iPhone, iPad, Android:</strong> the printer appears automatically when you print,
       as long as you're on the same Wi-Fi.</p>
  </details>
</section>

<footer><a href="https://github.com/mnimonic/rpi-print-server/releases" style="color:inherit">rpi-print-server {{version}}</a> · Updates every 5 seconds · <span id="updated"></span></footer>
</main>
<script>
const host = location.hostname;
const cupsPrinter = 'http://' + host + ':631/printers/{{queue}}';
document.getElementById('l-scan').href = 'http://' + host + ':{{scanport}}/';
document.getElementById('l-queue').href = cupsPrinter;
document.getElementById('l-cups').href = 'http://' + host + ':631/';
document.getElementById('ipp-url').textContent = cupsPrinter;

const $ = id => document.getElementById(id);
const set = (id, text, cls) => { const e = $(id); e.textContent = text; if (cls !== undefined) e.className = 'v ' + cls; };
function duration(s) {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? d + 'd ' + h + 'h' : h ? h + 'h ' + m + 'm' : m + ' min';
}
function spark(id, values, lo, hi) {
  const svg = $(id);
  if (!values || values.length < 2) { svg.innerHTML = ''; return; }
  const n = values.length, W = 120, H = 38;
  const y = v => (H - 2) - (Math.min(Math.max(v, lo), hi) - lo) / (hi - lo) * (H - 4);
  const pts = values.map((v, i) => (i * W / (n - 1)).toFixed(1) + ',' + y(v).toFixed(1));
  svg.innerHTML = '<polyline points="0,' + H + ' ' + pts.join(' ') + ' ' + W + ',' + H +
    '" fill="currentColor" fill-opacity=".12" stroke="none"/>' +
    '<polyline points="' + pts.join(' ') + '" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/>';
}
function renderPrinter(p) {
  $('printer').className = 'status ' + p.state;
  $('p-label').textContent = p.label;
  $('p-detail').textContent = p.detail || '';
  const jobs = $('p-jobs');
  jobs.replaceChildren();
  if (p.jobs && p.jobs.length) {
    const h = document.createElement('li');
    const s = document.createElement('strong');
    s.textContent = p.jobs.length + (p.jobs.length === 1 ? ' document' : ' documents') + ' in the queue';
    h.append(s); jobs.append(h);
  }
  (p.jobs || []).forEach(j => {
    const li = document.createElement('li');
    const a = document.createElement('span'); a.textContent = 'Job ' + j.id + ' from ' + j.user;
    const b = document.createElement('span'); b.className = 'meta';
    b.textContent = (j.size_kb ? j.size_kb + ' KB · ' : '') + 'waiting';
    li.append(a, b); jobs.append(li);
  });
}
function renderPower(pi) {
  const fw = pi.fw || {}, now = fw.throttle_now || [], boot = fw.throttle_since_boot || [];
  const box = $('power-alert');
  let cls = '', title = '', body = '';
  if (now.includes('Under-voltage')) {
    cls = 'bad'; title = 'Under-voltage right now';
    body = 'The power supply can\'t deliver enough current. The Pi may slow down, crash or corrupt the SD card. Use a 5 V / 2.5 A supply and a short, thick USB cable.';
  } else if (now.length) {
    cls = 'warn'; title = now.join(', ') + ' right now';
    body = 'The Pi is limiting its speed to protect itself.';
  } else if (boot.includes('Under-voltage')) {
    cls = 'warn'; title = 'Under-voltage happened since the last restart';
    body = 'The supply voltage dipped too low at least once' +
      (boot.length > 1 ? ' and the Pi ' + boot.filter(x => x !== 'Under-voltage').join(', ').toLowerCase() + ' to protect itself' : '') +
      '. It\'s fine at the moment, but a stronger power supply (5 V / 2.5 A) and a better cable are recommended, especially with the Wi-Fi adapter and printer attached.';
  } else if (boot.length) {
    cls = 'warn'; title = boot.join(', ') + ' happened since the last restart'; body = 'Usually caused by heat.';
  }
  box.innerHTML = '';
  if (cls) {
    const d = document.createElement('div'); d.className = 'alert ' + cls;
    const s = document.createElement('strong'); s.textContent = title;
    d.append(s, document.createTextNode(body)); box.append(d);
  }
  if (now.includes('Under-voltage')) set('s-power', 'Too low', 'bad');
  else if (boot.includes('Under-voltage')) set('s-power', 'OK now', 'warn');
  else if (fw.throttled_raw) set('s-power', 'OK', 'ok');
  else set('s-power', '–', '');
  $('s-power-s').textContent = fw.throttled_raw ? 'throttled=' + fw.throttled_raw : '';
}
function renderHealth(pi) {
  const fw = pi.fw || {}, st = pi.static || {};
  $('ip').textContent = pi.ip || '';
  // CPU
  if (pi.cpu_pct != null) set('s-cpu', Math.round(pi.cpu_pct) + ' %', pi.cpu_pct > 90 ? 'warn' : '');
  $('s-load').textContent = pi.load && pi.load.length ? 'load ' + pi.load.map(x => x.toFixed(2)).join(' / ') : '';
  spark('g-cpu', pi.cpu_history, 0, 100);
  // Temperature
  if (pi.temp_c != null) set('s-temp', pi.temp_c.toFixed(1) + ' °C', pi.temp_c >= 75 ? 'bad' : pi.temp_c >= 60 ? 'warn' : 'ok');
  const th = pi.temp_history || [];
  if (th.length) $('s-temp-range').textContent = 'last 5 min: ' + Math.min(...th).toFixed(1) + '–' + Math.max(...th).toFixed(1) + ' °C';
  spark('g-temp', th, Math.min(30, ...th), Math.max(60, ...th));
  // Clock
  const mhz = fw.clock_arm_mhz || pi.clock_mhz_now;
  if (mhz) set('s-clock', mhz + ' MHz', st.cpu_max_mhz && mhz < st.cpu_max_mhz ? 'warn' : '');
  $('s-clock-s').textContent = (st.cpu_max_mhz ? 'max ' + st.cpu_max_mhz + ' MHz' : '') +
    (fw.clock_core_mhz ? ' · GPU ' + fw.clock_core_mhz + ' MHz' : '');
  // Voltage
  if (fw.volt_core) set('s-volt', fw.volt_core.toFixed(2) + ' V', '');
  $('s-volt-s').textContent = fw.volt_sdram_c ? 'RAM ' + fw.volt_sdram_c.toFixed(2) + ' V' : '';
  renderPower(pi);
  // Memory / disk
  if (pi.mem_total_mb) {
    set('s-mem', pi.mem_avail_mb + ' MB', pi.mem_avail_mb < 50 ? 'bad' : '');
    $('b-mem').style.width = (100 * (1 - pi.mem_avail_mb / pi.mem_total_mb)).toFixed(0) + '%';
  }
  if (pi.disk_total_gb) {
    set('s-disk', pi.disk_free_gb + ' GB', pi.disk_free_gb < 1 ? 'bad' : '');
    $('b-disk').style.width = (100 * (1 - pi.disk_free_gb / pi.disk_total_gb)).toFixed(0) + '%';
  }
  // Wi-Fi
  if (pi.wifi) {
    set('s-wifi', pi.wifi.quality_pct + ' %', pi.wifi.quality_pct < 40 ? 'warn' : '');
    $('s-wifi-s').textContent = pi.wifi.signal_dbm + ' dBm · ' + pi.wifi.iface;
  } else { set('s-wifi', 'n/a', ''); $('s-wifi-s').textContent = 'wired or no Wi-Fi'; }
  set('s-up', duration(pi.uptime_s), '');
  $('s-procs').textContent = pi.procs + ' processes';
  renderHardware(pi);
}
function cfgClocks(c) {
  // 0 means "not set in config.txt" = the board's stock clock
  const parts = [['CPU', c.arm_freq], ['GPU', c.core_freq], ['RAM', c.sdram_freq]]
    .filter(([, v]) => v != null).map(([k, v]) => k + ' ' + (v ? v + ' MHz' : 'stock'));
  if (!parts.length) return '';
  const all = [c.arm_freq, c.core_freq, c.sdram_freq].every(v => !v);
  return all ? 'all stock (no overclock)' : parts.join(' · ');
}
function renderHardware(pi) {
  const fw = pi.fw || {}, st = pi.static || {};
  const usb = (pi.usb || []).map(d => (d.maker ? d.maker + ' ' : '') + d.name + (d.speed_mbps ? ' (' + d.speed_mbps + ' Mb/s)' : ''));
  const rows = [
    ['Model', st.model], ['Board revision', st.revision],
    ['SoC / CPU', [st.soc, st.cpu].filter(Boolean).join(' · ') + (st.cores ? ' · ' + st.cores + (st.cores > 1 ? ' cores' : ' core') : '')],
    ['Architecture', st.arch],
    ['CPU speed range', st.cpu_min_mhz && st.cpu_max_mhz ? st.cpu_min_mhz + '–' + st.cpu_max_mhz + ' MHz' + (st.governor ? ' (' + st.governor + ')' : '') : ''],
    ['Clock settings', cfgClocks(st.config || {})],
    ['Over-voltage / turbo', st.config && st.config.over_voltage != null ?
       (st.config.over_voltage ? 'over_voltage=' + st.config.over_voltage : 'stock voltage') + ' · ' +
       (st.config.force_turbo ? 'force_turbo on' : 'no forced turbo') : ''],
    ['Live clocks', [fw.clock_arm_mhz && 'CPU ' + fw.clock_arm_mhz, fw.clock_core_mhz && 'GPU ' + fw.clock_core_mhz, fw.clock_sdram_mhz && 'RAM ' + fw.clock_sdram_mhz].filter(Boolean).join(' · ') + (fw.clock_arm_mhz ? ' MHz' : '')],
    ['Voltages', [fw.volt_core && 'core ' + fw.volt_core.toFixed(4) + ' V', fw.volt_sdram_c && 'sdram_c ' + fw.volt_sdram_c.toFixed(4) + ' V',
                  fw.volt_sdram_i && 'sdram_i ' + fw.volt_sdram_i.toFixed(4) + ' V', fw.volt_sdram_p && 'sdram_p ' + fw.volt_sdram_p.toFixed(4) + ' V'].filter(Boolean).join(' · ')],
    ['Throttling now', fw.throttled_raw ? ((fw.throttle_now || []).join(', ') || 'none') : ''],
    ['Throttling since boot', fw.throttled_raw ? ((fw.throttle_since_boot || []).join(', ') || 'none') : ''],
    ['Memory', pi.mem_total_mb ? pi.mem_total_mb + ' MB usable · ' + pi.mem_avail_mb + ' MB free' : ''],
    ['Memory split', fw.mem_arm_mb ? 'system ' + fw.mem_arm_mb + ' MB · GPU ' + fw.mem_gpu_mb + ' MB' : ''],
    ['Swap', pi.swap_total_mb != null ? pi.swap_used_mb + ' of ' + pi.swap_total_mb + ' MB used' : ''],
    ['SD card', [st.sd_name, st.sd_size_gb && st.sd_size_gb + ' GB'].filter(Boolean).join(' · ') + (pi.disk_total_gb ? ' · ' + pi.disk_free_gb + ' of ' + pi.disk_total_gb + ' GB free' : '')],
    ['USB devices', usb.join('\n')],
    ['Operating system', st.os], ['Kernel', st.kernel], ['Firmware', st.firmware],
  ];
  const t = $('hw-table');
  t.replaceChildren();
  rows.filter(r => r[1]).forEach(([k, v]) => {
    const tr = document.createElement('tr');
    const th = document.createElement('th'); th.textContent = k;
    const td = document.createElement('td'); td.textContent = v; td.style.whiteSpace = 'pre-line';
    tr.append(th, td); t.append(tr);
  });
}
async function refresh() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    const st = await r.json();
    st.pi.usb = st.usb;
    renderPrinter(st.printer);
    renderHealth(st.pi);
    $('updated').textContent = 'last update ' + new Date().toLocaleTimeString();
  } catch (e) {
    $('updated').textContent = 'cannot reach the Pi';
  }
}
refresh();
setInterval(refresh, 5000);
</script></body></html>"""

# Printable one-pager to stick on the printer: QR codes and addresses for both pages
SHEET = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Server Sheet</title>
<style>
@page { margin: 15mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: #000; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 720px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 1.9rem; margin: 0 0 4px; }
.sub { color: #444; margin: 0 0 22px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; }
.box { border: 2px solid #000; border-radius: 14px; padding: 16px; text-align: center; break-inside: avoid; }
.box h2 { font-size: 1.3rem; margin: 0; }
.box svg.qr { width: 200px; height: 200px; display: block; margin: 8px auto; }
.url { font-family: ui-monospace, Consolas, monospace; font-weight: 700; font-size: 1.05rem; overflow-wrap: anywhere; margin: 2px 0; }
.alt { font-family: ui-monospace, Consolas, monospace; color: #444; font-size: .92rem; overflow-wrap: anywhere; margin: 2px 0; }
.help { margin-top: 22px; line-height: 1.5; }
.help h2 { font-size: 1.15rem; margin: 0 0 6px; }
.help p { margin: 0 0 8px; }
.noprint { text-align: center; margin-top: 24px; }
button { font: inherit; font-weight: 600; padding: 10px 22px; border: 0; border-radius: 10px; background: #1a73e8; color: #fff; cursor: pointer; }
@media print { .noprint { display: none; } main { padding: 0; } }
</style></head><body><main>
<h1>Print &amp; scan server</h1>
<p class="sub">{{printer}} &middot; join the same Wi-Fi as the printer, then scan a code with your phone camera.</p>
<div class="grid">
  <div class="box"><h2>Printer status &amp; help</h2>{{qr_home}}
    <p class="url">http://{{ip}}/</p><p class="alt">http://{{local}}/</p></div>
  <div class="box"><h2>Scan a document</h2>{{qr_scan}}
    <p class="url">http://{{ip}}:{{scanport}}/</p><p class="alt">http://{{local}}:{{scanport}}/</p></div>
</div>
<div class="help">
  <h2>Printing</h2>
  <p><strong>iPhone, iPad, Mac, Android:</strong> nothing to install, the printer is offered automatically when you print.</p>
  <p><strong>Windows 11:</strong> Settings &rarr; Bluetooth &amp; devices &rarr; Printers &amp; scanners &rarr; Add device.
     If it isn't listed, add it by address: <span class="url">http://{{local}}:631/printers/{{queue}}</span></p>
</div>
<p class="noprint"><button onclick="window.print()">Print this sheet</button></p>
</main></body></html>"""


_status_cache = {"t": 0, "json": ""}
_status_lock = threading.Lock()


def status_json():
    """Full status, shared between viewers for a few seconds (building it costs ~0.5 s of a Pi 1 CPU)."""
    with _status_lock:
        if time.time() - _status_cache["t"] > SAMPLE_EVERY - 1:
            usb = usb_devices()
            st = {"printer": printer_status(usb), "pi": pi_status(), "usb": usb, "time": int(time.time())}
            _status_cache.update(t=time.time(), json=json.dumps(st))
        return _status_cache["json"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # status polling every 5 s would flood the journal

    def send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        ip = self.connection.getsockname()[0]   # the address this client reached us on
        if path in ("/", "/sheet"):
            page = PAGE if path == "/" else SHEET
            for key, val in {"host": html.escape(socket.gethostname()), "printer": html.escape(PRINTER_NAME),
                             "queue": QUEUE, "scanport": str(SCANNER_PORT), "version": html.escape(VERSION),
                             "ip": ip, "local": html.escape(socket.gethostname()) + ".local",
                             "qr_home": qr_svg(f"http://{ip}/"),
                             "qr_scan": qr_svg(f"http://{ip}:{SCANNER_PORT}/")}.items():
                page = page.replace("{{" + key + "}}", val)
            return self.send(200, page, "text/html; charset=utf-8")
        if path == "/api/status":
            return self.send(200, status_json(), "application/json")
        if path == "/upnp/device.xml":
            return self.send(200, device_xml(ip), 'text/xml; charset="utf-8"')
        self.send(404, "Not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    sampler.start()
    SSDP().start()
    print(f"Welcome page on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
