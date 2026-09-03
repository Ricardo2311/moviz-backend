import os
import random
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-*naive* datetime.

    SQLite stores datetimes without timezone info (naive UTC).  Using the
    deprecated ``datetime.utcnow()`` was the old way to get this; the modern
    equivalent is ``datetime.now(timezone.utc).replace(tzinfo=None)``, which
    silences the Python 3.12 deprecation warning while remaining compatible
    with existing SQLite rows.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import auth, models, schemas, storage, tmdb

# ── FCM (Firebase Cloud Messaging) ──────────────────────────────────────────
_FCM_ENABLED = False
_fcm_messaging = None

try:
    import firebase_admin
    from firebase_admin import credentials as _fb_creds, messaging as _fb_messaging  # noqa: F401
    _cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "")
    if _cred_path and os.path.exists(_cred_path):
        if not firebase_admin._apps:
            firebase_admin.initialize_app(_fb_creds.Certificate(_cred_path))
        _fcm_messaging = _fb_messaging
        _FCM_ENABLED = True
except Exception:
    pass

DEFAULT_PROFILE_IMAGE = "https://ui-avatars.com/api/?background=b34827&color=fff&name=Moviz"
POST_TEXT_LIMIT = 420
HASHTAG_PATTERN = re.compile(r"(?<!\w)#([A-Za-z0-9_]{1,40})")
MENTION_PATTERN = re.compile(r"(?<!\w)@([A-Za-z0-9_]{1,40})")
DEFAULT_WATCHLIST_TITLE = "Filmes para assistir depois"
DEMO_RATED_MOVIES = [
    {
        "movie_id": 1265609,
        "title": "War Machine",
        "poster_path": "/rFhKkXhk7ClU03jQ5rHIApJDwev.jpg",
        "release_date": "2026-02-12",
        "rating": 5.0,
    },
    {
        "movie_id": 1087192,
        "title": "How to Train Your Dragon",
        "poster_path": "/q5pXRYTycaeW6dEgsCrd4mYPmxM.jpg",
        "release_date": "2025-06-06",
        "rating": 4.5,
    },
    {
        "movie_id": 950387,
        "title": "A Minecraft Movie",
        "poster_path": "/yFHHfHcUgGAxziP1C3lLt0q2T4s.jpg",
        "release_date": "2025-03-31",
        "rating": 4.0,
    },
    {
        "movie_id": 552524,
        "title": "Lilo & Stitch",
        "poster_path": "/tUae3mefrDVTgm5mRzqWnZK6fOP.jpg",
        "release_date": "2025-05-17",
        "rating": 5.0,
    },
    {
        "movie_id": 1061474,
        "title": "Superman",
        "poster_path": "/ombsmhYUqR4qqOLOxAyr5V8hbyv.jpg",
        "release_date": "2025-07-09",
        "rating": 4.5,
    },
    {
        "movie_id": 1234821,
        "title": "Jurassic World Rebirth",
        "poster_path": "/1RICxzeoNCAO5NpcRMIgg1XT6fm.jpg",
        "release_date": "2025-07-01",
        "rating": 4.0,
    },
]


def normalize_movie_identity(title: str | None, release_date: str | None) -> tuple[str, str]:
    normalized_title = re.sub(r"\s+", " ", (title or "").strip().lower())
    normalized_release_date = (release_date or "").strip()
    return normalized_title, normalized_release_date


def normalize_movie_visual_identity(
    poster_path: str | None, release_date: str | None
) -> tuple[str, str]:
    normalized_poster_path = (poster_path or "").strip()
    normalized_release_date = (release_date or "").strip()
    return normalized_poster_path, normalized_release_date


def normalize_rating_to_half_step(value: float | int | None) -> float:
    rating = float(value or 0)
    rating = max(0.5, min(5.0, rating))
    return round(rating * 2) / 2


def get_default_notification_preferences() -> dict[str, bool]:
    return dict(DEFAULT_NOTIFICATION_PREFERENCES)


def merge_notification_preferences(raw_value: str | dict | None) -> dict[str, bool]:
    merged = get_default_notification_preferences()
    parsed = raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value or "{}")
        except Exception:
            parsed = {}
    if isinstance(parsed, dict):
        for key in merged:
            if key in parsed:
                merged[key] = bool(parsed[key])
    return merged


def serialize_notification_preferences(preferences: dict | None) -> str:
    return json.dumps(merge_notification_preferences(preferences))


def score_movie_list_item(item: models.MovieListItem) -> tuple[int, int, int]:
    return (
        1 if (item.movie_id or 0) > 1000 else 0,
        1 if item.poster_path else 0,
        item.movie_id or 0,
    )
# Demo account password: set DEMO_ACCOUNT_PASSWORD in env for a stable value
# across restarts (useful in CI). If unset, a random token is generated at
# startup — demo accounts will exist but won't be loginable without the token.
# This intentionally prevents the public "secret123" default from reaching prod.
_DEMO_ACCOUNT_PASSWORD: str = os.getenv("DEMO_ACCOUNT_PASSWORD") or secrets.token_urlsafe(32)

SOCIAL_DEMO_USERS = [
    {
        "username": "screenqueen",
        "email": "screenqueen@moviz.demo",
        "password": _DEMO_ACCOUNT_PASSWORD,
        "is_verified_critic": True,
    },
    {
        "username": "framehunter",
        "email": "framehunter@moviz.demo",
        "password": _DEMO_ACCOUNT_PASSWORD,
        "is_verified_critic": True,
    },
    {
        "username": "plot_twister",
        "email": "plot_twister@moviz.demo",
        "password": _DEMO_ACCOUNT_PASSWORD,
        "is_verified_critic": False,
    },
]
SOCIAL_DEMO_POSTS = [
    # ── screenqueen ──────────────────────────────────────────────────────────
    {
        "username": "screenqueen",
        "post_type": "review",
        "text_content": "Esse filme me pegou pela fotografia e por uma tensao que cresce sem apelar para excesso. Nolan em modo absoluto.",
        "sentiment": "impactado",
        "tags": ["cinematografia", "tenso", "must-watch"],
        "movie_id": 157336,
        "movie_title": "Interstellar",
        "movie_poster_path": "/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg",
        "movie_release_date": "2014-11-05",
        "review_headline": "Uma experiencia gigante em escala e emocao",
        "rating": 5.0,
        "comments": ["Essa trilha sonora nunca falha.", "Vontade de rever no cinema."],
    },
    {
        "username": "screenqueen",
        "post_type": "commentary",
        "text_content": "Kubrick transformou o horror em filosofia. Cada enquadramento e uma armadilha. Faz 40 anos e continua incomodo do jeito certo.",
        "sentiment": "perturbada",
        "tags": ["terror", "kubrick", "classico"],
        "movie_id": 694,
        "movie_title": "The Shining",
        "movie_poster_path": "/b6ko0IKC8MdYBBPkkA1aBPLe2yz.jpg",
        "movie_release_date": "1980-05-23",
        "comments": ["O corredor das gêmeas vive na minha cabeca.", "Filmografia mais densa do cinema."],
    },
    {
        "username": "screenqueen",
        "post_type": "note",
        "text_content": "Portrait of a Lady on Fire e um dos filmes mais sensiveis que ja vi. A Celine Sciamma entende de desejo e de silencio como poucos diretores.",
        "sentiment": "encantada",
        "tags": ["romance", "cinema-frances", "intimismo"],
        "movie_id": 601666,
        "movie_title": "Portrait of a Lady on Fire",
        "movie_poster_path": "/mKOBdgaEFguADkJhfFslY7TYxIh.jpg",
        "movie_release_date": "2019-09-18",
        "comments": ["Esse filme vive na memoria.", "Sciamma merecia todos os premios."],
    },
    {
        "username": "screenqueen",
        "post_type": "list",
        "text_content": "Minha lista da semana para quem quer suspense com clima forte e direcao segura.",
        "sentiment": "empolgada",
        "tags": ["lista", "suspense", "semana"],
        "list_title": "3 suspenses para a semana",
        "list_items": ["Prisoners", "Gone Girl", "Zodiac"],
    },
    # ── framehunter ──────────────────────────────────────────────────────────
    {
        "username": "framehunter",
        "post_type": "review",
        "text_content": "Reassisti ontem e continua um dos thrillers mais elegantes que ja vi. Cada layer faz sentido no rewatch.",
        "sentiment": "obcecado",
        "tags": ["thriller", "rewatch", "mind-game"],
        "movie_id": 27205,
        "movie_title": "Inception",
        "movie_poster_path": "/oYuLEt3zVCKq57qu2F8dT7NIa6f.jpg",
        "movie_release_date": "2010-07-15",
        "review_headline": "Ainda funciona em qualquer rewatch",
        "rating": 4.5,
        "comments": ["Top 10 facil.", "A montagem segura tudo."],
    },
    {
        "username": "framehunter",
        "post_type": "review",
        "text_content": "Bong Joon-ho faz parecer facil o que nao e. Parasita e uma obra de genero que transcende genero. Humor, tensao e critica social no mesmo plano.",
        "sentiment": "impressionado",
        "tags": ["premiado", "bong-joon-ho", "must-watch"],
        "movie_id": 496243,
        "movie_title": "Parasite",
        "movie_poster_path": "/7IiTTgloJzvGI1TAYymCfbfl3vT.jpg",
        "movie_release_date": "2019-05-30",
        "review_headline": "Obra-prima de construcao e subversao",
        "rating": 5.0,
        "comments": ["Melhor filme dos ultimos anos, sem discussao.", "Esse porão..."],
    },
    {
        "username": "framehunter",
        "post_type": "review",
        "text_content": "Mad Max: Fury Road e pura cinematografia em movimento. George Miller provou que acao pode ser arte. 2 horas de adrenalina com proposito.",
        "sentiment": "energizado",
        "tags": ["acao", "distopia", "george-miller"],
        "movie_id": 76341,
        "movie_title": "Mad Max: Fury Road",
        "movie_poster_path": "/hA2ple9q4qnwxp3hKVNhroipsir.jpg",
        "movie_release_date": "2015-05-13",
        "review_headline": "Acao elevada a arte",
        "rating": 4.5,
        "comments": ["Cada frame e um poster.", "A Imperator Furiosa e o melhor personagem da decada."],
    },
    {
        "username": "framehunter",
        "post_type": "commentary",
        "text_content": "2001: A Space Odyssey nao e um filme que voce assiste. e um filme que voce experimenta. Kubrick criou algo fora do tempo — 1968 ou 2068, tanto faz.",
        "sentiment": "filosofico",
        "tags": ["kubrick", "sci-fi", "classico"],
        "movie_id": 62,
        "movie_title": "2001: A Space Odyssey",
        "movie_poster_path": "/ve72VxNqjIqd6aKKIdAdT9sasEF.jpg",
        "movie_release_date": "1968-04-02",
        "comments": ["HAL 9000 e o vilao mais assustador do cinema.", "O monolito ainda me intriga."],
    },
    {
        "username": "framehunter",
        "post_type": "list",
        "text_content": "Cinco filmes que todo cinefilo precisa ter na lista. Nao e top, e essencial — cada um desses muda algo em voce.",
        "sentiment": "seletivo",
        "tags": ["lista", "essenciais", "cinema"],
        "list_title": "5 filmes que mudam sua visao de cinema",
        "list_items": ["2001: A Space Odyssey", "Parasite", "Mulholland Drive", "Stalker", "In the Mood for Love"],
    },
    # ── plot_twister ─────────────────────────────────────────────────────────
    {
        "username": "plot_twister",
        "post_type": "commentary",
        "text_content": "Terminei agora e achei um sci-fi melancolico, bonito e muito humano. Esse final pesa mais a cada rewatch.",
        "sentiment": "tocada",
        "tags": ["sci-fi", "melancolico", "humano"],
        "movie_id": 78,
        "movie_title": "Blade Runner",
        "movie_poster_path": "/63N9uy8nd9j7Eog2axPQ8lbr3Wj.jpg",
        "movie_release_date": "1982-06-25",
        "comments": ["Esse final pesa muito.", "Visual absurdo ate hoje."],
    },
    {
        "username": "plot_twister",
        "post_type": "review",
        "text_content": "La La Land e um filme que sabe que vai machucar desde o primeiro numero. Ryan Gosling e Emma Stone tem uma quimica absurda.",
        "sentiment": "melancolica",
        "tags": ["musical", "romance", "la-la-land"],
        "movie_id": 313369,
        "movie_title": "La La Land",
        "movie_poster_path": "/uDO8zWDhfWwoFdKS4fzkUJt0Rf0.jpg",
        "movie_release_date": "2016-11-29",
        "review_headline": "Quando o sonho e mais bonito que a realidade",
        "rating": 4.0,
        "comments": ["Esse final me destruiu.", "A trilha sonora fica na cabeca para sempre."],
    },
    {
        "username": "plot_twister",
        "post_type": "review",
        "text_content": "Black Swan e Aronofsky em modo obsessivo. Natalie Portman dando tudo — a degeneracao do personagem e filmada com uma precisao que incomoda.",
        "sentiment": "perturbada",
        "tags": ["drama", "aronofsky", "perturbador"],
        "movie_id": 37799,
        "movie_title": "Black Swan",
        "movie_poster_path": "/4HheRTRGrVXMjsTQlyFHxVKDSMa.jpg",
        "movie_release_date": "2010-12-03",
        "review_headline": "Obsessao levada ao limite com maestria",
        "rating": 4.0,
        "comments": ["Esse filme fica na cabeca por dias.", "Natalie Portman merecia o Oscar."],
    },
    {
        "username": "plot_twister",
        "post_type": "review",
        "text_content": "Whiplash e um soco no estomago. Damien Chazelle criou um duelo de egos que vai alem da musica — e sobre ambicao, crueldade e o preco da grandeza.",
        "sentiment": "tenso",
        "tags": ["drama", "musica", "intenso"],
        "movie_id": 244786,
        "movie_title": "Whiplash",
        "movie_poster_path": "/7fn624j5lj3xTme2SgiLCeuedmO.jpg",
        "movie_release_date": "2014-10-10",
        "review_headline": "Nao e sobre bateria. e sobre o limite humano.",
        "rating": 5.0,
        "comments": ["J.K. Simmons roubou o show.", "A cena final e uma das melhores da decada."],
    },
    {
        "username": "plot_twister",
        "post_type": "note",
        "text_content": "Her ainda me pega de surpresa. Spike Jonze criou o romance mais triste e mais honesto dos ultimos anos — e o objeto do afeto nem aparece em cena.",
        "sentiment": "melancolica",
        "tags": ["sci-fi", "romance", "spike-jonze"],
        "movie_id": 152601,
        "movie_title": "Her",
        "movie_poster_path": "/eCOtqtfvn7mxGaoDaB3OFPVFJlXl.jpg",
        "movie_release_date": "2013-12-18",
        "comments": ["Esse filme e quase profetico.", "A voz da Scarlett nesse papel e unica."],
    },
    {
        "username": "plot_twister",
        "post_type": "commentary",
        "text_content": "Cidade de Deus e o filme brasileiro que o mundo precisava ver. Meirelles e Lund criaram algo visceral, urgente e humano ao mesmo tempo. Orgulho nacional.",
        "sentiment": "impactada",
        "tags": ["cinema-brasileiro", "drama", "must-watch"],
        "movie_id": 769,
        "movie_title": "Cidade de Deus",
        "movie_poster_path": "/k7eYdWvhYQyRQoU2TB2A2Xu2TfD.jpg",
        "movie_release_date": "2002-08-30",
        "comments": ["Cinema brasileiro no seu pico.", "Esse filme mudou minha visao de narrativa."],
    },
]
FALLBACK_HASHTAGS = [
    "emocao",
    "emocionante",
    "epico",
    "empolgante",
    "estetica",
    "suspense",
    "rewatch",
    "ficcaocientifica",
]
DEFAULT_WATCHLIST_TITLE = "Filmes para assistir depois"
DEFAULT_WATCHLIST_DESCRIPTION = "Sua watchlist inicial para guardar filmes que voce quer ver."
DEFAULT_WATCHED_LIST_TITLE = "Filmes ja assistidos"
DEFAULT_WATCHED_LIST_DESCRIPTION = "Sua lista padrao para reunir os filmes que voce ja viu."
LIST_DESCRIPTION_LIMIT = 180
VALID_LIST_VISIBILITIES = {"PUBLIC", "PRIVATE"}
CLUB_NAME_LIMIT = 60
CLUB_SHORT_DESCRIPTION_LIMIT = 140
CLUB_DESCRIPTION_LIMIT = 1200
DEFAULT_NOTIFICATION_PREFERENCES = {
    "post_likes": True,
    "direct_messages": True,
    "following_posts": True,
    "new_followers": True,
    "follow_request_approved": True,
    "post_comments": True,
    "comment_replies": True,
    "mentions": True,
    "club_updates": True,
}
NOTIFICATION_PREFERENCE_MAP = {
    "post_like": "post_likes",
    "direct_message": "direct_messages",
    "following_post": "following_posts",
    "new_follower": "new_followers",
    "follow_request_approved": "follow_request_approved",
    "post_comment": "post_comments",
    "comment_reply": "comment_replies",
    "post_mention": "mentions",
    "comment_mention": "mentions",
    "club_post_mention": "mentions",
    "club_join_request": "club_updates",
    "club_request_approved": "club_updates",
    "club_request_rejected": "club_updates",
    "club_post_report": "club_updates",
    "club_new_post": "club_updates",
    "follow_request": "new_followers",
    "follow_request_rejected": "follow_request_approved",
}
DEFAULT_CLUB_COVERS = [
    "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?auto=format&fit=crop&w=1200&q=80",
    "https://images.unsplash.com/photo-1517604931442-7e0c8ed2963c?auto=format&fit=crop&w=1200&q=80",
    "https://images.unsplash.com/photo-1513106580091-1d82408b8cd6?auto=format&fit=crop&w=1200&q=80",
    "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?auto=format&fit=crop&w=1200&q=80",
]
DEMO_CLUBS = [
    {
        "creator_username": "screenqueen",
        "name": "Terror Psicologico",
        "short_description": "Para quem gosta de filmes tensos, atmosfericos e perturbadores.",
        "description": "Debates sobre terror psicologico, horror de atmosfera, finais ambiguos e diretores que trabalham suspense sem jumpscare facil.",
        "genres": ["Terror", "Misterio", "Suspense"],
        "cover_image_url": "https://image.tmdb.org/t/p/w500/qJ2tW6WMUDux911r6m7haRef0WH.jpg",
        "is_private": False,
    },
    {
        "creator_username": "framehunter",
        "name": "Sci-Fi Essencial",
        "short_description": "Comunidade para ficcao cientifica classica, moderna e experimental.",
        "description": "Vale blockbuster, cult, viagem no tempo, distopia, cyberpunk e sci-fi filosofico. O foco e discutir ideias e impacto visual.",
        "genres": ["Ficcao cientifica", "Drama"],
        "cover_image_url": "https://image.tmdb.org/t/p/w500/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg",
        "is_private": False,
    },
    {
        "creator_username": "plot_twister",
        "name": "Cinema Brasileiro em Cena",
        "short_description": "Espaco para falar de producoes brasileiras, classicos e novidades.",
        "description": "Um clube para comentar filmes nacionais, diretores, festivais, atuações marcantes e a identidade do cinema brasileiro.",
        "genres": ["Drama", "Comedia"],
        "cover_image_url": "https://image.tmdb.org/t/p/w500/8jNNQZrb5Vg5l5C1M8VxG2V8A2r.jpg",
        "is_private": False,
    },
    {
        "creator_username": "screenqueen",
        "name": "Oscar e Temporada de Premios",
        "short_description": "Para acompanhar indicacoes, campanhas e favoritos do ano.",
        "description": "Analises sobre Oscar, Globo de Ouro, BAFTA e a corrida dos premios. Bom para quem gosta de prever indicacoes e discutir hype.",
        "genres": ["Drama", "Historia"],
        "cover_image_url": "https://image.tmdb.org/t/p/w500/9O1Iy9od7y8P7oBvKxA5z7b2jKQ.jpg",
        "is_private": True,
    },
    {
        "creator_username": "framehunter",
        "name": "Classicos do Suspense",
        "short_description": "Comunidade para Hitchcock, neo-noir e thrillers influentes.",
        "description": "Clube mais fechado para quem quer discussoes mais profundas sobre linguagem, montagem, mise-en-scene e legado do suspense classico.",
        "genres": ["Suspense", "Misterio", "Crime"],
        "cover_image_url": "https://image.tmdb.org/t/p/w500/63N9uy8nd9j7Eog2axPQ8lbr3Wj.jpg",
        "is_private": True,
    },
]


def get_user_by_username(db: Session, username: str):
    return db.query(models.User).filter(models.User.username == username).first()


def get_user_by_email(db: Session, email: str):
    return db.query(models.User).filter(models.User.email == email).first()


def get_user_by_id(db: Session, user_id: int):
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_user_preference(db: Session, user_id: int):
    return (
        db.query(models.UserPreference)
        .filter(models.UserPreference.user_id == user_id)
        .first()
    )


def is_private_account_enabled(db: Session, user_id: int) -> bool:
    preference = get_user_preference(db, user_id)
    return bool(getattr(preference, "is_private_account", False)) if preference else False


def get_pending_follow_request(db: Session, requester_id: int, target_user_id: int):
    return (
        db.query(models.FollowRequest)
        .filter(
            models.FollowRequest.requester_id == requester_id,
            models.FollowRequest.target_user_id == target_user_id,
            models.FollowRequest.status == "pending",
        )
        .first()
    )


def can_view_user_private_profile(db: Session, target_user_id: int, viewer_user_id: int | None):
    if viewer_user_id == target_user_id:
        return True
    if not is_private_account_enabled(db, target_user_id):
        return True
    if viewer_user_id is None:
        return False
    return target_user_id in _get_followed_ids(db, viewer_user_id)


def get_or_create_user_preference(db: Session, user_id: int):
    preference = get_user_preference(db, user_id)
    if preference:
        return preference

    preference = models.UserPreference(
        user_id=user_id,
        favorite_movie_json="null",
        notification_preferences_json=serialize_notification_preferences(None),
        is_private_account=False,
    )
    db.add(preference)
    db.flush()
    return preference


def build_favorite_movie_input_from_record(movie) -> schemas.FavoriteMovieInput | None:
    if not movie:
        return None
    return schemas.FavoriteMovieInput(
        movie_id=movie["movie_id"] if isinstance(movie, dict) else movie.movie_id,
        title=movie["title"] if isinstance(movie, dict) else movie.title,
        poster_path=movie.get("poster_path") if isinstance(movie, dict) else movie.poster_path,
        release_date=movie.get("release_date") if isinstance(movie, dict) else movie.release_date,
    )


def get_profile_favorite_movie(db: Session, user_id: int, preference: models.UserPreference | None = None):
    preference = preference or get_user_preference(db, user_id)
    if preference and getattr(preference, "favorite_movie_json", None):
        try:
            payload = json.loads(preference.favorite_movie_json or "null")
        except Exception:
            payload = None
        movie = build_favorite_movie_input_from_record(payload) if isinstance(payload, dict) else None
        if movie:
            return movie

    favorite_movie = (
        db.query(models.FavoriteMovie)
        .filter(models.FavoriteMovie.user_id == user_id)
        .order_by(models.FavoriteMovie.created_at.asc(), models.FavoriteMovie.id.asc())
        .first()
    )
    if favorite_movie:
        return build_favorite_movie_input_from_record(favorite_movie)

    top_rating = (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.user_id == user_id)
        .order_by(models.RatedMovie.rating.desc(), models.RatedMovie.rated_at.desc())
        .first()
    )
    if top_rating:
        return build_favorite_movie_input_from_record(top_rating)

    return None


def ensure_demo_ratings(db: Session, user: models.User):
    existing = db.query(models.RatedMovie).filter(models.RatedMovie.user_id == user.id).count()
    if existing:
        return

    base_date = _utcnow()
    for index, movie in enumerate(DEMO_RATED_MOVIES):
        db.add(
            models.RatedMovie(
                user_id=user.id,
                movie_id=movie["movie_id"],
                title=movie["title"],
                poster_path=movie["poster_path"],
                release_date=movie["release_date"],
                rating=normalize_rating_to_half_step(movie["rating"]),
                rated_at=base_date - timedelta(days=index),
            )
        )
    db.commit()


def normalize_existing_rating_steps(db: Session):
    changed = False

    for rated_movie in db.query(models.RatedMovie).all():
        normalized_rating = normalize_rating_to_half_step(rated_movie.rating)
        if abs(float(rated_movie.rating or 0) - normalized_rating) > 1e-9:
            rated_movie.rating = normalized_rating
            changed = True

    for review in db.query(models.Review).filter(models.Review.rating.isnot(None)).all():
        normalized_rating = normalize_rating_to_half_step(review.rating)
        if abs(float(review.rating or 0) - normalized_rating) > 1e-9:
            review.rating = normalized_rating
            changed = True

    if changed:
        db.commit()


def backfill_profile_preferences(db: Session):
    changed = False
    preferences = db.query(models.UserPreference).all()
    for preference in preferences:
        normalized_notifications = serialize_notification_preferences(
            getattr(preference, "notification_preferences_json", None)
        )
        if preference.notification_preferences_json != normalized_notifications:
            preference.notification_preferences_json = normalized_notifications
            changed = True

        favorite_movie = get_profile_favorite_movie(db, preference.user_id, preference=preference)
        serialized_favorite_movie = json.dumps(favorite_movie.model_dump()) if favorite_movie else "null"
        if (preference.favorite_movie_json or "null") != serialized_favorite_movie:
            preference.favorite_movie_json = serialized_favorite_movie
            changed = True

    if changed:
        db.commit()


def create_user(db: Session, user: schemas.UserCreate):
    hashed_password = auth.get_password_hash(user.password)
    db_user = models.User(
        username=user.username,
        email=user.email,
        hashed_password=hashed_password,
        profile_image_url=f"{DEFAULT_PROFILE_IMAGE}&name={user.username}",
        bio="",
        is_verified_critic=False,
        onboarding_completed=False,
        default_watchlist_created=False,
        country=user.country or None,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    ensure_default_watchlist(db, db_user)
    ensure_demo_ratings(db, db_user)
    return db_user


def get_recent_ratings(db: Session, user_id: int, limit: int = 6):
    return (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.user_id == user_id)
        .order_by(models.RatedMovie.rated_at.desc())
        .limit(limit)
        .all()
    )


def get_top_ratings(db: Session, user_id: int, limit: int = 6):
    return (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.user_id == user_id)
        .order_by(models.RatedMovie.rating.desc(), models.RatedMovie.rated_at.desc())
        .limit(limit)
        .all()
    )


def get_all_ratings(db: Session, user_id: int):
    return (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.user_id == user_id)
        .order_by(models.RatedMovie.rated_at.desc())
        .all()
    )


def get_user_rating_for_movie(db: Session, user_id: int, movie_id: int):
    rating = (
        db.query(models.RatedMovie)
        .filter(
            models.RatedMovie.user_id == user_id,
            models.RatedMovie.movie_id == movie_id,
        )
        .order_by(models.RatedMovie.rated_at.desc(), models.RatedMovie.id.desc())
        .first()
    )
    return rating.rating if rating else None


def rate_movie(
    db: Session,
    current_user: models.User,
    movie_id: int,
    payload: schemas.MovieRatingCreate,
):
    current_user = db.merge(current_user)
    ensure_default_watchlist(db, current_user)
    raw_rating_value = float(payload.rating)
    if raw_rating_value < 0.5 or raw_rating_value > 5:
        raise ValueError("Rating must be between 0.5 and 5")
    if (raw_rating_value * 2) % 1 != 0:
        raise ValueError("Rating must use 0.5 steps")
    rating_value = normalize_rating_to_half_step(raw_rating_value)

    now = _utcnow()
    existing_rating = (
        db.query(models.RatedMovie)
        .filter(
            models.RatedMovie.user_id == current_user.id,
            models.RatedMovie.movie_id == movie_id,
        )
        .first()
    )

    if existing_rating:
        existing_rating.title = payload.title.strip()
        existing_rating.poster_path = payload.poster_path
        existing_rating.release_date = payload.release_date
        existing_rating.rating = rating_value
        existing_rating.rated_at = now
        rated_movie = existing_rating
    else:
        rated_movie = models.RatedMovie(
            user_id=current_user.id,
            movie_id=movie_id,
            title=payload.title.strip(),
            poster_path=payload.poster_path,
            release_date=payload.release_date,
            rating=rating_value,
            rated_at=now,
        )
        db.add(rated_movie)

    _sync_movie_between_default_lists(
        db,
        current_user,
        movie_id,
        payload.title.strip(),
        payload.poster_path,
        payload.release_date,
    )

    db.commit()
    db.refresh(rated_movie)
    return rated_movie


def get_friend_ratings_for_movie(db: Session, user_id: int, movie_id: int):
    friendships = (
        db.query(models.Friendship)
        .filter(models.Friendship.user_id == user_id)
        .all()
    )
    friend_ids = [friendship.friend_id for friendship in friendships]
    if not friend_ids:
        return []

    ratings = (
        db.query(models.RatedMovie, models.User)
        .join(models.User, models.User.id == models.RatedMovie.user_id)
        .filter(
            models.RatedMovie.user_id.in_(friend_ids),
            models.RatedMovie.movie_id == movie_id,
        )
        .order_by(models.RatedMovie.rating.desc(), models.RatedMovie.rated_at.desc())
        .all()
    )

    return [
        {
            "username": user.username,
            "profile_image_url": user.profile_image_url,
            "is_verified_critic": bool(user.is_verified_critic),
            "rating": rated_movie.rating,
            "rated_at": rated_movie.rated_at.isoformat(),
        }
        for rated_movie, user in ratings
    ]


def get_average_rating_for_movie(db: Session, movie_id: int):
    avg_rating = (
        db.query(func.avg(models.RatedMovie.rating))
        .filter(models.RatedMovie.movie_id == movie_id)
        .scalar()
    )
    rating_count = (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.movie_id == movie_id)
        .count()
    )
    return {
        "average_rating": round(float(avg_rating), 2) if avg_rating is not None else None,
        "rating_count": rating_count,
    }


def get_movie_reviews(db: Session, current_user: models.User, movie_id: int, limit: int = 6):
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)

    # Fetch a larger pool so random sampling has variety
    rows = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(models.Post.movie_id == movie_id, models.Post.visible_in_feed == True)
        .order_by(models.Post.id.desc())
        .limit(limit * 10)
        .all()
    )

    critic_rows = []
    community_rows = []
    for post, author in rows:
        if bool(author.is_verified_critic):
            critic_rows.append((post, author))
        else:
            community_rows.append((post, author))

    # Critics: newest first (curated content keeps chronological order)
    critic_rows.sort(key=lambda r: r[0].created_at, reverse=True)

    # Community: random sample each request for discovery variety
    random.shuffle(community_rows)

    critic_reviews = [
        _build_feed_post(db, post, author, current_user.id, followed_ids)
        for post, author in critic_rows[:limit]
    ]
    community_reviews = [
        _build_feed_post(db, post, author, current_user.id, followed_ids)
        for post, author in community_rows[:limit]
    ]

    return {
        "critic_reviews": critic_reviews,
        "community_reviews": community_reviews,
    }


def get_verified_critics(db: Session, current_user: models.User, limit: int = 50) -> list[schemas.CriticSummary]:
    critics = (
        db.query(models.User)
        .filter(models.User.is_verified_critic == True)
        .order_by(models.User.username)
        .limit(limit)
        .all()
    )
    result = []
    for critic in critics:
        review_count = (
            db.query(models.Post)
            .filter(models.Post.user_id == critic.id, models.Post.visible_in_feed == True)
            .count()
        )
        follower_count = (
            db.query(models.Follow)
            .filter(models.Follow.following_id == critic.id)
            .count()
        )
        result.append(schemas.CriticSummary(
            id=critic.id,
            username=critic.username,
            profile_image_url=critic.profile_image_url,
            bio=critic.bio,
            review_count=review_count,
            follower_count=follower_count,
        ))
    return result


def get_critic_reviews_by_username(
    db: Session,
    username: str,
    current_user: models.User,
    cursor: str | None = None,
    limit: int = 10,
) -> schemas.FeedResponse:
    critic = db.query(models.User).filter(models.User.username == username).first()
    if not critic or not bool(critic.is_verified_critic):
        raise LookupError("Verified critic not found")

    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)

    query = (
        db.query(models.Post)
        .filter(
            models.Post.user_id == critic.id,
            models.Post.visible_in_feed == True,
        )
        .order_by(models.Post.created_at.desc(), models.Post.id.desc())
    )

    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.filter(models.Post.id < cursor_id)
        except ValueError:
            pass

    posts = query.limit(limit + 1).all()
    has_more = len(posts) > limit
    posts = posts[:limit]

    items = [_build_feed_post(db, post, critic, current_user.id, followed_ids) for post in posts]
    next_cursor = str(posts[-1].id) if has_more and posts else None
    return schemas.FeedResponse(items=items, next_cursor=next_cursor)


def submit_critic_application(
    db: Session,
    current_user: models.User,
    motivation: str,
    portfolio_url: str | None,
) -> models.CriticApplication:
    if bool(current_user.is_verified_critic):
        raise ValueError("Sua conta ja e um critico verificado.")
    existing = (
        db.query(models.CriticApplication)
        .filter(models.CriticApplication.user_id == current_user.id)
        .first()
    )
    if existing:
        if existing.status == "pending":
            raise ValueError("Voce ja tem uma solicitacao pendente de avaliacao.")
        if existing.status == "approved":
            raise ValueError("Sua solicitacao ja foi aprovada.")
        existing.motivation = motivation.strip()
        existing.portfolio_url = (portfolio_url or "").strip() or None
        existing.status = "pending"
        existing.reviewed_at = None
        existing.created_at = _utcnow()
        db.commit()
        db.refresh(existing)
        return existing
    application = models.CriticApplication(
        user_id=current_user.id,
        motivation=motivation.strip(),
        portfolio_url=(portfolio_url or "").strip() or None,
        status="pending",
        created_at=_utcnow(),
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def get_my_critic_application(db: Session, current_user: models.User) -> models.CriticApplication | None:
    return (
        db.query(models.CriticApplication)
        .filter(models.CriticApplication.user_id == current_user.id)
        .first()
    )


def get_pending_critic_applications(db: Session, limit: int = 100) -> list[models.CriticApplication]:
    return (
        db.query(models.CriticApplication)
        .filter(models.CriticApplication.status == "pending")
        .order_by(models.CriticApplication.created_at)
        .limit(limit)
        .all()
    )


def review_critic_application(
    db: Session,
    application_id: int,
    approve: bool,
) -> models.CriticApplication:
    application = db.query(models.CriticApplication).filter(models.CriticApplication.id == application_id).first()
    if not application:
        raise LookupError("Application not found")
    if application.status != "pending":
        raise ValueError("This application has already been reviewed.")
    application.status = "approved" if approve else "rejected"
    application.reviewed_at = _utcnow()
    db.commit()
    if approve:
        user = db.query(models.User).filter(models.User.id == application.user_id).first()
        if user:
            user.is_verified_critic = True
            db.commit()
    db.refresh(application)
    return application


def set_critic_status(db: Session, user_id: int, is_verified_critic: bool) -> models.User:
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise LookupError("User not found")
    user.is_verified_critic = is_verified_critic
    db.commit()
    db.refresh(user)
    return user


def get_lists_containing_movie(
    db: Session,
    user_id: int,
    movie_id: int,
    movie_title: str | None = None,
    release_date: str | None = None,
    poster_path: str | None = None,
):
    normalized_title, normalized_release_date = normalize_movie_identity(movie_title, release_date)
    normalized_visual_identity = normalize_movie_visual_identity(poster_path, release_date)

    rows = (
        db.query(models.MovieList, models.MovieListItem)
        .join(models.MovieListItem, models.MovieListItem.list_id == models.MovieList.id)
        .filter(models.MovieList.user_id == user_id)
        .order_by(models.MovieList.updated_at.desc(), models.MovieList.id.desc())
        .all()
    )
    seen_ids = set()
    items = []
    for movie_list, list_item in rows:
        same_movie_id = list_item.movie_id == movie_id
        same_identity = False
        if normalized_title:
            item_title, item_release_date = normalize_movie_identity(
                list_item.movie_title, list_item.release_date
            )
            same_identity = item_title == normalized_title and (
                not normalized_release_date or item_release_date == normalized_release_date
            )
        same_visual_identity = False
        if normalized_visual_identity[0]:
            same_visual_identity = (
                normalize_movie_visual_identity(list_item.poster_path, list_item.release_date)
                == normalized_visual_identity
            )
        if not same_movie_id and not same_identity and not same_visual_identity:
            continue
        if movie_list.id in seen_ids:
            continue
        seen_ids.add(movie_list.id)
        items.append(
            {
                "id": movie_list.id,
                "item_id": list_item.id,
                "title": movie_list.title,
                "visibility": movie_list.visibility,
            }
        )
    return items


def get_badges_for_user(db: Session, user_id: int):
    # Batch all counts in a few queries
    rated_count = db.query(models.RatedMovie).filter(models.RatedMovie.user_id == user_id).count()
    post_count = (
        db.query(models.Post)
        .filter(models.Post.user_id == user_id, models.Post.club_id.is_(None))
        .count()
    )
    follower_count = db.query(models.Follow).filter(models.Follow.following_id == user_id).count()
    following_count = db.query(models.Follow).filter(models.Follow.follower_id == user_id).count()
    club_count = db.query(models.ClubMembership).filter(models.ClubMembership.user_id == user_id).count()
    comment_count = db.query(models.Comment).filter(models.Comment.user_id == user_id).count()
    is_critic = bool(
        db.query(models.User.is_verified_critic)
        .filter(models.User.id == user_id)
        .scalar()
    )

    return [
        # Cinephile tier (ratings)
        schemas.BadgeSummary(
            title="Critico em ascensao",
            description="Avalie 10 filmes.",
            locked=rated_count < 10,
        ),
        schemas.BadgeSummary(
            title="Maratona 50",
            description="Avalie 50 filmes.",
            locked=rated_count < 50,
        ),
        schemas.BadgeSummary(
            title="Centuriao",
            description="Avalie 100 filmes.",
            locked=rated_count < 100,
        ),
        schemas.BadgeSummary(
            title="Arquivo vivo",
            description="Avalie 500 filmes.",
            locked=rated_count < 500,
        ),
        # Social tier
        schemas.BadgeSummary(
            title="Criador de conteudo",
            description="Publique 10 reviews no feed.",
            locked=post_count < 10,
        ),
        schemas.BadgeSummary(
            title="Voz da comunidade",
            description="Conquiste 10 seguidores.",
            locked=follower_count < 10,
        ),
        schemas.BadgeSummary(
            title="Influenciador",
            description="Conquiste 50 seguidores.",
            locked=follower_count < 50,
        ),
        schemas.BadgeSummary(
            title="Descobridor",
            description="Siga 20 usuarios.",
            locked=following_count < 20,
        ),
        schemas.BadgeSummary(
            title="Comentarista ativo",
            description="Faca 25 comentarios em posts.",
            locked=comment_count < 25,
        ),
        # Club tier
        schemas.BadgeSummary(
            title="Membro de clube",
            description="Participe de pelo menos 1 clube.",
            locked=club_count < 1,
        ),
        schemas.BadgeSummary(
            title="Poliglota cinefilo",
            description="Participe de 3 clubes diferentes.",
            locked=club_count < 3,
        ),
        # Verified critic
        schemas.BadgeSummary(
            title="Critico verificado",
            description="Torne-se um critico verificado pelo Moviz.",
            locked=not is_critic,
        ),
    ]


def get_friends(db: Session, user_id: int):
    friendships = (
        db.query(models.Friendship)
        .filter(models.Friendship.user_id == user_id)
        .all()
    )
    friend_ids = [friendship.friend_id for friendship in friendships]
    if not friend_ids:
        return []
    return (
        db.query(models.User)
        .filter(models.User.id.in_(friend_ids))
        .order_by(models.User.username.asc())
        .all()
    )


def _build_follow_user_summary(user: models.User):
    return schemas.FollowUserSummary(
        id=user.id,
        username=user.username,
        profile_image_url=user.profile_image_url,
        is_verified_critic=bool(user.is_verified_critic),
    )


def get_followers(db: Session, user_id: int, limit: int = 18):
    users = (
        db.query(models.User)
        .join(models.Follow, models.Follow.follower_id == models.User.id)
        .filter(models.Follow.following_id == user_id)
        .order_by(models.User.username.asc())
        .limit(limit)
        .all()
    )
    return [_build_follow_user_summary(user) for user in users]


def get_following_users(db: Session, user_id: int, limit: int = 18):
    users = (
        db.query(models.User)
        .join(models.Follow, models.Follow.following_id == models.User.id)
        .filter(models.Follow.follower_id == user_id)
        .order_by(models.User.username.asc())
        .limit(limit)
        .all()
    )
    return [_build_follow_user_summary(user) for user in users]


def get_follow_connections(
    db: Session,
    username: str,
    kind: str,
    viewer_user_id: int | None = None,
):
    user = get_user_by_username(db, username)
    if not user:
        raise LookupError("User not found")

    normalized_kind = (kind or "").lower()
    if normalized_kind not in {"followers", "following"}:
        raise ValueError("Invalid connection kind")

    if not can_view_user_private_profile(db, user.id, viewer_user_id):
        raise ValueError("This private profile hides its connections list")

    items = (
        get_followers(db, user.id, limit=1000)
        if normalized_kind == "followers"
        else get_following_users(db, user.id, limit=1000)
    )

    return schemas.FollowConnectionListResponse(
        username=user.username,
        kind=normalized_kind,
        total=len(items),
        items=items,
    )


def get_follow_counts(db: Session, user_id: int):
    follower_count = (
        db.query(models.Follow)
        .filter(models.Follow.following_id == user_id)
        .count()
    )
    following_count = (
        db.query(models.Follow)
        .filter(models.Follow.follower_id == user_id)
        .count()
    )
    return follower_count, following_count


def build_profile_stats(db: Session, user: models.User):
    ratings = (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.user_id == user.id)
        .order_by(models.RatedMovie.rated_at.desc())
        .all()
    )
    rated_movies_count = len(ratings)
    average_rating = (
        round(sum(float(item.rating or 0) for item in ratings) / rated_movies_count, 2)
        if rated_movies_count
        else 0
    )

    genre_counts: dict[str, int] = {}
    for rating in ratings:
        try:
            genres = tmdb.fetch_movie_genres_sync(rating.movie_id)
        except Exception:
            genres = []
        for genre in genres:
            genre_counts[genre] = genre_counts.get(genre, 0) + 1

    top_genres = [
        schemas.ProfileTopGenre(
            name=name,
            count=count,
            percentage=min(100, round((count / rated_movies_count) * 100)) if rated_movies_count else 0,
        )
        for name, count in sorted(
            genre_counts.items(),
            key=lambda item: (-item[1], item[0].lower()),
        )[:3]
    ]

    posts = (
        db.query(models.Post)
        .filter(models.Post.user_id == user.id)
        .order_by(models.Post.created_at.desc())
        .limit(40)
        .all()
    )
    tag_pool = []
    sentiment_pool = []
    for post in posts:
        sentiment_pool.append((post.sentiment or "").strip().lower())
        try:
            tag_pool.extend([tag.strip().lower() for tag in json.loads(post.tags_json or "[]") if tag])
        except Exception:
            continue

    top_genre_names = [item.name.lower() for item in top_genres]
    avg = average_rating
    signal = "heuristica_inicial"
    if any(genre in top_genre_names for genre in ["terror", "misterio", "suspense"]):
        mood = schemas.ProfileMoodSummary(
            title="Sombrio e curioso",
            description="Voce tende a voltar para historias tensas, atmosfericas e cheias de misterio.",
            signal=signal,
        )
    elif any(genre in top_genre_names for genre in ["ficcao cientifica", "aventura", "acao"]) and avg >= 3.8:
        mood = schemas.ProfileMoodSummary(
            title="Explorador e empolgado",
            description="Seu padrao mostra busca por escala, descoberta e filmes que entregam energia ou imaginacao.",
            signal=signal,
        )
    elif any(genre in top_genre_names for genre in ["drama", "romance", "historia"]) or any(
        term in tag_pool for term in ["emocao", "emocionante", "melancolico", "humano"]
    ):
        mood = schemas.ProfileMoodSummary(
            title="Emocional e reflexivo",
            description="Suas notas e tags sugerem preferencia por filmes mais afetivos, intensos e contemplativos.",
            signal=signal,
        )
    elif avg >= 4.2 or any(term in tag_pool for term in ["rewatch", "must-watch", "epico"]):
        mood = schemas.ProfileMoodSummary(
            title="Entusiasmado e afetivo",
            description="Voce costuma avaliar bem o que ve e demonstra forte conexao com filmes que marcam sua experiencia.",
            signal=signal,
        )
    elif any(sentiment in sentiment_pool for sentiment in ["dividido", "frustrado", "confuso"]):
        mood = schemas.ProfileMoodSummary(
            title="Critico e seletivo",
            description="Seu historico mostra um olhar mais exigente, atento a nuance e menos propenso a notas altas por impulso.",
            signal=signal,
        )
    else:
        mood = schemas.ProfileMoodSummary(
            title="Ecletico em construcao",
            description="Seu historico ainda mistura sinais diferentes. Quando a IA entrar, esse retrato vai ficar mais preciso.",
            signal=signal,
        )

    return schemas.ProfileStats(
        average_rating=average_rating,
        rated_movies_count=rated_movies_count,
        top_genres=top_genres,
        mood=mood,
    )


def sync_following_to_friendships(db: Session, user_id: int):
    followed_ids = [
        row.following_id
        for row in db.query(models.Follow).filter(models.Follow.follower_id == user_id).all()
    ]
    if not followed_ids:
        return

    existing_friend_ids = {
        row.friend_id
        for row in db.query(models.Friendship).filter(models.Friendship.user_id == user_id).all()
    }

    created = False
    for followed_id in followed_ids:
        if followed_id not in existing_friend_ids:
            db.add(models.Friendship(user_id=user_id, friend_id=followed_id))
            created = True

    if created:
        db.commit()


def build_profile_response(db: Session, user: models.User, viewer_user_id: int | None = None):
    ensure_demo_ratings(db, user)
    viewer_context_user_id = viewer_user_id or user.id
    viewer_followed_ids = _get_followed_ids(db, viewer_context_user_id)
    can_view_notifications = viewer_user_id == user.id
    follower_count, following_count = get_follow_counts(db, user.id)
    preference = get_user_preference(db, user.id)
    favorite_movie = get_profile_favorite_movie(db, user.id, preference=preference)
    notification_preferences = merge_notification_preferences(
        getattr(preference, "notification_preferences_json", None)
    )
    is_private_account = bool(getattr(preference, "is_private_account", False)) if preference else False
    is_following = bool(viewer_user_id and user.id in viewer_followed_ids)
    pending_request = (
        get_pending_follow_request(db, viewer_user_id, user.id)
        if viewer_user_id and viewer_user_id != user.id
        else None
    )
    can_view_private_profile = can_view_user_private_profile(db, user.id, viewer_user_id)
    private_profile_message = (
        "Esta conta e privada. Envie um pedido para seguir e ver o conteudo completo."
        if is_private_account and not can_view_private_profile
        else None
    )
    return schemas.ProfileResponse(
        user=user,
        bio=user.bio,
        preferred_genres=json.loads(preference.favorite_genres) if preference else [],
        favorite_movie=favorite_movie,
        is_private_account=is_private_account,
        notification_preferences=schemas.NotificationPreferences(**notification_preferences),
        follower_count=follower_count,
        following_count=following_count,
        followers=get_followers(db, user.id),
        following=get_following_users(db, user.id),
        is_following=is_following,
        follow_request_pending=bool(pending_request),
        can_view_private_profile=can_view_private_profile,
        private_profile_message=private_profile_message,
        recent_ratings=get_recent_ratings(db, user.id) if can_view_private_profile else [],
        top_ratings=get_top_ratings(db, user.id) if can_view_private_profile else [],
        posts=get_user_feed_posts(db, user.id, viewer_context_user_id, viewer_followed_ids) if can_view_private_profile else [],
        saved_posts=get_saved_feed_posts(db, user.id, viewer_followed_ids) if can_view_notifications else [],
        public_lists=get_public_lists_for_user(db, user.id) if can_view_private_profile else [],
        notifications=get_notifications(db, user) if can_view_notifications else [],
        badges=get_badges_for_user(db, user.id) if can_view_private_profile else [],
        friends=[
            schemas.FriendSummary(
                username=friend.username,
                profile_image_url=friend.profile_image_url,
            )
            for friend in get_friends(db, user.id)
        ] if can_view_private_profile else [],
        stats=build_profile_stats(db, user) if can_view_private_profile else schemas.ProfileStats(
            average_rating=0,
            rated_movies_count=0,
            top_genres=[],
            mood=schemas.ProfileMoodSummary(
                title="Perfil privado",
                description="Os detalhes completos ficam disponiveis depois que o pedido for aceito.",
                signal="private",
            ),
        ),
    )


def default_profile_image_for_username(username: str):
    return f"{DEFAULT_PROFILE_IMAGE}&name={username}"


def save_profile_image(file) -> str:
    return storage.upload_profile_image(file)


def delete_uploaded_profile_image(image_url: str | None) -> None:
    storage.delete_profile_image(image_url)


def update_profile(db: Session, user: models.User, username: str, profile_image_url: str | None = None):
    return update_profile_details(db, user, username=username, profile_image_url=profile_image_url)


def update_profile_details(
    db: Session,
    user: models.User,
    username: str,
    profile_image_url: str | None = None,
    bio: str | None = None,
    favorite_genres: list[str] | None = None,
    favorite_movie: schemas.FavoriteMovieInput | None = None,
    is_private_account: bool | None = None,
    notification_preferences: dict[str, bool] | None = None,
    current_password: str | None = None,
    new_password: str | None = None,
    country: str | None = None,
):
    user = db.merge(user)
    username = (username or "").strip()
    if not username:
        raise ValueError("Username is required")
    username_owner = get_user_by_username(db, username)
    if username_owner and username_owner.id != user.id:
        raise ValueError("Username already registered")

    previous_image_url = user.profile_image_url
    user.username = username
    if profile_image_url:
        next_image_url = profile_image_url
    elif previous_image_url and not previous_image_url.startswith(DEFAULT_PROFILE_IMAGE):
        next_image_url = previous_image_url
    else:
        next_image_url = default_profile_image_for_username(username)

    user.profile_image_url = next_image_url
    user.bio = (bio or "").strip()[:280]
    if country is not None:
        user.country = country.strip() or None
    if new_password:
        if not current_password:
            raise ValueError("Current password is required to change your password")
        if not auth.verify_password(current_password, user.hashed_password):
            raise ValueError("Current password is incorrect")
        if len(new_password) < 6:
            raise ValueError("Password must have at least 6 characters")
        user.hashed_password = auth.get_password_hash(new_password)

    preference = None
    if (
        favorite_genres is not None
        or favorite_movie is not None
        or is_private_account is not None
        or notification_preferences is not None
    ):
        preference = get_or_create_user_preference(db, user.id)

    if favorite_genres is not None and preference is not None:
        preference.favorite_genres = json.dumps(favorite_genres[:8])
        preference.updated_at = _utcnow()

    if favorite_movie is not None and preference is not None:
        preference.favorite_movie_json = (
            json.dumps(favorite_movie.model_dump()) if favorite_movie else "null"
        )
        existing_primary_movie = (
            db.query(models.FavoriteMovie)
            .filter(
                models.FavoriteMovie.user_id == user.id,
                models.FavoriteMovie.movie_id == favorite_movie.movie_id,
            )
            .first()
            if favorite_movie
            else None
        )
        if favorite_movie and not existing_primary_movie:
            db.add(
                models.FavoriteMovie(
                    user_id=user.id,
                    movie_id=favorite_movie.movie_id,
                    title=favorite_movie.title,
                    poster_path=favorite_movie.poster_path,
                    release_date=favorite_movie.release_date,
                )
            )
        preference.updated_at = _utcnow()

    if is_private_account is not None and preference is not None:
        preference.is_private_account = bool(is_private_account)
        preference.updated_at = _utcnow()

    if notification_preferences is not None and preference is not None:
        preference.notification_preferences_json = serialize_notification_preferences(
            notification_preferences
        )
        preference.updated_at = _utcnow()

    db.commit()
    db.refresh(user)

    if previous_image_url != user.profile_image_url:
        delete_uploaded_profile_image(previous_image_url)

    return build_profile_response(db, user, viewer_user_id=user.id)



def get_user_profile_by_username(db: Session, username: str, viewer_user_id: int | None = None):
    user = get_user_by_username(db, username)
    if not user:
        return None
    return build_profile_response(db, user, viewer_user_id=viewer_user_id)


def reset_user_password(db: Session, user: models.User, new_password: str):
    user = db.merge(user)
    user.hashed_password = auth.get_password_hash(new_password)
    db.commit()
    db.refresh(user)
    return user


def delete_user_account(db: Session, current_user: models.User) -> None:
    uid = current_user.id

    # Collect post IDs owned by this user before any deletion
    post_ids = [
        row.id
        for row in db.query(models.Post.id).filter(models.Post.user_id == uid).all()
    ]

    # 1. Remove the user from all conversation memberships and their sent DMs
    db.query(models.DirectMessage).filter(models.DirectMessage.sender_user_id == uid).delete(synchronize_session=False)
    db.query(models.ConversationMember).filter(models.ConversationMember.user_id == uid).delete(synchronize_session=False)

    # 2. Reactions and comments made BY this user on any post
    db.query(models.Like).filter(models.Like.user_id == uid).delete(synchronize_session=False)
    db.query(models.Comment).filter(models.Comment.user_id == uid).delete(synchronize_session=False)
    db.query(models.SavedPost).filter(models.SavedPost.user_id == uid).delete(synchronize_session=False)
    db.query(models.PostShare).filter(
        or_(models.PostShare.sender_user_id == uid, models.PostShare.recipient_user_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.PostReport).filter(models.PostReport.reporter_user_id == uid).delete(synchronize_session=False)

    # 3. Notifications sent to or triggered by this user
    db.query(models.Notification).filter(
        or_(models.Notification.user_id == uid, models.Notification.actor_user_id == uid)
    ).delete(synchronize_session=False)

    # 4. All data associated with the user's own posts
    if post_ids:
        db.query(models.Like).filter(models.Like.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.Comment).filter(models.Comment.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.SavedPost).filter(models.SavedPost.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.PostShare).filter(models.PostShare.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.Notification).filter(models.Notification.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.Review).filter(models.Review.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.PostReport).filter(models.PostReport.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(models.Post).filter(models.Post.user_id == uid).delete(synchronize_session=False)

    # 5. Social graph
    db.query(models.Follow).filter(
        or_(models.Follow.follower_id == uid, models.Follow.following_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.FollowRequest).filter(
        or_(models.FollowRequest.requester_id == uid, models.FollowRequest.target_user_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.Friendship).filter(
        or_(models.Friendship.user_id == uid, models.Friendship.friend_id == uid)
    ).delete(synchronize_session=False)

    # 6. Club memberships and join requests
    db.query(models.ClubMembership).filter(models.ClubMembership.user_id == uid).delete(synchronize_session=False)
    db.query(models.ClubJoinRequest).filter(models.ClubJoinRequest.user_id == uid).delete(synchronize_session=False)

    # 7. Movie data
    db.query(models.RatedMovie).filter(models.RatedMovie.user_id == uid).delete(synchronize_session=False)
    db.query(models.FavoriteMovie).filter(models.FavoriteMovie.user_id == uid).delete(synchronize_session=False)
    db.query(models.FavoritePerson).filter(models.FavoritePerson.user_id == uid).delete(synchronize_session=False)

    # 8. Movie lists
    list_ids = [
        row.id
        for row in db.query(models.MovieList.id).filter(models.MovieList.user_id == uid).all()
    ]
    if list_ids:
        db.query(models.MovieListItem).filter(models.MovieListItem.list_id.in_(list_ids)).delete(synchronize_session=False)
        db.query(models.MovieList).filter(models.MovieList.user_id == uid).delete(synchronize_session=False)

    # 9. Preferences and critic application
    db.query(models.UserPreference).filter(models.UserPreference.user_id == uid).delete(synchronize_session=False)
    db.query(models.CriticApplication).filter(models.CriticApplication.user_id == uid).delete(synchronize_session=False)

    # 10. User row itself
    db.query(models.User).filter(models.User.id == uid).delete(synchronize_session=False)

    db.commit()


def save_onboarding_preferences(db: Session, user: models.User, payload: schemas.OnboardingPayload):
    user = db.merge(user)

    preference = (
        db.query(models.UserPreference)
        .filter(models.UserPreference.user_id == user.id)
        .first()
    )
    if not preference:
        preference = models.UserPreference(user_id=user.id)
        db.add(preference)

    preference.favorite_genres = json.dumps(payload.favorite_genres)
    primary_favorite_movie = payload.favorite_movies[0] if payload.favorite_movies else None
    preference.favorite_movie_json = (
        json.dumps(primary_favorite_movie.model_dump()) if primary_favorite_movie else "null"
    )
    preference.preferred_decades = json.dumps(payload.preferred_decades)
    preference.preferred_languages = json.dumps(payload.preferred_languages)
    preference.preferred_countries = json.dumps(payload.preferred_countries)
    preference.watch_frequency = payload.watch_frequency
    preference.notification_preferences_json = serialize_notification_preferences(
        getattr(preference, "notification_preferences_json", None)
    )
    preference.updated_at = _utcnow()

    db.query(models.FavoriteMovie).filter(models.FavoriteMovie.user_id == user.id).delete()
    db.query(models.FavoritePerson).filter(models.FavoritePerson.user_id == user.id).delete()

    for movie in payload.favorite_movies:
        db.add(
            models.FavoriteMovie(
                user_id=user.id,
                movie_id=movie.movie_id,
                title=movie.title,
                poster_path=movie.poster_path,
                release_date=movie.release_date,
            )
        )

    for person in payload.favorite_people:
        db.add(
            models.FavoritePerson(
                user_id=user.id,
                person_id=person.person_id,
                name=person.name,
                profile_path=person.profile_path,
                role=person.role,
            )
        )

    user.onboarding_completed = True
    db.commit()
    db.refresh(user)
    return user


def get_onboarding_preferences(db: Session, user: models.User):
    user = db.merge(user)
    preference = (
        db.query(models.UserPreference)
        .filter(models.UserPreference.user_id == user.id)
        .first()
    )

    favorite_movies = (
        db.query(models.FavoriteMovie)
        .filter(models.FavoriteMovie.user_id == user.id)
        .order_by(models.FavoriteMovie.created_at.asc(), models.FavoriteMovie.id.asc())
        .all()
    )
    favorite_people = (
        db.query(models.FavoritePerson)
        .filter(models.FavoritePerson.user_id == user.id)
        .order_by(models.FavoritePerson.created_at.asc(), models.FavoritePerson.id.asc())
        .all()
    )

    return schemas.OnboardingState(
        favorite_genres=json.loads(preference.favorite_genres) if preference else [],
        favorite_movies=[
            schemas.FavoriteMovieInput(
                movie_id=movie.movie_id,
                title=movie.title,
                poster_path=movie.poster_path,
                release_date=movie.release_date,
            )
            for movie in favorite_movies
        ],
        preferred_decades=json.loads(preference.preferred_decades) if preference else [],
        preferred_languages=json.loads(preference.preferred_languages) if preference else [],
        preferred_countries=json.loads(preference.preferred_countries) if preference else [],
        favorite_people=[
            schemas.FavoritePersonInput(
                person_id=person.person_id,
                name=person.name,
                profile_path=person.profile_path,
                role=person.role,
            )
            for person in favorite_people
        ],
        watch_frequency=preference.watch_frequency if preference else "",
    )


def _build_movie_list_summary(db: Session, movie_list: models.MovieList):
    owner = db.query(models.User).filter(models.User.id == movie_list.user_id).first()
    items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == movie_list.id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.added_at.asc())
        .all()
    )

    def find_movie_poster(movie_id: int):
        list_item = (
            db.query(models.MovieListItem.poster_path)
            .filter(
                models.MovieListItem.list_id == movie_list.id,
                models.MovieListItem.movie_id == movie_id,
                models.MovieListItem.poster_path.isnot(None),
            )
            .order_by(models.MovieListItem.position.asc(), models.MovieListItem.id.asc())
            .first()
        )
        if list_item and list_item[0]:
            return list_item[0]

        related_poster = (
            db.query(models.RatedMovie.poster_path)
            .filter(
                models.RatedMovie.user_id == movie_list.user_id,
                models.RatedMovie.movie_id == movie_id,
                models.RatedMovie.poster_path.isnot(None),
            )
            .first()
        )
        if not related_poster:
            related_poster = (
                db.query(models.FavoriteMovie.poster_path)
                .filter(
                    models.FavoriteMovie.user_id == movie_list.user_id,
                    models.FavoriteMovie.movie_id == movie_id,
                    models.FavoriteMovie.poster_path.isnot(None),
                )
                .first()
            )
        if not related_poster:
            related_poster = (
                db.query(models.Post.movie_poster_path)
                .filter(
                    models.Post.user_id == movie_list.user_id,
                    models.Post.movie_id == movie_id,
                    models.Post.movie_poster_path.isnot(None),
                )
                .first()
            )
        return related_poster[0] if related_poster and related_poster[0] else None

    poster_paths = []
    for item in items:
        poster_path = find_movie_poster(item.movie_id)
        if poster_path and poster_path not in poster_paths:
            poster_paths.append(poster_path)
        if len(poster_paths) >= 4:
            break

    derived_cover_image_url = movie_list.cover_image_url
    if not derived_cover_image_url and items:
        derived_cover_image_url = find_movie_poster(items[0].movie_id)

    return schemas.MovieListSummary(
        id=movie_list.id,
        title=movie_list.title,
        description=movie_list.description,
        visibility=movie_list.visibility,
        cover_image_url=derived_cover_image_url,
        slug=movie_list.slug,
        allow_social_sharing=movie_list.allow_social_sharing,
        owner_username=owner.username if owner else None,
        updated_at=movie_list.updated_at,
        item_count=len(items),
        poster_paths=poster_paths,
    )


def _build_movie_list_item_detail(db: Session, movie_list: models.MovieList, item: models.MovieListItem):
    title = item.movie_title or f"Filme #{item.movie_id}"
    poster_path = item.poster_path
    release_date = item.release_date

    return schemas.MovieListItemDetail(
        id=item.id,
        movie_id=item.movie_id,
        title=title,
        poster_path=poster_path,
        release_date=release_date,
        position=item.position,
        added_at=item.added_at,
    )


def _ensure_default_system_list(
    db: Session,
    user: models.User,
    title: str,
    description: str,
    slug_suffix: str,
):
    existing = (
        db.query(models.MovieList)
        .filter(
            models.MovieList.user_id == user.id,
            models.MovieList.title == title,
        )
        .first()
    )
    if existing:
        return existing

    movie_list = models.MovieList(
        user_id=user.id,
        title=title,
        description=description,
        visibility="PUBLIC",
        slug=f"{user.username.lower()}-{slug_suffix}",
        allow_social_sharing=False,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(movie_list)
    db.flush()
    return movie_list


def _sync_movie_between_default_lists(
    db: Session,
    user: models.User,
    movie_id: int,
    title: str,
    poster_path: str | None,
    release_date: str | None,
):
    watchlist = _ensure_default_system_list(
        db,
        user,
        DEFAULT_WATCHLIST_TITLE,
        DEFAULT_WATCHLIST_DESCRIPTION,
        "watchlist",
    )
    watched_list = _ensure_default_system_list(
        db,
        user,
        DEFAULT_WATCHED_LIST_TITLE,
        DEFAULT_WATCHED_LIST_DESCRIPTION,
        "watched",
    )

    watchlist_items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == watchlist.id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.id.asc())
        .all()
    )
    watched_items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == watched_list.id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.id.asc())
        .all()
    )

    normalized_identity = normalize_movie_identity(title, release_date)
    normalized_visual = normalize_movie_visual_identity(poster_path, release_date)

    items_to_remove = []
    for item in watchlist_items:
        same_movie_id = item.movie_id == movie_id
        same_identity = normalize_movie_identity(item.movie_title, item.release_date) == normalized_identity
        same_visual = normalized_visual[0] and (
            normalize_movie_visual_identity(item.poster_path, item.release_date) == normalized_visual
        )
        if same_movie_id or same_identity or same_visual:
            items_to_remove.append(item)

    for item in items_to_remove:
        db.delete(item)

    watched_has_movie = False
    for item in watched_items:
        same_movie_id = item.movie_id == movie_id
        same_identity = normalize_movie_identity(item.movie_title, item.release_date) == normalized_identity
        same_visual = normalized_visual[0] and (
            normalize_movie_visual_identity(item.poster_path, item.release_date) == normalized_visual
        )
        if same_movie_id or same_identity or same_visual:
            watched_has_movie = True
            break

    if not watched_has_movie:
        next_position = len(watched_items) + 1
        db.add(
            models.MovieListItem(
                list_id=watched_list.id,
                movie_id=movie_id,
                movie_title=title,
                poster_path=poster_path,
                release_date=release_date,
                position=next_position,
                added_at=_utcnow(),
            )
        )

    watched_list.updated_at = _utcnow()
    if items_to_remove:
        watchlist.updated_at = _utcnow()


def get_movie_watch_status(
    db: Session,
    user: models.User,
    movie_id: int,
) -> schemas.MovieWatchStatusResponse:
    user = db.merge(user)
    watchlist = _ensure_default_system_list(
        db, user, DEFAULT_WATCHLIST_TITLE, DEFAULT_WATCHLIST_DESCRIPTION, "watchlist"
    )
    watched_list = _ensure_default_system_list(
        db, user, DEFAULT_WATCHED_LIST_TITLE, DEFAULT_WATCHED_LIST_DESCRIPTION, "watched"
    )
    in_watchlist = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == watchlist.id, models.MovieListItem.movie_id == movie_id)
        .first()
    ) is not None
    in_watched = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == watched_list.id, models.MovieListItem.movie_id == movie_id)
        .first()
    ) is not None
    rated = (
        db.query(models.RatedMovie)
        .filter(models.RatedMovie.user_id == user.id, models.RatedMovie.movie_id == movie_id)
        .first()
    )
    db.commit()
    return schemas.MovieWatchStatusResponse(
        in_watchlist=in_watchlist,
        in_watched=in_watched,
        rating=float(rated.rating) if rated else None,
    )


def mark_movie_watched(
    db: Session,
    user: models.User,
    movie_id: int,
    title: str,
    poster_path: str | None,
    release_date: str | None,
):
    user = db.merge(user)
    watched_list = _ensure_default_system_list(
        db, user, DEFAULT_WATCHED_LIST_TITLE, DEFAULT_WATCHED_LIST_DESCRIPTION, "watched"
    )
    # Remove from watchlist
    watchlist = _ensure_default_system_list(
        db, user, DEFAULT_WATCHLIST_TITLE, DEFAULT_WATCHLIST_DESCRIPTION, "watchlist"
    )
    db.query(models.MovieListItem).filter(
        models.MovieListItem.list_id == watchlist.id,
        models.MovieListItem.movie_id == movie_id,
    ).delete()

    existing = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == watched_list.id, models.MovieListItem.movie_id == movie_id)
        .first()
    )
    if not existing:
        item_count = (
            db.query(models.MovieListItem)
            .filter(models.MovieListItem.list_id == watched_list.id)
            .count()
        )
        db.add(
            models.MovieListItem(
                list_id=watched_list.id,
                movie_id=movie_id,
                movie_title=title,
                poster_path=poster_path,
                release_date=release_date,
                position=item_count + 1,
                added_at=_utcnow(),
            )
        )
        watched_list.updated_at = _utcnow()
    db.commit()
    return schemas.MovieWatchStatusResponse(in_watchlist=False, in_watched=True, rating=None)


def unmark_movie_watched(
    db: Session,
    user: models.User,
    movie_id: int,
):
    user = db.merge(user)
    watched_list = _ensure_default_system_list(
        db, user, DEFAULT_WATCHED_LIST_TITLE, DEFAULT_WATCHED_LIST_DESCRIPTION, "watched"
    )
    deleted = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == watched_list.id, models.MovieListItem.movie_id == movie_id)
        .delete()
    )
    if deleted:
        watched_list.updated_at = _utcnow()
    db.commit()
    return schemas.MovieWatchStatusResponse(in_watchlist=False, in_watched=False, rating=None)


def ensure_default_watchlist(db: Session, user: models.User):
    user = db.merge(user)
    watchlist = _ensure_default_system_list(
        db,
        user,
        DEFAULT_WATCHLIST_TITLE,
        DEFAULT_WATCHLIST_DESCRIPTION,
        "watchlist",
    )
    _ensure_default_system_list(
        db,
        user,
        DEFAULT_WATCHED_LIST_TITLE,
        DEFAULT_WATCHED_LIST_DESCRIPTION,
        "watched",
    )
    user.default_watchlist_created = True
    db.commit()
    db.refresh(watchlist)
    return watchlist


def ensure_default_watchlists_for_all_users(db: Session):
    users = db.query(models.User).all()
    for user in users:
        ensure_default_watchlist(db, user)


def backfill_public_default_watchlists(db: Session):
    rows = (
        db.query(models.MovieList)
        .filter(
            models.MovieList.title.in_(
                [DEFAULT_WATCHLIST_TITLE, DEFAULT_WATCHED_LIST_TITLE, "Watchlist"]
            )
        )
        .all()
    )
    changed = False
    for movie_list in rows:
        if movie_list.visibility != "PUBLIC":
            movie_list.visibility = "PUBLIC"
            movie_list.updated_at = _utcnow()
            changed = True
    if changed:
        db.commit()


def dedupe_movie_list_items(db: Session):
    all_items = (
        db.query(models.MovieListItem)
        .order_by(
            models.MovieListItem.list_id.asc(),
            models.MovieListItem.position.asc(),
            models.MovieListItem.added_at.asc(),
            models.MovieListItem.id.asc(),
        )
        .all()
    )

    duplicates_to_delete: list[models.MovieListItem] = []
    changed = False
    items_by_list: dict[int, list[models.MovieListItem]] = {}

    for item in all_items:
        items_by_list.setdefault(item.list_id, []).append(item)

    for items in items_by_list.values():
        seen_movie_ids: set[int] = set()
        identity_groups: dict[tuple[str, str], list[models.MovieListItem]] = {}
        visual_identity_groups: dict[tuple[str, str], list[models.MovieListItem]] = {}
        release_date_groups: dict[str, list[models.MovieListItem]] = {}

        for item in items:
            if item.movie_id in seen_movie_ids:
                duplicates_to_delete.append(item)
                changed = True
                continue
            seen_movie_ids.add(item.movie_id)
            identity_groups.setdefault(
                normalize_movie_identity(item.movie_title, item.release_date), []
            ).append(item)
            if item.poster_path:
                visual_identity_groups.setdefault(
                    normalize_movie_visual_identity(item.poster_path, item.release_date), []
                ).append(item)
            if item.release_date:
                release_date_groups.setdefault(item.release_date, []).append(item)

        for grouped_items in identity_groups.values():
            if len(grouped_items) < 2:
                continue
            keep_item = max(grouped_items, key=score_movie_list_item)
            for item in grouped_items:
                if item.id == keep_item.id:
                    continue
                duplicates_to_delete.append(item)
                changed = True

        for grouped_items in visual_identity_groups.values():
            if len(grouped_items) < 2:
                continue
            keep_item = max(grouped_items, key=score_movie_list_item)
            for item in grouped_items:
                if item.id == keep_item.id:
                    continue
                duplicates_to_delete.append(item)
                changed = True

        for grouped_items in release_date_groups.values():
            if len(grouped_items) < 2:
                continue
            has_canonical_item = any((item.movie_id or 0) > 1000 for item in grouped_items)
            if not has_canonical_item:
                continue
            for item in grouped_items:
                if (item.movie_id or 0) > 1000:
                    continue
                duplicates_to_delete.append(item)
                changed = True

        unique_items = [item for item in items if item not in duplicates_to_delete]
        for index, item in enumerate(unique_items, start=1):
            if item.position != index:
                item.position = index
                changed = True

    if not changed:
        return

    seen_delete_ids: set[int] = set()
    for item in duplicates_to_delete:
        if item.id in seen_delete_ids:
            continue
        seen_delete_ids.add(item.id)
        db.delete(item)

    db.commit()


def get_my_lists(db: Session, current_user: models.User):
    current_user = db.merge(current_user)
    rows = (
        db.query(models.MovieList)
        .filter(models.MovieList.user_id == current_user.id)
        .order_by(models.MovieList.updated_at.desc(), models.MovieList.id.desc())
        .all()
    )
    return [_build_movie_list_summary(db, movie_list) for movie_list in rows]


def get_public_lists_for_user(db: Session, user_id: int):
    rows = (
        db.query(models.MovieList)
        .filter(
            models.MovieList.user_id == user_id,
            models.MovieList.visibility == "PUBLIC",
        )
        .order_by(models.MovieList.updated_at.desc(), models.MovieList.id.desc())
        .all()
    )
    return [_build_movie_list_summary(db, movie_list) for movie_list in rows]


def get_movie_list_detail(db: Session, current_user: models.User, list_id: int):
    current_user = db.merge(current_user)
    movie_list = (
        db.query(models.MovieList)
        .filter(models.MovieList.id == list_id)
        .first()
    )
    if not movie_list:
        raise LookupError("List not found")
    if movie_list.user_id != current_user.id:
        raise LookupError("List not found")

    items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == movie_list.id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.added_at.asc())
        .all()
    )
    owner = db.query(models.User).filter(models.User.id == movie_list.user_id).first()
    summary = _build_movie_list_summary(db, movie_list)
    return schemas.MovieListDetail(
        id=movie_list.id,
        title=movie_list.title,
        description=movie_list.description,
        visibility=movie_list.visibility,
        cover_image_url=summary.cover_image_url,
        slug=movie_list.slug,
        allow_social_sharing=movie_list.allow_social_sharing,
        owner_username=owner.username if owner else None,
        can_edit=True,
        created_at=movie_list.created_at,
        updated_at=movie_list.updated_at,
        item_count=len(items),
        items=[_build_movie_list_item_detail(db, movie_list, item) for item in items],
    )


def get_accessible_movie_list_detail(
    db: Session,
    current_user: models.User,
    list_id: int,
    owner_username: str | None = None,
):
    current_user = db.merge(current_user)
    movie_list = db.query(models.MovieList).filter(models.MovieList.id == list_id).first()
    if not movie_list:
        raise LookupError("List not found")
    owner = db.query(models.User).filter(models.User.id == movie_list.user_id).first()
    if owner_username and (not owner or owner.username.lower() != owner_username.lower()):
        raise LookupError("List not found")

    is_owner = movie_list.user_id == current_user.id
    if not is_owner and movie_list.visibility != "PUBLIC":
        raise LookupError("List not found")

    items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == movie_list.id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.added_at.asc())
        .all()
    )
    summary = _build_movie_list_summary(db, movie_list)
    return schemas.MovieListDetail(
        id=movie_list.id,
        title=movie_list.title,
        description=movie_list.description,
        visibility=movie_list.visibility,
        cover_image_url=summary.cover_image_url,
        slug=movie_list.slug,
        allow_social_sharing=movie_list.allow_social_sharing,
        owner_username=owner.username if owner else None,
        can_edit=is_owner,
        created_at=movie_list.created_at,
        updated_at=movie_list.updated_at,
        item_count=len(items),
        items=[_build_movie_list_item_detail(db, movie_list, item) for item in items],
    )


def _slugify_list_title(title: str):
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "lista"


def _generate_unique_list_slug(db: Session, title: str, exclude_list_id: int | None = None):
    base_slug = _slugify_list_title(title)
    candidate = base_slug
    counter = 2
    while True:
        query = db.query(models.MovieList).filter(models.MovieList.slug == candidate)
        if exclude_list_id is not None:
            query = query.filter(models.MovieList.id != exclude_list_id)
        if not query.first():
            return candidate
        candidate = f"{base_slug}-{counter}"
        counter += 1


def _validate_list_payload(title: str, description: str | None, visibility: str):
    normalized_title = (title or "").strip()
    if not normalized_title:
        raise ValueError("Title is required")

    normalized_description = (description or "").strip()
    if len(normalized_description) > LIST_DESCRIPTION_LIMIT:
        raise ValueError(f"Description cannot exceed {LIST_DESCRIPTION_LIMIT} characters")

    normalized_visibility = (visibility or "").strip().upper()
    if normalized_visibility not in VALID_LIST_VISIBILITIES:
        raise ValueError("Visibility must be PUBLIC or PRIVATE")

    return normalized_title, normalized_description or None, normalized_visibility


def create_movie_list(db: Session, current_user: models.User, payload: schemas.MovieListCreate):
    current_user = db.merge(current_user)
    title, description, visibility = _validate_list_payload(
        payload.title,
        payload.description,
        payload.visibility,
    )
    movie_list = models.MovieList(
        user_id=current_user.id,
        title=title,
        description=description,
        visibility=visibility,
        slug=_generate_unique_list_slug(db, title),
        allow_social_sharing=payload.allow_social_sharing,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(movie_list)
    db.commit()
    db.refresh(movie_list)
    return _build_movie_list_summary(db, movie_list)


def update_movie_list(db: Session, current_user: models.User, list_id: int, payload: schemas.MovieListUpdate):
    current_user = db.merge(current_user)
    movie_list = db.query(models.MovieList).filter(models.MovieList.id == list_id).first()
    if not movie_list:
        raise LookupError("List not found")
    if movie_list.user_id != current_user.id:
        raise ValueError("You do not have permission to edit this list")

    title, description, visibility = _validate_list_payload(
        payload.title,
        payload.description,
        payload.visibility,
    )
    movie_list.title = title
    movie_list.description = description
    movie_list.visibility = visibility
    movie_list.allow_social_sharing = payload.allow_social_sharing
    movie_list.slug = _generate_unique_list_slug(db, title, exclude_list_id=movie_list.id)
    movie_list.updated_at = _utcnow()
    db.commit()
    db.refresh(movie_list)
    return _build_movie_list_summary(db, movie_list)


def add_movies_to_list(db: Session, current_user: models.User, list_id: int, movies: list[schemas.MovieListItemCreate]):
    current_user = db.merge(current_user)
    movie_list = db.query(models.MovieList).filter(models.MovieList.id == list_id).first()
    if not movie_list:
        raise LookupError("List not found")
    if movie_list.user_id != current_user.id:
        raise ValueError("You do not have permission to edit this list")
    if not movies:
        raise ValueError("No movies selected")

    existing_items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == movie_list.id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.id.asc())
        .all()
    )
    existing_movie_ids = {item.movie_id for item in existing_items}
    existing_movie_keys = {
        normalize_movie_identity(item.movie_title, item.release_date) for item in existing_items
    }
    existing_visual_keys = {
        normalize_movie_visual_identity(item.poster_path, item.release_date) for item in existing_items
    }
    next_position = len(existing_items) + 1
    changed = False

    for movie in movies:
        movie_key = normalize_movie_identity(movie.title, movie.release_date)
        movie_visual_key = normalize_movie_visual_identity(movie.poster_path, movie.release_date)
        if (
            movie.movie_id in existing_movie_ids
            or movie_key in existing_movie_keys
            or (movie_visual_key[0] and movie_visual_key in existing_visual_keys)
        ):
            continue
        db.add(
            models.MovieListItem(
                list_id=movie_list.id,
                movie_id=movie.movie_id,
                movie_title=movie.title,
                poster_path=movie.poster_path,
                release_date=movie.release_date,
                position=next_position,
                added_at=_utcnow(),
            )
        )
        existing_movie_ids.add(movie.movie_id)
        existing_movie_keys.add(movie_key)
        if movie_visual_key[0]:
            existing_visual_keys.add(movie_visual_key)
        next_position += 1
        changed = True

    if not changed:
        raise ValueError("All selected movies are already in the list")

    movie_list.updated_at = _utcnow()
    db.commit()
    return get_movie_list_detail(db, current_user, list_id)


def remove_movie_from_list(db: Session, current_user: models.User, list_id: int, item_id: int):
    current_user = db.merge(current_user)
    movie_list = db.query(models.MovieList).filter(models.MovieList.id == list_id).first()
    if not movie_list:
        raise LookupError("List not found")
    if movie_list.user_id != current_user.id:
        raise ValueError("You do not have permission to edit this list")

    item = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == list_id, models.MovieListItem.id == item_id)
        .first()
    )
    if not item:
        raise LookupError("Movie not found in this list")

    db.delete(item)
    db.flush()
    remaining_items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == list_id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.added_at.asc(), models.MovieListItem.id.asc())
        .all()
    )
    for index, remaining in enumerate(remaining_items, start=1):
        remaining.position = index

    movie_list.updated_at = _utcnow()
    db.commit()
    return {"message": "Movie removed from list successfully"}


def reorder_movie_list_items(
    db: Session,
    current_user: models.User,
    list_id: int,
    item_ids: list[int],
):
    current_user = db.merge(current_user)
    movie_list = db.query(models.MovieList).filter(models.MovieList.id == list_id).first()
    if not movie_list:
        raise LookupError("List not found")
    if movie_list.user_id != current_user.id:
        raise ValueError("You do not have permission to edit this list")

    items = (
        db.query(models.MovieListItem)
        .filter(models.MovieListItem.list_id == list_id)
        .order_by(models.MovieListItem.position.asc(), models.MovieListItem.added_at.asc(), models.MovieListItem.id.asc())
        .all()
    )
    if not items:
        raise ValueError("This list has no movies to reorder")

    current_ids = [item.id for item in items]
    if len(item_ids) != len(current_ids) or set(item_ids) != set(current_ids):
        raise ValueError("The reorder payload must include all movies in the list exactly once")

    items_by_id = {item.id: item for item in items}
    for index, item_id in enumerate(item_ids, start=1):
        items_by_id[item_id].position = index

    movie_list.updated_at = _utcnow()
    db.commit()
    return get_movie_list_detail(db, current_user, list_id)


def delete_movie_list(db: Session, current_user: models.User, list_id: int):
    current_user = db.merge(current_user)
    movie_list = (
        db.query(models.MovieList)
        .filter(models.MovieList.id == list_id)
        .first()
    )
    if not movie_list:
        raise LookupError("List not found")
    if movie_list.user_id != current_user.id:
        raise ValueError("You do not have permission to delete this list")

    db.query(models.MovieListItem).filter(models.MovieListItem.list_id == movie_list.id).delete()
    db.delete(movie_list)
    db.commit()
    return {"message": "List deleted successfully"}


def _slugify_club_name(name: str):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "clube"


def _generate_unique_club_slug(db: Session, name: str):
    base_slug = _slugify_club_name(name)
    candidate = base_slug
    counter = 2
    while db.query(models.Club).filter(models.Club.slug == candidate).first():
        candidate = f"{base_slug}-{counter}"
        counter += 1
    return candidate


def _pick_club_cover(cover_image_url: str | None, name: str):
    if cover_image_url and cover_image_url.strip():
        return cover_image_url.strip()
    return DEFAULT_CLUB_COVERS[sum(ord(char) for char in name) % len(DEFAULT_CLUB_COVERS)]


def _validate_club_payload(payload: schemas.ClubCreate):
    normalized_name = (payload.name or "").strip()
    normalized_short_description = (payload.short_description or "").strip()
    normalized_description = (payload.description or "").strip() or None
    normalized_cover_image_url = (payload.cover_image_url or "").strip()
    normalized_genres = [
        genre.strip()
        for genre in (payload.genres or [])
        if genre and genre.strip()
    ]

    if not normalized_name:
        raise ValueError("Club name is required")
    if len(normalized_name) > CLUB_NAME_LIMIT:
        raise ValueError(f"Club name cannot exceed {CLUB_NAME_LIMIT} characters")
    if not normalized_short_description:
        raise ValueError("Short description is required")
    if len(normalized_short_description) > CLUB_SHORT_DESCRIPTION_LIMIT:
        raise ValueError(
            f"Short description cannot exceed {CLUB_SHORT_DESCRIPTION_LIMIT} characters"
        )
    if normalized_description and len(normalized_description) > CLUB_DESCRIPTION_LIMIT:
        raise ValueError(f"Description cannot exceed {CLUB_DESCRIPTION_LIMIT} characters")
    if not normalized_genres:
        raise ValueError("Choose at least one genre for the club")
    if not normalized_cover_image_url:
        raise ValueError("Choose a movie cover before creating the club")

    return (
        normalized_name,
        normalized_short_description,
        normalized_description,
        normalized_cover_image_url,
        normalized_genres,
    )


def _get_club_member_count(db: Session, club_id: int):
    return db.query(models.ClubMembership).filter(models.ClubMembership.club_id == club_id).count()


def _build_club_summary(db: Session, club: models.Club, current_user_id: int | None = None):
    creator = get_user_by_id(db, club.creator_user_id)
    joined_by_me = False
    pending_request_by_me = False
    if current_user_id is not None:
        joined_by_me = (
            db.query(models.ClubMembership)
            .filter(
                models.ClubMembership.club_id == club.id,
                models.ClubMembership.user_id == current_user_id,
            )
            .first()
            is not None
        )
        pending_request_by_me = (
            db.query(models.ClubJoinRequest)
            .filter(
                models.ClubJoinRequest.club_id == club.id,
                models.ClubJoinRequest.user_id == current_user_id,
                models.ClubJoinRequest.status == "pending",
            )
            .first()
            is not None
        )

    return schemas.ClubSummary(
        id=club.id,
        name=club.name,
        slug=club.slug,
        short_description=club.short_description,
        description=club.description,
        genres=json.loads(club.genres_json or "[]"),
        cover_image_url=club.cover_image_url,
        is_private=club.is_private,
        member_count=_get_club_member_count(db, club.id),
        creator=schemas.ClubCreatorSummary(
            id=creator.id,
            username=creator.username,
            profile_image_url=creator.profile_image_url,
        ) if creator else schemas.ClubCreatorSummary(id=0, username="desconhecido"),
        joined_by_me=joined_by_me,
        pending_request_by_me=pending_request_by_me,
        created_at=club.created_at,
    )


def create_club(db: Session, current_user: models.User, payload: schemas.ClubCreate):
    current_user = db.merge(current_user)
    name, short_description, description, cover_image_url, genres = _validate_club_payload(payload)
    existing = db.query(models.Club).filter(models.Club.name.ilike(name)).first()
    if existing:
        raise ValueError("A club with this name already exists")

    now = _utcnow()
    club = models.Club(
        creator_user_id=current_user.id,
        name=name,
        slug=_generate_unique_club_slug(db, name),
        short_description=short_description,
        description=description,
        genres_json=json.dumps(genres),
        cover_image_url=cover_image_url,
        is_private=payload.is_private,
        created_at=now,
        updated_at=now,
    )
    db.add(club)
    db.flush()
    db.add(
        models.ClubMembership(
            club_id=club.id,
            user_id=current_user.id,
            role="owner",
            created_at=now,
        )
    )
    db.commit()
    db.refresh(club)
    return _build_club_summary(db, club, current_user.id)


def get_club_hub(
    db: Session,
    current_user: models.User,
    query: str | None = None,
    genres: list[str] | None = None,
    limit: int = 24,
):
    ensure_demo_clubs(db)
    current_user = db.merge(current_user)
    query_value = (query or "").strip()
    normalized_genres = [genre.strip() for genre in (genres or []) if genre and genre.strip()]
    safe_limit = max(1, min(limit, 48))

    my_club_rows = (
        db.query(models.Club)
        .join(models.ClubMembership, models.ClubMembership.club_id == models.Club.id)
        .filter(models.ClubMembership.user_id == current_user.id)
        .order_by(models.Club.updated_at.desc(), models.Club.id.desc())
        .all()
    )

    discover_query = db.query(models.Club).order_by(models.Club.updated_at.desc(), models.Club.id.desc())
    if query_value:
        like_pattern = f"%{query_value}%"
        discover_query = discover_query.filter(
            or_(
                models.Club.name.ilike(like_pattern),
                models.Club.short_description.ilike(like_pattern),
            )
        )
    if normalized_genres:
        discover_query = discover_query.filter(
            or_(*[models.Club.genres_json.ilike(f'%"{genre}"%') for genre in normalized_genres])
        )
    discover_rows = discover_query.limit(safe_limit).all()

    joined_club_ids = {club.id for club in my_club_rows}
    discover_rows = [club for club in discover_rows if club.id not in joined_club_ids]

    return schemas.ClubSearchResponse(
        my_clubs=[_build_club_summary(db, club, current_user.id) for club in my_club_rows],
        discover_clubs=[_build_club_summary(db, club, current_user.id) for club in discover_rows],
    )


def join_club(db: Session, current_user: models.User, club_id: int):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    membership = (
        db.query(models.ClubMembership)
        .filter(
            models.ClubMembership.club_id == club.id,
            models.ClubMembership.user_id == current_user.id,
        )
        .first()
    )
    if membership:
        raise ValueError("You are already in this club")

    pending_request = (
        db.query(models.ClubJoinRequest)
        .filter(
            models.ClubJoinRequest.club_id == club.id,
            models.ClubJoinRequest.user_id == current_user.id,
            models.ClubJoinRequest.status == "pending",
        )
        .first()
    )

    if club.is_private:
        if pending_request:
            raise ValueError("Your request is already pending")
        now = _utcnow()
        db.add(
            models.ClubJoinRequest(
                club_id=club.id,
                user_id=current_user.id,
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
        _create_notification(
            db,
            recipient_user_id=club.creator_user_id,
            actor_user=current_user,
            notification_type="club_join_request",
            message=f"@{current_user.username} pediu para entrar em {club.name}.",
            club_id=club.id,
        )
        club.updated_at = now
        db.commit()
        db.refresh(club)
        return schemas.ClubJoinResponse(
            message="Request sent successfully",
            joined=False,
            request_pending=True,
            club=_build_club_summary(db, club, current_user.id),
        )

    now = _utcnow()
    db.add(
        models.ClubMembership(
            club_id=club.id,
            user_id=current_user.id,
            role="member",
            created_at=now,
        )
    )
    if pending_request:
        db.delete(pending_request)
    club.updated_at = now
    db.commit()
    db.refresh(club)
    return schemas.ClubJoinResponse(
        message="Joined club successfully",
        joined=True,
        request_pending=False,
        club=_build_club_summary(db, club, current_user.id),
    )


def leave_club(db: Session, current_user: models.User, club_id: int):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    membership = (
        db.query(models.ClubMembership)
        .filter(
            models.ClubMembership.club_id == club.id,
            models.ClubMembership.user_id == current_user.id,
        )
        .first()
    )
    if not membership:
        raise ValueError("You are not a member of this club")
    if membership.role == "owner":
        raise ValueError("The club creator cannot leave right now")

    pending_request = (
        db.query(models.ClubJoinRequest)
        .filter(
            models.ClubJoinRequest.club_id == club.id,
            models.ClubJoinRequest.user_id == current_user.id,
            models.ClubJoinRequest.status == "pending",
        )
        .first()
    )
    if pending_request:
        db.delete(pending_request)

    db.delete(membership)
    club.updated_at = _utcnow()
    db.commit()
    return {"message": "Left club successfully"}


def cancel_club_join_request(db: Session, current_user: models.User, club_id: int):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    pending_request = (
        db.query(models.ClubJoinRequest)
        .filter(
            models.ClubJoinRequest.club_id == club.id,
            models.ClubJoinRequest.user_id == current_user.id,
            models.ClubJoinRequest.status == "pending",
        )
        .first()
    )
    if not pending_request:
        raise LookupError("Pending request not found")

    db.delete(pending_request)
    club.updated_at = _utcnow()
    db.commit()
    return {"message": "Join request cancelled successfully"}


def _get_club_membership(db: Session, club_id: int, user_id: int):
    return (
        db.query(models.ClubMembership)
        .filter(
            models.ClubMembership.club_id == club_id,
            models.ClubMembership.user_id == user_id,
        )
        .first()
    )


def _user_can_manage_club(club: models.Club, membership: models.ClubMembership | None):
    return bool(club and membership and membership.role in {"owner", "admin"})


def _build_club_members(db: Session, club_id: int):
    rows = (
        db.query(models.ClubMembership, models.User)
        .join(models.User, models.User.id == models.ClubMembership.user_id)
        .filter(models.ClubMembership.club_id == club_id)
        .order_by(models.ClubMembership.role.desc(), models.User.username.asc())
        .all()
    )
    return [
        schemas.ClubMemberSummary(
            id=user.id,
            username=user.username,
            profile_image_url=user.profile_image_url,
            role=membership.role,
            joined_at=membership.created_at,
        )
        for membership, user in rows
    ]


def _build_club_pending_requests(db: Session, club_id: int):
    rows = (
        db.query(models.ClubJoinRequest, models.User)
        .join(models.User, models.User.id == models.ClubJoinRequest.user_id)
        .filter(
            models.ClubJoinRequest.club_id == club_id,
            models.ClubJoinRequest.status == "pending",
        )
        .order_by(models.ClubJoinRequest.created_at.asc(), models.ClubJoinRequest.id.asc())
        .all()
    )
    return [
        schemas.ClubJoinRequestSummary(
            id=join_request.id,
            created_at=join_request.created_at,
            requester=schemas.ClubCreatorSummary(
                id=user.id,
                username=user.username,
                profile_image_url=user.profile_image_url,
            ),
        )
        for join_request, user in rows
    ]


def _get_club_activity_totals(db: Session, club_id: int, user_id: int):
    post_ids_query = db.query(models.Post.id).filter(models.Post.club_id == club_id)
    post_ids = [row.id for row in post_ids_query.all()]
    like_count = 0
    if post_ids:
        like_count = (
            db.query(models.Like)
            .filter(models.Like.post_id.in_(post_ids), models.Like.user_id == user_id)
            .count()
        )
    post_count = (
        db.query(models.Post)
        .filter(models.Post.club_id == club_id, models.Post.user_id == user_id)
        .count()
    )
    comment_count = (
        db.query(models.Comment)
        .join(models.Post, models.Post.id == models.Comment.post_id)
        .filter(models.Post.club_id == club_id, models.Comment.user_id == user_id)
        .count()
    )
    return {"posts": post_count, "comments": comment_count, "likes": like_count}


CLUB_CHALLENGE_TEMPLATES = [
    {"id": "first-post", "title": "Primeira analise do clube", "description": "Publique 1 post dentro do clube.", "badge_name": "Voz ativa", "metric": "posts", "target": 1},
    {"id": "storyteller", "title": "Contador de historias", "description": "Publique 5 posts dentro do clube.", "badge_name": "Narrador", "metric": "posts", "target": 5},
    {"id": "comment-run", "title": "Conversa em movimento", "description": "Comente em 3 posts do clube.", "badge_name": "Debatedor", "metric": "comments", "target": 3},
    {"id": "super-commenter", "title": "Colunista do clube", "description": "Faca 10 comentarios nas discussoes do clube.", "badge_name": "Colunista", "metric": "comments", "target": 10},
    {"id": "community-support", "title": "Curadoria coletiva", "description": "Curta 5 posts do clube.", "badge_name": "Curador social", "metric": "likes", "target": 5},
    {"id": "mega-curator", "title": "Critico popular", "description": "Curta 20 posts do clube.", "badge_name": "Critico popular", "metric": "likes", "target": 20},
]


def _build_club_challenges(db: Session, club: models.Club, current_user_id: int | None = None):
    member_ids = [
        row.user_id
        for row in db.query(models.ClubMembership.user_id)
        .filter(models.ClubMembership.club_id == club.id)
        .all()
    ]

    # Batch all activity in 3 queries instead of N*3
    post_count_rows = (
        db.query(models.Post.user_id, func.count(models.Post.id))
        .filter(models.Post.club_id == club.id, models.Post.user_id.in_(member_ids))
        .group_by(models.Post.user_id)
        .all()
    ) if member_ids else []
    comment_count_rows = (
        db.query(models.Comment.user_id, func.count(models.Comment.id))
        .join(models.Post, models.Post.id == models.Comment.post_id)
        .filter(models.Post.club_id == club.id, models.Comment.user_id.in_(member_ids))
        .group_by(models.Comment.user_id)
        .all()
    ) if member_ids else []
    club_post_ids = [
        row.id for row in db.query(models.Post.id).filter(models.Post.club_id == club.id).all()
    ]
    like_count_rows = (
        db.query(models.Like.user_id, func.count(models.Like.id))
        .filter(models.Like.post_id.in_(club_post_ids), models.Like.user_id.in_(member_ids))
        .group_by(models.Like.user_id)
        .all()
    ) if (club_post_ids and member_ids) else []

    post_counts = dict(post_count_rows)
    comment_counts = dict(comment_count_rows)
    like_counts = dict(like_count_rows)

    def activity_for(uid: int | None):
        if uid is None:
            return {"posts": 0, "comments": 0, "likes": 0}
        return {
            "posts": post_counts.get(uid, 0),
            "comments": comment_counts.get(uid, 0),
            "likes": like_counts.get(uid, 0),
        }

    all_activities = [activity_for(mid) for mid in member_ids]
    current_activity = activity_for(current_user_id)

    items = []
    for template in CLUB_CHALLENGE_TEMPLATES:
        participant_count = sum(
            1 for act in all_activities if act.get(template["metric"], 0) >= template["target"]
        )
        progress_value = current_activity.get(template["metric"], 0)
        items.append(
            schemas.ClubChallengeProgress(
                id=template["id"],
                title=template["title"],
                description=template["description"],
                badge_name=template["badge_name"],
                target_value=template["target"],
                progress_value=progress_value,
                completed=progress_value >= template["target"],
                participant_count=participant_count,
            )
        )
    return items


def _build_club_ranking(db: Session, club: models.Club):
    members = _build_club_members(db, club.id)
    if not members:
        return []

    member_ids = [m.id for m in members]

    # Batch: post counts per member in one query
    post_count_rows = (
        db.query(models.Post.user_id, func.count(models.Post.id))
        .filter(models.Post.club_id == club.id, models.Post.user_id.in_(member_ids))
        .group_by(models.Post.user_id)
        .all()
    )
    post_counts = dict(post_count_rows)

    # Batch: comment counts per member on club posts
    comment_count_rows = (
        db.query(models.Comment.user_id, func.count(models.Comment.id))
        .join(models.Post, models.Post.id == models.Comment.post_id)
        .filter(models.Post.club_id == club.id, models.Comment.user_id.in_(member_ids))
        .group_by(models.Comment.user_id)
        .all()
    )
    comment_counts = dict(comment_count_rows)

    # Batch: like counts per member on club posts
    club_post_ids = [
        row.id for row in db.query(models.Post.id).filter(models.Post.club_id == club.id).all()
    ]
    like_counts: dict[int, int] = {}
    if club_post_ids:
        like_count_rows = (
            db.query(models.Like.user_id, func.count(models.Like.id))
            .filter(models.Like.post_id.in_(club_post_ids), models.Like.user_id.in_(member_ids))
            .group_by(models.Like.user_id)
            .all()
        )
        like_counts = dict(like_count_rows)

    # Challenge thresholds (same templates as _build_club_challenges)
    challenge_thresholds = [
        ("posts", 1), ("comments", 3), ("likes", 5),
        ("posts", 5), ("comments", 10), ("likes", 20),
    ]

    ranking = []
    for member in members:
        posts = post_counts.get(member.id, 0)
        comments = comment_counts.get(member.id, 0)
        likes = like_counts.get(member.id, 0)
        activity = {"posts": posts, "comments": comments, "likes": likes}
        badge_count = sum(
            1 for metric, target in challenge_thresholds if activity.get(metric, 0) >= target
        )
        score = posts * 3 + comments * 2 + likes
        ranking.append({"member": member, "badge_count": badge_count, "score": score})

    ranking.sort(
        key=lambda item: (
            -item["badge_count"],
            -item["score"],
            item["member"].username.lower(),
        )
    )

    return [
        schemas.ClubRankingEntry(
            rank=index + 1,
            user=schemas.ClubCreatorSummary(
                id=item["member"].id,
                username=item["member"].username,
                profile_image_url=item["member"].profile_image_url,
            ),
            role=item["member"].role,
            badge_count=item["badge_count"],
            score=item["score"],
        )
        for index, item in enumerate(ranking, start=1)
    ]


def _fetch_club_posts_page(
    db: Session,
    club: models.Club,
    current_user_id: int,
    followed_ids: set,
    cursor: str | None = None,
    limit: int = 20,
):
    PAGE = limit
    query = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(models.Post.club_id == club.id)
        .order_by(models.Post.created_at.desc(), models.Post.id.desc())
    )
    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.filter(models.Post.id < cursor_id)
        except ValueError:
            pass
    rows = query.limit(PAGE + 1).all()
    has_more = len(rows) > PAGE
    rows = rows[:PAGE]
    items = [
        _build_feed_post(db, post, author, current_user_id, followed_ids)
        for post, author in rows
    ]
    next_cursor = str(rows[-1][0].id) if has_more and rows else None
    return items, next_cursor


def get_club_detail(db: Session, current_user: models.User, club_id: int):
    ensure_demo_clubs(db)
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    membership = _get_club_membership(db, club.id, current_user.id)
    is_member = membership is not None
    can_manage = _user_can_manage_club(club, membership)
    can_view_full_content = is_member or not club.is_private

    followed_ids = _get_followed_ids(db, current_user.id)
    posts, posts_next_cursor = [], None
    if can_view_full_content:
        posts, posts_next_cursor = _fetch_club_posts_page(db, club, current_user.id, followed_ids)

    visible_members = _build_club_members(db, club.id)
    if club.is_private and not is_member:
        visible_members = [member for member in visible_members if member.role == "owner"][:1]

    return schemas.ClubDetailResponse(
        club=_build_club_summary(db, club, current_user.id),
        is_member=is_member,
        can_view_full_content=can_view_full_content,
        can_manage=can_manage,
        current_user_role=membership.role if membership else None,
        members=visible_members,
        pending_requests=_build_club_pending_requests(db, club.id) if can_manage else [],
        challenges=_build_club_challenges(db, club, current_user.id if is_member else None) if can_view_full_content else [],
        ranking=_build_club_ranking(db, club) if can_view_full_content else [],
        posts=posts,
        posts_next_cursor=posts_next_cursor,
    )


def get_club_posts_page(
    db: Session,
    current_user: models.User,
    club_id: int,
    cursor: str | None = None,
    limit: int = 20,
):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")
    membership = _get_club_membership(db, club.id, current_user.id)
    can_view = membership is not None or not club.is_private
    if not can_view:
        raise ValueError("You cannot view this private club's posts")
    followed_ids = _get_followed_ids(db, current_user.id)
    items, next_cursor = _fetch_club_posts_page(db, club, current_user.id, followed_ids, cursor, limit)
    return schemas.FeedResponse(items=items, next_cursor=next_cursor)


def create_club_post(db: Session, current_user: models.User, club_id: int, payload: schemas.FeedPostCreate):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")
    membership = _get_club_membership(db, club_id, current_user.id)
    if not membership:
        raise ValueError("You must join the club before posting")

    text_content = (payload.text_content or "").strip()
    if not text_content:
        raise ValueError("Post cannot be empty")
    if len(text_content) > POST_TEXT_LIMIT:
        raise ValueError(f"Post cannot exceed {POST_TEXT_LIMIT} characters")

    post = models.Post(
        user_id=current_user.id,
        club_id=club_id,
        post_type="commentary",
        text_content=text_content,
        sentiment=(payload.sentiment or "").strip() or None,
        contains_spoiler=bool(payload.contains_spoiler),
        would_watch_again=payload.would_watch_again,
        tags_json=json.dumps(_extract_hashtags(text_content)),
        movie_id=payload.movie_id,
        movie_title=payload.movie_title,
        movie_poster_path=payload.movie_poster_path,
        movie_release_date=payload.movie_release_date,
        created_at=_utcnow(),
    )
    db.add(post)
    db.flush()

    _create_mention_notifications(
        db,
        actor_user=current_user,
        content=text_content,
        notification_type="club_post_mention",
        message_builder=lambda _user: f"@{current_user.username} te marcou em um post do clube.",
        post_id=post.id,
        club_id=club.id,
        movie_id=post.movie_id,
    )

    # Notify all club members (owner + admins get notified; regular members gated by club_updates pref)
    member_ids = [
        row.user_id
        for row in db.query(models.ClubMembership.user_id)
        .filter(models.ClubMembership.club_id == club.id)
        .all()
    ]
    movie_suffix = f" sobre {payload.movie_title}" if payload.movie_title else ""
    for member_id in member_ids:
        _create_notification(
            db,
            recipient_user_id=member_id,
            actor_user=current_user,
            notification_type="club_new_post",
            message=f"@{current_user.username} publicou um novo post em {club.name}{movie_suffix}.",
            post_id=post.id,
            club_id=club.id,
            movie_id=post.movie_id,
        )

    db.commit()
    db.refresh(post)
    followed_ids = _get_followed_ids(db, current_user.id)
    return _build_feed_post(db, post, current_user, current_user.id, followed_ids)


def _assert_can_interact_with_club_post(db: Session, current_user: models.User, post: models.Post):
    if not post.club_id:
        return
    membership = _get_club_membership(db, post.club_id, current_user.id)
    if not membership:
        raise ValueError("You must be a member of this club")


def review_club_join_request(
    db: Session,
    current_user: models.User,
    club_id: int,
    request_id: int,
    approve: bool,
):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")
    membership = _get_club_membership(db, club_id, current_user.id)
    if not _user_can_manage_club(club, membership):
        raise ValueError("You do not have permission to manage this club")

    join_request = (
        db.query(models.ClubJoinRequest)
        .filter(
            models.ClubJoinRequest.id == request_id,
            models.ClubJoinRequest.club_id == club_id,
            models.ClubJoinRequest.status == "pending",
        )
        .first()
    )
    if not join_request:
        raise LookupError("Request not found")

    requester = get_user_by_id(db, join_request.user_id)
    now = _utcnow()
    if approve:
        if not _get_club_membership(db, club_id, join_request.user_id):
            db.add(
                models.ClubMembership(
                    club_id=club_id,
                    user_id=join_request.user_id,
                    role="member",
                    created_at=now,
                )
            )
        join_request.status = "approved"
        _create_notification(
            db,
            recipient_user_id=join_request.user_id,
            actor_user=current_user,
            notification_type="club_request_approved",
            message=f"Seu pedido para entrar em {club.name} foi aprovado.",
            club_id=club.id,
        )
    else:
        join_request.status = "rejected"
        _create_notification(
            db,
            recipient_user_id=join_request.user_id,
            actor_user=current_user,
            notification_type="club_request_rejected",
            message=f"Seu pedido para entrar em {club.name} foi recusado.",
            club_id=club.id,
        )
    join_request.updated_at = now
    club.updated_at = now
    db.commit()
    return {"message": "Request updated successfully"}


def update_club_member_role(
    db: Session,
    current_user: models.User,
    club_id: int,
    member_user_id: int,
    role: str,
):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")
    membership = _get_club_membership(db, club_id, current_user.id)
    if not _user_can_manage_club(club, membership):
        raise ValueError("You do not have permission to manage this club")
    target_membership = _get_club_membership(db, club_id, member_user_id)
    if not target_membership:
        raise LookupError("Member not found")
    if target_membership.role == "owner":
        raise ValueError("You cannot change the creator role")
    if role not in {"member", "admin"}:
        raise ValueError("Invalid role")
    target_membership.role = role
    club.updated_at = _utcnow()
    db.commit()
    return {"message": "Member role updated successfully"}


def remove_club_member(db: Session, current_user: models.User, club_id: int, member_user_id: int):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")
    membership = _get_club_membership(db, club_id, current_user.id)
    if not _user_can_manage_club(club, membership):
        raise ValueError("You do not have permission to manage this club")
    target_membership = _get_club_membership(db, club_id, member_user_id)
    if not target_membership:
        raise LookupError("Member not found")
    if target_membership.role == "owner":
        raise ValueError("You cannot remove the original creator")
    db.delete(target_membership)
    club.updated_at = _utcnow()
    db.commit()
    return {"message": "Member removed successfully"}


def remove_club_post(db: Session, current_user: models.User, club_id: int, post_id: int):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    post = (
        db.query(models.Post)
        .filter(
            models.Post.id == post_id,
            models.Post.club_id == club_id,
        )
        .first()
    )
    if not post:
        raise LookupError("Club post not found")

    membership = _get_club_membership(db, club_id, current_user.id)
    can_manage_club = _user_can_manage_club(club, membership)
    is_post_author = post.user_id == current_user.id
    if not can_manage_club and not is_post_author:
        raise ValueError("You do not have permission to remove this club post")

    review = db.query(models.Review).filter(models.Review.post_id == post.id).first()
    if review:
        db.delete(review)
    db.query(models.Like).filter(models.Like.post_id == post.id).delete()
    db.query(models.SavedPost).filter(models.SavedPost.post_id == post.id).delete()
    db.query(models.PostShare).filter(models.PostShare.post_id == post.id).delete()
    db.query(models.Comment).filter(models.Comment.post_id == post.id).delete()
    db.query(models.PostReport).filter(models.PostReport.post_id == post.id).delete()
    db.delete(post)
    club.updated_at = _utcnow()
    db.commit()
    return {"message": "Post removido do clube com sucesso"}


def report_club_post(
    db: Session,
    current_user: models.User,
    club_id: int,
    post_id: int,
    reason: str,
):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    post = (
        db.query(models.Post)
        .filter(
            models.Post.id == post_id,
            models.Post.club_id == club_id,
        )
        .first()
    )
    if not post:
        raise LookupError("Club post not found")

    membership = _get_club_membership(db, club_id, current_user.id)
    if not membership:
        raise ValueError("You must be a member to report a post")

    if post.user_id == current_user.id:
        raise ValueError("You cannot report your own post")

    existing = (
        db.query(models.PostReport)
        .filter(
            models.PostReport.post_id == post_id,
            models.PostReport.reporter_user_id == current_user.id,
        )
        .first()
    )
    if existing:
        raise ValueError("You have already reported this post")

    report = models.PostReport(
        post_id=post_id,
        reporter_user_id=current_user.id,
        club_id=club_id,
        reason=reason.strip(),
        created_at=_utcnow(),
    )
    db.add(report)
    db.flush()

    moderator_memberships = (
        db.query(models.ClubMembership)
        .filter(
            models.ClubMembership.club_id == club_id,
            models.ClubMembership.role.in_(["owner", "admin"]),
        )
        .all()
    )
    for mod_membership in moderator_memberships:
        _create_notification(
            db,
            recipient_user_id=mod_membership.user_id,
            actor_user=current_user,
            notification_type="club_post_report",
            message=f"@{current_user.username} reportou um post no clube {club.name}: {reason.strip()[:80]}",
            post_id=post_id,
            club_id=club_id,
        )

    db.commit()
    return {"message": "Post reportado com sucesso"}


def get_club_post_reports(db: Session, current_user: models.User, club_id: int):
    current_user = db.merge(current_user)
    club = db.query(models.Club).filter(models.Club.id == club_id).first()
    if not club:
        raise LookupError("Club not found")

    membership = _get_club_membership(db, club_id, current_user.id)
    if not _user_can_manage_club(club, membership):
        raise ValueError("You do not have permission to view reports")

    rows = (
        db.query(models.PostReport, models.User, models.Post)
        .join(models.User, models.User.id == models.PostReport.reporter_user_id)
        .join(models.Post, models.Post.id == models.PostReport.post_id)
        .filter(models.PostReport.club_id == club_id)
        .order_by(models.PostReport.created_at.desc())
        .limit(100)
        .all()
    )

    followed_ids = _get_followed_ids(db, current_user.id)
    author_ids = {post.user_id for _, _, post in rows}
    authors = {
        user.id: user
        for user in db.query(models.User).filter(models.User.id.in_(author_ids)).all()
    }

    result = []
    for report, reporter, post in rows:
        post_author = authors.get(post.user_id)
        result.append(
            schemas.PostReportResponse(
                id=report.id,
                post_id=report.post_id,
                club_id=report.club_id,
                reason=report.reason,
                created_at=report.created_at,
                reporter=schemas.FeedUserSummary(
                    id=reporter.id,
                    username=reporter.username,
                    profile_image_url=reporter.profile_image_url,
                    is_verified_critic=reporter.is_verified_critic,
                    is_following=reporter.id in followed_ids,
                ),
                post=schemas.ReportedPostSummary(
                    id=post.id,
                    author=schemas.FeedUserSummary(
                        id=post_author.id,
                        username=post_author.username,
                        profile_image_url=post_author.profile_image_url,
                        is_verified_critic=post_author.is_verified_critic,
                        is_following=post_author.id in followed_ids,
                    ) if post_author else None,
                    text_content=post.text_content,
                    movie_title=post.movie_title,
                    created_at=post.created_at,
                ),
            )
        )

    return result


def ensure_demo_clubs(db: Session):
    ensure_social_demo_data(db)
    creators = {
        user.username: user
        for user in db.query(models.User)
        .filter(models.User.username.in_([item["creator_username"] for item in DEMO_CLUBS]))
        .all()
    }

    changed = False
    for club_payload in DEMO_CLUBS:
        existing_club = db.query(models.Club).filter(models.Club.name == club_payload["name"]).first()
        if existing_club:
            expected_genres = club_payload.get("genres", [])
            current_genres = json.loads(existing_club.genres_json or "[]")
            if expected_genres and current_genres != expected_genres:
                existing_club.genres_json = json.dumps(expected_genres)
                changed = True
            if not existing_club.cover_image_url and club_payload.get("cover_image_url"):
                existing_club.cover_image_url = club_payload["cover_image_url"]
                changed = True
            continue

        creator = creators.get(club_payload["creator_username"])
        if not creator:
            continue

        now = _utcnow()
        club = models.Club(
            creator_user_id=creator.id,
            name=club_payload["name"],
            slug=_generate_unique_club_slug(db, club_payload["name"]),
            short_description=club_payload["short_description"],
            description=club_payload["description"],
            genres_json=json.dumps(club_payload.get("genres", [])),
            cover_image_url=club_payload["cover_image_url"],
            is_private=club_payload["is_private"],
            created_at=now,
            updated_at=now,
        )
        db.add(club)
        db.flush()
        db.add(
            models.ClubMembership(
                club_id=club.id,
                user_id=creator.id,
                role="owner",
                created_at=now,
            )
        )
        changed = True

    if changed:
        db.commit()


def search_users_by_username(db: Session, username_fragment: str):
    return (
        db.query(models.User)
        .filter(models.User.username.ilike(f"%{username_fragment}%"))
        .order_by(models.User.username.asc())
        .all()
    )


def _apply_text_search(query_obj, db: Session, search_text: str, *columns):
    normalized_query = (search_text or "").strip()
    if not normalized_query:
        return query_obj

    dialect_name = getattr(getattr(db, "bind", None), "dialect", None)
    dialect_name = getattr(dialect_name, "name", "")
    if dialect_name == "postgresql":
        document = func.concat_ws(" ", *[func.coalesce(column, "") for column in columns])
        return query_obj.filter(
            func.to_tsvector("simple", document).op("@@")(func.plainto_tsquery("simple", normalized_query))
        )

    pattern = f"%{normalized_query}%"
    return query_obj.filter(or_(*[column.ilike(pattern) for column in columns]))


def _build_search_user_summary(db: Session, user: models.User, current_user_id: int):
    followed_ids = _get_followed_ids(db, current_user_id)
    return schemas.FeedUserSummary(
        id=user.id,
        username=user.username,
        profile_image_url=user.profile_image_url,
        is_verified_critic=bool(user.is_verified_critic),
        is_following=user.id in followed_ids,
    )


def search_users(db: Session, current_user: models.User, query: str, limit: int = 8):
    current_user = db.merge(current_user)
    rows = (
        _apply_text_search(
            db.query(models.User).filter(models.User.id != current_user.id),
            db,
            query,
            models.User.username,
            models.User.email,
        )
        .order_by(models.User.username.asc())
        .limit(limit)
        .all()
    )
    return [_build_search_user_summary(db, user, current_user.id) for user in rows]


def search_clubs(db: Session, current_user: models.User, query: str, limit: int = 8):
    ensure_demo_clubs(db)
    current_user = db.merge(current_user)
    rows = (
        _apply_text_search(
            db.query(models.Club),
            db,
            query,
            models.Club.name,
            models.Club.short_description,
            models.Club.description,
        )
        .order_by(models.Club.updated_at.desc(), models.Club.id.desc())
        .limit(limit)
        .all()
    )
    return [_build_club_summary(db, club, current_user.id) for club in rows]


def _build_movie_list_summary_with_owner(db: Session, movie_list: models.MovieList):
    summary = _build_movie_list_summary(db, movie_list)
    owner = get_user_by_id(db, movie_list.user_id)
    payload = summary.model_dump()
    payload["owner_username"] = owner.username if owner else None
    return schemas.MovieListSummary(**payload)


def search_lists(db: Session, current_user: models.User, query: str, limit: int = 8):
    current_user = db.merge(current_user)
    rows = (
        _apply_text_search(
            db.query(models.MovieList)
            .join(models.User, models.User.id == models.MovieList.user_id)
            .outerjoin(models.MovieListItem, models.MovieListItem.list_id == models.MovieList.id)
            .filter(models.MovieList.visibility == "PUBLIC")
            .filter(models.MovieList.title != DEFAULT_WATCHLIST_TITLE),
            db,
            query,
            models.MovieList.title,
            models.MovieList.description,
            models.MovieListItem.movie_title,
            models.User.username,
        )
        .distinct()
        .order_by(models.MovieList.updated_at.desc(), models.MovieList.id.desc())
        .limit(limit)
        .all()
    )
    return [_build_movie_list_summary_with_owner(db, movie_list) for movie_list in rows]


def get_global_ranking(
    db: Session,
    current_user: models.User,
    limit: int = 50,
) -> schemas.GlobalRankingResponse:
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)

    users = db.query(models.User).all()

    # Batch counts with a single query each
    rated_counts = dict(
        db.query(models.RatedMovie.user_id, func.count(models.RatedMovie.id))
        .group_by(models.RatedMovie.user_id)
        .all()
    )
    post_counts = dict(
        db.query(models.Post.user_id, func.count(models.Post.id))
        .filter(models.Post.club_id.is_(None))
        .group_by(models.Post.user_id)
        .all()
    )
    follower_counts = dict(
        db.query(models.Follow.following_id, func.count(models.Follow.id))
        .group_by(models.Follow.following_id)
        .all()
    )

    scored = []
    for user in users:
        rated = rated_counts.get(user.id, 0)
        posts = post_counts.get(user.id, 0)
        followers = follower_counts.get(user.id, 0)
        badge_count = sum(1 for b in get_badges_for_user(db, user.id) if not b.locked)
        score = rated * 3 + posts * 2 + followers * 4 + badge_count * 10
        scored.append((score, user.username.lower(), user, rated, posts, followers, badge_count))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:limit]

    entries = [
        schemas.GlobalRankingEntry(
            rank=index + 1,
            user=schemas.FeedUserSummary(
                id=item[2].id,
                username=item[2].username,
                profile_image_url=item[2].profile_image_url,
                is_verified_critic=bool(item[2].is_verified_critic),
                is_following=item[2].id in followed_ids,
            ),
            rated_count=item[3],
            post_count=item[4],
            follower_count=item[5],
            badge_count=item[6],
            score=item[0],
        )
        for index, item in enumerate(top)
    ]
    return schemas.GlobalRankingResponse(entries=entries, total=len(users))


def get_friends_ranking(
    db: Session,
    current_user: models.User,
    limit: int = 50,
) -> schemas.GlobalRankingResponse:
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)
    # mutual follows only (you follow them AND they follow you)
    followers_of_me = {
        row.follower_id
        for row in db.query(models.Follow).filter(models.Follow.following_id == current_user.id).all()
    }
    mutual_ids = followed_ids & followers_of_me
    pool_ids = mutual_ids | {current_user.id}

    users = db.query(models.User).filter(models.User.id.in_(pool_ids)).all()

    rated_counts = dict(
        db.query(models.RatedMovie.user_id, func.count(models.RatedMovie.id))
        .filter(models.RatedMovie.user_id.in_(pool_ids))
        .group_by(models.RatedMovie.user_id)
        .all()
    )
    post_counts = dict(
        db.query(models.Post.user_id, func.count(models.Post.id))
        .filter(models.Post.club_id.is_(None), models.Post.user_id.in_(pool_ids))
        .group_by(models.Post.user_id)
        .all()
    )
    follower_counts = dict(
        db.query(models.Follow.following_id, func.count(models.Follow.id))
        .filter(models.Follow.following_id.in_(pool_ids))
        .group_by(models.Follow.following_id)
        .all()
    )

    scored = []
    for user in users:
        rated = rated_counts.get(user.id, 0)
        posts = post_counts.get(user.id, 0)
        followers = follower_counts.get(user.id, 0)
        badge_count = sum(1 for b in get_badges_for_user(db, user.id) if not b.locked)
        score = rated * 3 + posts * 2 + followers * 4 + badge_count * 10
        scored.append((score, user.username.lower(), user, rated, posts, followers, badge_count))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:limit]

    entries = [
        schemas.GlobalRankingEntry(
            rank=index + 1,
            user=schemas.FeedUserSummary(
                id=item[2].id,
                username=item[2].username,
                profile_image_url=item[2].profile_image_url,
                is_verified_critic=bool(item[2].is_verified_critic),
                is_following=item[2].id in followed_ids,
            ),
            rated_count=item[3],
            post_count=item[4],
            follower_count=item[5],
            badge_count=item[6],
            score=item[0],
        )
        for index, item in enumerate(top)
    ]
    return schemas.GlobalRankingResponse(entries=entries, total=len(users))


def get_country_ranking(
    db: Session,
    current_user: models.User,
    limit: int = 50,
) -> schemas.GlobalRankingResponse:
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)
    user_country = current_user.country

    if not user_country:
        return schemas.GlobalRankingResponse(entries=[], total=0)

    users = db.query(models.User).filter(models.User.country == user_country).all()
    pool_ids = {u.id for u in users}

    rated_counts = dict(
        db.query(models.RatedMovie.user_id, func.count(models.RatedMovie.id))
        .filter(models.RatedMovie.user_id.in_(pool_ids))
        .group_by(models.RatedMovie.user_id)
        .all()
    )
    post_counts = dict(
        db.query(models.Post.user_id, func.count(models.Post.id))
        .filter(models.Post.club_id.is_(None), models.Post.user_id.in_(pool_ids))
        .group_by(models.Post.user_id)
        .all()
    )
    follower_counts = dict(
        db.query(models.Follow.following_id, func.count(models.Follow.id))
        .filter(models.Follow.following_id.in_(pool_ids))
        .group_by(models.Follow.following_id)
        .all()
    )

    scored = []
    for user in users:
        rated = rated_counts.get(user.id, 0)
        posts = post_counts.get(user.id, 0)
        followers = follower_counts.get(user.id, 0)
        badge_count = sum(1 for b in get_badges_for_user(db, user.id) if not b.locked)
        score = rated * 3 + posts * 2 + followers * 4 + badge_count * 10
        scored.append((score, user.username.lower(), user, rated, posts, followers, badge_count))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:limit]

    entries = [
        schemas.GlobalRankingEntry(
            rank=index + 1,
            user=schemas.FeedUserSummary(
                id=item[2].id,
                username=item[2].username,
                profile_image_url=item[2].profile_image_url,
                is_verified_critic=bool(item[2].is_verified_critic),
                is_following=item[2].id in followed_ids,
            ),
            rated_count=item[3],
            post_count=item[4],
            follower_count=item[5],
            badge_count=item[6],
            score=item[0],
        )
        for index, item in enumerate(top)
    ]
    return schemas.GlobalRankingResponse(entries=entries, total=len(users))


def get_trending_posts(
    db: Session,
    current_user: models.User,
    limit: int = 10,
) -> list:
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)
    cutoff = _utcnow() - timedelta(days=7)

    rows = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(
            models.Post.club_id.is_(None),
            models.Post.visible_in_feed == True,  # noqa: E712
            models.Post.created_at >= cutoff,
        )
        .all()
    )

    # Score: likes*3 + comments*2 + shares (recency bias)
    scored = []
    for post, author in rows:
        like_count = db.query(models.Like).filter(models.Like.post_id == post.id).count()
        comment_count = db.query(models.Comment).filter(models.Comment.post_id == post.id).count()
        share_count = db.query(models.PostShare).filter(models.PostShare.post_id == post.id).count()
        score = like_count * 3 + comment_count * 2 + share_count
        scored.append((score, post, author))

    scored.sort(key=lambda x: -x[0])
    return [
        _build_feed_post(db, post, author, current_user.id, followed_ids)
        for _, post, author in scored[:limit]
    ]


def get_trending_hashtags(db: Session, limit: int = 10) -> list[schemas.HashtagTrend]:
    cutoff = _utcnow() - timedelta(days=7)
    posts = (
        db.query(models.Post.tags_json)
        .filter(
            models.Post.visible_in_feed == True,  # noqa: E712
            models.Post.created_at >= cutoff,
            models.Post.tags_json != "[]",
        )
        .all()
    )
    tag_counts: dict[str, int] = {}
    for (tags_json,) in posts:
        try:
            tags = json.loads(tags_json or "[]")
            for tag in tags:
                normalized = tag.strip().lower()
                if normalized:
                    tag_counts[normalized] = tag_counts.get(normalized, 0) + 1
        except Exception:
            continue

    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    return [
        schemas.HashtagTrend(tag=tag, post_count=count)
        for tag, count in sorted_tags[:limit]
    ]


def get_posts_by_hashtag(
    db: Session,
    current_user: models.User,
    tag: str,
    cursor: str | None = None,
    limit: int = 20,
) -> schemas.FeedResponse:
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)
    normalized = tag.lower().strip().lstrip("#")

    query = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(
            models.Post.visible_in_feed == True,  # noqa: E712
            models.Post.tags_json.like(f'%"{normalized}"%'),
        )
        .order_by(models.Post.created_at.desc(), models.Post.id.desc())
    )
    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.filter(models.Post.id < cursor_id)
        except ValueError:
            pass

    rows = query.limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_build_feed_post(db, post, author, current_user.id, followed_ids) for post, author in rows]
    next_cursor = str(rows[-1][0].id) if has_more and rows else None
    return schemas.FeedResponse(items=items, next_cursor=next_cursor)


def get_trending_users(db: Session, current_user: models.User, limit: int = 8):
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)  # 1 query

    # Aggregate subqueries — evaluated as a single JOIN by the DB engine
    follower_sq = (
        db.query(
            models.Follow.following_id.label("uid"),
            func.count(models.Follow.id).label("cnt"),
        )
        .group_by(models.Follow.following_id)
        .subquery()
    )
    post_sq = (
        db.query(
            models.Post.user_id.label("uid"),
            func.count(models.Post.id).label("cnt"),
        )
        .filter(models.Post.club_id.is_(None), models.Post.visible_in_feed == True)  # noqa: E712
        .group_by(models.Post.user_id)
        .subquery()
    )
    rating_sq = (
        db.query(
            models.RatedMovie.user_id.label("uid"),
            func.count(models.RatedMovie.id).label("cnt"),
        )
        .group_by(models.RatedMovie.user_id)
        .subquery()
    )

    score_expr = (
        func.coalesce(follower_sq.c.cnt, 0) * 3
        + func.coalesce(post_sq.c.cnt, 0) * 2
        + func.coalesce(rating_sq.c.cnt, 0)
    )

    users = (
        db.query(models.User)
        .outerjoin(follower_sq, follower_sq.c.uid == models.User.id)
        .outerjoin(post_sq, post_sq.c.uid == models.User.id)
        .outerjoin(rating_sq, rating_sq.c.uid == models.User.id)
        .filter(models.User.id != current_user.id)
        .order_by(score_expr.desc(), models.User.username.asc())
        .limit(limit)
        .all()
    )  # 1 query — score, ordering and limit done entirely in the DB

    return [
        schemas.FeedUserSummary(
            id=u.id,
            username=u.username,
            profile_image_url=u.profile_image_url,
            is_verified_critic=bool(u.is_verified_critic),
            is_following=u.id in followed_ids,
        )
        for u in users
    ]


def get_trending_clubs(db: Session, current_user: models.User, limit: int = 8):
    ensure_demo_clubs(db)
    current_user = db.merge(current_user)

    # Aggregate subqueries — evaluated as a single JOIN by the DB engine
    member_sq = (
        db.query(
            models.ClubMembership.club_id.label("cid"),
            func.count(models.ClubMembership.id).label("cnt"),
        )
        .group_by(models.ClubMembership.club_id)
        .subquery()
    )
    post_sq = (
        db.query(
            models.Post.club_id.label("cid"),
            func.count(models.Post.id).label("cnt"),
        )
        .filter(models.Post.club_id.isnot(None))
        .group_by(models.Post.club_id)
        .subquery()
    )

    score_expr = (
        func.coalesce(member_sq.c.cnt, 0) * 3
        + func.coalesce(post_sq.c.cnt, 0) * 2
    )

    rows = (
        db.query(
            models.Club,
            func.coalesce(member_sq.c.cnt, 0).label("member_count"),
        )
        .outerjoin(member_sq, member_sq.c.cid == models.Club.id)
        .outerjoin(post_sq, post_sq.c.cid == models.Club.id)
        .order_by(score_expr.desc(), models.Club.id.desc())
        .limit(limit)
        .all()
    )  # 1 query

    if not rows:
        return []

    clubs = [club for club, _ in rows]
    member_counts = {club.id: count for club, count in rows}
    club_ids = [club.id for club in clubs]

    # Batch-fetch creators in one query instead of one per club
    creator_ids = {club.creator_user_id for club in clubs}
    creators = {
        u.id: u
        for u in db.query(models.User).filter(models.User.id.in_(creator_ids)).all()
    }  # 1 query

    # Batch-fetch current user's memberships for these clubs
    my_memberships = {
        row.club_id
        for row in db.query(models.ClubMembership.club_id)
        .filter(
            models.ClubMembership.club_id.in_(club_ids),
            models.ClubMembership.user_id == current_user.id,
        )
        .all()
    }  # 1 query

    # Batch-fetch current user's pending join requests for these clubs
    my_pending = {
        row.club_id
        for row in db.query(models.ClubJoinRequest.club_id)
        .filter(
            models.ClubJoinRequest.club_id.in_(club_ids),
            models.ClubJoinRequest.user_id == current_user.id,
            models.ClubJoinRequest.status == "pending",
        )
        .all()
    }  # 1 query

    result = []
    for club in clubs:
        creator = creators.get(club.creator_user_id)
        result.append(schemas.ClubSummary(
            id=club.id,
            name=club.name,
            slug=club.slug,
            short_description=club.short_description,
            description=club.description,
            genres=json.loads(club.genres_json or "[]"),
            cover_image_url=club.cover_image_url,
            is_private=club.is_private,
            member_count=member_counts.get(club.id, 0),
            creator=schemas.ClubCreatorSummary(
                id=creator.id,
                username=creator.username,
                profile_image_url=creator.profile_image_url,
            ) if creator else schemas.ClubCreatorSummary(id=0, username="desconhecido"),
            joined_by_me=club.id in my_memberships,
            pending_request_by_me=club.id in my_pending,
            created_at=club.created_at,
        ))
    return result


def get_trending_lists(db: Session, current_user: models.User, limit: int = 8):
    current_user = db.merge(current_user)
    lists = (
        db.query(models.MovieList)
        .filter(models.MovieList.visibility == "PUBLIC")
        .filter(models.MovieList.title != DEFAULT_WATCHLIST_TITLE)
        .order_by(models.MovieList.updated_at.desc(), models.MovieList.id.desc())
        .all()
    )
    ranked = []
    for movie_list in lists:
        item_count = db.query(models.MovieListItem).filter(models.MovieListItem.list_id == movie_list.id).count()
        score = item_count * 3 + int(movie_list.allow_social_sharing) * 2
        ranked.append((score, movie_list.updated_at, movie_list))

    ranked.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    return [_build_movie_list_summary_with_owner(db, item[2]) for item in ranked[:limit]]


def _extract_hashtags(text: str | None):
    if not text:
        return []
    seen = set()
    tags = []
    for match in HASHTAG_PATTERN.findall(text):
        normalized = match.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            tags.append(normalized)
    return tags


def _extract_mentions(text: str | None):
    if not text:
        return []
    seen = set()
    usernames = []
    for match in MENTION_PATTERN.findall(text):
        normalized = match.strip()
        lowered = normalized.lower()
        if lowered and lowered not in seen:
            seen.add(lowered)
            usernames.append(normalized)
    return usernames


def _get_followed_ids(db: Session, user_id: int):
    return {
        row.following_id
        for row in db.query(models.Follow).filter(models.Follow.follower_id == user_id).all()
    }


# ── Device token (push notifications) ────────────────────────────────────────

def register_device_token(db: Session, user_id: int, token: str, platform: str | None = None):
    existing = db.query(models.DeviceToken).filter(
        models.DeviceToken.user_id == user_id,
        models.DeviceToken.token == token,
    ).first()
    if existing:
        existing.platform = platform
    else:
        db.add(models.DeviceToken(user_id=user_id, token=token, platform=platform))
    db.commit()


def _send_fcm_background(tokens: list[str], title: str, body: str, data: dict):
    if not _FCM_ENABLED or not _fcm_messaging:
        return
    from .database import SessionLocal
    invalid: list[str] = []
    for token in tokens:
        try:
            msg = _fcm_messaging.Message(
                notification=_fcm_messaging.Notification(title=title, body=body),
                data=data,
                token=token,
            )
            _fcm_messaging.send(msg)
        except _fcm_messaging.UnregisteredError:
            invalid.append(token)
        except Exception:
            pass
    if invalid:
        try:
            with SessionLocal() as db:
                db.query(models.DeviceToken).filter(
                    models.DeviceToken.token.in_(invalid)
                ).delete(synchronize_session=False)
                db.commit()
        except Exception:
            pass


def send_push_to_user(db: Session, user_id: int, title: str, body: str, data: dict | None = None):
    if not _FCM_ENABLED:
        return
    tokens = [
        dt.token
        for dt in db.query(models.DeviceToken).filter(models.DeviceToken.user_id == user_id).all()
    ]
    if not tokens:
        return
    clean_data = {k: str(v) for k, v in (data or {}).items() if v is not None}
    threading.Thread(
        target=_send_fcm_background,
        args=(tokens, title, body, clean_data),
        daemon=True,
    ).start()


def _create_notification(
    db: Session,
    recipient_user_id: int,
    actor_user: models.User,
    notification_type: str,
    message: str,
    post_id: int | None = None,
    comment_id: int | None = None,
    club_id: int | None = None,
    movie_id: int | None = None,
    conversation_id: int | None = None,
):
    if recipient_user_id == actor_user.id:
        return

    preference = get_user_preference(db, recipient_user_id)
    preference_key = NOTIFICATION_PREFERENCE_MAP.get(notification_type)
    if preference_key:
        merged_preferences = merge_notification_preferences(
            getattr(preference, "notification_preferences_json", None)
        )
        if not merged_preferences.get(preference_key, True):
            return

    db.add(
        models.Notification(
            user_id=recipient_user_id,
            actor_user_id=actor_user.id,
            notification_type=notification_type,
            message=message,
            post_id=post_id,
            comment_id=comment_id,
            club_id=club_id,
            movie_id=movie_id,
            conversation_id=conversation_id,
        )
    )
    send_push_to_user(
        db, recipient_user_id, "Moviz", message,
        data={"notification_type": notification_type},
    )


def _create_mention_notifications(
    db: Session,
    actor_user: models.User,
    content: str | None,
    notification_type: str,
    message_builder,
    post_id: int | None = None,
    comment_id: int | None = None,
    club_id: int | None = None,
    movie_id: int | None = None,
    conversation_id: int | None = None,
):
    mentioned_usernames = _extract_mentions(content)
    if not mentioned_usernames:
        return

    lowered_to_original = {username.lower(): username for username in mentioned_usernames}
    mentioned_users = (
        db.query(models.User)
        .filter(models.User.username.in_(list(lowered_to_original.values())))
        .all()
    )
    if len(mentioned_users) != len(lowered_to_original):
        all_users = db.query(models.User).all()
        by_lower = {user.username.lower(): user for user in all_users}
        mentioned_users = [
            by_lower[username_lower]
            for username_lower in lowered_to_original
            if username_lower in by_lower
        ]

    for user in mentioned_users:
        _create_notification(
            db,
            recipient_user_id=user.id,
            actor_user=actor_user,
            notification_type=notification_type,
            message=message_builder(user),
            post_id=post_id,
            comment_id=comment_id,
            club_id=club_id,
            movie_id=movie_id,
            conversation_id=conversation_id,
        )


def _resolve_notification_deep_link(notification: models.Notification, actor: models.User | None):
    if getattr(notification, "conversation_id", None):
        return f"/direct?conversation={notification.conversation_id}"
    if notification.post_id:
        return f"/feed?post={notification.post_id}"
    if getattr(notification, "club_id", None):
        return f"/clubs/{notification.club_id}"
    if actor:
        return f"/users/{actor.username}"
    return None


def _build_notification(
    db: Session,
    notification: models.Notification,
    current_user_id: int,
    followed_ids: set[int],
):
    actor = get_user_by_id(db, notification.actor_user_id)
    preference_key = NOTIFICATION_PREFERENCE_MAP.get(notification.notification_type)
    return schemas.NotificationResponse(
        id=notification.id,
        notification_type=notification.notification_type,
        preference_key=preference_key,
        message=notification.message,
        is_read=notification.is_read,
        created_at=notification.created_at,
        actor=_build_feed_user_summary(actor, followed_ids) if actor else schemas.FeedUserSummary(
            id=0,
            username="moviz",
            profile_image_url=None,
            is_following=False,
        ),
        post_id=notification.post_id,
        comment_id=notification.comment_id,
        club_id=getattr(notification, "club_id", None),
        movie_id=getattr(notification, "movie_id", None),
        conversation_id=getattr(notification, "conversation_id", None),
        request_id=actor.id if actor and notification.notification_type == "follow_request" else None,
        deep_link=_resolve_notification_deep_link(notification, actor),
    )


def _resolve_notification_type_filter(filter_value: str | None):
    normalized = (filter_value or "").strip().lower()
    if not normalized or normalized == "all":
        return None

    matching_exact_types = [
        notification_type
        for notification_type in NOTIFICATION_PREFERENCE_MAP
        if notification_type.lower() == normalized
    ]
    if matching_exact_types:
        return matching_exact_types

    matching_category_types = [
        notification_type
        for notification_type, preference_key in NOTIFICATION_PREFERENCE_MAP.items()
        if preference_key == normalized
    ]
    return matching_category_types or None


def get_notifications(db: Session, current_user: models.User, limit: int = 60, filter_type: str | None = None):
    current_user = db.merge(current_user)
    followed_ids = _get_followed_ids(db, current_user.id)
    preference = get_user_preference(db, current_user.id)
    merged_preferences = merge_notification_preferences(
        getattr(preference, "notification_preferences_json", None)
    )
    query = db.query(models.Notification).filter(models.Notification.user_id == current_user.id)
    allowed_types = _resolve_notification_type_filter(filter_type)
    if allowed_types is not None:
        query = query.filter(models.Notification.notification_type.in_(allowed_types))

    rows = query.order_by(models.Notification.created_at.desc(), models.Notification.id.desc()).limit(limit).all()
    visible_rows = []
    for row in rows:
        preference_key = NOTIFICATION_PREFERENCE_MAP.get(row.notification_type)
        if preference_key and not merged_preferences.get(preference_key, True):
            continue
        visible_rows.append(row)
    return [_build_notification(db, row, current_user.id, followed_ids) for row in visible_rows]


def mark_notification_as_read(db: Session, current_user: models.User, notification_id: int, is_read: bool = True):
    current_user = db.merge(current_user)
    notification = (
        db.query(models.Notification)
        .filter(
            models.Notification.id == notification_id,
            models.Notification.user_id == current_user.id,
        )
        .first()
    )
    if not notification:
        raise LookupError("Notification not found")

    notification.is_read = bool(is_read)
    db.commit()
    followed_ids = _get_followed_ids(db, current_user.id)
    return _build_notification(db, notification, current_user.id, followed_ids)


def get_hashtag_suggestions(db: Session, query: str, limit: int = 8):
    normalized_query = (query or "").strip().lower().lstrip("#")
    if not normalized_query:
        return []

    tag_counts = {}
    for post in db.query(models.Post).all():
        try:
            raw_tags = json.loads(post.tags_json or "[]")
        except json.JSONDecodeError:
            raw_tags = []
        for tag in raw_tags:
            normalized = str(tag).strip().lower()
            if normalized.startswith(normalized_query):
                tag_counts[normalized] = tag_counts.get(normalized, 0) + 1

    if not tag_counts:
        for tag in FALLBACK_HASHTAGS:
            if tag.startswith(normalized_query):
                tag_counts[tag] = 1

    return [
        schemas.HashtagSuggestion(tag=tag)
        for tag, _count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def get_mention_suggestions(db: Session, current_user: models.User, query: str, limit: int = 8):
    ensure_social_demo_data(db)
    current_user = db.merge(current_user)
    normalized_query = (query or "").strip().lower().lstrip("@")
    if not normalized_query:
        return []

    friend_ids = {
        row.friend_id
        for row in db.query(models.Friendship).filter(models.Friendship.user_id == current_user.id).all()
    }
    interaction_scores = {}

    for friend_id in friend_ids:
        interaction_scores[friend_id] = interaction_scores.get(friend_id, 0) + 100

    commented_posts = db.query(models.Comment).filter(models.Comment.user_id == current_user.id).all()
    for comment in commented_posts:
        post = db.query(models.Post).filter(models.Post.id == comment.post_id).first()
        if post and post.user_id != current_user.id:
            interaction_scores[post.user_id] = interaction_scores.get(post.user_id, 0) + 10

    shared_rows = db.query(models.PostShare).filter(models.PostShare.sender_user_id == current_user.id).all()
    for row in shared_rows:
        interaction_scores[row.recipient_user_id] = interaction_scores.get(row.recipient_user_id, 0) + 8

    direct_memberships = (
        db.query(models.ConversationMember)
        .filter(models.ConversationMember.user_id == current_user.id)
        .all()
    )
    for membership in direct_memberships:
        member_rows = (
            db.query(models.ConversationMember)
            .filter(models.ConversationMember.conversation_id == membership.conversation_id)
            .all()
        )
        for member in member_rows:
            if member.user_id != current_user.id:
                interaction_scores[member.user_id] = interaction_scores.get(member.user_id, 0) + 5

    all_users = (
        db.query(models.User)
        .filter(models.User.id != current_user.id)
        .order_by(models.User.username.asc())
        .all()
    )
    matched_users = [
        user for user in all_users if normalized_query in user.username.lower()
    ]

    if matched_users:
        matched_users.sort(
            key=lambda user: (
                -(interaction_scores.get(user.id, 0)),
                0 if user.id in friend_ids else 1,
                user.username.lower(),
            )
        )
        return [
            schemas.MentionSuggestion(
                username=user.username,
                profile_image_url=user.profile_image_url,
            )
            for user in matched_users[:limit]
        ]
    return []


# In-process timestamp of the last successful demo-data refresh.
# Lets us skip the heavy DB check on most requests (skipped if < 6 h old).
_demo_last_refreshed_at: datetime | None = None


def ensure_social_demo_data(db: Session):
    """Guarantee demo users and posts always exist and always look recent.

    Unlike a one-shot seed that checks ``db.query(Post).count()``, this
    function tracks demo posts by their authors so real user posts never
    suppress the seed.  On each call it also refreshes demo post timestamps
    so they stay within the discover-feed window regardless of when the
    server was first started.

    The in-process ``_demo_last_refreshed_at`` flag prevents DB overhead on
    every feed request: we only run the full refresh logic once every 6 hours
    per server process.
    """
    global _demo_last_refreshed_at
    now = _utcnow()
    # Fast path: skip within a 6-hour window to avoid a DB round-trip per request.
    if _demo_last_refreshed_at and (now - _demo_last_refreshed_at).total_seconds() < 6 * 3600:
        return

    # ── 1. Ensure demo users exist / keep is_verified_critic in sync ─────────
    demo_user_map: dict[str, models.User] = {}
    changed = False
    for demo_user in SOCIAL_DEMO_USERS:
        user = get_user_by_username(db, demo_user["username"])
        if not user:
            user = create_user(
                db,
                schemas.UserCreate(
                    username=demo_user["username"],
                    email=demo_user["email"],
                    password=demo_user["password"],
                ),
            )
            user.onboarding_completed = True
            user.is_verified_critic = bool(demo_user.get("is_verified_critic"))
            changed = True
        elif bool(user.is_verified_critic) != bool(demo_user.get("is_verified_critic")):
            user.is_verified_critic = bool(demo_user.get("is_verified_critic"))
            changed = True
        demo_user_map[user.username] = user

    if changed:
        db.commit()
        for user in demo_user_map.values():
            db.refresh(user)

    # ── 2. Check which demo posts already exist (by author) ──────────────────
    demo_user_ids = {u.id for u in demo_user_map.values()}
    existing_demo_posts: list[models.Post] = (
        db.query(models.Post)
        .filter(models.Post.user_id.in_(demo_user_ids))
        .order_by(models.Post.id.asc())
        .all()
        if demo_user_ids else []
    )

    # ── 3. Refresh timestamps so posts stay inside the discover window ────────
    # Spread the posts across the last 6 days (1 new-looking post every ~10 h).
    _DEMO_SPREAD_HOURS = 144  # 6 days
    if existing_demo_posts:
        # Only refresh when the oldest demo post is more than 6 days old.
        oldest = min(p.created_at for p in existing_demo_posts)
        if (now - oldest).total_seconds() > _DEMO_SPREAD_HOURS * 3600:
            n = len(existing_demo_posts)
            for idx, post in enumerate(existing_demo_posts):
                # Space posts evenly across last 6 days, newest post most-recent.
                hours_back = (n - 1 - idx) * (_DEMO_SPREAD_HOURS / max(n, 1))
                post.created_at = now - timedelta(hours=hours_back)
            db.commit()
        _demo_last_refreshed_at = now
        return  # posts already exist; nothing else to create

    # ── 4. Create demo posts for the first time ───────────────────────────────
    n_posts = len(SOCIAL_DEMO_POSTS)
    created_posts: list[models.Post] = []
    for index, payload in enumerate(SOCIAL_DEMO_POSTS):
        author = demo_user_map[payload["username"]]
        # Space across the last 6 days, most-recent post first.
        hours_back = (n_posts - 1 - index) * (_DEMO_SPREAD_HOURS / max(n_posts, 1))
        post = models.Post(
            user_id=author.id,
            post_type=payload["post_type"],
            text_content=payload.get("text_content"),
            sentiment=payload.get("sentiment"),
            tags_json=json.dumps(payload.get("tags", [])),
            movie_id=payload.get("movie_id"),
            movie_title=payload.get("movie_title"),
            movie_poster_path=payload.get("movie_poster_path"),
            movie_release_date=payload.get("movie_release_date"),
            list_title=payload.get("list_title"),
            list_items_json=json.dumps(payload.get("list_items", [])),
            created_at=now - timedelta(hours=hours_back),
        )
        db.add(post)
        db.flush()

        if payload.get("review_headline") or payload.get("rating") is not None:
            db.add(
                models.Review(
                    post_id=post.id,
                    headline=payload.get("review_headline"),
                    rating=payload.get("rating"),
                    created_at=post.created_at,
                )
            )

        for comment_index, comment_text in enumerate(payload.get("comments", [])):
            commenter = list(demo_user_map.values())[(index + comment_index + 1) % len(demo_user_map)]
            db.add(
                models.Comment(
                    post_id=post.id,
                    user_id=commenter.id,
                    content=comment_text,
                    created_at=post.created_at + timedelta(minutes=comment_index + 1),
                )
            )

        created_posts.append(post)

    # Mutual follows between demo users
    demo_users = list(demo_user_map.values())
    for idx, follower in enumerate(demo_users):
        following = demo_users[(idx + 1) % len(demo_users)]
        existing_follow = db.query(models.Follow).filter_by(
            follower_id=follower.id, following_id=following.id
        ).first()
        if not existing_follow:
            db.add(models.Follow(follower_id=follower.id, following_id=following.id))

    # Seed some likes so engagement scores look natural
    for idx, post in enumerate(created_posts):
        liker = demo_users[idx % len(demo_users)]
        existing_like = db.query(models.Like).filter_by(
            post_id=post.id, user_id=liker.id
        ).first()
        if not existing_like:
            db.add(models.Like(post_id=post.id, user_id=liker.id))

    db.commit()
    _demo_last_refreshed_at = now


def ensure_following_demo_data_for_user(db: Session, current_user: models.User):
    current_user = db.merge(current_user)
    ensure_social_demo_data(db)
    sync_following_to_friendships(db, current_user.id)

    existing_count = (
        db.query(models.Follow)
        .filter(models.Follow.follower_id == current_user.id)
        .count()
    )
    if existing_count:
        return

    demo_users = (
        db.query(models.User)
        .filter(models.User.username.in_([user["username"] for user in SOCIAL_DEMO_USERS]))
        .order_by(models.User.id.asc())
        .all()
    )

    for demo_user in demo_users[:2]:
        if demo_user.id != current_user.id:
            db.add(models.Follow(follower_id=current_user.id, following_id=demo_user.id))

    existing_friend_count = (
        db.query(models.Friendship)
        .filter(models.Friendship.user_id == current_user.id)
        .count()
    )
    if not existing_friend_count:
        first_demo_friend = next(
            (demo_user for demo_user in demo_users if demo_user.id != current_user.id),
            None,
        )
        if first_demo_friend:
            db.add(models.Friendship(user_id=current_user.id, friend_id=first_demo_friend.id))

    db.commit()


def follow_user(db: Session, follower: models.User, following_user_id: int):
    follower = db.merge(follower)
    if follower.id == following_user_id:
        raise ValueError("You cannot follow yourself")

    target = get_user_by_id(db, following_user_id)
    if not target:
        raise LookupError("User not found")

    existing = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == follower.id,
            models.Follow.following_id == target.id,
        )
        .first()
    )
    if existing:
        return {"target": target, "following": True, "pending": False}

    if is_private_account_enabled(db, target.id):
        pending_request = get_pending_follow_request(db, follower.id, target.id)
        if not pending_request:
            pending_request = models.FollowRequest(
                requester_id=follower.id,
                target_user_id=target.id,
                status="pending",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            db.add(pending_request)
            db.flush()
            _create_notification(
                db,
                recipient_user_id=target.id,
                actor_user=follower,
                notification_type="follow_request",
                message=f"@{follower.username} pediu para seguir seu perfil privado.",
            )
            db.commit()
        return {"target": target, "following": False, "pending": True}

    db.add(models.Follow(follower_id=follower.id, following_id=target.id))
    existing_friend = (
        db.query(models.Friendship)
        .filter(
            models.Friendship.user_id == follower.id,
            models.Friendship.friend_id == target.id,
        )
        .first()
    )
    if not existing_friend:
        db.add(models.Friendship(user_id=follower.id, friend_id=target.id))
    _create_notification(
        db,
        recipient_user_id=target.id,
        actor_user=follower,
        notification_type="new_follower",
        message=f"@{follower.username} comecou a seguir voce.",
    )
    db.commit()

    return {"target": target, "following": True, "pending": False}


def unfollow_user(db: Session, follower: models.User, following_user_id: int):
    follower = db.merge(follower)
    if follower.id == following_user_id:
        raise ValueError("You cannot unfollow yourself")

    target = get_user_by_id(db, following_user_id)
    if not target:
        raise LookupError("User not found")

    existing_follow = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == follower.id,
            models.Follow.following_id == target.id,
        )
        .first()
    )
    if existing_follow:
        db.delete(existing_follow)

    pending_request = (
        db.query(models.FollowRequest)
        .filter(
            models.FollowRequest.requester_id == follower.id,
            models.FollowRequest.target_user_id == target.id,
            models.FollowRequest.status == "pending",
        )
        .first()
    )
    if pending_request:
        db.delete(pending_request)

    existing_friend = (
        db.query(models.Friendship)
        .filter(
            models.Friendship.user_id == follower.id,
            models.Friendship.friend_id == target.id,
        )
        .first()
    )
    if existing_friend:
        db.delete(existing_friend)

    db.commit()
    return target


def remove_follower(db: Session, current_user: models.User, follower_user_id: int):
    current_user = db.merge(current_user)
    if current_user.id == follower_user_id:
        raise ValueError("You cannot remove yourself from your followers")

    follower = get_user_by_id(db, follower_user_id)
    if not follower:
        raise LookupError("User not found")

    existing_follow = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == follower.id,
            models.Follow.following_id == current_user.id,
        )
        .first()
    )
    if existing_follow:
        db.delete(existing_follow)

    reciprocal_friend = (
        db.query(models.Friendship)
        .filter(
            models.Friendship.user_id == follower.id,
            models.Friendship.friend_id == current_user.id,
        )
        .first()
    )
    if reciprocal_friend:
        db.delete(reciprocal_friend)

    db.commit()
    return follower


def review_follow_request(
    db: Session,
    current_user: models.User,
    requester_user_id: int,
    approve: bool,
):
    current_user = db.merge(current_user)
    follow_request = (
        db.query(models.FollowRequest)
        .filter(
            models.FollowRequest.requester_id == requester_user_id,
            models.FollowRequest.target_user_id == current_user.id,
            models.FollowRequest.status == "pending",
        )
        .first()
    )
    if not follow_request:
        raise LookupError("Follow request not found")

    requester = get_user_by_id(db, requester_user_id)
    if not requester:
        raise LookupError("User not found")

    follow_request.updated_at = _utcnow()
    if approve:
        follow_request.status = "approved"
        existing_follow = (
            db.query(models.Follow)
            .filter(
                models.Follow.follower_id == requester.id,
                models.Follow.following_id == current_user.id,
            )
            .first()
        )
        if not existing_follow:
            db.add(models.Follow(follower_id=requester.id, following_id=current_user.id))
        existing_friend = (
            db.query(models.Friendship)
            .filter(
                models.Friendship.user_id == requester.id,
                models.Friendship.friend_id == current_user.id,
            )
            .first()
        )
        if not existing_friend:
            db.add(models.Friendship(user_id=requester.id, friend_id=current_user.id))
        _create_notification(
            db,
            recipient_user_id=requester.id,
            actor_user=current_user,
            notification_type="follow_request_approved",
            message=f"@{current_user.username} aceitou seu pedido para seguir o perfil.",
        )
    else:
        follow_request.status = "rejected"
        _create_notification(
            db,
            recipient_user_id=requester.id,
            actor_user=current_user,
            notification_type="follow_request_rejected",
            message=f"@{current_user.username} recusou seu pedido para seguir o perfil.",
        )

    db.commit()
    return {"message": "Follow request updated successfully"}


def _encode_feed_cursor(created_at: datetime, post_id: int):
    return f"{created_at.isoformat()}|{post_id}"


def _decode_feed_cursor(cursor: str | None):
    if not cursor:
        return None, None
    try:
        created_at_raw, post_id_raw = cursor.split("|", 1)
        return datetime.fromisoformat(created_at_raw), int(post_id_raw)
    except (ValueError, TypeError):
        return None, None


def _build_feed_user_summary(user: models.User, followed_ids: set[int]):
    return schemas.FeedUserSummary(
        id=user.id,
        username=user.username,
        profile_image_url=user.profile_image_url,
        is_verified_critic=bool(user.is_verified_critic),
        is_following=user.id in followed_ids,
    )


def _build_feed_comments(db: Session, post_id: int, followed_ids: set[int], limit: int = 2):
    comment_rows = (
        db.query(models.Comment, models.User)
        .join(models.User, models.User.id == models.Comment.user_id)
        .filter(models.Comment.post_id == post_id)
        .order_by(models.Comment.created_at.desc(), models.Comment.id.desc())
        .limit(limit)
        .all()
    )
    comment_rows.reverse()
    return [
        schemas.FeedComment(
            id=comment.id,
            content=comment.content,
            created_at=comment.created_at,
            author=_build_feed_user_summary(author, followed_ids),
        )
        for comment, author in comment_rows
    ]


def get_post_comments(db: Session, current_user: models.User, post_id: int):
    current_user = db.merge(current_user)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise LookupError("Post not found")
    _assert_can_interact_with_club_post(db, current_user, post)

    followed_ids = {
        row.following_id
        for row in db.query(models.Follow).filter(models.Follow.follower_id == current_user.id).all()
    }
    comment_rows = (
        db.query(models.Comment, models.User)
        .join(models.User, models.User.id == models.Comment.user_id)
        .filter(models.Comment.post_id == post_id)
        .order_by(models.Comment.created_at.asc(), models.Comment.id.asc())
        .all()
    )
    return [
        schemas.FeedComment(
            id=comment.id,
            content=comment.content,
            created_at=comment.created_at,
            author=_build_feed_user_summary(author, followed_ids),
        )
        for comment, author in comment_rows
    ]


def _build_feed_post(db: Session, post: models.Post, author: models.User, current_user_id: int, followed_ids: set[int]):
    review = db.query(models.Review).filter(models.Review.post_id == post.id).first()
    club = db.query(models.Club).filter(models.Club.id == post.club_id).first() if post.club_id else None
    like_count = db.query(models.Like).filter(models.Like.post_id == post.id).count()
    comment_count = db.query(models.Comment).filter(models.Comment.post_id == post.id).count()
    share_count = db.query(models.PostShare).filter(models.PostShare.post_id == post.id).count()
    liked_by_me = (
        db.query(models.Like)
        .filter(models.Like.post_id == post.id, models.Like.user_id == current_user_id)
        .first()
        is not None
    )
    saved_by_me = (
        db.query(models.SavedPost)
        .filter(models.SavedPost.post_id == post.id, models.SavedPost.user_id == current_user_id)
        .first()
        is not None
    )

    return schemas.FeedPost(
        id=post.id,
        post_type=post.post_type,
        text_content=post.text_content,
        sentiment=post.sentiment,
        visible_in_feed=bool(post.visible_in_feed),
        contains_spoiler=bool(post.contains_spoiler),
        would_watch_again=post.would_watch_again,
        tags=json.loads(post.tags_json or "[]"),
        club_id=post.club_id,
        club_name=club.name if club else None,
        movie_id=post.movie_id,
        movie_title=post.movie_title,
        movie_poster_path=post.movie_poster_path,
        movie_release_date=post.movie_release_date,
        list_title=post.list_title,
        list_items=json.loads(post.list_items_json or "[]"),
        created_at=post.created_at,
        author=_build_feed_user_summary(author, followed_ids),
        review_headline=review.headline if review else None,
        rating=review.rating if review else None,
        like_count=like_count,
        comment_count=comment_count,
        share_count=share_count,
        liked_by_me=liked_by_me,
        saved_by_me=saved_by_me,
        comments=_build_feed_comments(db, post.id, followed_ids),
    )


def get_user_feed_posts(db: Session, owner_user_id: int, current_user_id: int, followed_ids: set[int], limit: int = 12, skip: int = 0):
    rows = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(models.Post.user_id == owner_user_id, models.Post.club_id.is_(None))
        .order_by(models.Post.created_at.desc(), models.Post.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        _build_feed_post(db, post, author, current_user_id, followed_ids)
        for post, author in rows
    ]


def get_saved_feed_posts(db: Session, current_user_id: int, followed_ids: set[int], limit: int = 12, skip: int = 0):
    rows = (
        db.query(models.Post, models.User, models.SavedPost)
        .join(models.SavedPost, models.SavedPost.post_id == models.Post.id)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(
            models.SavedPost.user_id == current_user_id,
            models.Post.club_id.is_(None),
            models.Post.visible_in_feed == True,  # noqa: E712
        )
        .order_by(models.SavedPost.created_at.desc(), models.SavedPost.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        _build_feed_post(db, post, author, current_user_id, followed_ids)
        for post, author, _saved in rows
    ]


def get_feed(db: Session, current_user: models.User, mode: str = "following", cursor: str | None = None, limit: int = 10):
    from math import log1p

    # ensure_following_demo_data_for_user already calls ensure_social_demo_data
    # internally — no need to call it twice here.
    current_user = db.merge(current_user)
    ensure_following_demo_data_for_user(db, current_user)

    followed_ids = {
        row.following_id
        for row in db.query(models.Follow).filter(models.Follow.follower_id == current_user.id).all()
    }

    # ── Suggested feed: public profiles, taste-ranked ─────────────────────
    if mode in ("suggested", "discover"):
        pref = db.query(models.UserPreference).filter_by(user_id=current_user.id).first()
        user_genres = set(json.loads(getattr(pref, "favorite_genres", "[]") or "[]"))

        def _build_discover_query(cutoff_days: int | None):
            q = (
                db.query(models.Post, models.User)
                .join(models.User, models.User.id == models.Post.user_id)
                .outerjoin(models.UserPreference, models.UserPreference.user_id == models.User.id)
                .filter(
                    models.Post.club_id.is_(None),
                    models.Post.visible_in_feed == True,  # noqa: E712
                    models.Post.user_id != current_user.id,
                    or_(
                        models.UserPreference.id.is_(None),
                        models.UserPreference.is_private_account == False,  # noqa: E712
                    ),
                )
            )
            if cutoff_days is not None:
                q = q.filter(models.Post.created_at >= _utcnow() - timedelta(days=cutoff_days))
            if followed_ids:
                q = q.filter(~models.Post.user_id.in_(followed_ids))
            return q

        fetch_limit = max(120, limit * 10)
        # Primary window: 30 days.  Fallback to all-time when content is thin
        # (cold-start / early launch with few real users).
        all_rows = _build_discover_query(30).order_by(models.Post.created_at.desc()).limit(fetch_limit).all()
        if len(all_rows) < limit:
            all_rows = _build_discover_query(None).order_by(models.Post.created_at.desc()).limit(fetch_limit).all()

        # Batch-fetch engagement counts
        all_post_ids = [p.id for p, _ in all_rows]
        like_counts: dict[int, int] = {}
        comment_counts: dict[int, int] = {}
        if all_post_ids:
            like_counts = dict(
                db.query(models.Like.post_id, func.count(models.Like.id))
                .filter(models.Like.post_id.in_(all_post_ids))
                .group_by(models.Like.post_id)
                .all()
            )
            comment_counts = dict(
                db.query(models.Comment.post_id, func.count(models.Comment.id))
                .filter(models.Comment.post_id.in_(all_post_ids))
                .group_by(models.Comment.post_id)
                .all()
            )

        # Batch-fetch genre data for genre-boost
        cached_genres: dict[int, set] = {}
        if user_genres:
            movie_ids = {p.movie_id for p, _ in all_rows if p.movie_id}
            if movie_ids:
                for cm in db.query(models.MovieFeatureCache).filter(models.MovieFeatureCache.movie_id.in_(movie_ids)).all():
                    cached_genres[cm.movie_id] = set(json.loads(cm.genres_json or "[]"))

        now = _utcnow()

        def _suggest_score(post: models.Post) -> float:
            hours_old = max(0.0, (now - post.created_at).total_seconds() / 3600)
            recency = 10.0 / (hours_old + 2)
            engagement = log1p(like_counts.get(post.id, 0)) + 0.5 * log1p(comment_counts.get(post.id, 0))
            movie_genres = cached_genres.get(post.movie_id, set()) if post.movie_id else set()
            genre_boost = 3.0 if (user_genres and movie_genres and user_genres & movie_genres) else 0.0
            return recency + engagement + genre_boost

        scored = sorted(all_rows, key=lambda r: _suggest_score(r[0]), reverse=True)

        # Offset-based pagination for suggested (cursor = str(offset))
        page_offset = 0
        if cursor:
            try:
                page_offset = int(cursor)
            except ValueError:
                page_offset = 0

        page_slice = scored[page_offset: page_offset + limit + 1]
        has_more = len(page_slice) > limit
        page_slice = page_slice[:limit]

        items = [
            _build_feed_post(db, post, author, current_user.id, followed_ids)
            for post, author in page_slice
        ]
        next_cursor = str(page_offset + limit) if has_more else None
        return schemas.FeedResponse(items=items, next_cursor=next_cursor)

    # ── Following feed ────────────────────────────────────────────────────
    query = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(models.Post.club_id.is_(None), models.Post.visible_in_feed == True)  # noqa: E712
    )

    if followed_ids:
        query = query.filter(
            (models.Post.user_id.in_(followed_ids)) | (models.Post.user_id == current_user.id)
        )

    cursor_created_at, cursor_post_id = _decode_feed_cursor(cursor)
    if cursor_created_at and cursor_post_id:
        query = query.filter(
            (models.Post.created_at < cursor_created_at)
            | ((models.Post.created_at == cursor_created_at) & (models.Post.id < cursor_post_id))
        )

    rows = (
        query.order_by(models.Post.created_at.desc(), models.Post.id.desc())
        .limit(limit + 1)
        .all()
    )

    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        _build_feed_post(db, post, author, current_user.id, followed_ids)
        for post, author in rows
    ]

    next_cursor = None
    if has_more and rows:
        last_post = rows[-1][0]
        next_cursor = _encode_feed_cursor(last_post.created_at, last_post.id)

    return schemas.FeedResponse(items=items, next_cursor=next_cursor)


def get_feed_post_by_id(db: Session, current_user: models.User, post_id: int):
    ensure_social_demo_data(db)
    current_user = db.merge(current_user)
    ensure_following_demo_data_for_user(db, current_user)
    followed_ids = _get_followed_ids(db, current_user.id)

    row = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.user_id)
        .filter(models.Post.id == post_id, models.Post.visible_in_feed == True)  # noqa: E712
        .first()
    )
    if not row:
        raise LookupError("Post not found")
    _assert_can_interact_with_club_post(db, current_user, row[0])

    return _build_feed_post(db, row[0], row[1], current_user.id, followed_ids)


def create_feed_post(db: Session, current_user: models.User, payload: schemas.FeedPostCreate):
    ensure_social_demo_data(db)
    current_user = db.merge(current_user)
    text_content = (payload.text_content or "").strip()
    if not text_content:
        raise ValueError("Post cannot be empty")
    if len(text_content) > POST_TEXT_LIMIT:
        raise ValueError(f"Post cannot exceed {POST_TEXT_LIMIT} characters")

    post = models.Post(
        user_id=current_user.id,
        post_type="commentary",
        text_content=text_content,
        sentiment=(payload.sentiment or "").strip() or None,
        visible_in_feed=bool(payload.publish_to_feed),
        contains_spoiler=bool(payload.contains_spoiler),
        would_watch_again=payload.would_watch_again,
        tags_json=json.dumps(_extract_hashtags(text_content)),
        movie_id=payload.movie_id,
        movie_title=payload.movie_title,
        movie_poster_path=payload.movie_poster_path,
        movie_release_date=payload.movie_release_date,
        created_at=_utcnow(),
    )
    db.add(post)
    db.flush()

    _create_mention_notifications(
        db,
        actor_user=current_user,
        content=text_content,
        notification_type="post_mention",
        message_builder=lambda _user: f"@{current_user.username} te marcou em um post.",
        post_id=post.id,
        movie_id=post.movie_id,
        club_id=post.club_id,
    )

    if post.visible_in_feed:
        follower_ids = [
            row.follower_id
            for row in db.query(models.Follow)
            .filter(models.Follow.following_id == current_user.id)
            .all()
        ]
        for follower_id in follower_ids:
            _create_notification(
                db,
                recipient_user_id=follower_id,
                actor_user=current_user,
                notification_type="following_post",
                message=f"@{current_user.username} publicou um novo post ou review.",
                post_id=post.id,
                movie_id=post.movie_id,
                club_id=post.club_id,
            )

    db.commit()
    db.refresh(post)
    followed_ids = _get_followed_ids(db, current_user.id)
    return _build_feed_post(db, post, current_user, current_user.id, followed_ids)


def toggle_post_like(db: Session, current_user: models.User, post_id: int):
    current_user = db.merge(current_user)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise LookupError("Post not found")
    _assert_can_interact_with_club_post(db, current_user, post)

    existing = (
        db.query(models.Like)
        .filter(models.Like.post_id == post_id, models.Like.user_id == current_user.id)
        .first()
    )
    if existing:
        db.delete(existing)
        liked = False
    else:
        db.add(models.Like(post_id=post_id, user_id=current_user.id))
        liked = True
        _create_notification(
            db,
            recipient_user_id=post.user_id,
            actor_user=current_user,
            notification_type="post_like",
            message=f"@{current_user.username} curtiu seu post.",
            post_id=post.id,
            movie_id=post.movie_id,
            club_id=post.club_id,
        )

    db.commit()
    like_count = db.query(models.Like).filter(models.Like.post_id == post_id).count()
    return schemas.LikeResponse(liked=liked, like_count=like_count)


def add_post_comment(db: Session, current_user: models.User, post_id: int, content: str):
    current_user = db.merge(current_user)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise LookupError("Post not found")
    _assert_can_interact_with_club_post(db, current_user, post)

    trimmed_content = content.strip()
    comment = models.Comment(post_id=post_id, user_id=current_user.id, content=trimmed_content)
    db.add(comment)
    db.flush()

    _create_mention_notifications(
        db,
        actor_user=current_user,
        content=trimmed_content,
        notification_type="comment_mention",
        message_builder=lambda _user: f"@{current_user.username} te marcou em um comentario.",
        post_id=post_id,
        comment_id=comment.id,
        movie_id=post.movie_id,
        club_id=post.club_id,
    )

    _create_notification(
        db,
        recipient_user_id=post.user_id,
        actor_user=current_user,
        notification_type="post_comment",
        message=f"@{current_user.username} comentou no seu post.",
        post_id=post_id,
        comment_id=comment.id,
        movie_id=post.movie_id,
        club_id=post.club_id,
    )

    db.commit()
    db.refresh(comment)

    author = get_user_by_id(db, current_user.id)
    followed_ids = _get_followed_ids(db, current_user.id)
    return schemas.FeedComment(
        id=comment.id,
        content=comment.content,
        created_at=comment.created_at,
        author=_build_feed_user_summary(author, followed_ids),
    )


def toggle_saved_post(db: Session, current_user: models.User, post_id: int):
    current_user = db.merge(current_user)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise LookupError("Post not found")
    _assert_can_interact_with_club_post(db, current_user, post)

    existing = (
        db.query(models.SavedPost)
        .filter(models.SavedPost.post_id == post_id, models.SavedPost.user_id == current_user.id)
        .first()
    )
    if existing:
        db.delete(existing)
        saved = False
    else:
        db.add(models.SavedPost(post_id=post_id, user_id=current_user.id))
        saved = True

    db.commit()
    return schemas.SaveResponse(saved=saved)


def _get_conversation_participants(db: Session, conversation_id: int):
    rows = (
        db.query(models.User)
        .join(models.ConversationMember, models.ConversationMember.user_id == models.User.id)
        .filter(models.ConversationMember.conversation_id == conversation_id)
        .order_by(models.User.username.asc())
        .all()
    )
    return [
        schemas.DirectParticipant(
            id=user.id,
            username=user.username,
            profile_image_url=user.profile_image_url,
        )
        for user in rows
    ]


def _build_conversation_title(conversation: models.Conversation, participants: list[schemas.DirectParticipant], current_user_id: int):
    if conversation.title:
        return conversation.title
    if conversation.is_group:
        return "Grupo sem nome"
    others = [participant.username for participant in participants if participant.id != current_user_id]
    return f"@{others[0]}" if others else "Nova conversa"


def _find_direct_conversation(db: Session, user_a_id: int, user_b_id: int):
    conversations = (
        db.query(models.Conversation)
        .join(models.ConversationMember, models.ConversationMember.conversation_id == models.Conversation.id)
        .filter(
            models.Conversation.is_group == False,  # noqa: E712
            models.ConversationMember.user_id.in_([user_a_id, user_b_id]),
        )
        .all()
    )
    for conversation in conversations:
        member_ids = {
            row.user_id
            for row in db.query(models.ConversationMember)
            .filter(models.ConversationMember.conversation_id == conversation.id)
            .all()
        }
        if member_ids == {user_a_id, user_b_id}:
            return conversation
    return None


def _ensure_direct_conversation(db: Session, user_a: models.User, user_b: models.User):
    conversation = _find_direct_conversation(db, user_a.id, user_b.id)
    if conversation:
        return conversation

    conversation = models.Conversation(is_group=False)
    db.add(conversation)
    db.flush()
    now = _utcnow()
    db.add(models.ConversationMember(conversation_id=conversation.id, user_id=user_a.id, last_read_at=now))
    db.add(models.ConversationMember(conversation_id=conversation.id, user_id=user_b.id, last_read_at=now))
    db.commit()
    db.refresh(conversation)
    return conversation


def _build_direct_message(db: Session, message: models.DirectMessage, current_user_id: int):
    sender = get_user_by_id(db, message.sender_user_id)
    shared_post = None
    if message.shared_post_id:
        post_row = (
            db.query(models.Post, models.User)
            .join(models.User, models.User.id == models.Post.user_id)
            .filter(models.Post.id == message.shared_post_id)
            .first()
        )
        if post_row:
            followed_ids = {
                row.following_id
                for row in db.query(models.Follow).filter(models.Follow.follower_id == current_user_id).all()
            }
            shared_post = _build_feed_post(db, post_row[0], post_row[1], current_user_id, followed_ids)

    return schemas.DirectMessage(
        id=message.id,
        content=message.content,
        created_at=message.created_at,
        sender=schemas.DirectParticipant(
            id=sender.id,
            username=sender.username,
            profile_image_url=sender.profile_image_url,
        ),
        shared_post=shared_post,
    )


def get_conversations(db: Session, current_user: models.User):
    current_user = db.merge(current_user)
    conversation_ids = [
        row.conversation_id
        for row in db.query(models.ConversationMember)
        .filter(models.ConversationMember.user_id == current_user.id)
        .all()
    ]
    if not conversation_ids:
        return []

    conversations = (
        db.query(models.Conversation)
        .filter(models.Conversation.id.in_(conversation_ids))
        .order_by(models.Conversation.created_at.desc(), models.Conversation.id.desc())
        .all()
    )

    summaries = []
    for conversation in conversations:
        participants = _get_conversation_participants(db, conversation.id)
        membership = (
            db.query(models.ConversationMember)
            .filter(
                models.ConversationMember.conversation_id == conversation.id,
                models.ConversationMember.user_id == current_user.id,
            )
            .first()
        )
        last_message = (
            db.query(models.DirectMessage)
            .filter(models.DirectMessage.conversation_id == conversation.id)
            .order_by(models.DirectMessage.created_at.desc(), models.DirectMessage.id.desc())
            .first()
        )
        if last_message:
            preview = "Post compartilhado" if last_message.shared_post_id and not last_message.content else (last_message.content or "Mensagem")
            updated_at = last_message.created_at
        else:
            preview = "Conversa criada"
            updated_at = conversation.created_at

        unread_query = db.query(models.DirectMessage).filter(
            models.DirectMessage.conversation_id == conversation.id,
            models.DirectMessage.sender_user_id != current_user.id,
        )
        if membership and membership.last_read_at:
            unread_query = unread_query.filter(models.DirectMessage.created_at > membership.last_read_at)
        unread_count = unread_query.count()

        summaries.append(
            schemas.ConversationSummary(
                id=conversation.id,
                title=_build_conversation_title(conversation, participants, current_user.id),
                is_group=conversation.is_group,
                participants=participants,
                last_message_preview=preview,
                updated_at=updated_at,
                unread_count=unread_count,
            )
        )

    return sorted(summaries, key=lambda item: item.updated_at, reverse=True)


_DM_PAGE_SIZE = 50


def get_conversation_detail(db: Session, current_user: models.User, conversation_id: int):
    current_user = db.merge(current_user)
    membership = (
        db.query(models.ConversationMember)
        .filter(
            models.ConversationMember.conversation_id == conversation_id,
            models.ConversationMember.user_id == current_user.id,
        )
        .first()
    )
    if not membership:
        raise LookupError("Conversation not found")

    conversation = db.query(models.Conversation).filter(models.Conversation.id == conversation_id).first()
    participants = _get_conversation_participants(db, conversation_id)

    # Fetch the most-recent page of messages (DESC) then reverse for chronological display
    recent = (
        db.query(models.DirectMessage)
        .filter(models.DirectMessage.conversation_id == conversation_id)
        .order_by(models.DirectMessage.created_at.desc(), models.DirectMessage.id.desc())
        .limit(_DM_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(recent) > _DM_PAGE_SIZE
    messages = list(reversed(recent[:_DM_PAGE_SIZE]))

    membership.last_read_at = _utcnow()
    db.commit()

    return schemas.ConversationDetail(
        id=conversation.id,
        title=_build_conversation_title(conversation, participants, current_user.id),
        is_group=conversation.is_group,
        participants=participants,
        messages=[_build_direct_message(db, message, current_user.id) for message in messages],
        has_more=has_more,
    )


def get_conversation_messages(
    db: Session, current_user: models.User, conversation_id: int, before_id: int | None = None
):
    current_user = db.merge(current_user)
    membership = (
        db.query(models.ConversationMember)
        .filter(
            models.ConversationMember.conversation_id == conversation_id,
            models.ConversationMember.user_id == current_user.id,
        )
        .first()
    )
    if not membership:
        raise LookupError("Conversation not found")

    q = db.query(models.DirectMessage).filter(
        models.DirectMessage.conversation_id == conversation_id
    )
    if before_id is not None:
        q = q.filter(models.DirectMessage.id < before_id)

    batch = (
        q.order_by(models.DirectMessage.created_at.desc(), models.DirectMessage.id.desc())
        .limit(_DM_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(batch) > _DM_PAGE_SIZE
    messages = list(reversed(batch[:_DM_PAGE_SIZE]))

    return schemas.ConversationMessagesResponse(
        messages=[_build_direct_message(db, m, current_user.id) for m in messages],
        has_more=has_more,
    )


def create_group_conversation(db: Session, current_user: models.User, payload: schemas.ConversationCreate):
    current_user = db.merge(current_user)
    member_usernames = [username for username in payload.member_usernames if username and username != current_user.username]
    users = (
        db.query(models.User)
        .filter(models.User.username.in_(member_usernames))
        .all()
    )
    user_map = {user.username: user for user in users}
    if len(user_map) != len(set(member_usernames)):
        raise LookupError("One or more users were not found")

    conversation = models.Conversation(title=(payload.title or "").strip() or None, is_group=True)
    db.add(conversation)
    db.flush()
    now = _utcnow()
    db.add(models.ConversationMember(conversation_id=conversation.id, user_id=current_user.id, last_read_at=now))
    for member in user_map.values():
        db.add(models.ConversationMember(conversation_id=conversation.id, user_id=member.id, last_read_at=now))
    db.commit()
    return get_conversation_detail(db, current_user, conversation.id)


def create_or_get_direct_conversation(db: Session, current_user: models.User, friend_username: str):
    current_user = db.merge(current_user)
    friend = get_user_by_username(db, friend_username)
    if not friend:
        raise LookupError("User not found")
    if friend.id == current_user.id:
        raise ValueError("You cannot start a conversation with yourself")

    follows_friend = (
        db.query(models.Follow)
        .filter(models.Follow.follower_id == current_user.id, models.Follow.following_id == friend.id)
        .first()
    )
    friend_follows_back = (
        db.query(models.Follow)
        .filter(models.Follow.follower_id == friend.id, models.Follow.following_id == current_user.id)
        .first()
    )
    if not (follows_friend or friend_follows_back):
        raise ValueError("Você só pode enviar mensagens para pessoas que você segue ou que te seguem")

    conversation = _ensure_direct_conversation(db, current_user, friend)
    return get_conversation_detail(db, current_user, conversation.id)


def send_direct_message(db: Session, current_user: models.User, conversation_id: int, content: str, shared_post_id: int | None = None):
    current_user = db.merge(current_user)
    membership = (
        db.query(models.ConversationMember)
        .filter(
            models.ConversationMember.conversation_id == conversation_id,
            models.ConversationMember.user_id == current_user.id,
        )
        .first()
    )
    if not membership:
        raise LookupError("Conversation not found")

    message = models.DirectMessage(
        conversation_id=conversation_id,
        sender_user_id=current_user.id,
        content=content.strip() if content else None,
        shared_post_id=shared_post_id,
    )
    db.add(message)
    db.flush()
    recipient_ids = [
        row.user_id
        for row in db.query(models.ConversationMember)
        .filter(models.ConversationMember.conversation_id == conversation_id)
        .all()
        if row.user_id != current_user.id
    ]
    for recipient_user_id in recipient_ids:
        _create_notification(
            db,
            recipient_user_id=recipient_user_id,
            actor_user=current_user,
            notification_type="direct_message",
            message=f"@{current_user.username} te mandou uma mensagem no direct.",
            conversation_id=conversation_id,
        )
    db.commit()
    db.refresh(message)
    return _build_direct_message(db, message, current_user.id)


def share_post_with_friend(db: Session, current_user: models.User, post_id: int, friend_username: str):
    current_user = db.merge(current_user)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise LookupError("Post not found")
    _assert_can_interact_with_club_post(db, current_user, post)

    friend = get_user_by_username(db, friend_username)
    if not friend:
        raise LookupError("User not found")

    friendship = (
        db.query(models.Friendship)
        .filter(models.Friendship.user_id == current_user.id, models.Friendship.friend_id == friend.id)
        .first()
    )
    if not friendship:
        raise ValueError("You can only share with users already added as friends")

    db.add(
        models.PostShare(
            post_id=post_id,
            sender_user_id=current_user.id,
            recipient_user_id=friend.id,
        )
    )
    conversation = _ensure_direct_conversation(db, current_user, friend)
    db.flush()
    db.add(
        models.DirectMessage(
            conversation_id=conversation.id,
            sender_user_id=current_user.id,
            content=None,
            shared_post_id=post_id,
        )
    )
    db.commit()

    return schemas.ShareResponse(
        message=f"Post shared with @{friend.username}",
        recipient=schemas.FeedUserSummary(
            id=friend.id,
            username=friend.username,
            profile_image_url=friend.profile_image_url,
            is_following=False,
        ),
    )
