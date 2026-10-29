"""CineBot — chatbot cinefilo com recomendacoes personalizadas via Anthropic Claude API."""

import asyncio
import json
import os

import anthropic
from sqlalchemy.orm import Session

from .models import FavoriteMovie, FavoritePerson, MovieFeatureCache, RatedMovie, User, UserPreference
from .recommender import (
    GENRE_NAME_TO_TMDB_ID,
    _build_user_taste_profile,
)
from . import tmdb

# Model can be overridden via env var. Haiku 4.5 is fast and cheap for chat;
# swap to claude-sonnet-5 for higher quality if latency is acceptable.
CLAUDE_MODEL = os.getenv("CLAUDE_CHAT_MODEL", "claude-haiku-4-5-20251001")

# Limites de custo por mensagem.
MAX_HISTORY_MESSAGES = 12   # últimas N mensagens reenviadas como contexto
MAX_HISTORY_CHARS = 1500    # corte por mensagem do histórico
MAX_TOOL_ROUNDS = 3         # chamadas a find_movies antes de forçar a resposta
MAX_KEYWORDS = 3            # temas resolvidos por busca (cada um custa 1 chamada ao TMDB)
MIN_VOTE_COUNT = 50         # abaixo disso o filme é obscuro demais para recomendar
FINAL_ROUND_NUDGE = (
    "Responda agora ao usuario com os melhores filmes ja encontrados, sem novas buscas."
)

# Peso de cada sinal no ranking dos filmes sugeridos ao modelo.
W_QUALITY, W_RELEVANCE, W_AFFINITY = 0.40, 0.30, 0.30
LOVED_RATING = 4.5          # nota que indica "amou"
DISLIKED_RATING = 2.0       # nota que indica "não curtiu"

LANGUAGE_NAMES = {
    "en": "inglês", "pt": "português", "es": "espanhol", "fr": "francês", "it": "italiano",
    "de": "alemão", "ja": "japonês", "ko": "coreano", "zh": "chinês", "hi": "hindi",
    "sv": "sueco", "da": "dinamarquês", "ru": "russo", "fa": "persa",
}


def is_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))

TMDB_ID_TO_GENRE: dict[int, str] = {v: k for k, v in GENRE_NAME_TO_TMDB_ID.items()}

SYSTEM_PROMPT_TEMPLATE = """Você é o CineBot, o cinéfilo de plantão do Moviz: um amigo que vive de cinema e já conhece {name} pelo que ele(a) assiste e avalia no app.

Como você fala:
- Português brasileiro natural, papo de amigo que ama cinema.
- Vocabulário de cinéfilo quando ele acrescenta algo (direção, fotografia, montagem, ritmo, atmosfera, subtexto, plano-sequência), nunca para exibir erudição. Se a pessoa fala de forma casual, fale simples também.
- Opinião própria e honesta: defende filmes subestimados, questiona hype, mas respeita o gosto do outro.
- Conciso: 1 a 3 parágrafos curtos, sem listas enormes nem enrolação.

O que você faz:
1. Conversa e tira dúvidas sobre cinema — história, movimentos, diretores, atores, gêneros, técnicas, bastidores, análise e interpretação de filmes, comparações. Responda direto, com conhecimento real e sem usar a ferramenta; aqui você pode citar qualquer filme. Se não tiver certeza de um fato, diga.
2. Recomenda o que assistir — inclui "qual X pra começar", "filmes do diretor Y", "algo parecido com Z". Nesse caso:
   - Chame find_movies antes de recomendar. Monte a busca juntando o pedido com o perfil abaixo (gêneros, temas, diretores e décadas que a pessoa curte) e evite o que ela costuma avaliar mal. Se o pedido for vago ("algo pra hoje"), o perfil é o guia principal.
   - Diretor ou ator → use person. Tema ou clima → keywords (+ genres). Filme específico → query.
   - Recomende só filmes que vieram da ferramenta, no máximo 4, com o título exatamente como veio, ano e diretor quando houver.
   - Para cada um, uma frase sobre por que combina com ESSA pessoa, ligando a filmes que ela amou, diretores ou gêneros favoritos (o campo combina_com ajuda). Ex.: "se Ilha do Medo te pegou, este aqui…"
   - Se o pedido vai contra o gosto dela (ex.: pede comédia mas avalia mal comédias), respeite o pedido: recomende já opções que conciliem os dois lados (ex.: comédia de humor ácido ou negro) e, se quiser, pergunte depois. Não responda só com perguntas.
3. Cite o histórico da pessoa só quando a ligação for clara e natural; nada de "olhando pro seu perfil" nem conexões forçadas.
4. Nunca fale de ferramentas, buscas, banco de dados ou limitações técnicas.

Perfil de {name}:
{user_context}"""

_TOOLS: list[dict] = [
    {
        "name": "find_movies",
        "description": (
            "Busca filmes para recomendar, já ordenados pela afinidade com o perfil do usuario. "
            "Use sempre que o usuario pedir o que assistir."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Temas em ingles no estilo das keywords do TMDB (ate 3) — ex: "
                        "['psychological thriller'], ['heist'], ['time travel', 'mind-bending']"
                    ),
                },
                "person": {
                    "type": "string",
                    "description": "Nome de um diretor(a) ou ator/atriz (ex: 'Akira Kurosawa').",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Titulo de um filme especifico (ex: 'Parasite'). "
                        "Deixe vazio em pedidos por tema, clima, genero ou pessoa."
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
            "required": [],
        },
    }
]

# Nome do tema -> id de keyword no TMDB (estável; cache do processo).
_keyword_id_cache: dict[str, int | None] = {}
# Nome da pessoa -> id no TMDB.
_person_id_cache: dict[str, int | None] = {}


async def _resolve_keyword_ids(keywords: list[str]) -> list[int]:
    import httpx as _httpx
    ids: list[int] = []
    async with _httpx.AsyncClient(timeout=10) as client:
        for raw in keywords[:MAX_KEYWORDS]:
            name = raw.strip().lower()
            if not name:
                continue
            if name not in _keyword_id_cache:
                resp = await client.get(
                    f"{tmdb.BASE_URL}/search/keyword",
                    headers=tmdb.build_tmdb_headers(),
                    params={"api_key": tmdb.TMDB_API_KEY, "query": name},
                )
                resp.raise_for_status()
                results = resp.json().get("results") or []
                _keyword_id_cache[name] = results[0]["id"] if results else None
            if _keyword_id_cache[name]:
                ids.append(_keyword_id_cache[name])
    return ids


async def _resolve_person_id(name: str) -> int | None:
    key = name.strip().lower()
    if not key:
        return None
    if key not in _person_id_cache:
        results = (await tmdb.search_people(name)).get("results") or []
        # A busca do TMDB já ordena por popularidade: o primeiro é o mais provável.
        _person_id_cache[key] = results[0]["id"] if results else None
    return _person_id_cache[key]


async def _search_title_for_chat(query: str) -> dict:
    """Busca por título em pt-BR e em inglês: o TMDB só casa o título traduzido do idioma
    pedido (ex.: 'Parasite' em pt-BR não acha o filme de 2019, que lá é 'Parasita')."""
    import httpx as _httpx
    async with _httpx.AsyncClient() as client:
        resp = await client.get(
            f"{tmdb.BASE_URL}/search/movie",
            headers=tmdb.build_tmdb_headers(),
            params={"api_key": tmdb.TMDB_API_KEY, "query": query, "language": "en-US"},
        )
        resp.raise_for_status()
        english = resp.json().get("results") or []
    localized = (await tmdb.search_movies(query)).get("results") or []
    pt_titles = {m["id"]: m.get("title") for m in localized}
    merged = {m["id"]: m for m in localized}
    for m in english:
        merged.setdefault(m["id"], {**m, "title": pt_titles.get(m["id"]) or m.get("title")})
    # Mais conhecidos primeiro: a busca do TMDB não ordena por relevância cinéfila.
    return {"results": sorted(merged.values(), key=lambda m: -(m.get("vote_count") or 0))}


async def _discover_for_chat(
    genre_ids: list[int],
    min_year: int,
    keyword_ids: list[int] | None = None,
    person_id: int | None = None,
) -> dict:
    """Discover para o chat: filmes bem avaliados e conhecidos, títulos em pt-BR."""
    import httpx as _httpx
    params: dict = {
        "api_key": tmdb.TMDB_API_KEY,
        "language": "pt-BR",
        "sort_by": "popularity.desc",
        "vote_count.gte": 300,
        "vote_average.gte": 6.5,
    }
    if person_id:
        # Filmografia: os mais vistos/votados primeiro, inclusive clássicos.
        params.update({"with_people": person_id, "sort_by": "vote_count.desc", "vote_count.gte": 100})
    if min_year:
        params["primary_release_date.gte"] = f"{min_year}-01-01"
    if genre_ids:
        params["with_genres"] = ",".join(str(g) for g in genre_ids)
    if keyword_ids:
        params["with_keywords"] = "|".join(str(k) for k in keyword_ids)  # OR entre temas
    async with _httpx.AsyncClient() as client:
        resp = await client.get(
            f"{tmdb.BASE_URL}/discover/movie",
            headers=tmdb.build_tmdb_headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


def _top(scores: dict, n: int, positive: bool = True) -> list:
    items = [(k, v) for k, v in scores.items() if (v > 0 if positive else v < 0)]
    items.sort(key=lambda kv: -kv[1] if positive else kv[1])
    return [k for k, _ in items[:n]]


def _build_user_context(db: Session, user_id: int, profile: dict) -> str:
    """Resumo do gosto do usuário para o prompt: avaliações + onboarding + favoritos."""
    ratings = db.query(RatedMovie).filter(RatedMovie.user_id == user_id)
    rated_count = ratings.count()
    lines = [f"Filmes avaliados: {rated_count}"]

    if rated_count:
        avg = sum(r.rating for r in ratings.all()) / rated_count
        style = " (exigente)" if avg < 3.2 else " (generoso nas notas)" if avg > 4.2 else ""
        lines.append(f"Nota média que dá: {avg:.1f}/5{style}")

    loved = (
        ratings.filter(RatedMovie.rating >= LOVED_RATING)
        .order_by(RatedMovie.rated_at.desc()).limit(6).all()
    )
    if loved:
        lines.append("Amou: " + ", ".join(f"{r.title} ({r.rating:g}★)" for r in loved))
    disliked = (
        ratings.filter(RatedMovie.rating <= DISLIKED_RATING)
        .order_by(RatedMovie.rated_at.desc()).limit(4).all()
    )
    if disliked:
        lines.append("Não curtiu: " + ", ".join(f"{r.title} ({r.rating:g}★)" for r in disliked))

    favorites = [f.title for f in db.query(FavoriteMovie).filter(FavoriteMovie.user_id == user_id).limit(5)]
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    if pref:
        try:
            onboarding_fav = json.loads(pref.favorite_movie_json or "null")
        except ValueError:
            onboarding_fav = None
        if isinstance(onboarding_fav, dict) and onboarding_fav.get("title"):
            favorites.insert(0, onboarding_fav["title"])
    if favorites:
        lines.append("Filmes favoritos: " + ", ".join(dict.fromkeys(favorites)))

    if genres := _top(profile["genres"], 5):
        lines.append("Gêneros que curte: " + ", ".join(genres))
    if bad_genres := _top(profile["genres"], 2, positive=False):
        lines.append("Gêneros que costuma avaliar mal: " + ", ".join(bad_genres))
    if directors := _top(profile["directors"], 4):
        lines.append("Diretores favoritos: " + ", ".join(directors))
    people = db.query(FavoritePerson).filter(FavoritePerson.user_id == user_id).limit(6).all()
    if people:
        lines.append("Pessoas favoritas: " + ", ".join(p.name for p in people))
    if decades := _top(profile["decades"], 2):
        lines.append("Décadas preferidas: " + ", ".join(f"anos {d}" for d in decades))
    if languages := _top(profile["languages"], 3):
        lines.append("Idiomas que mais assiste: " + ", ".join(LANGUAGE_NAMES.get(l, l) for l in languages))

    if rated_count == 0 and not favorites:
        lines.append("Usuário novo: pouco histórico ainda — descubra o gosto dele na conversa.")
    return "\n".join(lines)


def _affinity(profile: dict, feat: dict, language: str | None) -> tuple[float, list[str]]:
    """Afinidade do filme com o perfil, em [-1, 1], e os motivos legíveis para o modelo."""
    score = 0.0
    reasons: list[str] = []

    genre_scores = profile["genres"]
    max_abs = max((abs(v) for v in genre_scores.values()), default=0.0)
    genres = feat.get("genres") or []
    if max_abs and genres:
        normalized = {g: genre_scores.get(g, 0.0) / max_abs for g in genres}
        score += 0.5 * sum(normalized.values()) / len(genres)
        best = max(normalized, key=normalized.get)
        worst = min(normalized, key=normalized.get)
        if normalized[best] > 0.3:
            reasons.append(f"gênero que curte: {best}")
        if normalized[worst] < -0.3:
            reasons.append(f"atenção, costuma avaliar mal: {worst}")

    director = feat.get("director")
    if director and profile["directors"].get(director, 0.0) > 0:
        score += 0.3
        reasons.append(f"diretor favorito: {director}")

    year = feat.get("release_year")
    if year and str((year // 10) * 10) in _top(profile["decades"], 2):
        score += 0.1
        reasons.append(f"década que prefere: anos {(year // 10) * 10}")

    if language and language in _top(profile["languages"], 2) and language != "en":
        score += 0.1
        reasons.append(f"cinema {LANGUAGE_NAMES.get(language, language)}, que costuma ver")

    return max(-1.0, min(1.0, score)), reasons


async def _find_movies(
    db: Session,
    user_id: int,
    query: str,
    genres: list[str],
    min_year: int,
    limit: int,
    keywords: list[str] | None = None,
    person: str = "",
    profile: dict | None = None,
) -> list[dict]:
    profile = profile or _build_user_taste_profile(db, user_id)
    rated_ids = {
        r.movie_id
        for r in db.query(RatedMovie.movie_id).filter(RatedMovie.user_id == user_id).all()
    }

    genre_ids = [GENRE_NAME_TO_TMDB_ID[g] for g in genres if g in GENRE_NAME_TO_TMDB_ID]
    keyword_ids = await _resolve_keyword_ids(keywords or [])
    person_id = await _resolve_person_id(person) if person else None

    # Pessoa e temas primeiro (mais específicos), depois título, depois só gênero.
    tasks: list = []
    if person_id:
        tasks.append(_discover_for_chat(genre_ids, min_year, keyword_ids, person_id=person_id))
    elif keyword_ids:
        tasks.append(_discover_for_chat(genre_ids, min_year, keyword_ids))
    if query:
        tasks.append(_search_title_for_chat(query))
    if genre_ids and not keyword_ids and not person_id:
        tasks.append(_discover_for_chat(genre_ids, min_year))
    if not tasks:
        tasks.append(_discover_for_chat([], min_year))

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

    scored: list[tuple[float, dict, list[str]]] = []
    for pos, m in candidates:
        vote_avg = m.get("vote_average") or 0.0
        # Poucos votos = filme obscuro ou nota pouco confiável.
        if vote_avg < 6.0 or (m.get("vote_count") or 0) < MIN_VOTE_COUNT:
            continue

        cached = db.query(MovieFeatureCache).filter(
            MovieFeatureCache.movie_id == m["id"]
        ).first()

        if cached:
            feat = {
                "movie_id": cached.movie_id,
                # Título vindo do TMDB nesta busca (pt-BR); o cache pode estar em inglês.
                "title": m.get("title") or cached.title,
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

        # O pedido define o conjunto (relevância); o perfil desempata e reordena (afinidade).
        quality = (feat["tmdb_vote_avg"] - 6.0) / 4.0   # normalize 6–10 → 0–1
        relevance = max(0.0, 1.0 - pos / 20.0)           # position 0→1.0, 19→0.05
        affinity, reasons = _affinity(profile, feat, m.get("original_language"))
        chat_score = W_QUALITY * quality + W_RELEVANCE * relevance + W_AFFINITY * (affinity + 1) / 2

        scored.append((chat_score, feat, reasons))

    scored.sort(key=lambda x: -x[0])

    output: list[dict] = []
    for _, feat, reasons in scored[:limit]:
        output.append({
            "movie_id": feat["movie_id"],
            "title": feat["title"],
            "year": feat.get("release_year"),
            "director": feat.get("director"),
            "genres": feat["genres"][:3],
            "vote_average": feat["tmdb_vote_avg"],
            "overview": (feat.get("overview") or "")[:200],
            "poster_path": feat.get("poster_path"),
            "combina_com": reasons,
        })
    return output


async def chat(
    db: Session,
    user_id: int,
    message: str,
    history: list[dict],
) -> dict:
    # Perfil calculado uma vez por mensagem: vai para o prompt e para o ranking da ferramenta.
    profile = _build_user_taste_profile(db, user_id)
    user = db.get(User, user_id)
    name = user.username if user else "o usuário"
    system = SYSTEM_PROMPT_TEMPLATE.format(
        name=name, user_context=_build_user_context(db, user_id, profile)
    )

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Build messages array — Anthropic doesn't take system in messages[], only in system param.
    # Só as últimas mensagens entram no contexto: o histórico reenviado a cada
    # turno é o que mais pesa no custo por mensagem.
    trimmed = history[-MAX_HISTORY_MESSAGES:]
    while trimmed and trimmed[0]["role"] != "user":
        trimmed = trimmed[1:]
    messages: list[dict] = [
        {"role": m["role"], "content": m["content"][:MAX_HISTORY_CHARS]} for m in trimmed
    ]
    messages.append({"role": "user", "content": message})

    found_movies: list[dict] = []

    # Agentic loop, com teto de rodadas: na última, a ferramenta é desligada
    # para forçar uma resposta final (evita loop caro).
    for round_index in range(MAX_TOOL_ROUNDS + 1):
        last_round = round_index == MAX_TOOL_ROUNDS
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=_TOOLS,
            tool_choice={"type": "none"} if last_round else {"type": "auto"},
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
                keywords=list(args.get("keywords") or []),
                person=args.get("person") or "",
                profile=profile,
            )
            found_movies.extend(movies)
            # O modelo só precisa do essencial para escolher, citar e justificar;
            # poster/id ficam no servidor para montar os cards.
            for_model = [
                {
                    "title": mv["title"],
                    "year": mv["year"],
                    "director": mv["director"],
                    "genres": mv["genres"],
                    "vote_average": mv["vote_average"],
                    "overview": (mv["overview"] or "")[:160],
                    "combina_com": mv["combina_com"],
                }
                for mv in movies
            ]
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(for_model, ensure_ascii=False),
            })

        # Return tool results as a user turn so the model can continue.
        # Antes da rodada final (sem ferramenta), pede a resposta explicitamente —
        # senão o modelo pode só anunciar "vou buscar de novo" e parar.
        if round_index + 1 == MAX_TOOL_ROUNDS:
            tool_results.append({"type": "text", "text": FINAL_ROUND_NUDGE})
        messages.append({"role": "user", "content": tool_results})

    # Junta todos os blocos de texto da resposta final (pode vir em mais de um).
    text = "\n\n".join(
        block.text.strip() for block in response.content
        if getattr(block, "type", None) == "text" and block.text.strip()
    )
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
