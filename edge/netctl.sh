#!/usr/bin/env bash
# netctl.sh — aksi jaringan untuk panel hvac-edge (dijalankan via sudo, lihat /etc/sudoers.d/hvac-net)
#   wifi-status | wifi-scan | wifi-connect <ssid> (password dari stdin) | wifi-forget <nama>
#   hotspot-status | hotspot-on | hotspot-off | hotspot-check
#   ts-status
# Keluaran: JSON satu baris.
set -uo pipefail
AP_CON=hvac-hotspot
UP_CON=hvac-uplink                       # profil internet di dongle saat mode "tukar"
HS_CONF=/etc/hvac/hotspot.conf          # SSID & password hotspot (root:pi 640)
HS_STATE=/etc/hvac/hotspot.state        # profil Wi-Fi wlan0 yang dinonaktifkan selama mode "tukar"
NMCLI="nmcli"

supports_ap() {                           # radio interface $1 bisa jadi access point?
  local phy; phy=$(cat /sys/class/net/"$1"/phy80211/name 2>/dev/null) || return 1
  iw phy "$phy" info 2>/dev/null | grep -Eq '^\s+\* AP$'
}

# Peran radio:
#   dongle     : wlan1 bisa AP  -> wlan0 internet, wlan1 hotspot
#   tukar      : wlan1 tak bisa AP (mis. mt7601u) -> wlan1 internet, wlan0 (bawaan) hotspot
#   concurrent : tanpa dongle -> wlan0 internet + uap0 hotspot di radio yang sama (eksperimental)
hs_mode() {
  if [[ -e /sys/class/net/wlan1 ]]; then
    if supports_ap wlan1; then echo dongle; elif supports_ap wlan0; then echo tukar; else echo none; fi
  else echo concurrent; fi
}
ap_iface() { case "$(hs_mode)" in dongle) echo wlan1;; tukar) echo wlan0;; *) echo uap0;; esac; }
uplink_iface() {                          # interface yang sedang membawa rute default
  local d; d=$(ip route show default 2>/dev/null | awk '/dev wlan/ {for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}' | head -1)
  echo "${d:-wlan0}"
}
STA=$(uplink_iface)

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

internet_ok() { local i="${1:-$STA}"; ping -c1 -W3 -I "$i" 1.1.1.1 >/dev/null 2>&1 || ping -c1 -W3 -I "$i" 8.8.8.8 >/dev/null 2>&1; }

case "${1:-}" in
wifi-status)
  STA="$STA" python3 - <<'PY'
import json, os, subprocess
def run(*a):
    return subprocess.run(a, capture_output=True, text=True, timeout=20).stdout
sta = os.environ["STA"]
cur = None
for l in run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "dev", "wifi", "list", "ifname", sta, "--rescan", "no").splitlines():
    p = l.replace("\\:", "\x00").split(":")
    if p and p[0] == "*":
        cur = {"ssid": p[1].replace("\x00", ":"), "signal": int(p[2] or 0), "security": p[3], "channel": p[4]}
ip = run("ip", "-4", "-o", "addr", "show", sta).split()
saved = []
for l in run("nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show").splitlines():
    p = l.split(":")
    if len(p) >= 2 and p[1] == "802-11-wireless" and p[0] not in ("hvac-hotspot", "hvac-uplink"):
        ssid = run("nmcli", "-g", "802-11-wireless.ssid", "connection", "show", p[0]).strip()
        saved.append({"name": p[0], "ssid": ssid or p[0]})
print(json.dumps({"ok": True, "iface": sta, "current": cur, "ip": ip[3].split("/")[0] if len(ip) > 3 else None, "saved": saved}))
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
  CH=$($NMCLI -g 802-11-wireless.channel connection show "$AP_CON" 2>/dev/null); [[ -n "$CH" ]] || CH=$(sta_channel)
  echo "{\"ok\":true,\"on\":$ON,\"iface\":\"$IF\",\"mode\":\"$(hs_mode)\",\"uplink\":\"$STA\",\"ssid\":$(json_str "${SSID:-}"),\"password\":$(json_str "${PSK:-}"),\"channel\":\"$CH\",\"clients\":$CLIENTS}"
  ;;
hotspot-on)
  IF=$(ap_iface)
  if [[ ! -f "$HS_CONF" ]]; then
    HOST=$(hostname)
    printf 'SSID=%q\nPSK=%q\n' "HVAC-${HOST}" "$(tr -dc 'a-hjkmnp-z2-9' </dev/urandom | head -c 10)" > "$HS_CONF"
    chown root:pi "$HS_CONF" 2>/dev/null; chmod 640 "$HS_CONF"
  fi
  . "$HS_CONF"
  MODE=$(hs_mode)
  [[ "$MODE" == none ]] && fail "tidak ada radio yang mendukung mode hotspot"
  CH=$(sta_channel); [[ -n "$CH" && "$CH" != 0 ]] || CH=6
  if [[ "$MODE" == tukar && "$STA" == wlan0 ]]; then
    # 1) pindahkan internet ke dongle dengan Wi-Fi & password yang sama
    ORIG=$($NMCLI -g GENERAL.CONNECTION device show wlan0 2>/dev/null)
    [[ -n "$ORIG" && "$ORIG" != "$AP_CON" ]] || fail "wlan0 belum tersambung Wi-Fi; sambungkan dulu"
    USSID=$($NMCLI -g 802-11-wireless.ssid connection show "$ORIG")
    UPSK=$($NMCLI -s -g 802-11-wireless-security.psk connection show "$ORIG" 2>/dev/null)
    $NMCLI connection delete "$UP_CON" >/dev/null 2>&1 || true
    if [[ -n "$UPSK" ]]; then
      $NMCLI connection add type wifi ifname wlan1 con-name "$UP_CON" ssid "$USSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$UPSK" \
        connection.autoconnect yes connection.autoconnect-priority 30 >/dev/null || fail "profil internet dongle gagal dibuat"
    else
      $NMCLI connection add type wifi ifname wlan1 con-name "$UP_CON" ssid "$USSID" \
        connection.autoconnect yes connection.autoconnect-priority 30 >/dev/null || fail "profil internet dongle gagal dibuat"
    fi
    if ! $NMCLI --wait 40 connection up "$UP_CON" >/dev/null 2>&1 || ! { sleep 3; internet_ok wlan1; }; then
      $NMCLI connection delete "$UP_CON" >/dev/null 2>&1
      fail "dongle wlan1 gagal tersambung ke $USSID; Wi-Fi bawaan tidak diubah"
    fi
    echo "ORIG=$(printf %q "$ORIG")" > "$HS_STATE"
    $NMCLI connection modify "$ORIG" connection.autoconnect no
    STA=wlan1
    # 2) kanal hotspot dijauhkan dari kanal internet (dua radio berdekatan)
    UCH=$(iw dev wlan1 link 2>/dev/null | awk '/freq:/ {print (int($2)-2407)/5}')
    CH=1; [[ -n "$UCH" && "$UCH" -le 4 ]] && CH=11
  fi
  if [[ "$MODE" == dongle ]]; then
    CH=1; [[ "$(sta_channel)" -le 4 ]] 2>/dev/null && CH=11
  fi
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
    "$0" hotspot-off >/dev/null; fail "hotspot gagal menyala di $IF"
  fi
  if [[ "$MODE" == tukar ]] && ! { sleep 5; internet_ok wlan1; }; then
    "$0" hotspot-off >/dev/null; fail "internet lewat dongle putus setelah hotspot menyala; dikembalikan seperti semula"
  fi
  # pengaman: bila internet Pi putus setelah hotspot menyala, matikan lagi
  sleep 8
  if [[ "$IF" == uap0 ]] && ! internet_ok; then
    "$0" hotspot-off >/dev/null
    $NMCLI device connect "$STA" >/dev/null 2>&1 || true
    fail "internet Pi putus saat hotspot menyala (radio tidak stabil di mode AP+STA); hotspot dimatikan lagi. Pakai dongle Wi-Fi USB."
  fi
  echo "{\"ok\":true,\"mode\":\"$MODE\",\"iface\":\"$IF\",\"uplink\":\"$STA\",\"ssid\":$(json_str "$SSID"),\"channel\":\"$CH\"}"
  ;;
hotspot-off)
  $NMCLI connection delete "$AP_CON" >/dev/null 2>&1 || true
  [[ -e /sys/class/net/uap0 ]] && iw dev uap0 del 2>/dev/null || true
  if [[ -f "$HS_STATE" ]]; then           # mode tukar: kembalikan internet ke Wi-Fi bawaan
    . "$HS_STATE"
    $NMCLI connection modify "$ORIG" connection.autoconnect yes 2>/dev/null
    if $NMCLI --wait 40 connection up "$ORIG" >/dev/null 2>&1; then
      $NMCLI connection delete "$UP_CON" >/dev/null 2>&1 || true
    fi
    rm -f "$HS_STATE"
  fi
  echo '{"ok":true}'
  ;;
hotspot-boot)
  # saat boot: nyalakan lagi hotspot bila profilnya ada (uap0 tidak bertahan setelah reboot)
  if $NMCLI -t -f NAME connection show | grep -Fxq "$AP_CON"; then
    [[ "$(ap_iface)" == uap0 ]] || { echo '{"ok":true,"skipped":"NetworkManager menyalakan sendiri"}'; exit 0; }
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
