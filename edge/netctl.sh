#!/usr/bin/env bash
# netctl.sh — aksi jaringan untuk panel hvac-edge (dijalankan via sudo, lihat /etc/sudoers.d/hvac-net)
#   wifi-status | wifi-scan | wifi-connect <ssid> (password dari stdin) | wifi-forget <nama>
#   hotspot-status | hotspot-on | hotspot-off | hotspot-check
#   ts-status
# Keluaran: JSON satu baris.
set -uo pipefail
STA=wlan0
AP_CON=hvac-hotspot
HS_CONF=/etc/hvac/hotspot.conf          # SSID & password hotspot (root:pi 640)
NMCLI="nmcli"

json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
fail() { echo "{\"ok\":false,\"error\":$(json_str "$1")}"; exit 1; }

ap_iface() {                              # wlan1 (dongle USB) bila ada, kalau tidak uap0 (AP+STA satu radio)
  if [[ -e /sys/class/net/wlan1 ]]; then echo wlan1; else echo uap0; fi
}

sta_channel() { iw dev "$STA" link 2>/dev/null | awk '/freq:/ {f=int($2); if (f==2484) print 14; else if (f<2484) print (f-2407)/5; else print 0}'; }

con_by_ssid() {                           # nama profil NM untuk SSID (mis. netplan-wlan0-<ssid> di Trixie)
  local n
  while IFS= read -r n; do
    [[ "$n" == "$AP_CON" ]] && continue
    [[ "$($NMCLI -g 802-11-wireless.ssid connection show "$n" 2>/dev/null)" == "$1" ]] && { echo "$n"; return 0; }
  done < <($NMCLI -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}')
  return 1
}

internet_ok() { ping -c1 -W3 -I "$STA" 1.1.1.1 >/dev/null 2>&1 || ping -c1 -W3 -I "$STA" 8.8.8.8 >/dev/null 2>&1; }

case "${1:-}" in
wifi-status)
  python3 - <<'PY'
import json, subprocess
def run(*a):
    return subprocess.run(a, capture_output=True, text=True, timeout=20).stdout
cur = None
for l in run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "dev", "wifi", "list", "--rescan", "no").splitlines():
    p = l.replace("\\:", "\x00").split(":")
    if p and p[0] == "*":
        cur = {"ssid": p[1].replace("\x00", ":"), "signal": int(p[2] or 0), "security": p[3], "channel": p[4]}
ip = run("ip", "-4", "-o", "addr", "show", "wlan0").split()
saved = []
for l in run("nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show").splitlines():
    p = l.split(":")
    if len(p) >= 2 and p[1] == "802-11-wireless" and p[0] != "hvac-hotspot":
        ssid = run("nmcli", "-g", "802-11-wireless.ssid", "connection", "show", p[0]).strip()
        saved.append({"name": p[0], "ssid": ssid or p[0]})
print(json.dumps({"ok": True, "current": cur, "ip": ip[3].split("/")[0] if len(ip) > 3 else None, "saved": saved}))
PY
  ;;
wifi-scan)
  python3 - <<'PY'
import json, subprocess
out = subprocess.run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "dev", "wifi", "list", "--rescan", "yes"],
                     capture_output=True, text=True, timeout=40).stdout
nets = {}
for l in out.splitlines():
    p = l.replace("\\:", "\x00").split(":")
    if len(p) < 5 or not p[1]:
        continue
    ssid = p[1].replace("\x00", ":")
    n = {"ssid": ssid, "signal": int(p[2] or 0), "security": p[3] or "", "in_use": p[0] == "*", "channel": p[4]}
    if ssid not in nets or n["signal"] > nets[ssid]["signal"]:
        nets[ssid] = n
print(json.dumps({"ok": True, "networks": sorted(nets.values(), key=lambda n: (-n["in_use"], -n["signal"]))}))
PY
  ;;
wifi-connect)
  SSID="${2:-}"; [[ -n "$SSID" ]] || fail "SSID kosong"
  IFS= read -r PSK || true
  OLD=$(con_by_ssid "$SSID" || true)
  if [[ -z "$PSK" && -n "$OLD" ]]; then
    # Wi-Fi yang sudah tersimpan: cukup aktifkan
    if OUT=$($NMCLI --wait 40 connection up "$OLD" 2>&1); then echo "{\"ok\":true,\"ssid\":$(json_str "$SSID")}"; exit 0; fi
    $NMCLI device connect "$STA" >/dev/null 2>&1 &
    fail "gagal tersambung ke $SSID: ${OUT:0:120}"
  fi
  # buat/ganti profil lalu aktifkan; profil Wi-Fi lain tetap tersimpan sebagai cadangan
  [[ -n "$OLD" ]] && $NMCLI connection delete "$OLD" >/dev/null 2>&1 || true
  $NMCLI connection delete "$SSID" >/dev/null 2>&1 || true
  if [[ -n "$PSK" ]]; then
    $NMCLI connection add type wifi ifname "$STA" con-name "$SSID" ssid "$SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" connection.autoconnect yes connection.autoconnect-priority 20 >/dev/null || fail "profil gagal dibuat"
  else
    $NMCLI connection add type wifi ifname "$STA" con-name "$SSID" ssid "$SSID" \
      connection.autoconnect yes connection.autoconnect-priority 20 >/dev/null || fail "profil gagal dibuat"
  fi
  if OUT=$($NMCLI --wait 40 connection up "$SSID" 2>&1); then
    echo "{\"ok\":true,\"ssid\":$(json_str "$SSID")}"
  else
    $NMCLI connection delete "$SSID" >/dev/null 2>&1 || true
    $NMCLI device connect "$STA" >/dev/null 2>&1 &   # kembali ke Wi-Fi tersimpan
    fail "gagal tersambung ke $SSID (password salah atau sinyal lemah): ${OUT:0:120}"
  fi
  ;;
wifi-forget)
  NAME="${2:-}"; [[ -n "$NAME" && "$NAME" != "$AP_CON" ]] || fail "nama tidak valid"
  ACTIVE=$($NMCLI -t -f NAME connection show --active | grep -Fx "$NAME" || true)
  [[ -z "$ACTIVE" ]] || fail "Wi-Fi yang sedang dipakai tidak bisa dihapus"
  $NMCLI connection delete "$NAME" >/dev/null && echo '{"ok":true}' || fail "gagal menghapus"
  ;;
hotspot-status)
  IF=$(ap_iface)
  ON=false; [[ "$($NMCLI -t -f NAME connection show --active 2>/dev/null | grep -Fx "$AP_CON")" ]] && ON=true
  SSID=""; PSK=""
  [[ -f "$HS_CONF" ]] && . "$HS_CONF"
  CLIENTS="[]"
  LEASES=$(ls /var/lib/NetworkManager/dnsmasq-"$IF".leases 2>/dev/null | head -1)
  [[ -n "$LEASES" ]] && CLIENTS=$(awk '{printf "%s{\"mac\":\"%s\",\"ip\":\"%s\",\"name\":\"%s\"}", (NR>1?",":""), $2, $3, $4}' "$LEASES" | sed 's/^/[/; s/$/]/')
  [[ "$CLIENTS" == "" ]] && CLIENTS="[]"
  echo "{\"ok\":true,\"on\":$ON,\"iface\":\"$IF\",\"mode\":\"$([[ $IF == wlan1 ]] && echo dongle || echo concurrent)\",\"ssid\":$(json_str "${SSID:-}"),\"password\":$(json_str "${PSK:-}"),\"channel\":\"$(sta_channel)\",\"clients\":$CLIENTS}"
  ;;
hotspot-on)
  IF=$(ap_iface)
  if [[ ! -f "$HS_CONF" ]]; then
    HOST=$(hostname)
    printf 'SSID=%q\nPSK=%q\n' "HVAC-${HOST}" "$(tr -dc 'a-hjkmnp-z2-9' </dev/urandom | head -c 10)" > "$HS_CONF"
    chown root:pi "$HS_CONF" 2>/dev/null; chmod 640 "$HS_CONF"
  fi
  . "$HS_CONF"
  CH=$(sta_channel); [[ -n "$CH" && "$CH" != 0 ]] || CH=6
  if [[ "$IF" == uap0 ]]; then
    internet_ok || fail "wlan0 belum tersambung internet; sambungkan Wi-Fi dulu"
    [[ -e /sys/class/net/uap0 ]] || iw dev "$STA" interface add uap0 type __ap 2>/dev/null || fail "radio tidak mendukung AP+STA (iw gagal)"
    sleep 2
  fi
  $NMCLI connection delete "$AP_CON" >/dev/null 2>&1 || true
  $NMCLI connection add type wifi ifname "$IF" con-name "$AP_CON" ssid "$SSID" mode ap \
    802-11-wireless.band bg 802-11-wireless.channel "$CH" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp wifi-sec.psk "$PSK" \
    ipv4.method shared ipv6.method ignore connection.autoconnect yes >/dev/null || fail "profil hotspot gagal dibuat"
  if ! $NMCLI --wait 30 connection up "$AP_CON" >/dev/null 2>&1; then
    $0 hotspot-off >/dev/null; fail "hotspot gagal menyala di $IF"
  fi
  # pengaman: bila internet Pi putus setelah hotspot menyala, matikan lagi
  sleep 8
  if [[ "$IF" == uap0 ]] && ! internet_ok; then
    $0 hotspot-off >/dev/null
    $NMCLI device connect "$STA" >/dev/null 2>&1 || true
    fail "internet Pi putus saat hotspot menyala (radio tidak stabil di mode AP+STA); hotspot dimatikan lagi. Pakai dongle Wi-Fi USB."
  fi
  echo "{\"ok\":true,\"iface\":\"$IF\",\"ssid\":$(json_str "$SSID"),\"channel\":\"$CH\"}"
  ;;
hotspot-off)
  $NMCLI connection delete "$AP_CON" >/dev/null 2>&1 || true
  [[ -e /sys/class/net/uap0 ]] && iw dev uap0 del 2>/dev/null || true
  echo '{"ok":true}'
  ;;
hotspot-boot)
  # saat boot: nyalakan lagi hotspot bila profilnya ada (uap0 tidak bertahan setelah reboot)
  if $NMCLI -t -f NAME connection show | grep -Fxq "$AP_CON"; then
    for i in $(seq 1 30); do internet_ok && break; sleep 2; done
    exec "$0" hotspot-on
  fi
  echo '{"ok":true,"skipped":true}'
  ;;
hotspot-check)
  # dipanggil berkala: AP satu radio harus di kanal yang sama dengan Wi-Fi atas
  IF=$(ap_iface)
  [[ "$IF" == uap0 && -n "$($NMCLI -t -f NAME connection show --active | grep -Fx "$AP_CON")" ]] || { echo '{"ok":true,"changed":false}'; exit 0; }
  CH=$(sta_channel); CUR=$($NMCLI -g 802-11-wireless.channel connection show "$AP_CON")
  if [[ -n "$CH" && "$CH" != 0 && "$CH" != "$CUR" ]]; then
    $NMCLI connection modify "$AP_CON" 802-11-wireless.channel "$CH" && $NMCLI connection up "$AP_CON" >/dev/null 2>&1
    echo "{\"ok\":true,\"changed\":true,\"channel\":\"$CH\"}"
  else
    echo '{"ok":true,"changed":false}'
  fi
  ;;
ts-status)
  if ! command -v tailscale >/dev/null; then echo '{"ok":true,"installed":false}'; exit 0; fi
  IP=$(tailscale ip -4 2>/dev/null | head -1)
  STATE=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try: d=json.load(sys.stdin); print(d.get("BackendState",""), (d.get("Self") or {}).get("DNSName","").rstrip("."))
except Exception: print("")')
  echo "{\"ok\":true,\"installed\":true,\"ip\":$(json_str "$IP"),\"state\":$(json_str "${STATE%% *}"),\"dns\":$(json_str "${STATE#* }")}"
  ;;
*)
  fail "perintah tidak dikenal"
  ;;
esac
