#!/usr/bin/env python3
"""
0RESMON Revenue Tracker - Advanced
Survie aux redemarrages, accumulation multi-sessions, stats precises.
"""

import sqlite3
import re
import subprocess
import time
import os
import sys
import hashlib
import signal
import math
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
URL       = "https://0resmon.tebex.io/"
INTERVAL  = 15 * 60   # secondes (15 min) - plus frequent = moins de paiements rates
DATA_DIR  = Path(__file__).parent / "data"
DB_PATH   = DATA_DIR / "payments.db"
LOG_PATH  = DATA_DIR / "tracker.log"
_INSTANCE_LOCK = None


def acquire_daemon_lock() -> bool:
    """Empeche deux trackers de tourner en meme temps sous Windows."""
    global _INSTANCE_LOCK
    if sys.platform != "win32":
        return True
    import msvcrt

    DATA_DIR.mkdir(exist_ok=True)
    handle = open(DATA_DIR / "tracker.lock", "a+b")
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _INSTANCE_LOCK = handle
    return True

# ═══════════════════════════════════════════════════════════════════
#  COULEURS ANSI (Windows Terminal / tout terminal moderne)
# ═══════════════════════════════════════════════════════════════════
# pythonw n'a pas de console. On conserve les sorties terminal pour les commandes
# manuelles, mais le daemon silencieux n'effectue aucun rendu inutile.
HIDDEN_MODE = sys.stdout is None or sys.stderr is None

# Active le support ANSI + UTF-8 sur Windows lorsqu'une console existe.
if sys.platform == "win32" and not HIDDEN_MODE:
    os.system("")
    # Force UTF-8 pour stdout/stderr (evite les UnicodeEncodeError sur cp1252)
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
elif HIDDEN_MODE:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"
RED    = "\033[91m"
GREY   = "\033[90m"

def b(s): return f"{BOLD}{s}{RESET}"
def g(s): return f"{GREEN}{s}{RESET}"
def y(s): return f"{YELLOW}{s}{RESET}"
def c(s): return f"{CYAN}{s}{RESET}"
def r(s): return f"{RED}{s}{RESET}"
def d(s): return f"{DIM}{s}{RESET}"
def w(s): return f"{WHITE}{s}{RESET}"


# ═══════════════════════════════════════════════════════════════════
#  BASE DE DONNEES (SQLite - persiste entre redemarrages)
# ═══════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # meilleure resistance aux crashes
    conn.execute("PRAGMA synchronous=NORMAL")
    # Drop old catalog if schema changed (price → sale_price)
    try:
        conn.execute("SELECT sale_price FROM catalog LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("DROP TABLE IF EXISTS catalog")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS payments (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            amount      REAL NOT NULL,
            currency    TEXT NOT NULL,
            payment_dt  TEXT NOT NULL,   -- datetime absolu ISO
            time_label  TEXT,            -- label original site
            scraped_at  TEXT NOT NULL    -- quand on l'a scrapé
        );

        CREATE TABLE IF NOT EXISTS scrape_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            last_scrape_at  TEXT,
            total_scrapes   INTEGER DEFAULT 0,
            total_new       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS catalog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            sale_price  REAL NOT NULL,
            orig_price  REAL,
            pkg_id      TEXT,
            category    TEXT,
            updated_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_payment_dt ON payments(payment_dt);
        CREATE INDEX IF NOT EXISTS idx_scraped_at ON payments(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_catalog_sale ON catalog(sale_price);
        CREATE INDEX IF NOT EXISTS idx_catalog_orig ON catalog(orig_price);
    """)
    # Migrations
    try:
        conn.execute("ALTER TABLE payments ADD COLUMN package_guess TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════════
#  RESOLUTION TIMESTAMP ABSOLU
# ═══════════════════════════════════════════════════════════════════

def resolve_dt(label: str, scrape_dt: datetime) -> Optional[datetime]:
    """
    Convertit un label relatif du site en datetime absolu.
    Formats observes: "Today HH:MM", "Yesterday HH:MM", "DD/MM HH:MM"
    """
    label = label.strip()

    # "Today HH:MM" ou "Today HH:MM:SS"
    m = re.match(r"[Tt]oday\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", label)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        dt = scrape_dt.replace(hour=h, minute=mi, second=0, microsecond=0)
        # Si l'heure depasse le scrape de >1h → c'est hier (timezone offset)
        if dt > scrape_dt + timedelta(hours=1):
            dt -= timedelta(days=1)
        return dt

    # "Yesterday HH:MM"
    m = re.match(r"[Yy]esterday\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", label)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        yesterday = scrape_dt - timedelta(days=1)
        return yesterday.replace(hour=h, minute=mi, second=0, microsecond=0)

    # "DD/MM HH:MM" ou "MM/DD HH:MM"
    m = re.match(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", label)
    if m:
        d1, d2, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        year = scrape_dt.year
        try:
            return datetime(year, d2, d1, h, mi)  # DD/MM
        except ValueError:
            try:
                return datetime(year, d1, d2, h, mi)  # MM/DD
            except ValueError:
                pass

    # "X minutes ago", "X hours ago" (parfois affiche)
    m = re.match(r"(\d+)\s*min", label, re.IGNORECASE)
    if m:
        return scrape_dt - timedelta(minutes=int(m.group(1)))

    m = re.match(r"(\d+)\s*hour", label, re.IGNORECASE)
    if m:
        return scrape_dt - timedelta(hours=int(m.group(1)))

    return None


# ═══════════════════════════════════════════════════════════════════
#  CATALOGUE & MATCHING
# ═══════════════════════════════════════════════════════════════════

CATEGORY_URLS = [
    ("escrow", "/category/escrow-scripts"),
    ("open-source", "/category/open-source"),
    ("map", "/category/map"),
]

def scrape_catalog(conn: sqlite3.Connection):
    """Scrape toutes les categories du store pour construire le catalogue prix→script.
    Capture le prix de vente (promo) ET le prix original (barre)."""
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = 0

    for cat_name, cat_path in CATEGORY_URLS:
        try:
            result = subprocess.run(
                ["curl", "-s", "--compressed", "--max-time", "20",
                 "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                 f"{URL.rstrip('/')}{cat_path}"],
                capture_output=True, timeout=30
            )
            html = result.stdout.decode("utf-8", errors="replace")
        except Exception:
            continue

        articles = re.findall(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
        for art in articles:
            name_m = re.search(r'package-name">(.*?)</h[23]>', art, re.DOTALL)
            # Prix de vente (actuel, en gras)
            sale_m = re.search(r'text-2xl font-bold text-white">.*?([\d.]+)', art, re.DOTALL)
            # Prix original (barre, dans <del>)
            orig_m = re.search(r'<del[^>]*>.*?([\d.]+)', art, re.DOTALL)
            id_m = re.search(r'checkout/packages/add/(\d+)', art)

            if name_m and sale_m:
                name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip()
                sale_price = float(sale_m.group(1))
                orig_price = float(orig_m.group(1)) if orig_m else None
                pkg_id = id_m.group(1) if id_m else None

                # Upsert par nom
                existing = conn.execute(
                    "SELECT id FROM catalog WHERE name = ?", (name,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE catalog SET sale_price=?, orig_price=?, pkg_id=?, category=?, updated_at=? WHERE id=?",
                        (sale_price, orig_price, pkg_id, cat_name, now_s, existing["id"])
                    )
                else:
                    conn.execute(
                        "INSERT INTO catalog (name, sale_price, orig_price, pkg_id, category, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (name, sale_price, orig_price, pkg_id, cat_name, now_s)
                    )
                total += 1

    conn.commit()
    log(f"Catalogue mis a jour: {total} packages")
    return total


def guess_package(amount: float, conn: sqlite3.Connection) -> Optional[str]:
    """Devine le package achete en matchant le prix.
    Strategies (en ordre de fiabilite):
    1. Match exact sur prix de vente
    2. Match exact sur prix original (sans promo)
    3. Match avec code promo courant (5%, 10%, 15%, 20%, 25%, 30%)
    4. Match avec conversion devise (USD/GBP→EUR)
    5. Match devise + promo combine
    """
    all_pkgs = conn.execute(
        "SELECT name, sale_price, orig_price FROM catalog"
    ).fetchall()

    if not all_pkgs:
        return None

    # Build lookup: prix → [noms]
    price_to_names: dict[float, list[str]] = defaultdict(list)
    for p in all_pkgs:
        price_to_names[p["sale_price"]].append(p["name"])
        if p["orig_price"]:
            price_to_names[p["orig_price"]].append(p["name"])

    # 1. Match exact (sale ou orig)
    if amount in price_to_names:
        names = price_to_names[amount]
        if len(names) == 1:
            return names[0]

    # 2. Match avec code promo (montant = prix * (1 - discount))
    # On inverse : pour chaque discount, le prix catalogue serait amount / (1 - discount)
    PROMO_DISCOUNTS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
    for disc in PROMO_DISCOUNTS:
        undiscounted = round(amount / (1 - disc), 2)
        matches = [n for price, names in price_to_names.items()
                   for n in names if abs(price - undiscounted) < 0.50]
        # Deduplicate
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            return f"~{matches[0]}"

    # 3. Conversion devise (USD→EUR ~1.08-1.12, GBP→EUR ~0.85-0.87)
    CURRENCY_RATIOS = [1.08, 1.10, 1.12, 0.85, 0.87]
    for ratio in CURRENCY_RATIOS:
        converted = round(amount / ratio, 2)
        matches = [n for price, names in price_to_names.items()
                   for n in names if abs(price - converted) < 0.50]
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            return f"~{matches[0]}"

    # 4. Devise + promo combine
    for ratio in CURRENCY_RATIOS:
        for disc in PROMO_DISCOUNTS:
            undiscounted = round(amount / ratio / (1 - disc), 2)
            matches = [n for price, names in price_to_names.items()
                       for n in names if abs(price - undiscounted) < 0.50]
            matches = list(dict.fromkeys(matches))
            if len(matches) == 1:
                return f"~{matches[0]}"

    # 5. Tolerance large (~3%) sur prix de vente seulement
    close = []
    for p in all_pkgs:
        if abs(p["sale_price"] - amount) < amount * 0.03:
            close.append(p["name"])
    close = list(dict.fromkeys(close))
    if len(close) == 1:
        return f"~{close[0]}"

    # Ambigu
    if amount in price_to_names and len(price_to_names[amount]) > 1:
        return f"?({len(price_to_names[amount])} scripts)"

    return None


def update_package_guesses(conn: sqlite3.Connection, force_all: bool = False):
    """Met a jour les package_guess. force_all=True re-evalue tout."""
    if force_all:
        conn.execute("UPDATE payments SET package_guess = NULL")
        conn.commit()
    rows = conn.execute(
        "SELECT id, amount FROM payments WHERE package_guess IS NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        guess = guess_package(row["amount"], conn)
        if guess:
            conn.execute(
                "UPDATE payments SET package_guess = ? WHERE id = ?",
                (guess, row["id"])
            )
            updated += 1
    conn.commit()
    return updated


def make_id(username: str, amount: float, payment_dt: datetime) -> str:
    key = f"{username.lower()}|{amount:.2f}|{payment_dt.strftime('%Y-%m-%d %H:%M')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════
#  SCRAPING
# ═══════════════════════════════════════════════════════════════════

def fetch_html() -> str:
    result = subprocess.run(
        ["curl", "-s", "--compressed", "--max-time", "30",
         "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
         "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "-H", "Accept-Language: fr-FR,fr;q=0.9,en-US;q=0.8",
         URL],
        capture_output=True, text=True, timeout=40
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"curl echec (code {result.returncode}): {result.stderr[:200]}")
    return result.stdout


def parse_payments(html: str, scrape_dt: datetime) -> list[dict]:
    usernames = re.findall(r'alt="(\w+) Customer Profile Photo"', html)
    times     = re.findall(
        r'text-white/25 font-normal text-sm line-clamp-1">\s*(.*?)\s*</p>',
        html, re.DOTALL
    )
    prices    = re.findall(r'package-price">\s*([\d.]+)(EUR|USD|GBP)\s*</p>', html, re.DOTALL)

    results = []
    for i in range(min(len(usernames), len(times), len(prices))):
        label   = times[i].strip()
        amount  = float(prices[i][0])
        currency= prices[i][1]
        pdt     = resolve_dt(label, scrape_dt)
        if pdt is None:
            pdt = scrape_dt   # fallback
        pid = make_id(usernames[i], amount, pdt)
        results.append({
            "id":         pid,
            "username":   usernames[i],
            "amount":     amount,
            "currency":   currency,
            "payment_dt": pdt.strftime("%Y-%m-%d %H:%M:%S"),
            "time_label": label,
            "scraped_at": scrape_dt.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return results


# ═══════════════════════════════════════════════════════════════════
#  STATISTIQUES
# ═══════════════════════════════════════════════════════════════════

def compute_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT amount, currency, payment_dt, scraped_at FROM payments ORDER BY payment_dt"
    ).fetchall()

    if not rows:
        return {}

    # Groupe par jour (calendar day de payment_dt)
    by_day: dict[str, float] = defaultdict(float)
    by_day_count: dict[str, int] = defaultdict(int)
    by_hour: dict[int, list] = defaultdict(list)
    total = 0.0

    for row in rows:
        try:
            pdt = datetime.fromisoformat(row["payment_dt"])
        except Exception:
            continue
        day_key = pdt.strftime("%Y-%m-%d")
        by_day[day_key]    += row["amount"]
        by_day_count[day_key] += 1
        by_hour[pdt.hour].append(row["amount"])
        total += row["amount"]

    calendar_days = sorted(by_day.keys())
    num_cal_days  = len(calendar_days) or 1

    # ── Couverture par jour : combien de scrapes ont couvert chaque jour ──
    scrapes_by_day: dict[str, int] = defaultdict(int)
    session_rows = conn.execute(
        "SELECT started_at, last_scrape_at, total_scrapes FROM scrape_sessions "
        "WHERE last_scrape_at IS NOT NULL"
    ).fetchall()
    for s in session_rows:
        # Repartit les scrapes de la session sur les jours couverts
        try:
            s_start = datetime.fromisoformat(s["started_at"])
            s_end   = datetime.fromisoformat(s["last_scrape_at"])
        except Exception:
            continue
        n_scrapes = s["total_scrapes"] or 1
        day = s_start.date()
        end_day = s_end.date()
        while day <= end_day:
            scrapes_by_day[day.strftime("%Y-%m-%d")] += n_scrapes if day == s_start.date() == end_day else 1
            day += timedelta(days=1)

    # ── Classification des jours ──
    # "Bien couvert" = jour avec >= 5 scrapes (le tracker a tourne un moment)
    # Ces jours donnent l'estimation la plus fiable du revenu journalier
    well_covered = [d for d in calendar_days if scrapes_by_day.get(d, 0) >= 5]

    # ── Couverture horaire par jour (quelles heures on a scrape) ──
    # Utilise scraped_at pour savoir quand on etait actif
    scrape_hours_by_day: dict[str, set] = defaultdict(set)
    for row in rows:
        try:
            sdt = datetime.fromisoformat(row["scraped_at"])
        except Exception:
            continue
        scrape_hours_by_day[sdt.strftime("%Y-%m-%d")].add(sdt.hour)

    # ── Facteur de correction ──
    # Sur les jours bien couverts, on calcule le taux par heure couverte
    # puis on extrapole a 24h pour estimer le vrai revenu journalier
    correction_factor = 1.0
    if well_covered:
        raw_avg = sum(by_day[d] for d in well_covered) / len(well_covered)
        # Heures moyennes couvertes sur les bons jours
        avg_hours_covered = sum(len(scrape_hours_by_day.get(d, set())) for d in well_covered) / len(well_covered)
        if avg_hours_covered >= 2:
            correction_factor = 24.0 / avg_hours_covered
            avg_per_day = raw_avg * correction_factor
            estimate_source = f"{len(well_covered)}j couverts x{correction_factor:.1f} (corrige {avg_hours_covered:.0f}h→24h)"
        else:
            avg_per_day = raw_avg
            estimate_source = f"{len(well_covered)} jours bien couverts"
    else:
        # Fallback : moyenne sur tous les jours avec data
        avg_per_day = total / num_cal_days
        # Appliquer correction si on a des infos de couverture
        all_hours = [len(scrape_hours_by_day.get(d, set())) for d in calendar_days]
        avg_h = sum(all_hours) / max(len(all_hours), 1)
        if avg_h >= 2:
            correction_factor = 24.0 / avg_h
            avg_per_day *= correction_factor
            estimate_source = f"{num_cal_days}j data x{correction_factor:.1f} (corrige {avg_h:.0f}h→24h)"
        else:
            estimate_source = f"{num_cal_days} jours avec data"

    avg_per_hour = avg_per_day / 24

    # Duree totale pour info (mais PAS utilisee pour les moyennes)
    first_scrape = conn.execute("SELECT MIN(started_at) FROM scrape_sessions WHERE last_scrape_at IS NOT NULL").fetchone()[0]
    last_scrape  = conn.execute("SELECT MAX(last_scrape_at) FROM scrape_sessions").fetchone()[0]
    hours_tracked = 0.0
    days_span = 1.0
    if first_scrape and last_scrape:
        try:
            delta = datetime.fromisoformat(last_scrape) - datetime.fromisoformat(first_scrape)
            hours_tracked = max(delta.total_seconds() / 3600, 0)
            days_span = max(delta.total_seconds() / 86400, 1.0)
        except Exception:
            pass

    # Heure de pointe
    peak_hour = max(by_hour, key=lambda h: sum(by_hour[h]), default=None)
    peak_amt  = sum(by_hour[peak_hour]) if peak_hour is not None else 0

    # Confiance : basee sur le nombre de jours bien couverts
    # 100% apres 7 jours bien couverts, bonus pour le nombre total de jours
    if well_covered:
        confidence = min(100, int(len(well_covered) / 7 * 80) + min(20, num_cal_days * 2))
    else:
        confidence = min(50, num_cal_days * 5)  # Plafonné a 50% sans bonne couverture

    # Progression (compare premiere moitie vs deuxieme moitie des jours)
    trend_pct = None
    if len(calendar_days) >= 4:
        mid = len(calendar_days) // 2
        first_half  = sum(by_day[d] for d in calendar_days[:mid]) / max(mid, 1)
        second_half = sum(by_day[d] for d in calendar_days[mid:]) / max(len(calendar_days) - mid, 1)
        if first_half > 0:
            trend_pct = ((second_half - first_half) / first_half) * 100

    # Dernier jour complet vs moy
    last_day_rev = by_day.get(calendar_days[-1], 0) if calendar_days else 0

    # Heatmap data : revenu par heure (aggrege toutes les donnees)
    heatmap = {}
    for h in range(24):
        amounts = by_hour.get(h, [])
        heatmap[h] = {"count": len(amounts), "total": sum(amounts)}

    return {
        "total_payments":    len(rows),
        "total_revenue":     round(total, 2),
        "days_tracked":      round(days_span, 2),
        "hours_tracked":     round(hours_tracked, 1),
        "calendar_days":     num_cal_days,
        "well_covered_days": len(well_covered),
        "correction_factor": round(correction_factor, 2),
        "estimate_source":   estimate_source,
        "by_day":            dict(by_day),
        "scrapes_by_day":    dict(scrapes_by_day),
        "avg_per_day":       round(avg_per_day, 2),
        "avg_per_hour":      round(avg_per_hour, 2),
        "est_per_month":     round(avg_per_day * 30, 2),
        "est_per_year":      round(avg_per_day * 365, 2),
        "peak_hour":         peak_hour,
        "peak_hour_total":   round(peak_amt, 2),
        "heatmap":           heatmap,
        "confidence_pct":    confidence,
        "trend_pct":         round(trend_pct, 1) if trend_pct is not None else None,
        "last_day_revenue":  round(last_day_rev, 2),
        "last_day_label":    calendar_days[-1] if calendar_days else "-",
    }


# ═══════════════════════════════════════════════════════════════════
#  AFFICHAGE
# ═══════════════════════════════════════════════════════════════════

def line(char="─", width=58):
    return char * width

def box_top(width=58):    return "┌" + "─" * (width-2) + "┐"
def box_bot(width=58):    return "└" + "─" * (width-2) + "┘"
def box_sep(width=58):    return "├" + "─" * (width-2) + "┤"
def box_row(text, width=58):
    inner = width - 4
    return f"│ {text:<{inner}} │"

def box_row_kv(key, val, width=58):
    inner = width - 4
    key_w = 28
    val_w = inner - key_w - 2
    k = f"{CYAN}{key}{RESET}"
    v = f"{WHITE}{val}{RESET}"
    raw_key = key
    raw_val = str(val)
    # Calcule le padding en chars réels (pas ANSI)
    pad_k = key_w - len(raw_key)
    pad_v = val_w - len(raw_val)
    return f"│ {k}{' '*max(pad_k,0)}  {v}{' '*max(pad_v,0)} │"

def print_header(total_payments: int, first_seen: Optional[str]):
    W = 58
    print()
    print(b(f"{BLUE}{box_top(W)}{RESET}"))
    title = "0RESMON REVENUE TRACKER"
    pad   = (W - 4 - len(title)) // 2
    print(b(f"{BLUE}│{RESET} {' '*pad}{BOLD}{WHITE}{title}{RESET}{' '*(W-4-len(title)-pad)} {BLUE}│{RESET}"))
    since = f"Donnees depuis : {first_seen or 'maintenant'}"
    pad2  = (W - 4 - len(since)) // 2
    print(f"{BLUE}│{RESET} {' '*pad2}{DIM}{since}{RESET}{' '*(W-4-len(since)-pad2)} {BLUE}│{RESET}")
    count = f"{total_payments} paiements stockes en base"
    pad3  = (W - 4 - len(count)) // 2
    print(f"{BLUE}│{RESET} {' '*pad3}{GREEN}{count}{RESET}{' '*(W-4-len(count)-pad3)} {BLUE}│{RESET}")
    print(b(f"{BLUE}{box_bot(W)}{RESET}"))
    print()


def print_stats(stats: dict, label="STATS CUMULEES"):
    if not stats:
        print(d("  Pas encore assez de donnees."))
        return
    W = 58
    conf   = stats["confidence_pct"]
    conf_c = GREEN if conf >= 70 else YELLOW if conf >= 30 else RED
    trend  = stats.get("trend_pct")

    print(f"  {BOLD}{WHITE}{label}{RESET}")
    print(f"  {line('─', W-4)}")

    wc = stats.get("well_covered_days", 0)
    src = stats.get("estimate_source", "?")
    cf = stats.get("correction_factor", 1.0)
    coverage_label = f"{stats['calendar_days']}j data ({wc} bien couverts)"
    rows_kv = [
        ("Paiements traces",        str(stats["total_payments"])),
        ("Revenu total collecte",   f"{stats['total_revenue']:,.2f} EUR"),
        ("Couverture",              coverage_label),
        ("Methode estimation",      src),
        ("Moy / jour (estimee)",    f"{stats['avg_per_day']:,.2f} EUR"),
        ("Moy / heure",             f"{stats['avg_per_hour']:,.2f} EUR"),
        ("Estimation / mois",       f"{stats['est_per_month']:,.2f} EUR"),
        ("Estimation / an",         f"{stats['est_per_year']:,.0f} EUR"),
    ]
    for k, v in rows_kv:
        pad_k = 28 - len(k)
        pad_v = 18 - len(v)
        print(f"  {CYAN}{k}{RESET}{' '*max(pad_k,1)} {WHITE}{v}{RESET}")

    if stats.get("peak_hour") is not None:
        ph = stats["peak_hour"]
        print(f"  {'Heure de pointe':<28} {WHITE}{ph:02d}h-{(ph+1)%24:02d}h ({stats['peak_hour_total']:.2f} EUR total){RESET}")

    if stats.get("last_day_label") and stats["last_day_label"] != "-":
        print(f"  {'Dernier jour complet':<28} {WHITE}{stats['last_day_label']} → {stats['last_day_revenue']:.2f} EUR{RESET}")

    if trend is not None:
        arrow = "+" if trend >= 0 else ""
        tc    = GREEN if trend >= 0 else RED
        print(f"  {'Tendance':<28} {tc}{arrow}{trend:.1f}% vs debut{RESET}")

    conf_bar = "[" + "#" * (conf // 10) + "." * (10 - conf // 10) + "]"
    print(f"  {'Confiance estimation':<28} {conf_c}{conf_bar} {conf}%{RESET}")
    print(f"  {DIM}(lance le tracker plus souvent pour + de precision){RESET}")
    print()


def print_day_breakdown(stats: dict):
    by_day = stats.get("by_day", {})
    if len(by_day) < 2:
        return
    scrapes_by_day = stats.get("scrapes_by_day", {})
    print(f"  {BOLD}{WHITE}DETAIL PAR JOUR{RESET}")
    print(f"  {line('─', 54)}")
    avg = stats["avg_per_day"]
    for day in sorted(by_day.keys()):
        amt     = by_day[day]
        bar_len = min(int(amt / max(avg, 1) * 20), 40)
        bar     = "#" * bar_len
        n_scrapes = scrapes_by_day.get(day, 0)
        coverage  = f"{GREEN}*{RESET}" if n_scrapes >= 5 else f"{YELLOW}~{RESET}"
        print(f"  {coverage} {DIM}{day}{RESET}  {WHITE}{amt:>8.2f} EUR{RESET}  {GREY}{bar:<20}{RESET}  {DIM}{n_scrapes} scrapes{RESET}")
    print(f"  {DIM}  * = bien couvert (5+ scrapes)  ~ = partiel{RESET}")
    print()


def print_heatmap(stats: dict):
    """Affiche une heatmap 24h des ventes."""
    heatmap = stats.get("heatmap", {})
    if not heatmap or all(v["count"] == 0 for v in heatmap.values()):
        return

    print(f"  {BOLD}{WHITE}HEATMAP HORAIRE{RESET}")
    print(f"  {line('─', 54)}")

    max_count = max(v["count"] for v in heatmap.values()) or 1
    max_rev   = max(v["total"] for v in heatmap.values()) or 1

    # Blocs visuels du plus faible au plus fort
    BLOCKS = " ░▒▓█"

    for h in range(24):
        data = heatmap.get(h, {"count": 0, "total": 0})
        cnt = data["count"]
        rev = data["total"]

        # Intensite basee sur le revenu
        intensity = min(4, int(rev / max_rev * 4.99)) if rev > 0 else 0
        block = BLOCKS[intensity]

        # Barre proportionnelle
        bar_len = int(cnt / max_count * 20) if cnt > 0 else 0
        bar = block * bar_len

        # Couleur selon intensite
        if intensity >= 3:
            col = GREEN
        elif intensity >= 2:
            col = YELLOW
        elif intensity >= 1:
            col = GREY
        else:
            col = DIM

        rev_str = f"{rev:>7.0f}EUR" if rev > 0 else "       -"
        cnt_str = f"{cnt:>3}" if cnt > 0 else "  -"
        print(f"  {DIM}{h:02d}h{RESET} {col}{bar:<20}{RESET}  {cnt_str} ventes  {WHITE}{rev_str}{RESET}")

    print(f"  {DIM}(donnees aggregees sur {stats['calendar_days']} jours){RESET}")
    print()


def print_top_scripts(conn: sqlite3.Connection):
    """Affiche les scripts les plus vendus (basé sur package_guess)."""
    rows = conn.execute("""
        SELECT package_guess, COUNT(*) as cnt, SUM(amount) as total
        FROM payments
        WHERE package_guess IS NOT NULL AND package_guess NOT LIKE '?%'
        GROUP BY package_guess
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    if not rows:
        return

    print(f"  {BOLD}{WHITE}TOP SCRIPTS VENDUS{RESET}")
    print(f"  {line('─', 54)}")
    for row in rows:
        name = row["package_guess"]
        if name.startswith("~"):
            name = name[1:]  # retire le ~ pour l'affichage
        name_short = name[:40]
        print(f"  {WHITE}{row['cnt']:>3}x{RESET}  {CYAN}{name_short:<40}{RESET}  {WHITE}{row['total']:>8.2f} EUR{RESET}")

    # Stats de matching
    total = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    matched = conn.execute(
        "SELECT COUNT(*) FROM payments WHERE package_guess IS NOT NULL"
    ).fetchone()[0]
    pct = int(matched / max(total, 1) * 100)
    print(f"  {DIM}{matched}/{total} paiements identifies ({pct}%){RESET}")
    print()


def print_scrape_result(new_payments: list[dict], scrape_num: int):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n  {DIM}{line('─', 54)}{RESET}")
    print(f"  {GREY}[{now}]{RESET}  Scrape #{scrape_num}  │  "
          f"{GREEN}{len(new_payments)} nouveaux{RESET}")

    if new_payments:
        for p in new_payments:
            amt_c = YELLOW if p["amount"] >= 100 else WHITE
            pkg = p.get("package_guess")
            pkg_label = ""
            if pkg:
                pkg_short = pkg.lstrip("~")[:35]
                pkg_label = f"\n      {DIM}└ {pkg_short}{RESET}"
            print(f"    {GREEN}+{RESET} {p['username']:<22} "
                  f"{amt_c}{p['amount']:>8.2f} {p['currency']}{RESET}  "
                  f"{DIM}{p['time_label']}{RESET}{pkg_label}")
    else:
        print(f"    {DIM}Pas de nouveau paiement (normal, site affiche ~5 recents){RESET}")


def print_countdown(seconds_left: int):
    m, s = divmod(seconds_left, 60)
    bar_total = 30
    elapsed_pct = 1 - (seconds_left / INTERVAL)
    bar_filled  = int(elapsed_pct * bar_total)
    bar = f"{GREEN}{'█' * bar_filled}{GREY}{'░' * (bar_total - bar_filled)}{RESET}"
    msg = f"\r  {DIM}Prochain scrape dans {m:02d}:{s:02d}  {bar}{RESET}  "
    sys.stdout.write(msg)
    sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line_txt = f"[{ts}] {msg}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line_txt)


# ═══════════════════════════════════════════════════════════════════
#  LOGIQUE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════

scrape_count = 0
session_id   = None

def do_scrape(conn: sqlite3.Connection) -> list[dict]:
    global scrape_count, session_id
    scrape_dt = datetime.now()

    # --- Fetch ---
    html = fetch_html()
    payments = parse_payments(html, scrape_dt)

    # --- Deduplication & insertion ---
    new_payments = []
    for p in payments:
        existing = conn.execute(
            "SELECT id FROM payments WHERE id = ?", (p["id"],)
        ).fetchone()
        if not existing:
            # Guess package name from catalog
            pkg_guess = guess_package(p["amount"], conn)
            conn.execute("""
                INSERT INTO payments (id, username, amount, currency, payment_dt, time_label, scraped_at, package_guess)
                VALUES (:id, :username, :amount, :currency, :payment_dt, :time_label, :scraped_at, :package_guess)
            """, {**p, "package_guess": pkg_guess})
            p["package_guess"] = pkg_guess
            new_payments.append(p)
            pkg_label = f"  [{pkg_guess[:30]}]" if pkg_guess else ""
            log(f"NEW {p['username']:<20} {p['amount']:>8.2f} {p['currency']}  ({p['time_label']}){pkg_label}")

    conn.commit()

    # --- Update session ---
    scrape_count += 1
    now_s = scrape_dt.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE scrape_sessions
        SET last_scrape_at = ?, total_scrapes = total_scrapes + 1, total_new = total_new + ?
        WHERE id = ?
    """, (now_s, len(new_payments), session_id))
    conn.commit()

    log(f"Scrape #{scrape_count} | trouves={len(payments)} nouveaux={len(new_payments)}")
    return new_payments


def wait_with_countdown(seconds: int, conn: sqlite3.Connection, stats_refresh_every=5):
    """Attend en affichant une progress bar. Rafraichit les stats toutes les X min."""
    if HIDDEN_MODE:
        # En tache de fond, un seul sommeil remplace 900 reveils et rendus / 15 min.
        time.sleep(seconds)
        return

    start = time.time()
    refreshed_stats = False
    try:
        while True:
            elapsed  = time.time() - start
            remaining = max(0, seconds - int(elapsed))
            print_countdown(remaining)

            if remaining == 0:
                break

            # Rafraichit les stats a mi-chemin
            if elapsed >= seconds * 0.5 and not refreshed_stats:
                refreshed_stats = True

            time.sleep(1)
    except KeyboardInterrupt:
        raise


def run(interval: int = INTERVAL):
    global session_id

    # Init DB
    conn = init_db()

    # Cree une nouvelle session
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO scrape_sessions (started_at) VALUES (?)", (now_s,)
    )
    session_id = cur.lastrowid
    conn.commit()

    # Infos historiques
    total_existing = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    first_seen_row = conn.execute("SELECT MIN(scraped_at) FROM payments").fetchone()[0]

    # ── Affichage initial ──────────────────────────────────────────
    if not HIDDEN_MODE:
        os.system("cls" if sys.platform == "win32" else "clear")
    print_header(total_existing, first_seen_row)

    # ── Mise a jour du catalogue ──────────────────────────────────
    print(f"  {DIM}Mise a jour du catalogue...{RESET}", end=" ", flush=True)
    try:
        n_cat = scrape_catalog(conn)
        n_guess = update_package_guesses(conn, force_all=True)
        print(f"{GREEN}{n_cat} packages{RESET}", end="")
        if n_guess:
            print(f"  {GREEN}+{n_guess} paiements identifies{RESET}")
        else:
            print()
    except Exception as e:
        print(f"{YELLOW}erreur: {e}{RESET}")

    if total_existing > 0:
        stats = compute_stats(conn)
        print_stats(stats, "HISTORIQUE CHARGE")
        print_day_breakdown(stats)
        print_heatmap(stats)
        print_top_scripts(conn)

    print(f"  {GREEN}Tracker demarre.{RESET}  Intervalle : {interval//60} min")
    print(f"  Donnees : {DB_PATH}")
    print(f"  Log     : {LOG_PATH}")
    print(f"  {DIM}Ctrl+C pour arreter et afficher le rapport final.{RESET}\n")
    log(f"=== SESSION {session_id} DEMARREE ===")

    # ── Boucle principale ──────────────────────────────────────────
    retries = 0
    while True:
        try:
            new = do_scrape(conn)
            retries = 0

            stats = compute_stats(conn)
            print_scrape_result(new, scrape_count)
            print()
            print_stats(stats)

            if scrape_count % 5 == 0 and stats.get("calendar_days", 0) >= 2:
                print_day_breakdown(stats)

            wait_with_countdown(interval, conn)
            print()  # newline apres la progress bar

        except KeyboardInterrupt:
            print("\n")
            print(f"  {YELLOW}Arret demande.{RESET}\n")
            break

        except Exception as e:
            retries += 1
            wait_s = min(60 * retries, 300)  # backoff exponentiel, max 5min
            log(f"ERREUR scrape: {e}")
            print(f"\n  {RED}Erreur scrape #{scrape_count + 1}: {e}{RESET}")
            print(f"  {YELLOW}Retry dans {wait_s}s (tentative {retries})...{RESET}")
            try:
                time.sleep(wait_s)
            except KeyboardInterrupt:
                print("\n")
                break

    # ── Rapport final ──────────────────────────────────────────────
    stats = compute_stats(conn)
    total_now = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    new_this_session = conn.execute(
        "SELECT total_new FROM scrape_sessions WHERE id = ?", (session_id,)
    ).fetchone()[0] or 0

    print(f"\n  {'═'*54}")
    print(f"  {BOLD}{WHITE}RAPPORT FINAL DE SESSION{RESET}")
    print(f"  {'═'*54}")
    print(f"  Scrapes effectues       {scrape_count}")
    print(f"  Nouveaux cette session  {new_this_session}")
    print(f"  Total en base           {total_now}")
    print()
    print_stats(stats, "ESTIMATION FINALE")
    print_day_breakdown(stats)
    print_heatmap(stats)
    print_top_scripts(conn)
    log(f"=== SESSION {session_id} TERMINEE | scrapes={scrape_count} nouveaux={new_this_session} ===")
    conn.close()


# ═══════════════════════════════════════════════════════════════════
#  COMMANDES UTILITAIRES
# ═══════════════════════════════════════════════════════════════════

def cmd_report():
    """Affiche juste le rapport sans scraper."""
    conn = init_db()
    total = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    first = conn.execute("SELECT MIN(scraped_at) FROM payments").fetchone()[0]
    print_header(total, first)

    # Mise a jour catalogue
    print(f"  {DIM}Mise a jour du catalogue...{RESET}", end=" ", flush=True)
    try:
        n_cat = scrape_catalog(conn)
        n_guess = update_package_guesses(conn, force_all=True)
        print(f"{GREEN}{n_cat} packages, {n_guess} nouveaux matchs{RESET}")
    except Exception as e:
        print(f"{YELLOW}erreur: {e}{RESET}")

    stats = compute_stats(conn)
    if stats:
        print_stats(stats, "RAPPORT COMPLET")
        print_day_breakdown(stats)
        print_heatmap(stats)
        print_top_scripts(conn)

        # Export CSV
        csv_path = DATA_DIR / "payments_export.csv"
        rows = conn.execute(
            "SELECT username, amount, currency, payment_dt, time_label, scraped_at, package_guess "
            "FROM payments ORDER BY payment_dt"
        ).fetchall()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            import csv as csv_mod
            w2 = csv_mod.writer(f)
            w2.writerow(["username", "amount", "currency", "payment_dt", "time_label", "scraped_at", "package_guess"])
            for row in rows:
                w2.writerow(list(row))
        print(f"  {GREEN}CSV exporte : {csv_path}{RESET}")
    else:
        print(f"  {YELLOW}Aucune donnee en base.{RESET}")
    conn.close()


def cmd_install():
    """Installe le tracker en lancement silencieux dans HKCU (pas besoin d'admin)."""
    import winreg

    python_exe = Path(sys.executable)
    pythonw_exe = python_exe.with_name("pythonw.exe")
    if not pythonw_exe.exists():
        pythonw_exe = python_exe
    script_path = str(Path(__file__).resolve())

    # Dossier Startup de l'utilisateur
    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    bat_path = startup_dir / "0ResmonTracker.bat"

    print(f"  {CYAN}Installation dans le dossier Startup...{RESET}")
    print(f"  Python : {pythonw_exe}")
    print(f"  Script : {script_path}")
    print(f"  Startup: {startup_dir}")
    print()

    if not startup_dir.exists():
        print(f"  {RED}Dossier Startup introuvable: {startup_dir}{RESET}")
        return

    try:
        command = f'"{pythonw_exe}" "{script_path}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "0ResmonTracker", 0, winreg.REG_SZ, command)
        # Nettoie l'ancien lanceur visible s'il existe.
        if bat_path.exists():
            bat_path.unlink()
        print(f"  {GREEN}Installe ! Le tracker se lancera silencieusement avec Windows.{RESET}")
        print(f"  {DIM}Commande: {command}{RESET}")
        print(f"  {DIM}Pour desinstaller: lance --uninstall{RESET}")
    except Exception as e:
        print(f"  {RED}Erreur: {e}{RESET}")


def cmd_uninstall():
    """Supprime le lancement automatique du tracker."""
    import winreg

    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    bat_path = startup_dir / "0ResmonTracker.bat"
    removed = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "0ResmonTracker")
        removed = True
    except FileNotFoundError:
        pass
    if bat_path.exists():
        bat_path.unlink()
        removed = True
    if removed:
        print(f"  {GREEN}Desinstalle. Le tracker ne se lancera plus au demarrage.{RESET}")
    else:
        print(f"  {YELLOW}Pas installe.{RESET}")


def cmd_reset():
    """Remet a zero la base de donnees (confirmation requise)."""
    answer = input(f"  {RED}ATTENTION: supprimer toutes les donnees ? (oui/non) : {RESET}")
    if answer.strip().lower() == "oui":
        if DB_PATH.exists():
            DB_PATH.unlink()
        print(f"  {GREEN}Base supprimee.{RESET}")
    else:
        print("  Annule.")


# ═══════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════

HELP = f"""
  {BOLD}0RESMON Revenue Tracker{RESET}

  {CYAN}python tracker.py{RESET}              Lancer le tracker (15 min)
  {CYAN}python tracker.py --fast{RESET}        Scrape toutes les 5 min (test)
  {CYAN}python tracker.py --once{RESET}        Un seul scrape puis quitte
  {CYAN}python tracker.py --report{RESET}      Afficher rapport + exporter CSV
  {CYAN}python tracker.py --reset{RESET}       Supprimer toutes les donnees
  {CYAN}python tracker.py --install{RESET}     Installer auto-start Windows
  {CYAN}python tracker.py --uninstall{RESET}   Desinstaller auto-start
"""

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(HELP)
    elif "--install" in args:
        cmd_install()
    elif "--uninstall" in args:
        cmd_uninstall()
    elif "--report" in args:
        cmd_report()
    elif "--reset" in args:
        cmd_reset()
    elif "--once" in args:
        INTERVAL = 0
        conn = init_db()
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute("INSERT INTO scrape_sessions (started_at) VALUES (?)", (now_s,))
        session_id = cur.lastrowid
        conn.commit()
        total_existing = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        first_seen_row = conn.execute("SELECT MIN(scraped_at) FROM payments").fetchone()[0]
        print_header(total_existing, first_seen_row)
        try:
            new = do_scrape(conn)
            stats = compute_stats(conn)
            print_scrape_result(new, 1)
            print()
            print_stats(stats)
            print_day_breakdown(stats)
        except Exception as e:
            print(f"\n  {RED}Erreur: {e}{RESET}")
        conn.close()
    elif "--fast" in args:
        INTERVAL = 5 * 60
        if acquire_daemon_lock():
            run(INTERVAL)
    else:
        if acquire_daemon_lock():
            run(INTERVAL)
