#!/usr/bin/env bash
#
# ahenk-debug önyükleyici — indirme/kurulum gerektirmez.
# Aracı doğrudan depodan çekip çalıştırır (kalıcı dosya yazmaz).
#
# Kullanım:
#   curl -fsSL https://raw.githubusercontent.com/enseitankado/ahenk-debug/main/run.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/enseitankado/ahenk-debug/main/run.sh | sudo bash -s -- --json
#   curl -fsSL https://raw.githubusercontent.com/enseitankado/ahenk-debug/main/run.sh | sudo bash -s -- --no-net
#
# Not: Araç root ister; bu yüzden 'sudo bash' ile çağırın.
#
set -euo pipefail

# İndirilecek araç (gerekirse AHENK_DEBUG_URL ile geçersiz kılınabilir)
RAW_URL="${AHENK_DEBUG_URL:-https://raw.githubusercontent.com/enseitankado/ahenk-debug/main/ahenk_debug.py}"

# Gerekli araçlar
command -v python3 >/dev/null 2>&1 || { echo "HATA: python3 bulunamadı." >&2; exit 1; }

# İndirici seç (curl ya da wget)
if command -v curl >/dev/null 2>&1; then
  DL() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then
  DL() { wget -qO- "$1"; }
else
  echo "HATA: curl veya wget bulunamadı." >&2
  exit 1
fi

# Aracı bir akış olarak çekip python3'e ver — kalıcı dosya yazılmaz.
# Process substitution kullanılır; argümanlar ("$@") araca aktarılır.
exec python3 <(DL "$RAW_URL") "$@"
