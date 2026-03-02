# İETT 59RK / 59RS Bus Tracker — Project Documentation

## What This Project Does

Tracks the real-time positions of IETT routes **59RK** (Sarıtepe → Boğaziçi/Rumeli Hisarüstü) and **59RS** (Sarıyer → Rumeli Hisar Üstü) and shows how long until each bus reaches **4. Levent** (41.0845294, 29.0072518). It runs as a local Python process that:

- Polls the IETT SOAP API every **2 minutes** (one call per line, 2 calls per cycle = 60 req/hour)
- Serves an interactive Leaflet.js map at `http://localhost:8765`
- Displays a live terminal table alongside the map
- Filters out ghost buses and buses that have already passed the destination
- Calculates traffic-aware ETA via Google Maps Distance Matrix API
- Logs every event to `logs/YYYY-MM-DD.log` (JSON lines)
- Logs raw IETT API responses (pre-filter) to `logs/raw/YYYY-MM-DD.jsonl`

---

## Files

| File | Purpose |
|---|---|
| `map_tracker.py` | Main application — API polling, filtering, HTTP server, terminal table |
| `static/index.html` | Map page — Leaflet.js map with extended bus popups, log panel, config modal |
| `static/display.html` | Street display page — large-text arrival board, passed buses |
| `static/logs.html` | Log viewer page — event table, filters, raw API browser |
| `tracker.py` | Earlier terminal-only version (kept for reference) |
| `log_viewer.py` | CLI log inspection tool — summary, timeline, bus history, live tail |
| `logs/YYYY-MM-DD.log` | Daily JSON-lines event log |
| `logs/raw/YYYY-MM-DD.jsonl` | Raw IETT API responses (pre-filter), for debugging |
| `tr_iett-web-servis-kullanm-dokumanv.1.5.pdf` | Official IETT API documentation |
| `DOCS.md` | This file |

---

## How to Run

```bash
# Install dependencies (one time)
pip install requests rich

# Run the tracker
python3 map_tracker.py

# Inspect logs
python3 log_viewer.py                      # today's summary
python3 log_viewer.py --tail               # live tail
python3 log_viewer.py --stats              # poll statistics table
python3 log_viewer.py --errors             # only errors/warnings
python3 log_viewer.py --bus O2289          # history for a specific bus
python3 log_viewer.py --event bus_passed   # filter by event type
python3 log_viewer.py --date 2026-02-24    # a specific date's log
python3 log_viewer.py --raw                # raw IETT API responses
python3 log_viewer.py --raw --bus O2289    # raw responses for one bus
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        map_tracker.py                           │
│                                                                 │
│  main()                                                         │
│   │                                                             │
│   ├── Thread: fetch_loop() ─── every 120s ─────────────────┐   │
│   │    │                                                    │   │
│   │    ├── fetch_buses("59RK")   ← IETT SOAP API           │   │
│   │    │    └── direction filter: _D_ (→ Boğaziçi)         │   │
│   │    │    └── retry once on failure                       │   │
│   │    │    └── deduplicate by kapino                       │   │
│   │    │    └── tag each bus with hat_no = "59RK"           │   │
│   │    │                                                    │   │
│   │    ├── fetch_buses("59RS")   ← IETT SOAP API           │   │
│   │    │    └── direction filter: _G_ (→ Rumeli Hisar Üstü)│   │
│   │    │    └── retry once on failure                       │   │
│   │    │    └── deduplicate by kapino                       │   │
│   │    │    └── tag each bus with hat_no = "59RS"           │   │
│   │    │                                                    │   │
│   │    ├── apply_filters(raw)                               │   │
│   │    │    ├── skip buses in passed_cooldown               │   │
│   │    │    ├── update position history per bus             │   │
│   │    │    ├── pending: seen < 4 polls → hidden            │   │
│   │    │    ├── ghost: moved < 20m over 4+ polls → hidden   │   │
│   │    │    └── passed: inside PASSED_BOX rectangle         │   │
│   │    │                                                    │   │
│   │    ├── get_traffic_etas(confirmed)  ← Google Maps API   │   │
│   │    │    └── fallback to haversine if key missing        │   │
│   │    │                                                    │   │
│   │    └── log_event("poll", ...)  → logs/YYYY-MM-DD.log   │   │
│   │                                                         │   │
│   ├── Thread: HTTPServer (port 8765) ──────────────────────┘   │
│   │    ├── GET /             → static/index.html (map)          │
│   │    ├── GET /display      → static/display.html (board)      │
│   │    ├── GET /logs-page    → static/logs.html (log viewer)    │
│   │    ├── GET /buses        → JSON (buses, passed, ETA, box)   │
│   │    ├── GET /config       → ghost filter config (JSON)       │
│   │    ├── POST /config      → update ghost filter config       │
│   │    ├── GET /logs         → recent log events (JSON)         │
│   │    └── GET /logs/raw     → recent raw API responses (JSON)  │
│   │                                                             │
│   └── Main thread: rich.Live table (refreshes every 1s)        │
└─────────────────────────────────────────────────────────────────┘

Browser
  ├── / (map)      — polls /buses every 120s, updates Leaflet markers
  ├── /display     — polls /buses every 120s, large-text arrival board
  └── /logs-page   — fetches /logs?n=200 and /logs/raw?n=20 on demand
```

---

## Data Flow

```
IETT SOAP API
    │  GetHatOtoKonum_json(HatKodu="59RK")  → filter _D_  (Sarıtepe → Boğaziçi)
    │  GetHatOtoKonum_json(HatKodu="59RS")  → filter _G_  (Sarıyer → Rumeli Hisar Üstü)
    │  Returns: list of bus objects per call
    ▼
fetch_buses(hat_no)
    │  Per-line direction filter via DIRECTION_FILTERS dict
    │  Deduplicate: keep fresher GPS timestamp per kapino
    │  Retry once on network/parse failure
    │  Log raw response to logs/raw/YYYY-MM-DD.jsonl
    ▼
apply_filters(raw_buses)
    │  1. Skip kapinos in passed_cooldown (30 min ban after passing dest)
    │  2. Update bus_history[kapino].append((lat, lon))  — max 6 snapshots
    │  3. Update bus_closest[kapino] = min(dist_to_dest ever seen)
    │  4. Classify:
    │       pending  → seen_count < 4 polls
    │       ghost    → seen_count ≥ 4 AND max_displacement < 20m
    │       passed   → latest position inside PASSED_BOX rectangle
    │       confirmed → everything else
    ▼
get_traffic_etas(confirmed)
    │  Single batched Google Maps Distance Matrix call (all buses in one request)
    │  Uses duration_in_traffic.value (seconds)
    │  ETA formula: raw_secs − ETA_ADJUST_SECS + dist_km × ETA_DIST_SECS_PER_KM
    │  Falls back to haversine / average speed (labeled [~]) if:
    │    - GMAPS_API_KEY not set
    │    - API returns error / quota exceeded
    │    - Per-element status != OK
    ▼
state["buses"] = confirmed  (thread-safe via lock)
    │
    ├── → Terminal table (rich.Live, 1s refresh)
    │        Active buses: Hat / Kapı / Mesafe / ETA / Hız / GPS / Son konum
    │        Passed buses: shown below with "geçti X dk önce"
    │
    └── → HTTP /buses endpoint → three browser pages poll every 120s
             → map:     Leaflet markers + passed zone rectangle
             → display: large-text arrival board + passed list
             → logs:    event table + raw API browser
```

---

## The IETT API

**Endpoint:** `https://api.ibb.gov.tr/iett/FiloDurum/SeferGerceklesme.asmx`
**Method:** `GetHatOtoKonum_json`
**Protocol:** SOAP 1.1 (ASMX)
**Rate limit:** 100 requests/hour
**Auth:** None required for this endpoint

**Request parameter:** `HatKodu` (string) — the route code, e.g. `"59RK"` or `"59RS"`

**Response fields per bus:**

| Field | Type | Description |
|---|---|---|
| `kapino` | string | Vehicle door number (unique bus ID) |
| `enlem` | string | Latitude |
| `boylam` | string | Longitude |
| `hiz` | string | Speed in km/h |
| `yon` | string | Text destination (e.g. "BOĞAZİÇİ ÜNİVERSİTESİ KAMPÜSÜ") |
| `hatkodu` | string | Route code (e.g. "59RK") |
| `guzergahkodu` | string | Direction code — see per-line table below |
| `son_konum_zamani` | string | Last GPS fix timestamp: `"YYYY-MM-DD HH:MM:SS"` |
| `yakinDurakKodu` | string | Nearest stop code by current coordinates |

**Per-line direction codes:**

| Line | Wanted direction | `guzergahkodu` contains | Route |
|---|---|---|---|
| 59RK | `_D_` | `59RK_D_D0` | Sarıtepe → Boğaziçi / Rumeli Hisarüstü |
| 59RS | `_G_` | `59RS_G_D0` | Sarıyer → Rumeli Hisar Üstü |

The `DIRECTION_FILTERS` dict in the code maps each line to its correct code. The wrong direction (buses heading away from 4. Levent) is discarded before any further processing.

**Known instabilities:**
- Occasionally returns HTTP 500 — retry logic handles this
- Sometimes returns empty list even when buses are active — previous data kept for 1 cycle, cleared after 2 consecutive empties
- Duplicate `kapino` entries can appear — deduplication keeps the fresher `son_konum_zamani`
- GPS timestamps can lag several minutes behind real time (stale GPS)
- "Ghost buses" — kapino appears for 1–3 polls with static coordinates, then vanishes

---

## Ghost Bus Detection

The API sometimes returns buses with valid kapino IDs that never move. These are likely GPS artifacts or stale replayed data from the server.

**Detection logic:**
1. Track the last 6 GPS positions per bus (`bus_history[kapino]`)
2. A bus needs at least **4 consecutive polls** before it's shown at all (pending phase)
3. After 4 polls, compute the **maximum pairwise displacement** across all recorded positions
4. If `max_displacement < 20 m` → classified as ghost → never shown
5. If the bus disappears for **3+ consecutive polls** → history entry is deleted

**Why 4 polls / 20m:** A bus stuck in heavy Istanbul traffic still creeps ~20–50m over 4 minutes. A ghost bus reports identical coordinates indefinitely. The thresholds are tight enough to catch ghosts but loose enough not to filter real buses stopped at traffic lights.

**Runtime adjustment:** Open the map in a browser and click **⚙ Ayar** to change `MIN_POLLS` and `MIN_MOVE_KM` without restarting. Changes take effect on the next poll and are logged as `config_changed`.

---

## Passed-Destination Detection

Once a bus enters the defined zone around 4. Levent, it is removed from the active list and shown in the terminal table as "geçti X dk önce" (passed X min ago).

**Detection logic:**
1. After every poll, check if the bus's latest GPS position falls inside `PASSED_BOX`
2. `PASSED_BOX` is a lat/lon bounding rectangle defined by two corner points in the code
3. If the bus is inside the box → flagged as "passed", enters `passed_cooldown` for **30 minutes**
4. During cooldown, the bus appears in the terminal table (below confirmed buses) with "geçti X dk önce", but is excluded from the map and active ETA calculations
5. When the cooldown expires, all in-memory tracking data for that kapino is cleaned up

**The passed zone rectangle** is drawn on the Leaflet map as a dashed red rectangle. Tap/hover it to see the "Geçti bölgesi" tooltip. This lets you visually verify and tune the zone boundaries.

**Corner points (current):**
```
41.07255239019356, 29.058743864878764
41.100338391079866, 29.01610010256362
```

To change the zone, update `PASSED_BOX` in `map_tracker.py` with new corner point coordinates.

**Why 30 min cooldown:** Without it, a passed bus re-enters tracking on the next poll due to GPS drift or route looping. 30 minutes is safely longer than a typical turnaround time.

**Cleanup on expiry:** `bus_history`, `bus_closest`, and `bus_misses` entries are removed when cooldown expires, preventing unbounded memory growth. The full history is preserved in the log files.

---

## ETA Calculation

**Formula (applied to both Google Maps and haversine fallback):**
```
final_eta = raw_secs − ETA_ADJUST_SECS + (dist_km × ETA_DIST_SECS_PER_KM)
          = raw_secs − 240             + (dist_km × 50)
```

**With Google Maps API key:**
- Single batched `Distance Matrix API` call: all confirmed bus origins → 4. Levent destination
- `departure_time=now`, `traffic_model=best_guess`, `mode=driving`
- Uses `duration_in_traffic.value` as `raw_secs`
- Falls back per-element if element status != OK

**Without API key (or on error):**
- Haversine straight-line distance ÷ bus speed (fallback 20 km/h if stopped) as `raw_secs`
- Same formula applied
- Labeled `[~]` in the display

**Adjustment breakdown:**
- `ETA_ADJUST_SECS = 240` — fixed 4-minute subtraction (route consistently overestimated)
- `ETA_DIST_SECS_PER_KM = 50` — adds ~50 seconds per km remaining (accounts for last-km traffic and stop delays near the destination)

**Example:** bus 6 km away with 18-min traffic ETA → `1080 − 240 + 300 = 1140s = 19 min`

Adjust both constants at the top of `map_tracker.py` if accuracy drifts.

---

## Web Pages

All three pages share the same dark color palette, monospace font, and top navigation bar (🗺 Harita / 🚏 Durak / 📋 Log). All are responsive for mobile and desktop. HTML lives in `static/` and is served as static files by the HTTP server.

### 🗺 Harita — `/` (`static/index.html`)

Interactive Leaflet.js map.

| Element | Description |
|---|---|
| 🚌 marker | 59RK bus — tap for full detail popup |
| 🚎 marker | 59RS bus — same popup |
| 📍 marker | 4. Levent destination |
| Dashed red rectangle | Passed zone (PASSED_BOX) — hover for tooltip |
| Bus popup | kapino, line, mesafe, ETA, hız, yön, güzergah kodu, yakın durak, GPS yaşı, son konum |
| Bottom bar | Per-line active count, last update time, next update countdown |
| **📋 Loglar** button | Collapsible panel: last 40 events, newest first, auto-refreshes each poll |
| **⚙ Ayar** button | Modal to adjust `MIN_POLLS` and `MIN_MOVE_KM` at runtime |

### 🚏 Durak — `/display` (`static/display.html`)

Large-text arrival board — designed to be glanceable, like a real bus stop display.

| Section | Content |
|---|---|
| Header | "4. LEVENT" + route names |
| Active buses | Hat / Mesafe / ETA — large font, ETA green (≤5 min) / yellow (≤10 min) / white |
| Son Geçenler | Recently passed buses: line + "X dk önce geçti" (no kapino shown) |
| Footer | Last update time + countdown to next poll |

### 📋 Log — `/logs-page` (`static/logs.html`)

Full log viewer.

| Element | Description |
|---|---|
| Filter buttons | Tümü · poll · ✓ geçti · ☠ hayalet · ✗ hatalar |
| Event table | Time / Event type / Details — color-coded, newest first, up to 200 events |
| **↻ Yenile** button | Manually refresh |
| **⏱ Oto** toggle | Auto-refresh every 30 seconds |
| **📦 Ham API Yanıtları** | Collapsible section: last 20 raw IETT responses with all bus fields |

---

## HTTP Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` or `/map` | Serves `static/index.html` (Leaflet map) |
| GET | `/display` | Serves `static/display.html` (street board) |
| GET | `/logs-page` | Serves `static/logs.html` (log viewer) |
| GET | `/buses` | JSON: confirmed buses + passed buses + dest coords + passed box + countdown |
| GET | `/config` | JSON: current ghost filter config (`min_polls`, `min_move_km`) |
| POST | `/config` | Update ghost filter config (JSON body: `{"min_polls": 4, "min_move_km": 0.02}`) |
| GET | `/logs?n=N` | JSON: last N event log entries (default 30, max 200) |
| GET | `/logs/raw?n=N` | JSON: last N raw IETT API response entries (default 20, max 100) |

### `/buses` response shape

```json
{
  "buses": [
    {
      "kapino": "O2289", "hat_no": "59RK",
      "enlem": "41.123", "boylam": "29.012",
      "hiz": "35", "yon": "BOĞAZİÇİ ÜNİVERSİTESİ KAMPÜSÜ",
      "guzergahkodu": "59RK_D_D0", "yakinDurakKodu": "113253",
      "son_konum_zamani": "2026-02-24 14:30:01",
      "eta": "19 dk", "dist_km": 6.2, "gps_age_secs": 12
    }
  ],
  "passed_buses": [
    {"kapino": "O1234", "hat_no": "59RS", "mins_ago": 7, "son_konum_zamani": "..."}
  ],
  "dest_lat": 41.0845294, "dest_lon": 29.0072518,
  "last_update": "14:32:01",
  "passed_box": [41.0726, 41.1003, 29.0161, 29.0587],
  "next_update_secs": 94
}
```

---

## Configuration Constants

All tuneable values are at the top of `map_tracker.py`:

| Constant | Default | Description |
|---|---|---|
| `GMAPS_API_KEY` | `"..."` | Google Maps API key — paste here or set `GMAPS_KEY` env var |
| `DEST_LAT` / `DEST_LON` | 41.0845294 / 29.0072518 | 4. Levent coordinates |
| `DIRECTION_FILTERS` | `{"59RK": "_D_", "59RS": "_G_"}` | Per-line direction code filter |
| `HAT_NOS` | `["59RK", "59RS"]` | Lines to track |
| `REFRESH_SECS` | `120` | Polling interval in seconds (2 min = 60 req/hour for 2 lines) |
| `ETA_ADJUST_SECS` | `240` | Fixed seconds subtracted from all ETAs (4 min) |
| `ETA_DIST_SECS_PER_KM` | `50` | Seconds added per km of remaining distance |
| `HISTORY_SIZE` | `6` | GPS snapshots kept per bus |
| `EXPIRE_AFTER` | `3` | Missed polls before bus is dropped from history |
| `MIN_POLLS` | `4` | Polls required before a bus is shown (also adjustable at runtime) |
| `MIN_MOVE_KM` | `0.02` | 20m — movement threshold for ghost detection (also adjustable at runtime) |
| `PASSED_BOX` | two corner points | Bounding rectangle — bus inside → marked as passed |
| `PASSED_COOLDOWN_SECS` | `1800` | 30 min before a passed bus is forgotten |
| `GPS_STALE_SECS` | `300` | 5 min — GPS age threshold for staleness warning |
| `STATIC_DIR` | `./static` | Directory containing the three HTML page files |

---

## Shared State (`state` dict)

All mutable data lives in a single thread-safe dict protected by `lock`:

| Key | Type | Description |
|---|---|---|
| `buses` | `list[dict]` | Currently confirmed buses (shown on map and in table) |
| `last_update` | `str` | Timestamp of last successful poll |
| `next_update` | `float` | Epoch time of next poll (used for countdown) |
| `poll_stats` | `dict` | Latest poll counts: raw/cooldown/pending/ghosts/passed/confirmed |
| `bus_history` | `dict[str, list]` | `kapino → [(lat, lon), ...]` — last 6 positions |
| `bus_misses` | `dict[str, int]` | `kapino → consecutive missed polls` |
| `bus_closest` | `dict[str, float]` | `kapino → min km to dest ever recorded` |
| `passed_cooldown` | `dict[str, float]` | `kapino → epoch time when flagged as passed` |
| `passed_info` | `dict[str, dict]` | `kapino → bus dict snapshot at time of passing (for table display)` |
| `consecutive_empty` | `int` | Consecutive polls returning no buses |
| `config` | `dict` | Runtime-adjustable ghost filter settings (`min_polls`, `min_move_km`) |

---

## Log Format

Log file: `logs/YYYY-MM-DD.log`
Raw API log: `logs/raw/YYYY-MM-DD.jsonl`
Format: one JSON object per line

### Event types

**`startup`**
```json
{"ts": "...", "event": "startup", "gmaps_active": true, "dest_lat": 41.08, "dest_lon": 29.00, "refresh_secs": 120, "eta_adjust_secs": 240}
```

**`poll`** — emitted every cycle, contains full bus data
```json
{"ts": "...", "event": "poll", "stats": {"raw": 3, "cooldown": 1, "pending": 0, "ghosts": 0, "passed": 0, "confirmed": 2},
 "buses": [{"kapino": "O2289", "lat": "41.123", "lon": "29.012", "speed_kmh": "35", "direction": "BOĞAZİÇİ...",
            "guzergah": "59RK_D_D0", "nearest_stop": "113253", "gps_ts": "2026-02-24 14:30:01",
            "gps_age_secs": 7, "dist_to_dest_km": 8.51, "eta": "17 dk", "bearing_deg": 168.7,
            "closest_km": 0.12, "status": "confirmed"}]}
```

**`bus_passed`**
```json
{"ts": "...", "event": "bus_passed", "kapino": "O2289", "lat": 41.085, "lon": 29.003, "cooldown_until": "15:30:00"}
```

**`ghost_filtered`**
```json
{"ts": "...", "event": "ghost_filtered", "kapino": "O1234", "positions": [[41.12, 29.01], ...], "max_move_km": 0.000012}
```

**`api_empty`**
```json
{"ts": "...", "event": "api_empty", "consecutive": 2, "previous_bus_count": 1}
```

**`stale_gps`**
```json
{"ts": "...", "event": "stale_gps", "kapino": "O3007", "gps_age_secs": 387, "gps_ts": "2026-02-24 14:20:01"}
```

**`api_retry`** / **`api_fail`** / **`gmaps_error`**
```json
{"ts": "...", "event": "api_retry", "error": "ConnectionError: ..."}
```

**`cooldown_expired`**
```json
{"ts": "...", "event": "cooldown_expired", "kapino": "O2289"}
```

**`config_changed`**
```json
{"ts": "...", "event": "config_changed", "min_polls": 4, "min_move_km": 0.02}
```

**`shutdown`**
```json
{"ts": "...", "event": "shutdown"}
```

---

## Terminal Table Columns

| Column | Description |
|---|---|
| `Hat` | Bus line (59RK or 59RS) |
| `Kapı` | Bus vehicle door number (unique ID) |
| `Mesafe` | Straight-line distance to 4. Levent (km) |
| `ETA` | Estimated arrival time — traffic-aware if key set, else `[~]`; or "geçti X dk önce" for passed buses |
| `Hız` | Current speed from GPS (km/h) |
| `GPS` | Age of last GPS fix — green (<2 min) / yellow (2–5 min) / red (>5 min, `Xdk!`) |
| `Son konum` | Raw GPS timestamp from the bus |

Footer shows: last update time · next update countdown · passed count · pending / ghost counts.

Active buses appear first. Passed buses appear below in dimmed style with "geçti X dk önce" in the ETA column and disappear after 30 minutes.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No buses shown for >10 min | All buses pending or ghost-filtered | Normal on startup — wait 4 polls (~8 min). Check `--stats` in log viewer. |
| `api_empty` events repeated | IETT API returning empty (line may be inactive) | 59RK/59RS don't run 24/7. Check İETT schedule. |
| ETA shows `[~]` | Google Maps API key not set or failed | Set `GMAPS_API_KEY` in code or `export GMAPS_KEY=...` |
| `gmaps_error` in logs | API quota exceeded or key issue | Check Google Cloud Console for quota usage |
| Bus stuck in `pending` | Bus appeared then API stopped returning it | Ghost or real bus at terminal. Resets if it reappears. |
| GPS column shows red `Xdk!` | GPS hasn't updated in >5 min | Bus may be in a tunnel or GPS hardware issue. |
| Port 8765 already in use | Previous instance still running | `fuser -k 8765/tcp` then restart |
| Buses appear then vanish | Ghost bus failed movement check | Expected. Logged as `ghost_filtered`. |
| Bus flagged as passed too early | Passed zone box too large or misplaced | Adjust `PASSED_BOX` in `map_tracker.py`. The box is drawn on the map for visual verification. |
| 59RS buses show wrong direction | `_G_` may not be correct for Sarıyer→Rumeli Hisar Üstü | Check `yon` field in map popup or raw logs. Update `DIRECTION_FILTERS["59RS"]` if needed. |
| Web page shows 404 | `static/` directory or HTML file missing | Ensure `static/index.html`, `display.html`, `logs.html` exist next to `map_tracker.py`. |
| Log page shows no data | `/logs` or `/logs/raw` returned empty | Log files may not exist yet — run the tracker for at least one poll cycle. |

---

## Known Limitations

1. **~8 minute startup delay** before buses appear (4 polls × 2 min interval) — required for ghost filtering.
2. **Bounding box passed detection** flags any bus whose GPS falls inside the rectangle. GPS drift can trigger a false positive if the bus is near the box edge. Tune `PASSED_BOX` if this happens; the box is drawn on the map for visual verification.
3. **ETA adjustments are empirical** — `ETA_ADJUST_SECS` (−4 min) and `ETA_DIST_SECS_PER_KM` (+50s/km) were tuned manually. Accuracy may vary with time of day and traffic conditions. Adjust both constants at the top of `map_tracker.py`.
4. **Haversine fallback ignores road network** — straight-line distance only, labeled `[~]`. Google Maps API gives accurate routing.
5. **Rate limit:** 2 lines × 30 req/hour = 60 req/hour, well within the 100 req/hour limit at the default 2-minute refresh.
6. **30-min cooldown** means a bus that passes 4. Levent and loops back will be invisible for 30 minutes.
7. **No historical replay** — the tracker only shows current state. Log files (including `logs/raw/`) are the only way to review past positions.
8. **59RS direction code assumed** — `_G_` was selected based on IETT naming convention but has not been verified against live 59RS data. Check the `yon` field in bus popups or the raw API log page to confirm.
9. **Static files must be present** — the `static/` directory and its three HTML files must exist alongside `map_tracker.py`. The server returns 404 if they are missing.
