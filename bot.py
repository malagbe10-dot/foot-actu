#!/usr/bin/env python3
"""Bot football : récupère actus + scores + classements,
publie sur une page Facebook et génère un site statique (docs/index.html).

Aucune dépendance externe (bibliothèque standard Python 3.9+).
Usage :
    python bot.py --dry-run          # simulation, ne publie rien
    python bot.py                    # publie sur Facebook + met à jour le site
    python bot.py --mode news        # seulement les actus (news | scores | standings | all)
"""
import argparse
import html
import json
import os
import sys
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
            items.append({"id": "news-" + link, "title": title, "link": link,
                          "source": feed["name"], "date": date})
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


def build_posts(cfg, mode, state):
    posts = []
    tz = timezone(timedelta(hours=cfg.get("utc_offset_hours", 0)))
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "")

    if mode in ("news", "all"):
        for n in fetch_news(cfg)[: cfg["news_per_run"]]:
            posts.append({"id": n["id"], "type": "news", "title": n["title"],
                          "message": f"⚽ {n['title']}\n\nSource : {n['source']}",
                          "link": n["link"]})

    if mode in ("scores", "all"):
        if not token:
            log("[scores] FOOTBALL_DATA_TOKEN manquant : scores ignorés")
        else:
            try:
                matches = fetch_matches(cfg, token)
            except Exception as e:  # noqa
                log(f"[scores] erreur API: {e}")
                matches = []
            for m in matches:
                if m["status"] == "FINISHED":
                    line = score_line(m)
                    comp = m["competition"]["name"]
                    posts.append({"id": f"result-{m['id']}", "type": "score", "title": line,
                                  "message": f"🏁 Score final – {comp}\n\n{line}\n\n#Football #{comp.replace(' ', '')}"})
            # programme du jour (une fois par jour)
            today = datetime.now(tz).date()
            todays = [m for m in matches
                      if m["status"] in ("TIMED", "SCHEDULED")
                      and datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(tz).date() == today]
            if todays:
                lines = []
                for m in todays:
                    h = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")
                    lines.append(f"🕒 {h} – {m['homeTeam']['shortName'] or m['homeTeam']['name']} vs {m['awayTeam']['shortName'] or m['awayTeam']['name']}")
                posts.append({"id": f"fixtures-{today}", "type": "fixtures",
                              "title": f"Programme du {today}",
                              "message": "📅 Au programme aujourd'hui\n\n" + "\n".join(lines[:15]) + "\n\n#Football"})

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
            rows = [f"{r['position']}. {r['team']['shortName'] or r['team']['name']} – {r['points']} pts" for r in table]
            posts.append({"id": pid, "type": "standings", "title": f"Classement {name}",
                          "message": f"📊 Classement {name} (top 10)\n\n" + "\n".join(rows) + "\n\n#Football"})
    return posts


# ---------- Facebook ----------
def post_facebook(post):
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_TOKEN"]
    data = {"message": post["message"], "access_token": token}
    if post.get("link"):
        data["link"] = post["link"]  # Facebook génère l'aperçu avec la photo de l'article
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{FB_API}/{page_id}/feed", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
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
        cards.append(f'<article class="{h["type"]}"><small>{when} UTC</small><h2>{title}</h2><p>{body}</p>{link}</article>')
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
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien à Facebook")
    ap.add_argument("--mode", default="all", choices=["all", "news", "scores", "standings"])
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("config.json introuvable")
    state = load_json(STATE_FILE, {"posted_ids": [], "history": []})

    posts = [p for p in build_posts(cfg, args.mode, state) if p["id"] not in state["posted_ids"]]
    posts = posts[: cfg["max_posts_per_run"]]
    log(f"{len(posts)} nouvelle(s) publication(s) à traiter")

    for p in posts:
        if args.dry_run:
            log(f"--- [SIMULATION] {p['type']} ---\n{p['message']}\n{p.get('link','')}\n")
            continue
        if not (os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN")):
            log("FB_PAGE_ID / FB_PAGE_TOKEN manquants : publication Facebook ignorée (site mis à jour seulement)")
        else:
            try:
                res = post_facebook(p)
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
    log("Site généré dans docs/index.html")


if __name__ == "__main__":
    main()
