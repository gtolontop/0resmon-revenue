"""Exporte les agregats du dashboard depuis payments_clean.db (base nettoyee par audit_clean.py)."""
import sqlite3, json, statistics as st
from datetime import datetime as D, timedelta as T
from collections import defaultdict, Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

DB, OUT = ROOT / "data/clean/payments_clean.db", ROOT / "data/dashboard"; OUT.mkdir(exist_ok=True)
CODE = Path(__file__).read_text(encoding="utf-8")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
P = [dict(r) for r in c.execute("select * from payments")]
for p in P: p["t"] = D.fromisoformat(p["paid_paris"])
W = [(D.fromisoformat(r["start_paris"]), D.fromisoformat(r["end_paris"])) for r in c.execute("select * from coverage where verified=1")]
ALLW = [(D.fromisoformat(r["start_paris"]), D.fromisoformat(r["end_paris"])) for r in c.execute("select * from coverage")]
START, END = ALLW[0][0], ALLW[-1][1]

def split(key_fn, step_fn):
    acc = defaultdict(float)
    for a, b in W:
        t = a
        while t < b:
            n = min(b, step_fn(t)); acc[key_fn(t)] += (n - t).total_seconds() / 3600; t = n
    return acc
next_hour = lambda t: t.replace(minute=0, second=0, microsecond=0) + T(hours=1)
next_day = lambda t: t.replace(hour=0, minute=0, second=0, microsecond=0) + T(days=1)
next_month = lambda t: (t.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + T(days=32)).replace(day=1)

def save(name, rows, desc):
    json.dump({"rows": rows, "meta": [{"label": "export.py", "value": CODE, "type": "code", "lang": "python"},
                                      {"label": "Entree", "value": "payments_clean.db (" + desc + ")"}]},
              open(OUT / f"{name}.json", "w", encoding="utf-8"), ensure_ascii=False)

V = [p for p in P if p["in_verified_window"]]
ver_h = sum((b - a).total_seconds() for a, b in W) / 3600
span_h = (END - START).total_seconds() / 3600

# heure x jour
eh = split(lambda t: (t.weekday(), t.hour), next_hour)
rv, cn = defaultdict(float), Counter()
for p in V: k = (p["t"].weekday(), p["t"].hour); rv[k] += p["amount_eur"]; cn[k] += 1
DAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
heat = [{"jour": DAYS[d], "jour_n": d, "heure": h, "expo_h": round(eh[(d, h)], 2), "eur_h": round(rv[(d, h)] / eh[(d, h)], 2), "ventes_h": round(cn[(d, h)] / eh[(d, h)], 3)}
        for d in range(7) for h in range(24)]
week = sum(r["eur_h"] for r in heat); week_n = sum(r["ventes_h"] for r in heat)

# mois
em = split(lambda t: t.strftime("%Y-%m"), next_month)
mo = defaultdict(lambda: {"obs_eur": 0.0, "obs_n": 0, "v_eur": 0.0, "v_n": 0})
for p in P:
    m = mo[p["t"].strftime("%Y-%m")]; m["obs_eur"] += p["amount_eur"]; m["obs_n"] += 1
    if p["in_verified_window"]: m["v_eur"] += p["amount_eur"]; m["v_n"] += 1
NOMS = {"02": "Fev", "03": "Mars", "04": "Avr", "05": "Mai", "06": "Juin", "07": "Juil", "08": "Aout", "09": "Sept", "10": "Oct", "11": "Nov"}
months, est_total = [], 0.0
for k in sorted(mo):
    m = mo[k]; h = em[k]; a = max(START, D.fromisoformat(k + "-01")); b = min(END, next_month(D.fromisoformat(k + "-01")))
    days = (b - a).total_seconds() / 86400; per_day = m["v_eur"] / h * 24
    est = per_day * days; est_total += est
    months.append({"mois": k, "label": NOMS[k[5:]], "jours": round(days, 2), "eur_jour": round(per_day, 2), "ventes_jour": round(m["v_n"] / h * 24, 2),
                   "heures_verifiees": round(h, 1), "observe_eur": round(m["obs_eur"], 2), "observe_n": m["obs_n"], "estime_eur": round(est, 2)})

# jours
ed = split(lambda t: t.strftime("%Y-%m-%d"), next_day)
dd = defaultdict(lambda: [0.0, 0])
for p in P: k = p["t"].strftime("%Y-%m-%d"); dd[k][0] += p["amount_eur"]; dd[k][1] += 1
d, daily = START.date(), []
while d <= END.date():
    k = d.isoformat(); daily.append({"jour": k, "eur": round(dd[k][0], 2), "ventes": dd[k][1], "heures_verifiees": round(ed.get(k, 0), 2)}); d += T(days=1)

# paniers
am = [p["amount_eur"] for p in P]; tot = sum(am)
baskets = []
for lo, hi, lab in [(0, 15, "< 15 €"), (15, 30, "15–30 €"), (30, 50, "30–50 €"), (50, 100, "50–100 €"), (100, 200, "100–200 €"), (200, 1e9, "200 € +")]:
    s = [x for x in am if lo <= x < hi]
    baskets.append({"tranche": lab, "ventes": len(s), "eur": round(sum(s), 2), "part_ventes": round(len(s) / len(am) * 100, 1), "part_eur": round(sum(s) / tot * 100, 1)})

# acheteurs
u = defaultdict(lambda: {"n": 0, "eur": 0.0, "nom": ""})
for p in P:
    if p["username_reliable"]: x = u[p["username"].lower()]; x["n"] += 1; x["eur"] += p["amount_eur"]; x["nom"] = p["username"]
uv = sorted(u.values(), key=lambda x: -x["eur"]); allu = sum(x["eur"] for x in uv)
buyers = [{"rang": i + 1, "acheteur": x["nom"], "achats": x["n"], "eur": round(x["eur"], 2), "panier": round(x["eur"] / x["n"], 2)} for i, x in enumerate(uv[:12])]

# kpis
nov = next(m for m in months if m["mois"] == "2026-11"); octo = next(m for m in months if m["mois"] == "2026-10")
kpi = [{
    "est_jour": round(week / 7, 2), "est_semaine": round(week, 2), "est_mois": round(week / 7 * 30.44, 2), "est_an": round(week / 7 * 365),
    "ventes_jour": round(week_n / 7, 1), "eur_heure": round(sum(p["amount_eur"] for p in V) / ver_h, 2),
    "observe_eur": round(tot, 2), "observe_n": len(P), "estime_periode": round(est_total, 2), "capte_pct": round(tot / est_total * 100, 1),
    "heures_verifiees": round(ver_h, 1), "heures_total": round(span_h, 1), "couverture_pct": round(ver_h / span_h * 100, 1), "jours": round(span_h / 24, 1),
    "panier_moy": round(st.mean(am), 2), "panier_med": round(st.median(am), 2), "panier_max": max(am),
    "acheteurs": len(uv), "recurrents": sum(1 for x in uv if x["n"] >= 2), "part_recurrents": round(sum(x["eur"] for x in uv if x["n"] >= 2) / allu * 100, 1),
    "top10_part": round(sum(x["eur"] for x in uv[:len(uv) // 10]) / allu * 100, 1),
    "nov_jour": nov["eur_jour"], "oct_jour": octo["eur_jour"], "nov_vs_oct": round((nov["eur_jour"] / octo["eur_jour"] - 1) * 100, 1),
    "pic_eur_h": round(max(sum(r["eur_h"] for r in heat if r["heure"] == h) / 7 for h in range(24)), 2),
    "creux_eur_h": round(min(sum(r["eur_h"] for r in heat if r["heure"] == h) / 7 for h in range(24)), 2),
}]
dups = c.execute("select count(*) from dropped_duplicates").fetchone()[0]
quality = [
    {"probleme": "Pseudos décalés", "nombre": dups, "unite": "doublons supprimés", "gravite": "critique",
     "detail": "Les pseudos avec un point ou un tiret ne sont pas lus : chaque vente prend le pseudo de sa voisine et se réinsère sous un autre nom."},
    {"probleme": "Pseudos incertains", "nombre": sum(1 for p in P if not p["username_reliable"]), "unite": "paiements", "gravite": "moyen",
     "detail": "Montant et heure justes, acheteur peut-être faux (vus pendant un scrape décalé). Exclus du classement acheteurs."},
    {"probleme": "Scrapes vides", "nombre": 8311, "unite": "scrapes sur 17 285", "gravite": "critique",
     "detail": "Page anti-bot Cloudflare comptée comme 0 vente, sans erreur. Les ventes de ces trous sont perdues si plus de 5 passent."},
    {"probleme": "Heures en UTC", "nombre": len(P), "unite": "heures corrigées", "gravite": "moyen",
     "detail": "Le site affiche l'heure UTC : tout était décalé de 1 h (hiver) ou 2 h (été). Converti en heure de Paris."},
    {"probleme": "GBP comptés comme EUR", "nombre": sum(1 for p in P if p["currency"] == "GBP"), "unite": "paiements", "gravite": "faible",
     "detail": "Convertis à 1 GBP = 1,16 €."},
    {"probleme": "Date incertaine", "nombre": sum(1 for p in P if p["date_uncertain"]), "unite": "paiements", "gravite": "faible",
     "detail": "Vus après plus de 20 h sans scrape : le site n'affiche que « Today HH:MM »."},
]
for name, rows, desc in [("kpis", kpi, "indicateurs"), ("mois", months, "par mois"), ("jours", daily, "par jour"), ("heat", heat, "heure x jour"),
                         ("paniers", baskets, "tranches"), ("acheteurs", buyers, "top 12, pseudos fiables"), ("qualite", quality, "audit")]:
    save(name, rows, desc)
print(json.dumps(kpi[0], ensure_ascii=False))
