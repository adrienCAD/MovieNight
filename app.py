#!/usr/bin/env python3
"""Browser front end for the movie picker.  Run:  python3 app.py   then open http://localhost:8765"""
import csv
import datetime
import json
import os
import random
import re
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import recommend as R

PORT = 8765
RATINGS = R.HERE / "ratings.csv"
IMG = "https://image.tmdb.org/t/p/"
MYLIST = R.HERE / "mylist.json"  # "maybe later" picks, stored as the card data shown in the UI
list_lock = threading.Lock()  # mylist.json is read-modify-written from several requests
csv_lock = threading.Lock()  # ratings.csv rewrites must not wait behind a slow recommendation
lock = threading.Lock()  # one recommendation at a time (the local model isn't thread-safe)


def tmdb_post(path, body, **params):
    params["api_key"] = os.environ["TMDB_API_KEY"]
    req = urllib.request.Request(
        f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def synopsis(m):
    """TMDB's premise text, cut to two sentences so nothing past the setup sneaks in."""
    sentences = re.split(r"(?<=[.!?])\s+", (m.get("overview") or "").strip())
    return " ".join(sentences[:2])


def details(item, year, genres, why, kind, services):
    m = R.as_movie(R.tmdb(f"/{kind}/{item['id']}", append_to_response="credits,watch/providers"))
    flatrate = m["watch/providers"]["results"].get(R.REGION, {}).get("flatrate", [])
    provider_ids = {p["provider_id"] for p in flatrate}
    return {
        "id": item["id"],
        "kind": kind,
        "title": item["title"],
        "year": year,
        "score": round(item["vote_average"], 1),
        "genres": genres,
        "runtime": m.get("runtime") or (m.get("episode_run_time") or [None])[0],
        "seasons": m.get("number_of_seasons"),
        "tagline": m.get("tagline"),
        "synopsis": synopsis(m),
        "why": why,
        "streaming": [s for s in services if R.SERVICES[s] in provider_ids],  # only the services you selected
        "poster": f"{IMG}w780{m['poster_path']}" if m.get("poster_path") else None,
        "cast": [
            {"name": c["name"], "role": c["character"],
             "photo": f"{IMG}w185{c['profile_path']}" if c.get("profile_path") else None}
            for c in m["credits"]["cast"][:5]
        ],
    }


def recommend(req):
    kind = "tv" if req.get("kind") == "tv" else "movie"
    styles = [s for s in req.get("styles", []) if s in R.STYLES]
    group = [R.PEOPLE[k] for k in req["people"] if k in R.PEOPLE] or list(R.PEOPLE.values())
    moods = [k for k in req["moods"] if k in R.MOODS and k != "7"]
    mood_label = " + ".join(R.MOODS[k][0] for k in moods) or R.MOODS["7"][0]
    mood_genres = [g for k in moods for g in R.MOODS[k][1]]
    label = f"{' + '.join(n for n, _ in group)} / {mood_label}"
    services = [s for s in req["services"] if s in R.SERVICES] or R.DEFAULT_SERVICES
    R.save_config(services=services)
    exclude = set(req.get("exclude", []))  # ids already shown, for the "new picks" button

    ratings = R.load_ratings(RATINGS)
    seen = R.seen_titles(ratings) | {(R.norm(e["title"]), e["year"]) for e in load_list()}
    profile = R.taste_profile(ratings)
    genre_names = {g["id"]: g["name"] for g in R.tmdb(f"/genre/{kind}/list")["genres"]}
    params, rules = R.build_query(services, group, mood_genres, genre_names, kind, req.get("max_cert", "auto"), styles)

    with lock:
        R.CANCEL.clear()  # a Stop pressed while nothing was running must not kill this run
        try:
            return run(req, ratings, seen, profile, params, rules, genre_names, kind, styles, label, mood_label, services, exclude)
        except R.Cancelled:
            return {"cancelled": True}


def run(req, ratings, seen, profile, params, rules, genre_names, kind, styles, label, mood_label, services, exclude):
    ranked = [c for c in R.candidates_for(seen, profile, params, rules, genre_names, kind, styles) if c[1]["id"] not in exclude]
    summary = R.taste_summary(RATINGS, ratings, profile) if req.get("ai") else None
    if summary:
        picks = R.rerank(ranked, summary, label, mood_label)
    else:
        picks = [(p, None) for p in ranked[:R.TOP_N]]
    return {"label": label, "movies": [details(m, y, g, why, kind, services) for (_, m, y, g), why in picks]}


REWATCH_MIN = 7  # only movies you rated at least this much
REWATCH_N = 5


def rewatch(req):
    """5 favourites (rated 7+) to rewatch, preferring ones streaming on the chosen services and matching the mood."""
    kind = "tv" if req.get("kind") == "tv" else "movie"
    types = ("TV Series", "TV Mini Series") if kind == "tv" else ("Movie",)
    moods = [k for k in req.get("moods", []) if k in R.MOODS and k != "7"]
    mood_genres = {g for k in moods for g in R.MOODS[k][1]}
    services = [s for s in req.get("services", []) if s in R.SERVICES] or R.DEFAULT_SERVICES
    provider_ids = {R.SERVICES[s] for s in services}
    exclude = set(req.get("exclude", []))

    favs = [
        r for r in R.load_ratings(RATINGS)
        if r["Title Type"] in types and int(r["Your Rating"]) >= REWATCH_MIN and r["Const"] not in exclude
        and (not mood_genres or mood_genres & {R.GENRE_ALIASES.get(g, g) for g in r["Genres"].split(", ")})
    ]
    random.shuffle(favs)

    def check(r):
        found = R.tmdb(f"/find/{r['Const']}", external_source="imdb_id").get(f"{kind}_results")
        if not found:
            return None
        flatrate = R.tmdb(f"/{kind}/{found[0]['id']}/watch/providers")["results"].get(R.REGION, {}).get("flatrate", [])
        return r, found[0]["id"], bool(provider_ids & {p["provider_id"] for p in flatrate})

    available, other = [], []
    with ThreadPoolExecutor(8) as pool:
        for start in range(0, min(len(favs), 40), 10):  # check in batches; stop once there are enough streaming ones
            for hit in pool.map(check, favs[start:start + 10]):
                if hit:
                    (available if hit[2] else other).append(hit)
            if len(available) >= REWATCH_N:
                break
        picks = (available + other)[:REWATCH_N]

        def card(hit):
            r, tmdb_id, streaming = hit
            m = R.as_movie(R.tmdb(f"/{kind}/{tmdb_id}"))
            entry = details(m, (m.get("release_date") or "")[:4], [g["name"] for g in m["genres"]], None, kind, services)
            return {**entry, "my_rating": int(r["Your Rating"]), "available": streaming, "const": r["Const"]}

        return {"movies": list(pool.map(card, picks)), "remaining": max(len(favs) - 40, 0)}


def load_list():
    items = json.loads(MYLIST.read_text()) if MYLIST.exists() else []
    for e in items:
        e.setdefault("streaming", [])  # entries saved before availability was tracked
    return items


def drop_from_list(movie_id, kind):
    with list_lock:
        items = load_list()
        kept = [e for e in items if (e["id"], e["kind"]) != (movie_id, kind)]
        if len(kept) != len(items):
            MYLIST.write_text(json.dumps(kept, indent=1))


def refresh_list(req):
    """Re-check which of the selected services stream each saved title (availability changes over time)."""
    services = [s for s in req.get("services", []) if s in R.SERVICES] or R.DEFAULT_SERVICES

    def streaming(e):
        flatrate = R.tmdb(f"/{e['kind']}/{e['id']}/watch/providers")["results"].get(R.REGION, {}).get("flatrate", [])
        ids = {p["provider_id"] for p in flatrate}
        return (e["id"], e["kind"]), [s for s in services if R.SERVICES[s] in ids]

    with ThreadPoolExecutor(8) as pool:
        fresh = dict(pool.map(streaming, load_list()))
    with list_lock:  # reload: the list may have changed while TMDB was being queried
        items = load_list()
        for e in items:
            e["streaming"] = fresh.get((e["id"], e["kind"]), e.get("streaming", []))
        MYLIST.write_text(json.dumps(items, indent=1))
    return items


def later(req):
    kind = "tv" if req.get("kind") == "tv" else "movie"
    movie_id = int(req["id"])
    if any((e["id"], e["kind"]) == (movie_id, kind) for e in load_list()):
        return {"saved": True}
    m = R.as_movie(R.tmdb(f"/{kind}/{movie_id}"))
    entry = details(m, (m.get("release_date") or "")[:4], [g["name"] for g in m["genres"]], None, kind,
                    R.load_config().get("services", R.DEFAULT_SERVICES))
    with list_lock:
        MYLIST.write_text(json.dumps(load_list() + [entry], indent=1))
    return {"saved": True}


def remove_from_list(req):
    drop_from_list(int(req["id"]), "tv" if req.get("kind") == "tv" else "movie")
    return {"removed": True}


def list_ratings():
    return [
        {"const": r["Const"], "title": r["Title"], "year": r["Year"], "type": r["Title Type"],
         "rating": int(r["Your Rating"]), "date": r["Date Rated"], "url": r["URL"]}
        for r in R.load_ratings(RATINGS)
    ]


def update_rating(req):
    const, rating = req["const"], int(req["rating"])
    if not 1 <= rating <= 10:
        raise ValueError("rating must be 1-10")
    with csv_lock, open(RATINGS, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    row = next((r for r in rows if r["Const"] == const), None)
    if not row:
        raise ValueError(f"{const} is not in ratings.csv")
    row["Your Rating"], row["Date Rated"] = rating, datetime.date.today().isoformat()
    with csv_lock, open(RATINGS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    kind = "tv" if row["Title Type"].startswith("TV") else "movie"
    cfg = R.load_config()
    session = cfg.get("tmdb_session")
    found = R.tmdb(f"/find/{const}", external_source="imdb_id").get(f"{kind}_results") if const else None
    synced = False
    if found:
        tmdb_id = found[0]["id"]
        if session:
            tmdb_post(f"/{kind}/{tmdb_id}/rating", {"value": rating}, session_id=session)
            synced = True
        else:
            key = f"tv:{tmdb_id}" if kind == "tv" else str(tmdb_id)
            R.save_config(pending={**cfg.get("pending", {}), key: rating})
    return {"saved": True, "date": row["Date Rated"], "synced_to_tmdb": synced, "tmdb_connected": bool(session)}


def save_rating(req):
    kind = "tv" if req.get("kind") == "tv" else "movie"
    movie_id, rating = int(req["id"]), int(req["rating"])
    drop_from_list(movie_id, kind)
    saved = R.record_ratings(RATINGS, [{"id": movie_id}], f"1 {rating}", kind)
    cfg = R.load_config()
    session = cfg.get("tmdb_session")
    synced = False
    if saved and session:
        tmdb_post(f"/{kind}/{movie_id}/rating", {"value": rating}, session_id=session)
        synced = True
    elif saved:  # not connected yet: queue it ("tv:ID" for series), it is sent when you connect
        key = f"tv:{movie_id}" if kind == "tv" else str(movie_id)
        R.save_config(pending={**cfg.get("pending", {}), key: rating})
    return {"saved": bool(saved), "synced_to_tmdb": synced, "tmdb_connected": bool(session)}


def reject(req):
    m = R.as_movie(R.tmdb(f"/{'tv' if req.get('kind') == 'tv' else 'movie'}/{int(req['id'])}"))
    R.add_rejected(m["title"], (m.get("release_date") or "")[:4], [g["name"] for g in m["genres"]])
    return {"rejected": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, ctype="application/json", status=200, headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            self.send((R.HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/config":
            cfg = R.load_config()
            self.send({
                "services": list(R.SERVICES),
                "selected": cfg.get("services", R.DEFAULT_SERVICES),
                "people": {k: n for k, (n, _) in R.PEOPLE.items()},
                "styles": R.STYLES,
                "moods": {k: label for k, (label, _) in R.MOODS.items()},
                "tmdb_connected": bool(cfg.get("tmdb_session")),
            })
        elif url.path == "/api/mylist":
            self.send(load_list())
        elif url.path == "/api/ratings":
            self.send(list_ratings())
        elif url.path == "/tmdb/login":  # step 1: approve on themoviedb.org (no redirect, TMDB blocks localhost redirects)
            token = R.tmdb("/authentication/token/new")["request_token"]
            R.save_config(tmdb_token=token)
            page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Connect TMDB</title><body style="font:16px system-ui;max-width:520px;margin:48px auto;padding:0 16px">
<h2>Connect TMDB</h2>
<p>1. <a href="https://www.themoviedb.org/authenticate/{token}" target="_blank">Open TMDB and click Approve</a></p>
<p>2. Come back here and <a href="/tmdb/finish"><b>click Finish</b></a></p>"""
            self.send(page.encode(), "text/html; charset=utf-8")
        elif url.path == "/tmdb/finish":  # step 2: turn the approved token into a session and send queued ratings
            cfg = R.load_config()
            try:
                session = tmdb_post("/authentication/session/new", {"request_token": cfg["tmdb_token"]})["session_id"]
            except Exception:
                self.send("<p style='font:16px system-ui;margin:48px'>TMDB hasn't approved yet. Go back, approve, then retry. <a href='/tmdb/login'>Start over</a></p>".encode(), "text/html; charset=utf-8")
                return
            R.save_config(tmdb_session=session)
            for key, rating in cfg.get("pending", {}).items():
                kind, _, movie_id = key.rpartition(":")
                tmdb_post(f"/{kind or 'movie'}/{movie_id}/rating", {"value": rating}, session_id=session)
            R.save_config(pending={})
            self.send(b"", status=302, headers=[("Location", "/")])
        else:
            self.send({"error": "not found"}, status=404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/api/cancel":
                R.CANCEL.set()
                self.send({"cancelled": True})
            elif self.path == "/api/recommend":
                self.send(recommend(body))
            elif self.path == "/api/reject":
                self.send(reject(body))
            elif self.path == "/api/rewatch":
                self.send(rewatch(body))
            elif self.path == "/api/mylist/refresh":
                self.send(refresh_list(body))
            elif self.path == "/api/later":
                self.send(later(body))
            elif self.path == "/api/mylist/remove":
                self.send(remove_from_list(body))
            elif self.path == "/api/ratings/update":
                self.send(update_rating(body))
            elif self.path == "/api/rate":
                self.send(save_rating(body))
            else:
                self.send({"error": "not found"}, status=404)
        except Exception as e:
            self.send({"error": f"{type(e).__name__}: {e}"}, status=500)


def main():
    venv = R.HERE / ".venv"
    if (venv / "bin/python").exists() and Path(sys.prefix).resolve() != venv.resolve():
        os.execv(venv / "bin/python", [str(venv / "bin/python"), *sys.argv])  # mlx-lm lives in the venv
    R.load_tmdb_key()
    if "TMDB_API_KEY" not in os.environ or not RATINGS.exists():
        sys.exit(R.__doc__)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def warm_up():  # load the local model now so the first "Use local AI" search isn't slow
        with lock:
            R.llm("Hi", max_tokens=1)
    threading.Thread(target=warm_up, daemon=True).start()
    print(f"Open http://localhost:{PORT}  (Ctrl+C to stop)")
    webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
