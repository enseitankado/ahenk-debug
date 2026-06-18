#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ahenk-debug — Ahenk istemci kayıt/bağlantı teşhis aracı
========================================================

Amaç
----
Lider tarafında ÇEVRİMDIŞI görünen ya da uzak komut kabul etmeyen Ahenk
makinelerini yerinde teşhis eder. Aşağıdaki başlıca arıza sınıflarını ayırt eder:

  1) KİMLİK ÇAKIŞMASI / KLON İMAJ
     Aynı UUID+parola (klonlanmış disk imajı) birden fazla makinede.
     Ahenk Pulsar'da `task-<uuid>` topic'ine `ahenk-<uuid>` adıyla
     ConsumerType.Exclusive ile abone olur. İkinci subscriber `ConsumerBusy`
     alır → komutları alamaz → Lider'de çevrimdışı/yanıtsız görünür.

  2) AĞ / BAĞLANTI
     DNS, TCP, TLS, sertifika, kayıt (register) ucu erişilebilirliği,
     varsayılan ağ geçidi / internet.

  3) YEREL YAZILIM SORUNU
     Servis durumu, config/DB tutarlılığı, kayıt durumu (registered/dn),
     conf<->db UUID/parola uyuşmazlığı, kimlik MAC'inin çözülememesi.

Ayrıca her makinede farklılık gösterebilecek bağlamı raporlar:
  - Kimlik MAC'i: Ahenk kimliği kablolu ethernet (ilk PCI, sürücülü, kablosuz
    olmayan) arayüzünün MAC'ine dayanır (etainfo.network).
  - Dağıtım / alt sürüm, Linux çekirdeği, mimari.
  - Donanım ve FAZ (Faz 1 / Faz 2 / Faz 3) tahmini: işlemci + anakart/BIOS +
    GPU + dokunmatik donanımı ve sürücüsü.

Kullanım
--------
    sudo ./ahenk_debug.py                 # tam insan-okur rapor
    sudo ./ahenk_debug.py --json          # makine-okur JSON
    sudo ./ahenk_debug.py --no-net        # ağ testlerini atla (hızlı)
    sudo ./ahenk_debug.py --out rapor.txt # raporu dosyaya da yaz

NOT: /etc/ahenk/ahenk.conf ve messaging.conf 0600 (yalnız root). Tam teşhis
için root gerekir; root değilse araç kısıtlı çalışır ve uyarır.
"""

import argparse
import datetime
import glob
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from configparser import ConfigParser, ExtendedInterpolation

# ----------------------------------------------------------------------------
# Sabitler
# ----------------------------------------------------------------------------
AHENK_CONF = "/etc/ahenk/ahenk.conf"
CONFIG_D = "/etc/ahenk/config.d"
DEFAULT_DB = "/etc/ahenk/ahenk.db"
LOG_FILES = ["/var/log/ahenk.log", "/var/log/ahenk.log.1"]
DMI = "/sys/devices/virtual/dmi/id"
NET_PATH = "/sys/class/net"

# ETA Register (eta-register) API — tahta MAC'inin okul/şehir/ilçe kaydını tutan
# sunucu. Lider, Ahenk MAC ile bağlanınca arka planda bu API'ye "kayıtlı mı?"
# diye sorar; kayıtlı değilse Ahenk kaydı ilerlemez. Değerler eta-register'ın
# kendi config.py'sinden okunur, okunamazsa bilinen üretim varsayılanı kullanılır.
ETA_REGISTER_CONFIG = "/usr/share/pardus/eta-register/src/config.py"
ETA_BACKEND_DEFAULT = "http://api-etap.eba.gov.tr:1000/api"
ETA_HEADER_DEFAULT = {"etap-app-code": "eta_register!"}

# FAZ tablosu: işlemci markasındaki alt dize -> faz etiketi.
# (Ahenk system.py içindeki devre-dışı bırakılmış get_eta_phase mantığından türetildi.)
PHASE_TABLE = [
    (("i3-2330m", "i3 2330"),            "Faz 1 (VESTEL)"),
    (("i3-3120m", "i3 3120"),            "Faz 2 Kısım 1 (INTEL/VESTEL)"),
    (("a10-5750m",),                     "Faz 2 Kısım 1 (AMD/VESTEL)"),
    (("i3-4000m", "i3 4000m"),           "Faz 2 Kısım 2 (VESTEL)"),
    (("i3-8100t",),                      "Faz 3"),
]

# ----------------------------------------------------------------------------
# Çıktı / renk yardımcıları
# ----------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty()


def _c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if _USE_COLOR else s


def bold(s):  return _c("1", s)
def red(s):   return _c("1;31", s)
def green(s): return _c("1;32", s)
def yellow(s):return _c("1;33", s)
def cyan(s):  return _c("1;36", s)
def grey(s):  return _c("2", s)

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
_BADGE = {
    OK:   green("[ OK ]"),
    WARN: yellow("[WARN]"),
    FAIL: red("[FAIL]"),
    INFO: cyan("[INFO]"),
}


class Report:
    """Bulguları toplar; hem ekrana basar hem JSON üretir hem özet/teşhis çıkarır."""

    def __init__(self):
        self.sections = []          # [(title, [lines])]
        self._cur = None
        self.findings = []          # (severity, title, detail)
        self.data = {}              # ham veri (json çıktısı için)

    # --- bölüm/satır ---
    def section(self, title):
        self._cur = (title, [])
        self.sections.append(self._cur)
        return self

    def line(self, key, value, severity=None):
        badge = (_BADGE[severity] + " ") if severity else ""
        if key:
            self._cur[1].append("  %s%s: %s" % (badge, bold(key), value))
        else:
            self._cur[1].append("  %s%s" % (badge, value))

    def note(self, text):
        self._cur[1].append("        %s" % grey(text))

    # --- teşhis bulgusu (özet için) ---
    def finding(self, severity, title, detail=""):
        self.findings.append((severity, title, detail))

    # --- render ---
    def render(self):
        out = []
        bar = "=" * 70
        out.append(bar)
        out.append(bold("  AHENK TEŞHİS RAPORU"))
        out.append("  %s  |  host: %s" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            platform.node()))
        out.append(bar)
        for title, lines in self.sections:
            out.append("")
            out.append(cyan("▸ " + title))
            out.extend(lines)
        out.append("")
        out.append(bar)
        out.append(bold("  ÖZET / TEŞHİS"))
        out.append(bar)
        order = {FAIL: 0, WARN: 1, OK: 2, INFO: 3}
        for sev, title, detail in sorted(self.findings, key=lambda f: order.get(f[0], 9)):
            out.append("  %s %s" % (_BADGE[sev], title))
            if detail:
                for d in detail.split("\n"):
                    out.append("        %s" % grey(d))
        if not self.findings:
            out.append("  (bulgu yok)")
        out.append(bar)
        return "\n".join(out)


# ----------------------------------------------------------------------------
# Düşük seviye yardımcılar
# ----------------------------------------------------------------------------
def run(cmd, timeout=8):
    """Komutu çalıştır, (rc, stdout, stderr) döndür. Hata durumunda rc=-1."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return -1, "", "komut yok"
    except subprocess.TimeoutExpired:
        return -1, "", "zaman aşımı"
    except Exception as e:
        return -1, "", str(e)


def read_file(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def readline_strip(path):
    c = read_file(path)
    return c.strip().splitlines()[0].strip() if c else None


def mask(secret):
    if not secret:
        return "(yok)"
    s = str(secret)
    return s[:4] + "…" + s[-4:] if len(s) > 9 else "****"


def is_root():
    return os.geteuid() == 0


# ----------------------------------------------------------------------------
# Konfigürasyon okuma
# ----------------------------------------------------------------------------
def load_config():
    """ahenk.conf + config.d/*.conf -> ConfigParser. (root yoksa kısmi)."""
    cp = ConfigParser()
    cp._interpolation = ExtendedInterpolation()
    files = []
    if os.path.exists(AHENK_CONF):
        files.append(AHENK_CONF)
    if os.path.isdir(CONFIG_D):
        for f in sorted(os.listdir(CONFIG_D)):
            if f.endswith(".conf") and "-old" not in f:
                files.append(os.path.join(CONFIG_D, f))
    readable = []
    perm_err = False
    for f in files:
        if os.access(f, os.R_OK):
            readable.append(f)
        else:
            perm_err = True
    try:
        cp.read(readable)
    except Exception:
        pass
    return cp, files, perm_err


def cfg(cp, section, option, fallback=None):
    try:
        return cp.get(section, option)
    except Exception:
        return fallback


# ----------------------------------------------------------------------------
# Veritabanı okuma
# ----------------------------------------------------------------------------
def load_db(db_path):
    import sqlite3
    out = {"path": db_path, "ok": False, "registration": None, "messaging": None,
           "error": None}
    if not os.path.exists(db_path):
        out["error"] = "DB dosyası yok"
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        cur = con.cursor()
        cur.execute("select jid,password,registered,dn,params,timestamp "
                    "from registration limit 1")
        row = cur.fetchone()
        if row:
            params = {}
            try:
                params = json.loads(row[4]) if row[4] else {}
            except Exception:
                params = {}
            out["registration"] = {
                "jid": row[0], "password": row[1], "registered": row[2],
                "dn": row[3], "params": params, "timestamp": row[5],
            }
        cur.execute("select topic_name,type,timestamp from messaging limit 1")
        m = cur.fetchone()
        if m:
            out["messaging"] = {"topic_name": m[0], "type": m[1], "timestamp": m[2]}
        out["ok"] = True
        con.close()
    except Exception as e:
        out["error"] = str(e)
    return out


# ----------------------------------------------------------------------------
# Kimlik MAC'i (etainfo mantığı)
# ----------------------------------------------------------------------------
def enumerate_nics():
    """Tüm arayüzleri (lo hariç) bus/sürücü/kablosuz bilgisiyle döndür."""
    nics = []
    for path in sorted(glob.glob(os.path.join(NET_PATH, "*"))):
        name = os.path.basename(path)
        if name == "lo":
            continue
        mac = readline_strip(os.path.join(path, "address"))
        dev = os.path.join(path, "device")
        drv_link = os.path.join(dev, "driver")
        driver = None
        if os.path.islink(drv_link):
            driver = os.path.basename(os.path.realpath(drv_link))
        wireless = os.path.exists(os.path.join(path, "wireless"))
        bus = "unknown"
        subsys = os.path.join(dev, "subsystem")
        if os.path.exists(subsys):
            bus = os.path.basename(os.path.realpath(subsys))
        operstate = readline_strip(os.path.join(path, "operstate"))
        nics.append({"interface": name, "mac": mac, "driver": driver,
                     "wireless": wireless, "bus": bus, "operstate": operstate,
                     "has_driver": driver is not None})
    return nics


def compute_identity_mac(nics):
    """
    etainfo.network.get() ile birebir aynı seçim:
    ilk PCI bus, sürücüye bağlı, kablosuz OLMAYAN arayüz.
    os.walk dizin sırasını taklit etmek için arayüz adına göre sıralı bakıyoruz.
    """
    for n in nics:
        if n["bus"] == "pci" and n["has_driver"] and not n["wireless"]:
            return n
    return None


def reproduce_etainfo():
    """Ahenk'in gerçekte kullandığı kodu çalıştır; çökerse yakala (teşhis!)."""
    try:
        from etainfo import network  # yalnızca os kullanır, güvenli
        d = network.get()
        if d is None:
            return {"ok": False, "mac": None, "error":
                    "etainfo.network.get() None döndü (uygun PCI ethernet yok)"}
        return {"ok": True, "mac": d.mac, "interface": d.interface,
                "bus_type": d.bus_type, "error": None}
    except Exception as e:
        return {"ok": False, "mac": None, "error": "%s: %s" % (type(e).__name__, e)}


# ----------------------------------------------------------------------------
# Log analizi
# ----------------------------------------------------------------------------
LOG_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_log_ts(line):
    """Log satırından datetime çıkar (format: 'YYYY-MM-DD HH:MM:SS,mmm')."""
    m = LOG_TS_RE.search(line)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None


def analyze_logs(max_lines=6000):
    """Son logları tara; her olay için sayı + son zaman damgası (str ve datetime)."""
    patterns = {
        "consumer_busy":    re.compile(r"ConsumerBusy", re.I),
        "not_authorized":   re.compile(r"not_authorized|registration_error", re.I),
        "pulsar_conn_fail": re.compile(r"(Pulsar connection failed|Failed to connect to Pulsar|Producer connection failed)", re.I),
        "conn_refused":     re.compile(r"refused|ConnectError|Connection reset", re.I),
        "timeout":          re.compile(r"timed out|TimeoutError|operation_timeout", re.I),
        "reg_success":      re.compile(r"Registration successful|already registered", re.I),
        "publish_ok":       re.compile(r"Message published successfully", re.I),
        "pulsar_connected": re.compile(r"Connected to Pulsar|Pulsar connection successful", re.I),
        "received":         re.compile(r"Received message|Fired event", re.I),
        "tls_err":          re.compile(r"certificate|TLS|SSL", re.I),
        "stopping":         re.compile(r"Stopping the service|Ahenk is stopping|systemctl stop ahenk", re.I),
    }
    res = {k: {"count": 0, "last": None, "last_dt": None} for k in patterns}
    res["available"] = False
    res["last_error"] = None
    res["last_line_dt"] = None
    # Logları KRONOLOJİK sırayla birleştir (eski .1 önce, güncel .log sonra).
    readable = [lf for lf in LOG_FILES if os.access(lf, os.R_OK)]
    readable.sort(key=lambda p: os.path.getmtime(p))  # en eski -> en yeni
    text = None
    for lf in readable:
        c = read_file(lf)
        if c:
            text = (text or "") + c
    if not text:
        return res
    res["available"] = True
    lines = text.splitlines()[-max_lines:]
    for ln in lines:
        dt = parse_log_ts(ln)
        if dt:
            res["last_line_dt"] = dt
        for key, rx in patterns.items():
            if rx.search(ln):
                res[key]["count"] += 1
                res[key]["last"] = LOG_TS_RE.search(ln).group(1) if LOG_TS_RE.search(ln) else ln[:60]
                if dt:
                    res[key]["last_dt"] = dt
        if "ERROR" in ln:
            res["last_error"] = ln.strip()[:200]
    return res


# ----------------------------------------------------------------------------
# Ağ / bağlantı testleri
# ----------------------------------------------------------------------------
def dns_resolve(host):
    try:
        return socket.gethostbyname(host)
    except Exception as e:
        return None


def tcp_check(host, port, timeout=5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, None
    except Exception as e:
        return False, str(e)


def tls_check(host, port, cafile=None, timeout=6):
    """TLS el sıkışması + sunucu sertifikası bitiş tarihi."""
    try:
        if cafile and os.path.exists(cafile):
            ctx = ssl.create_default_context(cafile=cafile)
        else:
            ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # ahenk de hostname doğrulamıyor
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                der = ss.getpeercert(binary_form=True)
                return True, None, len(der) if der else 0
    except Exception as e:
        return False, str(e), 0


def cert_expiry(path):
    if not path or not os.path.exists(path):
        return None
    rc, out, _ = run(["openssl", "x509", "-enddate", "-noout", "-in", path])
    if rc == 0 and "=" in out:
        return out.split("=", 1)[1].strip()
    return None


def default_gateway():
    rc, out, _ = run(["ip", "route", "show", "default"])
    if rc == 0 and out:
        return out.splitlines()[0]
    return None


def tcp_latency(host, port, timeout=5, attempts=2):
    """(ok, gecikme_ms, hata) — temel TCP erişilebilirlik testi (geçici hataya karşı tekrar)."""
    last = None
    for i in range(attempts):
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True, (time.monotonic() - t0) * 1000.0, None
        except Exception as e:
            last = str(e)
            if i < attempts - 1:
                time.sleep(0.6)
    return False, None, last


def ping_host(host, timeout=2):
    rc, _, _ = run(["ping", "-c", "1", "-W", str(timeout), host], timeout=timeout + 3)
    return rc == 0


def resolv_conf_nameservers():
    c = read_file("/etc/resolv.conf") or ""
    return re.findall(r"^\s*nameserver\s+(\S+)", c, re.M)


def nic_link_status(iface):
    """Kimlik arayüzünün fiziksel/IP durumunu döndür."""
    if not iface:
        return None
    base = os.path.join(NET_PATH, iface)
    carrier = readline_strip(os.path.join(base, "carrier"))
    oper = readline_strip(os.path.join(base, "operstate"))
    speed = readline_strip(os.path.join(base, "speed"))
    ip = None
    rc, out, _ = run(["ip", "-4", "addr", "show", "dev", iface])
    if rc == 0 and out:
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            ip = m.group(1)
    return {"carrier": carrier, "operstate": oper, "speed": speed,
            "ip": ip, "has_ip": ip is not None}


def discover_broker_endpoints():
    """Canlı soketlerden broker uç noktalarını keşfet (config root gerektirmeden)."""
    eps = set()
    rc, out, _ = run("ss -tn")
    if rc == 0 and out:
        for ln in out.splitlines():
            if "ESTAB" not in ln:
                continue
            cols = ln.split()
            if len(cols) < 5:
                continue
            ip, _, port = cols[4].rpartition(":")
            if port in ("6650", "6651", "5222", "5223"):
                eps.add((ip, port))
    return sorted(eps)


def disk_usage_info(path):
    try:
        st = shutil.disk_usage(path)
        return {"total": st.total, "used": st.used, "free": st.free,
                "pct": st.used / st.total * 100.0 if st.total else 0}
    except Exception:
        return None


def db_integrity(path):
    import sqlite3
    if not os.path.exists(path):
        return "dosya yok"
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        r = con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return r[0] if r else None
    except Exception as e:
        return "hata: %s" % e


def service_restart_count():
    rc, out, _ = run("systemctl show ahenk.service -p NRestarts --value")
    if rc == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


def service_result():
    rc, out, _ = run("systemctl show ahenk.service -p Result --value")
    return out.strip() if rc == 0 else None


def scan_session_errors(srv_start, max_samples=4):
    """Mevcut servis oturumunda (srv_start sonrası) ölümcül/başlangıç hatalarını tara."""
    out = {"available": False, "count": 0, "samples": []}
    text = read_file("/var/log/ahenk.log")
    if not text:
        return out
    out["available"] = True
    pat = re.compile(r"ERROR|CRITICAL|Traceback|ImportError|ModuleNotFound|No module named", re.I)
    for ln in text.splitlines():
        if "ConsumerBusy" in ln:   # bağlantı çakışması ayrı ele alınıyor
            continue
        if not pat.search(ln):
            continue
        dt = parse_log_ts(ln)
        if srv_start and dt and dt < srv_start:
            continue
        out["count"] += 1
        if len(out["samples"]) < max_samples:
            s = ln.strip()
            s = re.sub(r"^format=", "", s)        # logger önekini temizle
            out["samples"].append(s[:170])
    return out


def service_start_dt():
    """ahenk.service'in en son aktif olduğu (yeniden başladığı) zaman."""
    rc, out, _ = run("systemctl show ahenk.service -p ActiveEnterTimestamp --value")
    if rc == 0 and out:
        m = LOG_TS_RE.search(out)
        if m:
            try:
                return datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
    return None


def service_main_pid():
    rc, out, _ = run("systemctl show ahenk.service -p MainPID --value")
    if rc == 0 and out.strip().isdigit() and out.strip() != "0":
        return out.strip()
    return None


def live_broker_connections(cp):
    """
    Ahenk'in mesajlaşma broker'ına (Pulsar/XMPP) ŞU ANDAKİ canlı TCP
    bağlantılarını bul. Yerel bir kontrol — trafik üretmez, --no-net'ten etkilenmez.
    """
    res = {"host": None, "port": None, "ips": [], "established": [], "count": 0,
           "pid": None, "owned_by_ahenk": None, "method": None, "error": None}
    ALL_BROKER_PORTS = {"6650", "6651", "5222", "5223"}
    mtype = (cfg(cp, "MESSENGER", "messenger_type", None) or "").lower()
    if mtype == "pulsar":
        host = cfg(cp, "PULSAR", "pulsar_host")
        port = cfg(cp, "PULSAR", "pulsar_port")
        fallback_ports = {"6650", "6651"}
    elif mtype == "xmpp":
        host = cfg(cp, "CONNECTION", "host")
        port = cfg(cp, "CONNECTION", "port", "5222")
        fallback_ports = {"5222", "5223"}
    else:
        # config okunamadı (büyük olasılıkla root değil): tüm broker portlarını kabul et
        host = cfg(cp, "PULSAR", "pulsar_host") or cfg(cp, "CONNECTION", "host")
        port = cfg(cp, "PULSAR", "pulsar_port") or cfg(cp, "CONNECTION", "port")
        fallback_ports = ALL_BROKER_PORTS
    res["host"], res["port"], res["messenger_type"] = host, port, (mtype or "bilinmiyor")
    # host okunamıyorsa port eşleşmesine güven
    match_by_port_only = host is None
    pid = service_main_pid()
    res["pid"] = pid

    ips = set()
    if host:
        try:
            for fam, _, _, _, sa in socket.getaddrinfo(host, None):
                ips.add(sa[0])
        except Exception:
            pass
    res["ips"] = sorted(ips)

    rc, out, err = run("ss -tnp")
    if rc != 0 or not out:
        # ss yoksa psutil dene (root gerekli)
        if pid:
            try:
                import psutil
                conns = psutil.Process(int(pid)).connections(kind="tcp")
                for c in conns:
                    if c.status == "ESTABLISHED" and c.raddr:
                        peer = "%s:%s" % (c.raddr.ip, c.raddr.port)
                        rp = str(c.raddr.port)
                        if (not match_by_port_only and c.raddr.ip in ips and
                                (not port or rp == str(port))) or rp in fallback_ports:
                            res["established"].append({"peer": peer, "owned": True})
                res["count"] = len(res["established"])
                res["owned_by_ahenk"] = res["count"] > 0
                res["method"] = "psutil"
            except Exception as e:
                res["error"] = "ss yok; psutil: %s" % e
        else:
            res["error"] = "ss çalıştırılamadı: %s" % err
        return res

    res["method"] = "ss"
    owned_any = False
    for ln in out.splitlines():
        if "ESTAB" not in ln:
            continue
        cols = ln.split()
        if len(cols) < 5:
            continue
        peer = cols[4]
        pip, _, pport = peer.rpartition(":")
        match = False
        if not match_by_port_only and ips and pip in ips and (not port or pport == str(port)):
            match = True
        elif pport in fallback_ports:
            match = True
        if match:
            has_pid = (pid is not None) and ("pid=%s" % pid in ln)
            if has_pid:
                owned_any = True
            res["established"].append({"peer": peer, "owned": has_pid})
    res["count"] = len(res["established"])
    # ss root değilse process sütununu göstermez; o durumda owned bilinemez
    if pid and any("pid=" in l for l in out.splitlines()):
        res["owned_by_ahenk"] = owned_any
    return res


def active_pulsar_probe(cp, timeout=20):
    """
    AKTİF DOĞRULAMA (varsayılan, --no-net dışında). Ahenk'in connect() öz-testini taklit
    eder: gerçek uid/parola ile broker'a bağlanıp TEST_TOPIC'e bir producer açar
    ve test mesajı yollar. Böylece DNS+TCP+TLS+KİMLİK DOĞRULAMA'nın ŞU AN
    çalıştığı kanıtlanır. Exclusive komut aboneliğine (ahenk-<uid>) DOKUNMAZ,
    dolayısıyla çalışan Ahenk'i bozmaz.
    """
    res = {"attempted": True, "ok": False, "auth_ok": None, "stage": None,
           "error": None, "skipped": False}
    uid = cfg(cp, "CONNECTION", "uid")
    password = cfg(cp, "CONNECTION", "password")
    host = cfg(cp, "PULSAR", "pulsar_host")
    port = cfg(cp, "PULSAR", "pulsar_port")
    use_tls = (cfg(cp, "PULSAR", "pulsar_use_tls", "false") or "false").lower() == "true"
    ca = cfg(cp, "PULSAR", "tls_trust_certs_file_path")
    if not (uid and password and host and port):
        res["attempted"] = False
        res["skipped"] = True
        res["error"] = "uid/parola/host/port okunamadı (root gerekli)"
        return res
    # Bundled pulsar istemci kütüphanesini ekle
    lib = "/usr/share/ahenk/base/messaging/pulsar/pulsar_client_libs"
    if os.path.isdir(lib) and lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import pulsar
    except Exception as e:
        res["error"] = "pulsar kütüphanesi yüklenemedi: %s" % e
        return res
    client = None
    try:
        res["stage"] = "connect"
        ip = dns_resolve(host) or host
        scheme = "pulsar+ssl" if use_tls else "pulsar"
        url = "%s://%s:%s" % (scheme, ip, port)
        kwargs = dict(
            service_url=url,
            authentication=pulsar.AuthenticationBasic(uid, password, "customAuth"),
            connection_timeout_ms=int(timeout * 1000),
            operation_timeout_seconds=timeout,
        )
        if use_tls:
            kwargs.update(use_tls=True, tls_allow_insecure_connection=False,
                          tls_validate_hostname=False)
            if ca:
                kwargs["tls_trust_certs_file_path"] = ca
        client = pulsar.Client(**kwargs)
        res["stage"] = "producer(test-topic-lider)"
        prod = client.create_producer("test-topic-lider")
        prod.send(b"ahenk-debug probe")
        prod.close()
        res["ok"] = True
        res["auth_ok"] = True
        res["stage"] = "done"
    except Exception as e:
        msg = str(e)
        res["error"] = msg
        # Yetkilendirme/kimlik hatalarını ayırt et
        if re.search(r"auth|Authoriz|Authentic|Unauthorized|permission", msg, re.I):
            res["auth_ok"] = False
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return res


# ----------------------------------------------------------------------------
# ETA Register API (okul/şehir/ilçe kayıt sorgusu)
# ----------------------------------------------------------------------------
def load_eta_config():
    """eta-register/src/config.py'den BACKEND_URL ve SECURE_HEADER'ı çek."""
    backend = ETA_BACKEND_DEFAULT
    header = dict(ETA_HEADER_DEFAULT)
    src = read_file(ETA_REGISTER_CONFIG)
    if src:
        m = re.search(r'BACKEND_URL\s*=\s*["\']([^"\']+)["\']', src)
        if m:
            backend = m.group(1).strip()
        h = re.search(r'SECURE_HEADER\s*=\s*(\{[^}]*\})', src)
        if h:
            try:
                # {"etap-app-code": "..."} biçimi — güvenli ayrıştırma
                import ast
                parsed = ast.literal_eval(h.group(1))
                if isinstance(parsed, dict):
                    header = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                pass
    return backend, header


def query_eta_board(mac, backend, header, timeout=12, attempts=2):
    """
    GET {backend}/board/check?mac=<mac>  (eta-register'ın açılışta yaptığı sorgu).
    Lider'in arka planda sorduğu kayıt durumunu birebir taklit eder.
    Geçici ağ hatasına karşı birkaç kez dener (eta-register de tekrar dener).
    Dönen yapı: registered, registered_ip, data{school/city/town/unit/board_id/phase}.
    """
    out = {"ok": False, "url": None, "status": None, "registered": None,
           "registered_ip": None, "data": None, "raw": None, "error": None}
    if not mac:
        out["error"] = "MAC yok — sorgu yapılamadı"
        return out
    url = "%s/board/check?mac=%s" % (backend.rstrip("/"), mac)
    out["url"] = url
    # eta-register sertifika doğrulamasını kapatır (ssl-strict=False); aynısını yap
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = None
    for i in range(attempts):
        req = urllib.request.Request(url, method="GET")
        for k, v in header.items():
            req.add_header(k, v)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "ahenk-debug")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                out["status"] = getattr(r, "status", r.getcode())
                body = r.read().decode("utf-8", errors="replace")
            out["error"] = None
            break
        except urllib.error.HTTPError as e:
            # HTTP hata kodu da anlamlı gövde içerebilir (registered burada olabilir)
            out["status"] = e.code
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = None
            out["error"] = None
            break
        except Exception as e:
            out["error"] = str(e)
            if i < attempts - 1:
                time.sleep(1.0)
    if out["error"] and body is None:
        return out
    out["raw"] = body
    if body:
        try:
            j = json.loads(body)
            out["ok"] = True
            out["registered"] = j.get("registered")
            out["registered_ip"] = j.get("registered_ip")
            out["data"] = j.get("data")
            out["msg"] = j.get("msg")
        except Exception as e:
            out["error"] = "JSON ayrıştırılamadı: %s" % e
    else:
        out["error"] = "Boş yanıt (HTTP %s)" % out["status"]
    return out


# ----------------------------------------------------------------------------
# Donanım / faz / dokunmatik
# ----------------------------------------------------------------------------
def cpu_brand():
    c = read_file("/proc/cpuinfo") or ""
    for ln in c.splitlines():
        if "model name" in ln:
            return ln.split(":", 1)[1].strip()
    return None


def detect_phase(brand, board_vendor):
    if not brand:
        return "Bilinmiyor", "İşlemci markası okunamadı"
    b = brand.lower()
    for needles, label in PHASE_TABLE:
        if any(n in b for n in needles):
            if label == "Faz 3" and board_vendor and "gigabyte" in board_vendor.lower():
                return "Faz 3 (ARÇELİK)", "i3-8100T + GIGABYTE anakart"
            return label, "işlemci eşleşmesi: %s" % brand
    return "Bilinmiyor (manuel kontrol)", "tabloda eşleşmeyen işlemci: %s" % brand


def gpu_info():
    """PCI sınıf 0x030000 cihazlarını sürücüsüyle listele."""
    gpus = []
    for d in glob.glob("/sys/bus/pci/devices/*"):
        cls = readline_strip(os.path.join(d, "class"))
        if cls and cls.startswith("0x0300"):
            ven = readline_strip(os.path.join(d, "vendor"))
            dev = readline_strip(os.path.join(d, "device"))
            drv = None
            drv_link = os.path.join(d, "driver")
            if os.path.islink(drv_link):
                drv = os.path.basename(os.path.realpath(drv_link))
            gpus.append({"vendor": ven, "device": dev, "driver": drv})
    return gpus


def _resolve_input_driver(sysfs):
    """sysfs girdi düğümünden köke doğru yürüyerek bağlı çekirdek sürücüsünü bul."""
    if not sysfs:
        return None
    node = os.path.realpath("/sys" + sysfs)
    root = os.path.realpath("/sys/devices")
    seen = 0
    while node and node.startswith(root) and seen < 12:
        drv = os.path.join(node, "driver")
        if os.path.islink(drv):
            target = os.path.realpath(drv)
            # gerçek sürücü hedefi .../bus/<x>/drivers/<name> altında olur
            if "drivers" in target.split(os.sep):
                return os.path.basename(target)
        node = os.path.dirname(node)
        seen += 1
    return None


def touch_devices():
    """Dokunmatik/digitizer girdi cihazları + sürücü/bus bilgisi.

    'board' anahtarını bilerek kullanmıyoruz: 'keyboard' ile çakışır.
    """
    devs = []
    content = read_file("/proc/bus/input/devices") or ""
    blocks = content.split("\n\n")
    kw = re.compile(r"touch|stylus|digitizer|tablet|dokunmatik", re.I)
    excl = re.compile(r"keyboard", re.I)
    for blk in blocks:
        name = None
        handlers = None
        sysfs = None
        vendor = product = None
        for ln in blk.splitlines():
            if ln.startswith("N: Name="):
                name = ln.split("=", 1)[1].strip().strip('"')
            elif ln.startswith("H: Handlers="):
                handlers = ln.split("=", 1)[1].strip()
            elif ln.startswith("S: Sysfs="):
                sysfs = ln.split("=", 1)[1].strip()
            elif ln.startswith("I: "):
                mv = re.search(r"Vendor=(\w+)", ln)
                mp = re.search(r"Product=(\w+)", ln)
                vendor = mv.group(1) if mv else None
                product = mp.group(1) if mp else None
        if name and kw.search(name) and not excl.search(name):
            driver = _resolve_input_driver(sysfs)
            devs.append({"name": name, "handlers": handlers, "vendor": vendor,
                         "product": product, "driver": driver, "sysfs": sysfs})
    return devs


# ----------------------------------------------------------------------------
# Ana akış
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Ahenk kayıt/bağlantı teşhis aracı")
    ap.add_argument("--json", action="store_true", help="JSON çıktı")
    ap.add_argument("--no-net", action="store_true", help="ağ testlerini atla")
    ap.add_argument("--out", metavar="DOSYA", help="raporu dosyaya da yaz")
    ap.add_argument("--db", default=None, help="ahenk.db yolu (vars: conf'tan)")
    ap.add_argument("--mac", metavar="MAC", default=None,
                    help="ETA API'de sorgulanacak MAC (vars: bu makinenin kimlik MAC'i)")
    args = ap.parse_args()

    # --- ZORUNLU ROOT ---
    # Araç; ahenk.conf/messaging.conf (0600), parolalar, aktif Pulsar probe ve
    # süreç soketleri için root yetkisi ister. Root değilse çalıştırmayı reddet.
    if not is_root():
        sys.stderr.write(
            red("HATA: ") + "Bu araç root (sudo) yetkisiyle çalıştırılmalıdır.\n"
            "  ahenk.conf/messaging.conf (0600), parolalar, aktif bağlantı testi ve\n"
            "  süreç soketleri yalnızca root tarafından okunabilir.\n\n"
            "  Şu komutla yeniden çalıştırın:\n"
            "    sudo %s\n" % " ".join(sys.argv))
        return 3

    R = Report()
    D = R.data

    cp, conf_files, perm_err = load_config()
    root = is_root()

    srv_start = service_start_dt()

    # ---------------- 1) Genel / servis / yerel sağlık ----------------
    R.section("1) Genel Durum, Servis ve Yerel Sağlık")
    rc, pkg, _ = run("dpkg-query -W -f='${Version}' ahenk")
    D["ahenk_version"] = pkg if rc == 0 else None
    R.line("Ahenk paketi", pkg if rc == 0 else "tespit edilemedi",
           OK if rc == 0 else WARN)

    rc, active, _ = run("systemctl is-active ahenk.service")
    rc2, enabled, _ = run("systemctl is-enabled ahenk.service")
    D["service_active"] = active
    D["service_enabled"] = enabled
    sev = OK if active == "active" else FAIL
    R.line("Servis", "%s / %s" % (active, enabled), sev)
    if active != "active":
        R.finding(FAIL, "Ahenk servisi çalışmıyor (%s)" % active,
                  "Servis durduysa makine kesinlikle çevrimdışı görünür. Pulsar "
                  "bağlantısı 5 denemede kurulamazsa Ahenk kendini durdurur "
                  "(systemctl stop ahenk.service).\n"
                  "Komut: systemctl status ahenk.service  |  journalctl -u ahenk")
    if srv_start is not None:
        R.line("Başlangıç", srv_start.strftime("%Y-%m-%d %H:%M:%S"), INFO)

    # Servis crash-loop / sonuç
    nrestarts = service_restart_count()
    sresult = service_result()
    D["service_nrestarts"] = nrestarts
    D["service_result"] = sresult
    if nrestarts is not None:
        if nrestarts >= 3:
            R.line("Servis yeniden başlatma", "%d kez (DÖNGÜ?)" % nrestarts, FAIL)
            R.finding(FAIL, "Servis tekrar tekrar yeniden başlıyor (crash-loop)",
                      "NRestarts=%d. Ahenk başlıyor, hata alıp ölüyor ve systemd "
                      "yeniden başlatıyor. Başlangıç hatalarını inceleyin "
                      "(aşağıdaki 'mevcut oturum hataları' + journalctl)." % nrestarts)
        elif nrestarts > 0:
            R.line("Servis yeniden başlatma", "%d kez" % nrestarts, WARN)
        else:
            R.line("Servis yeniden başlatma", "0 (kararlı)", OK)
    if sresult and sresult != "success":
        R.line("Servis son sonucu", sresult, WARN)

    # Disk doluluğu (dolu disk DB/log yazımını ve kaydı bozar)
    for p in ("/", "/var"):
        du = disk_usage_info(p)
        if du:
            sev = OK
            if du["pct"] >= 95:
                sev = FAIL
            elif du["pct"] >= 90:
                sev = WARN
            R.line("Disk %s" % p, "%%%.0f kullanılıyor (%.1f GB boş)" %
                   (du["pct"], du["free"] / 1e9), sev)
            if du["pct"] >= 95:
                R.finding(FAIL, "Disk neredeyse dolu (%s %%%.0f)" % (p, du["pct"]),
                          "Dolu disk SQLite DB ve log yazımını engeller; kayıt ve "
                          "mesajlaşma bozulur.")
    D["disk"] = {p: disk_usage_info(p) for p in ("/", "/var")}

    # Mevcut servis oturumundaki hatalar (başlangıç/ölümcül)
    sess_err = scan_session_errors(srv_start)
    D["session_errors"] = sess_err
    if sess_err["available"]:
        if sess_err["count"] == 0:
            R.line("Mevcut oturum hataları", "yok (ConsumerBusy hariç)", OK)
        else:
            R.line("Mevcut oturum hataları", "%d ERROR/ölümcül satır" % sess_err["count"],
                   WARN)
            for s in sess_err["samples"]:
                R.note(s)
            # ImportError/ModuleNotFound gibi başlangıç çökmeleri ölümcüldür
            if any(re.search(r"ImportError|ModuleNotFound|No module named|Traceback", s, re.I)
                   for s in sess_err["samples"]):
                R.finding(FAIL, "Mevcut oturumda başlangıç/bağımlılık hatası",
                          "Loglarda ImportError/Traceback türü hata var; Ahenk düzgün "
                          "başlayamıyor olabilir.")
            else:
                R.finding(WARN, "Mevcut oturumda hata kayıtları var",
                          "Servis bu oturumda %d hata satırı üretmiş; ayrıntılar "
                          "yukarıda." % sess_err["count"])

    R.line("Yetki", "root (tam erişim)", OK)

    # ---------------- 2) Kimlik ----------------
    R.section("2) Kimlik (UUID / Parola / Kayıt)")
    db_path = args.db or cfg(cp, "BASE", "dbPath", DEFAULT_DB)
    db = load_db(db_path)
    D["db"] = {"path": db_path, "ok": db["ok"], "error": db["error"]}

    # DB bütünlük kontrolü (bozuk/kilitli DB Ahenk'i sekteye uğratır)
    integ = db_integrity(db_path)
    D["db"]["integrity"] = integ
    if integ == "ok":
        R.line("DB bütünlüğü", "ok (%s)" % db_path, OK)
    elif integ:
        R.line("DB bütünlüğü", "%s" % integ, FAIL)
        R.finding(FAIL, "Ahenk veritabanı sorunlu (integrity=%s)" % integ,
                  "Bozuk/erişilemez SQLite DB; kayıt ve mesajlaşma durumu okunamaz. "
                  "Gerekirse 'ahenkd.py clean' ile sıfırlanıp yeniden kayıt yapılmalı.")

    conf_uid = cfg(cp, "CONNECTION", "uid")
    conf_pass = cfg(cp, "CONNECTION", "password")
    db_reg = db.get("registration")

    if db["ok"] and db_reg:
        D["uuid"] = db_reg["jid"]
        D["registered"] = db_reg["registered"]
        D["dn"] = db_reg["dn"]
        R.line("UUID (jid, DB)", db_reg["jid"], INFO)
        R.line("Parola (DB)", mask(db_reg["password"]), INFO)
        R.line("registered", str(db_reg["registered"]),
               OK if str(db_reg["registered"]) == "1" else WARN)
        dn = (db_reg["dn"] or "").strip()
        registered_flag = str(db_reg["registered"]) == "1"
        D["ldap_registered"] = bool(dn)
        if dn:
            R.line("LDAP dn", dn, OK)
            R.line("Kayıt türü", "Tam kayıt (LDAP dizin nesnesi var)", OK)
        elif registered_flag:
            # registered=1 ama dn boş => Lider 'registered_without_ldap' döndürmüş;
            # mesajlaşma çalışır, dizin (LDAP) nesnesi yoktur.
            D["registration_kind"] = "registered_without_ldap"
            R.line("LDAP dn", "(boş)", WARN)
            R.line("Kayıt türü", "registered_without_ldap (LDAP'sız kayıt)", WARN)
            R.finding(WARN, "Kayıt 'registered_without_ldap' — LDAP dizin nesnesi yok",
                      "Tahta Pulsar mesajlaşması için KAYITLI (registered=1) ama Lider "
                      "kayıt yanıtında boş 'agentDn' döndürmüş; LDAP dizininde bu ajan "
                      "için bir nesne (DN) yok.\n"
                      "ETKİ: Çevrimiçilik, komut topic'ine subscribe ve görev (task) "
                      "yürütme ÇALIŞIR. Ancak LDAP DİZİN AĞACINA dayalı politikalar "
                      "(OU/grup/DN'e bağlı profil ve politikalar) bu makineye "
                      "UYGULANMAZ.\n"
                      "NEDEN: İstemci/donanım kaynaklı değildir; kayıt anında Lider'in "
                      "LDAP'a erişememesi, agent OU/DN yolunun yapılandırılmamış olması "
                      "veya kurulumun bilinçli olarak LDAP'sız olması. Bu Ahenk "
                      "sürümünde kayıt sonrası LDAP tamamlama adımı (ahenkd.py) "
                      "yorum satırıdır; dn kendiliğinden dolmaz.\n"
                      "KONTROL: Lider tarafında ajanın LDAP ağacında (agents/ajanlar "
                      "OU'su altında) görünüp görünmediğine bakın. Görünmüyorsa Lider'in "
                      "LDAP entegrasyonu ve agent-OU ayarını kontrol edin. Yeniden kayıt "
                      "(ahenkd.py clean + restart) YALNIZCA Lider bu kez dolu bir "
                      "agentDn döndürürse dn'yi doldurur.\n"
                      "NOT: Dizin-tabanlı politika kullanılmıyorsa (yalnız task/Pulsar "
                      "yönetimi) bu uyarı pratikte zararsızdır.")
        else:
            # registered=0 ve dn boş => kayıt hiç tamamlanmamış (farklı/daha ciddi durum)
            R.line("LDAP dn", "(boş) — kayıt tamamlanmamış", FAIL)
            R.finding(FAIL, "Kayıt tamamlanmamış (registered=0, dn boş)",
                      "Ne LDAP nesnesi var ne de registered=1. Tahta Lider'e hiç "
                      "kaydolamamış olabilir; kayıt akışını (ağ + Lider yanıtı) inceleyin.")
        R.line("Kayıt zamanı", db_reg["timestamp"], INFO)
    else:
        R.line("DB", "okunamadı: %s" % (db["error"] or "?"),
               FAIL if root else WARN)

    # conf <-> db tutarlılığı
    if root and conf_uid is not None and db_reg:
        if conf_uid != db_reg["jid"]:
            R.line("conf.uid vs db.jid", "UYUŞMUYOR", FAIL)
            R.finding(FAIL, "ahenk.conf UID ile DB JID farklı",
                      "conf=%s\ndb =%s\nBağlantı conf'taki UID ile yapılır; DB kaydı "
                      "ile uyuşmazsa kimlik tutarsızdır." % (conf_uid, db_reg["jid"]))
        else:
            R.line("conf.uid == db.jid", "tutarlı", OK)
        if conf_pass and conf_pass != db_reg["password"]:
            R.line("conf.password vs db", "UYUŞMUYOR", WARN)
            R.finding(WARN, "conf parolası ile DB parolası farklı",
                      "Yeniden kayıt yarıda kalmış olabilir.")
    elif not root and conf_uid is None:
        R.note("conf.uid root olmadan okunamadı")

    # mesajlaşma topic
    if db.get("messaging"):
        topic = db["messaging"]["topic_name"]
        R.line("Topic (DB)", topic, INFO)
    uid_for_topic = (db_reg["jid"] if db_reg else conf_uid)
    if uid_for_topic:
        R.line("Pulsar topic", "task-%s" % uid_for_topic, INFO)
        R.line("Pulsar abonelik", "ahenk-%s (ConsumerType.Exclusive)" % uid_for_topic, INFO)
        R.note("Exclusive: aynı UUID'yi ikinci bir makine kullanırsa ConsumerBusy alır.")

    # ---------------- 3) Kimlik MAC'i + klon tespiti ----------------
    R.section("3) Kimlik MAC'i, Klon/Çakışma ve Canlı Bağlantı")
    nics = enumerate_nics()
    D["nics"] = nics
    ident = compute_identity_mac(nics)
    eta = reproduce_etainfo()
    D["identity_mac"] = ident["mac"] if ident else None
    D["etainfo"] = eta

    for n in nics:
        flags = []
        if n["wireless"]:
            flags.append("kablosuz")
        if not n["has_driver"]:
            flags.append("sürücüsüz")
        tag = (" [%s]" % ",".join(flags)) if flags else ""
        R.line(n["interface"],
               "%s  drv=%s bus=%s %s%s" % (n["mac"], n["driver"], n["bus"],
                                           n["operstate"] or "?", tag), INFO)

    if ident:
        R.line("KİMLİK MAC'i", "%s  (%s, drv=%s)" %
               (ident["mac"], ident["interface"], ident["driver"]), OK)
    else:
        R.line("KİMLİK MAC'i", "BELİRLENEMEDİ (uygun PCI kablolu ethernet yok)", FAIL)
        R.finding(FAIL, "Kimlik MAC'i çözülemiyor",
                  "Ahenk kimliği ilk PCI/sürücülü/kablosuz-olmayan arayüzün MAC'ine "
                  "dayanır. Sadece USB/WiFi adaptör varsa etainfo.network.get() None "
                  "döner ve KAYIT AŞAMASINDA ÇÖKER. Kablolu ethernet sürücüsünü "
                  "(ör. r8169) ve bağlantıyı kontrol edin.")

    # etainfo'nun gerçek sonucu (Ahenk'in kullandığı)
    if eta["ok"]:
        R.line("etainfo.network", "%s (%s)" % (eta["mac"], eta.get("interface")), OK)
        if ident and eta["mac"] and ident["mac"] != eta["mac"]:
            R.finding(WARN, "Hesaplanan MAC ile etainfo MAC farklı",
                      "Arayüz sıralaması/sürücü durumu beklenenden farklı olabilir.")
    else:
        R.line("etainfo.network", "HATA: %s" % eta["error"], FAIL)
        R.finding(FAIL, "etainfo.network kimlik MAC'i veremiyor",
                  "Ahenk'in kayıt sırasında kullandığı kod bu makinede başarısız: %s\n"
                  "Bu, kaydın hiç tamamlanamamasına yol açar." % eta["error"])

    # Klon tespiti: DB kayıt anındaki MAC vs şu anki canlı kimlik MAC
    reg_mac = None
    if db_reg and isinstance(db_reg["params"], dict):
        reg_mac = db_reg["params"].get("macAddresses")
    live_mac = (ident["mac"] if ident else (eta["mac"] if eta["ok"] else None))
    D["registered_mac"] = reg_mac
    D["live_mac"] = live_mac
    if reg_mac and live_mac:
        if reg_mac.lower() == live_mac.lower():
            R.line("Kayıt MAC == canlı MAC", "%s (tutarlı)" % live_mac, OK)
        else:
            R.line("Kayıt MAC vs canlı MAC", "FARKLI", FAIL)
            R.finding(FAIL, "KLON İMAJ ŞÜPHESİ: kayıttaki MAC bu donanımla uyuşmuyor",
                      "Kayıt anı MAC=%s, şu anki donanım MAC=%s.\n"
                      "Bu UUID/parola başka bir makinede üretilip imaj olarak "
                      "kopyalanmış olabilir. Lider bu UUID'yi başka MAC ile tanır; "
                      "çakışma/çevrimdışı olur.\nÇÖZÜM: bu makinede yeniden kayıt "
                      "(ahenk clean + servis restart) ile yeni UUID üretin."
                      % (reg_mac, live_mac))

    # --- CANLI BAĞLANTI DOĞRULAMASI (statik log değil, gerçek durum) ---
    logs = analyze_logs()
    D["logs"] = {k: v for k, v in logs.items() if isinstance(v, dict)}
    live = live_broker_connections(cp)
    D["live_connection"] = live
    # En son başarılı bağlantı/iletişim olayı (datetime)
    success_dts = [logs[k]["last_dt"] for k in
                   ("pulsar_connected", "publish_ok", "received", "reg_success")
                   if logs[k]["last_dt"]]
    last_success = max(success_dts) if success_dts else None
    cb = logs["consumer_busy"]
    cb_dt = cb["last_dt"]

    # Canlı TCP kanıtı
    if live["count"] > 0:
        owner = ""
        if live["owned_by_ahenk"] is True:
            owner = " (ahenk PID %s'e ait)" % live["pid"]
        elif live["owned_by_ahenk"] is None:
            owner = " (sahip için root gerekli)"
        peers = ", ".join(sorted({e["peer"] for e in live["established"]}))
        R.line("Canlı broker bağlantısı", "%d ESTABLISHED → %s%s" %
               (live["count"], peers, owner), OK)
    else:
        detail = "yok"
        if live["error"]:
            detail += " (%s)" % live["error"]
        sev = WARN if not live["host"] else FAIL
        R.line("Canlı broker bağlantısı", detail, sev)

    # Bağlantı CANLI mı? (çoklu kanıt)
    conn_live = (live["count"] > 0) or \
                (last_success is not None and (cb_dt is None or last_success >= cb_dt) and
                 (srv_start is None or last_success >= srv_start))

    # ConsumerBusy değerlendirmesi — bayat mı, güncel mi?
    if logs["available"] and cb["count"] > 0:
        cb_stale = False
        reasons = []
        if srv_start and cb_dt and cb_dt < srv_start:
            cb_stale = True
            reasons.append("son ConsumerBusy (%s) servis yeniden başlamadan (%s) önce"
                           % (cb["last"], srv_start.strftime("%H:%M:%S")))
        if last_success and cb_dt and last_success > cb_dt:
            cb_stale = True
            reasons.append("ConsumerBusy'den sonra başarılı Pulsar olayı var (%s)"
                           % last_success.strftime("%Y-%m-%d %H:%M:%S"))
        if live["count"] > 0:
            reasons.append("şu an broker'a aktif TCP bağlantısı mevcut")

        if cb_stale and conn_live:
            R.line("ConsumerBusy (log)", "%d kez ama BAYAT (son: %s)" %
                   (cb["count"], cb["last"]), OK)
            R.finding(OK, "Geçmiş ConsumerBusy GÜNCEL DEĞİL — bağlantı şu an sağlıklı",
                      "Loglardaki ConsumerBusy kayıtları eski bir oturuma ait ve artık "
                      "geçerli değil:\n- " + "\n- ".join(reasons) +
                      "\nKanıtlar bu makinenin komut topic'ine bağlı olduğunu gösteriyor.")
        elif cb_stale and not conn_live:
            R.line("ConsumerBusy (log)", "%d kez, bayat görünüyor ama canlı bağlantı "
                   "doğrulanamadı (son: %s)" % (cb["count"], cb["last"]), WARN)
            R.finding(WARN, "Eski ConsumerBusy var; güncel bağlantı doğrulanamadı",
                      "Hata eski oturumdan ama şu an broker bağlantısı teyit edilemedi. "
                      "Ağ testlerini --no-net'siz çalıştırın; aktif Pulsar testi "
                      "(varsayılan) bağlantıyı kesinleştirir.")
        else:
            # Güncel/aktif ConsumerBusy
            R.line("ConsumerBusy (log)", "%d kez, GÜNCEL (son: %s)" %
                   (cb["count"], cb["last"]), FAIL)
            R.finding(FAIL, "AKTİF KLON/ÇAKIŞMA: Pulsar ConsumerBusy sürüyor",
                      "Aynı 'ahenk-%s' Exclusive aboneliğini başka bir subscriber "
                      "tutuyor; bu makine komut topic'ini DİNLEYEMİYOR → Lider'de "
                      "çevrimdışı/yanıtsız. ConsumerBusy mevcut servis oturumunda ve "
                      "sonrasında başarılı bir bağlantı kanıtı yok. En olası neden: "
                      "KLONLANMIŞ İMAJ (aynı UUID birden çok makinede).\nÇÖZÜM: çakışan "
                      "diğer makineyi bulun ya da bu makineyi temizleyip yeniden "
                      "kaydedin (yeni UUID)." % (uid_for_topic or "<uid>"))
    elif logs["available"]:
        R.line("ConsumerBusy (log)", "yok", OK)

    # not_authorized — yine bayatlık-duyarlı
    if logs["available"]:
        na = logs["not_authorized"]
        if na["count"] > 0:
            na_stale = (srv_start and na["last_dt"] and na["last_dt"] < srv_start) or \
                       (last_success and na["last_dt"] and last_success > na["last_dt"])
            if na_stale:
                R.line("not_authorized (log)", "%d kez ama bayat (son: %s)" %
                       (na["count"], na["last"]), INFO)
            else:
                R.line("not_authorized/registration_error", "%d kez, son: %s" %
                       (na["count"], na["last"]), WARN)
                R.finding(WARN, "Güncel kayıt reddi (not_authorized)",
                          "Lider kaydı reddetmiş; Ahenk yeni UUID ile yeniden deniyor. "
                          "Parola/UUID çakışması veya Lider tarafı kuralları olabilir.")

    # Genel canlı bağlantı yargısı
    if conn_live:
        R.finding(OK, "Mesajlaşma bağlantısı ŞU AN canlı görünüyor",
                  "Kanıt: %s" % (
                      ("%d aktif TCP + " % live["count"] if live["count"] else "") +
                      ("son başarılı olay %s" % last_success.strftime("%Y-%m-%d %H:%M:%S")
                       if last_success else "log/TCP kanıtı")))
    if not logs["available"]:
        R.line("Log", "/var/log/ahenk.log okunamadı (root gerekli)", WARN)

    # --- AKTİF BAĞLANTI DOĞRULAMASI (varsayılan; --no-net ile atlanır) ---
    _mtype = (cfg(cp, "MESSENGER", "messenger_type", "xmpp") or "xmpp").lower()
    if args.no_net:
        R.line("Aktif bağlantı testi", "--no-net ile atlandı", INFO)
    elif _mtype != "pulsar":
        R.line("Aktif bağlantı testi", "yalnız Pulsar için geçerli (messenger=%s)" % _mtype, INFO)
    else:
        R.line("Aktif Pulsar testi", "deneniyor (test-topic-lider'e producer)...", INFO)
        probe = active_pulsar_probe(cp)
        D["active_probe"] = probe
        if probe.get("skipped"):
            R.line("Aktif probe sonucu", "ATLANDI: %s" % probe["error"], WARN)
        elif probe["ok"]:
            R.line("Aktif probe sonucu", "BAŞARILI — broker+TLS+kimlik doğrulama çalışıyor", OK)
            R.finding(OK, "Aktif bağlantı testi başarılı",
                      "Gerçek uid/parola ile broker'a bağlanıldı ve test mesajı "
                      "yollandı. DNS/TCP/TLS/kimlik doğrulama şu an sağlıklı. "
                      "(Komut aboneliğine dokunulmadı.)")
        else:
            sev = FAIL
            extra = ""
            if probe["auth_ok"] is False:
                extra = "KİMLİK DOĞRULAMA reddedildi — uid/parola Lider/broker'da geçersiz. "
            R.line("Aktif probe sonucu", "BAŞARISIZ (%s): %s" %
                   (probe.get("stage"), probe["error"]), sev)
            R.finding(sev, "Aktif bağlantı testi başarısız",
                      "%sAşama: %s\nHata: %s" %
                      (extra, probe.get("stage"), probe["error"]))

    # ---------------- 4) Bağlantı / Temel TCP / DNS ----------------
    R.section("4) Bağlantı, Temel TCP ve DNS")
    messenger_type = (cfg(cp, "MESSENGER", "messenger_type", "xmpp") or "xmpp").lower()
    D["messenger_type"] = messenger_type
    R.line("messenger_type", messenger_type, INFO)

    # --- Temel ağ katmanı: arayüz linki ---
    nb = {"net_basics": {}}
    ident_iface = ident["interface"] if ident else None
    link = nic_link_status(ident_iface)
    nb["net_basics"]["link"] = link
    if link:
        carrier_ok = link["carrier"] == "1"
        up = link["operstate"] == "up"
        sev = OK if (carrier_ok and up and link["has_ip"]) else FAIL
        R.line("Kablolu arayüz (%s)" % ident_iface,
               "link=%s oper=%s ip=%s hız=%s" %
               ("var" if carrier_ok else "YOK", link["operstate"],
                link["ip"] or "YOK", (link["speed"] or "?") + "Mb/s"), sev)
        if not carrier_ok:
            R.finding(FAIL, "Kablolu ağ bağlantısı (carrier) yok",
                      "%s arayüzünde kablo/link yok. Hiçbir sunucuya ulaşılamaz." % ident_iface)
        elif not link["has_ip"]:
            R.finding(FAIL, "Kimlik arayüzünde IPv4 adresi yok",
                      "%s 'up' ama IP almamış (DHCP sorunu?). Bağlantı kurulamaz." % ident_iface)

    gw = default_gateway()
    gw_ip = None
    if gw:
        m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", gw)
        gw_ip = m.group(1) if m else None
    R.line("Varsayılan ağ geçidi", gw or "YOK", OK if gw else FAIL)
    if not gw:
        R.finding(FAIL, "Varsayılan ağ geçidi yok", "Makinenin ağ bağlantısı kopuk.")

    # --- DNS yeteneği ---
    nameservers = resolv_conf_nameservers()
    nb["net_basics"]["nameservers"] = nameservers
    R.line("DNS sunucuları", ", ".join(nameservers) if nameservers else "TANIMLI DEĞİL",
           OK if nameservers else WARN)
    if not nameservers:
        R.finding(WARN, "resolv.conf'ta nameserver yok",
                  "DNS çözümlemesi yapılamaz; broker adı IP'ye çevrilemez.")

    if args.no_net:
        R.line("Ağ testleri", "--no-net ile atlandı (link/DNS dışı)", INFO)
        # Temel TCP testleri ağ gerektirir; atla
        gw = gw  # no-op
    else:
        # --- Temel TCP erişilebilirlik testleri ---
        # 1) Ağ geçidine ping (L3 ulaşılabilirlik)
        if gw_ip:
            pong = ping_host(gw_ip)
            R.line("Ağ geçidi ping (%s)" % gw_ip, "yanıt var" if pong else "yanıt YOK",
                   OK if pong else WARN)
            if not pong:
                R.note("Ping ICMP ile engellenmiş olabilir; TCP testleri yine de geçerli.")

        # 2) Broker'a DOĞRUDAN TCP — config yoksa canlı soketlerden keşfet
        broker_targets = []
        bhost = cfg(cp, "PULSAR", "pulsar_host") or cfg(cp, "CONNECTION", "host")
        bport = cfg(cp, "PULSAR", "pulsar_port") or cfg(cp, "CONNECTION", "port")
        if bhost and bport:
            bip = dns_resolve(bhost) or bhost
            broker_targets.append((bhost, bip, bport, "config"))
        else:
            for ip, port in discover_broker_endpoints():
                broker_targets.append((ip, ip, port, "canlı soket"))
        nb["net_basics"]["broker_targets"] = [
            {"host": h, "ip": i, "port": p, "src": s} for h, i, p, s in broker_targets]
        if broker_targets:
            tested = set()
            any_open = False
            for host, bip, port, src in broker_targets:
                key = (bip, port)
                if key in tested:
                    continue
                tested.add(key)
                ok, ms, err = tcp_latency(bip, port)
                if ok:
                    any_open = True
                    R.line("Broker TCP %s:%s" % (host, port),
                           "AÇIK (%.0f ms, %s)" % (ms, src), OK)
                else:
                    R.line("Broker TCP %s:%s" % (host, port),
                           "KAPALI — %s (%s)" % (err, src), FAIL)
                    R.finding(FAIL, "Broker TCP portuna ulaşılamıyor (%s:%s)" % (host, port),
                              "Temel TCP bağlantısı kurulamadı: %s\nGüvenlik duvarı, "
                              "yanlış adres/port veya sunucu kapalı olabilir. Ahenk bu "
                              "porta bağlanamazsa Lider'e subscribe OLAMAZ." % err)
            nb["net_basics"]["broker_tcp_open"] = any_open
        else:
            R.line("Broker TCP", "hedef belirlenemedi (config root gerekli, canlı soket yok)",
                   WARN)
            R.finding(WARN, "Broker uç noktası saptanamadı",
                      "Ne config okunabildi ne de canlı broker soketi bulundu. "
                      "Bağlanma denemesi olup olmadığını anlamak için sudo ile çalıştırın.")
    D["net_basics"] = nb["net_basics"]

    if args.no_net:
        pass
    else:
        if messenger_type == "pulsar":
            host = cfg(cp, "PULSAR", "pulsar_host")
            port = cfg(cp, "PULSAR", "pulsar_port")
            use_tls = (cfg(cp, "PULSAR", "pulsar_use_tls", "false") or "false").lower() == "true"
            ca = cfg(cp, "PULSAR", "tls_trust_certs_file_path")
            D["pulsar"] = {"host": host, "port": port, "tls": use_tls, "ca": ca}
            if host and port:
                R.line("Pulsar hedefi", "%s:%s (tls=%s)" % (host, port, use_tls), INFO)
                ip = dns_resolve(host)
                R.line("DNS", "%s -> %s" % (host, ip) if ip else "%s ÇÖZÜLEMEDİ" % host,
                       OK if ip else FAIL)
                if not ip:
                    R.finding(FAIL, "Pulsar sunucu adı DNS ile çözülemiyor",
                              "host=%s — DNS/hosts ayarlarını kontrol edin." % host)
                else:
                    ok, err = tcp_check(ip, port)
                    R.line("TCP %s:%s" % (ip, port), "açık" if ok else "KAPALI (%s)" % err,
                           OK if ok else FAIL)
                    if not ok:
                        R.finding(FAIL, "Pulsar portuna TCP bağlantı kurulamıyor",
                                  "%s:%s erişilemez. Güvenlik duvarı/sunucu kapalı?\n%s"
                                  % (host, port, err))
                    if ok and use_tls:
                        tok, terr, dn_len = tls_check(ip, port, ca)
                        R.line("TLS el sıkışma", "başarılı" if tok else "BAŞARISIZ (%s)" % terr,
                               OK if tok else FAIL)
                        if not tok:
                            R.finding(FAIL, "Pulsar TLS el sıkışması başarısız",
                                      "Sertifika/protokol uyuşmazlığı olabilir: %s" % terr)
                    if use_tls and ca:
                        exp = cert_expiry(ca)
                        if os.path.exists(ca):
                            R.line("TLS trust cert", "%s (bitiş: %s)" % (ca, exp or "?"),
                                   OK)
                        else:
                            R.line("TLS trust cert", "DOSYA YOK: %s" % ca, FAIL)
                            R.finding(FAIL, "Pulsar trust sertifika dosyası yok",
                                      "tls_trust_certs_file_path=%s bulunamadı." % ca)
            else:
                R.line("Pulsar config", "host/port okunamadı (root?)",
                       WARN if not root else FAIL)

            # kayıt (register) ucu
            reg_url = cfg(cp, "REGISTRATION", "registration_url")
            if reg_url:
                rhost = reg_url.split("/")[0].split(":")[0]
                rport = "443"
                if ":" in reg_url.split("/")[0]:
                    rport = reg_url.split("/")[0].split(":")[1]
                ip = dns_resolve(rhost)
                ok, err = (tcp_check(ip, rport) if ip else (False, "DNS yok"))
                R.line("Kayıt ucu", "%s -> %s:%s %s" %
                       (reg_url, ip, rport, "açık" if ok else "KAPALI"),
                       OK if ok else WARN)
        else:
            host = cfg(cp, "CONNECTION", "host")
            port = cfg(cp, "CONNECTION", "port", "5222")
            D["xmpp"] = {"host": host, "port": port}
            if host:
                ip = dns_resolve(host)
                R.line("XMPP DNS", "%s -> %s" % (host, ip) if ip else "ÇÖZÜLEMEDİ",
                       OK if ip else FAIL)
                if ip:
                    ok, err = tcp_check(ip, port)
                    R.line("XMPP TCP %s:%s" % (host, port),
                           "açık" if ok else "KAPALI (%s)" % err, OK if ok else FAIL)
            else:
                R.line("XMPP config", "host okunamadı (root?)", WARN)

    # log: son başarılı yayın / hata
    if logs["available"]:
        if logs["publish_ok"]["last"]:
            R.line("Son başarılı yayın", logs["publish_ok"]["last"], OK)
        if logs["pulsar_conn_fail"]["count"]:
            R.line("Pulsar bağlantı hataları", "%d kez, son: %s" %
                   (logs["pulsar_conn_fail"]["count"], logs["pulsar_conn_fail"]["last"]),
                   WARN)
        if logs["last_error"]:
            R.note("Son ERROR: %s" % logs["last_error"])

    # ---------------- 5) ETA Kayıt Sunucusu (Okul/Şehir/İlçe) ----------------
    R.section("5) ETA Kayıt Sunucusu — Okul / Şehir / İlçe")
    eta_backend, eta_header = load_eta_config()
    R.line("API tabanı", eta_backend, INFO)
    query_mac = args.mac or live_mac
    if args.mac:
        R.note("--mac ile verilen MAC sorgulanıyor: %s" % args.mac)
    D["eta_api"] = {"backend": eta_backend, "query_mac": query_mac}

    if args.no_net:
        R.line("ETA sorgusu", "--no-net ile atlandı", INFO)
    elif not query_mac:
        R.line("ETA sorgusu", "kimlik MAC'i yok — sorgu yapılamadı", FAIL)
    else:
        R.line("Sorgu MAC", query_mac, INFO)
        eta = query_eta_board(query_mac, eta_backend, eta_header)
        D["eta_api"]["result"] = {k: v for k, v in eta.items() if k != "raw"}
        if eta["error"] and eta["registered"] is None:
            R.line("ETA API", "ERİŞİLEMEDİ: %s" % eta["error"], FAIL)
            R.finding(FAIL, "ETA kayıt sunucusuna ulaşılamadı",
                      "%s\nLider, Ahenk'i doğrularken bu API'ye sorar. API "
                      "erişilemezse kayıt doğrulaması yapılamaz." % eta["error"])
        else:
            reg = eta["registered"]
            R.line("HTTP durumu", str(eta["status"]), INFO)
            if reg is True:
                R.line("Kayıt durumu", "KAYITLI ✓", OK)
            elif reg is False:
                R.line("Kayıt durumu", "KAYITLI DEĞİL ✗", FAIL)
                R.finding(FAIL, "Tahta ETA API'de KAYITLI DEĞİL",
                          "MAC=%s bu API'de kayıtlı görünmüyor. Lider, Ahenk "
                          "bağlanınca bu sunucuya sorar; 'kayıtlı değil' yanıtı "
                          "gelince kayıt İLERLEMEZ → makine çevrimdışı kalır.\n"
                          "ÇÖZÜM: eta-register ile okul/il/ilçe seçilerek tahta "
                          "kaydedilmeli." % query_mac)
            else:
                R.line("Kayıt durumu", "belirsiz (registered=%s)" % reg, WARN)

            if eta.get("registered_ip") is not None:
                R.line("registered_ip", str(eta["registered_ip"]),
                       OK if eta["registered_ip"] else WARN)

            data = eta.get("data") or {}
            if data:
                R.line("Şehir", "%s (id=%s)" % (data.get("city_name"),
                       data.get("city_id")), INFO)
                R.line("İlçe", "%s (id=%s)" % (data.get("town_name"),
                       data.get("town_id")), INFO)
                R.line("Okul", "%s" % data.get("school_name"), INFO)
                R.line("Okul kodu", str(data.get("school_code")), INFO)
                R.line("Birim (unit_name)", str(data.get("unit_name")), INFO)
                R.line("board_id (Lider eşlemesi)", str(data.get("board_id")), INFO)
                api_phase = data.get("phase")
                if api_phase:
                    R.line("API faz bilgisi", str(api_phase), INFO)
                    D["eta_api"]["phase"] = api_phase
                D["school"] = {
                    "city": data.get("city_name"), "town": data.get("town_name"),
                    "school": data.get("school_name"), "code": data.get("school_code"),
                    "unit_name": data.get("unit_name"), "board_id": data.get("board_id"),
                }
                if reg is True:
                    R.finding(OK, "Tahta kaydı: %s / %s / %s" %
                              (data.get("city_name"), data.get("town_name"),
                               data.get("school_name")),
                              "Okul kodu %s · birim '%s' · board_id %s" %
                              (data.get("school_code"), data.get("unit_name"),
                               data.get("board_id")))
            elif reg is True:
                R.note("Kayıtlı ama 'data' alanı boş döndü.")

    # ---------------- 6) Sistem / Dağıtım / Çekirdek ----------------
    R.section("6) Sistem / Dağıtım / Çekirdek")
    distro = None
    osrel = read_file("/etc/os-release") or ""
    m = re.search(r'PRETTY_NAME="?([^"\n]+)', osrel)
    distro = m.group(1) if m else platform.platform()
    D["distro"] = distro
    D["kernel"] = platform.release()
    D["arch"] = platform.machine()
    R.line("Dağıtım", distro, INFO)
    rc, lsb, _ = run("lsb_release -d -s")
    if rc == 0 and lsb:
        R.line("lsb_release", lsb, INFO)
    R.line("Çekirdek", platform.release(), INFO)
    R.line("Mimari", "%s / %s" % (platform.machine(), platform.architecture()[0]), INFO)
    R.line("Hostname", platform.node(), INFO)
    rc, sync, _ = run("timedatectl show -p NTPSynchronized --value")
    R.line("Saat senkron (NTP)", sync if rc == 0 else "?",
           OK if sync == "yes" else WARN)
    if rc == 0 and sync != "yes":
        R.finding(WARN, "Sistem saati NTP ile senkron değil",
                  "Saat kayması TLS sertifika doğrulamasını ve zaman damgalarını bozar.")

    # ---------------- 7) Donanım / Faz / Dokunmatik ----------------
    R.section("7) Donanım, Faz ve Dokunmatik")
    brand = cpu_brand()
    board_vendor = readline_strip(os.path.join(DMI, "board_vendor"))
    board_name = readline_strip(os.path.join(DMI, "board_name"))
    product = readline_strip(os.path.join(DMI, "product_name"))
    bios_ver = readline_strip(os.path.join(DMI, "bios_version"))
    bios_date = readline_strip(os.path.join(DMI, "bios_date"))
    mem_total = None
    mi = read_file("/proc/meminfo") or ""
    mm = re.search(r"MemTotal:\s+(\d+)", mi)
    if mm:
        mem_total = "%d MB" % (int(mm.group(1)) // 1024)

    D["cpu"] = brand
    D["board"] = {"vendor": board_vendor, "name": board_name, "product": product}
    D["bios"] = {"version": bios_ver, "date": bios_date}
    D["memory"] = mem_total

    R.line("İşlemci", brand or "?", INFO)
    R.line("Anakart", "%s / %s (%s)" % (board_vendor, board_name, product), INFO)
    R.line("BIOS", "%s (%s)" % (bios_ver, bios_date or "?"), INFO)
    R.line("RAM", mem_total or "?", INFO)

    gpus = gpu_info()
    D["gpu"] = gpus
    if gpus:
        for g in gpus:
            R.line("GPU", "ven=%s dev=%s drv=%s" % (g["vendor"], g["device"], g["driver"]),
                   INFO)
    else:
        R.line("GPU", "tespit edilemedi", WARN)

    phase, why = detect_phase(brand, board_vendor)
    D["phase"] = phase
    R.line("FAZ tahmini (yerel)", phase, OK if phase.startswith("Faz") else WARN)
    R.note(why)
    api_phase = (D.get("eta_api") or {}).get("phase")
    if api_phase:
        R.line("FAZ (ETA API)", str(api_phase), INFO)
        R.note("ETA kayıt sunucusunun tahta için tuttuğu faz bilgisi.")

    touches = touch_devices()
    D["touch"] = touches
    if touches:
        for t in touches:
            vp = ""
            if t["vendor"] or t["product"]:
                vp = " [%s:%s]" % (t["vendor"], t["product"])
            R.line("Dokunmatik", "%s%s  drv=%s handlers=%s" %
                   (t["name"], vp, t["driver"], t["handlers"]), INFO)
    else:
        R.line("Dokunmatik", "tespit edilemedi (USB/HID girdi cihazı yok?)", WARN)

    # ---------------- Olumlu özet ----------------
    if not any(f[0] == FAIL for f in R.findings):
        if active == "active" and logs.get("consumer_busy", {}).get("count", 0) == 0:
            R.finding(OK, "Kritik hata bulunamadı",
                      "Servis çalışıyor, kimlik tutarlı, ConsumerBusy yok. "
                      "Çevrimdışı görünüm geçici ağ kesintisi kaynaklı olabilir.")

    # ---------------- Çıktı ----------------
    if args.json:
        D["findings"] = [{"severity": s, "title": t, "detail": d}
                         for s, t, d in R.findings]
        text = json.dumps(D, indent=2, ensure_ascii=False, default=str)
        print(text)
    else:
        text = R.render()
        print(text)

    if args.out:
        try:
            with open(args.out, "w") as f:
                # dosyaya renksiz yaz
                global _USE_COLOR
                _USE_COLOR = False
                f.write(R.render() if not args.json else
                        json.dumps(D, indent=2, ensure_ascii=False, default=str))
            sys.stderr.write("\nRapor yazıldı: %s\n" % args.out)
        except Exception as e:
            sys.stderr.write("Rapor yazılamadı: %s\n" % e)

    # çıkış kodu: FAIL varsa 2, WARN varsa 1, yoksa 0
    if any(f[0] == FAIL for f in R.findings):
        return 2
    if any(f[0] == WARN for f in R.findings):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
