"""
Offline evaluation — v1 vs v2 comparison.
Measures Genre Precision@10, Director Hit@10, Mean Vote@10, Diversity.
"""

import asyncio
import httpx
import math
from collections import Counter

TMDB_API_KEY = "bb5761808480aa3ebaa814d58b109aad"
BASE_URL = "https://api.themoviedb.org/3"

# ── TMDB helpers ──────────────────────────────────────────────────────────────

async def tmdb_get(client, path, **params):
    params["api_key"] = TMDB_API_KEY
    r = await client.get(f"{BASE_URL}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


async def get_movie_features(client, movie_id):
    try:
        data = await tmdb_get(client, f"/movie/{movie_id}",
                              append_to_response="credits", language="en-US")
    except Exception as e:
        return None

    credits = data.get("credits") or {}
    cast = credits.get("cast") or []
    crew = credits.get("crew") or []
    director = next((c["name"] for c in crew if c.get("job") == "Director"), None)

    release_year = None
    rd = data.get("release_date") or ""
    if rd:
        try:
            release_year = int(rd[:4])
        except ValueError:
            pass

    return {
        "movie_id": data["id"],
        "title": data.get("title"),
        "genres": [g["name"] for g in data.get("genres", []) if g.get("name")],
        "cast": [c["id"] for c in cast[:10]],
        "director": director,
        "release_year": release_year,
        "vote_average": data.get("vote_average") or 0.0,
        "vote_count": data.get("vote_count") or 0,
    }


async def get_candidate_pool(client):
    tasks = []
    for page in range(1, 5):
        tasks.append(tmdb_get(client, "/movie/top_rated", page=page))
    for page in range(1, 3):
        tasks.append(tmdb_get(client, "/movie/popular", page=page))

    pages = await asyncio.gather(*tasks, return_exceptions=True)
    seen, candidates = set(), []
    for page in pages:
        if isinstance(page, Exception):
            continue
        for m in page.get("results", []):
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                candidates.append(m)
    return candidates


# ── V1 engine (original) ──────────────────────────────────────────────────────

V1_W_GENRE = 2.0
V1_W_CAST = 1.0
V1_W_DIRECTOR = 3.0
V1_W_DECADE = 0.8
V1_W_QUALITY = 0.5


def v1_build_profile(rated_features, preferred_genres, preferred_decades, favorite_people):
    p = {"genres": {}, "cast": {}, "directors": {}, "decades": {}}
    for feat, rating in rated_features:
        w = (rating / 5.0) ** 2
        for g in feat.get("genres", []):
            p["genres"][g] = p["genres"].get(g, 0.0) + w
        for aid in feat.get("cast", []):
            p["cast"][aid] = p["cast"].get(aid, 0.0) + w
        d = feat.get("director")
        if d:
            p["directors"][d] = p["directors"].get(d, 0.0) + w
        yr = feat.get("release_year")
        if yr:
            decade = str((yr // 10) * 10)
            p["decades"][decade] = p["decades"].get(decade, 0.0) + w
    for g in preferred_genres:
        p["genres"][g] = p["genres"].get(g, 0.0) + 1.5
    for dec in preferred_decades:
        p["decades"][dec] = p["decades"].get(dec, 0.0) + 1.5
    for person in favorite_people:
        if person.get("role", "").lower() == "director":
            p["directors"][person["name"]] = p["directors"].get(person["name"], 0.0) + 2.0
        else:
            p["cast"][person["id"]] = p["cast"].get(person["id"], 0.0) + 2.0
    return p


def v1_score(profile, feat):
    score = 0.0
    for g in feat.get("genres", []):
        score += profile["genres"].get(g, 0.0) * V1_W_GENRE
    for aid in feat.get("cast", []):
        score += profile["cast"].get(aid, 0.0) * V1_W_CAST
    d = feat.get("director")
    if d:
        score += profile["directors"].get(d, 0.0) * V1_W_DIRECTOR
    yr = feat.get("release_year")
    if yr:
        score += profile["decades"].get(str((yr // 10) * 10), 0.0) * V1_W_DECADE
    score += (feat.get("vote_average", 0) / 10.0) * V1_W_QUALITY
    return score


def v1_recommend(candidates, candidate_features, rated_features, preferred_genres,
                  preferred_decades, favorite_people, excluded_ids, limit=20):
    profile = v1_build_profile(rated_features, preferred_genres, preferred_decades, favorite_people)
    scored = []
    for cand in candidates:
        mid = cand.get("id")
        if not mid or mid in excluded_ids:
            continue
        feat = candidate_features.get(mid)
        if not feat:
            continue
        scored.append((v1_score(profile, feat), feat))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:limit]]


# ── V2 engine (fixed) ─────────────────────────────────────────────────────────

V2_W_GENRE = 2.0
V2_W_CAST = 1.0
V2_W_DIRECTOR = 3.0
V2_W_DECADE = 0.8
V2_W_QUALITY = 1.5          # raised
V2_MIN_VOTE = 6.0            # quality floor
V2_DISLIKE_THRESH = 2.5      # negative signal below this
V2_MAX_PER_DIR = 2           # series dedup
V2_CAST_POS_W = [3.0, 2.0, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

PTBR_TO_EN = {
    "Acao": "Action", "Ação": "Action",
    "Aventura": "Adventure",
    "Animacao": "Animation", "Animação": "Animation",
    "Comedia": "Comedy", "Comédia": "Comedy",
    "Crime": "Crime",
    "Documentario": "Documentary", "Documentário": "Documentary",
    "Drama": "Drama",
    "Familia": "Family", "Família": "Family",
    "Fantasia": "Fantasy",
    "Historia": "History", "História": "History",
    "Terror": "Horror",
    "Musica": "Music", "Música": "Music",
    "Misterio": "Mystery", "Mistério": "Mystery",
    "Romance": "Romance",
    "Ficcao cientifica": "Science Fiction", "Ficção Científica": "Science Fiction",
    "Suspense": "Thriller",
    "Guerra": "War",
    "Faroeste": "Western",
}


def v2_build_profile(rated_features, preferred_genres, preferred_decades, favorite_people):
    p = {"genres": {}, "cast": {}, "directors": {}, "decades": {}}
    for feat, rating in rated_features:
        positive = rating >= V2_DISLIKE_THRESH
        weight = (rating / 5.0) ** 2 if positive else -((5.0 - rating) / 5.0) ** 2
        for g in feat.get("genres", []):
            p["genres"][g] = p["genres"].get(g, 0.0) + weight
        for pos, aid in enumerate(feat.get("cast", [])):
            pw = V2_CAST_POS_W[min(pos, len(V2_CAST_POS_W) - 1)]
            p["cast"][aid] = p["cast"].get(aid, 0.0) + weight * pw
        d = feat.get("director")
        if d:
            p["directors"][d] = p["directors"].get(d, 0.0) + weight
        yr = feat.get("release_year")
        if yr:
            decade = str((yr // 10) * 10)
            p["decades"][decade] = p["decades"].get(decade, 0.0) + weight

    for raw_g in preferred_genres:
        g = PTBR_TO_EN.get(raw_g, raw_g)
        p["genres"][g] = p["genres"].get(g, 0.0) + 1.5
    for dec in preferred_decades:
        p["decades"][str(dec)] = p["decades"].get(str(dec), 0.0) + 1.5
    for person in favorite_people:
        if person.get("role", "").lower() == "director":
            p["directors"][person["name"]] = p["directors"].get(person["name"], 0.0) + 2.0
        else:
            p["cast"][person["id"]] = p["cast"].get(person["id"], 0.0) + 2.0
    return p


def v2_score(profile, feat):
    score = 0.0
    for g in feat.get("genres", []):
        score += profile["genres"].get(g, 0.0) * V2_W_GENRE
    for pos, aid in enumerate(feat.get("cast", [])):
        pw = V2_CAST_POS_W[min(pos, len(V2_CAST_POS_W) - 1)]
        score += profile["cast"].get(aid, 0.0) * V2_W_CAST * pw
    d = feat.get("director")
    if d:
        score += profile["directors"].get(d, 0.0) * V2_W_DIRECTOR
    yr = feat.get("release_year")
    if yr:
        score += profile["decades"].get(str((yr // 10) * 10), 0.0) * V2_W_DECADE
    score += (feat.get("vote_average", 0) / 10.0) * V2_W_QUALITY
    return score


def v2_dedup_director(scored, max_per_dir):
    counts = {}
    result = []
    for score, feat in scored:
        d = feat.get("director") or "__none__"
        if counts.get(d, 0) < max_per_dir:
            counts[d] = counts.get(d, 0) + 1
            result.append((score, feat))
    return result


def v2_recommend(candidates, candidate_features, rated_features, preferred_genres,
                  preferred_decades, favorite_people, excluded_ids, limit=20):
    profile = v2_build_profile(rated_features, preferred_genres, preferred_decades, favorite_people)
    scored = []
    for cand in candidates:
        mid = cand.get("id")
        if not mid or mid in excluded_ids:
            continue
        feat = candidate_features.get(mid)
        if not feat:
            continue
        if feat.get("vote_average", 0) < V2_MIN_VOTE:
            continue
        scored.append((v2_score(profile, feat), feat))
    scored.sort(key=lambda x: -x[0])
    deduped = v2_dedup_director(scored, V2_MAX_PER_DIR)
    return [f for _, f in deduped[:limit]]


# ── Metrics ───────────────────────────────────────────────────────────────────

def genre_precision_at_k(recs, expected_genres, k):
    hits = sum(1 for r in recs[:k] if any(g in expected_genres for g in r.get("genres", [])))
    return hits / k


def director_hit_at_k(recs, liked_directors, k):
    return any(r.get("director") in liked_directors for r in recs[:k])


def mean_vote(recs, k):
    votes = [r.get("vote_average", 0) for r in recs[:k] if r.get("vote_average")]
    return sum(votes) / len(votes) if votes else 0.0


def genre_diversity(recs, k):
    counts = Counter(g for r in recs[:k] for g in r.get("genres", []))
    total = sum(counts.values())
    if not total:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def min_vote_in_list(recs, k):
    votes = [r.get("vote_average", 0) for r in recs[:k]]
    return min(votes) if votes else 0.0


# ── Personas ──────────────────────────────────────────────────────────────────

PERSONAS = [
    {
        "name": "Cinefilo Nolan/Sci-fi",
        "rated_movie_ids": [
            (157336, 5.0),   # Interstellar
            (49026, 5.0),    # The Dark Knight Rises
            (155, 4.5),      # The Dark Knight
            (27205, 4.5),    # Inception
            (680, 4.0),      # Pulp Fiction
            (244786, 4.0),   # Whiplash
            (8587, 3.5),     # The Lion King
            (12445, 2.0),    # Harry Potter (disliked)
        ],
        "preferred_genres": ["Science Fiction", "Thriller", "Drama"],
        "preferred_decades": ["2000", "2010"],
        "favorite_people": [
            {"name": "Christopher Nolan", "id": 525, "role": "director"},
            {"name": "Matthew McConaughey", "id": 10980, "role": "actor"},
        ],
        "expected_genres": {"Science Fiction", "Thriller", "Drama", "Action"},
        "liked_directors": {"Christopher Nolan", "Quentin Tarantino", "David Fincher"},
    },
    {
        "name": "Fa de Romance/Drama",
        "rated_movie_ids": [
            (597, 5.0),      # Titanic
            (11036, 5.0),    # The Notebook
            (14836, 4.5),    # The Proposal
            (68718, 4.5),    # Django Unchained
            (299534, 3.0),   # Avengers Endgame
            (299536, 2.5),   # Infinity War
            (24428, 2.0),    # The Avengers (disliked)
        ],
        "preferred_genres": ["Romance", "Drama"],
        "preferred_decades": ["1990", "2000", "2010"],
        "favorite_people": [
            {"name": "James Cameron", "id": 2710, "role": "director"},
        ],
        "expected_genres": {"Romance", "Drama"},
        "liked_directors": {"James Cameron"},
    },
    {
        "name": "Horror/Suspense",
        "rated_movie_ids": [
            (539681, 5.0),   # The Conjuring 2
            (259693, 5.0),   # The Conjuring
            (76341, 4.5),    # Mad Max Fury Road
            (411, 4.5),      # The Shining
            (745, 4.0),      # Silence of the Lambs
            (346364, 4.0),   # It (2017)
            (950387, 2.0),   # disliked comedy
        ],
        "preferred_genres": ["Horror", "Thriller", "Mystery"],
        "preferred_decades": ["1980", "1990", "2010"],
        "favorite_people": [
            {"name": "James Wan", "id": 98947, "role": "director"},
            {"name": "Anthony Hopkins", "id": 4173, "role": "actor"},
        ],
        "expected_genres": {"Horror", "Thriller", "Mystery"},
        "liked_directors": {"James Wan", "Stanley Kubrick"},
    },
]


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("Moviz Recommender — V1 vs V2 Comparison")
    print("=" * 65)

    async with httpx.AsyncClient(timeout=20.0) as client:
        print("\n[1/3] Fetching candidate pool...")
        candidates = await get_candidate_pool(client)
        print(f"      {len(candidates)} candidates")

        print("\n[2/3] Fetching candidate features...")
        candidate_features = {}
        batch_size = 20
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i+batch_size]
            feats = await asyncio.gather(
                *[get_movie_features(client, c["id"]) for c in batch],
                return_exceptions=True,
            )
            for c, feat in zip(batch, feats):
                if isinstance(feat, dict):
                    candidate_features[c["id"]] = feat
            print(f"      {min(i+batch_size, len(candidates))}/{len(candidates)}...", end="\r")
        print(f"\n      {len(candidate_features)} features cached")

        print("\n[3/3] Evaluating personas (v1 vs v2)...")
        all_v1, all_v2 = [], []

        for persona in PERSONAS:
            print(f"\n{'='*65}")
            print(f"  PERSONA: {persona['name']}")
            print(f"{'='*65}")

            rated_ids_raw = [mid for mid, _ in persona["rated_movie_ids"]]
            raw_feats = await asyncio.gather(
                *[get_movie_features(client, mid) for mid in rated_ids_raw]
            )
            rated_features = [
                (f, r) for (_, r), f in zip(persona["rated_movie_ids"], raw_feats) if f
            ]
            excluded = {mid for mid, _ in persona["rated_movie_ids"]}

            # Run both engines
            top_v1 = v1_recommend(candidates, candidate_features, rated_features,
                                   persona["preferred_genres"], persona["preferred_decades"],
                                   persona["favorite_people"], excluded, limit=20)
            top_v2 = v2_recommend(candidates, candidate_features, rated_features,
                                   persona["preferred_genres"], persona["preferred_decades"],
                                   persona["favorite_people"], excluded, limit=20)

            k = 10
            def metrics(recs, label):
                return {
                    "label": label,
                    "gp": genre_precision_at_k(recs, persona["expected_genres"], k),
                    "dh": director_hit_at_k(recs, persona["liked_directors"], k),
                    "mv": mean_vote(recs, k),
                    "div": genre_diversity(recs, k),
                    "min_v": min_vote_in_list(recs, k),
                }

            m1 = metrics(top_v1, "v1")
            m2 = metrics(top_v2, "v2")

            # Side-by-side top-10
            print(f"\n  {'#':<3} {'V1 title':<36} {'V':<4}  |  {'V2 title':<36} {'V':<4}")
            print(f"  {'-'*88}")
            for i in range(10):
                t1 = (top_v1[i].get("title") or "")[:34] if i < len(top_v1) else ""
                v1v = f"{top_v1[i].get('vote_average',0):.1f}" if i < len(top_v1) else ""
                t2 = (top_v2[i].get("title") or "")[:34] if i < len(top_v2) else ""
                v2v = f"{top_v2[i].get('vote_average',0):.1f}" if i < len(top_v2) else ""
                improved = i < len(top_v2) and i < len(top_v1) and top_v2[i].get("vote_average", 0) > top_v1[i].get("vote_average", 0) + 0.5
                mark = " <-" if improved else ""
                print(f"  {i+1:<3} {t1:<36} {v1v:<4}  |  {t2:<36} {v2v:<4}{mark}")

            print(f"\n  -- METRICS @K={k} --")
            print(f"  {'Metric':<22} {'v1':>8} {'v2':>8} {'delta':>8}")
            print(f"  {'-'*50}")
            print(f"  {'Genre Precision':<22} {m1['gp']:>7.1%} {m2['gp']:>7.1%} {m2['gp']-m1['gp']:>+7.1%}")
            print(f"  {'Director Hit':<22} {'YES' if m1['dh'] else 'NO':>8} {'YES' if m2['dh'] else 'NO':>8}")
            print(f"  {'Mean Vote Avg':<22} {m1['mv']:>8.2f} {m2['mv']:>8.2f} {m2['mv']-m1['mv']:>+8.2f}")
            print(f"  {'Min Vote (floor)':<22} {m1['min_v']:>8.2f} {m2['min_v']:>8.2f} {m2['min_v']-m1['min_v']:>+8.2f}")
            print(f"  {'Diversity (bits)':<22} {m1['div']:>8.3f} {m2['div']:>8.3f} {m2['div']-m1['div']:>+8.3f}")

            all_v1.append(m1)
            all_v2.append(m2)

    print(f"\n\n{'='*65}")
    print("  AGGREGATE DELTA (v2 - v1, avg across 3 personas)")
    print(f"{'='*65}")
    avg = lambda key: sum(m[key] for m in all_v1) / len(all_v1), sum(m[key] for m in all_v2) / len(all_v2)
    for key, label in [("gp", "Genre Precision"), ("mv", "Mean Vote Avg"), ("min_v", "Min Vote"), ("div", "Diversity")]:
        a1, a2 = avg(key)
        print(f"  {label:<22}: v1={a1:.3f}  v2={a2:.3f}  delta={a2-a1:+.3f}")

    dh_v1 = sum(1 for m in all_v1 if m["dh"])
    dh_v2 = sum(1 for m in all_v2 if m["dh"])
    print(f"  {'Director Hits':<22}: v1={dh_v1}/3  v2={dh_v2}/3")

    print("""

WHAT WAS FIXED IN V2:
  1. Quality floor (vote >= 6.0)    -> removes War of the Worlds (4.1), Shelter (6.8 marginally)
  2. Negative signal (rating < 2.5) -> low-rated genres get suppressed, not ignored
  3. Cast position weighting        -> lead actor 3x vs supporting cast 1x
  4. Portuguese genre mapping       -> "Acao" now matches "Action" in feature cache
  5. Director deduplication         -> Gabriel's Inferno I+II+III capped at 2 entries
  6. W_QUALITY raised 0.5 -> 1.5    -> quality has meaningful impact on ranking

REMAINING WEAKNESSES (next phases):
  - Candidate pool only ~120 movies; niche genres under-represented
  - No collaborative filtering (Phase 2)
  - No recency decay on ratings
  - No serendipity/novelty control (MMR)
  - No minimum vote_count filter (high-average films with only 10 votes)
""")


if __name__ == "__main__":
    asyncio.run(main())
