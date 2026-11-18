"""Audit + nettoyage des donnees du 0RESMON Revenue Tracker (lecture seule sur la base source)."""
import sqlite3, re, json, csv, statistics as st, bisect
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
from datetime import datetime as D, timedelta as T
from collections import defaultdict, Counter

SRC_DB = ROOT / "data/raw/payments.db"
LOG = ROOT / "data/raw/tracker.log"
OUT_DB = str(ROOT / "data/clean/payments_clean.db")
GBP_EUR = 1.16
OUT = open(ROOT / "data/clean/audit_report.txt", "w", encoding="utf-8")
def P(*a): print(*a, file=OUT, flush=True)

# ---------- fuseau : site en UTC, PC en Europe/Paris ----------
DST_ON, DST_OFF = D(2026, 3, 29, 1), D(2026, 10, 25, 1)   # en UTC
def utc_to_paris(u): return u + T(hours=2 if DST_ON <= u < DST_OFF else 1)

# ---------- log : tous les scrapes ----------
scrapes = []   # dict(ts, found, new, err, sess)
sess = 0
for l in open(LOG, encoding="utf-8"):
    m = re.match(r"\[([^\]]+)\] (.*)", l.rstrip("\n"))
    if not m: continue
    ts, msg = D.fromisoformat(m.group(1)), m.group(2)
    if msg.startswith("=== SESSION") and "DEMARREE" in msg: sess += 1
    mm = re.match(r"Scrape #\d+ \| trouves=(\d+) nouveaux=(\d+)", msg)
    if mm: scrapes.append(dict(ts=ts, found=int(mm.group(1)), new=int(mm.group(2)), err=False, sess=sess))
    elif msg.startswith("ERREUR scrape"): scrapes.append(dict(ts=ts, found=0, new=0, err=True, sess=sess))
scrapes.sort(key=lambda s: s["ts"])
sts = [s["ts"] for s in scrapes]

# ---------- base ----------
c = sqlite3.connect(f"file:{SRC_DB.as_posix()}?mode=ro", uri=True); c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute("select * from payments")]
# rattache chaque ligne a son scrape (premier scrape loggue apres scraped_at)
unmapped = 0
for r in rows:
    sa = D.fromisoformat(r["scraped_at"])
    i = bisect.bisect_left(sts, sa)
    if i < len(sts) and (sts[i] - sa).total_seconds() < 300: r["si"] = i
    else: r["si"] = None; unmapped += 1
    r["utc"] = D.fromisoformat(r["payment_dt"])
    r["paris"] = utc_to_paris(r["utc"])
    r["eur"] = r["amount"] * (GBP_EUR if r["currency"] == "GBP" else 1)

def misaligned(r): return r["si"] is not None and 0 < scrapes[r["si"]]["found"] < 5

# ---------- dedup : meme paiement (minute UTC + montant + devise) re-insere par decalage ----------
groups = defaultdict(list)
for r in rows: groups[(r["payment_dt"], r["amount"], r["currency"])].append(r)
drop_shift, legit_collisions = 0, 0
for g in groups.values():
    g.sort(key=lambda r: r["scraped_at"])
    for k, r in enumerate(g):
        r["dup"] = False
        if k == 0: continue
        if r["scraped_at"] > g[0]["scraped_at"] and misaligned(r): r["dup"] = True; drop_shift += 1
        else: legit_collisions += 1
clean = [r for r in rows if not r["dup"]]
for r in clean: r["user_ok"] = not misaligned(r)

# ---------- couverture verifiee ----------
new_by_scrape = Counter(r["si"] for r in clean if r["si"] is not None)
ok = [i for i, s in enumerate(scrapes) if s["found"] > 0]
windows = []   # (start, end, verified)
for a, b in zip(ok, ok[1:]):
    overlap = scrapes[b]["found"] - new_by_scrape.get(b, 0) > 0
    windows.append((scrapes[a]["ts"], scrapes[b]["ts"], overlap))
wstarts = [w[0] for w in windows]
ok_ts = [scrapes[i]["ts"] for i in ok]
def window_of(t):
    i = bisect.bisect_left(wstarts, t) - 1
    if 0 <= i < len(windows) and windows[i][0] < t <= windows[i][1]: return i
    return None
for r in clean:
    wi = window_of(r["paris"])
    r["verified"] = wi is not None and windows[wi][2]
    # date incertaine : vu pour la 1re fois apres > 20h sans scrape reussi (site n'affiche que "Today HH:MM")
    sa = D.fromisoformat(r["scraped_at"]); k = bisect.bisect_left(ok_ts, sa) - 1
    r["date_doubt"] = k < 0 or (sa - ok_ts[k]).total_seconds() > 20 * 3600
ver_h = sum((b - a).total_seconds() for a, b, v in windows if v) / 3600
unver_h = sum((b - a).total_seconds() for a, b, v in windows if not v) / 3600
span_h = (scrapes[-1]["ts"] - scrapes[0]["ts"]).total_seconds() / 3600

# exposition par heure-de-semaine
expo = defaultdict(float); rev_how = defaultdict(float); cnt_how = defaultdict(int)
for a, b, v in windows:
    if not v: continue
    t = a
    while t < b:
        nxt = min(b, (t.replace(minute=0, second=0, microsecond=0) + T(hours=1)))
        expo[(t.weekday(), t.hour)] += (nxt - t).total_seconds() / 3600; t = nxt
for r in clean:
    if r["verified"]:
        k = (r["paris"].weekday(), r["paris"].hour); rev_how[k] += r["eur"]; cnt_how[k] += 1
week_est = sum(rev_how[k] / expo[k] for k in expo if expo[k] > 0)
week_cnt = sum(cnt_how[k] / expo[k] for k in expo if expo[k] > 0)
missing_buckets = [k for k in [(d, h) for d in range(7) for h in range(24)] if expo.get(k, 0) < 3]

# ---------- sortie ----------
P("=== VOLUME ===")
P("scrapes log", len(scrapes), "| ok", len(ok), "| vides(Cloudflare/0)", sum(1 for s in scrapes if s['found']==0 and not s['err']), "| erreurs", sum(s['err'] for s in scrapes),
  "| desalignes", sum(1 for s in scrapes if 0 < s['found'] < 5))
P("lignes brutes", len(rows), "non rattachees", unmapped, "| doublons decalage", drop_shift, "| collisions legit", legit_collisions, "| propres", len(clean))
P("pseudos incertains", sum(not r["user_ok"] for r in clean), "| date incertaine", sum(r["date_doubt"] for r in clean))
P("verifie: %.1f h | non verifie %.1f h | periode %.1f h (%.1f j) | couverture verifiee %.1f%%" % (ver_h, unver_h, span_h, span_h/24, ver_h/span_h*100))
tot = sum(r["eur"] for r in clean); vtot = sum(r["eur"] for r in clean if r["verified"]); vcnt = sum(r["verified"] for r in clean)
P("revenu observe propre %.2f EUR (%d paiements) | dont fenetres verifiees %.2f EUR (%d)" % (tot, len(clean), vtot, vcnt))
P("brut avant nettoyage (EUR+GBP additionnes tels quels) %.2f" % sum(r["amount"] for r in rows))
P("taux moyen brut verifie %.3f EUR/h ; %.3f paiements/h" % (vtot/ver_h, vcnt/ver_h))
P("estimation semaine (pondere heure-de-semaine) %.2f EUR | %.1f paiements | /jour %.2f | /mois(30.44j) %.2f | /an %.0f" % (week_est, week_cnt, week_est/7, week_est/7*30.44, week_est/7*365))
P("buckets heure-semaine <3h d'expo:", len(missing_buckets))

# par mois : taux verifie
P("\n=== PAR MOIS (heure Paris) ===")
mexp = defaultdict(float)
for a, b, v in windows:
    if not v: continue
    t = a
    while t < b:
        nm = (t.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + T(days=32)).replace(day=1)
        nxt = min(b, nm); mexp[t.strftime("%Y-%m")] += (nxt - t).total_seconds()/3600; t = nxt
mo = defaultdict(lambda: [0, 0.0, 0, 0.0])
for r in clean:
    k = r["paris"].strftime("%Y-%m"); mo[k][0] += 1; mo[k][1] += r["eur"]
    if r["verified"]: mo[k][2] += 1; mo[k][3] += r["eur"]
for k in sorted(mo):
    n, e, vn, ve = mo[k]; h = mexp.get(k, 0)
    P(k, "obs %d / %.2f EUR | verif %.0fh -> %.2f EUR/h, %.2f EUR/j, %.2f ventes/j" % (n, e, h, ve/h if h else 0, ve/h*24 if h else 0, vn/h*24 if h else 0))

P("\n=== PROFIL HORAIRE (EUR/h verifie, heure Paris) ===")
for h in range(24):
    ex = sum(expo[(d, h)] for d in range(7)); rv = sum(rev_how[(d, h)] for d in range(7)); cn = sum(cnt_how[(d, h)] for d in range(7))
    P("%02dh expo %6.1fh  %7.2f EUR/h  %.2f ventes/h" % (h, ex, rv/ex if ex else 0, cn/ex if ex else 0))
P("\n=== PROFIL JOUR SEMAINE ===")
for d, nm in enumerate(["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]):
    ex = sum(expo[(d, h)] for h in range(24)); rv = sum(rev_how[(d, h)] for h in range(24)); cn = sum(cnt_how[(d, h)] for h in range(24))
    P(nm, "expo %.0fh  %.2f EUR/h -> %.2f EUR/j  %.1f ventes/j" % (ex, rv/ex, rv/ex*24, cn/ex*24))

P("\n=== PANIER ===")
am = sorted(r["eur"] for r in clean)
P("moy %.2f med %.2f min %.2f max %.2f p90 %.2f p99 %.2f" % (st.mean(am), st.median(am), am[0], am[-1], am[int(len(am)*.9)], am[int(len(am)*.99)]))
for lo, hi in [(0, 15), (15, 30), (30, 50), (50, 100), (100, 200), (200, 1e9)]:
    s = [x for x in am if lo <= x < hi]; P("%s-%s: %d (%.1f%%) = %.2f EUR (%.1f%%)" % (lo, hi if hi < 1e9 else "+", len(s), len(s)/len(am)*100, sum(s), sum(s)/tot*100))
P("devise", Counter(r["currency"] for r in clean), "GBP en EUR %.2f" % sum(r["eur"] for r in clean if r["currency"] == "GBP"))
P("top montants", Counter(r["amount"] for r in clean).most_common(12))
P("plus gros paiements", [(r["username"], r["amount"], r["currency"], r["paris"].strftime("%Y-%m-%d %H:%M")) for r in sorted(clean, key=lambda r: -r["eur"])[:8]])

P("\n=== JOURS (obs, Paris) ===")
byd = defaultdict(float); byc = Counter()
for r in clean: k = r["paris"].strftime("%Y-%m-%d"); byd[k] += r["eur"]; byc[k] += 1
P("top jours", sorted(byd.items(), key=lambda x: -x[1])[:5], [byc[k] for k, _ in sorted(byd.items(), key=lambda x: -x[1])[:5]])
P("top heures-pleines (fenetre 60min glissante max)")
srt = sorted(clean, key=lambda r: r["paris"]); best = (0, None, 0)
j = 0; s = 0
for i, r in enumerate(srt):
    s += r["eur"]
    while srt[j]["paris"] <= r["paris"] - T(hours=1): s -= srt[j]["eur"]; j += 1
    if s > best[0]: best = (s, r["paris"], i - j + 1)
P("  %.2f EUR en 1h finissant %s (%d ventes)" % best)

P("\n=== ACHETEURS (pseudos fiables uniquement) ===")
u = defaultdict(lambda: [0, 0.0]);
for r in clean:
    if r["user_ok"]: k = r["username"].lower(); u[k][0] += 1; u[k][1] += r["eur"]
P("acheteurs distincts", len(u), "| 1 achat", sum(1 for v in u.values() if v[0] == 1), "| 2+", sum(1 for v in u.values() if v[0] >= 2), "| 5+", sum(1 for v in u.values() if v[0] >= 5))
rep = sum(v[1] for v in u.values() if v[0] >= 2); allu = sum(v[1] for v in u.values())
P("CA des recurrents %.1f%%" % (rep/allu*100))
P("top par CA", sorted(((k, v[0], round(v[1], 2)) for k, v in u.items()), key=lambda x: -x[2])[:10])
P("top par nb", sorted(((k, v[0], round(v[1], 2)) for k, v in u.items()), key=lambda x: -x[1])[:5])
srt_u = sorted(u.values(), key=lambda v: -v[1]); top10 = sum(v[1] for v in srt_u[:max(1, len(srt_u)//10)])
P("top 10%% acheteurs = %.1f%% du CA" % (top10/allu*100))

P("\n=== PACKAGES (catalogue fige depuis %s) ===" % c.execute("select max(updated_at) from catalog").fetchone()[0])
pg = defaultdict(lambda: [0, 0.0, 0])
for r in clean:
    g = r["package_guess"]
    if g and not g.startswith("?"): k = g.lstrip("~"); pg[k][0] += 1; pg[k][1] += r["eur"]; pg[k][2] += (not g.startswith("~"))
P("exacts", sum(1 for r in clean if r["package_guess"] and not r["package_guess"][0] in "~?"), "approx ~", sum(1 for r in clean if (r["package_guess"] or "").startswith("~")), "ambigus ?", sum(1 for r in clean if (r["package_guess"] or "").startswith("?")), "aucun", sum(1 for r in clean if not r["package_guess"]))
for k, v in sorted(pg.items(), key=lambda x: -x[1][1])[:12]: P("  %-40s %4d ventes %9.2f EUR (exact %d)" % (k[:40], v[0], v[1], v[2]))

# ---------- base nettoyee ----------
o = sqlite3.connect(OUT_DB)
o.executescript("drop table if exists payments; drop table if exists coverage; drop table if exists dropped_duplicates; drop table if exists meta;")
o.execute("""create table payments(id text primary key, username text, username_reliable int, amount real, currency text, amount_eur real,
             paid_utc text, paid_paris text, first_seen_paris text, in_verified_window int, date_uncertain int, package_guess text)""")
o.executemany("insert into payments values (?,?,?,?,?,?,?,?,?,?,?,?)", [(r["id"], r["username"], int(r["user_ok"]), r["amount"], r["currency"], round(r["eur"], 2),
    r["payment_dt"], r["paris"].strftime("%Y-%m-%d %H:%M:%S"), r["scraped_at"], int(r["verified"]), int(r["date_doubt"]), r["package_guess"]) for r in sorted(clean, key=lambda r: r["paris"])])
o.execute("create table dropped_duplicates(id text, username text, amount real, currency text, paid_utc text, first_seen text)")
o.executemany("insert into dropped_duplicates values (?,?,?,?,?,?)", [(r["id"], r["username"], r["amount"], r["currency"], r["payment_dt"], r["scraped_at"]) for r in rows if r["dup"]])
o.execute("create table coverage(start_paris text, end_paris text, hours real, verified int)")
o.executemany("insert into coverage values (?,?,?,?)", [(a.isoformat(" "), b.isoformat(" "), round((b-a).total_seconds()/3600, 3), int(v)) for a, b, v in windows])
o.execute("create table meta(k text, v text)")
o.executemany("insert into meta values (?,?)", [("gbp_eur", str(GBP_EUR)), ("built", D.now().isoformat(" ")), ("source", "payments.db + tracker.log"),
    ("note", "paid_utc = heure affichee par le site (UTC). paid_paris = heure reelle Paris. in_verified_window=1 : aucun paiement n'a pu etre rate dans cette fenetre.")])
o.commit()
with open(OUT_DB.replace(".db", ".csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); cur = o.execute("select * from payments order by paid_paris"); w.writerow([d[0] for d in cur.description]); w.writerows(cur)
o.close()
OUT.close()
print((ROOT / "data/clean/audit_report.txt").read_text(encoding="utf-8"))
