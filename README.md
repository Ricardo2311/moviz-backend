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

2. Set environment variables (we recommend using a `.env` file):

```
DATABASE_URL=postgresql://user:password@localhost/movizdb
TMDB_API_KEY=bb5761808480aa3ebaa814d58b109aad
TMDB_READ_TOKEN=eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJiYjU3NjE4MDg0ODBhYTNlYmFhODE0ZDU4YjEwOWFhZCIsIm5iZiI6MTc3MDQ5MzU4Mi4yNzIsInN1YiI6IjY5ODc5NjhlYWYxMWZkNTczNmMxYzRlMCIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.wUUeXZQZJsAbccO0BaS0KQd4y2hNis8EGWZBr0HPXu4
SECRET_KEY=changeme
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
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


