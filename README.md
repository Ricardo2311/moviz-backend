# Moviz Backend

This is a FastAPI backend for the Moviz application. It provides user authentication (registration/login) and integrates with the TMDB API to fetch movies for the React frontend.

## Requirements

- Python 3.9+
- PostgreSQL database (or you can modify to use SQLite)

## Setup

1. Create a virtual environment and install requirements:

```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

2. Set environment variables: copy `.env.example` to `.env` and fill in the values
   (never commit real keys — `.env` is gitignored):

```
DATABASE_URL=postgresql://user:password@localhost/movizdb
TMDB_API_KEY=your_tmdb_api_key_here
TMDB_READ_TOKEN=your_tmdb_read_token_here
SECRET_KEY=replace_with_secrets_token_hex_32
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=10080
```

3. Create the database and run migrations (or simply let SQLAlchemy create tables).

4. Start the server:

```bash
uvicorn app.main:app --reload
```

The backend will be available at `http://localhost:8000`.

## API Endpoints

- `POST /auth/register` – register a new user.
- `POST /auth/login` – login and receive a JWT access token.
- `GET /movies` – fetch popular movies from TMDB (requires authorization).

### Example React Integration

To authenticate from the existing React frontend, send requests to the backend (running at `http://localhost:8000` by default):

```js
// register
await fetch("http://localhost:8000/auth/register", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username, email, password }),
});

// login
const res = await fetch("http://localhost:8000/auth/login", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username, password }),
});
const data = await res.json();
const token = data.access_token;

// fetch movies (store token in localStorage or context)
const movies = await fetch("http://localhost:8000/movies", {
  headers: { Authorization: `Bearer ${token}` },
});
const json = await movies.json();
```

Be sure the React app sends the token in the `Authorization` header for protected routes.


