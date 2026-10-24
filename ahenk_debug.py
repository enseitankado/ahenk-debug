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
import gzip
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

# Raporu sunucusuz olarak operatöre ulaştırma kanalları (varsayılan: açık).
# Rapor metni paste.rs'e yüklenir (kısa URL), özet+link ntfy.sh konusuna itilir.
# Konu adı AHENK_DEBUG_NTFY ortam değişkeniyle değiştirilebilir; abone olan operatör
# tüm raporları telefondan/webden anında alır.
PASTE_URL = "https://paste.rs"
NTFY_BASE = "https://ntfy.sh"
NTFY_TOPIC_DEFAULT = "ahenk-debug"

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
# Renk yalnız terminalde; NO_COLOR (https://no-color.org) ve TERM=dumb
# renkleri kapatır, --renk ile zorlanabilir.
_USE_COLOR = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")
              and os.environ.get("TERM", "") != "dumb")


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


# Çalışma aşaması göstergesi (stderr): terminalde tek satır, yerinde güncellenir ve
# rapor basılmadan önce silinir; terminal değilse her aşama ayrı satır.
_PROGRESS_TTY = sys.stderr.isatty()


def progress(msg):
    # Birikimli: her adım kendi satırında kalır (üzerine yazılmaz), simge yok.
    sys.stderr.write(grey("ahenk-debug: ") + msg + "\n")
    sys.stderr.flush()


def progress_clear():
    pass


class Report:
    """Bulguları toplar; hem ekrana basar hem JSON üretir hem özet/teşhis çıkarır."""

    def __init__(self):
        self.sections = []          # [(title, [lines])]
        self._cur = None
        self.findings = []          # (severity, title, detail)
        self.data = {}              # ham veri (json çıktısı için)
        self.timeline = []          # [(datetime, metin)]
        self.fix = None             # --fix: {"before": (fail, warn), "actions": [...]}

    # --- bölüm/satır ---
    def section(self, title):
        desc = re.sub(r"^\s*\d+\)\s*", "", title)
        progress(desc.lower() + " denetleniyor")
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

    # --- zaman çizelgesi ---
    def event(self, dt, text):
        if isinstance(dt, datetime.datetime):
            self.timeline.append((dt, text))

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
        if self.fix is not None:
            out.append("")
            out.append(cyan("▸ Otomatik düzeltme (--fix)"))
            bf, bw = self.fix["before"]
            af = sum(1 for f in self.findings if f[0] == FAIL)
            aw = sum(1 for f in self.findings if f[0] == WARN)
            out.append("  Önce: %d KRİTİK, %d UYARI  →  Sonra: %d KRİTİK, %d UYARI" %
                       (bf, bw, af, aw))
            if self.fix.get("backup"):
                out.append(grey("  Yedek: %s" % self.fix["backup"]))
            badge = {"yapıldı": OK, "başarısız": FAIL, "atlandı": INFO,
                     "düzeltilemez": WARN}
            for st, title, detail in self.fix["actions"]:
                out.append("  %s %s — %s" % (_BADGE[badge.get(st, INFO)], title, st))
                if detail:
                    for d in str(detail).split("\n"):
                        out.append("        %s" % grey(d))
            if not self.fix["actions"]:
                out.append("  (düzeltilecek bir şey bulunamadı)")
            out.append(grey("  Aşağıdaki rapor düzeltme SONRASI durumdur."))
        def _secnum(t):
            m = re.match(r"\s*(\d+)\)", t)
            return int(m.group(1)) if m else 99
        for title, lines in sorted(self.sections, key=lambda s: _secnum(s[0])):
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
        if self.timeline:
            out.append(bold("  ZAMAN ÇİZELGESİ") + grey("  (son açılıştan bu yana; her olay "
                                                       "türünün son kaydı)"))
            out.append(bar)
            for dt, text in sorted(self.timeline, key=lambda e: e[0]):
                out.append("  %s  %s" % (dt.strftime("%Y-%m-%d %H:%M:%S"), text))
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


# Ahenk'in bilinen, zararsız hataları: (desen, kısa ad, açıklama). "Son hata" seçilirken
# atlanır, ayrıca bilgi olarak gösterilir.
KNOWN_BENIGN_ERRORS = [
    (re.compile(r"SHUTDOWN_MODE on resource-usage plugin.*has no attribute 'handle_mode'"),
     "kapanışta resource-usage eklentisi",
     "Tahta kapanırken Ahenk her eklentide 'shutdown' adlı modülün handle_mode() "
     "fonksiyonunu çağırır; resource-usage eklentisindeki shutdown.py ise Lider'den "
     "gelen 'bilgisayarı kapat' görevinin modülüdür ve bu fonksiyonu yoktur (isim "
     "çakışması). Her kapanışta yazılır; bir şey bozulmaz."),
]


def known_benign_error(line):
    for rx, name, expl in KNOWN_BENIGN_ERRORS:
        if rx.search(line):
            return name, expl
    return None


def boot_time_dt():
    """Son açılış zamanı (/proc/stat btime)."""
    m = re.search(r"^btime (\d+)", read_file("/proc/stat") or "", re.M)
    return datetime.datetime.fromtimestamp(int(m.group(1))) if m else None


def analyze_logs(max_lines=6000, since=None):
    """Son logları tara; her olay için sayı + son zaman damgası (str ve datetime)."""
    patterns = {
        "consumer_busy":    re.compile(r"ConsumerBusy", re.I),
        "not_authorized":   re.compile(r"not_authorized|registration_error", re.I),
        # her başarısız deneme bir kez sayılır: ardından gelen "Retrying in N minutes" ve
        # "after maximum retries" satırları aynı denemenin parçasıdır
        "pulsar_conn_fail": re.compile(r"(Pulsar connection failed(?!\. Retrying| after maximum)"
                                       r"|Failed to connect to Pulsar|Producer connection failed"
                                       r"|Pulsar connection attempt \d+ failed)", re.I),
        "conn_refused":     re.compile(r"refused|ConnectError|Connection reset", re.I),
        "timeout":          re.compile(r"timed out|TimeoutError|operation_timeout", re.I),
        "reg_success":      re.compile(r"Registration successful", re.I),
        "already_registered": re.compile(r"already registered", re.I),
        "publish_ok":       re.compile(r"Message published successfully", re.I),
        "pulsar_connected": re.compile(r"Connected to Pulsar|Pulsar connection successful", re.I),
        "pulsar_attempt":   re.compile(r"Pulsar connection retry \d+/\d+", re.I),
        "pulsar_wait":      re.compile(r"Pulsar connection failed\. Retrying in \d+ minutes", re.I),
        "received":         re.compile(r"Received message|Fired event", re.I),
        "tls_err":          re.compile(r"certificate|TLS|SSL", re.I),
        "stopping":         re.compile(r"Stopping the service|Ahenk is stopping|Stopping Ahenk service|systemctl stop ahenk", re.I),
    }
    res = {k: {"count": 0, "last": None, "last_dt": None} for k in patterns}
    res["available"] = False
    res["last_error"] = None
    res["last_error_dt"] = None
    res["benign_errors"] = {"count": 0, "last": None, "last_dt": None, "name": None,
                            "explanation": None}
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
    in_window = since is None
    for ln in lines:
        dt = parse_log_ts(ln)
        if dt:
            in_window = since is None or dt >= since
        if not in_window:        # bu oturumdan önceki satırlar: sayılmaz, değerlendirilmez
            continue
        if dt:
            res["last_line_dt"] = dt
        for key, rx in patterns.items():
            if rx.search(ln):
                res[key]["count"] += 1
                res[key]["last_msg"] = ln.strip()
                res[key]["last"] = LOG_TS_RE.search(ln).group(1) if LOG_TS_RE.search(ln) else ln[:60]
                if dt:
                    res[key]["last_dt"] = dt
        if "ERROR" in ln:
            benign = known_benign_error(ln)
            if benign:
                b = res["benign_errors"]
                b["count"] += 1
                b["name"], b["explanation"] = benign
                b["last"] = LOG_TS_RE.search(ln).group(1) if LOG_TS_RE.search(ln) else None
                b["last_dt"] = dt
            else:
                res["last_error"] = ln.strip()[:400]
                res["last_error_dt"] = dt
    return res


# ----------------------------------------------------------------------------
# ETA kayıt (eta-register) günlüğü ve Ahenk sürüm/depo durumu
# ----------------------------------------------------------------------------
# Ahenk ISO'da gelmez; tahtaya yalnızca ETA kayıt kurar. Arka plan denetimi
# (eta-register -c) yalnızca etapadmin oturum açılışında çalışır ve
# ~/.cache/eta-register/eta-register.log'a yazar. Kurulum adımı pkexec ile root
# olarak çalıştığından kurulum/servis sonuçları /root/.cache altına düşer.
ETA_REG_LOGS = ["/home/*/.cache/eta-register/eta-register.log",
                "/root/.cache/eta-register/eta-register.log"]
ETA_REG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - (\w+) - [\w.]+:\d+ - (.*)$")

# (alt dize, sonuç kodu, Türkçe açıklama) — arka plan denetiminin çıkış nedenleri
ETA_REG_OUTCOMES = [
    ("cannot be run in a virtual machine", "vm",
     "sanal makine algılandı; ETA kayıt çalışmıyor"),
    ("must be run by the", "user",
     "oturum etapadmin değil; denetim yalnızca etapadmin oturumunda çalışır"),
    ("Could not get MAC address", "no-mac",
     "kablolu ağ kartının MAC adresi alınamadı"),
    ("Touch vendor check failed", "vendor",
     "dokunmatik üretici etap.org.tr/devices.json listesinde yok; ETA kayıt kurulum yapmaz"),
    ("All attempts to connect failed", "no-connection",
     "10 dakika boyunca internete/ETA sunucusuna ulaşılamadı"),
    ("Failed to parse server response", "server",
     "ETA sunucusu geçersiz yanıt verdi"),
    ("required package is not installed. Launching installer", "installer",
     "tahta kayıtlı, Ahenk kurulu değil; Ahenk kurulumu başlatıldı"),
    ("Device is registered and package is installed", "installed",
     "tahta kayıtlı ve Ahenk kurulu; denetim başka işlem yapmadan çıktı "
     "(durmuş servisi başlatmaz)"),
    ("Device not registered. Launching registration GUI", "not-registered",
     "tahta ETA'da kayıtlı değil; kayıt ekranı açıldı"),
]


def analyze_eta_register_log(since=None):
    """ETA kayıt günlüklerini oku; son arka plan denetimi, kurulum ve servis sonucu."""
    res = {"available": False, "files": [], "runs": 0, "last_run": None,
           "outcome": None, "outcome_text": None, "outcome_dt": None,
           "gui_update_ok": None, "install": None, "install_dt": None,
           "service": None, "service_dt": None}
    entries = []
    for pat in ETA_REG_LOGS:
        for path in sorted(glob.glob(pat)):
            c = read_file(path)
            if c is None:
                continue
            res["files"].append(path)
            for ln in c.splitlines():
                m = ETA_REG_LINE_RE.match(ln)
                if m:
                    entries.append((m.group(1), m.group(3)))
    if not res["files"]:
        return res
    res["available"] = True
    entries.sort(key=lambda e: e[0])  # birden çok dosya: zamana göre birleştir
    if since is not None:             # yalnızca bu açılıştan beri
        entries = [e for e in entries
                   if (parse_log_ts(e[0]) or since) >= since]
    for ts, msg in entries:
        dt = parse_log_ts(ts)
        if "--- Dispatcher Starting ---" in msg:
            res["runs"] += 1
            res["last_run"] = dt
            res["outcome"] = res["outcome_text"] = res["outcome_dt"] = None
            continue
        for needle, code, text in ETA_REG_OUTCOMES:
            if needle in msg:
                res["outcome"], res["outcome_text"], res["outcome_dt"] = code, text, dt
                break
        if "Registration/update API call successful" in msg:
            res["gui_update_ok"] = dt
        if "Installation completed successfully" in msg:
            res["install"], res["install_dt"] = "ok", dt
        elif "Installation failed" in msg or "Update failed with exit code" in msg:
            res["install"], res["install_dt"] = "fail", dt
        if re.search(r"Service \S+ started successfully", msg):
            res["service"], res["service_dt"] = "ok", dt
        elif "Failed to start service" in msg:
            res["service"], res["service_dt"] = "fail", dt
    return res


ETAP_REPO_BASE = "http://depo.etap.org.tr/etap"


def version_lt(a, b):
    """Debian sürüm karşılaştırması: a < b mi?"""
    rc, _, _ = run(["dpkg", "--compare-versions", a, "lt", b])
    return rc == 0


_PKG_INDEX_CACHE = {}


def published_version(package, timeout=15):
    """ETAP deposunun yayındaki paket dizininden paketin en son sürümü.
    Dönüş: (sürüm|None, kaynak URL|None, hata|None)."""
    osrel = read_file("/etc/os-release") or ""
    # ETAP: VERSION_CODENAME=etap-yirmiuc, PARDUS_CODENAME=yirmiuc; depo dizini 'yirmiuc'
    suites = []
    for key in ("PARDUS_CODENAME", "VERSION_CODENAME"):
        m = re.search(r"^%s=\"?([\w-]+)" % key, osrel, re.M)
        if m:
            suites.append(re.sub(r"^etap-", "", m.group(1)))
    suites.append("yirmiuc")
    suites = list(dict.fromkeys(suites))
    err = None
    for suite, comp in [(su, co) for su in suites for co in ("contrib", "main")]:
        url = "%s/dists/%s/%s/binary-amd64/Packages.gz" % (ETAP_REPO_BASE, suite, comp)
        if url not in _PKG_INDEX_CACHE:
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    _PKG_INDEX_CACHE[url] = gzip.decompress(r.read()).decode("utf-8", "replace")
            except Exception as e:
                _PKG_INDEX_CACHE[url] = None
                err = "%s: %s" % (type(e).__name__, e)
        text = _PKG_INDEX_CACHE[url]
        if not text:
            continue
        latest = None
        for blk in text.split("\n\n"):
            if re.search(r"^Package: %s$" % re.escape(package), blk, re.M):
                v = re.search(r"^Version: (\S+)", blk, re.M)
                if v and (latest is None or version_lt(latest, v.group(1))):
                    latest = v.group(1)
        if latest:
            return latest, url, None
    return None, None, err or "%s paket dizininde bulunamadı" % package


def package_repo_status(package, installed, online=True):
    """Kurulu paket; yayındaki en son sürüm ve yerel apt adayıyla karşılaştırılır."""
    res = {"candidate": None, "published": None, "published_url": None,
           "published_error": None, "latest": None, "outdated": None,
           "lists_stale": None, "etap_repo": None}
    rc, out, _ = run("LC_ALL=C apt-cache policy %s" % package)
    if rc == 0:
        m = re.search(r"Candidate:\s*(\S+)", out)
        if m and m.group(1) != "(none)":
            res["candidate"] = m.group(1)
        res["etap_repo"] = "depo.etap.org.tr/etap" in out
    if online:
        res["published"], res["published_url"], res["published_error"] = \
            published_version(package)
    # Ölçüt: yayındaki en son sürüm; alınamazsa yerel apt adayı
    res["latest"] = res["published"] or res["candidate"]
    if res["published"] and res["candidate"]:
        res["lists_stale"] = version_lt(res["candidate"], res["published"])
    if installed and res["latest"]:
        res["outdated"] = installed != res["latest"] and version_lt(installed, res["latest"])
    return res


def ahenk_repo_status(installed, online=True):
    """Ahenk için package_repo_status + eta-unattended-upgrade durumu."""
    res = package_repo_status("ahenk", installed, online)
    rc, st, _ = run("dpkg-query -W -f='${Status}' eta-unattended-upgrade")
    res["unattended"] = (rc == 0 and "install ok installed" in st)
    return res

def service_down_reason(D):
    """3. bölümdeki servis durumuna göre broker bağlantısı olmamasının nedeni.
    Servis çalışıyorsa None."""
    if D.get("ahenk_version") is None:
        return "Ahenk kurulu değil"
    active = D.get("service_active") or "?"
    enabled = D.get("service_enabled") or "?"
    if active == "active":
        return None
    if active == "failed":
        why = "servis çökmüş (failed): Ahenk hata verip kapanmış"
    elif D.get("self_stopped"):
        why = ("Ahenk kendini durdurmuş (stop): Pulsar/kayıt denemeleri tükenince "
               "'systemctl stop' ile kapanmış, systemd yeniden başlatmaz")
    else:
        why = "servis çalışmıyor (%s)" % active
    if enabled == "masked":
        why += "; servis maskelenmiş (masked): hiçbir şekilde başlatılamaz, " \
               "'sudo systemctl unmask ahenk.service' gerekir"
    elif enabled == "disabled":
        why += "; servis devre dışı (disabled): açılışta da başlamaz, yeniden " \
               "başlatma çözmez — 'sudo systemctl enable --now ahenk.service' gerekir"
    elif enabled == "enabled":
        why += "; servis etkin (enabled): sonraki açılışta başlar, hemen başlatmak " \
               "için 'sudo systemctl start ahenk.service'"
    return "3. bölüm: %s/%s — %s" % (active, enabled, why)


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


def scan_session_errors(srv_start, max_samples=4, cert_path_empty=False):
    """Mevcut servis oturumunda (srv_start sonrası) ölümcül/başlangıç hatalarını tara.
    cert_path_empty: messaging.conf'ta sertifika yolu boşsa (kayıt tamamlanmamış)
    Ahenk'in açılıştaki sertifika indirme hatası beklenen hata olarak ayrı sayılır."""
    out = {"available": False, "count": 0, "samples": [], "expected": 0}
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
        if cert_path_empty and "Error during certificate download" in ln:
            out["expected"] += 1
            continue
        if PULSAR_FAIL_RE.search(ln):     # Pulsar bağlantı hataları ayrı raporlanıyor
            out["pulsar"] = out.get("pulsar", 0) + 1
            continue
        if ("registration.py" in ln or "REGISTRATION" in ln) and REG_ERR_RE.search(ln):
            out["registration"] = out.get("registration", 0) + 1
            continue
        out["count"] += 1
        if len(out["samples"]) < max_samples:
            s = ln.strip()
            s = re.sub(r"^format=", "", s)        # logger önekini temizle
            out["samples"].append(s[:170])
    return out


PULSAR_FAIL_RE = re.compile(
    r"Pulsar connection failed|Failed to connect to Pulsar|Producer connection failed", re.I)
REG_ERR_RE = re.compile(
    r"HTTP error occurred: (\d{3})|HTTP connection error occurred|The request timed out")


def lider_status_text(D):
    """'Lider kaydı' özeti: yerel DB bayrağı + sunucu tarafı doğrulama (varsa)."""
    reg = str(D.get("registered"))
    if reg == "0":
        return "yok (yerel DB)"
    if reg != "1":
        return "?"
    srv = (D.get("lider_server") or {}).get("short")
    return "var (yerel DB)" + (" — %s" % srv if srv else "")


def stop_reason(msg):
    """Ahenk'in kendini durdurma satırından neden: 'pulsar' | 'registration' | None."""
    msg = msg or ""
    if "Pulsar" in msg:
        return "pulsar"
    if re.search(r"registration", msg, re.I):
        return "registration"
    return None


def hms(dt_or_str):
    """Zaman damgasının saat kısmı (HH:MM:SS)."""
    if isinstance(dt_or_str, datetime.datetime):
        return dt_or_str.strftime("%H:%M:%S")
    m = re.search(r"\d{2}:\d{2}:\d{2}", str(dt_or_str or ""))
    return m.group(0) if m else str(dt_or_str)


def registration_request_errors(srv_start):
    """Ahenk'in Lider'e kayıt isteğindeki (/register) hatalar — mevcut servis
    oturumunda (srv_start sonrası). HTTP kodları, son zaman ve URL."""
    out = {"count": 0, "codes": [], "last": None, "last_dt": None, "url": None,
           "kinds": [], "first": None, "first_dt": None}
    texts = [read_file(lf) or "" for lf in sorted(
        [l for l in LOG_FILES if os.access(l, os.R_OK)], key=os.path.getmtime)]
    for ln in "\n".join(texts).splitlines():
        if "registration.py" not in ln and "REGISTRATION" not in ln:
            continue
        m = REG_ERR_RE.search(ln)
        if not m:
            continue
        dt = parse_log_ts(ln)
        if srv_start and dt and dt < srv_start:
            continue
        out["count"] += 1
        if m.group(1):
            out["codes"].append(m.group(1))
            kind = "HTTP %s" % m.group(1)
        elif "timed out" in m.group(0):
            kind = "zaman aşımı"
        else:
            kind = "bağlantı hatası"
        if kind not in out["kinds"]:
            out["kinds"].append(kind)
        u = re.search(r"for url: (\S+?)[,\s]", ln + " ")
        if u:
            out["url"] = u.group(1)
        ts = LOG_TS_RE.search(ln)
        out["last"] = ts.group(1) if ts else out["last"]
        out["last_dt"] = dt or out["last_dt"]
        if out["first"] is None:
            out["first"], out["first_dt"] = out["last"], out["last_dt"]
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


PROBE_TIMEOUT = 60  # sn; Ahenk 120 sn bağlanma / 300 sn işlem bekler
PROBE_SLOW = 5      # sn; başarılı ama bu süreyi aşan adım "yavaş" (WARN) işaretlenir

# Testin adımları: (anahtar, rapor adı, katman)
PROBE_STAGES = [
    ("tcp", "TCP bağlantısı", "ağ"),
    ("tls", "TLS el sıkışma", "ağ"),
    ("auth", "Kimlik doğrulama (Pulsar oturumu)", "uygulama"),
    ("lookup", "Topic arama (proxy → iç broker)", "uygulama"),
    ("producer", "Producer açma", "uygulama"),
    ("send", "Mesaj gönderme + onay", "uygulama"),
]


def active_pulsar_probe(cp, timeout=PROBE_TIMEOUT, registered=None):
    """
    AKTİF DOĞRULAMA (varsayılan, --no-net dışında). Ahenk'in connect() öz-testini
    taklit eder ve her adımın süresini ayrı ölçer:
      1) TCP  2) TLS  — ağ katmanı (kendi soketimizle)
      3) kimlik doğrulama  4) topic arama  5) producer  6) gönderim — uygulama
         katmanı (Pulsar sunucusu). 3 ve 4, get_topic_partitions iki kez
         çağrılarak ayrılır: ilk çağrı oturum açar (TCP+TLS+kimlik+arama),
         ikincisi açık oturumu yeniden kullanır (yalnızca arama).
    Gerçek uid/parola kullanılır; Exclusive komut aboneliğine (ahenk-<uid>)
    DOKUNMAZ, dolayısıyla çalışan Ahenk'i bozmaz.
    """
    res = {"attempted": True, "ok": False, "auth_ok": None, "stage": None,
           "error": None, "skipped": False, "timings": {}, "failed_stage": None,
           "timeout": timeout}
    uid = cfg(cp, "CONNECTION", "uid")
    password = cfg(cp, "CONNECTION", "password")
    host = cfg(cp, "PULSAR", "pulsar_host")
    port = cfg(cp, "PULSAR", "pulsar_port")
    use_tls = (cfg(cp, "PULSAR", "pulsar_use_tls", "false") or "false").lower() == "true"
    ca = cfg(cp, "PULSAR", "tls_trust_certs_file_path")
    if not (uid and password and host and port):
        res["attempted"] = False
        res["skipped"] = True
        if str(registered) != "1":
            res["error"] = ("kayıt tamamlanmadığı için tahtanın Pulsar kimliği ve sunucu "
                            "adresi henüz yok (ahenk.conf / messaging.conf kayıt "
                            "yanıtıyla yazılır)")
        else:
            res["error"] = ("ahenk.conf / messaging.conf içinde uid, parola ya da Pulsar "
                            "sunucu adresi eksik")
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

    T = res["timings"]
    ip = dns_resolve(host) or host

    def fail(stage, err):
        res["stage"] = res["failed_stage"] = stage
        res["error"] = err
        if re.search(r"auth|Authoriz|Authentic|Unauthorized|permission", err, re.I):
            res["auth_ok"] = False
        return res

    # 1) TCP
    t0 = time.monotonic()
    ok, err = tcp_check(ip, port, timeout=min(timeout, 15))
    T["tcp"] = time.monotonic() - t0
    if not ok:
        return fail("tcp", err)
    # 2) TLS
    if use_tls:
        t0 = time.monotonic()
        ok, err, _ = tls_check(ip, port, ca, timeout=min(timeout, 15))
        T["tls"] = time.monotonic() - t0
        if not ok:
            return fail("tls", err)

    # İstemcinin C++ günlüklerini ekrana basma; zaman damgasıyla yakala. Hem ekran
    # kalabalığını önler hem de "Connected to broker" anı ve sunucunun ERROR yanıtı
    # gibi adım ayrıntılarını verir.
    import logging
    plog_msgs = []

    class _Cap(logging.Handler):
        def emit(self, rec):
            try:
                plog_msgs.append((time.monotonic(), rec.getMessage()))
            except Exception:
                pass
    plog = logging.getLogger("ahenk-debug.pulsar")
    plog.propagate = False
    plog.setLevel(logging.DEBUG)
    plog.handlers[:] = [_Cap()]

    def log_time(needle, since):
        for t, m in plog_msgs:
            if t >= since and needle in m:
                return t
        return None
    client = None
    topic = "test-topic-lider"
    try:
        kwargs = dict(
            service_url="%s://%s:%s" % ("pulsar+ssl" if use_tls else "pulsar", ip, port),
            authentication=pulsar.AuthenticationBasic(uid, password, "customAuth"),
            connection_timeout_ms=int(timeout * 1000),
            operation_timeout_seconds=timeout,
            logger=plog,
        )
        if use_tls:
            kwargs.update(use_tls=True, tls_allow_insecure_connection=False,
                          tls_validate_hostname=False)
            if ca:
                kwargs["tls_trust_certs_file_path"] = ca
        try:
            client = pulsar.Client(**kwargs)
        except TypeError:  # eski istemci: logger parametresi yok
            kwargs.pop("logger", None)
            client = pulsar.Client(**kwargs)

        # 3) oturum: TCP+TLS+kimlik doğrulama+arama
        res["stage"] = "auth"
        t0 = time.monotonic()
        try:
            client.get_topic_partitions(topic)
        except Exception as e:
            t_conn = log_time("Connected to broker", t0)
            T["auth"] = time.monotonic() - (t_conn or t0)
            fail("auth", str(e))
            # TCP+TLS kuruldu, sunucu oturum isteğine (CONNECT) ERROR komutuyla yanıt
            # verip bağlantıyı kapattı → oturum/kimlik reddi (kütüphane ConnectError der)
            if t_conn and log_time("incoming command: ERROR", t_conn):
                res["auth_ok"] = False
                res["server_rejected"] = True
                res["error"] = ("sunucu oturum isteğini (CONNECT) ERROR yanıtıyla reddetti "
                                "ve bağlantıyı kapattı (%s)" % e)
            return res
        first = time.monotonic() - t0
        t_first0 = t0
        t_conn = log_time("Connected to broker", t0)
        # 4) arama: açık oturum yeniden kullanılır
        res["stage"] = "lookup"
        t0 = time.monotonic()
        try:
            client.get_topic_partitions(topic)
        except Exception as e:
            T["lookup"] = time.monotonic() - t0
            return fail("lookup", str(e))
        T["lookup"] = time.monotonic() - t0
        # kimlik doğrulama: ilk oturumda "Connected to broker" (istemcinin TCP+TLS'i
        # bitti) anından oturum bitişine kadar geçen süre − arama süresi. Günlük
        # yoksa tahmin: ilk oturum − (TCP + TLS + arama).
        if t_conn:
            T["auth"] = max(0.0, (t_first0 + first - t_conn) - T["lookup"])
            res["auth_measured"] = True
        else:
            T["auth"] = max(0.0, first - T["lookup"] - T.get("tcp", 0) - T.get("tls", 0))
            res["auth_measured"] = False
        res["auth_ok"] = True
        # 5) producer
        res["stage"] = "producer"
        t0 = time.monotonic()
        try:
            prod = client.create_producer(topic)
        except Exception as e:
            T["producer"] = time.monotonic() - t0
            return fail("producer", str(e))
        T["producer"] = time.monotonic() - t0
        # 6) gönderim + onay
        res["stage"] = "send"
        t0 = time.monotonic()
        try:
            prod.send(b"ahenk-debug probe")
        except Exception as e:
            T["send"] = time.monotonic() - t0
            return fail("send", str(e))
        T["send"] = time.monotonic() - t0
        try:
            prod.close()
        except Exception:
            pass
        res["ok"] = True
        res["stage"] = "done"
    except Exception as e:
        return fail(res["stage"] or "connect", str(e))
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return res


def fmt_secs(sec):
    return "%.0f ms" % (sec * 1000) if sec < 1 else "%.1f sn" % sec


def probe_layer_verdict(probe):
    """Testin hangi katmanda takıldığını açıkça söyleyen cümle."""
    st = probe.get("failed_stage")
    if probe.get("ok"):
        return ("TCP ve TLS başarılı (ağ katmanı sağlam); kimlik doğrulama, topic arama, "
                "producer ve gönderim de başarılı (uygulama katmanı sağlam).")
    if st in ("tcp", "tls"):
        return ("%s başarısız: sorun AĞ KATMANINDA (güvenlik duvarı, yönlendirme, "
                "sertifika/protokol) — Pulsar uygulamasına hiç ulaşılamadı."
                % ("TCP" if st == "tcp" else "TLS"))
    name = dict((k, n) for k, n, _ in PROBE_STAGES).get(st, st)
    return ("TCP ve TLS başarılı → ağ katmanı sağlam. Hata/zaman aşımı UYGULAMA "
            "KATMANINDA: %s adımında (Pulsar sunucusu tarafı). Tahtanın ağıyla "
            "ilgili değildir." % name)


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


MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", re.I)


def parse_macs(value):
    """Metinden (tek MAC, liste gösterimi vb.) MAC adreslerini küçük harfle çıkar."""
    return list(dict.fromkeys(m.lower() for m in MAC_RE.findall(str(value or ""))))


def physical_wired_nics(nics):
    """Kablosuz olmayan, gerçek (sürücülü ya da PCI/USB) ağ kartları."""
    return [n for n in nics if n.get("mac") and not n.get("wireless")
            and (n.get("has_driver") or n.get("bus") in ("pci", "usb"))]


def find_alt_eta_registration(nics, reg_macs, exclude, backend, header, limit=6):
    """ETA'da kimlik MAC'iyle kayıt yoksa: Ahenk'in kayıt MAC'i ve tahtadaki diğer
    kablolu kartların MAC'leriyle sorgula. Dönüş: (mac, kaynak, sorgu sonucu) ya da
    None; ikinci değer sorgulanan MAC listesi."""
    cands = []
    for m in reg_macs:
        cands.append((m, "Ahenk'in kayıt anındaki MAC'i"))
    for n in physical_wired_nics(nics):
        cands.append((n["mac"].lower(), "bu tahtadaki %s arayüzü (bus=%s)" %
                      (n["interface"], n.get("bus"))))
    seen, tried = {(exclude or "").lower()}, []
    for mac, src in cands:
        if mac in seen or len(tried) >= limit:
            continue
        seen.add(mac)
        tried.append(mac)
        q = query_eta_board(mac, backend, header)
        if q.get("registered") is True:
            return (mac, src, q), tried
    return None, tried


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
# Raporu sunucusuz, otomatik gönderme: paste.rs + ntfy.sh
# ----------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_for_paste(text):
    """Paste/dosya için düz metin: ANSI renk kodlarını sök, sekmeleri boşluğa çevir.

    Rapor ekranda renkli üretilir (ANSI kaçış kodları); paste servisleri bunları
    render etmez, ham görünür. Burada renk kodlarını ve hizayı bozan sekmeleri
    temizleyerek sabit-genişlikli fontta düzgün okunan bir metin elde ederiz.
    """
    text = ANSI_RE.sub("", text)
    text = text.replace("\t", " ")
    return text


def upload_paste(text, timeout=15):
    """Rapor metnini paste.rs'e yükle, kısa URL döndür. (ok, url, hata)."""
    data = text.encode("utf-8", errors="replace")
    req = urllib.request.Request(PASTE_URL, data=data, method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("User-Agent", "ahenk-debug")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            url = r.read().decode("utf-8", errors="replace").strip()
        return (url.startswith("http"), url, None)
    except Exception as e:
        return (False, None, str(e))


def notify_ntfy(topic, title, message, click=None, priority=None, tags=None,
                timeout=12):
    """ntfy.sh konusuna bildirim it. (ok, hata)."""
    base = NTFY_BASE.rstrip("/")
    # Konu tam URL olarak da verilebilir
    url = topic if topic.startswith("http") else "%s/%s" % (base, topic)
    data = message.encode("utf-8", errors="replace")
    req = urllib.request.Request(url, data=data, method="POST")

    def _ascii(s):
        # ntfy başlık/etiket başlıkları ASCII olmalı; Türkçe karakterleri sadeleştir
        return s.encode("ascii", "replace").decode("ascii")
    req.add_header("Title", _ascii(title))
    if click:
        req.add_header("Click", click)
    if priority:
        req.add_header("Priority", str(priority))
    if tags:
        req.add_header("Tags", _ascii(",".join(tags)))
    req.add_header("User-Agent", "ahenk-debug")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            r.read()
        return (True, None)
    except Exception as e:
        return (False, str(e))


def build_send_summary(D, findings):
    """ntfy mesajı için kısa özet metni (D ve bulgulardan)."""
    n_fail = sum(1 for s, _, _ in findings if s == "FAIL")
    n_warn = sum(1 for s, _, _ in findings if s == "WARN")
    lines = []
    host = D.get("hostname") or platform.node()
    lines.append("host: %s" % host)
    if D.get("identity_mac"):
        lines.append("MAC: %s" % D["identity_mac"])
    sch = D.get("school") or {}
    if sch.get("school"):
        lines.append("okul: %s / %s / %s" %
                     (sch.get("city"), sch.get("town"), sch.get("school")))
    lc = D.get("live_connection") or {}
    lines.append("servis: %s | canlı TCP: %s | Lider kaydı: %s" % (
        D.get("service_active"), lc.get("count"), lider_status_text(D)))
    fx = D.get("fix")
    if fx:
        st = [a["status"] for a in fx["actions"]]
        lines.append("DÜZELTME (--fix): %d işlem — %d yapıldı, %d başarısız, %d düzeltilemez; "
                     "önce %d FAIL/%d WARN" % (len(st), st.count("yapıldı"),
                                                st.count("başarısız"), st.count("düzeltilemez"),
                                                fx["before"]["fail"], fx["before"]["warn"]))
    lines.append("BULGU: %d FAIL, %d WARN" % (n_fail, n_warn))
    # En önemli birkaç bulguyu ekle
    order = {"FAIL": 0, "WARN": 1}
    for sev, title, _ in sorted(findings, key=lambda f: order.get(f[0], 9))[:4]:
        if sev in ("FAIL", "WARN"):
            lines.append("· [%s] %s" % (sev, title))
    return "\n".join(lines), n_fail, n_warn


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
# ----------------------------------------------------------------------------
# Komut satırı: GNU düzeninde, Türkçe ve renkli yardım
# ----------------------------------------------------------------------------
PROG = "ahenk_debug.py"
RUN_URL = "https://raw.githubusercontent.com/enseitankado/ahenk-debug/main/run.sh"

# (seçenek, takma adlar, argüman, açıklama)
_HELP_OPTIONS = [
    ("Genel seçenekler:", [
        ("-h, --help", ("--yardim",), "", "bu yardımı göster ve çık"),
        ("    --renk", ("--color",), "NE-ZAMAN",
         "renkli çıktı: otomatik (varsayılan; yalnız terminalde), her-zaman "
         "ya da hicbir-zaman"),
    ]),
    ("Rapor ve çıktı:", [
        ("    --json", (), "", "bütün ham veriyi ve bulguları JSON olarak bas "
                               "(betiklerle işlenebilir)"),
        ("    --out", (), "DOSYA", "raporu (renksiz) bu dosyaya da yaz"),
        ("    --no-send", (), "", "raporun operatöre otomatik gönderimini kapat "
                                  "(varsayılan: paste.rs + ntfy.sh ile gönderilir)"),
    ]),
    ("Düzeltme:", [
        ("    --fix", (), "", "bulunan sorunları otomatik düzelt: ağ (DHCP), disk, dpkg, "
                              "paketleri kur/güncelle, saat, servisi etkinleştir/başlat, "
                              "Lider kaydını gerekirse 3 kez yeniden dene, ETA kaydını (eski MAC bu "
                              "tahtadaysa) bugünkü MAC'e taşı, klon/sunucu "
                              "reddinde Ahenk kimliğini sıfırla (yedekli); ardından yeniden "
                              "teşhis et. Yedekler: /var/backups/ahenk-debug (son 5)"),
    ]),
    ("Testler ve kaynaklar:", [
        ("    --no-net", (), "", "ağ testlerini (DNS/TCP/TLS, aktif Pulsar) atla; "
                                 "yalnız yerel kontroller yapılır"),
        ("    --db", (), "YOL", "ahenk.db yolunu elle ver (varsayılan: ahenk.conf'taki "
                              "BASE/dbPath)"),
        ("    --mac", (), "MAC", "kayıt durumu sorgusunu bu makine yerine verilen MAC "
                               "için yap (uzaktaki bir tahtayı denetlemek için)"),
    ]),
]

_HELP_EXAMPLES = [
    (f"curl -fsSL {RUN_URL} | sudo bash",
     "Depodan indirmeden çalıştır: tam, renkli rapor; sonunda özet ve teşhis."),
    (f"curl -fsSL {RUN_URL} | sudo bash -s -- --no-net",
     "Aynısı, ağ testleri olmadan (hızlı, yalnız yerel kontroller)."),
    ("sudo ./ahenk_debug.py", "Yerel dosyadan tam rapor."),
    (f"curl -fsSL {RUN_URL} | sudo bash -s -- --fix",
     "Teşhis et, bulunan sorunları otomatik düzelt, yeniden teşhis et."),
    ("sudo ./ahenk_debug.py --out rapor.txt",
     "Raporu ekrana basarken renksiz olarak dosyaya da yaz."),
    ("sudo ./ahenk_debug.py --json > ahenk.json",
     "Bütün veriyi JSON olarak dosyaya kaydet (izleme/otomasyon için)."),
    ("sudo ./ahenk_debug.py --mac 00:11:22:33:44:55 --no-send",
     "Başka bir tahtanın kayıt durumunu sorgula; raporu kimseye gönderme."),
    ("sudo ./ahenk_debug.py --no-send --renk=hicbir-zaman | less",
     "Raporu renksiz olarak sayfa sayfa oku; gönderim yapma."),
    ("sudo ./ahenk_debug.py; echo $?",
     "Raporu al ve çıkış kodunu gör (0 sorunsuz, 1 uyarı, 2 kritik)."),
]

_HELP_EXIT = [
    (0, "kritik ya da uyarı düzeyinde bulgu yok"),
    (1, "en az bir UYARI var, KRİTİK yok"),
    (2, "en az bir KRİTİK bulgu var"),
    (3, "root değil; araç çalışmadan çıktı (sudo ile yeniden deneyin)"),
    (4, "--fix işlem yaptı ama düzeltme sonrası KRİTİK bulgu sürüyor"),
    (64, "hatalı kullanım (tanınmayan seçenek, eksik argüman)"),
    (130, "Ctrl+C ile kesildi"),
]


def _help_text():
    import textwrap

    width, col = 79, 26
    head = lambda s: _c("1;32", s)      # noqa: E731  bölüm başlığı
    opt = lambda s: _c("1;36", s)       # noqa: E731  seçenek
    arg = lambda s: _c("36", s)         # noqa: E731  seçenek argümanı
    out = [f"{head('Kullanım:')} {bold('sudo ' + PROG)} [{arg('SEÇENEK')}]...", ""]
    for para in (
        "Lider tarafında çevrimdışı görünen ya da uzak komut kabul etmeyen Ahenk "
        "istemcilerini yerinde teşhis eder. Pardus ETAP tahtasında Ahenk'in kayıt "
        "ve bağlanma zincirini baştan sona inceler; arızayı kimlik/klon çakışması, "
        "ağ/bağlantı ve yerel yazılım sınıflarına ayırır.",
        "Seçeneksiz çalıştırıldığında renkli, bölümlü bir rapor basar ve raporu "
        "operatöre otomatik gönderir (bkz. --no-send).",
    ):
        out += textwrap.wrap(para, width, break_on_hyphens=False) + [""]
    for i, (title, options) in enumerate(_HELP_OPTIONS):
        out += ([] if i == 0 else [""]) + [head(title)]
        for flags, _aliases, meta, desc in options:
            plain = "  " + flags + ("=" + meta if meta else "")
            indent = "      " if flags.startswith("    ") else "  "
            colored = indent + ", ".join(opt(f.strip()) for f in flags.split(","))
            colored += ("=" + arg(meta)) if meta else ""
            lines = textwrap.wrap(desc, width - col, break_on_hyphens=False)
            if len(plain) >= col - 1:
                out.append(colored)
                out += [" " * col + ln for ln in lines]
            else:
                out.append(colored + " " * (col - len(plain)) + lines[0])
                out += [" " * col + ln for ln in lines[1:]]
    out += ["", head("Notlar:")]
    for para in (
        "Root (sudo) yetkisi zorunludur: ahenk.conf ve messaging.conf (0600), "
        "parolalar, aktif bağlantı testi ve süreç soketleri yalnız root "
        "tarafından okunabilir.",
        "Aktif Pulsar bağlantı testi her çalıştırmada yapılır; yalnız --no-net ile "
        "ya da mesajlaşma Pulsar değilse atlanır.",
    ):
        out += textwrap.wrap(para, width - 2, initial_indent="  ",
                             subsequent_indent="  ", break_on_hyphens=False) + [""]
    out.append(head("Örnekler:"))
    for cmd, desc in _HELP_EXAMPLES:
        cmd_lines = [cmd]
        if len(cmd) > width - 4 and " | " in cmd:
            left, right = cmd.split(" | ", 1)
            cmd_lines = [left + " \\", "  | " + right]
        out.append("  " + _c("2", "$") + " " + bold(cmd_lines[0]))
        out += ["      " + bold(ln) for ln in cmd_lines[1:]]
        out += textwrap.wrap(desc, width - 6, initial_indent="      ",
                             subsequent_indent="      ", break_on_hyphens=False)
    out += ["", head("Çıkış durumu:")]
    out += [f"  {bold(f'{code:>3}')}  {desc}" for code, desc in _HELP_EXIT]
    out += ["", "Ayrıntılı belge, hata bildirimi ve kaynak kod:",
            "<https://github.com/enseitankado/ahenk-debug>"]
    return "\n".join(out)


def _tr_argparse(message):
    """argparse'ın İngilizce hata iletilerini Türkçeye çevirir."""
    rules = (
        (r"^unrecognized arguments: (.*)$", "tanınmayan seçenek: {0}"),
        (r"^argument (\S+): expected one argument$", "{0} bir değer bekliyor"),
        (r"^argument (\S+): ignored explicit argument '(.*)'$", "{0} değer almaz (verilen: {1})"),
        (r"^ambiguous option: (\S+) could match (.*)$", "belirsiz seçenek: {0} (olası: {1})"),
        (r"^argument (\S+): invalid choice: '(.*?)'.*$", "{0} için geçersiz değer: {1}"),
    )
    for pattern, text in rules:
        m = re.match(pattern, message)
        if m:
            parts = [p for p in m.group(1).split("/") if p not in ("--color", "--yardim")]
            return text.format("/".join(parts) or m.group(1), *m.groups()[1:])
    return message


class _Parser(argparse.ArgumentParser):
    """Türkçe hata iletisi ve elle düzenlenmiş GNU yardımı."""

    def error(self, message):
        sys.stderr.write(red(f"{PROG}: {_tr_argparse(message)}") + "\n"
                         f"Ayrıntılı bilgi için: {PROG} --help\n")
        sys.exit(64)

    def print_help(self, file=None):
        print(_help_text(), file=file or sys.stdout)


def _set_color_mode(argv):
    """--renk / --color'ı ayrıştırmadan önce uygula (hata iletileri de uysun)."""
    global _USE_COLOR
    mode = None
    for i, a in enumerate(argv):
        if a.startswith(("--renk=", "--color=")):
            mode = a.split("=", 1)[1]
        elif a in ("--renk", "--color") and i + 1 < len(argv):
            mode = argv[i + 1]
    mode = {"always": "her-zaman", "never": "hicbir-zaman", "auto": "otomatik"}.get(mode, mode)
    if mode == "her-zaman":
        _USE_COLOR = True
    elif mode == "hicbir-zaman":
        _USE_COLOR = False


# ----------------------------------------------------------------------------
# Otomatik düzeltme (--fix)
# ----------------------------------------------------------------------------
BACKUP_ROOT = "/var/backups/ahenk-debug"
BACKUP_KEEP = 5
LIDER_RETRY = 3          # Lider kaydı HTTP 5xx iken servis yeniden başlatma denemesi
FIX_WAIT = 90            # sn; başlatma sonrası kayıt/bağlantı bekleme
APT_OPTS = ["-y", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold"]


def fix_run(cmd, timeout=600):
    """Düzeltme komutu: etkileşimsiz ortamda çalıştır, (rc, stdout, stderr)."""
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C")
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "zaman aşımı (%d sn)" % timeout
    except Exception as e:
        return -1, "", str(e)


def fix_progress(msg):
    progress_clear()
    sys.stderr.write(grey("  → ") + msg + "\n")
    sys.stderr.flush()


def last_line(text):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def messaging_conf_path():
    return os.path.join(CONFIG_D, "messaging.conf")


def backup_ahenk_files(db_path):
    """ahenk.conf, messaging.conf ve ahenk.db'yi zaman damgalı dizine kopyala; son
    BACKUP_KEEP yedeği tut. Dönüş: yedek dizini."""
    d = os.path.join(BACKUP_ROOT, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, mode=0o700, exist_ok=True)
    for f in (AHENK_CONF, messaging_conf_path(), db_path):
        if f and os.path.exists(f):
            shutil.copy2(f, os.path.join(d, os.path.basename(f)))
    dirs = sorted(x for x in os.listdir(BACKUP_ROOT)
                  if os.path.isdir(os.path.join(BACKUP_ROOT, x)))
    for old in dirs[:-BACKUP_KEEP]:
        shutil.rmtree(os.path.join(BACKUP_ROOT, old), ignore_errors=True)
    return d


def edit_conf(path, changes):
    """INI dosyasında yalnızca var olan bölümlerdeki anahtarları değiştir."""
    import configparser
    c = configparser.ConfigParser(interpolation=None)
    c.read(path)
    for sec, kv in changes.items():
        if not c.has_section(sec):
            continue
        for k, v in kv.items():
            c.set(sec, k, v)
    with open(path, "w") as f:
        c.write(f)
    os.chmod(path, 0o600)


def db_registered(db_path):
    reg = (load_db(db_path).get("registration") or {})
    return str(reg.get("registered")) if reg else None


def wait_ahenk_outcome(db_path, timeout=FIX_WAIT):
    """Servis başlatıldıktan sonra sonucu bekle: 'connected' (canlı Pulsar soketi),
    'registered' (Lider kaydı tamamlandı, bağlantı henüz yok), 'stopped' (Ahenk
    kendini durdurdu) ya da 'timeout' (servis çalışıyor, sonuç yok)."""
    t0 = time.monotonic()
    state = "timeout"
    while time.monotonic() - t0 < timeout:
        time.sleep(3)
        cp, _, _ = load_config()
        if live_broker_connections(cp)["count"] > 0:
            return "connected"
        if db_registered(db_path) == "1":
            state = "registered"
        rc, act, _ = run("systemctl is-active ahenk.service")
        if act not in ("active", "activating") and time.monotonic() - t0 > 5:
            return "stopped" if state != "registered" else "registered"
    return state


OUTCOME_TEXT = {
    "connected": "Ahenk Pulsar'a bağlandı",
    "registered": "Lider kaydı tamamlandı (Pulsar bağlantısı henüz kurulmadı)",
    "stopped": "Ahenk yeniden kendini durdurdu",
    "timeout": "servis çalışıyor, %d sn içinde bağlantı kurulmadı (Ahenk bekleme "
               "aşamasında olabilir)" % FIX_WAIT,
}


def eta_update_board_mac(backend, header, board_id, school_code, unit_name, mac,
                         timeout=15):
    """ETA Kayıt'ın 'Düzenle → Kaydet' isteğinin aynısı: POST {backend}/board/update.
    Kaydın okul/derslik bilgisi korunur, yalnızca MAC (mac_id) güncellenir.
    Dönüş: (başarılı mı, HTTP kodu, yanıt/hata metni)."""
    payload = {"id": board_id, "school_code": int(school_code),
               "unit_name": str(unit_name).strip(), "mac_id": mac}
    req = urllib.request.Request("%s/board/update" % backend.rstrip("/"),
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    for k, v in header.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ahenk-debug")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300, r.status, r.read(500).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, e.code, e.read(500).decode("utf-8", "replace")
    except Exception as e:
        return False, None, str(e)


def run_fix(args, R, D):
    """--fix: ilk teşhise (D) göre düzeltmeleri sırayla uygula, yeniden teşhis et.
    Dönüş: (yeni Report, yeni D, işlem yapıldı mı)."""
    actions = []                         # (durum, başlık, ayrıntı)

    def act(status, title, detail=""):
        actions.append((status, title, detail))
        fix_progress("%s — %s" % (title, status))

    before = (sum(1 for f in R.findings if f[0] == FAIL),
              sum(1 for f in R.findings if f[0] == WARN))
    progress_clear()
    sys.stderr.write(bold("\n--fix: düzeltmeler uygulanıyor (bu birkaç dakika "
                          "sürebilir)...\n"))
    db_path = (D.get("db") or {}).get("path") or DEFAULT_DB
    eta_res = (D.get("eta_api") or {}).get("result") or {}
    eta_alt = (D.get("eta_api") or {}).get("alt")
    eta_ok = eta_res.get("registered") is True or bool(eta_alt)
    reg_err = D.get("registration_errors") or {}
    lider_5xx = any(str(c).startswith("5") for c in reg_err.get("codes", []))
    backup_dir = None
    restart_needed = False

    # 1) Ağ: arayüz açık ama IP yok → DHCP yenile
    if D.get("no_carrier"):
        act("düzeltilemez", "Kablo/link yok (%s)" % D["no_carrier"],
            "Fiziksel bağlantı: kabloyu, prizi ve switch portunu kontrol edin.")
    elif D.get("iface_no_ip"):
        iface = D["iface_no_ip"]
        if shutil.which("nmcli"):
            rc, o, e = fix_run(["nmcli", "device", "reapply", iface], 60)
            if rc != 0:
                fix_run(["nmcli", "device", "disconnect", iface], 30)
                rc, o, e = fix_run(["nmcli", "device", "connect", iface], 60)
        elif shutil.which("dhclient"):
            fix_run(["dhclient", "-r", iface], 30)
            rc, o, e = fix_run(["dhclient", "-1", iface], 60)
        else:
            rc, o, e = -1, "", "nmcli/dhclient yok"
        got = False
        for _ in range(10):
            lk = nic_link_status(iface)
            if lk and lk.get("has_ip"):
                got = True
                break
            time.sleep(2)
        act("yapıldı" if got else "başarısız", "DHCP yenilendi (%s)" % iface,
            ("IP: %s" % nic_link_status(iface)["ip"]) if got else
            "IP alınamadı: %s — DHCP sunucusunu/switch'i kontrol edin." % (e or o or rc))

    # 2) Disk dolu → güvenli temizlik
    full = [p for p, du in (D.get("disk") or {}).items() if du and du.get("pct", 0) >= 90]
    if full:
        fix_run(["apt-get", "clean"], 120)
        fix_run(["journalctl", "--vacuum-size=50M"], 120)
        removed = 0
        for f in glob.glob("/var/log/ahenk.log.*.gz"):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
        after = {p: disk_usage_info(p) for p in full}
        still = [p for p, du in after.items() if du and du["pct"] >= 90]
        act("yapıldı" if not still else "başarısız", "Disk temizliği (%s)" % ", ".join(full),
            "apt önbelleği, journal (50 MB'a), %d eski Ahenk günlüğü silindi. Sonra: %s%s" % (
                removed, ", ".join("%s %%%.0f" % (p, du["pct"]) for p, du in after.items() if du),
                " — hâlâ dolu, kullanıcı dosyalarını elle inceleyin." if still else ""))

    # 3) Yarım kalmış dpkg işlemi
    if D.get("dpkg_audit"):
        rc, o, e = fix_run(["dpkg", "--configure", "-a"], 900)
        act("yapıldı" if rc == 0 else "başarısız", "Yarım kalmış paket işlemleri tamamlandı "
            "(dpkg --configure -a)", "" if rc == 0 else last_line(e or o))

    # 4) Paketler
    install, reinstall = [], []
    ahenk_missing = not D.get("ahenk_version")
    if ahenk_missing:
        if eta_ok:
            install.append("ahenk")
        else:
            act("atlandı", "Ahenk kurulmadı",
                "Tahta ETA'da kayıtlı değil; Lider kaydı ETA kaydına bağlı olduğundan önce "
                "ETA Kayıt ile kayıt yapılmalı (aşağıya bakın).")
    elif (D.get("ahenk_repo") or {}).get("outdated"):
        install.append("ahenk")
    er = D.get("eta_register_repo") or {}
    if not er.get("installed") or er.get("outdated"):
        install.append("eta-register")
    if D.get("pulsar_ca_missing"):
        reinstall.append("ahenk-eba-cert")
    if D.get("etainfo_failed"):
        reinstall += ["etainfo", "eta-info"]
    if (D.get("import_error") or (D.get("service_nrestarts") or 0) >= 3) and not ahenk_missing:
        reinstall.append("ahenk")
    if install or reinstall:
        rc, o, e = fix_run(["apt-get", "update"], 300)
        act("yapıldı" if rc == 0 else "başarısız", "Paket listeleri güncellendi (apt update)",
            "" if rc == 0 else last_line(e or o))
        if install:
            rc, o, e = fix_run(["apt-get", "install"] + APT_OPTS + install, 900)
            act("yapıldı" if rc == 0 else "başarısız", "Kuruldu/güncellendi: %s" %
                ", ".join(install), "" if rc == 0 else last_line(e or o))
            restart_needed = restart_needed or ("ahenk" in install and rc == 0)
        if reinstall:
            rc, o, e = fix_run(["apt-get", "install", "--reinstall"] + APT_OPTS + reinstall, 900)
            act("yapıldı" if rc == 0 else "başarısız", "Yeniden kuruldu: %s" %
                ", ".join(reinstall), "" if rc == 0 else last_line(e or o))
            restart_needed = restart_needed or ("ahenk" in reinstall and rc == 0)

    # 5) Saat
    if D.get("ntp_sync") not in (None, "yes"):
        fix_run(["timedatectl", "set-ntp", "true"], 30)
        fix_run(["systemctl", "restart", "systemd-timesyncd"], 30)
        ok_sync = False
        for _ in range(10):
            time.sleep(3)
            rc, sync, _ = run("timedatectl show -p NTPSynchronized --value")
            if sync == "yes":
                ok_sync = True
                break
        act("yapıldı" if ok_sync else "başarısız", "Saat senkronu (NTP) açıldı",
            "" if ok_sync else "30 sn içinde senkron olmadı; NTP (UDP 123) erişimini kontrol "
                               "edin.")

    # 6) Kimlik: sıfırlama gerekçeleri (yalnızca güçlü kanıt)
    reset_why = None
    if os.path.exists(AHENK_CONF) and D.get("ahenk_version"):
        integ = (D.get("db") or {}).get("integrity")
        if D.get("consumer_busy_current"):
            reset_why = ("KLON İMAJ: aynı UUID başka bir tahtada aktif (güncel "
                         "ConsumerBusy)")
        elif os.path.exists(db_path) and integ not in (None, "ok", "dosya yok") and (
                not str(integ).startswith("hata:")
                or re.search(r"malformed|not a database", str(integ), re.I)):
            # Yalnızca gerçek bozulma: kilitli/geçici açılamayan DB'ye dokunulmaz
            reset_why = "Ahenk veritabanı bozuk (integrity=%s)" % integ
        elif str(D.get("registered")) == "1" and \
                (D.get("active_probe") or {}).get("auth_ok") is False:
            fix_progress("Pulsar sunucusunun reddi ikinci kez doğrulanıyor...")
            cp2, _, _ = load_config()
            p2 = active_pulsar_probe(cp2, registered="1")
            if p2.get("auth_ok") is False:
                reset_why = ("Lider/Pulsar sunucusu tahtanın kimliğini TANIMIYOR (oturum iki "
                             "kez reddedildi; kayıt Lider'de silinmiş/değişmiş)")
            else:
                act("atlandı", "Kimlik sıfırlanmadı",
                    "Sunucu reddi ikinci denemede tekrarlanmadı (geçici olabilir).")
        reg_macs = D.get("registered_macs") or []
        live_mac = (D.get("live_mac") or "").lower()
        local_macs = {(n.get("mac") or "").lower() for n in (D.get("nics") or [])}
        if not reset_why and reg_macs and live_mac and live_mac not in reg_macs \
                and not any(m in local_macs for m in reg_macs):
            act("düzeltilemez", "Kimlik sıfırlanmadı (kayıt MAC'i bu tahtada yok)",
                "Klon imaj mı donanım değişimi mi ayırt edilemiyor; sıfırlama gereksiz kayıp "
                "yaratabilir. ConsumerBusy görülürse --fix kimliği sıfırlar; donanım "
                "değiştiyse operatörden ETA kaydının MAC'ini güncellemesini isteyin.")

    if reset_why or D.get("conf_db_mismatch"):
        try:
            backup_dir = backup_ahenk_files(db_path)
        except Exception as e:
            act("başarısız", "Yedek alınamadı — kimlik işlemleri yapılmadı", str(e))
            reset_why = None
            D["conf_db_mismatch"] = False

    if reset_why:
        fix_run(["systemctl", "stop", "ahenk.service"], 60)
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
            for pid in ("/var/run/ahenkd.pid", "/run/ahenkd.pid"):
                if os.path.exists(pid):
                    os.remove(pid)
            edit_conf(AHENK_CONF, {"CONNECTION": {"uid": "", "password": "", "host": ""},
                                   "MACHINE": {"user_disabled": "false"}})
            if os.path.exists(messaging_conf_path()):
                edit_conf(messaging_conf_path(), {"PULSAR": {
                    "pulsar_host": "", "pulsar_port": "", "pulsar_use_tls": "",
                    "tls_trust_certs_file_path": ""}})
            act("yapıldı", "Ahenk kimliği sıfırlandı (yeni UUID ile yeni Lider kaydı "
                "yapılacak)", "Neden: %s\nYedek: %s\nNot: eski ajan kaydı Lider'de sahipsiz "
                "kalır." % (reset_why, backup_dir))
            restart_needed = True
        except Exception as e:
            act("başarısız", "Ahenk kimliği sıfırlanamadı", "%s (yedek: %s)" % (e, backup_dir))
    elif D.get("conf_db_mismatch"):
        reg = (load_db(db_path).get("registration") or {})
        if reg.get("jid"):
            try:
                edit_conf(AHENK_CONF, {"CONNECTION": {"uid": reg["jid"],
                                                      "password": reg.get("password") or ""}})
                act("yapıldı", "ahenk.conf kimliği veritabanına eşitlendi",
                    "uid=%s (yedek: %s)" % (reg["jid"], backup_dir))
                restart_needed = True
            except Exception as e:
                act("başarısız", "ahenk.conf eşitlenemedi", str(e))

    # 6b) ETA kaydı farklı MAC ile: eski MAC bu tahtadaki bir karta aitse kaydı bugünkü
    # kimlik MAC'ine taşı (ETA Kayıt'ın Düzenle → Kaydet isteği). Eski MAC tahtada yoksa
    # (klon imaj / donanım değişimi ayırt edilemez) dokunulmaz.
    eta_moved = False
    if eta_alt and not getattr(args, "mac", None):
        old = (eta_alt.get("mac") or "").lower()
        new = (D.get("live_mac") or "").lower()
        local = {(n.get("mac") or "").lower(): n.get("interface")
                 for n in (D.get("nics") or []) if n.get("mac")}
        data = ((eta_alt.get("result") or {}).get("data")) or {}
        unit = str(data.get("unit_name") or "").strip()
        if old in local and new and data.get("board_id") and data.get("school_code") \
                and 2 <= len(unit) <= 12:
            backend, header = load_eta_config()
            ok, code, resp = eta_update_board_mac(backend, header, data["board_id"],
                                                  data["school_code"], unit, new)
            chk = query_eta_board(new, backend, header) if ok else {}
            eta_moved = ok and chk.get("registered") is True
            act("yapıldı" if eta_moved else "başarısız",
                "ETA kaydı bugünkü MAC'e taşındı (%s → %s)" % (old, new),
                ("board_id %s · okul kodu %s · derslik '%s' korunarak yalnızca MAC "
                 "güncellendi (eski MAC bu tahtadaki %s kartının). Yeni MAC ile sorgu: "
                 "KAYITLI." % (data["board_id"], data["school_code"], unit, local[old]))
                if eta_moved else
                ("ETA sunucusu güncellemeyi %s (HTTP %s): %s" % (
                    "kabul etti ama yeni MAC ile kayıt görünmüyor" if ok else "reddetti",
                    code, (resp or "").strip()[:200])))
        elif old in local:
            act("atlandı", "ETA kaydı taşınmadı",
                "Kayıt bilgisi eksik ya da derslik adı 2–12 karakter değil (board_id=%s, "
                "derslik='%s')." % (data.get("board_id"), unit))

    # 7) Servis
    if D.get("ahenk_version") or "ahenk" in install:
        _, en, _ = run("systemctl is-enabled ahenk.service")
        if en == "masked":
            rc, o, e = fix_run(["systemctl", "unmask", "ahenk.service"], 30)
            act("yapıldı" if rc == 0 else "başarısız", "Servis maskesi kaldırıldı (unmask)",
                "" if rc == 0 else last_line(e or o))
            _, en, _ = run("systemctl is-enabled ahenk.service")
        if en != "enabled":
            rc, o, e = fix_run(["systemctl", "enable", "ahenk.service"], 30)
            act("yapıldı" if rc == 0 else "başarısız", "Servis açılışta başlayacak şekilde "
                "etkinleştirildi", "" if rc == 0 else last_line(e or o))
        _, active, _ = run("systemctl is-active ahenk.service")
        backoff = D.get("backoff") and (D.get("net_basics") or {}).get("broker_tcp_open")
        need_start = active != "active" or restart_needed or bool(backoff)
        retry = LIDER_RETRY if (reset_why or (lider_5xx and str(D.get("registered")) != "1")) \
            else 1
        if need_start:
            why = ("bekleme aşaması atlanıyor (deneme %s/%s, sonraki deneme %s)" % (
                D["backoff"]["attempt"], D["backoff"]["max"], D["backoff"]["next"])
                if backoff and active == "active" and not restart_needed else "")
            for i in range(1, retry + 1):
                t_start = datetime.datetime.now() - datetime.timedelta(seconds=1)
                rc, o, e = fix_run(["systemctl", "restart", "ahenk.service"], 60)
                if rc != 0:
                    act("başarısız", "Ahenk servisi başlatılamadı", last_line(e or o))
                    break
                fix_progress("Ahenk başlatıldı%s; sonuç bekleniyor (en çok %d sn)..." % (
                    " (deneme %d/%d)" % (i, retry) if retry > 1 else "", FIX_WAIT))
                outcome = wait_ahenk_outcome(db_path)
                errs = registration_request_errors(t_start)
                detail = OUTCOME_TEXT[outcome]
                if errs["count"]:
                    detail += "; Lider kayıt isteği: %s ×%d" % (", ".join(errs["kinds"]),
                                                                errs["count"])
                title = "Ahenk servisi başlatıldı" + (" (deneme %d/%d)" % (i, retry)
                                                       if retry > 1 else "")
                if why:
                    title += " — " + why
                ok = outcome in ("connected", "registered")
                act("yapıldı" if ok else "başarısız", title, detail)
                if ok:
                    break
                server_5xx = any(str(c).startswith("5") for c in errs["codes"])
                if not server_5xx or i == retry:
                    if server_5xx:
                        act("düzeltilemez", "Lider kayıt sunucusu %d denemede de HTTP 5xx "
                            "döndürdü" % retry, "Sunucu tarafı hatası; tahtada yapılacak "
                            "başka bir şey yok. Operatöre bildirin (MAC, board_id: raporda).")
                    break
                time.sleep(5)

    # 8) Tahtadan düzeltilemeyen ETA durumları
    if eta_alt and not eta_moved:
        local_macs = {(n.get("mac") or "").lower() for n in (D.get("nics") or [])}
        if (eta_alt.get("mac") or "").lower() not in local_macs:
            act("düzeltilemez", "ETA kaydı farklı bir MAC ile (%s)" % eta_alt.get("mac"),
                "Bu MAC bu tahtadaki hiçbir karta ait değil: klon imaj (kayıt başka bir "
                "tahtanın) mı donanım değişimi mi ayırt edilemez; kayda dokunulmadı. Yeniden "
                "kayıt yinelenen kayıt üretir. Operatöre: board_id ve iki MAC (raporda).")
    elif not eta_alt and eta_res.get("registered") is False:
        act("düzeltilemez", "Tahta ETA'da kayıtlı değil",
            "Okul ve derslik seçimi gerekir: etapadmin ile oturum açıp ETA Kayıt'ı açın. "
            "Kayıt sonrası Ahenk otomatik kurulup başlar.")

    # 9) Tahtadan düzeltilemeyen ağ/sunucu sorunları (ilk teşhisteki bulgulardan)
    unfixable = [
        ("Varsayılan ağ geçidi yok", "Ağ yapılandırması: DHCP/ağ yöneticisi ağ geçidi "
         "vermiyor; okul ağını kontrol edin."),
        ("nameserver yok", "DNS sunucusu tanımlı değil: DHCP/ağ yapılandırmasını kontrol edin."),
        ("DNS ile çözülemiyor", "DNS adı çözemiyor: okul DNS'ini ya da filtreyi kontrol edin."),
        ("TCP portuna ulaşılamıyor", "Broker portu kapalı: okul güvenlik duvarı/filtre "
         "6650-6651'i engelliyor olabilir."),
        ("TCP bağlantı kurulamıyor", "Broker portu kapalı: okul güvenlik duvarı/filtre "
         "6650-6651'i engelliyor olabilir."),
        ("TLS el sıkışması başarısız", "TLS kurulamıyor: araya giren proxy/SSL denetimi "
         "olabilir."),
        ("ETA kayıt sunucusuna ulaşılamadı", "ETA API'ye erişilemiyor: ağ/filtre ya da sunucu "
         "tarafı."),
        ("Aktif bağlantı testi başarısız", "Pulsar sunucusu uygulama katmanında yanıt "
         "vermiyor/yavaş: sunucu tarafı (adım süreleri raporda)."),
        ("not_authorized", "Lider kaydı reddediyor: Lider tarafındaki kurallar; operatöre "
         "bildirin."),
    ]
    done_titles = set()
    for sev, title, _ in R.findings:
        if sev != FAIL and "not_authorized" not in title:
            continue
        for key, why in unfixable:
            if key in title and key not in done_titles:
                done_titles.add(key)
                act("düzeltilemez", title, why)

    # 10) Yeniden teşhis
    sys.stderr.write(bold("--fix: yeniden teşhis ediliyor...\n\n"))
    R2, D2 = diagnose(args)
    R2.fix = {"before": before, "actions": actions, "backup": backup_dir}
    D2["fix"] = {"before": {"fail": before[0], "warn": before[1]},
                 "actions": [{"status": s_, "title": t, "detail": d}
                             for s_, t, d in actions],
                 "backup": backup_dir}
    fixed_any = any(s_ == "yapıldı" for s_, _, _ in actions)
    return R2, D2, fixed_any


def diagnose(args):
    """Tam teşhis: bütün bölümleri çalıştırır, (Report, ham veri) döndürür."""
    R = Report()
    D = R.data

    cp, conf_files, perm_err = load_config()
    root = is_root()

    srv_start = service_start_dt()
    boot_dt = boot_time_dt()
    # Analiz penceresi: Ahenk'in kendi (son) oturumu. Servis bu açılışta hiç başlamadıysa
    # açılıştan beri. Öncesindeki günlük kayıtları ne gösterilir ne değerlendirilir.
    log_since = srv_start or boot_dt
    if boot_dt and srv_start and srv_start < boot_dt:
        log_since = boot_dt        # önceki açılışın oturumu bu açılışa ait değil

    # ---------------- 1) Genel / servis / yerel sağlık ----------------
    R.section("3) Genel Durum, Servis ve Yerel Sağlık")
    rc, pkg, _ = run("dpkg-query -W -f='${Version}' ahenk")
    ahenk_installed = rc == 0 and bool(pkg)
    D["ahenk_version"] = pkg if ahenk_installed else None
    logs = analyze_logs(since=log_since)
    etareg = analyze_eta_register_log(since=boot_dt)
    D["log_window_start"] = log_since.strftime("%Y-%m-%d %H:%M:%S") if log_since else None
    D["eta_register_log"] = {k: (v.strftime("%Y-%m-%d %H:%M:%S")
                                 if isinstance(v, datetime.datetime) else v)
                             for k, v in etareg.items()}
    etareg_fix = ("ÇÖZÜM: etapadmin ile oturum açıp ETA Kayıt'ı açın → Düzenle → "
                  "(sınıf adını değiştirin) → Kaydet. Kayıt güncellenince ETA "
                  "Kayıt 'apt install ahenk' ve 'systemctl enable --now "
                  "ahenk.service' çalıştırır. (ETA Kayıt kayıtlı tahtada yalnızca "
                  "bilgi sayfasını gösterir; kurulumu yalnızca Kaydet tetikler.)")

    def etareg_reason():
        if not etareg["available"]:
            return ("ETA Kayıt günlüğü yok: arka plan denetimi bu tahtada hiç "
                    "çalışmamış (etapadmin oturumu hiç açılmamış olabilir).")
        if etareg["last_run"] is None:
            return ("ETA Kayıt arka plan denetimi bu açılışta çalışmamış (etapadmin "
                    "oturumu açılmamış olabilir).")
        return ("ETA Kayıt son arka plan denetimi (%s): %s." % (
            etareg["last_run"].strftime("%Y-%m-%d %H:%M"),
            etareg["outcome_text"] or "sonuç kaydı yok"))

    if log_since:
        R.line("Günlük analizi", "yalnızca Ahenk'in bu oturumu (başlangıç: %s); öncesi "
               "gösterilmez ve değerlendirilmez" % log_since.strftime("%Y-%m-%d %H:%M:%S")
               if srv_start and log_since == srv_start else
               "yalnızca bu açılış (açılış: %s; Ahenk bu açılışta başlamamış); öncesi "
               "gösterilmez ve değerlendirilmez" % log_since.strftime("%Y-%m-%d %H:%M:%S"),
               INFO)
    if ahenk_installed:
        R.line("Ahenk paketi", pkg, OK)
    else:
        R.line("Ahenk paketi", "KURULU DEĞİL", FAIL)
        R.finding(FAIL, "Ahenk kurulu değil — ETA Kayıt kurulumu yapmamış",
                  "Ahenk ETAP imajında gelmez; tahtaya yalnızca ETA Kayıt kurar "
                  "(etapadmin oturum açılışındaki arka plan denetimi ya da kayıt/"
                  "güncelleme sonrası). Ahenk olmadan tahta Lider'de görünmez.\n"
                  "%s\n%s" % (etareg_reason(), etareg_fix))

    # Sürüm: depodaki adaydan eski mi?
    progress("depodaki ahenk ve eta-register sürümleri alınıyor")
    repo = ahenk_repo_status(pkg if ahenk_installed else None, online=not args.no_net)
    D["ahenk_repo"] = repo
    if repo["published"]:
        R.line("Yayındaki en son Ahenk", "%s (%s)" % (repo["published"], ETAP_REPO_BASE), INFO)
    elif args.no_net:
        R.line("Yayındaki en son Ahenk", "--no-net ile atlandı (yerel apt listesine bakılıyor)",
               INFO)
    else:
        R.line("Yayındaki en son Ahenk", "alınamadı: %s" % repo["published_error"], WARN)
    if repo["candidate"]:
        R.line("Yerel apt adayı", repo["candidate"],
               WARN if repo["lists_stale"] else INFO)
    elif repo["etap_repo"] is False:
        R.line("Yerel apt adayı", "yok — ETAP deposu (depo.etap.org.tr/etap) tanımlı değil",
               WARN)
    if repo["lists_stale"]:
        R.note("Yerel paket listesi eski ('apt update' yapılmamış): apt yalnızca %s "
               "sürümünü görüyor, yayında %s var." % (repo["candidate"], repo["published"]))
    if ahenk_installed and repo["latest"]:
        ref = "yayındaki" if repo["published"] else "yerel apt listesindeki"
        if repo["outdated"]:
            R.line("Ahenk sürümü", "ESKİ (kurulu %s < %s en son %s)" %
                   (pkg, ref, repo["latest"]), WARN)
            R.finding(WARN, "Ahenk en son sürüm değil (kurulu %s, en son %s)" %
                      (pkg, repo["latest"]),
                      "%s%s\n"
                      "ÇÖZÜM: sudo apt update && sudo apt install ahenk  (ya da Pardus "
                      "Güncelleme / 'sudo apt full-upgrade'). Yükseltme servisi de yeniden "
                      "başlatır.%s" % (
                          "Depoda daha yeni sürüm yayında ama kurulmamış."
                          if repo["unattended"] else
                          "eta-unattended-upgrade kurulu değil: otomatik güncelleme ETAP "
                          "paketlerini hiç yükseltmez; yeni Ahenk bu paketi bağımlılık "
                          "olarak istediğinden 'apt-get upgrade' de onu yükseltmez, "
                          "'geri tutar'.",
                          " Yerel paket listesi de eski; önce 'apt update' gerekir."
                          if repo["lists_stale"] else "",
                          "" if repo["published"] else
                          "\nNOT: Yayındaki sürüm alınamadı; karşılaştırma yerel paket "
                          "listesine göredir."))
        else:
            R.line("Ahenk sürümü", "güncel (%s en son sürüm: %s)" % (ref, repo["latest"]), OK)

    # eta-register (ETA Kayıt): Ahenk'i kuran/başlatan uygulama — en son sürüm mü?
    rc, erv, _ = run("dpkg-query -W -f='${Version}' eta-register")
    er_installed = erv if rc == 0 and erv else None
    er = package_repo_status("eta-register", er_installed, online=not args.no_net)
    er["installed"] = er_installed
    D["eta_register_repo"] = er
    if not er_installed:
        R.line("ETA Kayıt (eta-register)", "KURULU DEĞİL", WARN)
        R.finding(WARN, "ETA Kayıt (eta-register) kurulu değil",
                  "Ahenk'i tahtaya kuran ve kayıt sonrası servisi başlatan uygulama "
                  "ETA Kayıt'tır; kurulu değilse Ahenk kurulumu/yeniden başlatması "
                  "kendiliğinden olmaz.\nÇÖZÜM: sudo apt update && sudo apt install "
                  "eta-register")
    elif er["latest"]:
        ref = "yayındaki" if er["published"] else "yerel apt listesindeki"
        if er["outdated"]:
            R.line("ETA Kayıt sürümü", "ESKİ (kurulu %s < %s en son %s)" %
                   (er_installed, ref, er["latest"]), WARN)
            R.finding(WARN, "ETA Kayıt en son sürüm değil (kurulu %s, en son %s)" %
                      (er_installed, er["latest"]),
                      "Eski ETA Kayıt, Ahenk kurulum/kayıt akışındaki düzeltmeleri "
                      "içermeyebilir.%s\nÇÖZÜM: sudo apt update && sudo apt install "
                      "eta-register%s" % (
                          " Yerel paket listesi de eski; önce 'apt update' gerekir."
                          if er["lists_stale"] else "",
                          "" if er["published"] else
                          "\nNOT: Yayındaki sürüm alınamadı; karşılaştırma yerel paket "
                          "listesine göredir."))
        else:
            R.line("ETA Kayıt sürümü", "güncel (%s en son sürüm: %s)" %
                   (ref, er["latest"]), OK)
    else:
        R.line("ETA Kayıt sürümü", "%s (en son sürüm belirlenemedi)" % er_installed, INFO)

    rc, audit, _ = run("dpkg --audit", timeout=30)
    if rc == 0 and audit.strip():
        D["dpkg_audit"] = True
        R.line("dpkg durumu", "yarım kalmış paket işlemi var", WARN)
        R.finding(WARN, "Yarım kalmış paket işlemi (dpkg)",
                  "Bir kurulum/güncelleme yarıda kalmış; yeni paket işlemleri başarısız "
                  "olabilir.\nÇÖZÜM: sudo dpkg --configure -a\n%s" % audit.strip()[:300])
    reg_err = registration_request_errors(log_since)
    D["registration_errors"] = reg_err
    rc, active, _ = run("systemctl is-active ahenk.service")
    rc2, enabled, _ = run("systemctl is-enabled ahenk.service")
    D["service_active"] = active
    D["service_enabled"] = enabled
    sev = OK if active == "active" else FAIL
    R.line("Servis", "%s / %s" % (active, enabled), sev)
    if ahenk_installed and active != "active":
        st = logs["stopping"]
        self_stopped = logs["available"] and st["count"] > 0
        D["self_stopped"] = self_stopped
        why = stop_reason(st.get("last_msg"))
        D["stop_reason"] = why if self_stopped else None
        started = ("Ahenk servisi başladı (%s)" % hms(srv_start)) if srv_start else \
            "Ahenk servisi başladı"
        if self_stopped:
            R.line("Kendini durdurma (log)", "son: %s" % st["last"], FAIL)
        if self_stopped and reg_err["count"]:
            R.finding(FAIL, "Ahenk başladı, Lider kaydı başarısız olunca kendini durdurdu "
                      "(%s)" % ", ".join(reg_err["kinds"]),
                      "%s. Lider'e kayıt isteği %d kez başarısız oldu (%s, %s–%s).\n"
                      "Lider kaydı olmadan Ahenk'in mesajlaşma kimliği oluşmaz; bağlanamayacağı "
                      "için Ahenk kendini durdurdu (%s).\n"
                      "Sorun Lider kayıt sunucusunda. ETA Kayıt'ta yeniden kaydetmek aynı "
                      "zinciri tekrarlar (yeni UUID, aynı hata). Operatöre iletilecek bilgiler: "
                      "'Lider kayıt sunucusu' bulgusu; olay sırası: rapor sonundaki zaman "
                      "çizelgesi." % (started, reg_err["count"], ", ".join(reg_err["kinds"]),
                                      hms(reg_err["first"]), hms(reg_err["last"]),
                                      hms(st["last"])))
        elif self_stopped and why == "registration":
            R.finding(FAIL, "Ahenk başladı, Lider kaydını tamamlayamayınca kendini durdurdu",
                      "%s. Lider'e kayıt denemeleri sonuçsuz kaldı; Lider kaydı olmadan "
                      "mesajlaşma kimliği oluşmadığı için Ahenk kendini durdurdu (%s).\n"
                      "Kayıt ucuna erişimi (6. bölüm) ve Lider'in yanıtını (günlük) inceleyin; "
                      "sonra: sudo systemctl start ahenk.service (ya da ETA Kayıt → Düzenle → "
                      "Kaydet)." % (started, hms(st["last"])))
        elif self_stopped and why == "pulsar":
            R.finding(FAIL, "Ahenk Pulsar'a bağlanamayınca kendini durdurdu",
                      "%s; Pulsar sunucusuna 10 denemede (1–64 dk aralıklarla, ≈5–6 saat) "
                      "bağlanamadı ve kendini durdurdu (%s).\n"
                      "Bilinçli durdurma olduğu için kendiliğinden yeniden başlamaz; tahta "
                      "yeniden açılana ya da servis başlatılana kadar çevrimdışı kalır.\n"
                      "Önce ağ/Pulsar erişimini düzeltin (5–6. bölüm), sonra: sudo systemctl "
                      "start ahenk.service (ya da ETA Kayıt → Düzenle → Kaydet)."
                      % (started, st["last"]))
        elif self_stopped:
            R.finding(FAIL, "Ahenk kendini durdurmuş",
                      "%s; ardından kendini durdurdu (%s, systemctl stop). Bilinçli durdurma "
                      "olduğu için kendiliğinden yeniden başlamaz.\n"
                      "Nedeni için günlüğe ve aşağıdaki bölümlere bakın; sonra: sudo systemctl "
                      "start ahenk.service (ya da ETA Kayıt → Düzenle → Kaydet)."
                      % (started, st["last"]))
        else:
            R.finding(FAIL, "Ahenk servisi çalışmıyor (%s)" % active,
                      "Servis durduysa makine kesinlikle çevrimdışı görünür. ETA Kayıt "
                      "arka plan denetimi durmuş servisi başlatmaz.\n"
                      "Komut: systemctl status ahenk.service  |  journalctl -u ahenk\n"
                      "%s" % etareg_fix)

    # ETA Kayıt günlüğü (bilgi; sorun varsa yukarıdaki bulgulara gerekçe olur)
    if etareg["available"]:
        if not etareg["last_run"]:
            R.line("ETA Kayıt son denetim", "bu açılışta çalışmamış (etapadmin oturumu "
                   "açılmamış olabilir)", INFO)
        if etareg["last_run"]:
            R.line("ETA Kayıt son denetim", "%s — %s" % (
                etareg["last_run"].strftime("%Y-%m-%d %H:%M"),
                etareg["outcome_text"] or "sonuç kaydı yok"),
                WARN if etareg["outcome"] in ("no-mac", "vendor", "no-connection",
                                              "server", "user") else INFO)
            R.line("ETA Kayıt denetim sayısı", str(etareg["runs"]), INFO)
        if etareg["gui_update_ok"]:
            R.line("ETA Kayıt son kayıt/güncelleme",
                   etareg["gui_update_ok"].strftime("%Y-%m-%d %H:%M"), INFO)
        if etareg["install"]:
            R.line("ETA Kayıt Ahenk kurulumu", "%s (%s)" % (
                "başarılı" if etareg["install"] == "ok" else "BAŞARISIZ",
                etareg["install_dt"].strftime("%Y-%m-%d %H:%M")),
                OK if etareg["install"] == "ok" else FAIL)
            if etareg["install"] == "fail" and not ahenk_installed:
                R.note("Kurulum günlüğü: /root/.cache/eta-register/eta-register.log")
        if etareg["service"] == "fail":
            R.line("ETA Kayıt servis başlatma", "BAŞARISIZ (%s)" %
                   etareg["service_dt"].strftime("%Y-%m-%d %H:%M"), WARN)
    else:
        R.line("ETA Kayıt günlüğü", "yok (etapadmin oturumunda hiç çalışmamış olabilir)",
               WARN if not ahenk_installed else INFO)
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
    cert_path_empty = not (cfg(cp, "PULSAR", "tls_trust_certs_file_path") or "").strip()
    sess_err = scan_session_errors(log_since, cert_path_empty=cert_path_empty)
    D["session_errors"] = sess_err
    if sess_err["available"]:
        if sess_err["expected"]:
            R.line("Sertifika indirme hatası", "%d kez — kayıt öncesi BEKLENEN hata" %
                   sess_err["expected"], INFO)
            R.note("messaging.conf'taki sertifika yolu (tls_trust_certs_file_path) kayıt "
                   "başarılı olunca Lider'in yanıtıyla doldurulur. Yol boşken Ahenk "
                   "indirdiği sertifikayı open('') ile yazmaya çalışır ve FileNotFoundError "
                   "alır. Kaydın tamamlanmamasının sonucudur, nedeni değildir.")
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
                D["import_error"] = True
                R.finding(FAIL, "Mevcut oturumda başlangıç/bağımlılık hatası",
                          "Loglarda ImportError/Traceback türü hata var; Ahenk düzgün "
                          "başlayamıyor olabilir.")
            else:
                R.finding(WARN, "Mevcut oturumda hata kayıtları var",
                          "Servis bu oturumda %d hata satırı üretmiş; ayrıntılar "
                          "yukarıda." % sess_err["count"])

    R.line("Yetki", "root (tam erişim)", OK)

    # ---------------- 2) Kimlik ----------------
    R.section("4) Ahenk Kimliği ve Lider Kaydı (UUID / Parola)")
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
        R.line("Lider kaydı (yerel DB, registered)", "%s — %s" % (
            db_reg["registered"], "tamamlanmış" if str(db_reg["registered"]) == "1"
            else "TAMAMLANMAMIŞ"),
            OK if str(db_reg["registered"]) == "1" else FAIL)
        dn = (db_reg["dn"] or "").strip()
        registered_flag = str(db_reg["registered"]) == "1"
        # Mevcut mimari LDAP kullanmıyor: dn (LDAP dizin nesnesi) yalnızca bilgi
        # amaçlı gösterilir, boş olması bulgu üretmez. Kayıt kararı registered
        # bayrağına göre verilir.
        R.line("LDAP dn", dn or "(boş — LDAP kullanılmıyor, denetlenmez)", INFO)
        if not registered_flag:
            rerr = D.get("registration_errors") or {}
            R.finding(FAIL, "Lider kaydı (Ahenk) tamamlanmamış (registered=0)",
                      "Bu, Ahenk'in Lider'e kaydıdır; ETA kaydından ayrıdır:\n"
                      "· ETA kaydı — tahtanın envanteri (okul, derslik adı, board_id). ETA "
                      "Kayıt programı yapar (api-etap.eba.gov.tr). Durumu: 7. bölüm.\n"
                      "· Lider kaydı — Ahenk'in mesajlaşma kimliği (UUID/parola). Ahenk servisi "
                      "yapar (register-liderahenk.eba.gov.tr). ETA kaydı başarılı olsa bile "
                      "ayrıca tamamlanmalıdır; tamamlanmadan tahta Lider'e bağlanamaz.\n%s" % (
                          ("Neden: Lider kayıt isteği başarısız (%s) — 'Lider kayıt sunucusu' "
                           "bulgusuna bakın." % ", ".join(rerr["kinds"])) if rerr.get("count")
                          else "Neden günlükte görünmüyor; kayıt ucuna erişimi (6. bölüm) "
                               "inceleyin."))
        R.line("Kayıt zamanı", db_reg["timestamp"], INFO)
        mv = re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-([0-9a-f])", (db_reg["jid"] or "").lower())
        uuid_ver = mv.group(1) if mv else None
        legacy = registered_flag and (bool(dn) or uuid_ver == "3")
        D["legacy_identity"] = {"legacy": legacy, "uuid_version": uuid_ver,
                                "ldap_dn": bool(dn), "timestamp": db_reg["timestamp"]}
        if legacy:
            R.line("Kimlik dönemi", "ESKİ MİMARİ (LDAP dönemi): %s" % ", ".join(
                x for x in ("LDAP DN dolu" if dn else "",
                            "UUID sürüm 3 (MAC'ten türetilmiş)" if uuid_ver == "3" else "",
                            "kayıt %s" % db_reg["timestamp"]) if x), INFO)
    else:
        R.line("DB", "okunamadı: %s" % (db["error"] or "?"),
               FAIL if root else WARN)

    # conf <-> db tutarlılığı
    if root and conf_uid is not None and db_reg and not conf_uid.strip() \
            and str(db_reg["registered"]) != "1":
        R.line("conf.uid", "boş — kayıt tamamlanınca yazılır (beklenen)", INFO)
    elif root and conf_uid is not None and db_reg:
        if conf_uid != db_reg["jid"]:
            D["conf_db_mismatch"] = True
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
    R.section("5) Kimlik MAC'i, Klon/Çakışma ve Canlı Bağlantı")
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
        D["etainfo_failed"] = True
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
    reg_macs = parse_macs(reg_mac)
    D["registered_macs"] = reg_macs
    if reg_macs and live_mac:
        local = {n["mac"].lower(): n for n in nics if n.get("mac")}
        if live_mac.lower() in reg_macs:
            R.line("Kayıt MAC == canlı MAC", "%s (tutarlı)" % live_mac, OK)
        elif any(m in local for m in reg_macs):
            m = next(m for m in reg_macs if m in local)
            R.line("Kayıt MAC vs canlı MAC", "FARKLI — kayıt MAC'i bu tahtadaki %s "
                   "arayüzüne ait" % local[m]["interface"], WARN)
            R.finding(WARN, "Kimlik ağ kartı değişmiş (aynı tahta, klon değil)",
                      "Ahenk kayıt anında %s (%s) kartının MAC'ini kullanmış; bugün kimlik "
                      "MAC'i %s. İki kart da bu tahtada — klon imaj değil, kimlik kartı "
                      "seçimi değişmiş (ilk PCI, sürücülü, kablosuz olmayan kart; sürücü "
                      "yüklenmemesi, arayüz adı/sıra değişimi ya da yeni imaj nedeniyle).\n"
                      "Ahenk'in kimliği (UUID) MAC'e bağlı değildir, yeniden kayıt "
                      "GEREKMEZ. Ancak ETA kaydı eski MAC ile olabilir: 7. bölüme bakın."
                      % (m, local[m]["interface"], live_mac))
        else:
            R.line("Kayıt MAC vs canlı MAC", "FARKLI — kayıt MAC'i bu tahtada yok", FAIL)
            R.finding(FAIL, "Kayıt MAC'i bu tahtada yok: klon imaj ya da ağ kartı/anakart "
                      "değişimi",
                      "Kayıt anı MAC=%s, şu anki kimlik MAC=%s; kayıt MAC'i bu tahtadaki "
                      "hiçbir kartta yok. İki olasılık:\n"
                      "1) KLON İMAJ: bu UUID/parola başka bir tahtada üretilip imajla "
                      "kopyalanmış. Lider aynı UUID'yi iki tahtada görür → çakışma "
                      "(ConsumerBusy) / çevrimdışı. ÇÖZÜM: bu tahtada yeniden kayıt "
                      "(ahenk clean + servis restart) ile yeni UUID.\n"
                      "2) DONANIM DEĞİŞİMİ: OPS modülü ya da anakart değiştirilmiş; UUID "
                      "bu tahtanındır. ETA kaydı eski MAC ile kalmıştır (7. bölüm); "
                      "operatörden kaydın MAC'ini güncellemesini isteyin.\n"
                      "Ayırt etmek için: ConsumerBusy (aynı UUID başka tahtada çalışıyor "
                      "mu?) ve tahtanın servis geçmişi." % (", ".join(reg_macs), live_mac))

    # --- CANLI BAĞLANTI DOĞRULAMASI (statik log değil, gerçek durum) ---
    D["logs"] = {k: v for k, v in logs.items() if isinstance(v, dict)}
    live = live_broker_connections(cp)
    D["live_connection"] = live
    # En son başarılı bağlantı/iletişim olayı (datetime)
    # "already registered" yalnızca yerel kayıt kontrolüdür, bağlantı kanıtı değildir.
    success_dts = [logs[k]["last_dt"] for k in
                   ("pulsar_connected", "publish_ok", "received")
                   if logs[k]["last_dt"]]
    last_success = max(success_dts) if success_dts else None
    last_reg_ok = logs["reg_success"]["last_dt"]
    pcf_dt = logs["pulsar_conn_fail"]["last_dt"]
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
        detail = "Broker'a aktif bir bağlantı yok"
        reason = service_down_reason(D)
        if reason:
            detail += " (%s)" % reason
        elif live["error"]:
            detail += " (%s)" % live["error"]
        sev = WARN if not live["host"] else FAIL
        R.line(None, detail, sev)

    # Bağlantı CANLI mı? (çoklu kanıt)
    conn_live = (live["count"] > 0) or \
                (last_success is not None and (cb_dt is None or last_success >= cb_dt) and
                 (srv_start is None or last_success >= srv_start) and
                 not (pcf_dt and pcf_dt > last_success))

    D["conn_live"] = bool(conn_live)
    D["consumer_busy_current"] = False

    # Ahenk bekleme aşamasında mı? (servis çalışıyor, bağlantı yok, son denemeden
    # sonra "Retrying in N minutes" ile uyuyor)
    pw = logs["pulsar_wait"]
    if (active == "active" and not conn_live and pw["last_dt"]
            and (srv_start is None or pw["last_dt"] >= srv_start)
            and (last_success is None or pw["last_dt"] > last_success)):
        mins = re.search(r"Retrying in (\d+) minutes", pw.get("last_msg") or "")
        att = re.search(r"retry (\d+)/(\d+)", logs["pulsar_attempt"].get("last_msg") or "")
        nxt = pw["last_dt"] + datetime.timedelta(minutes=int(mins.group(1))) if mins else None
        D["backoff"] = {"attempt": att.group(1) if att else "?",
                        "max": att.group(2) if att else "?",
                        "minutes": int(mins.group(1)) if mins else None,
                        "next": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None}
        R.line("Ahenk bekleme aşamasında", "deneme %s/%s başarısız; %s dk bekliyor, sonraki "
               "deneme ≈ %s" % (D["backoff"]["attempt"], D["backoff"]["max"],
                                 D["backoff"]["minutes"] or "?",
                                 nxt.strftime("%H:%M") if nxt else "?"), WARN)
        R.note("Bekleme kesintisizdir; ağ düzelse bile Ahenk bu süre dolmadan denemez. "
               "Beklemeden denemek için: sudo systemctl restart ahenk.service")

    # ConsumerBusy değerlendirmesi — bayat mı, güncel mi?
    if logs["available"] and cb["count"] > 0:
        # Geçmiş (bayat) ConsumerBusy gösterilmez: servis yeniden başlamadan
        # önceki ya da sonrasında başarılı Pulsar olayı olan kayıtlar eski
        # oturuma aittir. Yalnızca güncel ConsumerBusy raporlanır.
        cb_stale = bool((srv_start and cb_dt and cb_dt < srv_start) or
                        (boot_dt and cb_dt and cb_dt < boot_dt) or
                        (last_success and cb_dt and last_success > cb_dt))
        D["consumer_busy_current"] = not cb_stale
        if not cb_stale:
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
                       (boot_dt and na["last_dt"] and na["last_dt"] < boot_dt) or \
                       (last_success and na["last_dt"] and last_success > na["last_dt"]) or \
                       (last_reg_ok and na["last_dt"] and last_reg_ok > na["last_dt"])
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
    elif conn_live:
        D["active_probe"] = {"attempted": False, "skipped": True,
                             "error": "Pulsar bağlantısı canlı"}
        R.line("Aktif Pulsar testi", "yapılmadı — Ahenk'in Pulsar mesajlaşma bağlantısı "
               "şu an canlı (%s); test yalnızca bağlantı yokken teşhis için yapılır" % (
                   "%d aktif TCP" % live["count"] if live["count"] else
                   "son başarılı olay %s" % last_success.strftime("%Y-%m-%d %H:%M:%S")),
               INFO)
    else:
        R.line("Aktif Pulsar testi", "deneniyor (adım başına en çok %d sn)..." %
               PROBE_TIMEOUT, INFO)
        progress("aktif pulsar testi yapılıyor (adım başına en çok %d sn)" % PROBE_TIMEOUT)
        probe = active_pulsar_probe(cp, registered=D.get("registered"))
        D["active_probe"] = probe
        if probe.get("skipped"):
            R.line("Aktif test", "ATLANDI: %s" % probe["error"],
                   INFO if str(D.get("registered")) != "1" else WARN)
        else:
            # Adım adım süreler
            T = probe.get("timings", {})
            for key, name, layer in PROBE_STAGES:
                if key == probe.get("failed_stage"):
                    R.line("  %s" % name, "BAŞARISIZ (%s sonra): %s" % (
                        fmt_secs(T.get(key, 0)), probe["error"]), FAIL)
                    break
                if key in T:
                    note = " (tahmini)" if key == "auth" and \
                        not probe.get("auth_measured") else ""
                    slow = T[key] >= PROBE_SLOW
                    R.line("  %s" % name, "%s%s%s" % (
                        fmt_secs(T[key]), note,
                        " — YAVAŞ (≥%d sn; sunucu tarafı)" % PROBE_SLOW if slow else ""),
                        WARN if slow else OK)
            verdict = probe_layer_verdict(probe)
            R.line("Katman", verdict, OK if probe["ok"] else
                   (FAIL if probe.get("failed_stage") in ("tcp", "tls") else WARN))
            if probe["ok"]:
                R.finding(OK, "Aktif bağlantı testi başarılı",
                          "Gerçek uid/parola ile broker'a bağlanıldı ve test mesajı "
                          "yollandı. %s (Komut aboneliğine dokunulmadı.)" % verdict)
            else:
                st = probe.get("failed_stage")
                timeout_hit = bool(re.search(r"time ?out", probe["error"] or "", re.I))
                if probe["auth_ok"] is False:
                    sev, title = FAIL, "Aktif test: Pulsar sunucusu oturumu REDDETTİ"
                    extra = ("TCP ve TLS kuruldu ama sunucu tahtanın oturum isteğini "
                             "(uid/parola ile CONNECT) hemen reddetti. En olası neden: "
                             "tahtanın uid/parolası Pulsar sunucusunda geçersiz (Lider'de "
                             "silinmiş/değişmiş kayıt ya da klon imajdan gelen eski "
                             "kimlik). Sunucu reddin nedenini bildirmez. ")
                else:
                    sev, title = FAIL, "Aktif bağlantı testi başarısız"
                    extra = ""
                R.finding(sev, title,
                          "%s%s\nAşama: %s · Hata: %s%s" % (
                              extra, verdict,
                              dict((k, n) for k, n, _ in PROBE_STAGES).get(st, st),
                              probe["error"],
                              "\nZaman aşımı adımın %d sn içinde tamamlanmadığını gösterir."
                              % probe.get("timeout", PROBE_TIMEOUT) if timeout_hit else ""))

    # Lider kaydı sunucu tarafında geçerli mi? (yerel DB yalnızca geçmişi gösterir)
    if str(D.get("registered")) == "1":
        pr = D.get("active_probe") or {}
        if conn_live:
            ls = ("sunucu tanıyor", "Ahenk bu kimlikle Pulsar'a bağlı", OK)
        elif pr.get("auth_ok") is True:
            ls = ("sunucu tanıyor", "aktif test kimlik doğrulamasını geçti", OK)
        elif pr.get("auth_ok") is False:
            ls = ("sunucu TANIMIYOR", "aktif test oturumu reddetti — kayıt Lider'de "
                  "silinmiş/değişmiş olabilir", FAIL)
        else:
            ls = ("sunucu tarafı doğrulanamadı", "canlı bağlantı yok, aktif test kimliği "
                  "doğrulayamadı", INFO)
        li = D.get("legacy_identity") or {}
        if li.get("legacy") and ls[2] == FAIL:
            ls = (ls[0], ls[1] + "; kimlik eski mimariden (LDAP dönemi)", FAIL)
        D["lider_server"] = {"short": ls[0], "evidence": ls[1]}
        R.line("Lider kaydı (sunucu tarafı)", "%s — %s" % (ls[0], ls[1]), ls[2])
        if ls[2] == INFO:
            R.note("Yerel DB'deki registered=1, Lider'in kaydı geçmişte kabul ettiğini "
                   "gösterir; bugün hâlâ geçerli olduğunu doğrulamaz.")

    # Ahenk çalışıyor ama Pulsar oturumu kurulamıyor (bu oturumda bağlantı hatası var)
    if active == "active" and not conn_live and pcf_dt and \
            (srv_start is None or pcf_dt >= srv_start):
        n_fail = logs["pulsar_conn_fail"]["count"]
        bo = D.get("backoff") or {}
        pr = D.get("active_probe") or {}
        li = D.get("legacy_identity") or {}
        cause = ""
        if pr.get("auth_ok") is False:
            cause = ("Neden: Pulsar sunucusu tahtanın kimliğini REDDEDİYOR (aktif test)%s. "
                     "Çözüm: Ahenk kimliğini sıfırlayıp yeniden kayıt (--fix bunu yapar)."
                     % (" — kimlik eski mimariden (LDAP dönemi)" if li.get("legacy") else ""))
        elif pr.get("failed_stage"):
            cause = "Neden: %s" % probe_layer_verdict(pr)
        elif li.get("legacy"):
            cause = ("Olası neden: kimlik eski mimariden (LDAP dönemi); sunucu bu kimliği "
                     "tanımıyor olabilir. Aktif test sonucuna bakın.")
        D["pulsar_session_failing"] = True
        R.finding(FAIL, "Ahenk çalışıyor ama Pulsar oturumu kurulamıyor",
                  "Servis çalışıyor, broker'a aktif bağlantı yok; günlükte son Pulsar hatası "
                  "%s (toplam %d kayıt).%s\n%s" % (
                      hms(pcf_dt), n_fail,
                      (" Ahenk bekleme aşamasında: deneme %s/%s, sonraki deneme %s." % (
                          bo.get("attempt"), bo.get("max"), hms(bo.get("next"))))
                      if bo else "", cause or "Ağ/TLS sağlamsa sorun uygulama katmanında "
                      "(Pulsar sunucusu)."))
    li = D.get("legacy_identity") or {}
    if li.get("legacy") and not conn_live and str(D.get("registered")) == "1" \
            and not D.get("pulsar_session_failing"):
        R.finding(WARN, "Ahenk kimliği eski mimariden (LDAP dönemi)",
                  "Kimlik LDAP'lı dönemde oluşturulmuş (kayıt %s%s%s). Mevcut mimaride Pulsar "
                  "sunucusu bu kimlikleri tanımayabilir; Ahenk yerel kaydı 'var' gördüğü için "
                  "('already registered') yeniden kayıt olmaz. Aktif test oturumu "
                  "reddederse --fix kimliği sıfırlayıp yeni kayıt yaptırır." % (
                      li.get("timestamp"), ", LDAP DN dolu" if li.get("ldap_dn") else "",
                      ", UUID sürüm 3" if li.get("uuid_version") == "3" else ""))

    # ---------------- 4) Bağlantı / Temel TCP / DNS ----------------
    R.section("6) Bağlantı, Temel TCP ve DNS")
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
            D["no_carrier"] = ident_iface
            R.finding(FAIL, "Kablolu ağ bağlantısı (carrier) yok",
                      "%s arayüzünde kablo/link yok. Hiçbir sunucuya ulaşılamaz." % ident_iface)
        elif not link["has_ip"]:
            D["iface_no_ip"] = ident_iface
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
            if str(D.get("registered")) != "1":
                R.line("Broker TCP", "hedef yok — kayıt tamamlanmadığı için Pulsar sunucu "
                       "adresi henüz messaging.conf'a yazılmamış", INFO)
            else:
                R.line("Broker TCP", "hedef belirlenemedi (messaging.conf'ta adres yok, "
                       "canlı soket yok)", WARN)
                R.finding(WARN, "Broker uç noktası saptanamadı",
                          "Tahta kayıtlı görünüyor ama messaging.conf'ta Pulsar sunucu "
                          "adresi yok ve canlı broker soketi bulunamadı.")
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
                            D["pulsar_ca_missing"] = ca
                            R.line("TLS trust cert", "DOSYA YOK: %s" % ca, FAIL)
                            R.finding(FAIL, "Pulsar trust sertifika dosyası yok",
                                      "tls_trust_certs_file_path=%s bulunamadı." % ca)
            else:
                if str(D.get("registered")) != "1":
                    R.line("Pulsar config", "host/port boş — kayıt tamamlanınca Lider'in "
                           "yanıtıyla yazılır", INFO)
                else:
                    R.line("Pulsar config", "host/port boş (messaging.conf)",
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
        # Geçmiş (bayat) hatalar gösterilmez: önceki açılışa ait, servis yeniden
        # başlamadan önceki, sonrasında başarılı Pulsar olayı olan ya da şu an canlı
        # broker bağlantısı varken kalan hatalar artık geçerli değildir.
        def _stale(dt):
            if live["count"] > 0:
                return True
            if dt is None:
                return False
            return bool((boot_dt and dt < boot_dt) or
                        (srv_start and dt < srv_start) or
                        (last_success and last_success > dt))
        pcf = logs["pulsar_conn_fail"]
        if pcf["count"] and not _stale(pcf["last_dt"]):
            R.line("Pulsar bağlantı hataları", "%d kez, son: %s" %
                   (pcf["count"], pcf["last"]), WARN)
        if logs["last_error"] and not _stale(logs["last_error_dt"]):
            m = re.match(r"^(?:format=)?(\S+ \S+?),\d+\s.*?\]\s*(.*)$", logs["last_error"])
            R.line("Ahenk günlüğündeki son hata",
                   "%s — %s" % (m.group(1), m.group(2)[:300]) if m else logs["last_error"],
                   WARN)
        be = logs["benign_errors"]
        if be["count"] and not _stale(be["last_dt"]):
            R.line("Bilinen zararsız Ahenk hatası", "%s: %d kez, son: %s" %
                   (be["name"], be["count"], be["last"]), INFO)
            R.note(be["explanation"])

    # ---------------- 5) ETA Kayıt Sunucusu (Okul/Şehir/İlçe) ----------------
    R.section("7) ETA Kaydı (tahta envanteri) — Okul / Şehir / İlçe")
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
        progress("eta sunucusu sorgulanıyor")
        eta = query_eta_board(query_mac, eta_backend, eta_header)
        D["eta_api"]["result"] = {k: v for k, v in eta.items() if k != "raw"}
        alt, alt_tried = None, []
        if eta.get("registered") is False and not args.mac:
            alt, alt_tried = find_alt_eta_registration(
                nics, D.get("registered_macs") or [], query_mac, eta_backend, eta_header)
            D["eta_api"]["alt_tried"] = alt_tried
            if alt:
                D["eta_api"]["alt"] = {"mac": alt[0], "source": alt[1],
                                       "result": {k: v for k, v in alt[2].items()
                                                  if k != "raw"}}
        if eta["error"] and eta["registered"] is None:
            R.line("ETA API", "ERİŞİLEMEDİ: %s" % eta["error"], FAIL)
            R.finding(FAIL, "ETA kayıt sunucusuna ulaşılamadı",
                      "%s\nLider, Ahenk'i doğrularken bu API'ye sorar. API "
                      "erişilemezse kayıt doğrulaması yapılamaz." % eta["error"])
        else:
            reg = eta["registered"]
            R.line("HTTP durumu", str(eta["status"]), INFO)
            if reg is True:
                R.line("ETA kaydı", "KAYITLI ✓", OK)
            elif reg is False and alt:
                R.line("ETA kaydı", "bu MAC ile KAYITLI DEĞİL ✗", FAIL)
                R.line("Farklı MAC ile kayıt", "BULUNDU: %s (%s)" % (alt[0], alt[1]), WARN)
                R.note("Aşağıdaki okul/birim bilgileri %s kaydındandır." % alt[0])
                eta = alt[2]
            elif reg is False:
                R.line("ETA kaydı", "KAYITLI DEĞİL ✗", FAIL)
                R.finding(FAIL, "Tahta ETA API'de KAYITLI DEĞİL",
                          "MAC=%s bu API'de kayıtlı görünmüyor.%s Lider, Ahenk "
                          "bağlanınca bu sunucuya sorar; 'kayıtlı değil' yanıtı "
                          "gelince kayıt İLERLEMEZ → makine çevrimdışı kalır.\n"
                          "ÇÖZÜM: eta-register ile okul/il/ilçe seçilerek tahta "
                          "kaydedilmeli." % (query_mac, (
                              " Ahenk'in kayıt MAC'i ve tahtadaki diğer kablolu "
                              "kartların MAC'leri de sorgulandı (%s); hiçbirinde kayıt "
                              "yok." % ", ".join(alt_tried)) if alt_tried else ""))
            else:
                R.line("ETA kaydı", "belirsiz (registered=%s)" % reg, WARN)

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
                    R.finding(OK, "ETA kaydı: %s / %s / %s" %
                              (data.get("city_name"), data.get("town_name"),
                               data.get("school_name")),
                              "Okul kodu %s · birim '%s' · board_id %s" %
                              (data.get("school_code"), data.get("unit_name"),
                               data.get("board_id")))
            elif reg is True:
                R.note("Kayıtlı ama 'data' alanı boş döndü.")
            if reg is False and alt:
                on_board = any(n["mac"].lower() == alt[0] for n in nics if n.get("mac"))
                R.finding(FAIL, "Tahta ETA'da FARKLI BİR MAC ile kayıtlı (%s)" % alt[0],
                          "Kimlik MAC'i %s ile kayıt yok, ama %s (%s) ile kayıt var: "
                          "%s / %s / %s · birim '%s' · board_id %s.\n"
                          "YENİDEN KAYDETMEYİN: ETA Kayıt'ın kayıt ekranı yeni bir kayıt "
                          "(yeni board_id) oluşturur, %s'li kayıt sahipsiz kalır (envanterde "
                          "yinelenen/pasif tahta). ETA Kayıt'ın Düzenle'si de kullanılamaz; "
                          "yalnızca mevcut MAC kayıtlıyken görünür.\nÇÖZÜM: %s"
                          "ya da ETA/Lider operatöründen board_id %s kaydının MAC'ini %s "
                          "olarak güncellemesini isteyin." % (
                              query_mac, alt[0], alt[1], data.get("city_name"),
                              data.get("town_name"), data.get("school_name"),
                              data.get("unit_name"), data.get("board_id"), alt[0],
                              ("%s MAC'li kartın yeniden kimlik kartı seçilmesini sağlayın "
                               "(sürücü yüklü mü, BIOS'ta açık mı, arayüz sırası) " % alt[0])
                              if on_board else "",
                              data.get("board_id"), query_mac))

    # Lider kayıt sunucusu hatası: operatöre iletilecek bilgilerle
    reg_err = D.get("registration_errors") or registration_request_errors(log_since)
    if reg_err["count"] and str(D.get("registered")) != "1":
        sch = D.get("school") or {}
        server_side = any(c.startswith("5") for c in reg_err["codes"])
        R.finding(FAIL, "Lider kayıt sunucusu kayıt isteğini reddediyor (%s)" %
                  ", ".join(reg_err["kinds"]),
                  "Ahenk'in kayıt isteği (%s) %d kez başarısız oldu, son: %s. Kayıt "
                  "tamamlanmadığı için tahta Lider'e bağlanamaz.\n%s\n"
                  "OPERATÖRE İLETİN: MAC %s · board_id %s · okul kodu %s · hostname %s · "
                  "son deneme %s." % (
                      reg_err["url"] or "register-liderahenk.eba.gov.tr/register",
                      reg_err["count"], reg_err["last"],
                      ("HTTP 5xx SUNUCU TARAFI hatasıdır (Lider kayıt servisi isteği "
                       "işlerken iç hataya düşüyor); tahtada yapılacak bir düzeltme yok. "
                       "ETA Kayıt → Düzenle → Kaydet yalnızca yeni UUID ile yeni bir deneme "
                       "başlatır, sonuç değişmez." if server_side else
                       "İstek sunucuya ulaşamadı ya da yanıt alınamadı; ağ ve kayıt ucu "
                       "erişimini kontrol edin."),
                      live_mac or "?", sch.get("board_id", "?"), sch.get("code", "?"),
                      platform.node(), reg_err["last"]))

    # ---------------- 6) Sistem / Dağıtım / Çekirdek ----------------
    R.section("1) Sistem / Dağıtım / Çekirdek")
    distro = None
    osrel = read_file("/etc/os-release") or ""
    m = re.search(r'PRETTY_NAME="?([^"\n]+)', osrel)
    distro = m.group(1) if m else platform.platform()
    D["distro"] = distro
    D["hostname"] = platform.node()
    D["kernel"] = platform.release()
    D["arch"] = platform.machine()
    R.line("Dağıtım", distro, INFO)
    rc, lsb, _ = run("lsb_release -d -s")
    if rc == 0 and lsb:
        R.line("lsb_release", lsb, INFO)
    R.line("Çekirdek", platform.release(), INFO)
    R.line("Mimari", "%s / %s" % (platform.machine(), platform.architecture()[0]), INFO)
    R.line("Hostname", platform.node(), INFO)
    if boot_dt:
        up = int((datetime.datetime.now() - boot_dt).total_seconds())
        D["uptime_seconds"] = up
        d_, rem = divmod(up, 86400)
        h_, rem = divmod(rem, 3600)
        m_ = rem // 60
        R.line("Çalışma süresi", "%s%d sa %d dk (açılış: %s)" % (
            "%d gün " % d_ if d_ else "", h_, m_, boot_dt.strftime("%Y-%m-%d %H:%M")), INFO)
    rc, sync, _ = run("timedatectl show -p NTPSynchronized --value")
    D["ntp_sync"] = sync if rc == 0 else None
    R.line("Saat senkron (NTP)", sync if rc == 0 else "?",
           OK if sync == "yes" else WARN)
    if rc == 0 and sync != "yes":
        R.finding(WARN, "Sistem saati NTP ile senkron değil",
                  "Saat kayması TLS sertifika doğrulamasını ve zaman damgalarını bozar.")

    # ---------------- 7) Donanım / Faz / Dokunmatik ----------------
    R.section("2) Donanım, Faz ve Dokunmatik")
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
        R.line("Dokunmatik", "tespit edilemedi (USB/HID girdi cihazı yok)", WARN)
        sch = D.get("school") or {}
        mac = D.get("live_mac") or "?"
        model = "%s %s" % (board_vendor or "?", board_name or "?")
        okul = ("%s (kod %s)" % (sch.get("school"), sch.get("code"))
                if sch.get("school") else "ETA'da kayıtlı değil")
        R.finding(WARN, "Dokunmatik çerçeve USB'de görünmüyor",
                  "Dokunmatik donanımı USB arabiriminde hiç görünmüyor; çerçeve arızalı ya da "
                  "bağlı değil (sürücü sorunu değil). Tahta USB fareyle kullanılabilir, ancak "
                  "ETA Kayıt dokunmatik denetimine takıldığı için kayıt ekranına geçemez.\n"
                  "Tahta Lider'de yönetilecekse ETA ekibinden bu tahta için kayıt desteği "
                  "isteyin. E-posta şablonu:\n"
                  "  Kime  : eta@pardus.org.tr\n"
                  "  Konu  : Dokunmatiği arızalı tahta - ETA kayıt desteği\n"
                  "  İçerik: Okul: %s · MAC: %s · Model: %s · Durum: dokunmatik çerçeve "
                  "arızalı ve temin edilemiyor (eski model), tahta USB fareyle kullanılıyor; "
                  "ETA Kayıt dokunmatik denetimine takıldığı için tahtayı kaydedemiyoruz. "
                  "Bu tahta için kayıt desteği rica ederiz.\n"
                  "Not: Tahta ETA'da kaydedildikten sonra 'ahenk-debug --fix' Ahenk'i kurup "
                  "Lider'e bağlar." % (okul, mac, model))

    # ---------------- Olumlu özet ----------------
    if not any(f[0] == FAIL for f in R.findings):
        if active == "active" and D.get("conn_live") and \
                logs.get("consumer_busy", {}).get("count", 0) == 0:
            R.finding(OK, "Kritik hata bulunamadı",
                      "Servis çalışıyor, kimlik tutarlı, ConsumerBusy yok. "
                      "Çevrimdışı görünüm geçici ağ kesintisi kaynaklı olabilir.")

    # ---------------- Zaman çizelgesi ----------------
    def ev(dt, text):
        if isinstance(dt, datetime.datetime) and (not boot_dt or dt >= boot_dt):
            R.event(dt, text)
    if boot_dt:
        R.event(boot_dt, "Tahta açıldı")
    if etareg.get("last_run"):
        ev(etareg["last_run"], "ETA Kayıt arka plan denetimi: %s" %
           (etareg["outcome_text"] or "sonuç kaydı yok"))
    if etareg.get("gui_update_ok"):
        unit = (D.get("school") or {}).get("unit_name")
        ev(etareg["gui_update_ok"], "ETA Kayıt: ETA kaydı kaydedildi/güncellendi%s" %
           (" (derslik: %s)" % unit if unit else ""))
    if etareg.get("install_dt"):
        ev(etareg["install_dt"], "ETA Kayıt: Ahenk kurulumu %s" %
           ("başarılı" if etareg["install"] == "ok" else "BAŞARISIZ"))
    if etareg.get("service_dt"):
        ev(etareg["service_dt"], "ETA Kayıt: Ahenk servisini %s" %
           ("etkinleştirip başlattı" if etareg["service"] == "ok" else "başlatamadı"))
    ev(srv_start, "Ahenk servisi başladı")
    rerr = D.get("registration_errors") or {}
    if rerr.get("count"):
        ev(rerr["first_dt"], "Ahenk: Lider kayıt isteği başarısız — %s ×%d (son: %s)" % (
            ", ".join(rerr["kinds"]), rerr["count"], hms(rerr["last"])))
    ev(logs["reg_success"]["last_dt"], "Ahenk: Lider kaydı başarılı (yeni kayıt)")
    ev(logs["already_registered"]["last_dt"], "Ahenk: yerel Lider kaydı mevcut — yeniden "
       "kayıt yapılmadı")
    ev(logs["pulsar_connected"]["last_dt"], "Ahenk: Pulsar'a bağlandı")
    if logs["pulsar_conn_fail"]["count"]:
        ev(logs["pulsar_conn_fail"]["last_dt"], "Ahenk: Pulsar bağlantı hatası")
    ev(logs["consumer_busy"]["last_dt"], "Ahenk: ConsumerBusy (abonelik başka tahtada)")
    ev(logs["publish_ok"]["last_dt"], "Ahenk: son başarılı mesaj yayını")
    if logs["stopping"]["count"]:
        ev(logs["stopping"]["last_dt"], "Ahenk kendini durdurdu%s" % {
            "registration": " (Lider kaydı başarısız)",
            "pulsar": " (Pulsar'a bağlanamadı)"}.get(
                stop_reason(logs["stopping"].get("last_msg")), ""))
    R.event(datetime.datetime.now(), "Rapor alındı — servis: %s/%s, Lider kaydı: %s" % (
        D.get("service_active"), D.get("service_enabled"), lider_status_text(D)))
    D["timeline"] = [(dt.strftime("%Y-%m-%d %H:%M:%S"), t)
                     for dt, t in sorted(R.timeline, key=lambda e: e[0])]

    return R, D


def main():
    _set_color_mode(sys.argv[1:])
    ap = _Parser(prog=PROG, add_help=False)
    ap.add_argument("-h", "--help", "--yardim", action="help")
    ap.add_argument("--renk", "--color", default="otomatik",
                    choices=("otomatik", "her-zaman", "hicbir-zaman",
                             "auto", "always", "never"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-net", action="store_true")
    ap.add_argument("--out", metavar="DOSYA")
    ap.add_argument("--db", default=None)
    ap.add_argument("--mac", metavar="MAC", default=None)
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    # --- ZORUNLU ROOT ---
    # Araç; ahenk.conf/messaging.conf (0600), parolalar, aktif Pulsar probe ve
    # süreç soketleri için root yetkisi ister. Root değilse çalıştırmayı reddet.
    if not is_root():
        sys.stderr.write(
            red("HATA: ") + "Bu araç root (sudo) yetkisiyle çalıştırılmalıdır.\n"
            "  ahenk.conf/messaging.conf (0600), parolalar, aktif bağlantı testi ve\n"
            "  süreç soketleri yalnızca root tarafından okunabilir.\n\n"
            "  Kurulumsuz (depodan) çalıştırma:\n"
            "    curl -fsSL https://raw.githubusercontent.com/enseitankado/"
            "ahenk-debug/main/run.sh | sudo bash\n"
            "  Yerel dosyadan:\n"
            "    sudo python3 ahenk_debug.py\n")
        return 3

    fixed_any = False
    R, D = diagnose(args)
    if args.fix:
        R, D, fixed_any = run_fix(args, R, D)

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
                # dosyaya renksiz (ANSI'siz) yaz
                f.write(clean_for_paste(R.render()) if not args.json else
                        json.dumps(D, indent=2, ensure_ascii=False, default=str))
            sys.stderr.write("\nRapor yazıldı: %s\n" % args.out)
        except Exception as e:
            sys.stderr.write("Rapor yazılamadı: %s\n" % e)

    # ---------------- Sunucusuz otomatik gönderim (vars. açık; --no-send kapatır) ----------------
    if (not args.no_send) and args.no_net:
        sys.stderr.write("\nOtomatik gönderim atlandı: --no-net (gönderim ağ gerektirir).\n")
    elif not args.no_send:
        # paste düz metin gösterir: renk kodlarını ve sekmeleri temizleyerek gönder
        plain = clean_for_paste(R.render())
        # 1) Tam raporu paste.rs'e yükle
        ok, url, perr = upload_paste(plain)
        # 2) Özet + linki ntfy.sh konusuna it
        topic = os.environ.get("AHENK_DEBUG_NTFY") or NTFY_TOPIC_DEFAULT
        summary, n_fail, n_warn = build_send_summary(D, R.findings)
        if url:
            summary += "\n\nTam rapor: %s" % url
        title = "ahenk%s: %s" % (" --fix" if args.fix else "",
                                 D.get("hostname") or platform.node())
        prio = "urgent" if n_fail else ("high" if n_warn else "default")
        tags = ["rotating_light"] if n_fail else (["warning"] if n_warn else ["white_check_mark"])
        nok, nerr = notify_ntfy(topic, title, summary, click=url, priority=prio, tags=tags)
        disp_topic = topic if topic.startswith("http") else "%s/%s" % (NTFY_BASE, topic)
        D["send"] = {"paste_url": url, "ntfy_topic": disp_topic,
                     "paste_ok": ok, "ntfy_ok": nok}
        progress_clear()
        # Başarıda sessiz; yalnızca gönderilemezse bildir (rapor elle iletilmeli)
        if not nok:
            reason = nerr or perr or "bilinmeyen hata"
            print("\nRapor operatöre gönderilemedi: %s" % reason)

    # çıkış kodu: FAIL varsa 2 (--fix işlem yaptıysa 4), WARN varsa 1, yoksa 0
    if any(f[0] == FAIL for f in R.findings):
        return 4 if (args.fix and fixed_any) else 2
    if any(f[0] == WARN for f in R.findings):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
