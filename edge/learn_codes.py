#!/usr/bin/env python3
"""
Learning kode IR AC lewat SMATRUL (memakai API hvac-edge lokal, jadi aman dijalankan
saat panel menyala). Jalankan di Pi:

    python3 /opt/hvac/learn_codes.py            # pelajari semua kode yang belum ada
    python3 /opt/hvac/learn_codes.py --only cool_25 off
    python3 /opt/hvac/learn_codes.py --all      # ulangi semua

v0.3: kode masuk DRAF dulu, lalu disimpan ke profil aktif di akhir (atau dibuang).
Cara yang lebih mudah: Pengaturan > Remote AC di layar panel.

Kode yang dibutuhkan: off, cool_<sp_min>..cool_<sp_max> (default 20..28 °C).
Siapkan remote AC: mode COOL, kipas AUTO.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:8080"


def call(method, path, timeout=70, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(API + path, method=method, data=data,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = str(e)
        raise RuntimeError(detail) from None
    except urllib.error.URLError as e:
        sys.exit(f"hvac-edge tidak menjawab di {API} ({e.reason}). Cek: systemctl status hvac-edge")


def ask(q):
    try:
        return input(q).strip().lower()
    except EOFError:
        return ""


def instruction(key):
    if key == "off":
        return ("Nyalakan AC dengan remote dulu. Lalu arahkan remote ke SMATRUL (≤ 10 cm) dan, "
                "saat diminta, tekan tombol POWER sehingga AC MATI.")
    t = int(key.split("_")[1])
    return (f"Arahkan remote MENJAUH dari SMATRUL, atur ke COOL, kipas AUTO, suhu {t - 1} °C.\n"
            f"   Saat diminta, arahkan ke SMATRUL (≤ 10 cm) dan tekan ▲ SEKALI sehingga layar remote {t} °C.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="pelajari ulang semua kode")
    ap.add_argument("--only", nargs="+", help="hanya kode tertentu, mis. cool_25 off")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--name", help="nama profil saat disimpan (default: nama profil aktif)")
    a = ap.parse_args()

    info = call("GET", "/api/codes")
    keys = a.only or (info["wanted"] if a.all else info["missing"])
    if not keys:
        print("Semua kode sudah dipelajari:", ", ".join(info["learned"]))
        return
    print(f"Akan dipelajari ({len(keys)}): {', '.join(keys)}\n")

    for key in keys:
        while True:
            print(f"== {key} ==\n   {instruction(key)}")
            ans = ask("   Siap? [Enter] = mulai, s = lewati, q = keluar: ")
            if ans == "q":
                sys.exit("Dihentikan. Jalankan lagi kapan saja untuk melanjutkan.")
            if ans == "s":
                break
            print(f"   Menunggu sinyal remote ({a.timeout} s)… tekan tombolnya SEKARANG")
            try:
                r = call("POST", f"/api/learn/{key}?timeout={a.timeout}")
                print(f"   ✓ tertangkap ({r['length']} karakter)")
            except RuntimeError as e:
                print(f"   ✗ {e}")
                if ask("   Ulangi? [Enter] = ya, s = lewati: ") == "s":
                    break
                continue
            if ask("   Uji: arahkan SMATRUL ke AC lalu tekan [Enter] untuk memancarkan kode (s = lewati uji): ") != "s":
                try:
                    call("POST", f"/api/test/{key}")
                except RuntimeError as e:
                    print(f"   ✗ gagal kirim: {e}")
                if ask("   AC berubah sesuai? [Enter] = ya, n = pelajari ulang: ") == "n":
                    continue
            break
        print()
    d = call("GET", "/api/draft").get("draft")
    if not d or not d["keys"]:
        print("Tidak ada kode baru.")
        return
    print("Kode baru di draf:", ", ".join(d["keys"]))
    if ask("Simpan ke profil? [Enter] = simpan, b = buang: ") == "b":
        call("POST", "/api/draft/discard")
        print("Draf dibuang. Profil lama tidak berubah.")
        return
    body = {"activate": True}
    if a.name:
        body["name"] = a.name
    r = call("POST", "/api/draft/save", body=body)["profile"]
    print(f"Tersimpan: {r['name']} · {r['count']}/{r['total']} kode")
    if r["missing"]:
        print("Belum:", ", ".join(r["missing"]))


if __name__ == "__main__":
    main()
