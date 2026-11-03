#!/usr/bin/env python3
"""Tonight's movie picker: IMDb ratings (taste + already seen) + TMDB (streaming availability).

Setup:
  1. Export your IMDb ratings (IMDb > Your Ratings > Export) and save as ratings.csv next to this file.
  2. Get a free TMDB API key (themoviedb.org/settings/api) and run:  export TMDB_API_KEY=...
  3. python3 recommend.py [path/to/ratings.csv]
"""
import csv
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
ENV_FILE = Path("/Users/adriencaudron/OneDrive/REPOs/.env")
REGION = "US"
TOP_N = 5
TASTE_FILE = HERE / "taste.json"
REJECTED_FILE = HERE / "rejected.json"  # "not for me" movies you haven't seen
REJECT_WEIGHT = 3  # counted like a 3/10 rating when learning genre taste
MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"  # local, runs on Apple Silicon via MLX

# TMDB watch-provider ids
SERVICES = {
    "Netflix": 8,
    "Prime Video": 9,
    "Paramount+": 531,
    "Apple TV+": 350,
    "Peacock": 386,
    "Hulu": 15,
    "Disney+": 337,
    "Max": 1899,
}
DEFAULT_SERVICES = ["Netflix", "Paramount+", "Prime Video"]

# Who can watch what. anime: 2 = loves it, 1 = only Ghibli-type, 0 = no.
# animation: "any" or "studios" (only Disney / Pixar / DreamWorks / Ghibli).
PEOPLE = {
    "1": ("Me", {"anime": 2, "animation": "any", "kid": False}),
    "2": ("Vanessa", {"anime": 0, "animation": "studios", "kid": False}),
    "3": ("Ian", {"anime": 2, "animation": "any", "kid": True}),
    "4": ("Leah", {"anime": 1, "animation": "any", "kid": True}),
}
STUDIOS = ("disney", "pixar", "dreamworks", "ghibli")

MOODS = {
    "1": ("Make us laugh", ["Comedy"]),
    "2": ("Action / thriller", ["Action", "Thriller", "Crime"]),
    "3": ("Mind-bending / sci-fi", ["Science Fiction", "Mystery"]),
    "4": ("Heartwarming / drama", ["Drama", "Romance", "Family"]),
    "5": ("Scary", ["Horror"]),
    "6": ("Adventure / fantasy", ["Adventure", "Fantasy"]),
    "7": ("Surprise me", []),
}

VIDEO_TYPES = ("Movie", "TV Series", "TV Mini Series")  # ratings that count as seen / shape taste
# TMDB TV genres are merged ("Action & Adventure", "Sci-Fi & Fantasy"); map our mood genre names onto their parts
TV_GENRE_PARTS = {"Science Fiction": "Sci-Fi", "Horror": "Mystery", "Thriller": "Crime", "Romance": "Drama", "Family": "Kids"}

# What it looks like. "stop" has no TMDB genre, so it is found through the "stop motion" keyword.
STYLES = {"live": "Live action", "anime": "Anime", "animation": "Animation (non-anime)", "stop": "Stop-motion"}
STOP_MOTION_KEYWORD = 10121

# IMDb genre name -> TMDB genre name
GENRE_ALIASES = {"Sci-Fi": "Science Fiction", "Musical": "Music", "Film-Noir": "Crime", "Sport": "Drama"}


def load_tmdb_key():
    """Use TMDB_API_KEY from the environment, else from the shared .env (that one key only)."""
    if "TMDB_API_KEY" in os.environ:
        return
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("TMDB_API_KEY="):
            os.environ["TMDB_API_KEY"] = line.split("=", 1)[1].strip().strip("\"'")


def tmdb(path, **params):
    params["api_key"] = os.environ["TMDB_API_KEY"]
    url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def as_movie(m):
    """Give a TMDB TV result the movie field names (title, release_date) so one code path serves both."""
    if "name" in m:
        m.setdefault("title", m["name"])
        m.setdefault("original_title", m.get("original_name"))
        m.setdefault("release_date", m.get("first_air_date"))
    return m


def norm(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def load_ratings(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r["Title Type"] in VIDEO_TYPES]


def load_rejected():
    return json.loads(REJECTED_FILE.read_text()) if REJECTED_FILE.exists() else []


def add_rejected(title, year, genres):
    rejected = [r for r in load_rejected() if (r["title"], r["year"]) != (title, year)]
    REJECTED_FILE.write_text(json.dumps(rejected + [{"title": title, "year": year, "genres": genres}], indent=1))


def seen_titles(ratings):
    """Everything not to suggest again: rated movies plus 'not for me' ones."""
    return {(norm(r["Title"]), r["Year"]) for r in ratings} | {(norm(r["title"]), r["year"]) for r in load_rejected()}


def taste_profile(ratings):
    """Average (rating - your mean) per genre: positive = you like it."""
    mean = sum(int(r["Your Rating"]) for r in ratings) / len(ratings)
    totals, counts = defaultdict(float), defaultdict(int)
    scored = [(r["Genres"].split(", "), int(r["Your Rating"])) for r in ratings]
    scored += [(r["genres"], REJECT_WEIGHT) for r in load_rejected()]
    for genres, rating in scored:
        for g in genres:
            g = GENRE_ALIASES.get(g, g)
            totals[g] += rating - mean
            counts[g] += 1
    return {g: totals[g] / counts[g] for g in totals}


def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_config(**updates):
    CONFIG.write_text(json.dumps({**load_config(), **updates}))


def ask_services():
    saved = load_config().get("services")
    current = saved or DEFAULT_SERVICES
    print(f"Streaming services: {', '.join(current)}")
    if input("Change them? [y/N] ").strip().lower() != "y":
        return current
    names = list(SERVICES)
    for i, n in enumerate(names, 1):
        print(f"  {i}. {n}")
    picks = input("Numbers, comma-separated: ").split(",")
    chosen = [names[int(p) - 1] for p in picks if p.strip().isdigit() and 0 < int(p) <= len(names)]
    chosen = chosen or current
    save_config(services=chosen)
    return chosen


def ask_audience():
    for k, (name, _) in PEOPLE.items():
        print(f"  {k}. {name}")
    picks = re.findall(r"\d", input("Who's watching? e.g. 1,2 or 2,3,4 (Enter = all four): "))
    keys = [k for k in dict.fromkeys(picks) if k in PEOPLE] or list(PEOPLE)
    return [PEOPLE[k] for k in keys]


def ask_mood():
    for k, (label, _) in MOODS.items():
        print(f"  {k}. {label}")
    picks = [k for k in dict.fromkeys(re.findall(r"\d", input("What are you in the mood for? e.g. 2 or 2,3: "))) if k in MOODS]
    if not picks or "7" in picks:
        return MOODS["7"]
    return " + ".join(MOODS[k][0] for k in picks), [g for k in picks for g in MOODS[k][1]]


def animation_ok(m, rules, kind="movie"):
    """Apply the group's anime / animation rules (needs a TMDB lookup for studio checks)."""
    if 16 not in m["genre_ids"]:
        return True
    anime = m["original_language"] == "ja"
    if anime and rules["anime"] == 2 and not rules["studios_only"]:
        return True
    if anime and rules["anime"] == 0:
        return False
    if not anime and not rules["studios_only"]:
        return True
    companies = " ".join(c["name"].lower() for c in tmdb(f"/{kind}/{m['id']}")["production_companies"])
    if anime and rules["anime"] == 1:
        return "ghibli" in companies
    return any(s in companies for s in STUDIOS)


_llm = None
CANCEL = threading.Event()  # set by the UI's Stop button; checked between generated tokens


class Cancelled(Exception):
    pass


def llm(prompt, max_tokens=500):
    """Ask the local MLX model; returns None if mlx-lm / the model isn't available."""
    global _llm
    try:
        from mlx_lm import load, stream_generate
        if CANCEL.is_set():
            raise Cancelled
        if _llm is None:
            _llm = load(MODEL)
        model, tok = _llm
        chat = tok.apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True)
        text = ""
        for chunk in stream_generate(model, tok, prompt=chat, max_tokens=max_tokens):
            if CANCEL.is_set():
                raise Cancelled
            text += chunk.text
        return text.strip()
    except Cancelled:
        raise
    except Exception as e:  # missing package, no network for first download, ...
        print(f"(local model unavailable: {type(e).__name__}; using plain scoring)")
        return None


def taste_summary(ratings_path, ratings, profile):
    """LLM-written taste profile, cached until ratings.csv changes."""
    rejected = load_rejected()
    digest = hashlib.md5(Path(ratings_path).read_bytes() + json.dumps(rejected).encode()).hexdigest()
    if TASTE_FILE.exists():
        cached = json.loads(TASTE_FILE.read_text())
        if cached["digest"] == digest:
            return cached["summary"]
    by_rating = sorted(ratings, key=lambda r: -int(r["Your Rating"]))
    fmt = lambda r: f"{r['Title']} ({r['Year']}, {r['Genres']}, dir. {r['Directors']}) = {r['Your Rating']}"
    ranked = sorted(profile, key=profile.get)
    summary = llm(
        "Here are one person's movie ratings out of 10.\n"
        "Loved:\n" + "\n".join(fmt(r) for r in by_rating[:25])
        + "\nDisliked:\n" + "\n".join(fmt(r) for r in by_rating[-12:])
        + "\nSkipped without watching (not interested): "
        + (", ".join(f"{r['title']} [{', '.join(r['genres'])}]" for r in rejected[-15:]) or "none")
        + f"\nGenres they rate above average: {', '.join(ranked[-5:])}; below: {', '.join(ranked[:4])}.\n"
        "Describe their taste in 5 short bullet points (genres, tone, directors, deal-breakers).",
        max_tokens=350,
    )
    if summary:
        TASTE_FILE.write_text(json.dumps({"digest": digest, "summary": summary}))
    return summary


def rerank(candidates, summary, who, mood):
    """Let the local model pick and justify the best TOP_N out of the top candidates."""
    pool = candidates[:12]
    listing = "\n".join(
        f"{i}. {m['title']} ({y}) [{', '.join(g)}] TMDB {m['vote_average']:.1f}: {m['overview'][:120]}"
        for i, (_, m, y, g) in enumerate(pool, 1)
    )
    out = llm(
        f"Taste of the main viewer:\n{summary}\n\nTonight: {who}. Mood: {mood}.\nCandidates:\n{listing}\n\n"
        f"Choose the best {TOP_N} for tonight, best first. Reply with only their numbers, comma-separated.",
        max_tokens=40,
    )
    chosen = []
    for n in re.findall(r"\d+", out or ""):
        n = int(n) - 1
        if 0 <= n < len(pool) and n not in chosen:
            chosen.append(n)
    chosen += [i for i in range(len(pool)) if i not in chosen]
    picks = [pool[i] for i in chosen[:TOP_N]]
    return [(p, why(summary, p[1], p[2], p[3]) if out else None) for p in picks]


def why(summary, m, year, genres):
    """One-line reason this movie suits the viewer."""
    line = llm(
        f"Viewer's taste:\n{summary}\n\nMovie: {m['title']} ({year}) [{', '.join(genres)}]: {m['overview'][:200]}\n\n"
        "In one sentence of under 20 words, say why this viewer might enjoy it. Do not repeat the title.",
        max_tokens=50,
    )
    return line.splitlines()[0] if line else None


def record_ratings(ratings_path, picks, feedback, kind="movie"):
    """Append 'pick# rating' pairs (e.g. '2 8, 4 6') to the IMDb-format ratings.csv. Returns count."""
    with open(ratings_path, newline="", encoding="utf-8-sig") as f:
        fields = csv.DictReader(f).fieldnames
    rows = []
    for n, rating in re.findall(r"(\d+)\s+(\d+)", feedback):
        n, rating = int(n), int(rating)
        if not (1 <= n <= len(picks) and 1 <= rating <= 10):
            print(f"  skipped '{n} {rating}' (pick 1-{len(picks)}, rating 1-10)")
            continue
        m = as_movie(tmdb(f"/{kind}/{picks[n - 1]['id']}", append_to_response="credits,external_ids"))
        imdb_id = m.get("imdb_id") or m["external_ids"].get("imdb_id") or ""
        genres = [{v: k for k, v in GENRE_ALIASES.items()}.get(g["name"], g["name"]) for g in m["genres"]]
        rows.append({
            "Const": imdb_id, "Your Rating": rating, "Date Rated": datetime.date.today().isoformat(),
            "Title": m["title"], "Original Title": m["original_title"],
            "URL": f"https://www.imdb.com/title/{imdb_id}", "Title Type": "TV Series" if kind == "tv" else "Movie",
            "IMDb Rating": m["vote_average"], "Runtime (mins)": m.get("runtime") or (m.get("episode_run_time") or [""])[0],
            "Year": (m["release_date"] or "")[:4], "Genres": ", ".join(genres),
            "Num Votes": m["vote_count"], "Release Date": m["release_date"],
            "Directors": ", ".join(c["name"] for c in m.get("created_by", [])) if kind == "tv"
            else ", ".join(c["name"] for c in m["credits"]["crew"] if c["job"] == "Director"),
        })
    if rows:
        needs_newline = not Path(ratings_path).read_bytes().endswith(b"\n")
        with open(ratings_path, "a", newline="", encoding="utf-8") as f:
            if needs_newline:
                f.write("\r\n")
            csv.DictWriter(f, fieldnames=fields).writerows(rows)
    return len(rows)


def style_ok(m, styles):
    """Live action = not animated; anime = Japanese animation; animation = any other animation."""
    animated = 16 in m["genre_ids"]
    anime = animated and m["original_language"] == "ja"
    return ((not animated and "live" in styles) or (anime and "anime" in styles)
            or (animated and not anime and "animation" in styles))


def candidates_for(seen, profile, params, rules, genre_names, kind="movie", styles=()):
    styles = set(styles) & set(STYLES)
    scored, found = [], set()

    def collect(query, pages, wanted):
        for page in range(1, pages + 1):
            for m in map(as_movie, tmdb(f"/discover/{kind}", page=page, **query)["results"]):
                year = (m.get("release_date") or "")[:4]
                if m["id"] in found or (norm(m["title"]), year) in seen or not wanted(m) or not animation_ok(m, rules, kind):
                    continue
                found.add(m["id"])
                genres = [genre_names[g] for g in m["genre_ids"] if g in genre_names]
                affinity = sum(profile.get(g, 0) for g in genres) / max(len(genres), 1)
                scored.append((affinity + (m["vote_average"] - 6.5) * 0.5, m, year, genres))

    if "stop" in styles:
        collect({**params, "with_keywords": STOP_MOTION_KEYWORD}, 4, lambda m: True)
    if not styles or styles - {"stop"}:
        collect(params, 8, (lambda m: True) if not styles else (lambda m: style_ok(m, styles)))
    return sorted(scored, key=lambda s: -s[0])


def build_query(services, group, mood_genres, genre_names, kind="movie", max_cert="auto", styles=()):
    """TMDB discover params + animation rules for this group, services and mood."""
    rules = {
        "anime": min(t["anime"] for _, t in group),
        "studios_only": any(t["animation"] == "studios" for _, t in group),
    }
    params = dict(
        watch_region=REGION,
        with_watch_providers="|".join(str(SERVICES[s]) for s in services),
        with_watch_monetization_types="flatrate",
        sort_by="popularity.desc",
        **{"vote_count.gte": 500},
    )
    styles = set(styles) & set(STYLES)
    if styles == {"anime"}:  # narrow the discover query itself so the first pages aren't mostly live action
        params["with_original_language"] = "ja"
    elif styles == {"live"}:
        params["without_genres"] = 16
    if kind == "tv":
        wanted = {TV_GENRE_PARTS.get(g, g) for g in mood_genres}
        mood_ids = [i for i, n in genre_names.items() if wanted & set(n.split(" & "))]
    else:
        mood_ids = [i for i, n in genre_names.items() if n in mood_genres]
    if mood_ids:
        params["with_genres"] = "|".join(map(str, mood_ids))
    if max_cert == "auto" and any(t["kid"] for _, t in group):
        max_cert = "PG-13"
    if kind == "movie" and max_cert in ("G", "PG", "PG-13", "R"):  # "any" adds no filter; TMDB has none for TV
        params.update(certification_country="US", **{"certification.lte": max_cert})
    return params, rules


def main():
    venv = HERE / ".venv"
    if (venv / "bin/python").exists() and Path(sys.prefix).resolve() != venv.resolve():
        os.execv(venv / "bin/python", [str(venv / "bin/python"), *sys.argv])  # mlx-lm lives in the venv
    ratings_path = sys.argv[1] if len(sys.argv) > 1 else HERE / "ratings.csv"
    load_tmdb_key()
    if "TMDB_API_KEY" not in os.environ or not Path(ratings_path).exists():
        sys.exit(__doc__)

    services = ask_services()
    group = ask_audience()
    mood_label, mood_genres = ask_mood()
    label = f"{' + '.join(n for n, _ in group)} / {mood_label}"
    genre_names = {g["id"]: g["name"] for g in tmdb("/genre/movie/list")["genres"]}
    params, rules = build_query(services, group, mood_genres, genre_names)

    while True:  # recommend -> you tell me what you've seen -> ratings.csv grows -> recommend again
        ratings = load_ratings(ratings_path)
        seen = seen_titles(ratings)
        profile = taste_profile(ratings)
        summary = taste_summary(ratings_path, ratings, profile)
        ranked = candidates_for(seen, profile, params, rules, genre_names)
        picks = rerank(ranked, summary, label, mood_label)

        print(f"\nTop {TOP_N} for tonight ({label}) on {', '.join(services)}:\n")
        for i, ((_, m, year, genres), why) in enumerate(picks, 1):
            print(f"{i}. {m['title']} ({year})  TMDB {m['vote_average']:.1f}  [{', '.join(genres)}]")
            print(f"   {why or m['overview'][:160] + '...'}\n")

        feedback = input("Already seen any? Type 'pick rating' pairs, e.g. '2 8, 4 6' (Enter = done): ").strip()
        if not feedback:
            break
        added = record_ratings(ratings_path, [m for (_, m, _, _), _ in picks], feedback)
        if not added:
            break
        print(f"Saved {added} rating(s) to ratings.csv. Re-learning your taste...")


if __name__ == "__main__":
    main()
