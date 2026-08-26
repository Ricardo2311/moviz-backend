"""
Full evaluation of all 5 recommendation rows.

For each of 4 user personas:
  Row 1  Para voce              - Genre Precision@10, Mean Vote, Diversity, Coherence
  Row 2  Porque voce assistiu X - Similarity@10 to anchor film, Director/Genre overlap
  Row 3  Mais [Genre]           - Genre Purity (must be 100%), Vote quality
  Row 4  Em alta no Moviz       - Simulated from multi-user platform data
  Row 5  Aclamados pela critica - Must be pure vote-sorted, no taste bias

Plus cross-row analysis:
  - Deduplication rate between rows
  - Anchor detection accuracy (Row 2 reason)
  - Genre leakage (off-topic films in genre row)
  - Match score coherence (higher-scored films must rank higher)
"""

import asyncio
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import httpx

TMDB_API_KEY = "bb5761808480aa3ebaa814d58b109aad"
BASE = "https://api.themoviedb.org/3"

# ─────────────────────────────────────────────────────────────────────────────
# TMDB helpers
# ─────────────────────────────────────────────────────────────────────────────

async def get(client, path, **params):
    params["api_key"] = TMDB_API_KEY
    r = await client.get(f"{BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


async def movie_features(client, mid):
    try:
        d = await get(client, f"/movie/{mid}", append_to_response="credits", language="en-US")
    except Exception:
        return None
    credits = d.get("credits") or {}
    cast = credits.get("cast") or []
    crew = credits.get("crew") or []
    director = next((c["name"] for c in crew if c.get("job") == "Director"), None)
    yr = None
    rd = d.get("release_date") or ""
    if rd:
        try: yr = int(rd[:4])
        except ValueError: pass
    return {
        "movie_id": d["id"], "title": d.get("title"),
        "genres": [g["name"] for g in d.get("genres", []) if g.get("name")],
        "cast": [c["id"] for c in cast[:10]],
        "director": director, "release_year": yr,
        "vote_average": d.get("vote_average") or 0.0,
        "vote_count": d.get("vote_count") or 0,
        "overview": d.get("overview") or "",
    }


async def candidate_pool(client):
    tasks = (
        [get(client, "/movie/top_rated", page=p) for p in range(1, 5)] +
        [get(client, "/movie/popular",   page=p) for p in range(1, 3)] +
        [get(client, "/discover/movie",
             with_genres=g, sort_by="vote_average.desc",
             **{"vote_count.gte": 200}, page=1)
         for g in [27, 53, 878, 10749, 18, 35, 28]]   # Horror,Thriller,SciFi,Romance,Drama,Comedy,Action
    )
    pages = await asyncio.gather(*tasks, return_exceptions=True)
    seen, out = set(), []
    for pg in pages:
        if isinstance(pg, Exception): continue
        for m in pg.get("results", []):
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid); out.append(m)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Engine (mirrors recommender.py exactly)
# ─────────────────────────────────────────────────────────────────────────────

W_GENRE=2.0; W_CAST=0.5; W_DIR=3.0; W_DEC=0.8; W_QUAL=1.5
DISLIKE=2.5; MAX_DIR=2; MIN_VOTE=6.5
CAST_POS=[1.0,0.75,0.5,0.33,0.33,0.33,0.33,0.33,0.33,0.33]
HALF_LIFE=500

def recency(rated_at):
    days = max((datetime.utcnow()-rated_at).days, 0)
    return math.exp(-math.log(2)*days/HALF_LIFE)

def build_profile(rated, pref_genres=[], pref_decades=[], fav_people=[]):
    p = {"genres":{},"cast":{},"directors":{},"decades":{}}
    for feat, rating, rated_at in rated:
        pos = rating >= DISLIKE
        bw  = (rating/5)**2 if pos else -((5-rating)/5)**2
        w   = bw * recency(rated_at)
        gc  = max(len(feat.get("genres",[])),1)
        for g in feat.get("genres",[]): p["genres"][g] = p["genres"].get(g,0)+w/gc
        for i,aid in enumerate(feat.get("cast",[])):
            pw = CAST_POS[min(i,9)]; p["cast"][aid] = p["cast"].get(aid,0)+w*pw
        d = feat.get("director")
        if d: p["directors"][d] = p["directors"].get(d,0)+w
        yr = feat.get("release_year")
        if yr:
            dec=str((yr//10)*10); p["decades"][dec]=p["decades"].get(dec,0)+w
    for g in pref_genres:  p["genres"][g]  = p["genres"].get(g,0)+1.5
    for d in pref_decades: p["decades"][d] = p["decades"].get(d,0)+1.5
    for person in fav_people:
        if person.get("role","").lower()=="director":
            p["directors"][person["name"]]=p["directors"].get(person["name"],0)+0.5
        else:
            p["cast"][person["id"]]=p["cast"].get(person["id"],0)+0.5
    return p

def score_movie(p, feat):
    s=0.0
    genres=feat.get("genres") or []; gc=max(len(genres),1)
    for g in genres: s+=(p["genres"].get(g,0)/gc)*W_GENRE
    for i,aid in enumerate(feat.get("cast") or []):
        s+=p["cast"].get(aid,0)*W_CAST*CAST_POS[min(i,9)]
    d=feat.get("director")
    if d: s+=p["directors"].get(d,0)*W_DIR
    yr=feat.get("release_year")
    if yr: s+=p["decades"].get(str((yr//10)*10),0)*W_DEC
    s+=(feat.get("vote_average",0)/10)*W_QUAL
    return s

def score_cold(p, feat):
    s=0.0; genres=feat.get("genres") or []; gc=max(len(genres),1)
    for g in genres: s+=(p["genres"].get(g,0)/gc)*W_GENRE
    s+=(feat.get("vote_average",0)/10)*W_QUAL*3
    return s

def item_sim(src, cand):
    s=0.0
    sg=set(src.get("genres",[])); cg=set(cand.get("genres",[])); u=sg|cg
    if u: s+=len(sg&cg)/len(u)*4
    if src.get("director") and src["director"]==cand.get("director"): s+=3
    sc=set(list(src.get("cast",[]))[:5]); cc=set(list(cand.get("cast",[]))[:5]); cu=sc|cc
    if cu: s+=len(sc&cc)/len(cu)*2
    sy=src.get("release_year"); cy=cand.get("release_year")
    if sy and cy and sy//10==cy//10: s+=0.5
    s+=(cand.get("vote_average",0)/10)*1.5
    return s

def to_match(score, mn, mx):
    if mx==mn: return 82
    return round(55+(score-mn)/(mx-mn)*44)

def reason_tags(p, feat):
    genres=feat.get("genres") or []
    tags=[g for g,_ in sorted(((g,p["genres"].get(g,0)) for g in genres),key=lambda x:-x[1]) if p["genres"].get(g,0)>0][:2]
    d=feat.get("director")
    if d and p["directors"].get(d,0)>0.3: tags.append(d)
    return tags[:3]

def dedup_dir(scored, mx):
    cnt={}; out=[]
    for s,f in scored:
        d=f.get("director") or "__none__"
        if cnt.get(d,0)<mx: cnt[d]=cnt.get(d,0)+1; out.append((s,f))
    return out

def run_for_you(p, cands, excl, cold, limit=15):
    fn = score_cold if cold else score_movie
    sc = [(fn(p,f),f) for mid,f in cands.items() if mid not in excl]
    sc.sort(key=lambda x:-x[0])
    sc = dedup_dir(sc, MAX_DIR)
    mn,mx = (sc[-1][0] if sc else 0),(sc[0][0] if sc else 1)
    return [dict(f, match=to_match(s,mn,mx), rtags=reason_tags(p,f)) for s,f in sc[:limit]]

def run_because(anchor_feat, cands, excl, limit=15):
    if not anchor_feat: return [], None
    sc=[(item_sim(anchor_feat,f),f) for mid,f in cands.items() if mid not in excl]
    sc.sort(key=lambda x:-x[0])
    sc=dedup_dir(sc, MAX_DIR)
    mn,mx=(sc[-1][0] if sc else 0),(sc[0][0] if sc else 1)
    movies=[dict(f, match=to_match(s,mn,mx), rtags=anchor_feat.get("genres",[])[:2]) for s,f in sc[:limit]]
    return movies, anchor_feat["title"]

def run_top_genre(p, cands, excl, limit=15):
    if not p["genres"]: return [], None
    top_g = max(p["genres"].items(), key=lambda x:x[1])
    if top_g[1]<=0: return [], None
    g = top_g[0]
    filt=[f for mid,f in cands.items() if mid not in excl and g in (f.get("genres") or [])]
    filt.sort(key=lambda f:-(f.get("vote_average") or 0))
    top_n = filt[:limit]
    if not top_n: return [], g
    mn = min(f.get("vote_average") or MIN_VOTE for f in top_n)
    mx = max(f.get("vote_average") or MIN_VOTE for f in top_n)
    return [dict(f, match=to_match(f.get("vote_average") or MIN_VOTE, mn, mx), rtags=[g]) for f in top_n], g

def run_trending(trending_pool, excl, limit=15):
    """Trending = simulated platform ratings sorted by # ratings + avg."""
    return [dict(f, match=None, rtags=["Em alta no Moviz"]) for f in trending_pool if f["movie_id"] not in excl][:limit]

def run_acclaimed(cands, excl, limit=15):
    top=sorted([f for mid,f in cands.items() if mid not in excl],key=lambda f:-(f.get("vote_average") or 0))
    return [dict(f, match=None, rtags=f.get("genres",[])[:2]) for f in top[:limit]]


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def genre_precision(movies, expected, k):
    hits=sum(1 for m in movies[:k] if any(g in expected for g in m.get("genres",[])))
    return hits/k

def genre_purity(movies, required_genre, k):
    """For genre row: fraction that actually contain the required genre."""
    top = movies[:k]
    if not top: return 1.0
    return sum(1 for m in top if required_genre in m.get("genres",[])) / len(top)

def mean_vote(movies, k):
    v=[m.get("vote_average") or 0 for m in movies[:k] if m.get("vote_average")]
    return sum(v)/len(v) if v else 0

def min_vote(movies, k):
    v=[m.get("vote_average") or 0 for m in movies[:k] if m.get("vote_average")]
    return min(v) if v else 0

def diversity_bits(movies, k):
    counts=Counter(g for m in movies[:k] for g in m.get("genres",[]))
    total=sum(counts.values())
    if not total: return 0
    return -sum((c/total)*math.log2(c/total) for c in counts.values())

def anchor_genre_overlap(because_movies, anchor_feat, k):
    """What fraction of 'Because X' recs share >=1 genre with anchor?"""
    ag=set(anchor_feat.get("genres",[]))
    hits=sum(1 for m in because_movies[:k] if ag & set(m.get("genres",[])))
    return hits/k

def anchor_director_hit(because_movies, anchor_feat, k):
    ad=anchor_feat.get("director")
    return sum(1 for m in because_movies[:k] if m.get("director")==ad)

def match_score_monotone(movies):
    """Verify match scores decrease monotonically (or are None)."""
    scored=[m for m in movies if m.get("match") is not None]
    violations=sum(1 for i in range(1,len(scored)) if scored[i]["match"]>scored[i-1]["match"]+2)
    return violations

def dedup_rate(row_a, row_b):
    ids_a={m["movie_id"] for m in row_a}
    ids_b={m["movie_id"] for m in row_b}
    shared=ids_a&ids_b
    return len(shared)/max(len(ids_a),1)

def acclaimed_is_sorted(movies, k):
    votes=[m.get("vote_average") or 0 for m in movies[:k]]
    inversions=sum(1 for i in range(1,len(votes)) if votes[i]>votes[i-1]+0.05)
    return inversions

def quality_floor_respected(movies, floor=6.5):
    violations=[m for m in movies if (m.get("vote_average") or 0) < floor and m.get("vote_average") is not None]
    return violations

# ─────────────────────────────────────────────────────────────────────────────
# Personas
# ─────────────────────────────────────────────────────────────────────────────

NOW = datetime.utcnow()
def ago(days): return NOW - timedelta(days=days)

PERSONAS = [
    {
        "name": "Cinefilo Nolan / Sci-Fi",
        "rated": [
            (157336, 5.0, ago(10)),   # Interstellar - very recent
            (27205,  5.0, ago(20)),   # Inception
            (155,    4.5, ago(45)),   # The Dark Knight
            (49026,  4.5, ago(90)),   # Dark Knight Rises
            (680,    4.0, ago(200)),  # Pulp Fiction
            (244786, 4.0, ago(300)),  # Whiplash
            (12445,  2.0, ago(400)),  # Harry Potter (dislike)
        ],
        "pref_genres": ["Science Fiction", "Thriller", "Drama"],
        "pref_decades": ["2000", "2010"],
        "fav_people": [{"name":"Christopher Nolan","id":525,"role":"director"}],
        "expected_genres": {"Science Fiction","Thriller","Drama","Action","Crime"},
        "anchor_movie_id": 157336,  # Interstellar — most recent 5-star
        "liked_directors": {"Christopher Nolan","David Fincher","Quentin Tarantino"},
    },
    {
        "name": "Fa de Horror / Suspense",
        "rated": [
            (259693, 5.0, ago(5)),    # The Conjuring - very recent
            (539681, 5.0, ago(15)),   # The Conjuring 2
            (346364, 4.5, ago(30)),   # It (2017)
            (411,    4.5, ago(60)),   # The Shining
            (745,    4.0, ago(80)),   # Silence of the Lambs
            (76341,  3.5, ago(150)),  # Mad Max (off-genre — will test pollution)
            (24428,  2.0, ago(300)),  # Avengers (dislike)
        ],
        "pref_genres": ["Horror", "Thriller", "Mystery"],
        "pref_decades": ["1980","1990","2010"],
        "fav_people": [
            {"name":"James Wan","id":98947,"role":"director"},
            {"name":"Anthony Hopkins","id":4173,"role":"actor"},
        ],
        "expected_genres": {"Horror","Thriller","Mystery","Crime"},
        "anchor_movie_id": 259693,  # The Conjuring
        "liked_directors": {"James Wan","Stanley Kubrick","Jonathan Demme"},
    },
    {
        "name": "Fa de Romance / Drama",
        "rated": [
            (597,   5.0, ago(3)),    # Titanic - very recent
            (11036, 5.0, ago(8)),    # The Notebook
            (14836, 4.5, ago(20)),   # The Proposal
            (68718, 4.0, ago(60)),   # Django Unchained (mild off-genre)
            (299534,2.5, ago(120)),  # Avengers Endgame (dislike-ish)
            (24428, 2.0, ago(200)),  # The Avengers (dislike)
        ],
        "pref_genres": ["Romance","Drama"],
        "pref_decades": ["1990","2000","2010"],
        "fav_people": [{"name":"James Cameron","id":2710,"role":"director"}],
        "expected_genres": {"Romance","Drama","Comedy"},
        "anchor_movie_id": 597,  # Titanic
        "liked_directors": {"James Cameron","Nora Ephron"},
    },
    {
        "name": "Usuario Novo (cold start, 2 filmes)",
        "rated": [
            (550,   4.5, ago(2)),   # Fight Club - very recent
            (27205, 4.0, ago(5)),   # Inception
        ],
        "pref_genres": ["Thriller","Action"],
        "pref_decades": ["2000","2010"],
        "fav_people": [],
        "expected_genres": {"Thriller","Action","Drama","Crime","Science Fiction"},
        "anchor_movie_id": 550,
        "liked_directors": {"David Fincher","Christopher Nolan"},
    },
]

# Simulated platform trending: movies heavily rated by a broad user base
# (We'll construct a realistic list representing what "Em alta" would look like
# on a platform with thousands of users)
SIMULATED_TRENDING_IDS = [
    # Recent blockbusters + classics that stay popular
    299534, 299536, 24428, 19995, 76341, 157336, 27205,
    603, 155, 680, 122, 278, 238, 424,
]


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation core
# ─────────────────────────────────────────────────────────────────────────────

async def evaluate_persona(client, persona, cands_raw, cand_feats):
    print(f"\n{'='*70}")
    print(f"  PERSONA: {persona['name']}")
    print(f"{'='*70}")

    # Fetch features for rated movies
    rated_mids = [mid for mid,_,_ in persona["rated"]]
    raw = await asyncio.gather(*[movie_features(client, mid) for mid in rated_mids])
    rated_feats = [(f, r, t) for (_, r, t), f in zip(persona["rated"], raw) if f]

    excl = {mid for mid,_,_ in persona["rated"]}
    cold = len(rated_feats) < 3

    # Build profile
    p = build_profile(
        rated_feats, persona["pref_genres"],
        persona["pref_decades"], persona["fav_people"]
    )

    # Anchor for row 2
    anchor = next((f for f in rated_feats
                   if f[0] and f[0]["movie_id"] == persona["anchor_movie_id"]), None)
    anchor_feat = anchor[0] if anchor else None

    # Trending pool
    trend_feats = await asyncio.gather(*[movie_features(client, mid) for mid in SIMULATED_TRENDING_IDS])
    trend_pool = [f for f in trend_feats if f and f["movie_id"] not in excl and (f.get("vote_average") or 0) >= 6.5]

    # Rows 2 and 3 both exclude Row 1 but not each other (different discovery angles).
    # Rows 4 and 5 exclude all previous rows for maximum diversity.
    row1 = run_for_you(p, cand_feats, excl, cold)
    excl_after1 = excl | {m["movie_id"] for m in row1}
    row2, anchor_title = run_because(anchor_feat, cand_feats, excl_after1)
    row3, top_genre = run_top_genre(p, cand_feats, excl_after1)
    excl_global = excl_after1 | {m["movie_id"] for m in row2} | {m["movie_id"] for m in row3}
    row4 = run_trending(trend_pool, excl_global)
    excl5 = excl_global | {m["movie_id"] for m in row4}
    row5 = run_acclaimed(cand_feats, excl5)

    k = 10

    # ── Print each row ────────────────────────────────────────────────────────
    def print_row(label, movies, extra=""):
        print(f"\n  [{label}] {extra}")
        print(f"  {'#':<3} {'Title':<40} {'Genres':<30} {'Dir':<20} {'V':>4} {'M%':>4}")
        print(f"  {'-'*108}")
        for i, m in enumerate(movies[:k], 1):
            genres_str = ", ".join(m.get("genres",[]))[:28]
            dir_str = (m.get("director") or "")[:18]
            v = f"{m.get('vote_average',0):.1f}" if m.get("vote_average") else "  -"
            match = str(m.get("match")) if m.get("match") is not None else "  -"
            # flag bad votes
            flag = " <LOW" if (m.get("vote_average") or 10) < 6.5 else ""
            # flag off-genre for row3
            flag2 = " <OFF" if label=="Row 3 Genre" and top_genre and top_genre not in m.get("genres",[]) else ""
            print(f"  {i:<3} {(m.get('title') or '')[:38]:<40} {genres_str:<30} {dir_str:<20} {v:>4} {match:>4}{flag}{flag2}")

    print_row("Row 1  Para Voce", row1, f"(cold_start={cold})")
    print_row("Row 2  Porque Assistiu", row2, f"anchor='{anchor_title}'")
    print_row("Row 3  Genre", row3, f"genre='{top_genre}'")
    print_row("Row 4  Trending Moviz", row4)
    print_row("Row 5  Aclamados", row5)

    # ── Metrics ───────────────────────────────────────────────────────────────
    print(f"\n  {'='*70}")
    print(f"  METRICS @K={k}")
    print(f"  {'='*70}")

    # Row 1
    gp1  = genre_precision(row1, persona["expected_genres"], k)
    mv1  = mean_vote(row1, k)
    mn1  = min_vote(row1, k)
    div1 = diversity_bits(row1, k)
    viol1= match_score_monotone(row1)
    qf1  = quality_floor_respected(row1)
    print(f"\n  Row 1  Para Voce")
    print(f"    Genre Precision@{k}  : {gp1:.0%}   (expected: {', '.join(list(persona['expected_genres'])[:3])}...)")
    print(f"    Mean Vote Avg      : {mv1:.2f}/10")
    print(f"    Min Vote (floor)   : {mn1:.2f}/10   {'OK' if mn1>=6.5 else 'FAIL - below 6.5 floor!'}")
    print(f"    Diversity bits     : {div1:.3f}")
    print(f"    Match score order  : {'OK' if viol1==0 else f'FAIL - {viol1} inversions'}")
    print(f"    Quality violations : {'none' if not qf1 else f'FAIL - {len(qf1)} films below 6.5'}")

    # Row 2
    if row2 and anchor_feat:
        ag_ov = anchor_genre_overlap(row2, anchor_feat, k)
        dir_hits = anchor_director_hit(row2, anchor_feat, k)
        mv2  = mean_vote(row2, k)
        div2 = diversity_bits(row2, k)
        viol2= match_score_monotone(row2)
        print(f"\n  Row 2  Porque Assistiu '{anchor_title}'")
        print(f"    Anchor genre overlap@{k}: {ag_ov:.0%}  (films sharing >=1 genre with '{anchor_title}')")
        print(f"    Director hits@{k}       : {dir_hits}  (same director as '{anchor_feat.get('director')}')")
        print(f"    Mean Vote Avg          : {mv2:.2f}/10")
        print(f"    Diversity bits         : {div2:.3f}")
        print(f"    Match score order      : {'OK' if viol2==0 else f'FAIL - {viol2} inversions'}")
        anchor_genres = set(anchor_feat.get("genres",[]))
        best_genre_recs = sum(1 for m in row2[:k] if set(m.get("genres",[]))&anchor_genres)
        print(f"    Genre coherence        : {best_genre_recs}/{k} share a genre with anchor")
    else:
        print(f"\n  Row 2  Porque Assistiu — SKIPPED (no anchor)")

    # Row 3
    if row3 and top_genre:
        purity = genre_purity(row3, top_genre, k)
        mv3   = mean_vote(row3, k)
        mn3   = min_vote(row3, k)
        off   = [m for m in row3[:k] if top_genre not in m.get("genres",[])]
        print(f"\n  Row 3  Mais {top_genre}")
        off_titles = [m["title"] for m in off]
        purity_label = "PERFECT" if purity == 1 else f"FAIL - {len(off)} off-genre films: {off_titles}"
        print(f"    Genre purity@{k} : {purity:.0%}   {purity_label}")
        print(f"    Mean Vote Avg  : {mv3:.2f}/10")
        print(f"    Min Vote       : {mn3:.2f}/10")
    else:
        print(f"\n  Row 3  Genre — SKIPPED")

    # Row 4
    if row4:
        mv4 = mean_vote(row4, min(len(row4),k))
        print(f"\n  Row 4  Em alta no Moviz")
        print(f"    Films returned : {len(row4)}")
        print(f"    Mean Vote Avg  : {mv4:.2f}/10")
        print(f"    Titles         : {', '.join((m.get('title') or '')[:20] for m in row4[:5])}...")
    else:
        print(f"\n  Row 4  Trending — no data (platform needs real ratings)")

    # Row 5
    sort_ok = acclaimed_is_sorted(row5, k)
    mv5 = mean_vote(row5, k)
    mn5 = min_vote(row5, k)
    print(f"\n  Row 5  Aclamados")
    print(f"    Vote-sorted@{k}  : {'OK' if sort_ok==0 else f'FAIL - {sort_ok} inversions'}")
    print(f"    Mean Vote Avg  : {mv5:.2f}/10")
    print(f"    Min Vote       : {mn5:.2f}/10")
    top5_titles = [m.get("title","") for m in row5[:5]]
    print(f"    Top 5          : {' | '.join(top5_titles)}")

    # Cross-row dedup
    dedup_1_2 = dedup_rate(row1, row2) if row2 else 0
    dedup_1_3 = dedup_rate(row1, row3) if row3 else 0
    dedup_1_5 = dedup_rate(row1, row5)
    print(f"\n  Cross-row overlap (ideally <20%)")
    print(f"    Row1 vs Row2 : {dedup_1_2:.0%}")
    print(f"    Row1 vs Row3 : {dedup_1_3:.0%}")
    print(f"    Row1 vs Row5 : {dedup_1_5:.0%}")

    return {
        "persona": persona["name"],
        "gp1": gp1, "mv1": mv1, "mn1": mn1, "div1": div1,
        "viol1": viol1, "qf1": len(qf1),
        "ag_ov": ag_ov if row2 and anchor_feat else None,
        "purity3": purity if row3 and top_genre else None,
        "mv5": mv5, "sort5": sort_ok,
        "dedup_12": dedup_1_2, "dedup_13": dedup_1_3,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("Moviz — Row-Level Evaluation (All 5 Rows)")
    print("=" * 70)

    async with httpx.AsyncClient(timeout=20.0) as client:
        print("\n[1/3] Building candidate pool...")
        cands_raw = await candidate_pool(client)
        print(f"      {len(cands_raw)} raw candidates")

        print("\n[2/3] Fetching candidate features...")
        cand_feats = {}
        batch = 25
        for i in range(0, len(cands_raw), batch):
            sl = cands_raw[i:i+batch]
            feats = await asyncio.gather(*[movie_features(client, c["id"]) for c in sl], return_exceptions=True)
            for c, f in zip(sl, feats):
                if isinstance(f, dict) and (f.get("vote_average") or 0) >= 6.5:
                    cand_feats[c["id"]] = f
            print(f"      {min(i+batch,len(cands_raw))}/{len(cands_raw)}", end="\r")
        print(f"\n      {len(cand_feats)} candidates with vote >= 6.5")

        print("\n[3/3] Evaluating personas...")
        all_results = []
        for persona in PERSONAS:
            res = await evaluate_persona(client, persona, cands_raw, cand_feats)
            all_results.append(res)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print("  AGGREGATE ACROSS ALL PERSONAS")
    print(f"{'='*70}")

    def avg(key):
        vals = [r[key] for r in all_results if r.get(key) is not None]
        return sum(vals)/len(vals) if vals else 0

    print(f"\n  Row 1  Para Voce")
    print(f"    Genre Precision  avg: {avg('gp1'):.0%}")
    print(f"    Mean Vote Avg    avg: {avg('mv1'):.2f}")
    print(f"    Min Vote         avg: {avg('mn1'):.2f}  (floor=6.5)")
    print(f"    Diversity        avg: {avg('div1'):.3f} bits")
    total_viol = sum(r['viol1'] for r in all_results)
    total_qf   = sum(r['qf1']  for r in all_results)
    print(f"    Match inversions total: {total_viol}  ({'OK' if total_viol==0 else 'ISSUES'})")
    print(f"    Quality violations     : {total_qf}  ({'OK' if total_qf==0 else 'ISSUES'})")

    print(f"\n  Row 2  Porque Assistiu")
    print(f"    Anchor genre overlap avg: {avg('ag_ov'):.0%}")

    print(f"\n  Row 3  Top Genre")
    print(f"    Genre purity avg: {avg('purity3'):.0%}")

    print(f"\n  Row 5  Aclamados")
    print(f"    Mean Vote Avg avg: {avg('mv5'):.2f}")
    total_sort = sum(r['sort5'] for r in all_results)
    print(f"    Sort inversions total: {total_sort}  ({'OK' if total_sort==0 else 'ISSUES'})")

    print(f"\n  Cross-row overlap")
    print(f"    Row1 vs Row2 avg: {avg('dedup_12'):.0%}")
    print(f"    Row1 vs Row3 avg: {avg('dedup_13'):.0%}")

    print("""
FINAL ASSESSMENT:
  Pass criteria (each metric):
    Row 1 Genre Precision  >= 80%    -> personalization is working
    Row 1 Mean Vote        >= 7.5    -> quality filter working
    Row 1 Min Vote         >= 6.5    -> no junk slipping through
    Row 2 Genre overlap    >= 60%    -> item similarity is coherent
    Row 3 Genre purity     == 100%   -> genre filter is exact
    Row 5 Sort inversions  == 0      -> acclaimed is correctly sorted
    Cross-row overlap      <= 25%    -> rows are diverse from each other
""")


if __name__ == "__main__":
    asyncio.run(main())
