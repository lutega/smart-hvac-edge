#!/usr/bin/env bash
# Pasang Tailscale di Pi lalu login. Jalankan: sudo bash /opt/hvac/tailscale_setup.sh
# Tanpa layar login: TS_AUTHKEY=tskey-... sudo -E bash tailscale_setup.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo" >&2; exit 1; }
if ! command -v tailscale >/dev/null; then
  echo "== memasang Tailscale (resmi, tailscale.com/install.sh)"
  for i in 1 2 3; do curl -fsSL https://tailscale.com/install.sh | sh && break; echo "  gagal, coba lagi ($i)"; sleep 5; done
fi
systemctl enable --now tailscaled
HOST="$(hostname)"
if [[ -n "${TS_AUTHKEY:-}" ]]; then
  tailscale up --authkey "$TS_AUTHKEY" --hostname "$HOST" --ssh
else
  echo
  echo "== Buka tautan login yang muncul di bawah (bisa dari HP), masuk ke akun Tailscale Anda."
  tailscale up --hostname "$HOST" --ssh --timeout 10m
fi
echo
echo "Tailscale aktif. IP Pi: $(tailscale ip -4 | head -1)"
echo "Dari laptop/HP yang juga login Tailscale:  ssh pi@$HOST   (atau ssh pi@$(tailscale ip -4 | head -1))"
echo "Panel dari jauh (lewat SSH tunnel):        ssh -L 8080:127.0.0.1:8080 pi@$HOST  lalu buka http://localhost:8080"
