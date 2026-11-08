#!/usr/bin/env bash
# Pasang / perbarui hvac-edge v0.3 di Pi (butuh setup_pi_edge.sh sudah pernah jalan).
# Jalankan: sudo bash edge/update_edge.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PI_USER="${PI_USER:-pi}"
[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo" >&2; exit 1; }
[[ -d /opt/hvac/venv ]] || { echo "!! /opt/hvac/venv tidak ada. Jalankan setup_pi_edge.sh dulu." >&2; exit 1; }
sed -i 's/\r$//' "$DIR"/*.py "$DIR"/*.sh "$DIR"/panel/*.html 2>/dev/null || true

echo "== 1/5 salin aplikasi & panel"
install -d -o "$PI_USER" -g "$PI_USER" /opt/hvac/app /opt/hvac/panel_static
install -o "$PI_USER" -g "$PI_USER" -m 644 "$DIR/hvac_edge.py"      /opt/hvac/app/hvac_edge.py
install -o "$PI_USER" -g "$PI_USER" -m 644 "$DIR/panel/index.html"  /opt/hvac/panel_static/index.html
install -o "$PI_USER" -g "$PI_USER" -m 755 "$DIR/learn_codes.py"    /opt/hvac/learn_codes.py
install -d -o "$PI_USER" -g "$PI_USER" /var/lib/hvac /var/lib/hvac/remotes /var/lib/hvac/export
chown -R "$PI_USER:$PI_USER" /var/lib/hvac
[[ -f /var/lib/hvac/codes.json ]] && cp -n /var/lib/hvac/codes.json /var/lib/hvac/codes.before-v03.json || true

echo "== 2/5 paket Python (segno untuk QR)"
for i in 1 2 3; do
  /opt/hvac/venv/bin/pip install -q --disable-pip-version-check segno && break
  echo "   pip gagal, coba lagi ($i)"; sleep 5
done || echo "   !! segno tidak terpasang: QR tidak tampil, fungsi lain tetap jalan"

echo "== 3/5 konfigurasi"
if ! grep -q '^\[ac\]' /etc/hvac/edge.toml; then
  cat >>/etc/hvac/edge.toml <<'TOML'

[ac]
sp_min = 20        # setpoint terendah yang dipakai/direkam (°C)
sp_max = 28        # setpoint tertinggi
sp_default = 25
TOML
  echo "   [ac] ditambahkan ke /etc/hvac/edge.toml"
fi
if [[ "$(timedatectl show -p Timezone --value 2>/dev/null)" =~ ^(Etc/UTC|UTC)$ ]]; then
  timedatectl set-timezone Asia/Jakarta && echo "   zona waktu -> Asia/Jakarta (jam panel & jadwal)"
fi

echo "== 4/5 layanan"
sed -i 's/hvac_edge_stub:app/hvac_edge:app/' /etc/systemd/system/hvac-edge.service
systemctl daemon-reload
systemctl restart hvac-edge.service
for i in $(seq 1 60); do curl -fs http://127.0.0.1:8080/health >/dev/null && break; sleep 1; done
curl -fs http://127.0.0.1:8080/health || { echo; echo "!! hvac-edge belum menjawab:"; journalctl -u hvac-edge -n 40 --no-pager; exit 1; }
echo
systemctl restart hvac-kiosk.service || true

echo "== 5/5 cek"
curl -s http://127.0.0.1:8080/api/profiles | /opt/hvac/venv/bin/python -c '
import json,sys; d=json.load(sys.stdin)
print("   profil aktif:", d["active"] or "-")
for p in d["profiles"]: print(f"   - {p[\"name\"]}: {p[\"count\"]}/{p[\"total\"]} kode")'
curl -fs -o /dev/null http://127.0.0.1:8080/api/qr.svg && echo "   QR booking: OK" || echo "   QR booking: tidak tersedia"
echo
echo "Selesai. Panel memuat ulang. PIN teknisi = [panel] technician_pin di /etc/hvac/edge.toml (bawaan 0000)."
echo "Rekam remote: layar Pengaturan > Remote AC > Panduan lengkap   (atau: python3 /opt/hvac/learn_codes.py)"
