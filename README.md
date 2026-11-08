# smart-hvac-edge

Gateway + panel kiosk Smart HVAC untuk ruang rapat (Raspberry Pi Zero 2 W + LCD 7" 1024×600 + SMATRUL YKQ002B IR/suhu/RH via Tuya LAN).
Tugas besar KS5102 — Paradigma Sistem dan Sistem Cerdas.

## Pasang / perbarui (di Pi)

Prasyarat: `setup_pi_edge.sh` + `smatrul_onboard.py` sudah pernah dijalankan (ada `/opt/hvac/venv`, `/etc/hvac/*.toml`).

```bash
curl -fsSL https://raw.githubusercontent.com/lutega/smart-hvac-edge/main/install.sh | sudo bash
```

Bisa dijalankan dari terminal Raspberry Pi Connect. Rahasia (local_key Tuya) **tidak** ada di repo ini; tetap di `/etc/hvac/secrets.toml` di Pi.

## Isi v0.3

| Layar | Isi |
|---|---|
| 1 · Idle | nama ruangan, jam & tanggal, suhu/RH, status AC, jadwal hari ini, QR booking (contoh), tombol mulai 60 menit |
| 2 · Sedang dipakai | judul rapat, durasi berjalan, jam selesai, +15 menit, suhu/RH/terasa, grafik 30 menit, ON/OFF & setpoint, rekomendasi AI (Terapkan/Abaikan) |
| 3 · Pengaturan (PIN) | Ruangan: nama, target, dimensi → luas/volume/PK, otomasi · Remote AC: profil, dataset kode, rekam lengkap/cepat/satu kode, uji, ekspor, simpan profil · Riwayat perintah + filter |

Data di Pi (`/var/lib/hvac`): `remotes/<profil>.json` (dataset IR per profil), `remotes/_draft.json` (rekaman belum disimpan),
`settings.json`, `meeting.json`, `ac_state.json`, `hvac.db` (log semua perintah), `export/`.

Rekomendasi (mesin aturan): R1 nyalakan AC saat rapat & panas · R4 turunkan setpoint bila tetap ≥ target+1,5 °C setelah 10 menit ·
R5 naikkan setpoint bila ≤ target−1 °C · R6 kelembapan ≥ 70 % · R7 matikan AC bila tidak ada rapat. Proteksi kompresor 180 s untuk ON↔OFF.

## API ringkas

`GET /api/status` · `POST /api/ac {action}` · `POST /api/recommendation/apply|dismiss {rule}` · `POST /api/meeting/start|end|extend` ·
`GET|POST /api/settings` · `POST /api/pin` · `GET|POST /api/profiles` · `POST /api/profiles/{id}/activate` · `GET|POST /api/profiles/{id}/export` ·
`POST /api/draft/start|save|discard` · `POST /api/learn/{key}` · `POST /api/test/{key}` · `GET /api/commands?kind=` · `GET /api/qr.svg`
