#!/usr/bin/env python3
"""
Log Viewer — İETT 59RK Bus Tracker

Usage:
  python3 log_viewer.py                        # today's summary + last 30 events
  python3 log_viewer.py --all                  # all events (no limit)
  python3 log_viewer.py --tail                 # live tail of today's log
  python3 log_viewer.py --date 2026-02-24      # a specific date
  python3 log_viewer.py --event poll           # filter by event type
  python3 log_viewer.py --event bus_passed     # only passed events
  python3 log_viewer.py --bus O2289            # position history for one bus
  python3 log_viewer.py --errors               # only errors / warnings
  python3 log_viewer.py --stats                # poll statistics table
  python3 log_viewer.py --raw                  # raw IETT API responses
  python3 log_viewer.py --raw --bus O2289      # raw responses filtered by bus
  python3 log_viewer.py --list                 # list available log files
"""

import argparse
import json
import sys
import time
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

LOG_DIR     = Path(__file__).parent / "logs"
RAW_LOG_DIR = LOG_DIR / "raw"

# ── Event styling ─────────────────────────────────────────────────────────────

EVENT_STYLE = {
    "startup":          ("▶",  "bold green"),
    "shutdown":         ("■",  "bold red"),
    "poll":             ("·",  "cyan"),
    "api_retry":        ("⚠",  "yellow"),
    "api_fail":         ("✗",  "bold red"),
    "api_empty":        ("○",  "yellow"),
    "stale_gps":        ("⏱",  "yellow"),
    "ghost_filtered":   ("☠",  "red"),
    "bus_passed":       ("✓",  "bold blue"),
    "cooldown_expired": ("↺",  "dim blue"),
    "gmaps_error":      ("⚡", "yellow"),
}

ERROR_EVENTS = {"api_retry", "api_fail", "api_empty", "stale_gps", "gmaps_error"}


# ── Log loading ───────────────────────────────────────────────────────────────

def load_log(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def get_log_path(date_str: str | None = None) -> Path | None:
    base = LOG_DIR / f"{date_str or datetime.now().strftime('%Y-%m-%d')}.log"
    if base.exists():
        return base
    logs = sorted(LOG_DIR.glob("*.log"))
    return logs[-1] if logs else None


def get_raw_log_path(date_str: str | None = None) -> Path | None:
    base = RAW_LOG_DIR / f"{date_str or datetime.now().strftime('%Y-%m-%d')}.jsonl"
    if base.exists():
        return base
    files = sorted(RAW_LOG_DIR.glob("*.jsonl"))
    return files[-1] if files else None


# ── Event summary formatter ───────────────────────────────────────────────────

def fmt_event(e: dict) -> str:
    evt = e.get("event", "")
    if evt == "poll":
        s = e.get("stats", {})
        parts = [f"raw={s.get('raw', 0)}"]
        if s.get("cooldown"): parts.append(f"cd={s['cooldown']}")
        if s.get("pending"):  parts.append(f"pend={s['pending']}")
        if s.get("ghosts"):   parts.append(f"ghost={s['ghosts']}")
        if s.get("passed"):   parts.append(f"passed={s['passed']}")
        parts.append(f"ok={s.get('confirmed', 0)}")
        buses = e.get("buses", [])
        if buses:
            bus_str = "  |  " + ", ".join(
                f"[yellow]{b['kapino']}[/yellow] {b.get('dist_to_dest_km','?')}km "
                f"[magenta]{b.get('eta','?')}[/magenta]"
                for b in buses
            )
        else:
            bus_str = ""
        return "  ".join(parts) + bus_str

    elif evt == "bus_passed":
        return (f"[bold yellow]{e.get('kapino')}[/bold yellow]  "
                f"bearing={e.get('bearing_deg')}°  closest={e.get('closest_km')}km  "
                f"cooldown→ {e.get('cooldown_until')}")

    elif evt == "ghost_filtered":
        pts = e.get("positions", [])
        return (f"[bold yellow]{e.get('kapino')}[/bold yellow]  "
                f"max_move={e.get('max_move_km')}km  "
                f"positions={len(pts)}  "
                + (f"last=({pts[-1][0]:.5f},{pts[-1][1]:.5f})" if pts else ""))

    elif evt == "api_empty":
        return f"consecutive={e.get('consecutive')}  kept_prev={e.get('previous_bus_count')}"

    elif evt == "stale_gps":
        return (f"[bold yellow]{e.get('kapino')}[/bold yellow]  "
                f"age={e.get('gps_age_secs')}s  ts={e.get('gps_ts')}")

    elif evt in ("api_retry", "api_fail", "gmaps_error"):
        err = str(e.get("error", ""))
        return ("[red]" + err[:90] + ("…" if len(err) > 90 else "") + "[/red]")

    elif evt == "startup":
        return (f"gmaps={'[green]on[/green]' if e.get('gmaps_active') else '[yellow]off[/yellow]'}  "
                f"refresh={e.get('refresh_secs')}s  "
                f"eta_adj={e.get('eta_adjust_secs')}s")

    elif evt == "cooldown_expired":
        return f"[bold yellow]{e.get('kapino')}[/bold yellow]"

    return ""


# ── Views ─────────────────────────────────────────────────────────────────────

def view_summary(events: list[dict], title: str):
    polls    = [e for e in events if e["event"] == "poll"]
    errors   = [e for e in events if e["event"] in ERROR_EVENTS]
    ghosts   = [e for e in events if e["event"] == "ghost_filtered"]
    passed   = [e for e in events if e["event"] == "bus_passed"]
    starts   = [e for e in events if e["event"] == "startup"]
    empties  = [e for e in events if e["event"] == "api_empty"]

    all_kapinos: set[str] = set()
    for e in polls:
        for b in e.get("buses", []):
            if b.get("kapino"):
                all_kapinos.add(b["kapino"])

    confirmed_total = sum(e.get("stats", {}).get("confirmed", 0) for e in polls)
    avg_confirmed   = confirmed_total / len(polls) if polls else 0

    t = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    t.add_column("k", style="dim",  min_width=26)
    t.add_column("v", style="bold", min_width=30)

    t.add_row("Toplam poll",          str(len(polls)))
    t.add_row("Ortalama gösterilen",  f"{avg_confirmed:.1f} araç/poll")
    t.add_row("Boş yanıt (api_empty)",str(len(empties)))
    t.add_row("Hata / uyarı",
              f"[{'red' if errors else 'green'}]{len(errors)}[/]")
    t.add_row("Hayalet filtrelenen",  str(len(ghosts)))
    t.add_row("Geçen otobüsler",      str(len(passed)))
    t.add_row("Uygulama başlatma",    str(len(starts)))
    t.add_row("Görülen araç ID'leri",
              f"{len(all_kapinos)}  " +
              (f"({', '.join(sorted(all_kapinos))})" if all_kapinos else "—"))

    if passed:
        kapinos_passed = [e.get("kapino") for e in passed]
        t.add_row("Geçen araçlar",
                  ", ".join(f"[blue]{k}[/blue] {e.get('cooldown_until','')}"
                            for e, k in zip(passed, kapinos_passed)))

    console.print(Panel(t,
        title=f"[bold cyan]{title}[/bold cyan]",
        border_style="cyan", padding=(0, 1)))


def view_timeline(events: list[dict], filter_event: str | None = None, limit: int = 30):
    filtered = [e for e in events if not filter_event or e.get("event") == filter_event]
    subset   = filtered[-limit:]

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
              pad_edge=False, show_edge=False)
    t.add_column("Zaman",  min_width=19, style="dim", no_wrap=True)
    t.add_column("Olay",   min_width=18, no_wrap=True)
    t.add_column("Detay")

    for e in subset:
        evt        = e.get("event", "?")
        icon, col  = EVENT_STYLE.get(evt, ("·", "white"))
        t.add_row(
            e.get("ts", ""),
            f"[{col}]{icon} {evt}[/{col}]",
            fmt_event(e),
        )

    hdg = f"Son {min(len(filtered), limit)} / {len(filtered)} olay"
    if filter_event:
        hdg += f"  [dim](filtre: {filter_event})[/dim]"
    console.print(Panel(t, title=f"[bold cyan]{hdg}[/bold cyan]",
                        border_style="cyan", padding=(0, 1)))

    if len(filtered) > limit:
        console.print(
            f"[dim]  {len(filtered) - limit} olay daha var — "
            f"tümünü görmek için --all ekleyin[/dim]"
        )


def view_bus_history(events: list[dict], kapino: str | None = None):
    polls = [e for e in events if e["event"] == "poll"]

    bus_records: dict[str, list[dict]] = {}
    for poll in polls:
        for b in poll.get("buses", []):
            kap = b.get("kapino")
            if not kap or (kapino and kap != kapino):
                continue
            bus_records.setdefault(kap, []).append({**b, "_ts": poll.get("ts", "")})

    if not bus_records:
        console.print("[yellow]Bu araç için onaylanmış kayıt bulunamadı.[/yellow]")
        return

    for kap, records in sorted(bus_records.items()):
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow",
                  pad_edge=False, title=f"Araç [bold yellow]{kap}[/bold yellow]  "
                                       f"({len(records)} gözlem)")
        t.add_column("Zaman",    min_width=19, style="dim", no_wrap=True)
        t.add_column("Konum",    min_width=24)
        t.add_column("Hız",      justify="right", min_width=8)
        t.add_column("Bearing",  justify="right", min_width=8)
        t.add_column("Mesafe",   justify="right", min_width=9)
        t.add_column("ETA",      min_width=14, style="magenta")
        t.add_column("GPS yaşı", justify="right", min_width=9)
        t.add_column("En yakın", justify="right", min_width=10)

        for r in records:
            age = r.get("gps_age_secs")
            if age is None:
                age_s = "[dim]?[/dim]"
            elif age > 300:
                age_s = f"[bold red]{age}s[/bold red]"
            elif age > 120:
                age_s = f"[yellow]{age}s[/yellow]"
            else:
                age_s = f"[green]{age}s[/green]"

            t.add_row(
                r.get("_ts", ""),
                f"{r.get('lat','?')}, {r.get('lon','?')}",
                f"{r.get('speed_kmh','?')} km/h",
                f"{r.get('bearing_deg','?')}°",
                f"{r.get('dist_to_dest_km','?')} km",
                r.get("eta") or "—",
                age_s,
                f"{r.get('closest_km','?')} km",
            )
        console.print(t)


def view_errors(events: list[dict]):
    filtered = [e for e in events if e["event"] in ERROR_EVENTS]
    if not filtered:
        console.print(Panel("[green]Bu günlükte hata / uyarı yok.[/green]",
                            border_style="green"))
        return
    view_timeline(filtered, limit=len(filtered))


def view_stats(events: list[dict]):
    polls = [e for e in events if e["event"] == "poll"]
    if not polls:
        console.print("[yellow]Poll verisi bulunamadı.[/yellow]")
        return

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
              pad_edge=False)
    t.add_column("Zaman",    min_width=19, style="dim", no_wrap=True)
    t.add_column("Ham",      justify="right", min_width=4)
    t.add_column("CD",       justify="right", min_width=4, style="dim")
    t.add_column("Pend",     justify="right", min_width=5, style="yellow")
    t.add_column("Ghost",    justify="right", min_width=6, style="red")
    t.add_column("Geçti",    justify="right", min_width=6, style="blue")
    t.add_column("OK",       justify="right", min_width=4, style="green")
    t.add_column("Araçlar",  min_width=30)

    for poll in polls[-60:]:
        s    = poll.get("stats", {})
        buses = poll.get("buses", [])
        bus_str = (
            "  ".join(
                f"[yellow]{b['kapino']}[/yellow] "
                f"{b.get('dist_to_dest_km','?')}km "
                f"[magenta]{b.get('eta','?')}[/magenta]"
                for b in buses
            ) if buses else "[dim]—[/dim]"
        )
        t.add_row(
            poll.get("ts", ""),
            str(s.get("raw", 0)),
            str(s.get("cooldown", 0)) or "—",
            str(s.get("pending",  0)) or "—",
            str(s.get("ghosts",   0)) or "—",
            str(s.get("passed",   0)) or "—",
            str(s.get("confirmed",0)),
            bus_str,
        )

    console.print(Panel(t, title="[bold cyan]Poll İstatistikleri[/bold cyan]",
                        border_style="cyan", padding=(0, 1)))


# ── Raw API response viewer ───────────────────────────────────────────────────

def view_raw(path: Path, kapino: str | None = None, limit: int = 40):
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if kapino:
        # Filter to entries containing this bus
        entries = [e for e in entries
                   if any(b.get("kapino") == kapino for b in e.get("buses", []))]

    subset = entries[-limit:]

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
              pad_edge=False, show_edge=False)
    t.add_column("Zaman",      min_width=19, style="dim", no_wrap=True)
    t.add_column("Araç",       min_width=7, style="bold yellow", no_wrap=True)
    t.add_column("Enlem",      justify="right", min_width=11)
    t.add_column("Boylam",     justify="right", min_width=11)
    t.add_column("Hız",        justify="right", min_width=9)
    t.add_column("Güzergah",   min_width=14, style="dim")
    t.add_column("Son GPS",    min_width=19, style="dim")
    t.add_column("Yak. Durak", min_width=8, style="dim")

    for entry in subset:
        ts   = entry.get("ts", "")
        buses = entry.get("buses", [])
        for b in buses:
            if kapino and b.get("kapino") != kapino:
                continue
            t.add_row(
                ts,
                b.get("kapino", "?"),
                b.get("enlem",  "?"),
                b.get("boylam", "?"),
                f"{b.get('hiz','?')} km/h",
                b.get("guzergahkodu", "?"),
                b.get("son_konum_zamani", "?"),
                b.get("yakinDurakKodu",  "?"),
            )

    hdg = f"Ham API yanıtları  {path.stem}  ({len(entries)} kayıt)"
    if kapino:
        hdg += f"  [yellow]{kapino}[/yellow]"
    if len(entries) > limit:
        hdg += f"  [dim](son {limit} gösteriliyor)[/dim]"

    console.print(Panel(t, title=f"[bold cyan]{hdg}[/bold cyan]",
                        border_style="cyan", padding=(0, 1)))


# ── Live tail ─────────────────────────────────────────────────────────────────

def live_tail(log_path: Path):
    offset = log_path.stat().st_size  # start at end of file

    console.print(Panel(
        f"[dim]İzleniyor:[/dim] [bold]{log_path.name}[/bold]\n"
        f"[dim]Çıkmak için Ctrl+C[/dim]",
        border_style="cyan", padding=(0, 1),
    ))

    # Show last 10 events as context
    events = load_log(log_path)
    if events:
        view_timeline(events, limit=10)
    console.print("\n[dim]Yeni olaylar bekleniyor...[/dim]\n")

    try:
        while True:
            current_size = log_path.stat().st_size
            if current_size > offset:
                with open(log_path, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    new_text = f.read()
                offset = current_size
                for line in new_text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e   = json.loads(line)
                        evt = e.get("event", "?")
                        icon, col = EVENT_STYLE.get(evt, ("·", "white"))
                        console.print(
                            f"[dim]{e.get('ts','')}[/dim]  "
                            f"[{col}]{icon} {evt:<20}[/{col}]  "
                            f"{fmt_event(e)}"
                        )
                    except json.JSONDecodeError:
                        pass
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[bold red]Çıkılıyor...[/bold red]")


# ── Log list ──────────────────────────────────────────────────────────────────

def view_list():
    event_logs = sorted(LOG_DIR.glob("*.log"), reverse=True)
    raw_logs   = sorted(RAW_LOG_DIR.glob("*.jsonl"), reverse=True) if RAW_LOG_DIR.exists() else []

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("Tarih",   min_width=12)
    t.add_column("Olay log",justify="right", min_width=10)
    t.add_column("Ham log", justify="right", min_width=10)

    dates = sorted(
        {p.stem for p in event_logs} | {p.stem for p in raw_logs},
        reverse=True
    )
    for date in dates:
        ep = LOG_DIR      / f"{date}.log"
        rp = RAW_LOG_DIR  / f"{date}.jsonl"
        e_size = f"{ep.stat().st_size // 1024} KB" if ep.exists() else "—"
        r_size = f"{rp.stat().st_size // 1024} KB" if rp.exists() else "—"
        t.add_row(date, e_size, r_size)

    console.print(Panel(t, title="[bold cyan]Mevcut Log Dosyaları[/bold cyan]",
                        border_style="cyan", padding=(0, 1)))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="İETT 59RK Bus Tracker — Log Viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--date",   metavar="YYYY-MM-DD", help="Belirli bir tarih")
    ap.add_argument("--event",  metavar="TYPE",        help="Olay tipine göre filtrele")
    ap.add_argument("--bus",    metavar="KAPINO",       help="Araç ID'sine göre filtrele")
    ap.add_argument("--errors", action="store_true",   help="Sadece hatalar / uyarılar")
    ap.add_argument("--stats",  action="store_true",   help="Poll istatistikleri tablosu")
    ap.add_argument("--tail",   action="store_true",   help="Canlı takip modu")
    ap.add_argument("--raw",    action="store_true",   help="Ham IETT API yanıtları")
    ap.add_argument("--list",   action="store_true",   help="Mevcut log dosyalarını listele")
    ap.add_argument("--all",    action="store_true",   help="Limit olmadan tüm olayları göster")
    args = ap.parse_args()

    if not LOG_DIR.exists():
        console.print(f"[red]Log dizini bulunamadı:[/red] {LOG_DIR}")
        sys.exit(1)

    if args.list:
        view_list()
        return

    # ── Raw API response log ──────────────────────────────────────────────
    if args.raw:
        raw_path = get_raw_log_path(args.date)
        if not raw_path:
            console.print("[red]Ham API log dosyası bulunamadı.[/red]")
            if RAW_LOG_DIR.exists():
                files = sorted(RAW_LOG_DIR.glob("*.jsonl"))
                if files:
                    console.print("Mevcut:", ", ".join(p.stem for p in files))
            sys.exit(1)
        limit = 999999 if args.all else 40
        view_raw(raw_path, kapino=args.bus, limit=limit)
        return

    # ── Event log ─────────────────────────────────────────────────────────
    log_path = get_log_path(args.date)
    if not log_path:
        console.print("[red]Log dosyası bulunamadı.[/red]")
        logs = sorted(LOG_DIR.glob("*.log"))
        if logs:
            console.print("Mevcut:", ", ".join(p.stem for p in logs))
        sys.exit(1)

    if args.tail:
        live_tail(log_path)
        return

    events = load_log(log_path)
    date_label = log_path.stem
    limit = 999999 if args.all else 30

    if args.bus:
        console.print(Panel.fit(
            f"[bold cyan]{date_label}[/bold cyan]  Araç: [bold yellow]{args.bus}[/bold yellow]",
            border_style="cyan"))
        view_bus_history(events, args.bus)

    elif args.errors:
        console.print(Panel.fit(
            f"[bold cyan]{date_label}[/bold cyan]  Hatalar / Uyarılar",
            border_style="cyan"))
        view_errors(events)

    elif args.stats:
        view_stats(events)

    elif args.event:
        view_timeline(events, filter_event=args.event, limit=limit)

    else:
        # Default: summary + recent timeline
        view_summary(events, f"Özet — {date_label}")
        view_timeline(events, limit=limit)
        if not args.all and len(events) > limit:
            console.print(
                f"\n[dim]  Tüm {len(events)} olayı görmek için:[/dim] "
                f"[bold]python3 log_viewer.py --all[/bold]"
            )
        console.print(
            "\n[dim]  Diğer görünümler:[/dim]  "
            "--stats  --errors  --bus KAPINO  --raw  --tail  --list"
        )


if __name__ == "__main__":
    main()
