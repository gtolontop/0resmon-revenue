"""Builds a standalone dashboard.html from the cleaned tracker data."""
import json, sqlite3
from collections import Counter
from datetime import datetime as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dashboard.html"
L = lambda n: json.load(open(ROOT / "data/dashboard" / f"{n}.json", encoding="utf-8"))["rows"]
k, months, days, heat, baskets, buyers = L("kpis")[0], L("mois"), L("jours"), L("heat"), L("paniers"), L("acheteurs")
c = sqlite3.connect(ROOT / "data/clean/payments_clean.db")
P = [(D.fromisoformat(a), e) for a, e in c.execute("select paid_paris, amount_eur from payments")]
ts = sorted(p[0] for p in P)
tot = sum(e for _, e in P)
big = [e for _, e in P if e >= 100]
price, price_n = Counter(round(e, 2) for _, e in P).most_common(1)[0]
best = max(heat, key=lambda r: r["eur_h"]); worst = min(heat, key=lambda r: r["eur_h"])
we = sum(r["eur_h"] for r in heat if r["jour_n"] >= 5) / 2; wd = sum(r["eur_h"] for r in heat if r["jour_n"] < 5) / 5
ev = sum(r["eur_h"] for r in heat if r["heure"] >= 18) / 42; mo = sum(r["eur_h"] for r in heat if 7 <= r["heure"] <= 11) / 35
same_min = sum(1 for a, b in zip(ts, ts[1:]) if a == b)
EN = {"Lun": "Mon", "Mar": "Tue", "Mer": "Wed", "Jeu": "Thu", "Ven": "Fri", "Sam": "Sat", "Dim": "Sun"}
MON = {"02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun", "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov"}
DAYN = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
data = {
    "k": k,
    "facts": {
        "every_min": 24 * 60 / k["ventes_jour"], "per_min": k["est_jour"] / 1440,
        "best": {"d": DAYN[EN[best["jour"]]], "h": best["heure"], "v": best["eur_h"]},
        "worst": {"d": DAYN[EN[worst["jour"]]], "h": worst["heure"], "v": worst["eur_h"]},
        "evening_x": ev / mo, "weekend_pct": (we / wd - 1) * 100,
        "big_orders_pct": len(big) / len(P) * 100, "big_rev_pct": sum(big) / tot * 100,
        "price": price, "price_n": price_n, "same_min": same_min,
    },
    "months": [{"m": MON[r["mois"][5:]], "v": r["eur_jour"], "n": r["ventes_jour"], "obs": r["observe_eur"], "est": r["estime_eur"], "h": r["heures_verifiees"]} for r in months],
    "days": [[r["jour"], r["eur"], r["ventes"], r["heures_verifiees"]] for r in days],
    "heat": [[r["jour_n"], r["heure"], r["eur_h"], r["ventes_h"]] for r in heat],
    "baskets": [{"t": r["tranche"].replace("< ", "under ").replace(" +", "+"), "n": r["part_ventes"], "e": r["part_eur"]} for r in baskets],
    "buyers": [{"u": r["acheteur"], "n": r["achats"], "e": r["eur"]} for r in buyers[:8]],
}
html = (ROOT / "analysis/dashboard_template.html").read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
OUT.write_text(html, encoding="utf-8")
print(OUT, len(html))
