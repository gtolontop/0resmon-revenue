#!/usr/bin/env python3
"""
0RESMON Tebex Store - Recent Payments Tracker
Scrape toutes les 30 min, stocke les paiements, estime les revenus.
"""

import re
import json
import csv
import time
import os
import subprocess
from datetime import datetime, date
from collections import defaultdict

URL = "https://0resmon.tebex.io/"

DATA_FILE = "payments.json"
CSV_FILE  = "payments.csv"
LOG_FILE  = "scraper.log"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_page() -> str:
    """Utilise curl pour contourner les protections anti-bot (TLS fingerprint)."""
    result = subprocess.run(
        ["curl", "-s", "--compressed", "--max-time", "30",
         "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
         "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "-H", "Accept-Language: fr-FR,fr;q=0.9,en-US;q=0.8",
         URL],
        capture_output=True, text=True, timeout=40
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl error: {result.stderr}")
    if not result.stdout:
        raise RuntimeError("Réponse vide de curl")
    return result.stdout


def parse_payments(html: str) -> list[dict]:
    """Extrait les paiements récents du HTML de la page."""
    usernames = re.findall(
        r'alt="(\w+) Customer Profile Photo"', html
    )
    times = re.findall(
        r'text-white/25 font-normal text-sm line-clamp-1">\s*(.*?)\s*</p>',
        html, re.DOTALL
    )
    prices_raw = re.findall(
        r'package-price">\s*([\d.]+)(EUR|USD|GBP)\s*</p>',
        html, re.DOTALL
    )

    payments = []
    count = min(len(usernames), len(times), len(prices_raw))
    for i in range(count):
        payments.append({
            "username":   usernames[i],
            "time_label": times[i].strip(),
            "amount":     float(prices_raw[i][0]),
            "currency":   prices_raw[i][1],
        })
    return payments


def build_uid(p: dict, scraped_at: str) -> str:
    """Clé unique : username + time_label + amount (évite doublons entre runs)."""
    return f"{p['username']}|{p['time_label']}|{p['amount']}{p['currency']}"


def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": [], "payments": []}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_csv(payment: dict):
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "scraped_at", "username", "time_label", "amount", "currency"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow(payment)


def compute_stats(payments: list[dict]) -> dict:
    """Calcule des estimations de revenus."""
    if not payments:
        return {}

    # Groupe par date de scraping (YYYY-MM-DD)
    by_day: dict[str, float] = defaultdict(float)
    for p in payments:
        day = p.get("scraped_at", "")[:10]
        by_day[day] += p["amount"]

    total      = sum(p["amount"] for p in payments)
    num_days   = len(by_day) if by_day else 1
    avg_day    = total / num_days
    avg_year   = avg_day * 365

    # Temps span des paiements récents (dernier run)
    # On prend les paiements du dernier scrape pour estimer le rythme
    last_scrape_ts = max(p.get("scraped_at", "") for p in payments)
    last_batch = [p for p in payments if p.get("scraped_at", "") == last_scrape_ts]

    return {
        "total_payments": len(payments),
        "total_revenue_eur": round(total, 2),
        "unique_days_tracked": num_days,
        "avg_per_day_eur": round(avg_day, 2),
        "estimated_year_eur": round(avg_year, 2),
        "last_batch_size": len(last_batch),
        "last_batch_total_eur": round(sum(p["amount"] for p in last_batch), 2),
    }


def print_report(stats: dict, new_count: int):
    print("\n" + "="*55)
    print("  RAPPORT 0RESMON - ESTIMATION REVENUS")
    print("="*55)
    print(f"  Nouveaux paiements ce run   : {new_count}")
    print(f"  Total paiements trackés     : {stats.get('total_payments', 0)}")
    print(f"  Revenu total collecté       : {stats.get('total_revenue_eur', 0):.2f} EUR")
    print(f"  Jours de tracking           : {stats.get('unique_days_tracked', 0)}")
    print(f"  Moy. par jour (estimée)     : {stats.get('avg_per_day_eur', 0):.2f} EUR")
    print(f"  Estimation annuelle         : {stats.get('estimated_year_eur', 0):.2f} EUR")
    print(f"  Dernier batch ({stats.get('last_batch_size',0)} paiements)  : {stats.get('last_batch_total_eur',0):.2f} EUR")
    print("="*55 + "\n")


def run_once():
    log("--- Scrape démarré ---")
    try:
        html = fetch_page()
    except Exception as e:
        log(f"ERREUR fetch: {e}")
        return 0

    payments = parse_payments(html)
    log(f"Paiements trouvés sur la page : {len(payments)}")

    data = load_data()
    seen = set(data["seen_ids"])
    new_count = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for p in payments:
        uid = build_uid(p, now)
        if uid not in seen:
            seen.add(uid)
            p["scraped_at"] = now
            data["payments"].append(p)
            data["seen_ids"].append(uid)
            append_csv(p)
            log(f"  NOUVEAU  {p['username']:20s}  {p['amount']:7.2f} {p['currency']}  ({p['time_label']})")
            new_count += 1

    save_data(data)

    stats = compute_stats(data["payments"])
    print_report(stats, new_count)
    log(f"Nouveaux: {new_count} | Total: {len(data['payments'])} | Avg/jour: {stats.get('avg_per_day_eur',0):.2f} EUR")
    return new_count


def run_loop(interval_minutes: int = 30):
    log(f"Démarrage du tracker (intervalle: {interval_minutes} min)")
    while True:
        run_once()
        log(f"Prochain scrape dans {interval_minutes} min...")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        run_once()
    else:
        run_loop(30)
