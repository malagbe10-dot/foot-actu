#!/usr/bin/env python3
"""Bot football : récupère actus + scores + classements,
publie sur une page Facebook et génère un site statique (docs/index.html).

Dépendance : Pillow (pour les visuels), installé automatiquement au besoin.
Sans Pillow, les publications restent en texte.
Usage :
    python bot.py --dry-run          # simulation, ne publie rien
    python bot.py                    # publie sur Facebook + met à jour le site
    python bot.py --mode news        # seulement les actus (news | scores | standings | all)
"""
import argparse
import html
import io
import json
import os
import re
import subprocess
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
STATE_FILE = os.path.join(BASE, "state.json")
SITE_DIR = os.path.join(BASE, "docs")

FB_API = "https://graph.facebook.com/v21.0"
FD_API = "https://api.football-data.org/v4"


# ---------- utilitaires ----------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "FootBot/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def log(msg):
    print(msg, flush=True)


# ---------- sources ----------
def clean_summary(desc, title, limit=280):
    """Extrait court (chapeau du flux RSS) : sans HTML, coupé proprement."""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", desc or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    if not txt or txt.lower().startswith(title.lower()[:40]):
        return ""  # vide, ou simple répétition du titre
    if len(txt) <= limit:
        return txt
    cut = txt[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > 80:
        return cut[: end + 1]
    return cut[: cut.rfind(" ")].rstrip(" ,;:") + "…"


def fetch_news(cfg):
    """Lit les flux RSS. On publie titre + lien (jamais le texte complet : droits d'auteur)."""
    items = []
    for feed in cfg["news_feeds"]:
        try:
            root = ET.fromstring(http_get(feed["url"]))
        except Exception as e:  # noqa
            log(f"[news] flux ignoré {feed['name']}: {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if not title or not link:
                continue
            try:
                date = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
            except Exception:  # noqa
                date = datetime.now(timezone.utc)
            summary = clean_summary(it.findtext("description"), title)
            items.append({"id": "news-" + link, "title": title, "link": link, "summary": summary,
                          "source": feed["name"], "lang": feed.get("lang", "fr"), "date": date})
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def fd_get(path, token):
    return json.loads(http_get(FD_API + path, {"X-Auth-Token": token}))


def fetch_matches(cfg, token):
    today = datetime.now(timezone.utc).date()
    d1, d2 = today - timedelta(days=1), today + timedelta(days=1)
    codes = ",".join(cfg["competitions"])
    path = f"/matches?dateFrom={d1}&dateTo={d2}&competitions={codes}"
    return fd_get(path, token).get("matches", [])


def fetch_standings(code, token):
    data = fd_get(f"/competitions/{code}/standings", token)
    table = data["standings"][0]["table"][:10]
    return data["competition"]["name"], table


# ---------- construction des messages ----------
def score_line(m):
    s = m["score"]["fullTime"]
    return f"{m['homeTeam']['shortName'] or m['homeTeam']['name']} {s['home']} - {s['away']} {m['awayTeam']['shortName'] or m['awayTeam']['name']}"


MYMEMORY_API = "https://api.mymemory.translated.net/get"


def translate(text, src, dst="fr"):
    """Traduit un titre avec MyMemory (gratuit). Renvoie None en cas d'échec."""
    params = {"q": text, "langpair": f"{src}|{dst}"}
    email = os.environ.get("MYMEMORY_EMAIL")
    if email:
        params["de"] = email  # augmente le quota gratuit
    try:
        data = json.loads(http_get(MYMEMORY_API + "?" + urllib.parse.urlencode(params)))
        out = (data.get("responseData") or {}).get("translatedText", "")
        if data.get("responseStatus") != 200 or not out or "MYMEMORY WARNING" in out.upper():
            log(f"[traduction] refusée : {str(out)[:80]}")
            return None
        return html.unescape(out).strip()
    except Exception as e:  # noqa
        log(f"[traduction] erreur : {e}")
        return None


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def date_fr(d):
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def ensure_pillow():
    try:
        import PIL  # noqa
        return True
    except ImportError:
        for extra in ([], ["--break-system-packages"]):
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pillow", *extra],
                               check=True, timeout=180)
                import importlib
                importlib.invalidate_caches()
                import PIL  # noqa
                return True
            except Exception:  # noqa
                continue
    log("[visuel] Pillow indisponible : publication en texte seulement")
    return False


def load_font(size, bold=True):
    from PIL import ImageFont
    names = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"] if bold else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
              "/usr/share/fonts/truetype/liberation2"):
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fit_font(d, text, maxw, size, bold=True):
    while size > 16:
        f = load_font(size, bold)
        if d.textlength(text, font=f) <= maxw:
            return f
        size -= 2
    return load_font(16, bold)


def fetch_crest(url, size):
    """Logo du club (PNG). Les SVG ne sont pas lus : on dessine alors un rond avec les initiales."""
    from PIL import Image
    if not url or url.lower().endswith(".svg"):
        return None
    try:
        im = Image.open(io.BytesIO(http_get(url))).convert("RGBA")
        im.thumbnail((size, size))
        return im
    except Exception:  # noqa
        return None


def draw_team(img, d, team, crest_x, cy, side, name_x, maxw):
    name = team.get("shortName") or team.get("name") or "?"
    crest = fetch_crest(team.get("crest"), 68)
    if crest:
        img.paste(crest, (crest_x - crest.width // 2, cy - crest.height // 2), crest)
    else:
        d.ellipse([crest_x - 34, cy - 34, crest_x + 34, cy + 34], fill=(8, 52, 31),
                  outline=(190, 230, 205), width=3)
        d.text((crest_x, cy), (team.get("tla") or name[:3]).upper(), font=load_font(24),
               fill=(255, 255, 255), anchor="mm")
    d.text((name_x, cy), name, font=fit_font(d, name, maxw, 32),
           fill=(255, 255, 255), anchor="rm" if side == "right" else "lm")


def make_fixtures_image(todays, tz, day, path):
    from PIL import Image, ImageDraw
    rows = todays[:10]
    W, top, rh, foot = 1080, 230, 118, 100
    img = Image.new("RGB", (W, top + rh * len(rows) + foot), (11, 74, 43))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, top - 24], fill=(8, 52, 31))
    d.text((W // 2, 78), "MATCHS DU JOUR", font=load_font(66), fill=(255, 255, 255), anchor="mm")
    d.text((W // 2, 150), date_fr(day), font=load_font(34, False), fill=(190, 230, 205), anchor="mm")
    y = top
    for m in rows:
        d.rounded_rectangle([36, y, W - 36, y + rh - 14], radius=20, fill=(18, 98, 60))
        cy = y + 44
        t = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")
        d.text((110, cy), t, font=load_font(38), fill=(255, 255, 255), anchor="mm")
        draw_team(img, d, m["homeTeam"], 500, cy, "right", 455, 250)
        d.text((580, cy), "VS", font=load_font(26), fill=(190, 230, 205), anchor="mm")
        draw_team(img, d, m["awayTeam"], 660, cy, "left", 705, 325)
        d.text((580, y + 85), m["competition"]["name"], font=load_font(22, False),
               fill=(190, 230, 205), anchor="mm")
        y += rh
    d.text((W // 2, img.height - 50), "Foot Actu", font=load_font(34), fill=(190, 230, 205), anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def result_text(m):
    s = m["score"]["fullTime"]
    h = m["homeTeam"]["shortName"] or m["homeTeam"]["name"]
    a = m["awayTeam"]["shortName"] or m["awayTeam"]["name"]
    if s["home"] > s["away"]:
        return f"✅ Victoire de {h}"
    if s["home"] < s["away"]:
        return f"✅ Victoire de {a}"
    return "🤝 Match nul"


def build_posts(cfg, mode, state):
    posts = []
    tz = timezone(timedelta(hours=cfg.get("utc_offset_hours", 0)))
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "")

    if mode in ("news", "all"):
        count = 0
        for n in fetch_news(cfg):
            if count >= cfg["news_per_run"]:
                break
            if n["id"] in state["posted_ids"]:
                continue  # déjà publié : on ne traduit pas pour rien
            title, summary = n["title"], n.get("summary", "")
            known = [h["title"] for h in state["history"][-60:] if h.get("type") == "news"] + [p["title"] for p in posts if p["type"] == "news"]
            if n["lang"] == "fr" and similar_title(title, known):
                continue  # même info déjà reprise par une autre source
            if n["lang"] != "fr":
                title = translate(title, n["lang"])
                if not title:
                    continue  # on ne publie jamais un titre non traduit
                if summary:
                    summary = translate(summary, n["lang"]) or ""
            detail = f"{summary}\n\n" if summary else ""
            posts.append({"id": n["id"], "type": "news", "title": title,
                          "message": f"🚨 {title}\n\n{detail}📰 Source : {n['source']}\n\n#Football #Actu",
                          "link": n["link"]})
            count += 1

    if mode in ("scores", "all"):
        if not token and not os.environ.get("API_FOOTBALL_KEY"):
            log("[scores] aucune clé de scores : scores ignorés")
        else:
            try:
                matches = fetch_matches(cfg, token)
            except Exception as e:  # noqa
                log(f"[scores] erreur football-data: {e}")
                matches = []
            if not matches and os.environ.get("API_FOOTBALL_KEY"):
                log("[scores] source principale vide : bascule sur API-Football")
                matches = fetch_matches_apif(cfg, os.environ["API_FOOTBALL_KEY"], state)
            done_titles = {h.get("title") for h in state["history"] if h.get("type") == "score"}
            for m in matches:
                if m["status"] == "FINISHED" and m["score"]["fullTime"]["home"] is not None:
                    line = score_line(m)
                    if str(m["id"]).startswith("a") and line in done_titles:
                        continue  # déjà publié via l'autre source
                    comp = m["competition"]["name"]
                    journee = f" · Journée {m['matchday']}" if m.get("matchday") else ""
                    posts.append({"id": f"result-{m['id']}", "type": "score", "title": line,
                                  "message": f"⏱️ TERMINÉ\n\n⚽ {line.upper()}\n\n{result_text(m)}\n🏆 {comp}{journee}\n\n#Football #{comp.replace(' ', '')}"})
            # programme du jour (une fois par jour)
            today = datetime.now(tz).date()
            todays = [m for m in matches
                      if m["status"] in ("TIMED", "SCHEDULED")
                      and datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(tz).date() == today]
            if todays:
                todays.sort(key=lambda m: m["utcDate"])
                fid = f"fixtures-{today}"
                lines = []
                for m in todays:
                    h = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")
                    lines.append(f"🕒 {h} – {m['homeTeam']['shortName'] or m['homeTeam']['name']} vs "
                                 f"{m['awayTeam']['shortName'] or m['awayTeam']['name']} ({m['competition']['name']})")
                post = {"id": fid, "type": "fixtures", "title": f"Matchs du {date_fr(today)}",
                        "message": f"📅 MATCHS DU JOUR – {date_fr(today)}\n\n" + "\n".join(lines[:15])
                                   + "\n\n🔥 Bon match à tous !\n\n#Football"}
                if fid not in state["posted_ids"] and ensure_pillow():
                    rel = f"cards/{fid}.png"
                    try:
                        make_fixtures_image(todays, tz, today, os.path.join(SITE_DIR, rel))
                        post["image"] = rel
                    except Exception as e:  # noqa
                        log(f"[visuel] échec de l'image : {e}")
                posts.append(post)

    if mode in ("standings", "all") and token:
        week = datetime.now(tz).strftime("%G-W%V")
        for code in cfg.get("standings_competitions", []):
            pid = f"standings-{code}-{week}"
            if pid in state["posted_ids"]:
                continue
            try:
                name, table = fetch_standings(code, token)
            except Exception as e:  # noqa
                log(f"[classement] {code}: {e}")
                continue
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            rows = [f"{medals.get(r['position'], str(r['position']) + '.')} {r['team']['shortName'] or r['team']['name']} – {r['points']} pts" for r in table]
            detail = ""
            if len(table) > 1:
                lead, sec = table[0], table[1]
                ln = lead["team"]["shortName"] or lead["team"]["name"]
                sn = sec["team"]["shortName"] or sec["team"]["name"]
                gap = lead["points"] - sec["points"]
                ecart = f"{gap} point(s) d'avance sur {sn}" if gap else f"à égalité de points avec {sn}"
                detail = f"\n\n📌 {ln} est en tête avec {lead['points']} points en {lead['playedGames']} matchs, {ecart}."
            posts.append({"id": pid, "type": "standings", "title": f"Classement {name}",
                          "message": f"📊 Classement {name} (top 10)\n\n" + "\n".join(rows) + detail + "\n\n#Football"})
    return posts


# ---------- direct (API-Football) ----------
APIF = "https://v3.football.api-sports.io"
DEFAULT_LIVE_LEAGUES = [61, 62, 39, 40, 140, 135, 78, 88, 94, 2, 3, 848, 1, 4, 6, 12, 71, 15]


def apif_get(path, key):
    return json.loads(http_get(APIF + path, {"x-apisports-key": key}))


def _evt_kind(e):
    """Retourne 'goal', 'red', 'var', 'penmiss' ou None."""
    t, d = (e.get("type") or ""), (e.get("detail") or "")
    if t == "Goal":
        if d == "Missed Penalty":
            return "penmiss"
        return "goal"
    if t == "Card" and d in ("Red Card", "Second Yellow card"):
        return "red"
    if t == "Var":
        return "var"
    return None


def _minute(e):
    t = e.get("time") or {}
    el, ex = t.get("elapsed"), t.get("extra")
    return f"{el}+{ex}'" if ex else f"{el}'"


def live_event_post(fx, e, kind, score):
    teams = fx["teams"]
    home, away = teams["home"]["name"], teams["away"]["name"]
    comp = fx["league"]["name"]
    team = (e.get("team") or {}).get("name", "")
    player = (e.get("player") or {}).get("name") or "un joueur"
    assist = (e.get("assist") or {}).get("name")
    d = e.get("detail") or ""
    comments = e.get("comments") or ""
    mn = _minute(e)
    sc = f"{home} {score[0]} - {score[1]} {away}"
    if kind == "goal":
        if d == "Own Goal":
            head = f"😱 CSC ! {mn}"
            body = f"{player} marque contre son camp et offre un but à l'adversaire de {team}."
        elif d == "Penalty":
            head = f"⚽ BUT SUR PENALTY ! {mn}"
            body = f"{player} ({team}) ne tremble pas sur penalty."
        else:
            head = f"⚽ BUUUT ! {mn}"
            body = f"{player} ({team}) marque."
            if assist:
                body += f" Passe décisive de {assist}."
    elif kind == "penmiss":
        head = f"❌ PENALTY RATÉ ! {mn}"
        body = f"{player} ({team}) manque son penalty."
    elif kind == "red":
        head = f"🟥 CARTON ROUGE ! {mn}"
        why = "après un second carton jaune" if d == "Second Yellow card" else "sur carton rouge direct"
        body = f"{player} ({team}) est expulsé {why}."
        if comments:
            body += f" Motif : {comments}."
        body += f" {team} termine à dix."
    else:  # var
        head = f"📺 VAR ! {mn}"
        VAR_FR = {"Goal Disallowed": "but refusé", "Goal confirmed": "but validé", "Penalty confirmed": "penalty accordé", "Card upgrade": "carton rouge après visionnage"}
        body = f"Intervention de l'arbitrage vidéo : {VAR_FR.get(d, d)}. Équipe concernée : {team}."
    msg = f"{head}\n\n{body}\n\n📊 {sc}\n🏆 {comp}\n\n#Football #{comp.replace(' ', '')} #Direct"
    return msg, sc


def fetch_live_events(cfg, state):
    key = os.environ.get("API_FOOTBALL_KEY", "")
    live_cfg = cfg.get("live", {})
    if not live_cfg.get("enabled", True):
        return []
    if not key:
        log("[direct] API_FOOTBALL_KEY manquant : direct ignoré")
        return []
    # économie de requêtes (plan gratuit = 100/jour) : fenêtre horaire, intervalle minimal, plafond journalier
    now = datetime.now(timezone.utc)
    h0, h1 = live_cfg.get("hours_utc", [12, 23])
    if not (h0 <= now.hour <= h1):
        log(f"[direct] hors fenêtre horaire ({h0}h-{h1}h UTC) : aucune requête")
        return []
    lc = state.setdefault("live_calls", {"date": "", "n": 0, "last": ""})
    if lc.get("date") != now.date().isoformat():
        lc.update({"date": now.date().isoformat(), "n": 0})
    if lc["n"] >= live_cfg.get("max_calls_per_day", 90):
        log("[direct] plafond de requêtes du jour atteint")
        return []
    if lc.get("last"):
        gap = (now - datetime.fromisoformat(lc["last"])).total_seconds() / 60
        if gap < live_cfg.get("min_minutes_between_calls", 10) - 1:
            log(f"[direct] dernière requête il y a {gap:.0f} min : on attend")
            return []
    lc["n"] += 1
    lc["last"] = now.isoformat()
    try:
        data = apif_get("/fixtures?live=all", key)
    except Exception as e:  # noqa
        log(f"[direct] erreur API: {e}")
        return []
    if data.get("errors"):
        log(f"[direct] l'API signale : {data['errors']}")
    leagues = set(live_cfg.get("league_ids") or DEFAULT_LIVE_LEAGUES)
    seen_fx = state.setdefault("live_fixtures", [])
    kinds = set(live_cfg.get("kinds") or ["goal", "red", "var", "penmiss"])
    posts = []
    for fx in data.get("response", []):
        if fx["league"]["id"] not in leagues:
            continue
        fid = fx["fixture"]["id"]
        elapsed = (fx["fixture"].get("status") or {}).get("elapsed") or 0
        events = fx.get("events") or []
        first_sight = fid not in seen_fx
        if first_sight:
            seen_fx.append(fid)
        gh = ga = 0
        home_id = fx["teams"]["home"]["id"]
        for i, e in enumerate(events):
            kind = _evt_kind(e)
            if kind is None:
                continue
            tid = (e.get("team") or {}).get("id")
            if kind == "goal":
                scorer_home = (tid == home_id)
                if e.get("detail") == "Own Goal":
                    scorer_home = not scorer_home
                if scorer_home:
                    gh += 1
                else:
                    ga += 1
            if kind not in kinds:
                continue
            eid = f"ev-{fid}-{i}-{(e.get('time') or {}).get('elapsed')}-{kind}-{(e.get('player') or {}).get('id')}"
            # rattrapage : premier passage sur un match déjà bien avancé -> on n'inonde pas la page
            if first_sight and elapsed > live_cfg.get("catchup_minutes", 12):
                if eid not in state["posted_ids"]:
                    state["posted_ids"].append(eid)
                continue
            msg, sc = live_event_post(fx, e, kind, (gh, ga))
            if kind == "goal":
                hn, an = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
                if goal_seen(state, hn, an, gh + ga):
                    p_ = (e.get("player") or {}).get("name") or "un joueur"
                    a_ = (e.get("assist") or {}).get("name")
                    detail = f"ℹ️ Détails du but ({_minute(e)}) : {p_} ({(e.get('team') or {}).get('name','')})"
                    if a_:
                        detail += f", passe décisive de {a_}"
                    msg = f"{detail}.\n\n📊 {sc}\n\n#Football #Direct"
                else:
                    goal_mark(state, hn, an, gh + ga)
            posts.append({"id": eid, "type": "event", "title": sc, "message": msg})
    state["live_fixtures"] = seen_fx[-300:]
    return posts


def fetch_matches_apif(cfg, key, state):
    """Source de secours pour les scores/programme : convertit API-Football au format football-data."""
    leagues = set(cfg.get("live", {}).get("league_ids") or DEFAULT_LIVE_LEAGUES)
    today = datetime.now(timezone.utc).date()
    out = []
    lc = state.setdefault("live_calls", {"date": "", "n": 0, "last": ""})
    for d in (today - timedelta(days=1), today):
        try:
            data = apif_get(f"/fixtures?date={d}", key)
        except Exception as e:  # noqa
            log(f"[scores/secours] erreur API-Football: {e}")
            continue
        lc["n"] = lc.get("n", 0) + 1
        for fx in data.get("response", []):
            lg = fx["league"]
            if lg["id"] not in leagues:
                continue
            st = fx["fixture"]["status"]["short"]
            status = "FINISHED" if st in ("FT", "AET", "PEN") else ("TIMED" if st in ("NS", "TBD") else "OTHER")
            rnd = re.findall(r"\d+", lg.get("round") or "")
            h, a = fx["teams"]["home"], fx["teams"]["away"]
            out.append({"id": f"a{fx['fixture']['id']}", "status": status,
                        "utcDate": fx["fixture"]["date"], "matchday": int(rnd[-1]) if rnd else None,
                        "competition": {"name": lg["name"]},
                        "homeTeam": {"name": h["name"], "shortName": h["name"], "crest": h.get("logo")},
                        "awayTeam": {"name": a["name"], "shortName": a["name"], "crest": a.get("logo")},
                        "score": {"fullTime": {"home": fx["goals"]["home"], "away": fx["goals"]["away"]}}})
    return out


# ---------- anti-doublon d'actualités entre sources ----------
_STOP = {"le", "la", "les", "de", "du", "des", "un", "une", "et", "en", "à", "au", "aux", "pour", "sur", "par", "d", "l", "a"}


def _tokens(t):
    return {w for w in re.findall(r"\w+", t.lower()) if w not in _STOP and len(w) > 2}


def similar_title(t, others, thr=0.6):
    a = _tokens(t)
    for o in others:
        b = _tokens(o)
        if a and b and len(a & b) / len(a | b) >= thr:
            return True
    return False


# ---------- direct : 2e source (football-data.org) + anti-doublon ----------
def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("fc", "").replace("ac", ""))


def _same(a, b):
    import difflib
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() > 0.65)


def goal_seen(state, home, away, total):
    for k in state.get("goal_keys", []):
        if k["n"] == total and _same(k["h"], home) and _same(k["a"], away):
            return True
    return False


def goal_mark(state, home, away, total):
    keys = state.setdefault("goal_keys", [])
    keys.append({"h": home, "a": away, "n": total})
    state["goal_keys"] = keys[-300:]


def fetch_fd_live_goals(cfg, state):
    """Détecte les buts par changement de score (football-data.org, ~1 requête/min autorisée)."""
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "")
    live_cfg = cfg.get("live", {})
    if not token or not live_cfg.get("fd_enabled", True):
        return []
    h0, h1 = live_cfg.get("hours_utc", [12, 23])
    if not (h0 <= datetime.now(timezone.utc).hour <= h1):
        return []
    codes = ",".join(live_cfg.get("fd_competitions", ["FL1", "PL", "PD", "SA", "BL1", "CL", "WC"]))
    try:
        matches = fd_get(f"/matches?competitions={codes}&status=LIVE", token).get("matches", [])
    except Exception as e:  # noqa
        log(f"[direct-fd] erreur API: {e}")
        return []
    scores = state.setdefault("fd_scores", {})
    posts = []
    for m in matches:
        mid = str(m["id"])
        ft = (m.get("score") or {}).get("fullTime") or {}
        h, a = ft.get("home"), ft.get("away")
        if h is None or a is None:
            continue
        home = m["homeTeam"].get("shortName") or m["homeTeam"]["name"]
        away = m["awayTeam"].get("shortName") or m["awayTeam"]["name"]
        prev = scores.get(mid)
        scores[mid] = [h, a]
        if prev is None or (h + a) <= (prev[0] + prev[1]):
            continue  # première vue ou pas de nouveau but
        total = h + a
        if goal_seen(state, home, away, total):
            continue
        goal_mark(state, home, away, total)
        comp = m["competition"]["name"]
        who = home if h > prev[0] else away
        msg = (f"⚽ BUUUT ! {who} marque.\n\n📊 {home} {h} - {a} {away}\n🏆 {comp}\n\n"
               f"#Football #{comp.replace(' ', '')} #Direct")
        posts.append({"id": f"fdgoal-{mid}-{h}-{a}", "type": "event", "title": f"{home} {h} - {a} {away}", "message": msg})
    if len(scores) > 400:
        for k in list(scores)[:100]:
            scores.pop(k, None)
    return posts


# ---------- Facebook ----------
def post_facebook(post, show_preview=False):
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_TOKEN"]
    data = {"message": post["message"], "access_token": token}
    if show_preview and post.get("link"):
        data["link"] = post["link"]  # Facebook génère l'aperçu avec la photo de l'article
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{FB_API}/{page_id}/feed", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode("utf-8", "replace"))


def post_facebook_photo(path, caption):
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_TOKEN"]
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in {"caption": caption, "access_token": token}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    with open(path, "rb") as f:
        data = f.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="source"; filename="card.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode("utf-8") + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(f"{FB_API}/{page_id}/photos", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode("utf-8", "replace"))


# ---------- site web ----------
def build_site(cfg, state):
    os.makedirs(SITE_DIR, exist_ok=True)
    cards = []
    for h in reversed(state["history"][-60:]):
        title = html.escape(h["title"])
        when = html.escape(h["date"][:16].replace("T", " "))
        link = f'<a href="{html.escape(h["link"])}" target="_blank" rel="noopener">Lire la suite →</a>' if h.get("link") else ""
        body = html.escape(h["message"]).replace("\n", "<br>")
        img = (f'<img src="{html.escape(h["image"])}" alt="{title}" style="max-width:100%;border-radius:8px">'
               if h.get("image") else "")
        cards.append(f'<article class="{h["type"]}"><small>{when} UTC</small><h2>{title}</h2><p>{body}</p>{img}{link}</article>')
    fb = cfg.get("facebook_page_url", "")
    fb_btn = f'<a class="fb" href="{html.escape(fb)}" target="_blank" rel="noopener">Suivre sur Facebook</a>' if fb else ""
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(cfg['site_name'])}</title>
<style>
:root{{--g:#0b6e3a;--bg:#f4f7f5;--t:#14201a}}
*{{box-sizing:border-box}}body{{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--t)}}
header{{background:var(--g);color:#fff;padding:24px 16px;text-align:center}}
header h1{{margin:0 0 8px;font-size:1.6rem}}
.fb{{display:inline-block;margin-top:8px;background:#1877f2;color:#fff;padding:8px 16px;border-radius:20px;text-decoration:none}}
main{{max-width:720px;margin:16px auto;padding:0 16px;display:grid;gap:12px}}
article{{background:#fff;border-radius:12px;padding:14px 16px;border-left:5px solid var(--g);box-shadow:0 1px 3px #0001}}
article.score{{border-color:#d97706}}article.fixtures{{border-color:#2563eb}}article.standings{{border-color:#7c3aed}}
article h2{{font-size:1.05rem;margin:4px 0 8px}}article p{{margin:0 0 8px;font-size:.95rem;line-height:1.45}}
small{{color:#667}}a{{color:var(--g)}}
</style></head><body>
<header><h1>⚽ {html.escape(cfg['site_name'])}</h1><div>{html.escape(cfg.get('tagline',''))}</div>{fb_btn}</header>
<main>{''.join(cards) or '<p>Aucune publication pour le moment.</p>'}</main>
</body></html>"""
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ---------- principal ----------
def run_once(args, cfg, state, mode):
    posts = [p for p in build_posts(cfg, mode, state) if p["id"] not in state["posted_ids"]]
    # Priorité : programme, scores, classements, puis actualités
    order = {"fixtures": 0, "score": 1, "standings": 2, "news": 3}
    posts.sort(key=lambda p: order.get(p["type"], 9))
    # Plafond par passage et par jour (hors événements en direct, qui ont leurs propres plafonds)
    today = datetime.now(timezone.utc).date().isoformat()
    done_today = sum(1 for h in state["history"]
                     if h.get("date", "")[:10] == today and h.get("type") != "event")
    remaining = max(0, cfg.get("max_posts_per_day", 20) - done_today)
    posts = posts[: min(cfg["max_posts_per_run"], remaining)]

    if mode in ("all", "live"):
        live_cfg = cfg.get("live", {})
        evs = fetch_fd_live_goals(cfg, state) + fetch_live_events(cfg, state)
        evs = [p for p in evs if p["id"] not in state["posted_ids"]]
        ev_today = sum(1 for h in state["history"]
                       if h.get("date", "")[:10] == today and h.get("type") == "event")
        ev_left = max(0, live_cfg.get("max_events_per_day", 80) - ev_today)
        evs = evs[: min(live_cfg.get("max_events_per_run", 8), ev_left)]
        posts = evs + posts  # le direct passe en premier
    if posts or mode != "live":
        log(f"{len(posts)} nouvelle(s) publication(s) à traiter")

    for p in posts:
        if args.dry_run:
            log(f"--- [SIMULATION] {p['type']} ---\n{p['message']}\n{p.get('link','')}\n"
                + (f"[image : docs/{p['image']}]\n" if p.get("image") else ""))
            continue
        if not (os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN")):
            log("FB_PAGE_ID / FB_PAGE_TOKEN manquants : publication Facebook ignorée (site mis à jour seulement)")
        else:
            try:
                if p.get("image"):
                    res = post_facebook_photo(os.path.join(SITE_DIR, p["image"]), p["message"])
                else:
                    res = post_facebook(p, cfg.get("show_link_preview", False))
                log(f"Publié sur Facebook : {res.get('id')}")
            except Exception as e:  # noqa
                log(f"Échec Facebook pour {p['id']}: {e}")
                continue
        state["posted_ids"].append(p["id"])
        state["history"].append({**p, "date": datetime.now(timezone.utc).isoformat()})

    state["posted_ids"] = state["posted_ids"][-2000:]
    state["history"] = state["history"][-200:]
    if not args.dry_run:
        save_json(STATE_FILE, state)
    build_site(cfg, state)
    return len(posts)


def git_push():
    """Enregistre l'état et le site (utilisé dans la boucle longue sur GitHub Actions)."""
    try:
        run = lambda *a: subprocess.run(a, cwd=BASE, check=False, capture_output=True, text=True)
        run("git", "config", "user.name", "foot-bot")
        run("git", "config", "user.email", "bot@users.noreply.github.com")
        run("git", "add", "state.json", "docs")
        if run("git", "diff", "--cached", "--quiet").returncode != 0:
            run("git", "commit", "-m", "maj auto")
            run("git", "pull", "--rebase", "-q")
            r = run("git", "push")
            log("[git] état enregistré" if r.returncode == 0 else f"[git] push refusé : {r.stderr.strip()[:200]}")
    except Exception as e:  # noqa
        log(f"[git] erreur : {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien à Facebook")
    ap.add_argument("--mode", default="all", choices=["all", "news", "scores", "standings", "live"])
    ap.add_argument("--loop-minutes", type=int, default=0,
                    help="reste actif ce nombre de minutes et vérifie le direct en boucle")
    ap.add_argument("--interval", type=int, default=60, help="secondes entre deux vérifications du direct")
    ap.add_argument("--full-every", type=int, default=30, help="minutes entre deux passages complets (actus, scores…)")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("config.json introuvable")
    state = load_json(STATE_FILE, {"posted_ids": [], "history": []})

    if not args.loop_minutes:
        run_once(args, cfg, state, args.mode)
        log("Site généré dans docs/index.html")
        return

    import time
    end = time.time() + args.loop_minutes * 60
    last_full = last_push = 0.0
    log(f"Boucle de {args.loop_minutes} min : direct toutes les {args.interval}s, passage complet toutes les {args.full_every} min")
    while time.time() < end:
        t0 = time.time()
        full = (t0 - last_full) >= args.full_every * 60
        try:
            cfg = load_json(CONFIG_FILE, cfg)
            n = run_once(args, cfg, state, "all" if full else "live")
        except Exception as e:  # noqa
            log(f"[boucle] erreur ignorée : {e}")
            n = 0
        if full:
            last_full = t0
        if (n or full) and os.environ.get("GITHUB_ACTIONS") and not args.dry_run and (time.time() - last_push) > 600:
            git_push()
            last_push = time.time()
        time.sleep(max(5, args.interval - (time.time() - t0)))
    if os.environ.get("GITHUB_ACTIONS") and not args.dry_run:
        git_push()
    log("Fin de la boucle")


if __name__ == "__main__":
    main()
