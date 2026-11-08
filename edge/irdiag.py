#!/usr/bin/env python3
"""Diagnosa SMATRUL: baca status, cek kode di profil aktif, kirim kode 'off' (atau --key).
Jalankan saat hvac-edge berhenti:
  sudo systemctl stop hvac-edge; /opt/hvac/venv/bin/python /opt/hvac/irdiag.py; sudo systemctl start hvac-edge
"""
import argparse, json, sys, time, traceback, tomllib
from pathlib import Path
sys.path.insert(0, "/opt/hvac/app")
import tinytuya
from tinytuya.Contrib import IRRemoteControlDevice
from hvac_edge import ir_ok

ap = argparse.ArgumentParser(); ap.add_argument("--key", default="off"); ap.add_argument("--debug", action="store_true")
a = ap.parse_args()
sm = {}
for p in ("/etc/hvac/edge.toml", "/etc/hvac/secrets.toml"):
    sm.update(tomllib.load(open(p, "rb")).get("smatrul", {}))
print(f"tinytuya {tinytuya.__version__} · SMATRUL {sm.get('device_id','?')[:6]}… di {sm.get('ip')} v{sm.get('version')}")
if a.debug:
    tinytuya.set_debug(True)
st = json.loads(Path("/var/lib/hvac/settings.json").read_text())
prof = json.loads(Path(f"/var/lib/hvac/remotes/{st['active_profile']}.json").read_text())
print(f"profil aktif: {prof['name']}")
for k, v in sorted(prof["codes"].items()):
    print(f"  {k:8} {'OK ' if ir_ok(v) else 'RUSAK'} {type(v).__name__} {len(str(v))} karakter")
d = IRRemoteControlDevice(sm["device_id"], sm["ip"], sm["local_key"], version=float(sm.get("version", 3.3)), control_type=1)
d.set_socketTimeout(5)
t0 = time.time()
print("status:", d.status(), f"({int((time.time()-t0)*1000)} ms)")
code = prof["codes"].get(a.key)
if not ir_ok(code):
    sys.exit(f"kode {a.key} tidak ada / rusak: rekam ulang dari layar Pengaturan > Remote AC > Satu kode")
print(f"kirim {a.key} … arahkan SMATRUL ke AC")
try:
    t0 = time.time(); r = d.send_button(code); print("hasil:", r, f"({int((time.time()-t0)*1000)} ms) -> amati AC")
except Exception:
    traceback.print_exc()
