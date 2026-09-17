"""Two-stage recommendation engine — Netflix-style multi-row output.

Architecture
============
Stage 1  Retrieval  — narrows the full catalogue to ~500 personalized candidates
         using multiple recall strategies (all local/DB-based when the similarity
         index is warm; TMDB API as fallback):
           A) item-item similarity index (movie_similarity table)
           B) content filter on MovieFeatureCache (genre + language)
           C) franchise completion (collection_id)
           D) TMDB /similar for anchor film  (external, 1-2 pages)
           E) TMDB top_rated/popular          (external, reduced from 12 → 3 pages)

Stage 2  Ranking    — scores the ~500 candidates with a weighted feature vector:
         genre, keyword, cast, director, decade, language, franchise, quality.
         Uses exponential recency decay on the user taste profile weights.

Stage 3  Row assembly — 5 themed rows with deduplication and director cap.

Background job
==============
compute_movie_similarities(db)  — pre-computes item-item similarity for all
cached movies and stores scores in movie_similarity. Should be triggered once
on startup (async, non-blocking) and periodically (e.g., daily via cron).
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from .models import (
    FavoritePerson,
    MovieFeatureCache,
    MovieSimilarity,
    RatedMovie,
    RecommendationCache,
    UserPreference,
)
from . import tmdb

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────

CACHE_TTL_HOURS = 2
FEATURE_CACHE_TTL_DAYS = 30
COLD_START_THRESHOLD = 3
MAX_CONCURRENT_TMDB = 12
MIN_VOTE_AVG = 6.5
MIN_VOTE_COUNT = 500           # lowered: keywords+similarity more than compensate
DISLIKE_THRESHOLD = 2.5
MAX_PER_DIRECTOR = 2
RECENCY_HALF_LIFE_DAYS = 500  # weight halves every ~1.4 years

# Retrieval
SIMILARITY_TOP_K = 25          # top-K similar movies per rated film
RETRIEVAL_TMDB_PAGES = 3       # TMDB API pages (down from 12) — fallback only
MAX_CANDIDATES = 500           # hard cap before ranking

# Scoring weights
W_GENRE = 2.0
W_KEYWORD = 1.8
W_CAST = 0.5
W_DIRECTOR = 3.0
W_DECADE = 0.8
W_LANGUAGE = 1.2
W_FRANCHISE = 2.5
W_QUALITY = 1.5

# Onboarding boosts
BOOST_ONBOARDING_GENRE = 1.5
BOOST_ONBOARDING_PERSON = 0.5

CAST_POSITION_WEIGHTS = [1.0, 0.75, 0.5, 0.33, 0.33, 0.33, 0.33, 0.33, 0.33, 0.33]

GENRE_NAME_TO_TMDB_ID: dict[str, int] = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35,
    "Crime": 80, "Documentary": 99, "Drama": 18, "Family": 10751,
    "Fantasy": 14, "History": 36, "Horror": 27, "Music": 10402,
    "Mystery": 9648, "Romance": 10749, "Science Fiction": 878,
    "Thriller": 53, "War": 10752, "Western": 37,
}

PTBR_TO_EN: dict[str, str] = {
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
    "Ficcao cientifica": "Science Fiction",
    "Ficção Científica": "Science Fiction",
    "Suspense": "Thriller",
    "Guerra": "War",
    "Faroeste": "Western",
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.utcnow()


def _normalize_genre(genre: str) -> str:
    return PTBR_TO_EN.get(genre, genre)


def _recency_weight(rated_at: datetime) -> float:
    days_ago = max((_utcnow() - rated_at).days, 0)
    return math.exp(-math.log(2) * days_ago / RECENCY_HALF_LIFE_DAYS)


def _to_match_score(score: float, min_s: float, max_s: float) -> int:
    if max_s == min_s:
        return 82
    return round(55 + (score - min_s) / (max_s - min_s) * 44)


def _get_reason_tags(profile: dict, features: dict) -> list[str]:
    genres = features.get("genres") or []
    tags = [
        g for g, _ in sorted(
            ((g, profile["genres"].get(g, 0)) for g in genres),
            key=lambda x: -x[1],
        )
        if profile["genres"].get(g, 0) > 0
    ][:2]
    director = features.get("director")
    if director and profile["directors"].get(director, 0) > 0.3:
        tags.append(director)
    return tags[:3]


def _row_to_features(row: MovieFeatureCache) -> dict:
    return {
        "movie_id": row.movie_id,
        "title": row.title,
        "genres": json.loads(row.genres_json or "[]"),
        "cast": json.loads(row.cast_json or "[]"),
        "director": row.director,
        "release_year": row.release_year,
        "tmdb_vote_avg": row.tmdb_vote_avg,
        "tmdb_vote_count": row.tmdb_vote_count,
        "poster_path": row.poster_path,
        "overview": row.overview,
        "keywords": json.loads(row.keywords_json or "[]"),
        "original_language": row.original_language,
        "runtime": row.runtime,
        "popularity": row.popularity,
        "collection_id": row.collection_id,
    }


def _make_item(
    features: dict,
    match_score: Optional[int],
    reason_tags: list[str],
    release_date: Optional[str] = None,
) -> dict:
    return {
        "movie_id": features["movie_id"],
        "title": features["title"],
        "poster_path": features["poster_path"],
        "release_date": release_date or (
            f"{features['release_year']}-01-01" if features.get("release_year") else None
        ),
        "vote_average": features["tmdb_vote_avg"],
        "genres": features["genres"],
        "overview": features["overview"],
        "match_score": match_score,
        "reason_tags": reason_tags,
    }


# ── item-item similarity (used for both the retrieval index and row 2) ─────────

def _item_similarity(source: dict, candidate: dict) -> float:
    score = 0.0

    # Genre Jaccard
    s_genres = set(source.get("genres") or [])
    c_genres = set(candidate.get("genres") or [])
    union = s_genres | c_genres
    if union:
        score += (len(s_genres & c_genres) / len(union)) * 4.0

    # Keyword Jaccard — strongest discriminating signal
    s_kw = set(source.get("keywords") or [])
    c_kw = set(candidate.get("keywords") or [])
    kw_union = s_kw | c_kw
    if kw_union:
        score += (len(s_kw & c_kw) / len(kw_union)) * 5.0

    # Same director
    if source.get("director") and source["director"] == candidate.get("director"):
        score += 3.0

    # Cast overlap (top 5 billed)
    s_cast = set(list(source.get("cast") or [])[:5])
    c_cast = set(list(candidate.get("cast") or [])[:5])
    cast_union = s_cast | c_cast
    if cast_union:
        score += (len(s_cast & c_cast) / len(cast_union)) * 2.0

    # Same decade
    s_yr = source.get("release_year")
    c_yr = candidate.get("release_year")
    if s_yr and c_yr and (s_yr // 10) == (c_yr // 10):
        score += 0.5

    # Same original language
    if source.get("original_language") and source["original_language"] == candidate.get("original_language"):
        score += 1.0

    # Same franchise
    s_col = source.get("collection_id")
    c_col = candidate.get("collection_id")
    if s_col and c_col and s_col == c_col:
        score += 4.0

    # Quality floor
    vote_avg = candidate.get("tmdb_vote_avg") or 0.0
    score += (vote_avg / 10.0) * 1.5

    return score


# ── background job: pre-compute movie_similarity index ─────────────────────────

_similarity_job_running = False


async def compute_movie_similarities(db: Session) -> int:
    """Build/refresh the movie_similarity index from cached movies.

    Computes pairwise similarity for all quality movies in MovieFeatureCache
    and stores the top-SIMILARITY_TOP_K neighbors per movie.

    Returns the number of (movie_id, similar_id) pairs written.
    """
    global _similarity_job_running
    if _similarity_job_running:
        return 0
    _similarity_job_running = True

    try:
        all_rows = db.query(MovieFeatureCache).filter(
            MovieFeatureCache.tmdb_vote_avg >= MIN_VOTE_AVG,
            MovieFeatureCache.tmdb_vote_count >= MIN_VOTE_COUNT,
        ).all()

        if len(all_rows) < 2:
            return 0

        features = [_row_to_features(r) for r in all_rows]
        id_to_feat = {f["movie_id"]: f for f in features}
        total_written = 0

        for source in features:
            src_id = source["movie_id"]
            scored = [
                (cand["movie_id"], _item_similarity(source, cand))
                for cand in features
                if cand["movie_id"] != src_id
            ]
            scored.sort(key=lambda x: -x[1])
            top = scored[:SIMILARITY_TOP_K]

            for similar_id, sim_score in top:
                existing = db.query(MovieSimilarity).filter(
                    MovieSimilarity.movie_id == src_id,
                    MovieSimilarity.similar_id == similar_id,
                ).first()
                if existing:
                    existing.score = sim_score
                else:
                    db.add(MovieSimilarity(
                        movie_id=src_id,
                        similar_id=similar_id,
                        score=sim_score,
                    ))
                total_written += 1

            # Yield control and commit in batches so we don't block the event loop
            await asyncio.sleep(0)

        try:
            db.commit()
        except Exception:
            db.rollback()

        logger.info("movie_similarity: wrote %d pairs for %d movies", total_written, len(features))
        return total_written

    finally:
        _similarity_job_running = False


def _similarity_index_size(db: Session) -> int:
    return db.query(MovieSimilarity).count()


# ── TMDB feature cache ─────────────────────────────────────────────────────────

async def _fetch_and_cache_features(
    db: Session, movie_id: int, semaphore: asyncio.Semaphore
) -> Optional[dict]:
    cached = db.query(MovieFeatureCache).filter(
        MovieFeatureCache.movie_id == movie_id
    ).first()

    cutoff = _utcnow() - timedelta(days=FEATURE_CACHE_TTL_DAYS)
    if cached and cached.cached_at > cutoff:
        # Re-fetch if keywords are missing (upgrade existing cache rows)
        has_keywords = bool(cached.keywords_json and cached.keywords_json != "[]")
        if has_keywords:
            return _row_to_features(cached)

    async with semaphore:
        try:
            raw = await tmdb.fetch_movie_features_raw(movie_id)
        except Exception as exc:
            logger.warning("TMDB features failed for %s: %s", movie_id, exc)
            return _row_to_features(cached) if cached else None

    release_year: Optional[int] = None
    rd = raw.get("release_date") or ""
    if rd:
        try:
            release_year = int(rd[:4])
        except ValueError:
            pass

    genres = raw.get("genres") or []
    cast_ids = [c["id"] for c in (raw.get("cast") or [])]
    director = raw.get("director")
    keywords = raw.get("keywords") or []

    fields = dict(
        title=raw.get("title"),
        genres_json=json.dumps(genres),
        cast_json=json.dumps(cast_ids),
        director=director,
        release_year=release_year,
        tmdb_vote_avg=raw.get("vote_average"),
        tmdb_vote_count=raw.get("vote_count"),
        poster_path=raw.get("poster_path"),
        overview=raw.get("overview"),
        cached_at=_utcnow(),
        keywords_json=json.dumps(keywords),
        original_language=raw.get("original_language"),
        runtime=raw.get("runtime"),
        popularity=raw.get("popularity"),
        collection_id=raw.get("collection_id"),
    )

    if cached:
        for k, v in fields.items():
            setattr(cached, k, v)
    else:
        cached = MovieFeatureCache(movie_id=movie_id, **fields)
        db.add(cached)

    try:
        db.commit()
    except Exception:
        db.rollback()

    return {
        "movie_id": movie_id,
        "title": fields["title"],
        "genres": genres,
        "cast": cast_ids,
        "director": director,
        "release_year": release_year,
        "tmdb_vote_avg": fields["tmdb_vote_avg"],
        "tmdb_vote_count": fields["tmdb_vote_count"],
        "poster_path": fields["poster_path"],
        "overview": fields["overview"],
        "keywords": keywords,
        "original_language": fields["original_language"],
        "runtime": fields["runtime"],
        "popularity": fields["popularity"],
        "collection_id": fields["collection_id"],
    }


# ── user taste profile ─────────────────────────────────────────────────────────

def _build_user_taste_profile(db: Session, user_id: int) -> dict:
    profile: dict = {
        "genres": {},
        "cast": {},
        "directors": {},
        "decades": {},
        "keywords": {},       # keyword_id (int) → weighted score
        "languages": {},      # "en" / "pt" / "ja" → weighted score
        "runtime_sum": 0.0,   # for computing weighted average runtime preference
        "runtime_weight": 0.0,
        "franchise_ids": set(),
    }

    ratings = db.query(RatedMovie).filter(RatedMovie.user_id == user_id).all()
    for rated in ratings:
        positive = rated.rating >= DISLIKE_THRESHOLD
        base_w = (rated.rating / 5.0) ** 2 if positive else -((5.0 - rated.rating) / 5.0) ** 2
        recency = _recency_weight(rated.rated_at)
        weight = base_w * recency

        row = db.query(MovieFeatureCache).filter(
            MovieFeatureCache.movie_id == rated.movie_id
        ).first()
        if not row:
            continue

        genres = json.loads(row.genres_json or "[]")
        genre_count = max(len(genres), 1)
        for genre in genres:
            profile["genres"][genre] = profile["genres"].get(genre, 0.0) + weight / genre_count

        cast_ids = json.loads(row.cast_json or "[]")
        for pos, actor_id in enumerate(cast_ids):
            pw = CAST_POSITION_WEIGHTS[min(pos, len(CAST_POSITION_WEIGHTS) - 1)]
            profile["cast"][actor_id] = profile["cast"].get(actor_id, 0.0) + weight * pw

        if row.director:
            profile["directors"][row.director] = profile["directors"].get(row.director, 0.0) + weight

        if row.release_year:
            decade = str((row.release_year // 10) * 10)
            profile["decades"][decade] = profile["decades"].get(decade, 0.0) + weight

        # Keywords
        keywords = json.loads(row.keywords_json or "[]")
        kw_count = max(len(keywords), 1)
        for kw_id in keywords:
            profile["keywords"][kw_id] = profile["keywords"].get(kw_id, 0.0) + weight / kw_count

        # Language
        if row.original_language:
            profile["languages"][row.original_language] = (
                profile["languages"].get(row.original_language, 0.0) + abs(weight)
            )

        # Runtime preference (only positive ratings)
        if positive and row.runtime and row.runtime > 0:
            profile["runtime_sum"] += row.runtime * abs(weight)
            profile["runtime_weight"] += abs(weight)

        # Franchise
        if row.collection_id and positive:
            profile["franchise_ids"].add(row.collection_id)

    # Onboarding preferences
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    if pref:
        for raw_genre in json.loads(pref.favorite_genres or "[]"):
            genre = _normalize_genre(raw_genre)
            profile["genres"][genre] = profile["genres"].get(genre, 0.0) + BOOST_ONBOARDING_GENRE
        for decade in json.loads(pref.preferred_decades or "[]"):
            decade_str = str(decade)
            profile["decades"][decade_str] = profile["decades"].get(decade_str, 0.0) + BOOST_ONBOARDING_GENRE
        for lang in json.loads(getattr(pref, "preferred_languages", None) or "[]"):
            profile["languages"][lang] = profile["languages"].get(lang, 0.0) + 1.0

    fav_people = db.query(FavoritePerson).filter(FavoritePerson.user_id == user_id).all()
    for person in fav_people:
        if person.role and person.role.lower() == "director":
            profile["directors"][person.name] = (
                profile["directors"].get(person.name, 0.0) + BOOST_ONBOARDING_PERSON
            )
        else:
            profile["cast"][person.person_id] = (
                profile["cast"].get(person.person_id, 0.0) + BOOST_ONBOARDING_PERSON
            )

    return profile


# ── scoring ────────────────────────────────────────────────────────────────────

def _score_movie(profile: dict, features: dict) -> float:
    score = 0.0

    genres = features.get("genres") or []
    genre_count = max(len(genres), 1)
    for genre in genres:
        score += (profile["genres"].get(genre, 0.0) / genre_count) * W_GENRE

    keywords = features.get("keywords") or []
    kw_count = max(len(keywords), 1)
    for kw_id in keywords:
        score += (profile["keywords"].get(kw_id, 0.0) / kw_count) * W_KEYWORD

    cast_ids = features.get("cast") or []
    for pos, actor_id in enumerate(cast_ids):
        pw = CAST_POSITION_WEIGHTS[min(pos, len(CAST_POSITION_WEIGHTS) - 1)]
        score += profile["cast"].get(actor_id, 0.0) * W_CAST * pw

    director = features.get("director")
    if director:
        score += profile["directors"].get(director, 0.0) * W_DIRECTOR

    release_year = features.get("release_year")
    if release_year:
        decade = str((release_year // 10) * 10)
        score += profile["decades"].get(decade, 0.0) * W_DECADE

    lang = features.get("original_language")
    if lang:
        score += profile["languages"].get(lang, 0.0) * W_LANGUAGE

    coll_id = features.get("collection_id")
    if coll_id and coll_id in profile.get("franchise_ids", set()):
        score += W_FRANCHISE

    vote_avg = features.get("tmdb_vote_avg") or 0.0
    score += (vote_avg / 10.0) * W_QUALITY

    return score


def _score_movie_cold_start(profile: dict, features: dict) -> float:
    score = 0.0
    genres = features.get("genres") or []
    genre_count = max(len(genres), 1)
    for genre in genres:
        score += (profile["genres"].get(genre, 0.0) / genre_count) * W_GENRE
    lang = features.get("original_language")
    if lang:
        score += profile["languages"].get(lang, 0.0) * W_LANGUAGE
    vote_avg = features.get("tmdb_vote_avg") or 0.0
    score += (vote_avg / 10.0) * W_QUALITY * 3
    return score


# ── deduplication ──────────────────────────────────────────────────────────────

def _dedup_director(
    scored: list[tuple[float, dict]], max_per: int
) -> list[tuple[float, dict]]:
    counts: dict[str, int] = {}
    result = []
    for s, feat in scored:
        d = feat.get("director") or "__none__"
        if counts.get(d, 0) < max_per:
            counts[d] = counts.get(d, 0) + 1
            result.append((s, feat))
    return result


# ── Stage 1: Retrieval ─────────────────────────────────────────────────────────

def _retrieval_from_similarity_db(
    db: Session,
    liked_movie_ids: list[int],
    excluded: set[int],
) -> dict[int, dict]:
    """Strategy A: look up pre-computed neighbors in movie_similarity table."""
    if not liked_movie_ids:
        return {}

    rows = (
        db.query(MovieSimilarity)
        .filter(
            MovieSimilarity.movie_id.in_(liked_movie_ids),
            MovieSimilarity.similar_id.notin_(excluded),
        )
        .order_by(MovieSimilarity.score.desc())
        .limit(SIMILARITY_TOP_K * len(liked_movie_ids))
        .all()
    )

    similar_ids = list({r.similar_id for r in rows})
    if not similar_ids:
        return {}

    cached_rows = db.query(MovieFeatureCache).filter(
        MovieFeatureCache.movie_id.in_(similar_ids),
        MovieFeatureCache.tmdb_vote_avg >= MIN_VOTE_AVG,
        MovieFeatureCache.tmdb_vote_count >= MIN_VOTE_COUNT,
    ).all()

    return {r.movie_id: _row_to_features(r) for r in cached_rows}


def _retrieval_from_content_filter(
    db: Session,
    top_genre_names: list[str],
    preferred_languages: list[str],
    excluded: set[int],
    limit: int = 200,
) -> dict[int, dict]:
    """Strategy B: query MovieFeatureCache by genre/language — no TMDB API call."""
    if not top_genre_names:
        return {}

    q = db.query(MovieFeatureCache).filter(
        MovieFeatureCache.tmdb_vote_avg >= MIN_VOTE_AVG,
        MovieFeatureCache.tmdb_vote_count >= MIN_VOTE_COUNT,
        MovieFeatureCache.movie_id.notin_(excluded),
    )

    rows = q.order_by(MovieFeatureCache.tmdb_vote_avg.desc()).limit(limit * 3).all()

    result = {}
    for row in rows:
        if len(result) >= limit:
            break
        genres = json.loads(row.genres_json or "[]")
        lang = row.original_language
        genre_match = any(g in top_genre_names for g in genres)
        lang_match = (not preferred_languages) or (lang in preferred_languages)
        if genre_match or lang_match:
            result[row.movie_id] = _row_to_features(row)

    return result


def _retrieval_franchise(
    db: Session,
    franchise_ids: set[int],
    excluded: set[int],
) -> dict[int, dict]:
    """Strategy C: surface unwatched sequels/prequels from franchises the user likes."""
    if not franchise_ids:
        return {}

    rows = db.query(MovieFeatureCache).filter(
        MovieFeatureCache.collection_id.in_(list(franchise_ids)),
        MovieFeatureCache.movie_id.notin_(excluded),
        MovieFeatureCache.tmdb_vote_avg >= MIN_VOTE_AVG,
    ).all()

    return {r.movie_id: _row_to_features(r) for r in rows}


async def _retrieval_tmdb_fallback(
    db: Session,
    user_id: int,
    rated_ids: set[int],
    semaphore: asyncio.Semaphore,
    top_genre_ids: list[int],
    anchor_id: Optional[int],
) -> dict[int, dict]:
    """Strategies D+E: TMDB API calls — used when DB retrieval yields < 50 candidates."""

    async def _page(endpoint: str, page: int, genre_ids: list[int] = [],
                    mid: Optional[int] = None) -> list[dict]:
        try:
            if endpoint == "top_rated":
                data = await tmdb.fetch_top_rated_movies(page)
            elif endpoint == "popular":
                data = await tmdb.fetch_popular_movies(page)
            elif endpoint == "similar" and mid:
                data = await tmdb.fetch_similar_movies(mid, page)
            else:
                data = await tmdb.fetch_discover_movies(genre_ids, page)
            return data.get("results") or []
        except Exception:
            return []

    pages = [
        _page("top_rated", 1), _page("top_rated", 2), _page("top_rated", 3),
        _page("popular", 1),
        *([_page("discover", 1, top_genre_ids), _page("discover", 2, top_genre_ids)]
          if top_genre_ids else []),
        *([_page("similar", 1, mid=anchor_id), _page("similar", 2, mid=anchor_id)]
          if anchor_id else []),
    ]

    all_results = await asyncio.gather(*pages)

    seen: set[int] = set()
    candidates: list[dict] = []
    for page_results in all_results:
        for movie in page_results:
            mid = movie.get("id")
            if mid and mid not in seen and mid not in rated_ids:
                seen.add(mid)
                candidates.append(movie)

    candidates = candidates[:150]

    raw_feats = await asyncio.gather(
        *[_fetch_and_cache_features(db, c["id"], semaphore) for c in candidates],
        return_exceptions=True,
    )

    result: dict[int, dict] = {}
    for cand, feat in zip(candidates, raw_feats):
        if (isinstance(feat, dict)
                and (feat.get("tmdb_vote_avg") or 0) >= MIN_VOTE_AVG
                and (feat.get("tmdb_vote_count") or 0) >= MIN_VOTE_COUNT):
            result[cand["id"]] = feat

    return result


async def _build_candidate_pool(
    db: Session,
    user_id: int,
    rated_ids: set[int],
    semaphore: asyncio.Semaphore,
    profile: dict,
) -> dict[int, dict]:
    """Multi-strategy retrieval: DB-first, TMDB as fallback."""

    excluded = rated_ids

    liked_ids = [
        r.movie_id for r in
        db.query(RatedMovie.movie_id)
        .filter(RatedMovie.user_id == user_id, RatedMovie.rating >= 3.5)
        .order_by(RatedMovie.rated_at.desc())
        .limit(20)
        .all()
    ]

    anchor_id: Optional[int] = liked_ids[0] if liked_ids else None

    top_genre_names = [
        g for g, _ in sorted(profile["genres"].items(), key=lambda x: -x[1])
        if _ > 0
    ][:5]
    preferred_languages = [
        lang for lang, _ in sorted(profile["languages"].items(), key=lambda x: -x[1])
        if _ > 0
    ][:3]
    top_genre_ids = [
        GENRE_NAME_TO_TMDB_ID[g] for g in top_genre_names if g in GENRE_NAME_TO_TMDB_ID
    ]

    # ── Strategy A: similarity index
    pool_a = _retrieval_from_similarity_db(db, liked_ids, excluded)

    # ── Strategy B: content filter on cache
    pool_b = _retrieval_from_content_filter(
        db, top_genre_names, preferred_languages, excluded | set(pool_a)
    )

    # ── Strategy C: franchise completion
    pool_c = _retrieval_franchise(db, profile.get("franchise_ids", set()), excluded | set(pool_a) | set(pool_b))

    combined = {**pool_a, **pool_b, **pool_c}

    # ── Strategy D+E: TMDB fallback if DB retrieval is thin
    if len(combined) < 50:
        pool_tmdb = await _retrieval_tmdb_fallback(
            db, user_id, rated_ids, semaphore, top_genre_ids, anchor_id
        )
        combined.update(pool_tmdb)

        # Also cache rated movie features (needed for profile building next run)
        uncached_rated = [
            mid for mid in list(rated_ids)[:20]
            if not db.query(MovieFeatureCache).filter(MovieFeatureCache.movie_id == mid).first()
        ]
        if uncached_rated:
            await asyncio.gather(
                *[_fetch_and_cache_features(db, mid, semaphore) for mid in uncached_rated],
                return_exceptions=True,
            )

    # Remove any that slipped through the exclusion filter
    for mid in list(excluded):
        combined.pop(mid, None)

    # Hard cap
    if len(combined) > MAX_CANDIDATES:
        combined = dict(list(combined.items())[:MAX_CANDIDATES])

    return combined


# ── individual rows ────────────────────────────────────────────────────────────

def _row_for_you(
    profile: dict,
    candidate_features: dict[int, dict],
    excluded: set[int],
    cold_start: bool,
    limit: int = 20,
) -> list[dict]:
    fn = _score_movie_cold_start if cold_start else _score_movie
    scored = [
        (fn(profile, feat), feat)
        for mid, feat in candidate_features.items()
        if mid not in excluded
    ]
    if not scored:
        return []

    scored.sort(key=lambda x: -x[0])
    deduped = _dedup_director(scored, MAX_PER_DIRECTOR)

    min_s = deduped[-1][0] if deduped else 0
    max_s = deduped[0][0] if deduped else 1

    return [
        _make_item(feat, _to_match_score(s, min_s, max_s), _get_reason_tags(profile, feat))
        for s, feat in deduped[:limit]
    ]


def _row_because_watched(
    db: Session,
    user_id: int,
    candidate_features: dict[int, dict],
    excluded: set[int],
    limit: int = 20,
) -> tuple[list[dict], Optional[str]]:
    anchor = (
        db.query(RatedMovie)
        .filter(RatedMovie.user_id == user_id, RatedMovie.rating >= 4.0)
        .order_by(RatedMovie.rated_at.desc())
        .first()
    )
    if not anchor:
        return [], None

    source_feat = db.query(MovieFeatureCache).filter(
        MovieFeatureCache.movie_id == anchor.movie_id
    ).first()
    if not source_feat:
        return [], None

    source = _row_to_features(source_feat)

    scored = [
        (_item_similarity(source, feat), feat)
        for mid, feat in candidate_features.items()
        if mid not in excluded
    ]
    scored.sort(key=lambda x: -x[0])
    deduped = _dedup_director(scored, MAX_PER_DIRECTOR)

    min_s = deduped[-1][0] if deduped else 0
    max_s = deduped[0][0] if deduped else 1

    movies = [
        _make_item(feat, _to_match_score(s, min_s, max_s), source.get("genres", [])[:2])
        for s, feat in deduped[:limit]
    ]
    return movies, anchor.title


def _row_top_genre(
    profile: dict,
    candidate_features: dict[int, dict],
    excluded: set[int],
    limit: int = 20,
) -> tuple[list[dict], Optional[str]]:
    if not profile["genres"]:
        return [], None

    top_genre = max(profile["genres"].items(), key=lambda x: x[1])[0]
    if profile["genres"][top_genre] <= 0:
        return [], None

    genre_candidates = [
        feat for mid, feat in candidate_features.items()
        if mid not in excluded and top_genre in (feat.get("genres") or [])
    ]
    genre_candidates.sort(key=lambda f: -(f.get("tmdb_vote_avg") or 0))

    top_n = genre_candidates[:limit]
    if not top_n:
        return [], top_genre
    mn = min(f.get("tmdb_vote_avg") or MIN_VOTE_AVG for f in top_n)
    mx = max(f.get("tmdb_vote_avg") or MIN_VOTE_AVG for f in top_n)
    movies = [
        _make_item(feat, _to_match_score(feat.get("tmdb_vote_avg") or MIN_VOTE_AVG, mn, mx), [top_genre])
        for feat in top_n
    ]
    return movies, top_genre


def _row_trending_moviz(
    db: Session,
    user_id: int,
    excluded: set[int],
    candidate_features: dict[int, dict],
    limit: int = 20,
) -> list[dict]:
    cutoff = _utcnow() - timedelta(days=30)

    def _query_trending(min_count: int, cutoff_dt) -> list:
        return (
            db.query(
                RatedMovie.movie_id,
                RatedMovie.title,
                RatedMovie.poster_path,
                RatedMovie.release_date,
                sqlfunc.avg(RatedMovie.rating).label("avg_rating"),
                sqlfunc.count(RatedMovie.id).label("cnt"),
            )
            .filter(RatedMovie.rated_at >= cutoff_dt, RatedMovie.rating >= 3.0)
            .group_by(RatedMovie.movie_id)
            .having(sqlfunc.count(RatedMovie.id) >= min_count)
            .order_by(sqlfunc.count(RatedMovie.id).desc(), sqlfunc.avg(RatedMovie.rating).desc())
            .limit(limit * 3)
            .all()
        )

    rows = _query_trending(2, cutoff)
    if len(rows) < 5:
        rows = _query_trending(1, cutoff)
    if len(rows) < 5:
        rows = (
            db.query(
                RatedMovie.movie_id,
                RatedMovie.title,
                RatedMovie.poster_path,
                RatedMovie.release_date,
                sqlfunc.avg(RatedMovie.rating).label("avg_rating"),
                sqlfunc.count(RatedMovie.id).label("cnt"),
            )
            .filter(RatedMovie.rating >= 3.0)
            .group_by(RatedMovie.movie_id)
            .order_by(sqlfunc.count(RatedMovie.id).desc(), sqlfunc.avg(RatedMovie.rating).desc())
            .limit(limit * 3)
            .all()
        )

    movies = []
    seen: set[int] = set()
    for row in rows:
        if row.movie_id in excluded or row.movie_id in seen:
            continue
        seen.add(row.movie_id)
        cached = db.query(MovieFeatureCache).filter(
            MovieFeatureCache.movie_id == row.movie_id
        ).first()
        movies.append({
            "movie_id": row.movie_id,
            "title": row.title,
            "poster_path": row.poster_path,
            "release_date": row.release_date,
            "vote_average": cached.tmdb_vote_avg if cached else None,
            "genres": json.loads(cached.genres_json) if cached else [],
            "overview": cached.overview if cached else None,
            "match_score": None,
            "reason_tags": ["Em alta no Moviz"],
        })
        if len(movies) >= limit:
            break

    return movies


def _row_acclaimed(
    candidate_features: dict[int, dict],
    excluded: set[int],
    limit: int = 20,
) -> list[dict]:
    eligible = [
        feat for mid, feat in candidate_features.items()
        if mid not in excluded and (feat.get("tmdb_vote_count") or 0) >= MIN_VOTE_COUNT
    ]
    C = 7.5
    m = float(MIN_VOTE_COUNT)

    def _bayesian(feat: dict) -> float:
        v = float(feat.get("tmdb_vote_count") or 0)
        r = float(feat.get("tmdb_vote_avg") or 0)
        return (v * r + m * C) / (v + m) if (v + m) > 0 else C

    top = sorted(eligible, key=lambda f: -_bayesian(f))
    return [
        _make_item(feat, None, feat.get("genres", [])[:2])
        for feat in top[:limit]
    ]


# ── main entry point ───────────────────────────────────────────────────────────

async def get_recommendation_rows(db: Session, user_id: int) -> list[dict]:
    """Return 5 themed recommendation rows, cached for CACHE_TTL_HOURS.

    Also triggers the similarity index build in the background when the index
    is empty (first run or after a cold deploy).
    """
    cache = db.query(RecommendationCache).filter(
        RecommendationCache.user_id == user_id
    ).first()
    ttl = timedelta(hours=CACHE_TTL_HOURS)
    if cache and (_utcnow() - cache.computed_at) < ttl:
        try:
            return json.loads(cache.rows_json)
        except Exception:
            pass

    # Kick off similarity index build in the background when empty.
    # Uses its own DB session so it doesn't share state with the request session.
    if _similarity_index_size(db) == 0:
        from .database import SessionLocal as _SL
        async def _bg_sim():
            _db = _SL()
            try:
                await compute_movie_similarities(_db)
            finally:
                _db.close()
        asyncio.create_task(_bg_sim())

    try:
        rows = await asyncio.wait_for(
            _compute_recommendation_rows(db, user_id, cache),
            timeout=45.0,
        )
        return rows
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("Recommendation compute failed (%s) — serving stale cache", exc)
        if cache:
            try:
                return json.loads(cache.rows_json)
            except Exception:
                pass
        return []


async def _compute_recommendation_rows(db: Session, user_id: int, cache) -> list[dict]:
    rated_ids: set[int] = {
        r.movie_id
        for r in db.query(RatedMovie.movie_id).filter(RatedMovie.user_id == user_id).all()
    }

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TMDB)

    # Build profile first — needed by retrieval to know top genres/languages
    profile = _build_user_taste_profile(db, user_id)
    cold_start = len(rated_ids) < COLD_START_THRESHOLD

    candidate_features = await _build_candidate_pool(
        db, user_id, rated_ids, semaphore, profile
    )

    excl_row1 = rated_ids
    for_you = _row_for_you(profile, candidate_features, excl_row1, cold_start)

    excl_after_row1 = excl_row1 | {m["movie_id"] for m in for_you}
    because_movies, anchor_title = _row_because_watched(
        db, user_id, candidate_features, excl_after_row1
    )
    genre_movies, top_genre = _row_top_genre(profile, candidate_features, excl_after_row1)

    excl_global = excl_after_row1 | {m["movie_id"] for m in because_movies} | {m["movie_id"] for m in genre_movies}
    trending_movies = _row_trending_moviz(db, user_id, excl_global, candidate_features)

    excl_acclaimed = excl_global | {m["movie_id"] for m in trending_movies}
    acclaimed = _row_acclaimed(candidate_features, excl_acclaimed)

    rows: list[dict] = []

    rows.append({
        "row_id": "for_you",
        "title": "Recomendados para voce",
        "reason": None,
        "movies": for_you,
    })

    if because_movies:
        rows.append({
            "row_id": "because_watched",
            "title": f"Porque voce assistiu {anchor_title}",
            "reason": anchor_title,
            "movies": because_movies,
        })

    if genre_movies and top_genre:
        rows.append({
            "row_id": f"genre_{top_genre.lower().replace(' ', '_')}",
            "title": f"Mais {top_genre} para voce",
            "reason": top_genre,
            "movies": genre_movies,
        })

    if trending_movies:
        rows.append({
            "row_id": "trending_moviz",
            "title": "Em alta no Moviz",
            "reason": None,
            "movies": trending_movies,
        })

    rows.append({
        "row_id": "acclaimed",
        "title": "Aclamados pela critica",
        "reason": None,
        "movies": acclaimed,
    })

    rows_json = json.dumps(rows, ensure_ascii=False, default=str)
    if cache:
        cache.rows_json = rows_json
        cache.computed_at = _utcnow()
    else:
        db.add(RecommendationCache(
            user_id=user_id,
            rows_json=rows_json,
            computed_at=_utcnow(),
        ))
    try:
        db.commit()
    except Exception:
        db.rollback()

    return rows


# ── legacy single-list endpoint ────────────────────────────────────────────────

async def get_recommendations(db: Session, user_id: int, limit: int = 20) -> list[dict]:
    rows = await get_recommendation_rows(db, user_id)
    for_you = next((r["movies"] for r in rows if r["row_id"] == "for_you"), [])
    return for_you[:limit]
