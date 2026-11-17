#!/usr/bin/env python3
"""
Smart HVAC — onboarding otomatis SMATRUL YKQ002B-WIF-WSD (Tuya IR + suhu + RH)

Dijalankan di Raspberry Pi SETELAH setup_pi_edge.sh dan setelah SMATRUL
dipasangkan (pairing) di aplikasi Smart Life:

  sudo /opt/hvac/venv/bin/python smatrul_onboard.py \
       --key <ACCESS_ID> --secret <ACCESS_SECRET> --region sg

Langkah otomatis:
  1. Login Tuya Cloud (sekali) -> daftar perangkat + local_key
  2. Pilih perangkat SMATRUL (IR + sensor) secara otomatis
  3. Cari IP & versi protokol di LAN (UDP broadcast)
  4. Petakan DPS suhu/RH + skala (spesifikasi cloud, fallback heuristik)
  5. Uji baca lokal suhu/RH 3x (tanpa cloud)
  6. Tulis /etc/hvac/secrets.toml dan /etc/hvac/edge.toml (cadangan .bak)
  7. (opsional --ir-test) uji IR end-to-end: tekan 1 tombol remote AC ke arah
     SMATRUL, kode ditangkap lalu dipancarkan ulang
Hasil: /var/lib/hvac/smatrul_onboard.json

Region = data center project di Tuya IoT Platform: sg, us, us-e, eu, eu-w, in, cn.
Tanpa --region, semua region dicoba berurutan.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

try:
    import tinytuya
except ImportError:
    sys.exit("tinytuya belum terpasang. Jalankan dengan /opt/hvac/venv/bin/python")

CFG_DIR = "/etc/hvac"
SECRETS = os.path.join(CFG_DIR, "secrets.toml")
EDGE = os.path.join(CFG_DIR, "edge.toml")
RESULT = "/var/lib/hvac/smatrul_onboard.json"
REGIONS = ["sg", "us", "eu", "in", "us-e", "eu-w", "cn"]
HINTS = ("ykq", "ir", "remote", "infrared", "temp", "humid", "smart ir", "wsd")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg, code=1):
    print(f"\nGAGAL: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
def cloud_login(key, secret, regions, device_id=None):
    last_err = None
    for r in regions:
        try:
            c = tinytuya.Cloud(apiRegion=r, apiKey=key, apiSecret=secret,
                               apiDeviceID=device_id, configFile="/nonexistent")
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        if c.error:
            last_err = c.error
            continue
        devs = c.getdevices(verbose=False)
        if isinstance(devs, dict) and devs.get("Error"):
            last_err = devs
            continue
        if devs:
            log(f"Tuya Cloud OK di region '{r}', {len(devs)} perangkat")
            return c, r, devs
        last_err = f"region {r}: 0 perangkat"
    fail("Tidak bisa mengambil daftar perangkat dari Tuya Cloud.\n"
         f"  Detail terakhir: {last_err}\n"
         "  Cek: Access ID/Secret benar, project sudah ditautkan ke akun Smart Life\n"
         "  (Devices > Link App Account), dan API 'IoT Core' + 'Authorization' aktif.")


def pick_device(devs, wanted):
    devs = [d for d in devs if not d.get("sub")]
    if wanted:
        for d in devs:
            if wanted.lower() in (d.get("id", "") + d.get("name", "")).lower():
                return d
        fail(f"Perangkat '{wanted}' tidak ditemukan. Ada: " +
             ", ".join(f"{d.get('name')} ({d.get('id')})" for d in devs))

    def score(d):
        text = " ".join(str(d.get(k, "")) for k in
                        ("name", "product_name", "model", "category")).lower()
        s = sum(h in text for h in HINTS)
        if d.get("category") in ("wnykq", "hwktwkq", "qt"):  # kategori IR Tuya
            s += 3
        return s

    ranked = sorted(devs, key=score, reverse=True)
    if not ranked or score(ranked[0]) == 0:
        fail("Tidak menemukan perangkat yang mirip SMATRUL. Pakai --device <nama/id>. Ada: " +
             ", ".join(f"{d.get('name')} ({d.get('id')})" for d in devs))
    if len(ranked) > 1 and score(ranked[0]) == score(ranked[1]):
        log("Beberapa kandidat setara; dipakai yang pertama. Pakai --device untuk memilih.")
    return ranked[0]


def lan_find(dev_id, tries=3):
    for i in range(tries):
        log(f"Memindai LAN (percobaan {i + 1}/{tries})…")
        found = tinytuya.deviceScan(verbose=False, maxretry=None, color=False,
                                    poll=False, byID=True)
        if dev_id in found:
            f = found[dev_id]
            return f.get("ip"), str(f.get("version") or "3.3")
        time.sleep(2)
    return None, None


def map_dps(cloud, dev_id, live):
    """Kembalikan (dp_temp, scale_temp, dp_hum, scale_hum)."""
    temp = hum = None
    try:
        spec = cloud.getdps(dev_id)
        items = []
        if isinstance(spec, dict):
            res = spec.get("result", spec)
            items = (res.get("status") or []) + (res.get("functions") or [])
        for it in items:
            code = str(it.get("code", "")).lower()
            dp = str(it.get("dp_id", ""))
            vals = it.get("values")
            try:
                vals = json.loads(vals) if isinstance(vals, str) else (vals or {})
            except ValueError:
                vals = {}
            scale = 10 ** int(vals.get("scale", 0) or 0)
            if not dp or "set" in code or "unit" in code or "alarm" in code:
                continue
            if temp is None and "temp" in code:
                temp = (dp, scale)
            elif hum is None and "humid" in code:
                hum = (dp, scale)
    except Exception as e:  # noqa: BLE001
        log(f"Spesifikasi cloud tidak terbaca ({e}); memakai heuristik")

    # Heuristik dari nilai live bila cloud tidak memberi jawaban
    nums = {k: v for k, v in live.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if temp is None:
        temp = next(((k, 10) for k, v in nums.items() if 150 <= v <= 450), None)
    if temp is None:
        temp = next(((k, 1) for k, v in nums.items() if 15 <= v <= 45), None)
    if hum is None:
        for k, v in nums.items():
            if temp and k == temp[0]:
                continue
            if 10 <= v <= 100:
                hum = (k, 1); break
            if 100 < v <= 1000:
                hum = (k, 10); break
    return temp, hum


def read_local(dev_id, ip, key, ver):
    d = tinytuya.Device(dev_id, ip, key, version=float(ver))
    d.set_socketTimeout(5)
    d.set_socketRetryLimit(2)
    st = d.status()
    if not isinstance(st, dict) or "dps" not in st:
        return None, st
    return st["dps"], st


def toml_set(path, assignments):
    """Ganti nilai baris `key = ...` (kunci unik di file template kita)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    shutil.copy2(path, path + ".bak")
    for k, v in assignments.items():
        val = f'"{v}"' if isinstance(v, str) else str(v)
        pat = re.compile(rf'^(\s*{re.escape(k)}\s*=\s*)([^#\n]*?)(\s*(#.*)?)$', re.M)
        if pat.search(text):
            text = pat.sub(lambda m: f"{m.group(1)}{val}{m.group(3)}", text, count=1)
        else:
            log(f"Kunci '{k}' tidak ada di {path}; dilewati")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def ir_test(dev_id, ip, key, ver, timeout):
    from tinytuya.Contrib import IRRemoteControlDevice
    ir = IRRemoteControlDevice(dev_id, ip, key, version=float(ver))
    log(f"UJI IR: dalam {timeout} s, arahkan remote AC ke SMATRUL (≤ 10 cm) dan tekan 1 tombol…")
    code = ir.receive_button(timeout=timeout)
    if not code:
        log("Tidak ada kode tertangkap (uji IR dilewati)")
        return None
    log(f"Kode tertangkap ({len(code)} karakter base64). Memancarkan ulang dalam 3 s — perhatikan AC…")
    time.sleep(3)
    ir.send_button(code)
    return code


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Onboarding otomatis SMATRUL untuk Smart HVAC")
    ap.add_argument("--key", default=os.environ.get("TUYA_API_KEY"), help="Access ID/Client ID Tuya IoT")
    ap.add_argument("--secret", default=os.environ.get("TUYA_API_SECRET"), help="Access Secret Tuya IoT")
    ap.add_argument("--region", default=os.environ.get("TUYA_REGION"), choices=REGIONS)
    ap.add_argument("--device", help="Nama atau ID perangkat (bila otomatis salah pilih)")
    ap.add_argument("--ip", help="IP SMATRUL bila pemindaian LAN gagal")
    ap.add_argument("--ir-test", action="store_true", help="Uji IR end-to-end (perlu menekan 1 tombol remote)")
    ap.add_argument("--ir-timeout", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="Jangan tulis file konfigurasi")
    a = ap.parse_args()

    if not a.key or not a.secret:
        fail("Butuh --key dan --secret (Access ID/Secret dari project Tuya IoT Platform)")
    if not a.dry_run and os.geteuid() != 0:
        fail("Jalankan dengan sudo (perlu menulis /etc/hvac)")

    regions = [a.region] if a.region else REGIONS
    cloud, region, devs = cloud_login(a.key, a.secret, regions)

    dev = pick_device(devs, a.device)
    dev_id, local_key = dev["id"], dev.get("key", "")
    log(f"Perangkat: {dev.get('name')} | {dev.get('product_name', '')} | id={dev_id}")
    if not local_key:
        fail("local_key kosong dari cloud. Pastikan akun Smart Life sudah ditautkan ke project.")

    ip, ver = (a.ip, "3.3") if a.ip else lan_find(dev_id)
    if not ip:
        fail("SMATRUL tidak ditemukan di LAN. Pastikan Pi dan SMATRUL di Wi-Fi 2,4 GHz/subnet yang sama "
             "tanpa client isolation, atau beri --ip <alamat>.")
    log(f"Ditemukan di LAN: ip={ip} versi={ver}")

    dps, raw = read_local(dev_id, ip, local_key, ver)
    if dps is None and ver != "3.3":
        for alt in ("3.3", "3.4", "3.5"):
            dps, raw = read_local(dev_id, ip, local_key, alt)
            if dps is not None:
                ver = alt
                break
    if dps is None:
        fail(f"Gagal membaca status lokal: {raw}")
    log(f"Status lokal (versi {ver}): {dps}")

    temp, hum = map_dps(cloud, dev_id, dps)
    if not temp:
        fail(f"DPS suhu tidak teridentifikasi. Status: {dps}. Isi manual di {EDGE}.")
    log(f"DPS suhu = {temp[0]} (skala {temp[1]}), DPS RH = {hum[0] if hum else '?'} "
        f"(skala {hum[1] if hum else '?'})")

    samples = []
    for i in range(3):
        d, _ = read_local(dev_id, ip, local_key, ver)
        if d:
            t = d.get(temp[0]); h = d.get(hum[0]) if hum else None
            t_c = round(t / temp[1], 1) if isinstance(t, (int, float)) else None
            h_p = round(h / hum[1], 1) if isinstance(h, (int, float)) and hum else None
            samples.append({"temp_c": t_c, "rh_pct": h_p})
            log(f"  bacaan {i + 1}: {t_c} °C, {h_p} %RH")
        if i < 2:
            time.sleep(10)
    ok = [s for s in samples if s["temp_c"] is not None and 5 <= s["temp_c"] <= 50]
    if not ok:
        fail("Nilai suhu tidak masuk akal; periksa pemetaan DPS secara manual.")

    code = None
    if a.ir_test:
        try:
            code = ir_test(dev_id, ip, local_key, ver, a.ir_timeout)
        except Exception as e:  # noqa: BLE001
            log(f"Uji IR gagal: {e}")

    if not a.dry_run:
        toml_set(SECRETS, {"device_id": dev_id, "ip": ip, "local_key": local_key, "version": ver})
        edge = {"dps_temp": str(temp[0]), "temp_scale": temp[1]}
        if hum:
            edge["dps_humidity"] = str(hum[0])
            edge["rh_scale"] = hum[1]
        toml_set(EDGE, edge)
        os.makedirs(os.path.dirname(RESULT), exist_ok=True)
        result = {
            "ts": int(time.time()), "region": region, "device_id": dev_id,
            "name": dev.get("name"), "product": dev.get("product_name"), "ip": ip,
            "version": ver, "dps_temp": temp[0], "temp_scale": temp[1],
            "dps_humidity": hum[0] if hum else None, "rh_scale": hum[1] if hum else None,
            "samples": samples, "raw_dps": dps, "ir_test_code_len": len(code) if code else None,
        }
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        os.chmod(RESULT, 0o640)
        log(f"Konfigurasi ditulis: {SECRETS}, {EDGE} (cadangan .bak); ringkasan: {RESULT}")

    print("\nSELESAI. Langkah berikutnya:")
    print(f"  - Buat DHCP reservation di router untuk IP SMATRUL {ip}")
    print("  - Setelah stabil, internet SMATRUL boleh diblok di router (kontrol tetap lokal)")
    print("  - Kalibrasi: bandingkan suhu/RH dengan termometer referensi 1 jam, isi temp_offset_c/rh_offset_pct")
    if not a.ir_test:
        print("  - Uji IR: jalankan ulang dengan --ir-test")


if __name__ == "__main__":
    main()
