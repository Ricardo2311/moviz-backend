import asyncio
import os
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.orm import Session

from . import auth, chatbot, crud, database, email_service, models, recommender, schemas, tmdb
from .database import get_db
import uvicorn


# Only trust X-Forwarded-For / X-Real-IP when the direct connection comes from a
# known reverse proxy.  Without this guard an attacker can spoof their IP and
# bypass the rate limiter entirely.  Set TRUSTED_PROXY_IPS to a comma-separated
# list of proxy CIDRs / IPs in production (e.g. "10.0.0.1,172.18.0.1").
_TRUSTED_PROXY_IPS: frozenset[str] = frozenset(
    ip.strip()
    for ip in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
    if ip.strip()
)


def _get_client_ip(request: Request) -> str:
    client_host: str = (request.client.host if request.client else None) or "unknown"
    # Only honour forwarded headers when the direct TCP peer is a trusted proxy.
    if client_host in _TRUSTED_PROXY_IPS:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return client_host


async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Muitas tentativas. Aguarde um momento e tente novamente."},
        headers={"Retry-After": "60"},
    )


limiter = Limiter(key_func=_get_client_ip)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.Base.metadata.create_all(bind=database.engine)
    database.ensure_schema_compatibility()
    with database.SessionLocal() as db:
        crud.ensure_default_watchlists_for_all_users(db)
        crud.backfill_public_default_watchlists(db)
        crud.dedupe_movie_list_items(db)
        crud.normalize_existing_rating_steps(db)
        crud.backfill_profile_preferences(db)
        crud.ensure_demo_clubs(db)
    yield


app = FastAPI(title="Moviz Backend", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)

UPLOADS_DIR = Path(__file__).resolve().parent.parent / "uploads"

origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3002",
    "http://127.0.0.1:3002",
]
extra_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[*origins, *extra_origins],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Inject hardening headers on every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-XSS-Protection", "0")  # modern browsers ignore it; old ones benefit
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    return response


@app.get("/health", include_in_schema=False)
def health_check():
    return {"status": "ok"}


_LEGAL_CSS = """
  :root { --bg:#f5f7fb; --surface:#fff; --border:#d8e0f0; --text:#0e1320; --text2:#4a5878; --accent:#2f5bef; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }
  .wrap { max-width:700px; margin:0 auto; padding:48px 20px 80px; }
  .back { display:inline-block; font-size:13px; color:var(--accent); text-decoration:none; margin-bottom:32px; }
  h1 { font-size:26px; font-weight:700; margin-bottom:6px; }
  .updated { font-size:13px; color:var(--text2); margin-bottom:36px; padding-bottom:24px; border-bottom:1px solid var(--border); }
  h2 { font-size:16px; font-weight:600; margin:28px 0 10px; }
  h3 { font-size:14px; font-weight:600; color:var(--text2); margin:16px 0 6px; }
  p,li { font-size:14px; color:var(--text2); margin-bottom:8px; }
  ul { padding-left:20px; margin-bottom:8px; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  strong { color:var(--text); }
  @media(prefers-color-scheme:dark){
    :root{ --bg:#0f1420; --surface:#161d2e; --border:#273048; --text:#d8e2f4; --text2:#7a8baa; --accent:#4f7fff; }
  }
"""


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_policy():
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Política de Privacidade — Moviz</title>
  <style>{_LEGAL_CSS}</style>
</head>
<body>
<div class="wrap">
  <a href="https://moviz.app" class="back">← Moviz</a>
  <h1>Política de Privacidade</h1>
  <p class="updated">Última atualização: setembro de 2025</p>

  <h2>1. Sobre o Moviz</h2>
  <p>O Moviz é um aplicativo social de cinema que permite avaliar filmes, compartilhar opiniões, criar listas e se conectar com outros cinéfilos. Esta Política descreve como coletamos, usamos e protegemos seus dados pessoais, em conformidade com a Lei Geral de Proteção de Dados (LGPD — Lei nº 13.709/2018).</p>

  <h2>2. Dados que coletamos</h2>
  <h3>Dados fornecidos por você</h3>
  <ul>
    <li>Nome de usuário e endereço de e-mail (obrigatórios para criar conta)</li>
    <li>Senha (armazenada de forma criptografada — nunca em texto simples)</li>
    <li>Foto de perfil (opcional)</li>
    <li>País de origem (opcional)</li>
  </ul>
  <h3>Dados gerados pelo uso do app</h3>
  <ul>
    <li>Avaliações e reviews de filmes</li>
    <li>Posts e comentários publicados no feed</li>
    <li>Listas de filmes criadas por você</li>
    <li>Conexões sociais (seguidores e seguidos)</li>
    <li>Mensagens diretas trocadas com outros usuários</li>
    <li>Histórico de conversas com o CineBot (assistente de IA)</li>
  </ul>
  <h3>Dados coletados automaticamente</h3>
  <ul>
    <li>Endereço IP (utilizado para controle de segurança e rate limiting)</li>
    <li>Data e hora de acesso</li>
  </ul>

  <h2>3. Como usamos seus dados</h2>
  <ul>
    <li>Criar e manter sua conta no Moviz</li>
    <li>Personalizar recomendações de filmes com base no seu histórico de avaliações</li>
    <li>Exibir seu perfil e conteúdo para outros usuários</li>
    <li>Enviar e-mails transacionais (ex.: redefinição de senha)</li>
    <li>Prevenir fraudes e garantir a segurança da plataforma</li>
    <li>Melhorar o app com base em padrões de uso agregados</li>
  </ul>
  <p>Não vendemos seus dados pessoais a terceiros. Não utilizamos seus dados para publicidade de terceiros.</p>

  <h2>4. Compartilhamento de dados</h2>
  <ul>
    <li><strong>The Movie Database (TMDB):</strong> buscas de filmes são enviadas à API pública do TMDB. Nenhum dado pessoal seu é enviado.</li>
    <li><strong>Anthropic (CineBot):</strong> mensagens ao CineBot são processadas pela API da Anthropic. Consulte a <a href="https://www.anthropic.com/privacy" target="_blank">Política de Privacidade da Anthropic</a>.</li>
    <li><strong>Obrigação legal:</strong> podemos divulgar dados se exigido por lei ou ordem judicial.</li>
  </ul>

  <h2>5. Segurança</h2>
  <p>Adotamos medidas técnicas e organizacionais para proteger seus dados: criptografia de senhas com bcrypt, transmissão via HTTPS, autenticação por token JWT com expiração e controle de acesso por perfil de usuário.</p>

  <h2>6. Seus direitos (LGPD)</h2>
  <ul>
    <li>Acessar os dados que temos sobre você</li>
    <li>Corrigir dados incompletos ou incorretos</li>
    <li>Solicitar a exclusão dos seus dados pessoais</li>
    <li>Revogar seu consentimento a qualquer momento</li>
    <li>Portabilidade dos seus dados</li>
  </ul>
  <p>Para excluir sua conta e todos os dados associados, acesse <strong>Perfil → Configurações → Excluir conta</strong> no app. Para outras solicitações, entre em contato pelo e-mail abaixo.</p>

  <h2>7. Retenção de dados</h2>
  <p>Seus dados são mantidos enquanto sua conta estiver ativa. Ao excluir sua conta, removemos seus dados pessoais em até 30 dias, exceto onde a retenção for exigida por lei.</p>

  <h2>8. Menores de idade</h2>
  <p>O Moviz não é destinado a menores de 13 anos. Se você acredita que uma criança nos forneceu dados, entre em contato para que possamos removê-los.</p>

  <h2>9. Alterações nesta política</h2>
  <p>Podemos atualizar esta Política periodicamente. Notificaremos usuários sobre alterações significativas pelo e-mail cadastrado ou por aviso no app.</p>

  <h2>10. Contato</h2>
  <p>Para exercer seus direitos ou tirar dúvidas sobre privacidade:</p>
  <p><strong>E-mail:</strong> privacidade@moviz.app</p>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms_of_service():
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Termos de Uso — Moviz</title>
  <style>{_LEGAL_CSS}</style>
</head>
<body>
<div class="wrap">
  <a href="https://moviz.app" class="back">← Moviz</a>
  <h1>Termos de Uso</h1>
  <p class="updated">Última atualização: setembro de 2025</p>

  <h2>1. Aceitação dos termos</h2>
  <p>Ao criar uma conta no Moviz ou utilizar o aplicativo, você concorda com estes Termos de Uso. Se não concordar, não utilize o serviço.</p>

  <h2>2. O serviço</h2>
  <p>O Moviz é uma plataforma social de cinema que permite avaliar filmes, criar listas, publicar reviews, seguir outros usuários e interagir com o CineBot, um assistente de inteligência artificial especializado em cinema.</p>

  <h2>3. Criação de conta</h2>
  <ul>
    <li>Você deve ter pelo menos 13 anos de idade para criar uma conta.</li>
    <li>Você é responsável por manter a segurança da sua senha.</li>
    <li>Cada pessoa pode criar apenas uma conta. Contas múltiplas podem ser removidas.</li>
    <li>Informações falsas ou enganosas podem resultar na suspensão da conta.</li>
  </ul>

  <h2>4. Conteúdo do usuário</h2>
  <p>Você é responsável pelo conteúdo que publica no Moviz (reviews, comentários, posts, mensagens). Ao publicar, você garante que tem o direito de fazê-lo, que o conteúdo não viola direitos de terceiros e que não é ilegal, ofensivo ou discriminatório.</p>
  <p>Você mantém os direitos sobre o conteúdo que cria. Ao publicar, concede ao Moviz uma licença não exclusiva para exibir esse conteúdo na plataforma.</p>

  <h2>5. Conduta proibida</h2>
  <ul>
    <li>Publicar conteúdo de ódio, assédio ou bullying</li>
    <li>Fazer spam ou publicidade não autorizada</li>
    <li>Tentar comprometer a segurança da plataforma</li>
    <li>Criar perfis falsos ou se passar por outras pessoas</li>
    <li>Coletar dados de outros usuários sem autorização</li>
    <li>Usar o app para fins comerciais sem autorização prévia</li>
  </ul>

  <h2>6. CineBot (assistente de IA)</h2>
  <p>O CineBot é um assistente de IA que fornece recomendações e informações sobre cinema. Suas respostas são geradas automaticamente e podem conter imprecisões. O Moviz não se responsabiliza por decisões tomadas com base nas respostas do CineBot.</p>

  <h2>7. Propriedade intelectual</h2>
  <p>O nome Moviz, logotipo, design e funcionalidades do app são propriedade do Moviz. Dados de filmes são fornecidos pelo <a href="https://www.themoviedb.org" target="_blank">The Movie Database (TMDB)</a>.</p>

  <h2>8. Disponibilidade do serviço</h2>
  <p>O Moviz é fornecido "como está". Podemos modificar, suspender ou encerrar funcionalidades a qualquer momento, com aviso prévio quando possível.</p>

  <h2>9. Exclusão de conta</h2>
  <p>Você pode excluir sua conta a qualquer momento em <strong>Perfil → Configurações → Excluir conta</strong>. Consulte nossa <a href="/privacy">Política de Privacidade</a> para detalhes sobre remoção de dados.</p>

  <h2>10. Lei aplicável</h2>
  <p>Estes Termos são regidos pelas leis da República Federativa do Brasil. Qualquer disputa será submetida ao foro da comarca de São Paulo, SP.</p>

  <h2>11. Contato</h2>
  <p><strong>E-mail:</strong> contato@moviz.app</p>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
def reset_password_page(token: str = ""):
    """Web page served via e-mail link so users can set a new password."""
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Redefinir senha — Moviz</title>
  <style>
    :root{{--bg:#f5f7fb;--surface:#fff;--border:#d8e0f0;--text:#0e1320;--text2:#4a5878;--accent:#2f5bef;--err:#c02020;--ok:#1a7040;}}
    @media(prefers-color-scheme:dark){{:root{{--bg:#0f1420;--surface:#161d2e;--border:#273048;--text:#d8e2f4;--text2:#7a8baa;--accent:#4f7fff;--err:#ff7a7a;--ok:#50cc88;}}}}
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}}
    .card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:36px 32px;width:100%;max-width:400px;}}
    .logo{{font-size:22px;font-weight:800;color:var(--accent);margin-bottom:24px;letter-spacing:-0.5px;}}
    h1{{font-size:18px;font-weight:700;margin-bottom:6px;}}
    p.sub{{font-size:13px;color:var(--text2);margin-bottom:24px;}}
    label{{display:block;font-size:13px;font-weight:600;margin-bottom:6px;}}
    input{{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font-size:14px;outline:none;}}
    input:focus{{border-color:var(--accent);}}
    .gap{{margin-top:14px;}}
    button{{margin-top:20px;width:100%;padding:11px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;}}
    button:disabled{{opacity:0.5;cursor:default;}}
    .msg{{margin-top:16px;font-size:13px;padding:10px 12px;border-radius:8px;display:none;}}
    .msg.ok{{background:rgba(50,200,100,0.12);color:var(--ok);border:1px solid var(--ok);display:block;}}
    .msg.err{{background:rgba(200,50,50,0.12);color:var(--err);border:1px solid var(--err);display:block;}}
  </style>
</head>
<body>
<div class="card">
  <div class="logo">Moviz</div>
  <h1>Redefinir senha</h1>
  <p class="sub">Digite sua nova senha abaixo.</p>
  <form id="form">
    <label for="pw">Nova senha</label>
    <input id="pw" type="password" minlength="8" maxlength="128" placeholder="Mínimo 8 caracteres" required>
    <div class="gap">
      <label for="pw2">Confirmar senha</label>
      <input id="pw2" type="password" minlength="8" maxlength="128" placeholder="Repita a senha" required>
    </div>
    <button type="submit" id="btn">Salvar nova senha</button>
    <div class="msg" id="msg"></div>
  </form>
</div>
<script>
  var TOKEN = {repr(token)};
  document.getElementById('form').addEventListener('submit', async function(e) {{
    e.preventDefault();
    var pw = document.getElementById('pw').value;
    var pw2 = document.getElementById('pw2').value;
    var msg = document.getElementById('msg');
    var btn = document.getElementById('btn');
    msg.className = 'msg';
    if (pw !== pw2) {{ msg.textContent = 'As senhas não coincidem.'; msg.className = 'msg err'; return; }}
    if (pw.length < 8) {{ msg.textContent = 'A senha deve ter pelo menos 8 caracteres.'; msg.className = 'msg err'; return; }}
    btn.disabled = true;
    btn.textContent = 'Salvando...';
    try {{
      var r = await fetch('/auth/reset-password', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token: TOKEN, password: pw}})
      }});
      var data = await r.json();
      if (r.ok) {{
        msg.textContent = '✓ Senha alterada com sucesso! Você já pode entrar no app.';
        msg.className = 'msg ok';
        document.getElementById('form').querySelectorAll('input,button').forEach(function(el){{el.disabled=true;}});
      }} else {{
        msg.textContent = data.detail || 'Link inválido ou expirado. Solicite um novo no app.';
        msg.className = 'msg err';
        btn.disabled = false;
        btn.textContent = 'Salvar nova senha';
      }}
    }} catch(err) {{
      msg.textContent = 'Erro de conexão. Tente novamente.';
      msg.className = 'msg err';
      btn.disabled = false;
      btn.textContent = 'Salvar nova senha';
    }}
  }});
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/auth/register", response_model=schemas.User)
@limiter.limit("5/minute")
def register(request: Request, user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = crud.get_user_by_username(db, username=user.username)
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    email_user = crud.get_user_by_email(db, email=user.email)
    if email_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    return crud.create_user(db=db, user=user)


@app.post("/auth/login", response_model=schemas.Token)
@limiter.limit("10/minute")
def login(request: Request, form_data: schemas.UserLogin, db: Session = Depends(get_db)):
    user = auth.authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth.create_access_token(data={"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/api/onboarding", response_model=schemas.MessageResponse)
def save_onboarding(
    payload: schemas.OnboardingPayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    crud.save_onboarding_preferences(db, current_user, payload)
    return {"message": "Onboarding saved successfully"}


@app.get("/api/onboarding", response_model=schemas.OnboardingState)
def read_onboarding(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.get_onboarding_preferences(db, current_user)


@app.get("/api/browse")
async def browse_movies(
    section: str = "top_rated",
    current_user: models.User = Depends(auth.get_current_active_user),
):
    if section not in ("top_rated", "recent"):
        raise HTTPException(status_code=400, detail="Invalid section. Use top_rated or recent.")
    if section == "top_rated":
        page1, page2 = await asyncio.gather(
            tmdb.fetch_top_rated_movies(page=1),
            tmdb.fetch_top_rated_movies(page=2),
        )
    else:
        page1, page2 = await asyncio.gather(
            tmdb.fetch_recent_movies(page=1),
            tmdb.fetch_recent_movies(page=2),
        )
    results = (page1.get("results") or []) + (page2.get("results") or [])
    return {"results": results[:30]}


@app.get("/api/genres")
async def get_movie_genres(
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return await tmdb.fetch_genre_list()


@app.get("/api/movies/{movie_id}/watch-status", response_model=schemas.MovieWatchStatusResponse)
def get_watch_status(
    movie_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.get_movie_watch_status(db, current_user, movie_id)


@app.post("/api/movies/mark-watched", response_model=schemas.MovieWatchStatusResponse)
def mark_watched(
    payload: schemas.MarkWatchedPayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.mark_movie_watched(
        db, current_user, payload.movie_id, payload.title, payload.poster_path, payload.release_date
    )


@app.delete("/api/movies/{movie_id}/mark-watched", response_model=schemas.MovieWatchStatusResponse)
def unmark_watched(
    movie_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.unmark_movie_watched(db, current_user, movie_id)


@app.get("/api/movies/search", response_model=list[schemas.SearchMovieResult])
async def search_movies(
    q: str,
    current_user: models.User = Depends(auth.get_current_active_user),
):
    payload = await tmdb.search_movies(q)
    return [
        schemas.SearchMovieResult(
            id=movie.get("id"),
            title=movie.get("title"),
            poster_path=movie.get("poster_path"),
            release_date=movie.get("release_date"),
            genre_ids=movie.get("genre_ids") or [],
        )
        for movie in (payload.get("results") or [])[:12]
        if movie.get("id") and movie.get("title")
    ]


@app.get("/api/people/search", response_model=list[schemas.SearchPersonResult])
async def search_people(
    q: str,
    current_user: models.User = Depends(auth.get_current_active_user),
):
    payload = await tmdb.search_people(q)
    return [
        schemas.SearchPersonResult(
            id=person.get("id"),
            name=person.get("name"),
            profile_path=person.get("profile_path"),
            known_for_department=person.get("known_for_department"),
        )
        for person in (payload.get("results") or [])[:12]
        if person.get("id") and person.get("name")
    ]


@app.get("/api/search", response_model=schemas.SearchResultsResponse)
async def universal_search(
    q: str = "",
    type: str = "all",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    normalized_type = (type or "all").lower()
    if normalized_type not in {"all", "movies", "users", "clubs", "lists"}:
        raise HTTPException(status_code=400, detail="Invalid search type")

    query = (q or "").strip()
    movie_results = []
    if query and normalized_type in {"all", "movies"}:
        payload = await tmdb.search_movies(query)
        movie_results = [
            schemas.SearchMovieResult(
                id=movie.get("id"),
                title=movie.get("title"),
                poster_path=movie.get("poster_path"),
                release_date=movie.get("release_date"),
                genre_ids=movie.get("genre_ids") or [],
            )
            for movie in (payload.get("results") or [])[:12]
            if movie.get("id") and movie.get("title")
        ]

    return schemas.SearchResultsResponse(
        query=query,
        type=normalized_type,
        movies=movie_results,
        users=crud.search_users(db, current_user, query, limit=8) if query and normalized_type in {"all", "users"} else [],
        clubs=crud.search_clubs(db, current_user, query, limit=8) if query and normalized_type in {"all", "clubs"} else [],
        lists=crud.search_lists(db, current_user, query, limit=8) if query and normalized_type in {"all", "lists"} else [],
    )


@app.get("/api/trending", response_model=schemas.TrendingResponse)
async def read_trending(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    popular_payload = await tmdb.fetch_popular_movies(page=1)
    movies = [
        schemas.SearchMovieResult(
            id=movie.get("id"),
            title=movie.get("title"),
            poster_path=movie.get("poster_path"),
            release_date=movie.get("release_date"),
            genre_ids=movie.get("genre_ids") or [],
        )
        for movie in (popular_payload.get("results") or [])
        if movie.get("id") and movie.get("title")
    ]

    fresh_movies = sorted(
        movies,
        key=lambda item: item.release_date or "",
        reverse=True,
    )[:10]
    top_rated_movies = sorted(
        (popular_payload.get("results") or []),
        key=lambda item: item.get("vote_average") or 0,
        reverse=True,
    )[:10]

    return schemas.TrendingResponse(
        movies=[
            schemas.TrendingMovieCategory(
                key="popular",
                title="Em alta agora",
                subtitle="Os filmes mais comentados do momento.",
                items=movies[:10],
            ),
            schemas.TrendingMovieCategory(
                key="top-rated",
                title="Melhores notas do momento",
                subtitle="Recorte dos populares com as maiores notas.",
                items=[
                    schemas.SearchMovieResult(
                        id=movie.get("id"),
                        title=movie.get("title"),
                        poster_path=movie.get("poster_path"),
                        release_date=movie.get("release_date"),
                        genre_ids=movie.get("genre_ids") or [],
                    )
                    for movie in top_rated_movies
                    if movie.get("id") and movie.get("title")
                ],
            ),
            schemas.TrendingMovieCategory(
                key="fresh",
                title="Lancamentos recentes",
                subtitle="Para quem quer descobrir o que acabou de chegar.",
                items=fresh_movies,
            ),
        ],
        users=crud.get_trending_users(db, current_user, limit=8),
        clubs=crud.get_trending_clubs(db, current_user, limit=8),
        lists=crud.get_trending_lists(db, current_user, limit=8),
        trending_posts=crud.get_trending_posts(db, current_user, limit=8),
        trending_hashtags=crud.get_trending_hashtags(db, limit=10),
    )


@app.get("/api/feed", response_model=schemas.FeedResponse)
def read_feed(
    mode: str = "discover",
    cursor: str | None = None,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    normalized_mode = mode.lower()
    if normalized_mode not in {"following", "discover", "suggested"}:
        raise HTTPException(status_code=400, detail="Invalid feed mode")

    safe_limit = max(1, min(limit, 20))
    return crud.get_feed(db, current_user, normalized_mode, cursor=cursor, limit=safe_limit)


@app.post("/api/posts", response_model=schemas.FeedPost)
@limiter.limit("20/minute")
def create_feed_post(
    request: Request,
    payload: schemas.FeedPostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.create_feed_post(db, current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/posts/{post_id}", response_model=schemas.FeedPost)
def read_feed_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_feed_post_by_id(db, current_user, post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/hashtags/suggest", response_model=list[schemas.HashtagSuggestion])
def suggest_hashtags(
    q: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    del current_user
    return crud.get_hashtag_suggestions(db, q)


@app.get("/api/hashtags/{tag}/posts", response_model=schemas.FeedResponse)
def get_hashtag_posts(
    tag: str,
    cursor: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 40))
    return crud.get_posts_by_hashtag(db, current_user, tag, cursor=cursor, limit=safe_limit)


@app.get("/api/mentions/suggest", response_model=list[schemas.MentionSuggestion])
def suggest_mentions(
    q: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.get_mention_suggestions(db, current_user, q)


@app.get("/api/notifications/unread-count")
def read_notifications_unread_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    from sqlalchemy import func as sqlfunc
    base_q = db.query(models.Notification).filter(
        models.Notification.user_id == current_user.id,
        models.Notification.is_read == False,  # noqa: E712
    )
    count = base_q.count()
    latest = None
    if count > 0:
        row = base_q.order_by(models.Notification.created_at.desc()).first()
        if row:
            latest = {
                "id": row.id,
                "message": row.message,
                "notification_type": row.notification_type,
            }
    return {"count": count, "latest": latest}


@app.post("/api/devices/register", status_code=204)
def register_device_token(
    token: str = Form(...),
    platform: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    crud.register_device_token(db, current_user.id, token, platform or None)


@app.get("/api/notifications", response_model=list[schemas.NotificationResponse])
def read_notifications(
    type: str | None = None,
    limit: int = 60,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 200))
    return crud.get_notifications(db, current_user, limit=safe_limit, filter_type=type)


@app.patch("/api/notifications/read-all")
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    db.query(models.Notification).filter(
        models.Notification.user_id == current_user.id,
        models.Notification.is_read == False,  # noqa: E712
    ).update({"is_read": True})
    db.commit()
    return {"message": "ok"}


@app.patch("/api/notifications/{notification_id}/read", response_model=schemas.NotificationResponse)
def update_notification_read_state(
    notification_id: int,
    payload: schemas.NotificationReadPayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.mark_notification_as_read(db, current_user, notification_id, is_read=payload.is_read)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/clubs", response_model=schemas.ClubSearchResponse)
def read_clubs(
    q: str | None = None,
    genres: str | None = None,
    limit: int = 24,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    parsed_genres = [
        item.strip()
        for item in (genres or "").split(",")
        if item.strip()
    ]
    return crud.get_club_hub(db, current_user, query=q, genres=parsed_genres, limit=limit)


@app.post("/api/clubs", response_model=schemas.ClubSummary)
def create_club(
    payload: schemas.ClubCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.create_club(db, current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clubs/{club_id}/join", response_model=schemas.ClubJoinResponse)
def join_club(
    club_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.join_club(db, current_user, club_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/clubs/{club_id}/membership", response_model=schemas.MessageResponse)
def leave_club(
    club_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.leave_club(db, current_user, club_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/clubs/{club_id}/join-request", response_model=schemas.MessageResponse)
def cancel_join_request(
    club_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.cancel_club_join_request(db, current_user, club_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/clubs/{club_id}", response_model=schemas.ClubDetailResponse)
def read_club_detail(
    club_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_club_detail(db, current_user, club_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/clubs/{club_id}/posts", response_model=schemas.FeedResponse)
def read_club_posts(
    club_id: int,
    cursor: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 40))
    try:
        return crud.get_club_posts_page(db, current_user, club_id, cursor=cursor, limit=safe_limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/clubs/{club_id}/posts", response_model=schemas.FeedPost)
def publish_club_post(
    club_id: int,
    payload: schemas.FeedPostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.create_club_post(db, current_user, club_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clubs/{club_id}/requests/{request_id}/approve", response_model=schemas.MessageResponse)
def approve_club_join_request(
    club_id: int,
    request_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.review_club_join_request(db, current_user, club_id, request_id, approve=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/clubs/{club_id}/requests/{request_id}/reject", response_model=schemas.MessageResponse)
def reject_club_join_request(
    club_id: int,
    request_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.review_club_join_request(db, current_user, club_id, request_id, approve=False)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.patch("/api/clubs/{club_id}/members/{member_user_id}/role", response_model=schemas.MessageResponse)
def edit_club_member_role(
    club_id: int,
    member_user_id: int,
    payload: schemas.ClubMemberRoleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.update_club_member_role(db, current_user, club_id, member_user_id, payload.role)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/clubs/{club_id}/members/{member_user_id}", response_model=schemas.MessageResponse)
def delete_club_member(
    club_id: int,
    member_user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.remove_club_member(db, current_user, club_id, member_user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/lists/me", response_model=list[schemas.MovieListSummary])
def read_my_lists(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.get_my_lists(db, current_user)


@app.post("/api/lists", response_model=schemas.MovieListSummary)
def create_my_list(
    payload: schemas.MovieListCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.create_movie_list(db, current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/lists/{list_id}", response_model=schemas.MovieListDetail)
def read_my_list_detail(
    list_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_movie_list_detail(db, current_user, list_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.delete("/api/clubs/{club_id}/posts/{post_id}", response_model=schemas.MessageResponse)
def delete_club_post(
    club_id: int,
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.remove_club_post(db, current_user, club_id, post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/clubs/{club_id}/posts/{post_id}/report", response_model=schemas.MessageResponse)
def report_club_post_endpoint(
    club_id: int,
    post_id: int,
    payload: schemas.PostReportCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.report_club_post(db, current_user, club_id, post_id, payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/clubs/{club_id}/reports", response_model=list[schemas.PostReportResponse])
def get_club_reports_endpoint(
    club_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_club_post_reports(db, current_user, club_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/users/{username}/lists/{list_id}", response_model=schemas.MovieListDetail)
def read_user_public_list_detail(
    username: str,
    list_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    user = crud.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not crud.can_view_user_private_profile(db, user.id, current_user.id):
        raise HTTPException(status_code=403, detail="This private profile hides its lists")
    try:
        return crud.get_accessible_movie_list_detail(db, current_user, list_id, username)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.put("/api/lists/{list_id}", response_model=schemas.MovieListSummary)
def edit_my_list(
    list_id: int,
    payload: schemas.MovieListUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.update_movie_list(db, current_user, list_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 403 if "permission" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/lists/{list_id}/items", response_model=schemas.MovieListDetail)
def append_movies_to_list(
    list_id: int,
    payload: schemas.MovieListItemsCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.add_movies_to_list(db, current_user, list_id, payload.movies)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/lists/{list_id}/items/{item_id}", response_model=schemas.MessageResponse)
def remove_movie_from_list(
    list_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.remove_movie_from_list(db, current_user, list_id, item_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.put("/api/lists/{list_id}/items/reorder", response_model=schemas.MovieListDetail)
def reorder_list_movies(
    list_id: int,
    payload: schemas.MovieListReorderPayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.reorder_movie_list_items(db, current_user, list_id, payload.item_ids)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/lists/{list_id}", response_model=schemas.MessageResponse)
def remove_my_list(
    list_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.delete_movie_list(db, current_user, list_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/posts/{post_id}/like", response_model=schemas.LikeResponse)
def like_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.toggle_post_like(db, current_user, post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/posts/{post_id}/comment", response_model=schemas.FeedComment)
@limiter.limit("30/minute")
def comment_post(
    request: Request,
    post_id: int,
    payload: schemas.FeedCommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Comment cannot be empty")

    try:
        return crud.add_post_comment(db, current_user, post_id, payload.content)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/posts/{post_id}/comments", response_model=list[schemas.FeedComment])
def read_post_comments(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_post_comments(db, current_user, post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/posts/{post_id}/save", response_model=schemas.SaveResponse)
def save_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.toggle_saved_post(db, current_user, post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/posts/{post_id}/share", response_model=schemas.ShareResponse)
def share_post(
    post_id: int,
    payload: schemas.FeedShareCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.share_post_with_friend(db, current_user, post_id, payload.friend_username)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/direct/conversations", response_model=list[schemas.ConversationSummary])
def read_direct_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.get_conversations(db, current_user)


@app.post("/api/direct/conversations", response_model=schemas.ConversationDetail)
def create_direct_group(
    payload: schemas.ConversationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.create_group_conversation(db, current_user, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/direct/conversations/direct", response_model=schemas.ConversationDetail)
def create_direct_conversation(
    payload: schemas.DirectConversationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.create_or_get_direct_conversation(db, current_user, payload.friend_username)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/direct/conversations/{conversation_id}", response_model=schemas.ConversationDetail)
def read_direct_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_conversation_detail(db, current_user, conversation_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/direct/conversations/{conversation_id}/messages", response_model=schemas.DirectMessage)
def post_direct_message(
    conversation_id: int,
    payload: schemas.DirectMessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        return crud.send_direct_message(db, current_user, conversation_id, payload.content)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/direct/conversations/{conversation_id}/messages", response_model=schemas.ConversationMessagesResponse)
def get_conversation_messages(
    conversation_id: int,
    before_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_conversation_messages(db, current_user, conversation_id, before_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/users/{user_id}/follow", response_model=schemas.FollowResponse)
def follow_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        follow_result = crud.follow_user(db, current_user, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return schemas.FollowResponse(
        following=bool(follow_result["following"]),
        pending=bool(follow_result["pending"]),
        user=schemas.FeedUserSummary(
            id=follow_result["target"].id,
            username=follow_result["target"].username,
            profile_image_url=follow_result["target"].profile_image_url,
            is_following=bool(follow_result["following"]),
        ),
    )


@app.delete("/api/users/{user_id}/follow", response_model=schemas.FollowResponse)
def unfollow_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        unfollowed_user = crud.unfollow_user(db, current_user, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return schemas.FollowResponse(
        following=False,
        user=schemas.FeedUserSummary(
            id=unfollowed_user.id,
            username=unfollowed_user.username,
            profile_image_url=unfollowed_user.profile_image_url,
            is_following=False,
        ),
    )


@app.delete("/api/users/{user_id}/follower", response_model=schemas.MessageResponse)
def remove_follower(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        removed_user = crud.remove_follower(db, current_user, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"message": f"@{removed_user.username} removido dos seus seguidores."}


@app.post("/api/users/{user_id}/follow-request/approve", response_model=schemas.MessageResponse)
def approve_follow_request(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.review_follow_request(db, current_user, user_id, approve=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/users/{user_id}/follow-request/reject", response_model=schemas.MessageResponse)
def reject_follow_request(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.review_follow_request(db, current_user, user_id, approve=False)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/auth/forgot-password", response_model=schemas.MessageResponse)
@limiter.limit("3/minute")
def forgot_password(request: Request, payload: schemas.PasswordResetRequest, db: Session = Depends(get_db)):
    user = crud.get_user_by_email(db, payload.email)
    if user:
        token = auth.create_password_reset_token(user.email)
        reset_link = email_service.build_password_reset_link(token)
        if email_service.is_smtp_configured():
            email_service.send_password_reset_email(user.email, token)
        elif email_service.PASSWORD_RESET_DEV_FALLBACK:
            return {
                "message": "SMTP is not configured. Use the link below to reset the password in local development.",
                "reset_link": reset_link,
            }

    return {
        "message": "If the email exists, a password reset link has been sent."
    }


@app.post("/auth/reset-password", response_model=schemas.MessageResponse)
def reset_password(payload: schemas.PasswordResetConfirm, db: Session = Depends(get_db)):
    email = auth.verify_password_reset_token(payload.token)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = crud.get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    crud.reset_user_password(db, user, payload.password)
    return {"message": "Password updated successfully"}


@app.get("/auth/me", response_model=schemas.User)
def read_current_user(current_user: models.User = Depends(auth.get_current_active_user)):
    return current_user


@app.post("/api/chat", response_model=schemas.ChatResponse)
@limiter.limit("30/hour")
async def chat_endpoint(
    request: Request,
    body: schemas.ChatRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return await chatbot.chat(
        db,
        current_user.id,
        body.message,
        [{"role": m.role, "content": m.content} for m in body.history],
    )


@app.get("/api/recommendations/rows", response_model=list[schemas.RecommendationRow])
async def get_recommendation_rows(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return await recommender.get_recommendation_rows(db, current_user.id)


@app.get("/api/recommendations", response_model=list[schemas.RecommendationItem])
async def get_recommendations(
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return await recommender.get_recommendations(db, current_user.id, limit=min(limit, 50))


@app.get("/api/movies")
async def get_movies(
    page: int = 1,
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return await tmdb.fetch_popular_movies(page=page)


@app.get("/api/movies/{movie_id}")
async def get_movie_details(
    movie_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    details = await tmdb.fetch_movie_details(movie_id)
    details["user_rating"] = crud.get_user_rating_for_movie(db, current_user.id, movie_id)
    details.update(crud.get_average_rating_for_movie(db, movie_id))
    details["friend_ratings"] = crud.get_friend_ratings_for_movie(db, current_user.id, movie_id)
    details.update(crud.get_movie_reviews(db, current_user, movie_id))
    details["user_lists"] = crud.get_lists_containing_movie(
        db,
        current_user.id,
        movie_id,
        details.get("title"),
        details.get("release_date"),
        details.get("poster_path"),
    )
    return details


@app.post("/api/movies/{movie_id}/rating", response_model=schemas.RatedMovie)
def save_movie_rating(
    movie_id: int,
    payload: schemas.MovieRatingCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.rate_movie(db, current_user, movie_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/profile", response_model=schemas.ProfileResponse)
def get_profile(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.build_profile_response(db, current_user, viewer_user_id=current_user.id)


@app.patch("/api/profile", response_model=schemas.ProfileResponse)
def update_profile(
    username: str = Form(...),
    bio: str = Form(default=""),
    country: str = Form(default=""),
    favorite_genres_json: str = Form(default="[]"),
    favorite_movie_json: str = Form(default="null"),
    is_private_account: str = Form(default="false"),
    notification_preferences_json: str = Form(default="{}"),
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    profile_image: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        profile_image_url = None
        if profile_image is not None and profile_image.filename:
            profile_image_url = crud.save_profile_image(profile_image)
        try:
            favorite_genres = [
                item.strip()
                for item in json.loads(favorite_genres_json or "[]")
                if isinstance(item, str) and item.strip()
            ]
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid favorite genres payload") from exc
        try:
            parsed_favorite_movie = json.loads(favorite_movie_json or "null")
            favorite_movie = (
                schemas.FavoriteMovieInput(**parsed_favorite_movie)
                if isinstance(parsed_favorite_movie, dict)
                else None
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid favorite movie payload") from exc
        try:
            notification_preferences = json.loads(notification_preferences_json or "{}")
            if not isinstance(notification_preferences, dict):
                raise ValueError("Invalid notification preferences payload")
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid notification preferences payload") from exc
        return crud.update_profile_details(
            db,
            current_user,
            username=username,
            profile_image_url=profile_image_url,
            bio=bio,
            country=(country or "").strip() or None,
            favorite_genres=favorite_genres,
            favorite_movie=favorite_movie,
            is_private_account=str(is_private_account).strip().lower() in {"1", "true", "on", "yes"},
            notification_preferences=notification_preferences,
            current_password=(current_password or "").strip() or None,
            new_password=(new_password or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/profile", response_model=schemas.MessageResponse)
def delete_my_account(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    crud.delete_user_account(db, current_user)
    return {"message": "Conta excluida com sucesso."}


@app.get("/api/profile/ratings", response_model=list[schemas.RatedMovie])
def get_my_ratings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    return crud.get_all_ratings(db, current_user.id)


@app.get("/api/profile/posts", response_model=list[schemas.FeedPost])
def get_my_posts_paginated(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    followed_ids = crud._get_followed_ids(db, current_user.id)
    return crud.get_user_feed_posts(db, current_user.id, current_user.id, followed_ids, limit=limit, skip=skip)


@app.get("/api/profile/saved", response_model=list[schemas.FeedPost])
def get_my_saved_posts_paginated(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    followed_ids = crud._get_followed_ids(db, current_user.id)
    return crud.get_saved_feed_posts(db, current_user.id, followed_ids, limit=limit, skip=skip)


@app.get("/api/users/{username}/profile", response_model=schemas.ProfileResponse)
def get_user_profile(
    username: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    profile = crud.get_user_profile_by_username(db, username, viewer_user_id=current_user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    return profile


@app.get("/api/users/{username}/ratings", response_model=list[schemas.RatedMovie])
def get_user_ratings(
    username: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    user = crud.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not crud.can_view_user_private_profile(db, user.id, current_user.id):
        raise HTTPException(status_code=403, detail="This private profile hides its ratings")
    crud.ensure_demo_ratings(db, user)
    return crud.get_all_ratings(db, user.id)


@app.get("/api/users/{username}/connections", response_model=schemas.FollowConnectionListResponse)
def get_user_connections(
    username: str,
    kind: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        return crud.get_follow_connections(db, username, kind, viewer_user_id=current_user.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 403 if "private profile" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/critic-application", response_model=schemas.CriticApplicationResponse)
def submit_critic_application(
    payload: schemas.CriticApplicationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    if not payload.motivation.strip():
        raise HTTPException(status_code=400, detail="A motivacao nao pode ser vazia.")
    try:
        application = crud.submit_critic_application(
            db, current_user, payload.motivation, payload.portfolio_url
        )
        return schemas.CriticApplicationResponse(
            id=application.id,
            status=application.status,
            motivation=application.motivation,
            portfolio_url=application.portfolio_url,
            created_at=application.created_at,
            reviewed_at=application.reviewed_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/critic-application", response_model=schemas.CriticApplicationResponse | None)
def get_my_critic_application(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    application = crud.get_my_critic_application(db, current_user)
    if not application:
        return None  # 200 + null — frontend treats null as "no application"
    return schemas.CriticApplicationResponse(
        id=application.id,
        status=application.status,
        motivation=application.motivation,
        portfolio_url=application.portfolio_url,
        created_at=application.created_at,
        reviewed_at=application.reviewed_at,
    )


@app.get("/api/admin/critic-applications", response_model=list[schemas.CriticApplicationResponse])
def admin_list_critic_applications(
    x_admin_key: str | None = Header(None),
    db: Session = Depends(get_db),
):
    admin_key = os.getenv("ADMIN_SECRET_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    applications = crud.get_pending_critic_applications(db)
    result = []
    for app_obj in applications:
        user = db.query(models.User).filter(models.User.id == app_obj.user_id).first()
        result.append(schemas.CriticApplicationResponse(
            id=app_obj.id,
            status=app_obj.status,
            motivation=app_obj.motivation,
            portfolio_url=app_obj.portfolio_url,
            created_at=app_obj.created_at,
            reviewed_at=app_obj.reviewed_at,
            applicant=schemas.FeedUserSummary(
                id=user.id,
                username=user.username,
                profile_image_url=user.profile_image_url,
                is_verified_critic=bool(user.is_verified_critic),
            ) if user else None,
        ))
    return result


@app.patch("/api/admin/critic-applications/{application_id}/review", response_model=schemas.CriticApplicationResponse)
def admin_review_critic_application(
    application_id: int,
    payload: schemas.AdminCriticApplicationReview,
    x_admin_key: str | None = Header(None),
    db: Session = Depends(get_db),
):
    admin_key = os.getenv("ADMIN_SECRET_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        app_obj = crud.review_critic_application(db, application_id, payload.approve)
        user = db.query(models.User).filter(models.User.id == app_obj.user_id).first()
        return schemas.CriticApplicationResponse(
            id=app_obj.id,
            status=app_obj.status,
            motivation=app_obj.motivation,
            portfolio_url=app_obj.portfolio_url,
            created_at=app_obj.created_at,
            reviewed_at=app_obj.reviewed_at,
            applicant=schemas.FeedUserSummary(
                id=user.id,
                username=user.username,
                profile_image_url=user.profile_image_url,
                is_verified_critic=bool(user.is_verified_critic),
            ) if user else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/rankings/global", response_model=schemas.GlobalRankingResponse)
def read_global_ranking(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 100))
    return crud.get_global_ranking(db, current_user, limit=safe_limit)


@app.get("/api/rankings/friends", response_model=schemas.GlobalRankingResponse)
def read_friends_ranking(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 100))
    return crud.get_friends_ranking(db, current_user, limit=safe_limit)


@app.get("/api/rankings/country", response_model=schemas.GlobalRankingResponse)
def read_country_ranking(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 100))
    return crud.get_country_ranking(db, current_user, limit=safe_limit)


@app.get("/api/critics", response_model=schemas.CriticListResponse)
def read_critics(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 100))
    critics = crud.get_verified_critics(db, current_user, limit=safe_limit)
    return schemas.CriticListResponse(critics=critics, total=len(critics))


@app.get("/api/critics/{username}", response_model=schemas.ProfileResponse)
def read_critic_profile(
    username: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    profile = crud.get_user_profile_by_username(db, username, viewer_user_id=current_user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="Critic not found")
    if not profile.user.is_verified_critic:
        raise HTTPException(status_code=404, detail="Critic not found")
    return profile


@app.get("/api/critics/{username}/reviews", response_model=schemas.FeedResponse)
def read_critic_reviews(
    username: str,
    cursor: str | None = None,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    safe_limit = max(1, min(limit, 20))
    try:
        return crud.get_critic_reviews_by_username(db, username, current_user, cursor=cursor, limit=safe_limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/admin/users/{user_id}/critic-status", response_model=schemas.MessageResponse)
def admin_toggle_critic_status(
    user_id: int,
    payload: schemas.AdminCriticTogglePayload,
    x_admin_key: str | None = Header(None),
    db: Session = Depends(get_db),
):
    admin_key = os.getenv("ADMIN_SECRET_KEY", "")
    if not admin_key:
        raise HTTPException(status_code=501, detail="Admin key not configured")
    if x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    try:
        user = crud.set_critic_status(db, user_id, payload.is_verified_critic)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    action = "promovido a" if payload.is_verified_critic else "removido de"
    return {"message": f"@{user.username} {action} critico verificado."}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
