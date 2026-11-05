#!/bin/zsh
# ดับเบิลคลิกใน Finder → เปิดหน้าเว็บควบคุม RBSI Control
# ครั้งแรก: สร้าง .venv + ติดตั้ง library ให้เอง (ต้องมี Python 3.11 ขึ้นไป) · ปิดหน้าต่างนี้ = ปิดหน้าเว็บ แต่บอทที่รันอยู่ไม่หยุด
cd "$(dirname "$0")"

pause_exit() { echo; echo "$1"; echo "กด Enter เพื่อปิดหน้าต่างนี้"; read; exit 1; }

find_python() {
  for p in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "▶ ติดตั้งครั้งแรก: สร้าง .venv"
  PY=$(find_python) || pause_exit "ไม่พบ Python 3.11 ขึ้นไป — ติดตั้งจาก https://www.python.org/downloads/ หรือ 'brew install python@3.12' แล้วดับเบิลคลิกใหม่"
  echo "  ใช้ $PY ($("$PY" --version))"
  rm -rf .venv
  "$PY" -m venv .venv || pause_exit "สร้าง .venv ไม่สำเร็จ"
fi

if ! .venv/bin/python -c 'import websockets' 2>/dev/null; then
  echo "▶ ติดตั้ง library (requirements.txt)"
  .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
  .venv/bin/python -m pip install -q -r requirements.txt || pause_exit "ติดตั้ง library ไม่สำเร็จ (เน็ตหลุด?) — ลองดับเบิลคลิกใหม่"
fi

[ -n "$RBSI_SETUP_ONLY" ] && { echo "ติดตั้งเสร็จ (RBSI_SETUP_ONLY)"; exit 0; }
exec .venv/bin/python -m rbsi_bot ui
