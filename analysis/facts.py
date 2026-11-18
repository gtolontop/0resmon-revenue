"""Fun facts for the dashboard, from payments_clean.db + heat.json."""
import json, sqlite3
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "data"; CODE = Path(__file__).read_text(encoding="utf-8")
k = json.load(open(S/"dashboard/kpis.json", encoding="utf-8"))["rows"][0]
heat = json.load(open(S/"dashboard/heat.json", encoding="utf-8"))["rows"]
P = [e for (e,) in sqlite3.connect(S/"clean/payments_clean.db").execute("select amount_eur from payments")]
DAY = {"Lun":"Monday","Mar":"Tuesday","Mer":"Wednesday","Jeu":"Thursday","Ven":"Friday","Sam":"Saturday","Dim":"Sunday"}
b = max(heat, key=lambda r: r["eur_h"]); big = [e for e in P if e >= 100]
we = sum(r["eur_h"] for r in heat if r["jour_n"] >= 5)/2; wd = sum(r["eur_h"] for r in heat if r["jour_n"] < 5)/5
ev = sum(r["eur_h"] for r in heat if r["heure"] >= 18)/42; mo = sum(r["eur_h"] for r in heat if 7 <= r["heure"] <= 11)/35
price, pn = Counter(round(e, 2) for e in P).most_common(1)[0]
row = {"minutes_between_sales": round(1440/k["ventes_jour"], 1), "eur_per_minute": round(k["est_jour"]/1440, 2),
       "best_day": DAY[b["jour"]], "best_hour": b["heure"], "best_eur_h": b["eur_h"],
       "evening_vs_morning": round(ev/mo, 2), "weekend_uplift_pct": round((we/wd-1)*100, 1),
       "big_orders_pct": round(len(big)/len(P)*100, 1), "big_revenue_pct": round(sum(big)/sum(P)*100, 1),
       "top_price": price, "top_price_orders": pn}
json.dump({"rows": [row], "meta": [{"label": "facts.py", "value": CODE, "type": "code", "lang": "python"}, {"label": "Input", "value": "payments_clean.db, heat.json"}]},
          open(S/"dashboard/facts.json", "w", encoding="utf-8"))
print(row)
