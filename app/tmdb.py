import os
from asyncio import to_thread
import httpx
from typing import Any, Dict
from functools import lru_cache
from dotenv import load_dotenv
from simplejustwatchapi import search as justwatch_search

load_dotenv()
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_READ_TOKEN = os.getenv("TMDB_READ_TOKEN")
TMDB_WATCH_REGION = os.getenv("TMDB_WATCH_REGION", "BR")
BASE_URL = "https://api.themoviedb.org/3"


def build_tmdb_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {TMDB_READ_TOKEN}"
    } if TMDB_READ_TOKEN else {}


def map_justwatch_language(country_code: str) -> str:
    language_by_country = {
        "BR": "pt",
        "PT": "pt",
        "US": "en",
        "GB": "en",
        "ES": "es",
        "MX": "es",
        "AR": "es",
        "FR": "fr",
        "DE": "de",
        "IT": "it",
    }
    return language_by_country.get(country_code.upper(), "en")


def normalize_justwatch_offers(entry: Any) -> Dict[str, Any]:
    grouped: Dict[str, list[Dict[str, Any]]] = {
        "flatrate": [],
        "rent": [],
        "buy": [],
    }

    monetization_map = {
        "FLATRATE": "flatrate",
        "ADS": "flatrate",
        "FREE": "flatrate",
        "RENT": "rent",
        "BUY": "buy",
    }

    for offer in getattr(entry, "offers", []) or []:
        target_group = monetization_map.get(getattr(offer, "monetization_type", ""), None)
        if not target_group:
            continue

        grouped[target_group].append(
            {
                "provider_id": getattr(offer.package, "package_id", None),
                "provider_name": getattr(offer.package, "name", None),
                "logo_path": getattr(offer.package, "icon", None),
                "url": getattr(offer, "url", None),
                "presentation_type": getattr(offer, "presentation_type", None),
                "price_string": getattr(offer, "price_string", None),
            }
        )

    return {
        "flatrate": grouped["flatrate"],
        "rent": grouped["rent"],
        "buy": grouped["buy"],
        "link": getattr(entry, "url", None),
    }


def find_justwatch_match(entries: list[Any], movie_id: int, title: str, release_date: str | None) -> Any | None:
    release_year = (release_date or "")[:4]

    for entry in entries:
        if str(getattr(entry, "tmdb_id", "")) == str(movie_id):
            return entry

    normalized_title = title.strip().lower()
    for entry in entries:
        if getattr(entry, "title", "").strip().lower() == normalized_title:
            entry_year = str(getattr(entry, "release_year", "") or "")
            if not release_year or release_year == entry_year:
                return entry

    return entries[0] if entries else None


async def fetch_justwatch_offers(movie_id: int, title: str, release_date: str | None) -> Dict[str, Any]:
    country = TMDB_WATCH_REGION.upper()
    language = map_justwatch_language(country)

    try:
        entries = await to_thread(justwatch_search, title, country, language, 8, True)
    except Exception:
        return {"flatrate": [], "rent": [], "buy": [], "link": None}

    matched_entry = find_justwatch_match(entries, movie_id, title, release_date)
    if not matched_entry:
        return {"flatrate": [], "rent": [], "buy": [], "link": None}

    return normalize_justwatch_offers(matched_entry)


async def fetch_discover_movies(genre_ids: list[int], page: int = 1) -> Dict[str, Any]:
    """Fetch movies via TMDB discover filtered by genre IDs (for personalized candidate pool)."""
    headers = build_tmdb_headers()
    params = {
        "api_key": TMDB_API_KEY,
        "with_genres": ",".join(str(g) for g in genre_ids),
        "sort_by": "vote_average.desc",
        "vote_count.gte": 200,
        "page": page,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/discover/movie", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_movie_features_raw(movie_id: int) -> Dict[str, Any]:
    """Fetch full feature set for the recommender: credits + keywords in one request."""
    headers = build_tmdb_headers()
    params = {
        "api_key": TMDB_API_KEY,
        "append_to_response": "credits,keywords",
        "language": "en-US",
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(f"{BASE_URL}/movie/{movie_id}", headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()

    credits = payload.get("credits") or {}
    cast = credits.get("cast") or []
    crew = credits.get("crew") or []
    director = next(
        (c.get("name") for c in crew if c.get("job") == "Director"),
        None,
    )

    keywords_raw = (payload.get("keywords") or {}).get("keywords") or []
    keyword_ids = [k["id"] for k in keywords_raw if k.get("id")]

    collection = payload.get("belongs_to_collection") or {}

    return {
        "movie_id": payload.get("id"),
        "title": payload.get("title"),
        "overview": payload.get("overview"),
        "vote_average": payload.get("vote_average"),
        "vote_count": payload.get("vote_count"),
        "release_date": payload.get("release_date"),
        "genres": [g.get("name") for g in payload.get("genres", []) if g.get("name")],
        "poster_path": payload.get("poster_path"),
        "cast": [{"id": a.get("id"), "name": a.get("name")} for a in cast[:10]],
        "director": director,
        "keywords": keyword_ids,
        "original_language": payload.get("original_language"),
        "runtime": payload.get("runtime"),
        "popularity": payload.get("popularity"),
        "collection_id": collection.get("id"),
    }


async def fetch_top_rated_movies(page: int = 1) -> Dict[str, Any]:
    headers = build_tmdb_headers()
    params = {"api_key": TMDB_API_KEY, "page": page, "language": "pt-BR"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/movie/top_rated", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_recent_movies(page: int = 1) -> Dict[str, Any]:
    """Fetch genuinely recent movies using discover with a 12-month release date window."""
    from datetime import date, timedelta
    today = date.today()
    one_year_ago = today - timedelta(days=365)
    headers = build_tmdb_headers()
    params = {
        "api_key": TMDB_API_KEY,
        "page": page,
        "language": "pt-BR",
        "sort_by": "release_date.desc",
        "primary_release_date.gte": one_year_ago.isoformat(),
        "primary_release_date.lte": today.isoformat(),
        "vote_count.gte": 10,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/discover/movie", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_genre_list() -> list:
    headers = build_tmdb_headers()
    params = {"api_key": TMDB_API_KEY, "language": "pt-BR"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/genre/movie/list", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json().get("genres", [])


async def fetch_popular_movies(page: int = 1) -> Dict[str, Any]:
    headers = build_tmdb_headers()
    params = {"api_key": TMDB_API_KEY, "page": page}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/movie/popular", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_similar_movies(movie_id: int, page: int = 1) -> Dict[str, Any]:
    """Fetch TMDB similar movies for a given film (used to enrich Row 2 candidate pool)."""
    headers = build_tmdb_headers()
    params = {"api_key": TMDB_API_KEY, "page": page, "language": "en-US"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{BASE_URL}/movie/{movie_id}/similar", headers=headers, params=params
        )
        resp.raise_for_status()
        return resp.json()


async def search_movies(query: str) -> Dict[str, Any]:
    headers = build_tmdb_headers()
    params = {"api_key": TMDB_API_KEY, "query": query, "language": "pt-BR"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/search/movie", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def search_people(query: str) -> Dict[str, Any]:
    headers = build_tmdb_headers()
    params = {"api_key": TMDB_API_KEY, "query": query, "language": "pt-BR"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/search/person", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_movie_details(movie_id: int) -> Dict[str, Any]:
    headers = build_tmdb_headers()
    params = {
        "api_key": TMDB_API_KEY,
        "append_to_response": "credits,watch/providers,videos,similar",
        "language": "pt-BR",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/movie/{movie_id}", headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()

    cast = (payload.get("credits") or {}).get("cast") or []
    videos = (payload.get("videos") or {}).get("results") or []
    similar_movies = (payload.get("similar") or {}).get("results") or []
    trailer = next(
        (
            video for video in videos
            if video.get("site") == "YouTube" and video.get("type") == "Trailer" and video.get("key")
        ),
        None,
    )
    justwatch_offers = await fetch_justwatch_offers(
        movie_id=payload.get("id"),
        title=payload.get("title") or "",
        release_date=payload.get("release_date"),
    )

    return {
        "id": payload.get("id"),
        "title": payload.get("title"),
        "overview": payload.get("overview"),
        "vote_average": payload.get("vote_average"),
        "release_date": payload.get("release_date"),
        "runtime": payload.get("runtime"),
        "genres": [genre.get("name") for genre in payload.get("genres", []) if genre.get("name")],
        "poster_path": payload.get("poster_path"),
        "backdrop_path": payload.get("backdrop_path"),
        "cast": [
            {
                "id": actor.get("id"),
                "name": actor.get("name"),
                "character": actor.get("character"),
            }
            for actor in cast[:8]
        ],
        "watch_region": TMDB_WATCH_REGION,
        "watch_provider_source": "justwatch",
        "watch_providers": justwatch_offers,
        "trailer_url": f"https://www.youtube.com/watch?v={trailer.get('key')}" if trailer else None,
        "similar_movies": [
            {
                "id": movie.get("id"),
                "title": movie.get("title"),
                "poster_path": movie.get("poster_path"),
                "release_date": movie.get("release_date"),
                "genre_ids": movie.get("genre_ids") or [],
            }
            for movie in similar_movies[:5]
            if movie.get("id") and movie.get("title")
        ],
    }


@lru_cache(maxsize=2048)
def fetch_movie_genres_sync(movie_id: int) -> list[str]:
    headers = build_tmdb_headers()
    params = {
        "api_key": TMDB_API_KEY,
        "language": "pt-BR",
    }
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(f"{BASE_URL}/movie/{movie_id}", headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
    return [genre.get("name") for genre in payload.get("genres", []) if genre.get("name")]
