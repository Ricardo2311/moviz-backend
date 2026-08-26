"""Content-based recommendation engine — Netflix-style multi-row output.

Architecture:
  get_recommendation_rows()  — main entry point, returns 5 themed rows,
                               result cached in DB for CACHE_TTL_HOURS.

  Row 1  "Para voce"           — personalized content-based scoring
  Row 2  "Porque voce assistiu X" — item-item similarity to last liked film
  Row 3  "Mais [Genre] para voce"  — top films in user's #1 genre
  Row 4  "Em alta no Moviz"    — most rated/reviewed on the platform this month
  Row 5  "Aclamados pela critica" — TMDB top-rated regardless of taste

Scoring model (full profile):
  score = genre_match / genre_count * W_GENRE
        + cast_match (position-weighted) * W_CAST
        + director_match * W_DIRECTOR
        + decade_match * W_DECADE
        + quality * W_QUALITY

  rating weights use exponential recency decay:
      w = (rating/5)^2 * exp(-0.5 * days_since_rating / 365)
  negative ratings (< DISLIKE_THRESHOLD) subtract weight.

Match score: min-max normalised across all scored candidates → [55, 99]%.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone


def _utcnow() -> datetime:
    """Timezone-naive UTC timestamp; compatible with SQLite naive datetime columns."""
    return _utcnow().replace(tzinfo=None)
from typing import Optional

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from .models import (
    FavoritePerson,
    MovieFeatureCache,
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
MAX_CONCURRENT_TMDB = 16
MIN_VOTE_AVG = 6.5
MIN_VOTE_COUNT = 1_000          # avoid obscure films with few votes inflating scores
DISLIKE_THRESHOLD = 2.5
MAX_PER_DIRECTOR = 2
RECENCY_HALF_LIFE_DAYS = 500   # weight halves every ~1.4 years

W_GENRE = 2.0
W_CAST = 0.5
W_DIRECTOR = 3.0
W_DECADE = 0.8
W_QUALITY = 1.5

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

def _normalize_genre(genre: str) -> str:
    return PTBR_TO_EN.get(genre, genre)


def _recency_weight(rated_at: datetime) -> float:
    days_ago = max((_utcnow() - rated_at).days, 0)
    return math.exp(-math.log(2) * days_ago / RECENCY_HALF_LIFE_DAYS)


def _to_match_score(score: float, min_s: float, max_s: float) -> int:
    """Map raw score into the [55, 99] range Netflix uses for match %."""
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
    }


# ── TMDB feature cache ─────────────────────────────────────────────────────────

async def _fetch_and_cache_features(
    db: Session, movie_id: int, semaphore: asyncio.Semaphore
) -> Optional[dict]:
    cached = db.query(MovieFeatureCache).filter(
        MovieFeatureCache.movie_id == movie_id
    ).first()

    cutoff = _utcnow() - timedelta(days=FEATURE_CACHE_TTL_DAYS)
    if cached and cached.cached_at > cutoff:
        return _row_to_features(cached)

    async with semaphore:
        try:
            raw = await tmdb.fetch_movie_features_raw(movie_id)
        except Exception as exc:
            logger.warning("TMDB features failed for %s: %s", movie_id, exc)
            return None

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
    vote_avg = raw.get("vote_average")
    vote_count = raw.get("vote_count")

    if cached:
        cached.title = raw.get("title")
        cached.genres_json = json.dumps(genres)
        cached.cast_json = json.dumps(cast_ids)
        cached.director = director
        cached.release_year = release_year
        cached.tmdb_vote_avg = vote_avg
        cached.tmdb_vote_count = vote_count
        cached.poster_path = raw.get("poster_path")
        cached.overview = raw.get("overview")
        cached.cached_at = _utcnow()
    else:
        cached = MovieFeatureCache(
            movie_id=movie_id,
            title=raw.get("title"),
            genres_json=json.dumps(genres),
            cast_json=json.dumps(cast_ids),
            director=director,
            release_year=release_year,
            tmdb_vote_avg=vote_avg,
            tmdb_vote_count=vote_count,
            poster_path=raw.get("poster_path"),
            overview=raw.get("overview"),
            cached_at=_utcnow(),
        )
        db.add(cached)

    try:
        db.commit()
    except Exception:
        db.rollback()

    return {
        "movie_id": movie_id,
        "title": raw.get("title"),
        "genres": genres,
        "cast": cast_ids,
        "director": director,
        "release_year": release_year,
        "tmdb_vote_avg": vote_avg,
        "tmdb_vote_count": vote_count,
        "poster_path": raw.get("poster_path"),
        "overview": raw.get("overview"),
    }


# ── user taste profile ─────────────────────────────────────────────────────────

def _build_user_taste_profile(db: Session, user_id: int) -> dict:
    profile: dict[str, dict] = {
        "genres": {}, "cast": {}, "directors": {}, "decades": {},
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
            profile["genres"][genre] = (
                profile["genres"].get(genre, 0.0) + weight / genre_count
            )

        cast_ids = json.loads(row.cast_json or "[]")
        for pos, actor_id in enumerate(cast_ids):
            pw = CAST_POSITION_WEIGHTS[min(pos, len(CAST_POSITION_WEIGHTS) - 1)]
            profile["cast"][actor_id] = (
                profile["cast"].get(actor_id, 0.0) + weight * pw
            )

        if row.director:
            profile["directors"][row.director] = (
                profile["directors"].get(row.director, 0.0) + weight
            )

        if row.release_year:
            decade = str((row.release_year // 10) * 10)
            profile["decades"][decade] = profile["decades"].get(decade, 0.0) + weight

    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    if pref:
        for raw_genre in json.loads(pref.favorite_genres or "[]"):
            genre = _normalize_genre(raw_genre)
            profile["genres"][genre] = (
                profile["genres"].get(genre, 0.0) + BOOST_ONBOARDING_GENRE
            )
        for decade in json.loads(pref.preferred_decades or "[]"):
            decade_str = str(decade)
            profile["decades"][decade_str] = (
                profile["decades"].get(decade_str, 0.0) + BOOST_ONBOARDING_GENRE
            )

    fav_people = db.query(FavoritePerson).filter(
        FavoritePerson.user_id == user_id
    ).all()
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

    vote_avg = features.get("tmdb_vote_avg") or 0.0
    score += (vote_avg / 10.0) * W_QUALITY

    return score


def _score_movie_cold_start(profile: dict, features: dict) -> float:
    score = 0.0
    genres = features.get("genres") or []
    genre_count = max(len(genres), 1)
    for genre in genres:
        score += (profile["genres"].get(genre, 0.0) / genre_count) * W_GENRE
    vote_avg = features.get("tmdb_vote_avg") or 0.0
    score += (vote_avg / 10.0) * W_QUALITY * 3
    return score


def _item_similarity(source: dict, candidate: dict) -> float:
    """Jaccard-based item-to-item similarity for 'Because you watched X' row."""
    score = 0.0

    s_genres = set(source.get("genres") or [])
    c_genres = set(candidate.get("genres") or [])
    union = s_genres | c_genres
    if union:
        score += (len(s_genres & c_genres) / len(union)) * 4.0

    if source.get("director") and source["director"] == candidate.get("director"):
        score += 3.0

    s_cast = set(list(source.get("cast") or [])[:5])
    c_cast = set(list(candidate.get("cast") or [])[:5])
    cast_union = s_cast | c_cast
    if cast_union:
        score += (len(s_cast & c_cast) / len(cast_union)) * 2.0

    s_yr = source.get("release_year")
    c_yr = candidate.get("release_year")
    if s_yr and c_yr and (s_yr // 10) == (c_yr // 10):
        score += 0.5

    vote_avg = candidate.get("tmdb_vote_avg") or 0.0
    score += (vote_avg / 10.0) * 1.5

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


def _make_item(
    features: dict,
    match_score: int,
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


# ── candidate pool ─────────────────────────────────────────────────────────────

async def _build_candidate_pool(
    db: Session,
    user_id: int,
    rated_ids: set[int],
    semaphore: asyncio.Semaphore,
    top_genre_ids: list[int],
) -> dict[int, dict]:
    """Fetch ~300 diverse candidates and return {movie_id: features}."""

    async def _page(endpoint: str, page: int, genre_ids: list[int] = [],
                    anchor_id: Optional[int] = None) -> list[dict]:
        try:
            if endpoint == "top_rated":
                data = await tmdb.fetch_top_rated_movies(page)
            elif endpoint == "popular":
                data = await tmdb.fetch_popular_movies(page)
            elif endpoint == "similar" and anchor_id:
                data = await tmdb.fetch_similar_movies(anchor_id, page)
            else:
                data = await tmdb.fetch_discover_movies(genre_ids, page)
            return data.get("results") or []
        except Exception:
            return []

    # Expanded base pool: top_rated p1–8 + popular p1–4 ≈ 400 raw candidates
    base = [
        _page("top_rated", 1), _page("top_rated", 2),
        _page("top_rated", 3), _page("top_rated", 4),
        _page("top_rated", 5), _page("top_rated", 6),
        _page("top_rated", 7), _page("top_rated", 8),
        _page("popular", 1),   _page("popular", 2),
        _page("popular", 3),   _page("popular", 4),
    ]

    # Genre-specific discovery: top 5 genres (not 3) for better niche coverage
    discover = (
        [_page("discover", 1, top_genre_ids), _page("discover", 2, top_genre_ids),
         _page("discover", 3, top_genre_ids), _page("discover", 4, top_genre_ids)]
        if top_genre_ids else
        [_page("top_rated", 9), _page("top_rated", 10), _page("popular", 5)]
    )

    # Row 2 anchor enrichment: fetch /similar for the most-liked recent film
    anchor_id: Optional[int] = None
    recent_liked = (
        db.query(RatedMovie.movie_id)
        .filter(RatedMovie.user_id == user_id, RatedMovie.rating >= 4.0)
        .order_by(RatedMovie.rated_at.desc())
        .first()
    )
    if recent_liked:
        anchor_id = recent_liked.movie_id
    similar_pages = (
        [_page("similar", 1, anchor_id=anchor_id), _page("similar", 2, anchor_id=anchor_id)]
        if anchor_id else []
    )

    all_pages = await asyncio.gather(*base, *discover, *similar_pages)

    seen: set[int] = set()
    candidates: list[dict] = []
    for page_results in all_pages:
        for movie in page_results:
            mid = movie.get("id")
            if mid and mid not in seen and mid not in rated_ids:
                seen.add(mid)
                candidates.append(movie)

    # Cap at 100 to keep TMDB feature fetches within the timeout budget
    # (100 × 16 concurrent × ≤5 s each ≈ 32 s worst-case, within the 45 s timeout)
    candidates = candidates[:100]

    # Fetch features in parallel; return_exceptions so one failure doesn't abort the rest
    raw_feats = await asyncio.gather(
        *[_fetch_and_cache_features(db, c["id"], semaphore) for c in candidates],
        return_exceptions=True,
    )
    feats = [f if isinstance(f, dict) else None for f in raw_feats]

    # Also cache features for rated movies (cap at 20 to avoid runaway TMDB calls)
    uncached_rated = [
        mid for mid in list(rated_ids)[:20]
        if not db.query(MovieFeatureCache)
        .filter(MovieFeatureCache.movie_id == mid).first()
    ]
    if uncached_rated:
        await asyncio.gather(
            *[_fetch_and_cache_features(db, mid, semaphore) for mid in uncached_rated],
            return_exceptions=True,
        )

    candidate_features: dict[int, dict] = {}
    for cand, feat in zip(candidates, feats):
        if (feat
                and (feat.get("tmdb_vote_avg") or 0) >= MIN_VOTE_AVG
                and (feat.get("tmdb_vote_count") or 0) >= MIN_VOTE_COUNT):
            candidate_features[cand["id"]] = feat

    # Fallback: if TMDB feature fetches all failed (cold cache + slow API), build
    # lightweight features from the raw page data so we still return something useful.
    if not candidate_features:
        logger.warning("No feature-cached candidates — falling back to raw page data")
        for movie in candidates:
            mid = movie.get("id")
            va  = movie.get("vote_average") or 0
            vc  = movie.get("vote_count") or 0
            if mid and va >= MIN_VOTE_AVG and vc >= MIN_VOTE_COUNT:
                candidate_features[mid] = {
                    "movie_id": mid,
                    "title": movie.get("title"),
                    "genres": [],
                    "cast": [],
                    "director": None,
                    "release_year": int((movie.get("release_date") or "0")[:4] or 0) or None,
                    "tmdb_vote_avg": va,
                    "tmdb_vote_count": vc,
                    "poster_path": movie.get("poster_path"),
                    "overview": movie.get("overview"),
                }

    return candidate_features


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
    """Item-item similarity row. Returns (movies, source_title)."""
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
    """Best films in the user's #1 genre."""
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
    """Most-rated films on Moviz in the last 30 days (with high avg).

    Fallback: if the platform is young and trending data is sparse (<5 films),
    supplement with all-time most-rated on Moviz so the row is never empty.
    """
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

    # Fallback 1: relax min_count to 1 within the same 30-day window
    if len(rows) < 5:
        rows = _query_trending(1, cutoff)

    # Fallback 2: all-time most-rated on the platform (no date cutoff)
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
    seen_in_trending: set[int] = set()
    for row in rows:
        if row.movie_id in excluded or row.movie_id in seen_in_trending:
            continue
        seen_in_trending.add(row.movie_id)
        cached = db.query(MovieFeatureCache).filter(
            MovieFeatureCache.movie_id == row.movie_id
        ).first()
        movies.append(
            {
                "movie_id": row.movie_id,
                "title": row.title,
                "poster_path": row.poster_path,
                "release_date": row.release_date,
                "vote_average": cached.tmdb_vote_avg if cached else None,
                "genres": json.loads(cached.genres_json) if cached else [],
                "overview": cached.overview if cached else None,
                "match_score": None,
                "reason_tags": ["Em alta no Moviz"],
            }
        )
        if len(movies) >= limit:
            break

    return movies


def _row_acclaimed(
    candidate_features: dict[int, dict],
    excluded: set[int],
    limit: int = 20,
) -> list[dict]:
    """Top films by TMDB vote_average, min 1000 votes, regardless of personal taste."""
    eligible = [
        feat for mid, feat in candidate_features.items()
        if mid not in excluded
        and (feat.get("tmdb_vote_count") or 0) >= MIN_VOTE_COUNT
    ]
    # Bayesian-style ranking: weight avg by vote confidence to avoid tiny-sample outliers
    # bayesian_score = (v * R + m * C) / (v + m)  where m = MIN_VOTE_COUNT, C = mean rating
    C = 7.5  # prior mean (conservative estimate)
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
    """Return 5 themed recommendation rows, cached for CACHE_TTL_HOURS."""

    # ── serve from cache if fresh ──────────────────────────────────────────
    cache = db.query(RecommendationCache).filter(
        RecommendationCache.user_id == user_id
    ).first()
    ttl = timedelta(hours=CACHE_TTL_HOURS)
    if cache and (_utcnow() - cache.computed_at) < ttl:
        try:
            return json.loads(cache.rows_json)
        except Exception:
            pass

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
    """Inner async worker — called with a 25-second overall timeout."""
    # ── build fresh rows ───────────────────────────────────────────────────
    rated_ids: set[int] = {
        r.movie_id
        for r in db.query(RatedMovie.movie_id)
        .filter(RatedMovie.user_id == user_id).all()
    }

    # Quick genre IDs from already-cached rated movies for the discover call
    cached_genres_raw = (
        db.query(MovieFeatureCache.genres_json)
        .join(RatedMovie, RatedMovie.movie_id == MovieFeatureCache.movie_id)
        .filter(RatedMovie.user_id == user_id, RatedMovie.rating >= 3.5)
        .all()
    )
    genre_tally: dict[str, float] = {}
    for (gjson,) in cached_genres_raw:
        for g in json.loads(gjson or "[]"):
            genre_tally[g] = genre_tally.get(g, 0) + 1
    top_genre_ids = [
        GENRE_NAME_TO_TMDB_ID[g]
        for g, _ in sorted(genre_tally.items(), key=lambda x: -x[1])[:5]  # top 5 genres
        if g in GENRE_NAME_TO_TMDB_ID
    ]

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TMDB)
    candidate_features = await _build_candidate_pool(
        db, user_id, rated_ids, semaphore, top_genre_ids
    )

    profile = _build_user_taste_profile(db, user_id)
    cold_start = len(rated_ids) < COLD_START_THRESHOLD

    # Rows 2 and 3 both exclude Row 1 but not each other — they serve different
    # discovery angles (item-similarity vs. genre-specific), so overlap between
    # them is acceptable. Rows 4 and 5 are global and exclude all previous rows.
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

    # ── persist cache ──────────────────────────────────────────────────────
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


# ── legacy single-list endpoint (kept for backwards compat) ────────────────────

async def get_recommendations(db: Session, user_id: int, limit: int = 20) -> list[dict]:
    rows = await get_recommendation_rows(db, user_id)
    for_you = next((r["movies"] for r in rows if r["row_id"] == "for_you"), [])
    return for_you[:limit]
