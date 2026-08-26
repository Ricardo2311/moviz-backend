"""CineBot — chatbot cinefilo com recomendacoes personalizadas via Anthropic Claude API."""

import asyncio
import json
import os

import anthropic
from sqlalchemy.orm import Session

from .models import MovieFeatureCache, RatedMovie
from .recommender import (
    GENRE_NAME_TO_TMDB_ID,
    _build_user_taste_profile,
)
from . import tmdb

# Model can be overridden via env var. Haiku 4.5 is fast and cheap for chat;
# swap to claude-sonnet-5 for higher quality if latency is acceptable.
CLAUDE_MODEL = os.getenv("CLAUDE_CHAT_MODEL", "claude-haiku-4-5-20251001")

TMDB_ID_TO_GENRE: dict[int, str] = {v: k for k, v in GENRE_NAME_TO_TMDB_ID.items()}

SYSTEM_PROMPT_TEMPLATE = """Voce e o CineBot — o assistente de cinema do Moviz.
Um cinefilo genuino e nerd com conhecimento enciclopedico: da Nouvelle Vague ao Cinema Novo,
do expressionismo alemao ao giallo italiano, de Bergman a Bong Joon-ho.

Personalidade:
- Apaixonado, direto, com opiniao propria. Defende filmes subestimados, questiona o hype excessivo
- Usa terminologia cinematografica com naturalidade: plano-sequencia, diegese, mise-en-scene
- Conhece trivia, bastidores e contexto historico dos filmes
- Fala portugues brasileiro coloquial mas com vocabulario preciso
- Quando recomenda, explica o POR QUE aquele filme especifico funciona para essa pessoa
- Respostas concisas e diretas — cinefilo fala com precisao, nao fica enrolando

Quando recomendar filmes:
- SEMPRE chame a ferramenta find_movies antes de recomendar qualquer coisa
- SOMENTE recomende filmes que estejam nos resultados retornados pela ferramenta — NUNCA invente ou mencione filmes fora dessa lista
- Se os resultados nao forem ideais, escolha os melhores disponiveis e explique por que funcionam
- Recomende no maximo 4 filmes por resposta — qualidade acima de quantidade
- Para cada filme: use o titulo EXATAMENTE como veio na ferramenta, inclua diretor e ano
- Conecte a recomendacao ao gosto especifico do usuario (generos e diretores que ele ja gosta)

Quando o usuario fizer perguntas sobre cinema (historia, diretores, tecnicas):
- Responda diretamente sem chamar ferramentas

Perfil de gosto do usuario:
{user_context}"""

_TOOLS: list[dict] = [
    {
        "name": "find_movies",
        "description": (
            "Busca filmes relevantes personalizados para recomendar ao usuario. "
            "Use sempre que o usuario pedir filmes, sugestoes ou recomendacoes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Busca textual em ingles — ex: 'action comedy ryan reynolds', "
                        "'psychological thriller mind bending', 'japanese animation melancholy'"
                    ),
                },
                "genres": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Generos em ingles — ex: ['Thriller', 'Horror', 'Drama']",
                },
                "min_year": {
                    "type": "integer",
                    "description": "Ano minimo de lancamento. Use quando o usuario restringir por epoca (ex: 2010, 2000, 1990)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Numero de filmes a retornar (padrao 6, maximo 10)",
                },
            },
            "required": ["query"],
        },
    }
]


async def _discover_for_chat(genre_ids: list[int], min_year: int) -> dict:
    """Discover endpoint tuned for chat: popularity-sorted, recent films only."""
    import httpx as _httpx
    params: dict = {
        "api_key": tmdb.TMDB_API_KEY,
        "sort_by": "popularity.desc",
        "vote_count.gte": 100,
        "vote_average.gte": 6.0,
        "primary_release_date.gte": f"{min_year}-01-01",
    }
    if genre_ids:
        params["with_genres"] = ",".join(str(g) for g in genre_ids)
    async with _httpx.AsyncClient() as client:
        resp = await client.get(
            f"{tmdb.BASE_URL}/discover/movie",
            headers=tmdb.build_tmdb_headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


def _build_user_context(db: Session, user_id: int) -> str:
    profile = _build_user_taste_profile(db, user_id)

    top_genres = sorted(profile["genres"].items(), key=lambda x: -x[1])[:5]
    top_dirs = sorted(profile["directors"].items(), key=lambda x: -x[1])[:3]

    recent = (
        db.query(RatedMovie)
        .filter(RatedMovie.user_id == user_id, RatedMovie.rating >= 4.0)
        .order_by(RatedMovie.rated_at.desc())
        .limit(5)
        .all()
    )
    rated_count = db.query(RatedMovie).filter(RatedMovie.user_id == user_id).count()

    lines = [f"Filmes avaliados: {rated_count}"]
    if top_genres:
        lines.append("Generos favoritos: " + ", ".join(g for g, _ in top_genres))
    if top_dirs:
        lines.append("Diretores preferidos: " + ", ".join(d for d, _ in top_dirs))
    if recent:
        lines.append(
            "Bem avaliados recentemente: "
            + ", ".join(f"{r.title} ({r.rating}*)" for r in recent)
        )
    if rated_count == 0:
        lines.append("Usuario novo — sem historico ainda.")
    return "\n".join(lines)


async def _find_movies(
    db: Session,
    user_id: int,
    query: str,
    genres: list[str],
    min_year: int,
    limit: int,
) -> list[dict]:
    rated_ids = {
        r.movie_id
        for r in db.query(RatedMovie.movie_id).filter(RatedMovie.user_id == user_id).all()
    }

    tasks: list = []
    if query:
        tasks.append(tmdb.search_movies(query))
    genre_ids = [GENRE_NAME_TO_TMDB_ID[g] for g in genres if g in GENRE_NAME_TO_TMDB_ID]
    if genre_ids:
        tasks.append(_discover_for_chat(genre_ids, min_year or 2005))
    if not tasks:
        tasks.append(_discover_for_chat([], min_year or 2005))

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect candidates preserving TMDB search rank (position = relevance signal)
    seen: set[int] = set()
    candidates: list[tuple[int, dict]] = []  # (position, raw_movie)
    position = 0
    for res in raw_results:
        if isinstance(res, Exception):
            continue
        for m in (res.get("results") or []):
            mid = m.get("id")
            if mid and mid not in seen and mid not in rated_ids:
                seen.add(mid)
                candidates.append((position, m))
                position += 1

    scored: list[tuple[float, dict]] = []
    for pos, m in candidates:
        vote_avg = m.get("vote_average") or 0.0
        if vote_avg < 6.0:
            continue

        cached = db.query(MovieFeatureCache).filter(
            MovieFeatureCache.movie_id == m["id"]
        ).first()

        if cached:
            feat = {
                "movie_id": cached.movie_id,
                "title": cached.title,
                "genres": json.loads(cached.genres_json or "[]"),
                "cast": json.loads(cached.cast_json or "[]"),
                "director": cached.director,
                "release_year": cached.release_year,
                "tmdb_vote_avg": cached.tmdb_vote_avg,
                "poster_path": cached.poster_path,
                "overview": cached.overview,
            }
        else:
            gnames = [
                TMDB_ID_TO_GENRE[gid]
                for gid in (m.get("genre_ids") or [])
                if gid in TMDB_ID_TO_GENRE
            ]
            rd = (m.get("release_date") or "")[:4]
            feat = {
                "movie_id": m["id"],
                "title": m.get("title"),
                "genres": gnames,
                "cast": [],
                "director": None,
                "release_year": int(rd) if rd.isdigit() else None,
                "tmdb_vote_avg": vote_avg,
                "poster_path": m.get("poster_path"),
                "overview": m.get("overview"),
            }

        # Skip if doesn't meet year constraint
        year = feat.get("release_year") or 0
        if min_year and year and year < min_year:
            continue

        # Chat scoring: pure quality + search relevance, no profile.
        # Profile is already in the system prompt context — here the query must drive results.
        quality = (feat["tmdb_vote_avg"] - 6.0) / 4.0   # normalize 6–10 → 0–1
        relevance = max(0.0, 1.0 - pos / 20.0)           # position 0→1.0, 19→0.05
        chat_score = 0.55 * quality + 0.45 * relevance

        scored.append((chat_score, feat))

    scored.sort(key=lambda x: -x[0])

    output: list[dict] = []
    for _, feat in scored[:limit]:
        output.append({
            "movie_id": feat["movie_id"],
            "title": feat["title"],
            "year": feat.get("release_year"),
            "director": feat.get("director"),
            "genres": feat["genres"][:3],
            "vote_average": feat["tmdb_vote_avg"],
            "overview": (feat.get("overview") or "")[:200],
            "poster_path": feat.get("poster_path"),
        })
    return output


async def chat(
    db: Session,
    user_id: int,
    message: str,
    history: list[dict],
) -> dict:
    user_context = _build_user_context(db, user_id)
    system = SYSTEM_PROMPT_TEMPLATE.format(user_context=user_context)

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Build messages array — Anthropic doesn't take system in messages[], only in system param.
    messages: list[dict] = []
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": message})

    found_movies: list[dict] = []

    # Agentic loop: keep calling until the model stops requesting tool use.
    while True:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=_TOOLS,
        )

        if response.stop_reason != "tool_use":
            # No more tool calls — final answer is ready.
            break

        # Append the full assistant turn (may include text + tool_use blocks).
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool call and collect results.
        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use" or block.name != "find_movies":
                continue
            args: dict = block.input or {}
            movies = await _find_movies(
                db,
                user_id,
                query=args.get("query", ""),
                genres=list(args.get("genres") or []),
                min_year=int(args.get("min_year") or 0),
                limit=min(int(args.get("limit") or 6), 10),
            )
            found_movies.extend(movies)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(movies, ensure_ascii=False),
            })

        # Return tool results as a user turn so the model can continue.
        messages.append({"role": "user", "content": tool_results})

    # Extract text from the final assistant response.
    text = ""
    for block in response.content:
        if hasattr(block, "text") and block.text:
            text = block.text
            break
    text = text or "Desculpe, nao consegui processar seu pedido."

    # Only show cards for movies the model actually mentioned in its response.
    text_lower = text.lower()
    seen_ids: set[int] = set()
    unique_movies: list[dict] = []
    for m in found_movies:
        if m["movie_id"] in seen_ids:
            continue
        title = (m.get("title") or "").lower()
        if title and title in text_lower:
            seen_ids.add(m["movie_id"])
            unique_movies.append(m)

    return {"message": text, "movies": unique_movies[:8]}
