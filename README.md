# Moviz — Backend

API do **Moviz — Diário de Filmes**, uma rede social mobile-first para cinéfilos: avalie filmes,
acompanhe o que seus amigos estão vendo e descubra o que assistir a seguir com recomendações
personalizadas e um assistente de cinema com IA.

> App (React + Capacitor): [moviz-frontend](https://github.com/Ricardo2311/moviz-frontend) ·
> Status: MVP completo, em preparação para lançamento na Google Play e App Store.

**Stack:** Python 3.11 · FastAPI · SQLAlchemy · PostgreSQL (SQLite em dev) · Pydantic ·
JWT · scikit-learn/NumPy · Claude API (Anthropic) · TMDB · Railway

---

## Funcionalidades

- **Contas:** cadastro, login por usuário ou e-mail, recuperação de senha por e-mail, onboarding
  de gosto (gêneros, filmes e pessoas favoritas), contas privadas e exclusão de conta.
- **Filmes:** catálogo via TMDB, avaliações de até 5 estrelas, "já assisti", favoritos e
  "onde assistir" no Brasil (JustWatch).
- **Social:** feed (descobrir / seguindo), posts com review, alerta de spoiler, curtidas,
  comentários, posts salvos, **#hashtags** e **@menções**, seguir pessoas e pedidos de follow.
- **Listas, clubes e Direct:** listas públicas/privadas, clubes com papéis (admin, moderador,
  membro) e denúncias, conversas individuais e em grupo.
- **Recomendações** em linhas temáticas, no estilo Netflix, com motor próprio.
- **CineBot:** assistente cinéfilo com IA que conversa sobre cinema e recomenda filmes com base
  no perfil de quem pergunta.
- **Rankings** (global, amigos, país) e notificações in-app + push (FCM).
- **Monetização:** limite diário do CineBot, bônus por anúncio recompensado e assinatura Premium.

## Destaques técnicos

### Motor de recomendação em dois estágios (`app/recommender.py`)
1. **Retrieval:** reduz o catálogo a ~500 candidatos combinando um índice de similaridade
   item-item pré-calculado (scikit-learn), filtro de conteúdo por gênero e idioma, continuação
   de franquias e, como fallback, filmes similares e mais bem avaliados do TMDB.
2. **Ranking:** pontua os candidatos contra um **perfil de gosto** construído a partir das
   avaliações (gênero, keywords, elenco, diretor, década, idioma, franquia e qualidade), com
   **decaimento exponencial por recência** e notas baixas contando como sinal negativo.
3. **Montagem:** 5 linhas temáticas, sem duplicatas e com limite de filmes por diretor.

### CineBot com tool use (`app/chatbot.py`)
- Claude (Haiku 4.5) com uma ferramenta `find_movies` que busca no TMDB por **tema**
  (keywords), **pessoa** (diretor/ator), título ou gênero.
- O perfil do usuário entra no prompt (filmes que amou e que não curtiu, favoritos, diretores,
  décadas, idiomas) **e** no ranking dos resultados da ferramenta (qualidade + relevância +
  afinidade), que devolve ao modelo o motivo de cada filme combinar com aquela pessoa.
- Custo controlado: histórico cortado no servidor, payload enxuto, teto de rodadas de
  ferramenta e limite diário por plano. Erros da API são registrados e o usuário recebe uma
  mensagem amigável.

### Monetização sem confiar no cliente
- **Anúncio recompensado:** o bônus só é creditado pelo callback de verificação server-side do
  AdMob, com **assinatura ECDSA conferida contra as chaves públicas do Google** e deduplicação
  por `transaction_id` (`app/admob.py`).
- **Premium:** o status é sempre confirmado na API do RevenueCat (sync após compra + webhook
  autenticado), nunca a partir do que o app informa (`app/revenuecat.py`).

### Segurança e operação
- JWT (HS256) com `SECRET_KEY` obrigatória (≥ 32 caracteres) e senhas com bcrypt.
- Rate limiting por IP real, confiando em `X-Forwarded-For` apenas de proxies conhecidos.
- Headers de segurança, validação de uploads por *magic bytes*, rotas de admin protegidas.
- Migrações sem Alembic, idempotentes e compatíveis com **SQLite e PostgreSQL**
  (`ensure_schema_compatibility()` em `app/database.py`).
- Deploy contínuo no Railway com healthcheck (`/health`).

## Estrutura

```
app/
├── main.py           # rotas (~100), CORS, rate limit, headers de segurança, startup
├── crud.py           # regras de negócio e acesso a dados
├── models.py         # modelos SQLAlchemy (~30 tabelas)
├── schemas.py        # schemas Pydantic de entrada e saída
├── database.py       # engine, sessão e migrações compatíveis SQLite/PostgreSQL
├── auth.py           # JWT e hash de senha
├── recommender.py    # motor de recomendação
├── chatbot.py        # CineBot (Claude + tool use)
├── admob.py          # verificação de recompensas (SSV)
├── revenuecat.py     # assinaturas Premium
├── tmdb.py           # cliente TMDB + JustWatch
├── storage.py        # upload de imagens (local ou S3/R2)
├── email_service.py  # e-mails de recuperação de senha (Resend/SMTP)
└── test_*.py         # testes automatizados
```

## Rodando localmente

Pré-requisitos: Python 3.11 e uma chave da [API do TMDB](https://developer.themoviedb.org/).

```bash
python -m venv venv
venv\Scripts\activate          # Windows  (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt

cp .env.example .env           # preencha os valores
python -c "import secrets; print(secrets.token_hex(32))"   # gere a SECRET_KEY

uvicorn app.main:app --reload
```

A documentação interativa da API fica em http://localhost:8000/docs.

Variáveis essenciais (lista completa em [`.env.example`](.env.example)):

| Variável | Para quê |
|---|---|
| `SECRET_KEY` | Assinatura dos tokens JWT (obrigatória, ≥ 32 caracteres) |
| `TMDB_API_KEY` / `TMDB_READ_TOKEN` | Catálogo de filmes |
| `DATABASE_URL` | PostgreSQL em produção (padrão: SQLite local) |
| `ANTHROPIC_API_KEY` | CineBot |

## Testes

```bash
python -m unittest discover -s app -t .
```

25 testes com `unittest` + `TestClient` em banco SQLite temporário, cobrindo autenticação,
feed, follow, compartilhamento, listas, limites do CineBot, verificação de anúncios e Premium.
Nenhum teste chama serviços externos.

---

Desenvolvido por **Ricardo Gebara** ([@Ricardo2311](https://github.com/Ricardo2311)).
Código publicado para fins de portfólio — todos os direitos reservados.
