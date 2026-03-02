#!/usr/bin/env python3
"""
IETT 59RK Bus Map Tracker
Serves a Leaflet.js map + live terminal table.
Refreshes every 1 minute. Logs all events to logs/YYYY-MM-DD.log.
"""

import json
import math
import os
import sys
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests
import xml.etree.ElementTree as ET
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import box

console = Console()

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  Google Maps API Anahtarı — buraya girin                                   │
# │  Distance Matrix API etkin olmalı.                                          │
# │  Alternatif: terminal'de  export GMAPS_KEY="anahtarınız"                   │
# └─────────────────────────────────────────────────────────────────────────────┘
GMAPS_API_KEY = "KEY"                                     # ← buraya yapıştırın
GMAPS_API_KEY = os.environ.get("GMAPS_KEY", GMAPS_API_KEY)   # env var override

# ── Sabit hedef: 4. Levent ────────────────────────────────────────────────────

DEST_LAT  = 41.0845294
DEST_LON  = 29.0072518
DEST_NAME = "4. Levent"

# ── IETT API ──────────────────────────────────────────────────────────────────

SOAP_URL    = "https://api.ibb.gov.tr/iett/FiloDurum/SeferGerceklesme.asmx"
SOAP_ACTION = "http://tempuri.org/GetHatOtoKonum_json"
SOAP_BODY   = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetHatOtoKonum_json xmlns="http://tempuri.org/">
      <HatKodu>{hat_no}</HatKodu>
    </GetHatOtoKonum_json>
  </soap:Body>
</soap:Envelope>"""

DIRECTION_FILTERS = {"59RK": "_D_", "59RS": "_G_"}  # per-line direction codes
HAT_NOS           = ["59RK", "59RS"]   # lines to track
REFRESH_SECS     = 120      # 2 minutes — 30*2 = 60 req/hour, within 100 req/hour limit

# ── Google Maps ───────────────────────────────────────────────────────────────

GMAPS_DM_URL        = "https://maps.googleapis.com/maps/api/distancematrix/json"
ETA_ADJUST_SECS     = 240  # subtract 4 minutes from all ETAs
ETA_DIST_SECS_PER_KM = 50   # add 50 secs per km of remaining distance

# ── Ghost bus filter ──────────────────────────────────────────────────────────

HISTORY_SIZE = 6    # snapshots kept per bus
EXPIRE_AFTER = 3    # missed polls before expiry
MIN_POLLS    = 4    # polls needed to confirm a bus is real
MIN_MOVE_KM  = 0.02 # 20 m — minimum displacement to not be a ghost

# ── Passed-destination detection ──────────────────────────────────────────────
# A bus inside the bounding box below is marked as passed.
# Define the zone with two diagonal corner points; the code derives the rectangle.

PASSED_BOX = (
    min(41.07255239019356, 41.100338391079866),  # lat_min  (south edge)
    max(41.07255239019356, 41.100338391079866),  # lat_max  (north edge)
    min(29.058743864878764, 29.01610010256362),  # lon_min  (west edge)
    max(29.058743864878764, 29.01610010256362),  # lon_max  (east edge)
)
PASSED_COOLDOWN_SECS = 1800  # 30 minutes before a passed bus is forgotten

# ── Stale GPS threshold ───────────────────────────────────────────────────────

GPS_STALE_SECS = 300   # 5 minutes — GPS older than this is flagged

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR     = Path(__file__).parent / "logs"
RAW_LOG_DIR = LOG_DIR / "raw"
STATIC_DIR  = Path(__file__).parent / "static"


def log_event(event: str, **data):
    """Append one JSON line to today's event log file."""
    LOG_DIR.mkdir(exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    log_path = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # never crash the main loop due to logging


def log_raw_response(hat_no: str, buses: list[dict]):
    """
    Append one JSON line to today's raw API response log.
    Captures the full IETT response before any filtering, for debugging.
    File: logs/raw/YYYY-MM-DD.jsonl
    """
    RAW_LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":       datetime.now().isoformat(timespec="seconds"),
        "hat_no":   hat_no,
        "count":    len(buses),
        "buses":    buses,
    }
    log_path = RAW_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── IETT fetch ────────────────────────────────────────────────────────────────

def fetch_buses(hat_no: str) -> list[dict]:
    """Fetch bus positions. Retries once on failure. Deduplicates by kapino."""
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.post(
                SOAP_URL,
                data=SOAP_BODY.format(hat_no=hat_no).encode("utf-8"),
                headers=headers, timeout=10,
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ns   = {"soap": "http://schemas.xmlsoap.org/soap/envelope/",
                    "tp":   "http://tempuri.org/"}
            el   = root.find(".//tp:GetHatOtoKonum_jsonResult", ns)
            if el is None or not el.text:
                return []
            buses = json.loads(el.text)
            if not isinstance(buses, list):
                return []
            # Log the full raw response before any filtering
            log_raw_response(hat_no, buses)
            # Deduplicate: same kapino may appear twice — keep fresher GPS timestamp
            seen: dict = {}
            for b in buses:
                kap = b.get("kapino")
                if not kap:
                    continue
                if (kap not in seen or
                        b.get("son_konum_zamani", "") > seen[kap].get("son_konum_zamani", "")):
                    seen[kap] = b
            direction = DIRECTION_FILTERS.get(hat_no, "_D_")
            return [b for b in seen.values()
                    if direction in b.get("guzergahkodu", "")]
        except Exception as e:
            last_err = e
            if attempt == 0:
                log_event("api_retry", error=str(e))
                time.sleep(5)

    log_event("api_fail", error=str(last_err))
    console.print(f"[red]IETT API hatası:[/red] {last_err}")
    return []


# ── Geo helpers ───────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def movement_bearing(h: list[tuple]) -> float | None:
    """Bearing (0–360°) from second-to-last to last position in history."""
    if len(h) < 2:
        return None
    (lat1, lon1), (lat2, lon2) = h[-2], h[-1]
    dlat, dlon = lat2 - lat1, lon2 - lon1
    if abs(dlat) < 1e-7 and abs(dlon) < 1e-7:
        return None
    return math.degrees(math.atan2(dlon, dlat)) % 360


def gps_age_secs(ts_str: str) -> int | None:
    """Return seconds since last GPS fix, or None if unparseable."""
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return int((datetime.now() - dt).total_seconds())
    except Exception:
        return None


def fmt_seconds(secs: int) -> str:
    """Format seconds into a human-readable duration string."""
    secs = max(0, secs)
    if secs < 60:
        return "< 1 dk"
    m = secs // 60
    h, m = divmod(m, 60)
    return f"{h}s {m}dk" if h else f"{m} dk"


def eta_str_fallback(dist_km: float, speed_kmh: float) -> str:
    """Haversine-based ETA with fixed and distance-based adjustments, labeled [~]."""
    if speed_kmh < 1:
        speed_kmh = 20.0
    raw_secs = int((dist_km / speed_kmh) * 3600)
    adjusted = raw_secs - ETA_ADJUST_SECS + int(dist_km * ETA_DIST_SECS_PER_KM)
    return fmt_seconds(adjusted) + " [~]"


# ── Google Maps traffic ETA ───────────────────────────────────────────────────

def get_traffic_etas(buses: list[dict]) -> list[str]:
    """
    Batch Distance Matrix request. Subtracts ETA_ADJUST_SECS from each result.
    Falls back to haversine per bus on any failure.
    """
    if not GMAPS_API_KEY:
        return [_fallback_eta(b) for b in buses]

    origins = "|".join(f"{b.get('enlem')},{b.get('boylam')}" for b in buses)
    params  = {
        "origins":        origins,
        "destinations":   f"{DEST_LAT},{DEST_LON}",
        "mode":           "driving",
        "departure_time": "now",
        "traffic_model":  "best_guess",
        "key":            GMAPS_API_KEY,
    }
    try:
        resp = requests.get(GMAPS_DM_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") not in ("OK", None):
            raise ValueError(f"GMaps status: {data.get('status')}")

        etas = []
        for i, bus in enumerate(buses):
            try:
                el = data["rows"][i]["elements"][0]
                if el.get("status") != "OK":
                    raise ValueError(el.get("status"))
                raw_secs = (
                    el.get("duration_in_traffic", {}).get("value")
                    or el.get("duration", {}).get("value", 0)
                )
                dist_km = haversine(float(bus.get("enlem", 0)), float(bus.get("boylam", 0)),
                                    DEST_LAT, DEST_LON)
                adjusted = int(raw_secs) - ETA_ADJUST_SECS + int(dist_km * ETA_DIST_SECS_PER_KM)
                etas.append(fmt_seconds(adjusted))
            except Exception:
                etas.append(_fallback_eta(bus))
        return etas

    except Exception as e:
        log_event("gmaps_error", error=str(e))
        console.print(f"[yellow]Google Maps hatası (haversine):[/yellow] {e}")
        return [_fallback_eta(b) for b in buses]


def _fallback_eta(bus: dict) -> str:
    dist = haversine(float(bus.get("enlem", 0)), float(bus.get("boylam", 0)),
                     DEST_LAT, DEST_LON)
    return eta_str_fallback(dist, float(bus.get("hiz", 0) or 0))


# ── Shared state ──────────────────────────────────────────────────────────────

lock  = threading.Lock()
state = {
    "buses":            [],
    "last_update":      None,
    "next_update":      None,
    "poll_stats":       {},
    "bus_history":      {},   # kapino -> list[(lat, lon)]
    "bus_misses":       {},   # kapino -> int
    "bus_closest":      {},   # kapino -> float (min km to dest ever seen)
    "passed_cooldown":  {},   # kapino -> epoch time when flagged
    "passed_info":      {},   # kapino -> bus dict (snapshot when flagged, for table display)
    "consecutive_empty": 0,
    "config": {
        "min_polls":    MIN_POLLS,
        "min_move_km":  MIN_MOVE_KM,
    },
}


# ── Ghost + passed filter ─────────────────────────────────────────────────────

def apply_filters(raw_buses: list[dict]) -> tuple[list[dict], dict]:
    """
    Classify each bus as cooldown / pending / ghost / passed / confirmed.
    Updates history, closest-approach, and passed-cooldown in state.
    Returns (confirmed_buses, stats_dict).
    """
    history   = state["bus_history"]
    misses    = state["bus_misses"]
    closest   = state["bus_closest"]
    cooldown  = state["passed_cooldown"]
    now_ts    = time.time()
    cfg_polls = state["config"]["min_polls"]
    cfg_move  = state["config"]["min_move_km"]

    # Expire old cooldowns and clean up associated tracking data
    for kap in list(cooldown.keys()):
        if now_ts - cooldown[kap] > PASSED_COOLDOWN_SECS:
            del cooldown[kap]
            state["passed_info"].pop(kap, None)
            closest.pop(kap, None)
            history.pop(kap, None)
            misses.pop(kap, None)
            log_event("cooldown_expired", kapino=kap)

    # Split: in-cooldown vs active
    n_cooldown, active_raw = 0, []
    for bus in raw_buses:
        if bus.get("kapino") in cooldown:
            n_cooldown += 1
        else:
            active_raw.append(bus)

    # Age-out buses missing from this poll
    seen_now = {b["kapino"] for b in active_raw}
    for kapino in list(history.keys()):
        if kapino not in seen_now:
            misses[kapino] = misses.get(kapino, 0) + 1
            if misses[kapino] >= EXPIRE_AFTER:
                history.pop(kapino, None)
                misses.pop(kapino, None)
                closest.pop(kapino, None)
        else:
            misses[kapino] = 0

    # Record positions and closest-approach
    for bus in active_raw:
        kapino = bus["kapino"]
        try:
            lat = float(bus.get("enlem", 0))
            lon = float(bus.get("boylam", 0))
        except (ValueError, TypeError):
            continue
        if lat == 0 and lon == 0:
            continue
        history.setdefault(kapino, []).append((lat, lon))
        if len(history[kapino]) > HISTORY_SIZE:
            history[kapino] = history[kapino][-HISTORY_SIZE:]
        dist = haversine(lat, lon, DEST_LAT, DEST_LON)
        closest[kapino] = min(closest.get(kapino, float("inf")), dist)

    # Classify
    confirmed, n_pending, n_ghost, n_passed = [], 0, 0, 0

    for bus in active_raw:
        kapino = bus["kapino"]
        h      = history.get(kapino, [])

        if len(h) < cfg_polls:
            n_pending += 1
            continue

        # Ghost check — never moved enough across all observations
        max_move = max(
            (haversine(h[i][0], h[i][1], h[j][0], h[j][1])
             for i in range(len(h)) for j in range(i + 1, len(h))),
            default=0.0,
        )
        if max_move < cfg_move:
            n_ghost += 1
            log_event("ghost_filtered",
                      kapino=kapino,
                      positions=[(round(p[0], 6), round(p[1], 6)) for p in h],
                      max_move_km=round(max_move, 6))
            continue

        # Passed-destination check — bus is inside the passed zone rectangle
        blat, blon = h[-1]
        in_box = (PASSED_BOX[0] <= blat <= PASSED_BOX[1] and
                  PASSED_BOX[2] <= blon <= PASSED_BOX[3])
        if in_box:
            n_passed += 1
            cooldown[kapino] = now_ts
            state["passed_info"][kapino] = bus
            until = datetime.fromtimestamp(now_ts + PASSED_COOLDOWN_SECS).strftime("%H:%M:%S")
            log_event("bus_passed",
                      kapino=kapino,
                      lat=round(blat, 6), lon=round(blon, 6),
                      cooldown_until=until)
            continue

        confirmed.append(bus)

    stats = {
        "raw":       len(raw_buses),
        "cooldown":  n_cooldown,
        "pending":   n_pending,
        "ghosts":    n_ghost,
        "passed":    n_passed,
        "confirmed": len(confirmed),
    }
    return confirmed, stats


# ── Terminal table ────────────────────────────────────────────────────────────

def make_renderable() -> Panel:
    with lock:
        buses       = list(state["buses"])
        updated     = state["last_update"]
        next_up     = state["next_update"]
        stats       = dict(state["poll_stats"])
        passed_cd   = dict(state["passed_cooldown"])
        passed_info = dict(state["passed_info"])

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
              pad_edge=False, show_edge=False)
    t.add_column("Hat",       style="bold red",    justify="center", min_width=6)
    t.add_column("Kapı",      style="bold yellow", justify="center", min_width=7)
    t.add_column("Mesafe",    justify="right",  min_width=9)
    t.add_column("ETA",       justify="right",  min_width=16, style="magenta")
    t.add_column("Hız",       justify="right",  min_width=9)
    t.add_column("GPS",       justify="center", min_width=7)
    t.add_column("Son konum", justify="center", min_width=17, style="dim")

    for b in buses:
        try:
            blat = float(b.get("enlem", 0))
            blon = float(b.get("boylam", 0))
            spd  = float(b.get("hiz", 0) or 0)
        except (ValueError, TypeError):
            continue
        dist = haversine(blat, blon, DEST_LAT, DEST_LON)

        # GPS staleness indicator
        age = gps_age_secs(b.get("son_konum_zamani", ""))
        if age is None:
            gps_cell = "[dim]?[/dim]"
        elif age > GPS_STALE_SECS:
            gps_cell = f"[bold red]{age // 60}dk![/bold red]"
        elif age > 120:
            gps_cell = f"[yellow]{age}s[/yellow]"
        else:
            gps_cell = f"[green]{age}s[/green]"

        t.add_row(
            b.get("hat_no", "-"),
            b.get("kapino", "-"),
            f"{dist:.2f} km",
            b.get("eta") or eta_str_fallback(dist, spd),
            f"{spd:.0f} km/h",
            gps_cell,
            b.get("son_konum_zamani", "-"),
        )

    if not buses and not passed_cd:
        t.add_row("[dim]bekleniyor...[/dim]", "", "", "", "", "", "")

    # Show passed buses below confirmed ones
    now_ts = time.time()
    for kapino, flagged_at in passed_cd.items():
        bus = passed_info.get(kapino, {})
        mins_ago = int((now_ts - flagged_at) / 60)
        try:
            blat = float(bus.get("enlem", 0))
            blon = float(bus.get("boylam", 0))
            dist_str = f"{haversine(blat, blon, DEST_LAT, DEST_LON):.2f} km" if blat else "—"
        except (ValueError, TypeError):
            dist_str = "—"
        t.add_row(
            f"[dim]{bus.get('hat_no', '?')}[/dim]",
            f"[dim]{kapino}[/dim]",
            f"[dim]{dist_str}[/dim]",
            f"[blue]geçti {mins_ago} dk önce[/blue]",
            "[dim]—[/dim]",
            "[dim]—[/dim]",
            f"[dim]{bus.get('son_konum_zamani', '—')}[/dim]",
        )

    secs_left = max(0, int((next_up or 0) - time.time())) if next_up else 0
    m, s = divmod(secs_left, 60)

    n_cd = len(passed_cd)
    status_parts = [f"[dim]güncelleme: {updated or '—'}[/dim]",
                    f"[dim]sonraki: {m}:{s:02d}[/dim]"]
    if n_cd:
        status_parts.append(f"[dim]geçti: {n_cd}[/dim]")
    if stats.get("pending"):
        status_parts.append(f"[yellow]{stats['pending']} beklemede[/yellow]")
    if stats.get("ghosts"):
        status_parts.append(f"[red]{stats['ghosts']} hayalet[/red]")

    return Panel(
        t,
        title=f"[bold cyan]59RK[/bold cyan]  Sarıtepe → [bold]{DEST_NAME}[/bold]",
        subtitle=Text("  ·  ".join(status_parts), justify="right"),
        border_style="cyan",
        padding=(0, 1),
    )


# ── HTTP request handler ──────────────────────────────────────────────────────

# HTML is now in static/ directory — see static/index.html, display.html, logs.html

HTML = """\
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8"/>
  <title>59RK/59RS · 4. Levent Takip</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { display: flex; flex-direction: column; height: 100vh;
           font-family: monospace; background: #1a1a2e; color: #eee; }
    #map  { flex: 1; position: relative; }
    #bar  { display: flex; align-items: center; gap: 10px; padding: 8px 12px;
            background: #16213e; border-top: 2px solid #0f3460; flex-wrap: wrap; }
    .chip { background: #0f3460; padding: 4px 10px; border-radius: 4px;
            font-size: 13px; white-space: nowrap; }
    .val  { color: #4fc3f7; font-weight: bold; }
    .red  { color: #e94560; font-weight: bold; }
    .btn  { background: #0f3460; border: 1px solid #4fc3f7; color: #4fc3f7;
            padding: 4px 10px; border-radius: 4px; font-size: 13px;
            cursor: pointer; font-family: monospace; }
    .btn:active { background: #1a4a6e; }
    #waiting { position: absolute; top: 50%; left: 50%;
               transform: translate(-50%,-50%);
               background: rgba(22,33,62,0.92); padding: 24px 36px;
               border-radius: 10px; border: 1px solid #0f3460;
               text-align: center; z-index: 1000;
               font-size: 15px; pointer-events: none; }
    #waiting b { color: #4fc3f7; }
    /* Log panel */
    #logpanel { display: none; background: #16213e; border-top: 1px solid #0f3460;
                max-height: 220px; overflow-y: auto; padding: 8px 12px; }
    #logpanel .lrow { font-size: 12px; padding: 2px 0; border-bottom: 1px solid #0f346033; }
    #logpanel .ts   { color: #4fc3f7; margin-right: 8px; }
    #logpanel .ev   { color: #e94560; margin-right: 6px; }
    /* Config modal */
    #cfgmodal { display: none; position: absolute; top: 50%; left: 50%;
                transform: translate(-50%,-50%);
                background: #16213e; border: 1px solid #4fc3f7;
                border-radius: 10px; padding: 20px 24px; z-index: 2000;
                min-width: 260px; }
    #cfgmodal h3 { color: #4fc3f7; margin-bottom: 14px; font-size: 14px; }
    #cfgmodal label { display: block; font-size: 12px; color: #aaa; margin-bottom: 4px; }
    #cfgmodal input { width: 100%; background: #0f3460; border: 1px solid #4fc3f7;
                      color: #eee; padding: 5px 8px; border-radius: 4px;
                      font-family: monospace; margin-bottom: 12px; }
    #cfgmodal .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
    #cfgsave { background: #4fc3f7; color: #1a1a2e; border: none;
               padding: 6px 16px; border-radius: 4px; cursor: pointer; font-weight: bold; }
    #cfgcancel { background: none; border: 1px solid #aaa; color: #aaa;
                 padding: 6px 14px; border-radius: 4px; cursor: pointer; }
  </style>
</head>
<body>
<div id="map">
  <div id="waiting">
    <div>⏳ Hayalet filtresi aktif — bekleniyor...</div>
    <div style="margin-top:8px;color:#aaa;font-size:12px">
      Araçlar <b id="poll-cdwn">~4 dakika</b> sonra görünecek
    </div>
  </div>
  <div id="cfgmodal">
    <h3>⚙ Hayalet Filtresi Ayarları</h3>
    <label>Minimum poll sayısı (MIN_POLLS)</label>
    <input id="inp-polls" type="number" min="1" max="20" step="1"/>
    <label>Minimum hareket mesafesi — km (MIN_MOVE_KM)</label>
    <input id="inp-move" type="number" min="0.001" max="1" step="0.001"/>
    <div id="cfgmsg" style="font-size:12px;color:#4fc3f7;min-height:18px;margin-bottom:6px"></div>
    <div class="row">
      <button id="cfgcancel" onclick="closeCfg()">İptal</button>
      <button id="cfgsave"   onclick="saveConfig()">Kaydet</button>
    </div>
  </div>
</div>
<div id="logpanel"></div>
<div id="bar">
  <span class="chip">Hat: <span class="red">59RK / 59RS</span></span>
  <span class="chip">→ <span class="red">4. Levent</span></span>
  <span class="chip">Aktif: <span class="val" id="cnt">—</span></span>
  <span class="chip">Güncelleme: <span class="val" id="upd">—</span></span>
  <span class="chip">Sonraki: <span class="val" id="cdwn">—</span></span>
  <button class="btn" onclick="toggleLogs()">📋 Loglar</button>
  <button class="btn" onclick="openCfg()">⚙ Ayar</button>
</div>
<script>
const REFRESH_MS = 60_000;
let nextAt = Date.now() + REFRESH_MS;
let busMarkers = {}, destMarker = null, passedRect = null, pollsDone = 0, logsOpen = false;

const map = L.map('map').setView([41.09, 28.99], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}).addTo(map);

const busIcons = {
  "59RK": L.divIcon({ html: '<span style="font-size:26px">🚌</span>', iconSize:[32,32], iconAnchor:[16,16], className:'' }),
  "59RS": L.divIcon({ html: '<span style="font-size:26px">🚎</span>', iconSize:[32,32], iconAnchor:[16,16], className:'' }),
};
const destIcon = L.divIcon({ html: '<span style="font-size:26px">📍</span>', iconSize:[32,32], iconAnchor:[16,30], className:'' });

function getBusIcon(b) { return busIcons[b.hat_no] || busIcons["59RK"]; }

function popup(b) {
  return `<b>${b.kapino}</b> <span style="color:#e94560">[${b.hat_no||'?'}]</span><br>
Hız: <b>${parseFloat(b.hiz||0).toFixed(0)} km/h</b><br>
Yön: ${b.yon}<br>
Mesafe: <b>${b.dist_km} km</b><br>
ETA: <b>${b.eta || '—'}</b><br>
Son konum: ${b.son_konum_zamani}`;
}

async function update() {
  try {
    const r = await fetch('/buses'), data = await r.json();
    pollsDone++;

    // Count per line
    const counts = {};
    for (const b of data.buses) counts[b.hat_no] = (counts[b.hat_no]||0)+1;
    const cntText = Object.entries(counts).map(([k,v])=>`${k}:${v}`).join(' / ') || '0';
    document.getElementById('cnt').textContent = cntText;
    document.getElementById('upd').textContent = data.last_update || '—';

    if (data.buses.length > 0) {
      const w = document.getElementById('waiting');
      if (w) w.style.display = 'none';
    } else {
      const rem = Math.max(0, 4 - pollsDone);
      const el = document.getElementById('poll-cdwn');
      if (el) el.textContent = rem > 0 ? `~${rem} dakika` : 'az kaldı';
    }

    if (!destMarker) {
      destMarker = L.marker([data.dest_lat, data.dest_lon], { icon: destIcon })
        .bindPopup('<b>4. Levent</b>').addTo(map);
    }
    if (!passedRect && data.passed_box) {
      const [latMin, latMax, lonMin, lonMax] = data.passed_box;
      passedRect = L.rectangle([[latMin, lonMin], [latMax, lonMax]], {
        color: '#e94560', weight: 1.5, dashArray: '6 4',
        fillColor: '#e94560', fillOpacity: 0.07,
      }).bindTooltip('Geçti bölgesi', {permanent: false}).addTo(map);
    }

    const live = new Set(data.buses.map(b => b.kapino));
    for (const id in busMarkers) {
      if (!live.has(id)) { map.removeLayer(busMarkers[id]); delete busMarkers[id]; }
    }
    for (const b of data.buses) {
      const lat = parseFloat(b.enlem), lon = parseFloat(b.boylam);
      if (!lat || !lon) continue;
      if (busMarkers[b.kapino]) {
        busMarkers[b.kapino].setLatLng([lat, lon]).setPopupContent(popup(b));
      } else {
        busMarkers[b.kapino] = L.marker([lat, lon], { icon: getBusIcon(b) })
          .bindPopup(popup(b)).addTo(map);
      }
    }
    if (logsOpen) await refreshLogs();
  } catch(e) { console.error(e); }
  nextAt = Date.now() + REFRESH_MS;
}

// ── Log panel ────────────────────────────────────────────────────────────────
const EVENT_ICONS = {
  startup:'▶', shutdown:'■', poll:'·', ghost_filtered:'☠',
  bus_passed:'✓', api_empty:'?', api_retry:'↻', api_fail:'✗',
  stale_gps:'⚠', gmaps_error:'!', config_changed:'⚙', cooldown_expired:'⌛'
};

async function refreshLogs() {
  try {
    const r = await fetch('/logs?n=40'), data = await r.json();
    const panel = document.getElementById('logpanel');
    panel.innerHTML = data.events.slice().reverse().map(e => {
      const icon = EVENT_ICONS[e.event] || '·';
      const ts = (e.ts||'').slice(11,19);
      const extra = e.kapino ? ` ${e.kapino}` : (e.error ? ` ${e.error}` : '');
      return `<div class="lrow"><span class="ts">${ts}</span><span class="ev">${icon} ${e.event}</span>${extra}</div>`;
    }).join('');
  } catch(e) {}
}

async function toggleLogs() {
  const panel = document.getElementById('logpanel');
  logsOpen = !logsOpen;
  panel.style.display = logsOpen ? 'block' : 'none';
  if (logsOpen) await refreshLogs();
}

// ── Config modal ─────────────────────────────────────────────────────────────
async function openCfg() {
  const r = await fetch('/config'), cfg = await r.json();
  document.getElementById('inp-polls').value = cfg.min_polls;
  document.getElementById('inp-move').value  = cfg.min_move_km;
  document.getElementById('cfgmsg').textContent = '';
  document.getElementById('cfgmodal').style.display = 'block';
}
function closeCfg() { document.getElementById('cfgmodal').style.display = 'none'; }

async function saveConfig() {
  const payload = {
    min_polls:   parseInt(document.getElementById('inp-polls').value),
    min_move_km: parseFloat(document.getElementById('inp-move').value),
  };
  try {
    const r = await fetch('/config', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (data.ok) {
      document.getElementById('cfgmsg').textContent = '✓ Kaydedildi';
      setTimeout(closeCfg, 800);
    } else {
      document.getElementById('cfgmsg').textContent = '✗ Hata: ' + (data.error||'?');
    }
  } catch(e) {
    document.getElementById('cfgmsg').textContent = '✗ ' + e;
  }
}

// ── Countdown + auto-refresh ─────────────────────────────────────────────────
setInterval(() => {
  const s = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
  const m = Math.floor(s / 60), sec = s % 60;
  document.getElementById('cdwn').textContent = `${m}:${String(sec).padStart(2,'0')}`;
}, 1000);

setInterval(update, REFRESH_MS);
update();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]  # strip query string for routing
        if p in ("/", "/map"):
            self._serve_file(STATIC_DIR / "index.html")
        elif p == "/display":
            self._serve_file(STATIC_DIR / "display.html")
        elif p == "/logs-page":
            self._serve_file(STATIC_DIR / "logs.html")
        elif p == "/buses":
            now_ts = time.time()
            with lock:
                buses       = list(state["buses"])
                updated     = state["last_update"]
                next_up     = state["next_update"]
                passed_cd   = dict(state["passed_cooldown"])
                passed_info = dict(state["passed_info"])
            enriched = []
            for b in buses:
                try:
                    blat = float(b.get("enlem", 0))
                    blon = float(b.get("boylam", 0))
                except (ValueError, TypeError):
                    continue
                if blat == 0 and blon == 0:
                    continue
                enriched.append({
                    **b,
                    "dist_km":     round(haversine(blat, blon, DEST_LAT, DEST_LON), 2),
                    "gps_age_secs": gps_age_secs(b.get("son_konum_zamani", "")),
                })
            passed_buses = sorted([
                {
                    "kapino":           kapino,
                    "hat_no":           passed_info.get(kapino, {}).get("hat_no", "?"),
                    "mins_ago":         int((now_ts - flagged_at) / 60),
                    "son_konum_zamani": passed_info.get(kapino, {}).get("son_konum_zamani", ""),
                }
                for kapino, flagged_at in passed_cd.items()
            ], key=lambda x: x["mins_ago"])
            payload = json.dumps({
                "buses":            enriched,
                "passed_buses":     passed_buses,
                "dest_lat":         DEST_LAT,
                "dest_lon":         DEST_LON,
                "last_update":      updated,
                "passed_box":       PASSED_BOX,
                "next_update_secs": max(0, int((next_up or 0) - now_ts)),
            }, ensure_ascii=False).encode()
            self._respond(200, "application/json", payload)
        elif p == "/config":
            with lock:
                cfg = dict(state["config"])
            self._respond(200, "application/json", json.dumps(cfg).encode())
        elif p == "/logs/raw":
            try:
                n = int(self.path.split("n=")[1].split("&")[0]) if "n=" in self.path else 20
            except (IndexError, ValueError):
                n = 20
            n = min(n, 100)
            raw_path = RAW_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            entries = []
            if raw_path.exists():
                lines = raw_path.read_text(encoding="utf-8").splitlines()
                for line in lines[-n:]:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            payload = json.dumps({"entries": entries}, ensure_ascii=False).encode()
            self._respond(200, "application/json", payload)
        elif p == "/logs":
            try:
                n = int(self.path.split("n=")[1].split("&")[0]) if "n=" in self.path else 30
            except (IndexError, ValueError):
                n = 30
            n = min(n, 200)
            log_path = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
            events = []
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8").splitlines()
                for line in lines[-n:]:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            payload = json.dumps({"events": events}, ensure_ascii=False).encode()
            self._respond(200, "application/json", payload)
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path == "/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                with lock:
                    if "min_polls" in body:
                        state["config"]["min_polls"] = max(1, int(body["min_polls"]))
                    if "min_move_km" in body:
                        state["config"]["min_move_km"] = max(0.001, float(body["min_move_km"]))
                    cfg = dict(state["config"])
                log_event("config_changed", **cfg)
                self._respond(200, "application/json", json.dumps({"ok": True, **cfg}).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"error": str(e)}).encode())
        else:
            self._respond(404, "text/plain", b"Not found")

    def _serve_file(self, path: Path):
        try:
            self._respond(200, "text/html; charset=utf-8", path.read_bytes())
        except FileNotFoundError:
            self._respond(404, "text/plain", b"Page not found")

    def _respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Background fetch loop ─────────────────────────────────────────────────────

def fetch_loop():
    while True:
        raw = []
        for hat_no in HAT_NOS:
            buses = fetch_buses(hat_no)
            for b in buses:
                b["hat_no"] = hat_no
            raw.extend(buses)
        now = datetime.now().strftime("%H:%M:%S")

        # Handle empty response — keep previous data for up to 2 consecutive failures
        if not raw:
            with lock:
                prev = len(state["buses"])
                state["consecutive_empty"] += 1
                empty_n = state["consecutive_empty"]
                if empty_n >= 2:
                    state["buses"] = []
                state["last_update"] = now + " [!]"
                state["next_update"] = time.time() + REFRESH_SECS
            log_event("api_empty", consecutive=empty_n, previous_bus_count=prev)
            console.print(f"[dim]{now}[/dim]  [yellow]Boş yanıt (ardışık: {empty_n})[/yellow]")
            time.sleep(REFRESH_SECS)
            continue

        with lock:
            state["consecutive_empty"] = 0
            confirmed, stats = apply_filters(raw)

        # Check GPS staleness on confirmed buses
        for bus in confirmed:
            age = gps_age_secs(bus.get("son_konum_zamani", ""))
            if age is not None and age > GPS_STALE_SECS:
                log_event("stale_gps",
                          kapino=bus["kapino"],
                          gps_age_secs=age,
                          gps_ts=bus.get("son_konum_zamani"))

        # Compute ETAs
        if confirmed:
            etas = get_traffic_etas(confirmed)
            for bus, eta in zip(confirmed, etas):
                bus["eta"] = eta

        # Full poll log — all the info you need for debugging
        with lock:
            hist_snap = {k: list(v) for k, v in state["bus_history"].items()}
            close_snap = dict(state["bus_closest"])
        log_event("poll",
            stats=stats,
            buses=[{
                "kapino":          b.get("kapino"),
                "lat":             b.get("enlem"),
                "lon":             b.get("boylam"),
                "speed_kmh":       b.get("hiz"),
                "direction":       b.get("yon"),
                "guzergah":        b.get("guzergahkodu"),
                "nearest_stop":    b.get("yakinDurakKodu"),
                "gps_ts":          b.get("son_konum_zamani"),
                "gps_age_secs":    gps_age_secs(b.get("son_konum_zamani", "")),
                "dist_to_dest_km": round(haversine(
                    float(b.get("enlem", 0)), float(b.get("boylam", 0)),
                    DEST_LAT, DEST_LON), 3),
                "eta":             b.get("eta"),
                "bearing_deg":     round(movement_bearing(
                    hist_snap.get(b.get("kapino", ""), [])) or 0, 1),
                "closest_km":      round(close_snap.get(b.get("kapino", ""), 0), 3),
                "status":          "confirmed",
            } for b in confirmed]
        )

        with lock:
            state["buses"]       = confirmed
            state["last_update"] = now
            state["next_update"] = time.time() + REFRESH_SECS
            state["poll_stats"]  = stats

        # Compact terminal status line
        parts = [f"[dim]{now}[/dim]  raw={stats['raw']}"]
        if stats.get("cooldown"): parts.append(f"[dim]cd={stats['cooldown']}[/dim]")
        if stats["pending"]:      parts.append(f"[yellow]pend={stats['pending']}[/yellow]")
        if stats["ghosts"]:       parts.append(f"[red]ghost={stats['ghosts']}[/red]")
        if stats["passed"]:       parts.append(f"[blue]passed={stats['passed']}[/blue]")
        parts.append(f"[green]ok={stats['confirmed']}[/green]")
        console.print("  ".join(parts))

        time.sleep(REFRESH_SECS)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    gmaps_tag = ("[green]aktif[/green]" if GMAPS_API_KEY
                 else "[yellow]yok — haversine kullanılacak[/yellow]")
    console.print(Panel.fit(
        f"[bold cyan]59 RK/RS[/bold cyan] → [bold]{DEST_NAME}[/bold]\n"
        f"[dim]1 dk'da bir güncellenir  ·  Google Maps: {gmaps_tag}[/dim]",
        border_style="cyan",
    ))

    log_event("startup", gmaps_active=bool(GMAPS_API_KEY),
              dest_lat=DEST_LAT, dest_lon=DEST_LON,
              refresh_secs=REFRESH_SECS, eta_adjust_secs=ETA_ADJUST_SECS)

    # First poll
    console.print("\n[dim]İlk veri alınıyor...[/dim]")
    raw = []
    for hat_no in HAT_NOS:
        buses = fetch_buses(hat_no)
        for b in buses:
            b["hat_no"] = hat_no
        raw.extend(buses)
    with lock:
        confirmed, stats = apply_filters(raw)
        state["buses"]            = confirmed
        state["last_update"]      = datetime.now().strftime("%H:%M:%S")
        state["next_update"]      = time.time() + REFRESH_SECS
        state["poll_stats"]       = stats
        state["consecutive_empty"] = 0
    console.print(f"[green]{stats['raw']} araç alındı[/green]  "
                  f"— {MIN_POLLS - 1} tur daha bekleniyor\n")

    threading.Thread(target=fetch_loop, daemon=True).start()

    PORT = 8765
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    today = datetime.now().strftime('%Y-%m-%d')
    console.print(f"[bold green]Harita:[/bold green] http://<sunucu-ip>:{PORT}  "
                  f"[dim]· Olaylar: logs/{today}.log  "
                  f"· Ham API: logs/raw/{today}.jsonl[/dim]\n")

    is_tty = sys.stdout.isatty()
    try:
        if is_tty:
            with Live(make_renderable(), console=console, refresh_per_second=1) as live:
                while True:
                    live.update(make_renderable())
                    time.sleep(1)
        else:
            # Headless mode (systemd service / no terminal) — fetch_loop logs to file
            console.print("[dim]Headless mod — terminal tablosu devre dışı.[/dim]")
            while True:
                time.sleep(60)
    except KeyboardInterrupt:
        log_event("shutdown")
        console.print("\n[bold red]Çıkılıyor...[/bold red]")
        sys.exit(0)


if __name__ == "__main__":
    main()
