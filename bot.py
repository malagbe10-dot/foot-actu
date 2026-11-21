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


# ---------- visuels générés (aucune photo de tiers) ----------
BRAND = "FOOTBALL SPOTLIGHT"
GREEN_D, GREEN_M, GREEN_L, WHITE, CHALK = (8, 52, 31), (11, 74, 43), (18, 98, 60), (255, 255, 255), (190, 230, 205)


def _wrap(d, text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_score_card(c, path):
    """Carte de score (fin de match ou but) : logos, score en grand, championnat."""
    from PIL import Image, ImageDraw
    W, H = 1080, 700
    img = Image.new("RGB", (W, H), GREEN_M)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 150], fill=GREEN_D)
    d.text((W // 2, 75), c["label"], font=load_font(66), fill=WHITE, anchor="mm")
    cy = 330
    for side, team, cx in (("home", c["home"], 230), ("away", c["away"], 850)):
        crest = fetch_crest(team.get("crest"), 150)
        if crest:
            img.paste(crest, (cx - crest.width // 2, cy - crest.height // 2 - 20), crest)
        else:
            d.ellipse([cx - 75, cy - 95, cx + 75, cy + 55], fill=GREEN_D, outline=CHALK, width=4)
            d.text((cx, cy - 20), (team.get("tla") or team["name"][:3]).upper(), font=load_font(52), fill=WHITE, anchor="mm")
        d.text((cx, cy + 115), team["name"], font=fit_font(d, team["name"], 340, 38), fill=WHITE, anchor="mm")
    d.text((W // 2, cy - 10), f"{c['h']} - {c['a']}", font=load_font(120), fill=WHITE, anchor="mm")
    d.text((W // 2, 560), c["comp"], font=load_font(32, False), fill=CHALK, anchor="mm")
    d.text((W // 2, 640), c.get("brand", BRAND), font=load_font(30), fill=CHALK, anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


THEMES = [
    (("transfert", "mercato", "recrut", "signe", "prêt", "contrat", "prolong"), "a football player signing a contract at a desk in a club office, camera flashes"),
    (("blessure", "blessé", "forfait", "absent", "indisponible"), "a football pitch at dusk with a lone ball and a medical bag on the grass, dramatic mood"),
    (("but", "buteur", "marque", "score", "victoire", "gagne", "remporte", "succès"), "players celebrating a goal in a packed stadium, confetti and floodlights"),
    (("entraîneur", "coach", "technicien", "limogé", "banc"), "a football coach silhouette on the touchline giving instructions, stadium lights behind"),
    (("finale", "coupe", "trophée", "titre", "champion"), "a golden football trophy on a pedestal in a stadium with spotlights"),
    (("arbitre", "var", "penalty", "carton", "expuls"), "a referee silhouette holding a card on a floodlit pitch"),
    (("sélection", "équipe nationale", "mondial", "coupe du monde", "qualif", "bleus"), "a national team stadium with flags waving in the stands at night"),
]


def theme_prompt(title):
    t = (title or "").lower()
    scene = "a football stadium at night, bright floodlights, ball on the grass"
    for keys, sc in THEMES:
        if any(k in t for k in keys):
            scene = sc
            break
    return ("cinematic football illustration, " + scene + ", dark green and blue tones, "
            "no text, no letters, no logos, no real people, no faces, silhouettes only")


def fetch_ai_background(title, size=1080):
    """Illustration générée (service gratuit sans clé). Retourne une image PIL ou None si indisponible."""
    from PIL import Image
    import hashlib
    seed = int(hashlib.md5((title or "").encode()).hexdigest()[:6], 16)
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(theme_prompt(title))
           + f"?width={size}&height={size}&nologo=true&seed={seed}")
    try:
        im = Image.open(io.BytesIO(http_get(url, timeout=60))).convert("RGB")
        return im.resize((size, size))
    except Exception as e:  # noqa
        log(f"[visuel] illustration IA indisponible, carte simple utilisée : {e}")
        return None


def make_news_card(c, path):
    """Carte d'actualité : le titre en grand sur fond aux couleurs de la page."""
    from PIL import Image, ImageDraw
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), GREEN_M)
    bg = fetch_ai_background(c["title"]) if c.get("ai") else None
    if bg is not None:
        # voile sombre pour que le titre reste lisible sur l'illustration
        img = Image.blend(bg, Image.new("RGB", (W, H), (4, 28, 17)), 0.55)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 130], fill=GREEN_D)
    d.text((W // 2, 65), c.get("brand", BRAND), font=load_font(46), fill=WHITE, anchor="mm")
    size, lines = 76, []
    while size >= 40:
        f = load_font(size)
        lines = _wrap(d, c["title"], f, W - 140)
        if len(lines) * (size + 14) <= 640:
            break
        size -= 4
    lines = lines[:8]
    y = 160 + (680 - len(lines) * (size + 14)) // 2
    for ln in lines:
        d.text((W // 2, y), ln, font=load_font(size), fill=WHITE, anchor="mt")
        y += size + 14
    d.rounded_rectangle([60, 860, W - 60, 940], radius=18, fill=GREEN_L)
    d.text((W // 2, 900), f"Source : {c['source']}", font=load_font(34, False), fill=CHALK, anchor="mm")
    d.text((W // 2, 1010), "Actus, scores et direct du football", font=load_font(30, False), fill=CHALK, anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


RED = (200, 30, 40)


def _pitch(d, x0, y0, x1, y1):
    """Terrain de football vu de dessus (dessiné)."""
    d.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=(22, 118, 66), outline=WHITE, width=4)
    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
    d.line([mx, y0, mx, y1], fill=WHITE, width=3)
    d.ellipse([mx - 60, my - 60, mx + 60, my + 60], outline=WHITE, width=3)
    bw, bh, sw, sh = 130, 250, 50, 130
    for sx, dirx in ((x0, 1), (x1, -1)):
        d.rectangle([min(sx, sx + dirx * bw), my - bh // 2, max(sx, sx + dirx * bw), my + bh // 2], outline=WHITE, width=3)
        d.rectangle([min(sx, sx + dirx * sw), my - sh // 2, max(sx, sx + dirx * sw), my + sh // 2], outline=WHITE, width=3)
    return mx, my


def make_event_card(c, path):
    """But, penalty raté, carton rouge, VAR : schéma de terrain + joueur + minute + score."""
    from PIL import Image, ImageDraw
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), GREEN_M)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 150], fill=GREEN_D)
    d.text((W // 2, 75), c["label"], font=fit_font(d, c["label"], 600, 70), fill=WHITE, anchor="mm")
    d.rounded_rectangle([W - 230, 40, W - 40, 110], radius=18, fill=GREEN_L)
    d.text((W - 135, 75), c["minute"], font=load_font(40), fill=WHITE, anchor="mm")
    x0, y0, x1, y1 = 70, 190, W - 70, 640
    mx, my = _pitch(d, x0, y0, x1, y1)
    t = c["type"]
    if t in ("goal", "penmiss"):
        gx = x1 - 60 if c["side"] == "home" else x0 + 60  # l'équipe locale attaque à droite
        d.ellipse([gx - 34, my - 34, gx + 34, my + 34], fill=WHITE, outline=GREEN_D, width=5)
        d.regular_polygon((gx, my, 16), 5, fill=GREEN_D) if hasattr(d, "regular_polygon") else None
        if t == "penmiss":
            d.line([gx - 50, my - 50, gx + 50, my + 50], fill=RED, width=12)
            d.line([gx - 50, my + 50, gx + 50, my - 50], fill=RED, width=12)
    elif t == "red":
        d.rounded_rectangle([mx - 55, my - 85, mx + 55, my + 85], radius=12, fill=RED, outline=WHITE, width=5)
    else:  # var
        d.rounded_rectangle([mx - 130, my - 70, mx + 130, my + 70], radius=18, fill=GREEN_D, outline=WHITE, width=5)
        d.text((mx, my), "VAR", font=load_font(80), fill=WHITE, anchor="mm")
    d.text((W // 2, 725), c["player"], font=fit_font(d, c["player"], W - 140, 76), fill=WHITE, anchor="mm")
    sub = c["team"] + (f" · passe de {c['assist']}" if c.get("assist") else "")
    d.text((W // 2, 800), sub, font=fit_font(d, sub, W - 140, 38, False), fill=CHALK, anchor="mm")
    # ligne de score avec logos
    cy = 895
    for team, cx in ((c["home"], 190), (c["away"], 890)):
        cr = fetch_crest(team.get("crest"), 90)
        if cr:
            img.paste(cr, (cx - cr.width // 2, cy - cr.height // 2), cr)
        else:
            d.ellipse([cx - 45, cy - 45, cx + 45, cy + 45], fill=GREEN_D, outline=CHALK, width=3)
            d.text((cx, cy), team["name"][:3].upper(), font=load_font(30), fill=WHITE, anchor="mm")
    d.text((W // 2, cy), f"{c['h']} - {c['a']}", font=load_font(96), fill=WHITE, anchor="mm")
    d.text((W // 2, 985), f"{c['home']['name']}  ·  {c['away']['name']}", font=fit_font(d, f"{c['home']['name']}  ·  {c['away']['name']}", W - 120, 30, False), fill=CHALK, anchor="mm")
    d.text((W // 2, 1035), f"{c['comp']}  |  {c.get('brand', BRAND)}", font=load_font(26, False), fill=CHALK, anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def make_table_card(c, path):
    """Classement : tableau avec logos."""
    from PIL import Image, ImageDraw
    rows = c["rows"][:10]
    W, top, rh = 1080, 230, 84
    H = top + rh * len(rows) + 100
    img = Image.new("RGB", (W, H), GREEN_M)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, top - 30], fill=GREEN_D)
    d.text((W // 2, 70), "CLASSEMENT", font=load_font(60), fill=WHITE, anchor="mm")
    d.text((W // 2, 140), c["comp"], font=fit_font(d, c["comp"], W - 100, 40, False), fill=CHALK, anchor="mm")
    d.text((760, top - 12), "J", font=load_font(26), fill=CHALK, anchor="mm")
    d.text((940, top - 12), "PTS", font=load_font(26), fill=CHALK, anchor="mm")
    y = top + 14
    for r in rows:
        d.rounded_rectangle([36, y, W - 36, y + rh - 10], radius=14, fill=GREEN_L if r["pos"] % 2 else (14, 88, 52))
        cy = y + (rh - 10) // 2
        d.text((86, cy), str(r["pos"]), font=load_font(34), fill=WHITE, anchor="mm")
        cr = fetch_crest(r.get("crest"), 54)
        if cr:
            img.paste(cr, (150 - cr.width // 2, cy - cr.height // 2), cr)
        d.text((200, cy), r["name"], font=fit_font(d, r["name"], 450, 34), fill=WHITE, anchor="lm")
        d.text((760, cy), str(r["played"]), font=load_font(32, False), fill=CHALK, anchor="mm")
        d.text((940, cy), str(r["pts"]), font=load_font(40), fill=WHITE, anchor="mm")
        y += rh
    d.text((W // 2, H - 45), c.get("brand", BRAND), font=load_font(30), fill=CHALK, anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def render_card(p, cfg):
    """Crée l'image d'une publication si elle a une carte demandée (et si c'est activé)."""
    c = p.pop("card", None)
    if not c or not cfg.get("images", {}).get(c["kind"], True) or not ensure_pillow():
        return
    c["brand"] = cfg.get("brand_name", BRAND).upper()
    c["ai"] = bool(cfg.get("images", {}).get("ai", True))
    rel = "cards/" + re.sub(r"[^A-Za-z0-9_-]", "_", p["id"])[:80] + ".png"
    try:
        {"news": make_news_card, "event": make_event_card, "table": make_table_card, "lineup": make_lineup_card}.get(c["kind"], make_score_card)(c, os.path.join(SITE_DIR, rel))
        p["image"] = rel
    except Exception as e:  # noqa
        log(f"[visuel] échec de l'image : {e}")


def clean_cards(keep=80):
    d = os.path.join(SITE_DIR, "cards")
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
        for f in files[:-keep]:
            os.remove(f)
    except OSError:
        pass


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
                          "link": n["link"],
                          "card": {"kind": "news", "title": title, "source": n["source"]}})
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
                                  "message": f"⏱️ TERMINÉ\n\n⚽ {line.upper()}\n\n{result_text(m)}\n🏆 {comp}{journee}\n\n#Football #{comp.replace(' ', '')}",
                                  "card": {"kind": "score", "label": "SCORE FINAL", "comp": comp,
                                           "home": m["homeTeam"], "away": m["awayTeam"],
                                           "h": m["score"]["fullTime"]["home"], "a": m["score"]["fullTime"]["away"]}})
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
                          "message": f"📊 Classement {name} (top 10)\n\n" + "\n".join(rows) + detail + "\n\n#Football",
                          "card": {"kind": "table", "comp": name,
                                   "rows": [{"pos": r["position"], "name": r["team"]["shortName"] or r["team"]["name"],
                                             "crest": r["team"].get("crest"), "played": r["playedGames"], "pts": r["points"]}
                                            for r in table]}})
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
        if not state.get("_out_logged"):
            log(f"[direct] hors fenêtre horaire ({h0}h-{h1}h UTC) : pause jusqu'à {h0}h")
            state["_out_logged"] = True
        return []
    state.pop("_out_logged", None)
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
            shooter_home = (tid == home_id)
            if kind == "goal" and e.get("detail") == "Own Goal":
                shooter_home = not shooter_home
            lab = {"goal": "BUUUT !", "penmiss": "PENALTY RATÉ", "red": "CARTON ROUGE", "var": "VAR"}[kind]
            if kind == "goal" and e.get("detail") == "Own Goal":
                lab = "CONTRE SON CAMP"
            elif kind == "goal" and e.get("detail") == "Penalty":
                lab = "BUT SUR PENALTY"
            card = {"kind": "event", "type": kind, "label": lab, "minute": _minute(e),
                    "player": (e.get("player") or {}).get("name") or (e.get("team") or {}).get("name", ""),
                    "assist": (e.get("assist") or {}).get("name"), "team": (e.get("team") or {}).get("name", ""),
                    "side": "home" if shooter_home else "away", "comp": fx["league"]["name"], "h": gh, "a": ga,
                    "home": {"name": fx["teams"]["home"]["name"], "crest": fx["teams"]["home"].get("logo")},
                    "away": {"name": fx["teams"]["away"]["name"], "crest": fx["teams"]["away"].get("logo")}}
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
            posts.append({"id": eid, "type": "event", "title": sc, "message": msg, "card": card})
    state["live_fixtures"] = seen_fx[-300:]
    return posts


# ---------- compositions d'équipe (API-Football) ----------
POS_FR = {"G": "Gardien", "D": "Défenseurs", "M": "Milieux", "F": "Attaquants"}


def _short_name(n):
    parts = (n or "").replace(".", ". ").split()
    return (parts[-1] if parts else "?")[:12]


def make_lineup_card(c, path):
    """Terrain dessiné avec les deux onzes de départ, placés selon la formation."""
    from PIL import Image, ImageDraw
    W, H = 1080, 1500
    img = Image.new("RGB", (W, H), GREEN_M)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 170], fill=GREEN_D)
    d.text((W // 2, 55), "COMPOSITIONS", font=load_font(56), fill=WHITE, anchor="mm")
    title = f"{c['home']['name']}  vs  {c['away']['name']}"
    d.text((W // 2, 110), title, font=fit_font(d, title, W - 80, 40), fill=CHALK, anchor="mm")
    d.text((W // 2, 150), f"{c['comp']}  ·  {c['time']}", font=load_font(26, False), fill=CHALK, anchor="mm")
    x0, y0, x1, y1 = 50, 200, W - 50, 1400
    d.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=(22, 118, 66), outline=WHITE, width=4)
    my, mx = (y0 + y1) // 2, (x0 + x1) // 2
    d.line([x0, my, x1, my], fill=WHITE, width=3)
    d.ellipse([mx - 90, my - 90, mx + 90, my + 90], outline=WHITE, width=3)
    for gy, dy in ((y0, 1), (y1, -1)):
        d.rectangle([mx - 190, min(gy, gy + dy * 160), mx + 190, max(gy, gy + dy * 160)], outline=WHITE, width=3)
        d.rectangle([mx - 90, min(gy, gy + dy * 60), mx + 90, max(gy, gy + dy * 60)], outline=WHITE, width=3)
    half = (y1 - y0) // 2
    for side in ("home", "away"):
        t = c[side]
        rows = {}
        for p in t["players"]:
            rows.setdefault(p["row"], []).append(p)
        nrows = max(rows) if rows else 1
        fill = WHITE if side == "home" else (250, 204, 21)
        for r, plist in rows.items():
            plist.sort(key=lambda p: p["col"])
            frac = (r - 0.5) / nrows
            y = (y1 - 40 - frac * (half - 60)) if side == "home" else (y0 + 40 + frac * (half - 60))
            n = len(plist)
            for i, p in enumerate(plist):
                x = x0 + (i + 0.5) * (x1 - x0) / n
                d.ellipse([x - 30, y - 30, x + 30, y + 30], fill=fill, outline=GREEN_D, width=4)
                d.text((x, y), str(p["num"]), font=load_font(28), fill=GREEN_D, anchor="mm")
                nm = _short_name(p["name"])
                d.text((x, y + 46), nm, font=fit_font(d, nm, (x1 - x0) / n - 8, 26), fill=WHITE, anchor="mm",
                       stroke_width=3, stroke_fill=GREEN_D)
        tag = f"{t['name']} · {t['formation']}" if t.get("formation") else t["name"]
        ty = y1 + 40 if side == "home" else y0 - 22
        if side == "away":
            ty = 182
        d.text((x0 + 8, y1 + 40) if side == "home" else (x0 + 8, 182), tag, font=fit_font(d, tag, 600, 30),
               fill=fill, anchor="lm")
    d.text((W // 2, 1465), c.get("brand", BRAND), font=load_font(30), fill=CHALK, anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def fetch_lineups(cfg, state):
    """Compositions publiées environ 50, 30 puis 12 min avant le coup d'envoi (quota dédié)."""
    key = os.environ.get("API_FOOTBALL_KEY", "")
    lcfg = cfg.get("live", {}).get("lineups", {})
    if not key or not lcfg.get("enabled", True):
        return []
    now = datetime.now(timezone.utc)
    h0, h1 = cfg.get("live", {}).get("hours_utc", [12, 23])
    if not (max(0, h0 - 2) <= now.hour <= h1):
        return []
    today = now.date().isoformat()
    lc = state.setdefault("lineup_calls", {"date": "", "n": 0, "last": ""})
    if lc.get("date") != today:
        lc.update({"date": today, "n": 0})
    cap = lcfg.get("max_calls_per_day", 30)
    if lc["n"] >= cap:
        return []
    if lc.get("last") and (now - datetime.fromisoformat(lc["last"])).total_seconds() < 240:
        return []
    plan = state.setdefault("lineup_plan", {"date": "", "fixtures": [], "tries": {}})
    leagues = set(cfg.get("live", {}).get("league_ids") or DEFAULT_LIVE_LEAGUES)
    if plan.get("date") != today:
        try:
            data = apif_get(f"/fixtures?date={today}", key)
        except Exception as e:  # noqa
            log(f"[compositions] erreur API: {e}")
            return []
        lc["n"] += 1
        plan.update({"date": today, "tries": {}, "fixtures": [
            {"id": f["fixture"]["id"], "ts": f["fixture"]["timestamp"], "comp": f["league"]["name"],
             "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"]}
            for f in data.get("response", []) if f["league"]["id"] in leagues]})
        log(f"[compositions] {len(plan['fixtures'])} match(s) suivis aujourd'hui")
        return []
    marks = lcfg.get("minutes_before", [50, 30, 12])
    posts = []
    for f in plan["fixtures"]:
        fid = str(f["id"])
        if f"lineup-{fid}" in state["posted_ids"]:
            continue
        left = (f["ts"] - now.timestamp()) / 60
        if left <= 0 or left > marks[0]:
            continue
        step = sum(1 for m in marks if left <= m)  # 1, 2 ou 3 : étape atteinte
        if plan["tries"].get(fid, 0) >= step or lc["n"] >= cap:
            continue
        plan["tries"][fid] = step
        lc["n"] += 1
        lc["last"] = now.isoformat()
        try:
            res = apif_get(f"/fixtures/lineups?fixture={fid}", key).get("response", [])
        except Exception as e:  # noqa
            log(f"[compositions] erreur API: {e}")
            continue
        if len(res) < 2:
            continue
        teams = []
        for t in res:
            pl = []
            for it in t.get("startXI", []):
                p = it["player"]
                try:
                    r_, c_ = (int(x) for x in (p.get("grid") or "1:1").split(":"))
                except ValueError:
                    r_, c_ = 1, 1
                pl.append({"num": p.get("number"), "name": p.get("name"), "row": r_, "col": c_, "pos": p.get("pos")})
            teams.append({"name": t["team"]["name"], "crest": t["team"].get("logo"),
                          "formation": t.get("formation"), "coach": (t.get("coach") or {}).get("name"), "players": pl})
        if not all(t["players"] for t in teams):
            continue
        ko = datetime.fromtimestamp(f["ts"], timezone.utc).strftime("%H:%M UTC")
        lines = [f"📋 COMPOSITIONS – {teams[0]['name']} vs {teams[1]['name']}", f"🏆 {f['comp']} · ⏰ {ko}", ""]
        for icon, t in (("🏠", teams[0]), ("✈️", teams[1])):
            lines.append(f"{icon} {t['name']}" + (f" ({t['formation']})" if t.get("formation") else ""))
            for pos in ("G", "D", "M", "F"):
                names = [f"{p['name']}" for p in sorted(t["players"], key=lambda p: (p["row"], p["col"])) if p["pos"] == pos]
                if names:
                    lines.append(f"{POS_FR[pos]} : " + ", ".join(names))
            if t.get("coach"):
                lines.append(f"🧑‍💼 Entraîneur : {t['coach']}")
            lines.append("")
        lines.append(f"#Football #{f['comp'].replace(' ', '')}")
        posts.append({"id": f"lineup-{fid}", "type": "event", "title": f"Compositions {teams[0]['name']} - {teams[1]['name']}",
                      "message": "\n".join(lines),
                      "card": {"kind": "lineup", "comp": f["comp"], "time": ko, "home": teams[0], "away": teams[1]}})
        break  # une composition à la fois (économie de requêtes)
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
        posts.append({"id": f"fdgoal-{mid}-{h}-{a}", "type": "event", "title": f"{home} {h} - {a} {away}", "message": msg,
                      "card": {"kind": "goal", "label": "BUUUT !", "comp": comp,
                               "home": m["homeTeam"], "away": m["awayTeam"], "h": h, "a": a}})
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
        evs = fetch_lineups(cfg, state) + fetch_fd_live_goals(cfg, state) + fetch_live_events(cfg, state)
        evs = [p for p in evs if p["id"] not in state["posted_ids"]]
        ev_today = sum(1 for h in state["history"]
                       if h.get("date", "")[:10] == today and h.get("type") == "event")
        ev_left = max(0, live_cfg.get("max_events_per_day", 80) - ev_today)
        evs = evs[: min(live_cfg.get("max_events_per_run", 8), ev_left)]
        posts = evs + posts  # le direct passe en premier
    if posts or mode != "live":
        log(f"{len(posts)} nouvelle(s) publication(s) à traiter")

    for p in posts:
        render_card(p, cfg)
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
    clean_cards(cfg.get("images", {}).get("keep", 80))
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
