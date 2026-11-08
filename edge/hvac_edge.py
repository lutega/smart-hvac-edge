"""
hvac-edge v0.3 (MVP) — gateway + panel kiosk Smart HVAC di Raspberry Pi Zero 2 W

Baru di v0.3
- Profil remote AC: dataset kode IR disimpan per profil di /var/lib/hvac/remotes/<id>.json,
  profil aktif dicatat di settings.json. codes.json v0.2 otomatis dipindah jadi profil pertama.
- Rekam dari layar: kode baru masuk DRAF dulu (remotes/_draft.json), baru ditulis ke profil
  saat "Simpan profil". Batal/buang tidak merusak dataset yang sudah jalan.
- Pengaturan ruangan: nama, dimensi P x L x T (luas, volume, perkiraan PK), target suhu, PIN.
- Rapat: jadwal contoh, mulai/akhiri manual, durasi berjalan & jam selesai,
  mulai otomatis dari jadwal, AC dimatikan otomatis saat rapat selesai (bisa diatur).
- Rekomendasi suhu berbasis aturan (R1 R4 R5 R6 R7) + tombol Terapkan/Abaikan, semua dicatat.
- QR booking (contoh) sebagai SVG: GET /api/qr.svg (butuh paket segno).

Semua perintah gateway -> IR blaster tetap dicatat di SQLite /var/lib/hvac/hvac.db (command_log).
"""
import asyncio
import io
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VERSION = "0.3.0"
STATIC = Path(os.environ.get("HVAC_STATIC", "/opt/hvac/panel_static"))
EDGE_CFG = Path(os.environ.get("HVAC_EDGE_CFG", "/etc/hvac/edge.toml"))
SECRETS = Path(os.environ.get("HVAC_SECRETS", "/etc/hvac/secrets.toml"))
DATA = Path(os.environ.get("HVAC_DATA", "/var/lib/hvac"))
LEGACY_CODES = DATA / "codes.json"
REMOTES = DATA / "remotes"
DRAFT = REMOTES / "_draft.json"
EXPORTS = DATA / "export"
SETTINGS = DATA / "settings.json"
STATE = DATA / "ac_state.json"
MEETING = DATA / "meeting.json"
DB = DATA / "hvac.db"
DEBOUNCE_S = 1.0
QUICK_KEYS = ["off", "cool_22", "cool_24", "cool_25", "cool_26"]
DISMISS_S = 15 * 60          # rekomendasi yang diabaikan tidak muncul lagi selama ini
SETTLE_S = 10 * 60           # setelah setpoint diubah, beri waktu ruangan merespons

app = FastAPI(title="hvac-edge", version=VERSION)
BOOT_TS = time.time()
ROOM = {"temp_c": None, "rh_pct": None, "ts": None, "error": "belum dikonfigurasi"}
EXEC_LOCK = threading.RLock()
DB_LOCK = threading.Lock()
FILE_LOCK = threading.RLock()
DISMISSED = {}               # rule -> ts
LEARNING = {"key": None, "until": None}


# ----------------------------------------------------------------- konfigurasi
def load_cfg():
    cfg = {}
    for p in (EDGE_CFG, SECRETS):
        try:
            with open(p, "rb") as f:
                for k, v in tomllib.load(f).items():
                    if isinstance(v, dict):
                        cfg.setdefault(k, {}).update(v)
                    else:
                        cfg[k] = v
        except Exception as e:  # noqa: BLE001
            cfg.setdefault("_errors", []).append(f"{p.name}: {e}")
    return cfg


def ac_cfg(cfg):
    a = cfg.get("ac", {})
    c = cfg.get("control", {})
    return {
        "sp_min": int(a.get("sp_min", 20)),
        "sp_max": int(a.get("sp_max", 28)),
        "sp_default": int(a.get("sp_default", 25)),
        "min_power_interval_s": int(c.get("min_cmd_interval_s", 180)),
    }


def wanted_keys(acc):
    return ["off"] + [f"cool_{t}" for t in range(acc["sp_min"], acc["sp_max"] + 1)]


def smatrul_ready(sm):
    return all(sm.get(k) for k in ("device_id", "ip", "local_key"))


# ------------------------------------------------------------ file JSON
def read_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


DEFAULT_SCHEDULE = [
    {"start": "08:00", "end": "09:00", "title": "Daily stand-up", "organizer": "Tim Produk", "people": 6},
    {"start": "11:00", "end": "12:00", "title": "Review desain panel", "organizer": "Lutfi", "people": 6},
    {"start": "13:30", "end": "15:00", "title": "Sprint planning", "organizer": "Tim Engineering", "people": 10},
    {"start": "16:00", "end": "16:30", "title": "Sesi mentoring", "organizer": "", "people": 2},
]


def load_settings():
    cfg = load_cfg()
    with FILE_LOCK:
        s = read_json(SETTINGS, {})
    room = s.setdefault("room", {})
    room.setdefault("name", cfg.get("room", {}).get("name") if isinstance(cfg.get("room"), dict) else None)
    if not room.get("name"):
        room["name"] = "Ruang Rapat A"
    room.setdefault("length_m", 6.0)
    room.setdefault("width_m", 5.0)
    room.setdefault("height_m", 3.0)
    room.setdefault("capacity", 8)
    room.setdefault("target_c", float(cfg.get("control", {}).get("t_target_c", 25)))
    s.setdefault("pin", str(cfg.get("panel", {}).get("technician_pin", "0000")))
    s.setdefault("active_profile", None)
    s.setdefault("meeting_default_min", 60)
    s.setdefault("automation", {"start_from_schedule": True, "on_at_start": False, "off_at_end": True})
    s.setdefault("schedule", DEFAULT_SCHEDULE)
    s.setdefault("booking_url", "https://booking.example/r/" + slug(room["name"]))
    return s


def save_settings(s):
    with FILE_LOCK:
        write_json(SETTINGS, s)


def sizing(room):
    area = round(room["length_m"] * room["width_m"], 1)
    vol = round(area * room["height_m"], 1)
    # aturan praktis: 500 BTU/h per m² untuk plafon 3 m, + 600 BTU/h per orang di atas 2 orang
    btu = area * 500 * max(1.0, room["height_m"] / 3.0) + max(0, int(room.get("capacity", 0)) - 2) * 600
    pk = "3 PK+"
    for limit, label in ((9000, "1 PK"), (12000, "1,5 PK"), (18000, "2 PK"), (24000, "2,5 PK")):
        if btu <= limit:
            pk = label
            break
    return {"area_m2": area, "volume_m3": vol, "btu_h": int(round(btu, -2)), "pk": pk}


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s[:40] or "profil"


# ----------------------------------------------------------- profil remote
def profile_path(pid):
    if not re.fullmatch(r"[a-z0-9-]{1,48}", pid or ""):
        raise HTTPException(400, "id profil tidak valid")
    return REMOTES / f"{pid}.json"


def list_profiles():
    out = []
    for p in sorted(REMOTES.glob("*.json")):
        if p.name.startswith("_"):
            continue
        d = read_json(p, None)
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    return out


def load_profile(pid):
    if not pid:
        return None
    try:
        return read_json(profile_path(pid), None)
    except HTTPException:
        return None


def save_profile(prof):
    prof["updated"] = int(time.time())
    write_json(profile_path(prof["id"]), prof)


def new_profile(name, brand=None):
    base = slug(name)
    pid, n = base, 2
    while (REMOTES / f"{pid}.json").exists():
        pid, n = f"{base}-{n}", n + 1
    prof = {"id": pid, "name": name.strip()[:60] or pid, "brand": brand or "",
            "protocol": "ir_learned", "mode": "cool", "fan": "auto",
            "created": int(time.time()), "updated": int(time.time()), "codes": {}}
    save_profile(prof)
    return prof


def active_profile():
    return load_profile(load_settings().get("active_profile"))


def active_codes():
    p = active_profile()
    return dict(p.get("codes", {})) if p else {}


def migrate_legacy():
    """v0.2 menyimpan kode di codes.json -> jadikan profil pertama yang aktif."""
    REMOTES.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    profs = list_profiles()
    if not profs and LEGACY_CODES.exists():
        codes = read_json(LEGACY_CODES, {})
        prof = new_profile("Remote awal (v0.2)")
        prof["codes"] = codes
        save_profile(prof)
        os.replace(LEGACY_CODES, DATA / "codes.v02.bak.json")
        s["active_profile"] = prof["id"]
        save_settings(s)
        print(f"[migrasi] {len(codes)} kode codes.json -> profil {prof['id']}", flush=True)
    elif profs and not load_profile(s.get("active_profile")):
        s["active_profile"] = profs[0]["id"]
        save_settings(s)


def profile_summary(prof, acc, active_id=None):
    codes = prof.get("codes", {})
    want = wanted_keys(acc)
    return {"id": prof["id"], "name": prof.get("name"), "brand": prof.get("brand", ""),
            "protocol": prof.get("protocol", "ir_learned"),
            "count": sum(1 for k in want if k in codes), "total": len(want),
            "learned": [k for k in want if k in codes],
            "missing": [k for k in want if k not in codes],
            "updated": prof.get("updated"), "active": prof["id"] == active_id}


# ----------------------------------------------------------- koneksi SMATRUL
class Hub:
    """Satu objek tinytuya untuk baca status + kirim/learn IR, dipakai bergantian."""

    def __init__(self):
        self.lock = threading.RLock()
        self.dev = None
        self.sig = None

    def _ensure(self, sm):
        from tinytuya.Contrib import IRRemoteControlDevice
        sig = (sm["device_id"], sm["ip"], sm["local_key"], str(sm.get("version", "3.3")))
        if self.dev is None or sig != self.sig:
            d = IRRemoteControlDevice(sm["device_id"], sm["ip"], sm["local_key"],
                                      version=float(sm.get("version", 3.3)), control_type=1)
            d.set_socketTimeout(5)
            d.set_socketRetryLimit(2)
            self.dev, self.sig = d, sig
        return self.dev

    def _call(self, sm, fn):
        with self.lock:
            try:
                return fn(self._ensure(sm))
            except Exception:
                self.dev = None
                raise

    def status(self, sm):
        return self._call(sm, lambda d: d.status())

    def send(self, sm, code):
        return self._call(sm, lambda d: d.send_button(code))

    def learn(self, sm, timeout):
        return self._call(sm, lambda d: d.receive_button(timeout=timeout))


HUB = Hub()


# ------------------------------------------------------------ log perintah
def db():
    con = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS command_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, source TEXT, action TEXT, code_key TEXT,
        power INTEGER, setpoint INTEGER, result TEXT, detail TEXT,
        latency_ms INTEGER, room_temp_c REAL)""")
    return con


def log_cmd(**row):
    row.setdefault("ts", time.time())
    row.setdefault("room_temp_c", ROOM.get("temp_c"))
    cols = ["ts", "source", "action", "code_key", "power", "setpoint", "result",
            "detail", "latency_ms", "room_temp_c"]
    with DB_LOCK:
        con = db()
        cur = con.execute(f"INSERT INTO command_log({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [row.get(c) for c in cols])
        con.commit()
        row["id"] = cur.lastrowid
        con.close()
    print(f"[cmd] {time.strftime('%H:%M:%S')} {row.get('source')} {row.get('action')} "
          f"key={row.get('code_key')} -> {row.get('result')} {row.get('detail') or ''}", flush=True)
    return row


KIND_SQL = {
    "panel": "source LIKE 'panel%'",
    "ai": "source LIKE 'ai%'",
    "auto": "source LIKE 'auto%'",
    "learn": "action IN ('learn','test','profile','save_profile')",
    "fail": "result IN ('gagal','ditolak')",
}


def recent_cmds(limit=20, kind=None):
    where = KIND_SQL.get(kind or "", "1=1")
    with DB_LOCK:
        con = db()
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM command_log WHERE {where} ORDER BY id DESC LIMIT ?", (limit,))]
        con.close()
    return rows


def load_state(acc):
    st = read_json(STATE, {})
    st.setdefault("power", False)
    st.setdefault("setpoint", acc["sp_default"])
    st.setdefault("last_power_change", 0)
    st.setdefault("source", "init")
    st.setdefault("ts", 0)
    return st


# ------------------------------------------------------------ eksekusi perintah
def nearest_code(codes, sp, acc):
    cands = sorted((int(k.split("_")[1]) for k in codes if k.startswith("cool_")
                    and k.split("_")[1].isdigit()), key=lambda x: (abs(x - sp), x))
    cands = [c for c in cands if acc["sp_min"] <= c <= acc["sp_max"]]
    return cands[0] if cands else None


def power_wait(st, acc):
    if not st["last_power_change"]:
        return 0
    return max(0, int(acc["min_power_interval_s"] - (time.time() - st["last_power_change"])))


def execute(action, source="panel", setpoint=None, prev_sp=None, extra=None):
    """Kirim perintah ke AC lewat SMATRUL dan catat hasilnya. Dipanggil dari thread."""
    with EXEC_LOCK:
        cfg = load_cfg()
        sm = cfg.get("smatrul", {})
        acc = ac_cfg(cfg)
        st = load_state(acc)
        prof = active_profile()
        codes = prof.get("codes", {}) if prof else {}
        now = time.time()

        if action == "power_on":
            power, sp = True, st["setpoint"] if setpoint is None else int(setpoint)
        elif action == "power_off":
            power, sp = False, st["setpoint"]
        elif action == "set_temp":
            power, sp = True, int(setpoint)
        else:
            raise ValueError(action)
        sp = max(acc["sp_min"], min(acc["sp_max"], sp))

        base = dict(source=source, action=action, power=int(power), setpoint=sp)
        details = []
        if prev_sp is not None and prev_sp != sp:
            details.append(f"{prev_sp} -> {sp} °C")
        if extra:
            details.append(extra)

        def done(**kw):
            d = details + ([kw.pop("detail")] if kw.get("detail") else [])
            return log_cmd(**base, **kw, detail=" · ".join(d) or None), st

        changes_power = power != st["power"]
        wait = power_wait(st, acc)
        if changes_power and wait > 0:
            return done(result="ditolak", detail=f"proteksi kompresor: tunggu {wait} s")
        if not smatrul_ready(sm):
            return done(result="gagal", detail="SMATRUL belum dikonfigurasi")
        if not prof:
            return done(result="gagal", detail="belum ada profil remote aktif (Pengaturan > Remote AC)")

        if power:
            key = f"cool_{sp}"
            if key not in codes:
                alt = nearest_code(codes, sp, acc)
                if alt is None:
                    return done(code_key=key, result="gagal", detail=f"kode {key} belum direkam di profil {prof['name']}")
                details.append(f"kode {key} belum ada, pakai cool_{alt}")
                sp, key = alt, f"cool_{alt}"
                base["setpoint"] = sp
        else:
            key = "off"
            if key not in codes:
                return done(code_key=key, result="gagal", detail=f"kode off belum direkam di profil {prof['name']}")

        t0 = time.time()
        try:
            HUB.send(sm, codes[key])
        except Exception as e:  # noqa: BLE001
            return done(code_key=key, result="gagal", detail=f"SMATRUL tidak menjawab: {str(e)[:100]}",
                        latency_ms=int((time.time() - t0) * 1000))
        lat = int((time.time() - t0) * 1000)

        if changes_power:
            st["last_power_change"] = now
        st.update(power=power, setpoint=sp, source=source, ts=now)
        write_json(STATE, st)
        return done(code_key=key, result="terkirim", latency_ms=lat)


# ---------------------------------------------------------------- rapat & jadwal
def hm_today(hm, ref=None):
    ref = ref or datetime.now()
    h, m = (int(x) for x in hm.split(":"))
    return ref.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def load_meeting():
    m = read_json(MEETING, {})
    m.setdefault("active", False)
    return m


def save_meeting(m):
    with FILE_LOCK:
        write_json(MEETING, m)


def schedule_today(s, now=None):
    now = now or time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    out, next_marked = [], False
    for ev in sorted(s.get("schedule", []), key=lambda e: e.get("start", "")):
        try:
            a, b = hm_today(ev["start"]), hm_today(ev["end"])
        except Exception:
            continue
        state = "done" if now >= b else "now" if a <= now < b else "later"
        if state == "later" and not next_marked:
            state, next_marked = "next", True
        out.append(dict(ev, start_ts=a, end_ts=b, state=state, key=f"{today} {ev['start']}"))
    return out


def start_meeting(title, minutes=None, end_ts=None, source="panel", organizer="", people=None, key=None):
    s = load_settings()
    now = time.time()
    if end_ts is None:
        end_ts = now + 60 * int(minutes or s["meeting_default_min"])
    m = {"active": True, "title": title or "Rapat", "organizer": organizer, "people": people,
         "start_ts": now, "end_ts": end_ts, "source": source, "schedule_key": key,
         "skip_key": load_meeting().get("skip_key")}
    save_meeting(m)
    log_cmd(source=source, action="meeting_start", result="ok",
            detail=f"{m['title']} · selesai {datetime.fromtimestamp(end_ts).strftime('%H:%M')}")
    if s["automation"].get("on_at_start"):
        acc = ac_cfg(load_cfg())
        st = load_state(acc)
        if not st["power"]:
            execute("power_on", "auto:rapat dimulai", setpoint=int(round(s["room"]["target_c"])))
    return m


def end_meeting(source="panel"):
    m = load_meeting()
    if not m.get("active"):
        return m
    s = load_settings()
    m.update(active=False, ended_ts=time.time(), skip_key=m.get("schedule_key") or m.get("skip_key"))
    m["pending_off"] = bool(s["automation"].get("off_at_end"))
    save_meeting(m)
    log_cmd(source=source, action="meeting_end", result="ok", detail=m.get("title"))
    return m


def control_tick():
    s = load_settings()
    m = load_meeting()
    now = time.time()
    if m.get("active") and now >= m.get("end_ts", now + 1):
        m = end_meeting("auto:rapat selesai")
    if not m.get("active") and s["automation"].get("start_from_schedule"):
        for ev in schedule_today(s, now):
            if ev["state"] == "now" and ev["key"] not in (m.get("skip_key"), m.get("schedule_key")):
                m = start_meeting(ev["title"], end_ts=ev["end_ts"], source="auto:jadwal",
                                  organizer=ev.get("organizer", ""), people=ev.get("people"), key=ev["key"])
                break
    if m.get("pending_off") and not m.get("active"):
        acc = ac_cfg(load_cfg())
        st = load_state(acc)
        manual_after = st.get("ts", 0) > m.get("ended_ts", 0) and str(st.get("source", "")).startswith("panel")
        if not st["power"] or manual_after:
            m["pending_off"] = False
            save_meeting(m)
        elif power_wait(st, acc) == 0:
            row, _ = execute("power_off", "auto:rapat selesai")
            if row["result"] == "terkirim" or row["result"] == "gagal":
                m["pending_off"] = False      # gagal (mis. kode off belum ada) tidak diulang terus
                save_meeting(m)


def control_loop():
    while True:
        try:
            control_tick()
        except Exception as e:  # noqa: BLE001
            print(f"[control] {e}", flush=True)
        time.sleep(10)


# ---------------------------------------------------------------- rekomendasi
def recommend(s=None, m=None, st=None, acc=None):
    s = s or load_settings()
    m = m or load_meeting()
    acc = acc or ac_cfg(load_cfg())
    st = st or load_state(acc)
    t, rh = room_now()
    if t is None:
        return None
    target = float(s["room"]["target_c"])
    sp = int(st["setpoint"])
    settled = time.time() - st.get("ts", 0) > SETTLE_S
    rec = None
    if not m.get("active"):
        if st["power"] and not m.get("pending_off"):
            rec = dict(rule="R7", title="Matikan AC", action="power_off",
                       reason=f"Tidak ada rapat berjalan, AC masih menyala di {sp} °C. Matikan untuk hemat energi.")
    else:
        people = m.get("people")
        who = f" dan dipakai {people} orang" if people else ""
        if not st["power"] and t > target + 0.5:
            new = int(round(target))
            rec = dict(rule="R1", title=f"Nyalakan AC ke {new} °C", action="set_temp", setpoint=new,
                       reason=f"Rapat berjalan{who}, suhu {fmt(t)} °C di atas target {fmt(target)} °C.")
        elif st["power"] and settled and t >= target + 1.5 and sp > acc["sp_min"]:
            rec = dict(rule="R4", title=f"Turunkan setpoint ke {sp - 1} °C", action="set_temp", setpoint=sp - 1,
                       reason=f"Suhu {fmt(t)} °C masih di atas batas nyaman {fmt(target + 1)} °C{who}, "
                              f"padahal setpoint {sp} °C sudah berjalan lebih dari 10 menit.")
        elif st["power"] and settled and rh is not None and rh >= 70 and t > target and sp > acc["sp_min"]:
            rec = dict(rule="R6", title=f"Turunkan setpoint ke {sp - 1} °C", action="set_temp", setpoint=sp - 1,
                       reason=f"Kelembapan {fmt(rh, 0)} % tinggi; menurunkan setpoint membantu AC membuang uap air.")
        elif st["power"] and settled and t <= target - 1.0 and sp < acc["sp_max"]:
            rec = dict(rule="R5", title=f"Naikkan setpoint ke {sp + 1} °C", action="set_temp", setpoint=sp + 1,
                       reason=f"Suhu {fmt(t)} °C sudah di bawah target {fmt(target)} °C. Naikkan 1 °C untuk hemat energi.")
    if rec and time.time() - DISMISSED.get(rec["rule"], 0) < DISMISS_S:
        return None
    return rec


def fmt(x, nd=1):
    return f"{x:.{nd}f}".replace(".", ",")


# ---------------------------------------------------------------- polling sensor
def room_now():
    age = time.time() - ROOM["ts"] if ROOM["ts"] else None
    if age is None or age > 120:
        return None, None
    return ROOM["temp_c"], ROOM["rh_pct"]


HISTORY = []                 # (ts, temp) 30 menit terakhir untuk grafik panel


def smatrul_loop():
    while True:
        cfg = load_cfg()
        sm = cfg.get("smatrul", {})
        poll = max(5, int(sm.get("poll_s", 10) or 10))
        if not (smatrul_ready(sm) and sm.get("dps_temp")):
            ROOM["error"] = "SMATRUL belum dikonfigurasi (jalankan smatrul_onboard.py)"
            time.sleep(15)
            continue
        if LEARNING["key"] and time.time() < (LEARNING["until"] or 0):
            time.sleep(2)        # jangan ganggu SMATRUL saat merekam
            continue
        try:
            st = HUB.status(sm)
            dps = st.get("dps") if isinstance(st, dict) else None
            if not dps:
                raise RuntimeError(st)
            t_raw = dps.get(str(sm["dps_temp"]))
            h_raw = dps.get(str(sm.get("dps_humidity"))) if sm.get("dps_humidity") else None
            if isinstance(t_raw, (int, float)) and not isinstance(t_raw, bool):
                ROOM["temp_c"] = round(t_raw / float(sm.get("temp_scale", 10) or 10)
                                       + float(sm.get("temp_offset_c", 0) or 0), 1)
            if isinstance(h_raw, (int, float)) and not isinstance(h_raw, bool):
                ROOM["rh_pct"] = round(h_raw / float(sm.get("rh_scale", 1) or 1)
                                       + float(sm.get("rh_offset_pct", 0) or 0), 1)
            ROOM.update(ts=time.time(), error=None)
            if ROOM["temp_c"] is not None:
                HISTORY.append((int(time.time()), ROOM["temp_c"]))
                cut = time.time() - 1800
                while HISTORY and HISTORY[0][0] < cut:
                    HISTORY.pop(0)
        except Exception as e:  # noqa: BLE001
            ROOM["error"] = f"gagal baca SMATRUL: {str(e)[:120]}"
        time.sleep(poll)


@app.on_event("startup")
def _startup():
    DATA.mkdir(parents=True, exist_ok=True)
    db().close()
    migrate_legacy()
    if os.environ.get("HVAC_NO_POLL") != "1":
        threading.Thread(target=smatrul_loop, daemon=True).start()
        threading.Thread(target=control_loop, daemon=True).start()


# --------------------------------------------------------------------- helpers
def _read(p):
    try:
        return Path(p).read_text().strip()
    except Exception:
        return None


def heat_index(t, rh):
    if t is None or rh is None:
        return None
    f = t * 9 / 5 + 32
    hi = 0.5 * (f + 61 + (f - 68) * 1.2 + rh * 0.094)
    if hi >= 80:
        hi = (-42.379 + 2.04901523 * f + 10.14333127 * rh - 0.22475541 * f * rh
              - 0.00683783 * f * f - 0.05481717 * rh * rh + 0.00122874 * f * f * rh
              + 0.00085282 * f * rh * rh - 0.00000199 * f * f * rh * rh)
    return round((hi - 32) * 5 / 9, 1)


def sysinfo():
    cpu = _read("/sys/class/thermal/thermal_zone0/temp")
    mem = None
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemAvailable:"):
            mem = int(line.split()[1]) // 1024
    try:
        thr = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout.strip().split("=")[-1]
    except Exception:
        thr = None
    return {
        "cpu_temp_c": round(int(cpu) / 1000, 1) if cpu else None,
        "throttled": thr,
        "mem_available_mb": mem,
        "uptime_s": int(float((_read("/proc/uptime") or "0 0").split()[0])),
    }


# --------------------------------------------------------------------- API: status
PENDING = {"sp": None, "from": None, "task": None}


@app.get("/api/status")
def status():
    cfg = load_cfg()
    acc = ac_cfg(cfg)
    st = load_state(acc)
    s = load_settings()
    m = load_meeting()
    prof = load_profile(s.get("active_profile"))
    t, h = room_now()
    now = time.time()
    meeting = None
    if m.get("active"):
        meeting = {"title": m["title"], "organizer": m.get("organizer"), "people": m.get("people"),
                   "start_ts": m["start_ts"], "end_ts": m["end_ts"], "source": m.get("source"),
                   "elapsed_s": int(now - m["start_ts"]), "remaining_s": max(0, int(m["end_ts"] - now))}
    draft = read_json(DRAFT, None)
    out = {
        "version": VERSION,
        "ts": int(now * 1000),
        "room": {"name": s["room"]["name"], "target_c": s["room"]["target_c"]},
        "room_temp_c": t, "room_rh_pct": h, "feels_like_c": heat_index(t, h),
        "smatrul_age_s": int(now - ROOM["ts"]) if ROOM["ts"] else None, "smatrul_error": ROOM["error"],
        "history": HISTORY[::max(1, len(HISTORY) // 60)],
        "ac": {
            "power": st["power"], "setpoint": st["setpoint"], "pending_setpoint": PENDING["sp"],
            "sp_min": acc["sp_min"], "sp_max": acc["sp_max"],
            "power_wait_s": power_wait(st, acc),
        },
        "profile": profile_summary(prof, acc, prof["id"]) if prof else None,
        "draft": {"profile_id": draft.get("profile_id"), "keys": sorted(draft.get("codes", {}))} if draft else None,
        "meeting": meeting,
        "schedule": schedule_today(s, now),
        "recommendation": recommend(s, m, st, acc),
        "booking_url": s["booking_url"],
        "commands": recent_cmds(8),
        "learning": LEARNING["key"],
    }
    out.update(sysinfo())
    return JSONResponse(out)


# --------------------------------------------------------------------- API: AC
class AcCmd(BaseModel):
    action: str
    source: str = "panel"


async def _debounced_temp(source):
    await asyncio.sleep(DEBOUNCE_S)
    sp, frm = PENDING["sp"], PENDING["from"]
    PENDING.update(sp=None, **{"from": None}, task=None)
    if sp is not None:
        await asyncio.to_thread(execute, "set_temp", source, sp, frm)


@app.post("/api/ac")
async def ac(cmd: AcCmd):
    acc = ac_cfg(load_cfg())
    st = load_state(acc)
    if cmd.action in ("power_on", "power_off"):
        if PENDING["task"]:
            PENDING["task"].cancel()
            PENDING.update(sp=None, **{"from": None}, task=None)
        row, st = await asyncio.to_thread(execute, cmd.action, cmd.source)
        return {"ok": row["result"] == "terkirim", "entry": row, "state": st}
    if cmd.action in ("temp_up", "temp_down"):
        step = 1 if cmd.action == "temp_up" else -1
        base = PENDING["sp"] if PENDING["sp"] is not None else st["setpoint"]
        new = max(acc["sp_min"], min(acc["sp_max"], base + step))
        if not st["power"]:
            st["setpoint"] = new
            write_json(STATE, st)
            return {"ok": True, "entry": None, "state": st,
                    "message": f"Setpoint {new} °C disimpan; berlaku saat AC dinyalakan"}
        if PENDING["from"] is None:
            PENDING["from"] = st["setpoint"]
        PENDING["sp"] = new
        if PENDING["task"]:
            PENDING["task"].cancel()
        PENDING["task"] = asyncio.create_task(_debounced_temp(cmd.source))
        return {"ok": True, "entry": None, "pending_setpoint": new, "state": st}
    raise HTTPException(400, "action harus power_on, power_off, temp_up, atau temp_down")


class RecCmd(BaseModel):
    rule: str


@app.post("/api/recommendation/apply")
def rec_apply(cmd: RecCmd):
    rec = recommend()
    if not rec or rec["rule"] != cmd.rule:
        raise HTTPException(409, "rekomendasi sudah tidak berlaku")
    acc = ac_cfg(load_cfg())
    prev = load_state(acc)["setpoint"]
    row, st = execute(rec["action"], f"ai:{rec['rule']}", setpoint=rec.get("setpoint"),
                      prev_sp=prev if rec["action"] == "set_temp" else None, extra="rekomendasi diterapkan")
    DISMISSED[rec["rule"]] = time.time() if row["result"] != "terkirim" else 0
    return {"ok": row["result"] == "terkirim", "entry": row, "state": st}


@app.post("/api/recommendation/dismiss")
def rec_dismiss(cmd: RecCmd):
    DISMISSED[cmd.rule] = time.time()
    log_cmd(source=f"ai:{cmd.rule}", action="dismiss", result="diabaikan", detail="rekomendasi diabaikan")
    return {"ok": True}


# --------------------------------------------------------------------- API: rapat
class MeetingCmd(BaseModel):
    minutes: int | None = None
    title: str | None = None


@app.post("/api/meeting/start")
def meeting_start(cmd: MeetingCmd):
    s = load_settings()
    title, organizer, people, end_ts, key = cmd.title, "", None, None, None
    if not title:
        nxt = next((e for e in schedule_today(s) if e["state"] in ("now", "next")), None)
        if nxt and nxt["state"] == "now":
            title, organizer, people, end_ts, key = nxt["title"], nxt.get("organizer", ""), nxt.get("people"), nxt["end_ts"], nxt["key"]
        else:
            title = "Rapat tanpa jadwal"
    m = start_meeting(title, minutes=cmd.minutes, end_ts=None if cmd.minutes else end_ts,
                      source="panel", organizer=organizer, people=people, key=key)
    return {"ok": True, "meeting": m}


@app.post("/api/meeting/end")
def meeting_end():
    return {"ok": True, "meeting": end_meeting("panel")}


@app.post("/api/meeting/extend")
def meeting_extend(cmd: MeetingCmd):
    m = load_meeting()
    if not m.get("active"):
        raise HTTPException(409, "tidak ada rapat berjalan")
    m["end_ts"] += 60 * int(cmd.minutes or 15)
    save_meeting(m)
    log_cmd(source="panel", action="meeting_extend", result="ok",
            detail=f"selesai {datetime.fromtimestamp(m['end_ts']).strftime('%H:%M')}")
    return {"ok": True, "meeting": m}


# --------------------------------------------------------------------- API: log
@app.get("/api/commands")
def commands(limit: int = 50, kind: str | None = None):
    return recent_cmds(max(1, min(limit, 1000)), kind)


# --------------------------------------------------------------------- API: pengaturan
@app.get("/api/settings")
def settings_get():
    s = load_settings()
    return {"room": s["room"], "sizing": sizing(s["room"]), "automation": s["automation"],
            "booking_url": s["booking_url"], "meeting_default_min": s["meeting_default_min"],
            "schedule": s["schedule"], "pin_default": s["pin"] in ("0000", "1234")}


class SettingsCmd(BaseModel):
    room: dict | None = None
    automation: dict | None = None
    booking_url: str | None = None
    meeting_default_min: int | None = None
    schedule: list | None = None


@app.post("/api/settings")
def settings_set(cmd: SettingsCmd):
    s = load_settings()
    if cmd.room:
        r = s["room"]
        if "name" in cmd.room and str(cmd.room["name"]).strip():
            r["name"] = str(cmd.room["name"]).strip()[:40]
        for k, lo, hi in (("length_m", 1, 50), ("width_m", 1, 50), ("height_m", 2, 10)):
            if k in cmd.room:
                r[k] = round(max(lo, min(hi, float(cmd.room[k]))), 1)
        if "capacity" in cmd.room:
            r["capacity"] = max(1, min(100, int(cmd.room["capacity"])))
        if "target_c" in cmd.room:
            r["target_c"] = max(18.0, min(30.0, float(cmd.room["target_c"])))
    if cmd.automation:
        s["automation"].update({k: bool(v) for k, v in cmd.automation.items()
                                if k in ("start_from_schedule", "on_at_start", "off_at_end")})
    if cmd.booking_url:
        s["booking_url"] = cmd.booking_url[:200]
    if cmd.meeting_default_min:
        s["meeting_default_min"] = max(15, min(240, cmd.meeting_default_min))
    if cmd.schedule is not None:
        s["schedule"] = cmd.schedule
    save_settings(s)
    log_cmd(source="settings", action="settings", result="ok", detail=s["room"]["name"])
    return settings_get()


class PinCmd(BaseModel):
    pin: str
    new_pin: str | None = None


@app.post("/api/pin")
def pin(cmd: PinCmd):
    s = load_settings()
    if cmd.pin != s["pin"]:
        raise HTTPException(403, "PIN salah")
    if cmd.new_pin:
        if not re.fullmatch(r"\d{4,8}", cmd.new_pin):
            raise HTTPException(400, "PIN baru harus 4-8 angka")
        s["pin"] = cmd.new_pin
        save_settings(s)
        log_cmd(source="settings", action="pin", result="ok", detail="PIN diganti")
    return {"ok": True}


# --------------------------------------------------------------------- API: profil remote
@app.get("/api/profiles")
def profiles():
    acc = ac_cfg(load_cfg())
    act = load_settings().get("active_profile")
    return {"active": act, "wanted": wanted_keys(acc), "quick": QUICK_KEYS,
            "profiles": [profile_summary(p, acc, act) for p in list_profiles()]}


class ProfileCmd(BaseModel):
    name: str
    brand: str | None = None


@app.post("/api/profiles")
def profile_create(cmd: ProfileCmd):
    prof = new_profile(cmd.name, cmd.brand)
    log_cmd(source="settings", action="profile", result="ok", detail=f"profil baru {prof['name']}")
    return profile_summary(prof, ac_cfg(load_cfg()))


@app.post("/api/profiles/{pid}/activate")
def profile_activate(pid: str):
    prof = load_profile(pid)
    if not prof:
        raise HTTPException(404, "profil tidak ada")
    s = load_settings()
    s["active_profile"] = pid
    save_settings(s)
    log_cmd(source="settings", action="profile", result="ok", detail=f"ganti profil ke {prof['name']}")
    return {"ok": True}


@app.delete("/api/profiles/{pid}")
def profile_delete(pid: str):
    if load_settings().get("active_profile") == pid:
        raise HTTPException(409, "profil aktif tidak bisa dihapus")
    p = profile_path(pid)
    if not p.exists():
        raise HTTPException(404, "profil tidak ada")
    EXPORTS.mkdir(parents=True, exist_ok=True)
    os.replace(p, EXPORTS / f"deleted-{pid}-{int(time.time())}.json")
    log_cmd(source="settings", action="profile", result="ok", detail=f"hapus profil {pid}")
    return {"ok": True}


@app.get("/api/profiles/{pid}/export")
def profile_export(pid: str):
    prof = load_profile(pid)
    if not prof:
        raise HTTPException(404, "profil tidak ada")
    body = json.dumps(dict(prof, exported=int(time.time()), format="smart-hvac-remote/1"), indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="remote-{pid}.json"'})


@app.post("/api/profiles/{pid}/export")
def profile_export_local(pid: str):
    prof = load_profile(pid)
    if not prof:
        raise HTTPException(404, "profil tidak ada")
    EXPORTS.mkdir(parents=True, exist_ok=True)
    path = EXPORTS / f"remote-{pid}-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    write_json(path, dict(prof, exported=int(time.time()), format="smart-hvac-remote/1"))
    log_cmd(source="settings", action="profile", result="ok", detail=f"ekspor {path.name}")
    return {"ok": True, "path": str(path), "url": f"/api/profiles/{pid}/export"}


class ImportCmd(BaseModel):
    profile: dict


@app.post("/api/profiles/import")
def profile_import(cmd: ImportCmd):
    src = cmd.profile
    if not isinstance(src.get("codes"), dict):
        raise HTTPException(400, "berkas bukan dataset remote")
    prof = new_profile(src.get("name", "Impor"), src.get("brand"))
    prof["codes"] = {k: v for k, v in src["codes"].items() if isinstance(v, str)}
    save_profile(prof)
    log_cmd(source="settings", action="profile", result="ok", detail=f"impor {prof['name']}")
    return profile_summary(prof, ac_cfg(load_cfg()))


# --------------------------------------------------------------------- API: rekam (draf)
class DraftStart(BaseModel):
    profile_id: str | None = None      # None = profil baru
    mode: str = "full"                 # full | quick | single


@app.post("/api/draft/start")
def draft_start(cmd: DraftStart):
    if cmd.profile_id and not load_profile(cmd.profile_id):
        raise HTTPException(404, "profil tidak ada")
    d = {"profile_id": cmd.profile_id, "mode": cmd.mode, "started": int(time.time()), "codes": {}}
    write_json(DRAFT, d)
    return {"ok": True, "draft": {"profile_id": cmd.profile_id, "mode": cmd.mode, "keys": []}}


@app.get("/api/draft")
def draft_get():
    d = read_json(DRAFT, None)
    if not d:
        return {"draft": None}
    return {"draft": {"profile_id": d.get("profile_id"), "mode": d.get("mode"), "keys": sorted(d.get("codes", {}))}}


class DraftSave(BaseModel):
    name: str | None = None
    brand: str | None = None
    activate: bool = True


@app.post("/api/draft/save")
def draft_save(cmd: DraftSave):
    d = read_json(DRAFT, None)
    if not d or not d.get("codes"):
        raise HTTPException(409, "belum ada kode yang direkam")
    prof = load_profile(d.get("profile_id")) if d.get("profile_id") else None
    if not prof:
        prof = new_profile(cmd.name or "Profil baru", cmd.brand)
    if cmd.name:
        prof["name"] = cmd.name.strip()[:60]
    if cmd.brand is not None:
        prof["brand"] = cmd.brand
    prof.setdefault("codes", {}).update(d["codes"])
    save_profile(prof)
    DRAFT.unlink(missing_ok=True)
    if cmd.activate:
        s = load_settings()
        s["active_profile"] = prof["id"]
        save_settings(s)
    acc = ac_cfg(load_cfg())
    summ = profile_summary(prof, acc, prof["id"] if cmd.activate else None)
    log_cmd(source="settings", action="save_profile", code_key=f"{summ['count']}/{summ['total']}",
            result="tersimpan", detail=f"{prof['name']} · {len(d['codes'])} kode baru")
    return {"ok": True, "profile": summ}


@app.post("/api/draft/discard")
def draft_discard():
    d = read_json(DRAFT, None)
    DRAFT.unlink(missing_ok=True)
    if d and d.get("codes"):
        log_cmd(source="settings", action="learn", result="dibuang", detail=f"{len(d['codes'])} kode draf dibuang")
    return {"ok": True}


def valid_key(key):
    return key == "off" or (key.startswith("cool_") and key[5:].isdigit())


@app.post("/api/learn/{key}")
def learn(key: str, timeout: int = 15, source: str = "learn"):
    """Rekam satu tombol remote ke DRAF. Draf dibuat otomatis (untuk profil aktif) bila belum ada."""
    if not valid_key(key):
        raise HTTPException(400, "key harus 'off' atau 'cool_<suhu>'")
    sm = load_cfg().get("smatrul", {})
    if not smatrul_ready(sm):
        raise HTTPException(409, "SMATRUL belum dikonfigurasi")
    timeout = max(5, min(timeout, 60))
    with EXEC_LOCK:
        d = read_json(DRAFT, None) or {"profile_id": load_settings().get("active_profile"),
                                        "mode": "single", "started": int(time.time()), "codes": {}}
        LEARNING.update(key=key, until=time.time() + timeout + 5)
        t0 = time.time()
        try:
            code = HUB.learn(sm, timeout)
        except Exception as e:  # noqa: BLE001
            log_cmd(source=source, action="learn", code_key=key, result="gagal", detail=str(e)[:100])
            raise HTTPException(502, f"gagal merekam: {e}")
        finally:
            LEARNING.update(key=None, until=None)
        if not code:
            log_cmd(source=source, action="learn", code_key=key, result="gagal", detail="tidak ada sinyal")
            raise HTTPException(408, "tidak ada sinyal remote tertangkap")
        d.setdefault("codes", {})[key] = code
        write_json(DRAFT, d)
        log_cmd(source=source, action="learn", code_key=key, result="direkam",
                detail=f"draf · {len(code)} karakter", latency_ms=int((time.time() - t0) * 1000))
    return {"ok": True, "key": key, "length": len(code), "draft_keys": sorted(d["codes"])}


@app.post("/api/test/{key}")
def test_code(key: str, source: str = "settings"):
    """Kirim kode dari draf (bila ada) atau dari profil aktif, untuk diamati di AC."""
    sm = load_cfg().get("smatrul", {})
    d = read_json(DRAFT, None) or {}
    code, where = d.get("codes", {}).get(key), "draf"
    if not code:
        code, where = active_codes().get(key), "profil"
    if not code:
        raise HTTPException(404, f"kode {key} belum direkam")
    with EXEC_LOCK:
        t0 = time.time()
        try:
            HUB.send(sm, code)
        except Exception as e:  # noqa: BLE001
            log_cmd(source=source, action="test", code_key=key, result="gagal", detail=str(e)[:100])
            raise HTTPException(502, f"gagal kirim: {e}")
        log_cmd(source=source, action="test", code_key=key, result="terkirim", detail=f"uji kode dari {where}",
                latency_ms=int((time.time() - t0) * 1000))
    return {"ok": True, "from": where}


@app.get("/api/codes")
def codes_list():
    """Kompatibel dengan learn_codes.py v0.2: kode di profil aktif + draf."""
    acc = ac_cfg(load_cfg())
    codes = active_codes()
    codes.update((read_json(DRAFT, None) or {}).get("codes", {}))
    want = wanted_keys(acc)
    return {"learned": sorted(codes), "missing": [k for k in want if k not in codes], "wanted": want}


# --------------------------------------------------------------------- QR & halaman
@app.get("/api/qr.svg")
def qr_svg():
    try:
        import segno
    except ImportError:
        raise HTTPException(503, "paket segno belum terpasang")
    url = load_settings()["booking_url"]
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=4, border=1, dark="#0F1B24", light="#FFFFFF",
                                    xmldecl=False, svgns=True)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "since_s": int(time.time() - BOOT_TS)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
