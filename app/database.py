import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "moviz.db"


def resolve_database_url(raw_url: str | None) -> str:
    if not raw_url:
        return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"

    if raw_url.startswith("sqlite:///./"):
        relative_path = raw_url.removeprefix("sqlite:///./")
        resolved_path = (BACKEND_DIR / relative_path).resolve()
        return f"sqlite:///{resolved_path.as_posix()}"

    return raw_url


DATABASE_URL = resolve_database_url(os.getenv("DATABASE_URL"))

engine = create_engine(DATABASE_URL, connect_args={
                       "check_same_thread": False} if "sqlite" in DATABASE_URL else {})

# Enable WAL journal mode for SQLite so concurrent reads never block writes
if "sqlite" in DATABASE_URL:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA cache_size=-8000")   # 8 MB page cache
        cursor.execute("PRAGMA synchronous=NORMAL")  # safe + faster than FULL
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def ensure_schema_compatibility():
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    with engine.begin() as connection:
        if "profile_image_url" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN profile_image_url VARCHAR"))
        if "bio" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN bio TEXT"))
        if "is_verified_critic" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_verified_critic BOOLEAN DEFAULT 0"))
            connection.execute(text("UPDATE users SET is_verified_critic = 0 WHERE is_verified_critic IS NULL"))
        if "onboarding_completed" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN onboarding_completed BOOLEAN DEFAULT 1"))
            connection.execute(text("UPDATE users SET onboarding_completed = 1 WHERE onboarding_completed IS NULL"))
        if "default_watchlist_created" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN default_watchlist_created BOOLEAN DEFAULT 0"))
            connection.execute(text("UPDATE users SET default_watchlist_created = 0 WHERE default_watchlist_created IS NULL"))
        if "country" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN country VARCHAR"))

        if "conversation_members" in inspector.get_table_names():
            member_columns = {column["name"] for column in inspector.get_columns("conversation_members")}
            if "last_read_at" not in member_columns:
                connection.execute(text("ALTER TABLE conversation_members ADD COLUMN last_read_at DATETIME"))

        if "list_items" in inspector.get_table_names():
            list_item_columns = {column["name"] for column in inspector.get_columns("list_items")}
            if "movie_title" not in list_item_columns:
                connection.execute(text("ALTER TABLE list_items ADD COLUMN movie_title VARCHAR"))
            if "poster_path" not in list_item_columns:
                connection.execute(text("ALTER TABLE list_items ADD COLUMN poster_path VARCHAR"))
            if "release_date" not in list_item_columns:
                connection.execute(text("ALTER TABLE list_items ADD COLUMN release_date VARCHAR"))

        if "lists" in inspector.get_table_names():
            list_columns = {column["name"] for column in inspector.get_columns("lists")}
            if "allow_social_sharing" not in list_columns:
                connection.execute(text("ALTER TABLE lists ADD COLUMN allow_social_sharing BOOLEAN DEFAULT 0"))
                connection.execute(text("UPDATE lists SET allow_social_sharing = 0 WHERE allow_social_sharing IS NULL"))

        if "clubs" in inspector.get_table_names():
            club_columns = {column["name"] for column in inspector.get_columns("clubs")}
            if "genres_json" not in club_columns:
                connection.execute(text("ALTER TABLE clubs ADD COLUMN genres_json TEXT DEFAULT '[]'"))
                connection.execute(text("UPDATE clubs SET genres_json = '[]' WHERE genres_json IS NULL"))

        if "user_preferences" in inspector.get_table_names():
            preference_columns = {column["name"] for column in inspector.get_columns("user_preferences")}
            if "favorite_movie_json" not in preference_columns:
                connection.execute(text("ALTER TABLE user_preferences ADD COLUMN favorite_movie_json TEXT DEFAULT 'null'"))
                connection.execute(text("UPDATE user_preferences SET favorite_movie_json = 'null' WHERE favorite_movie_json IS NULL"))
            if "is_private_account" not in preference_columns:
                connection.execute(text("ALTER TABLE user_preferences ADD COLUMN is_private_account BOOLEAN DEFAULT 0"))
                connection.execute(text("UPDATE user_preferences SET is_private_account = 0 WHERE is_private_account IS NULL"))
            if "notification_preferences_json" not in preference_columns:
                connection.execute(text("ALTER TABLE user_preferences ADD COLUMN notification_preferences_json TEXT DEFAULT '{}'"))
                connection.execute(text("UPDATE user_preferences SET notification_preferences_json = '{}' WHERE notification_preferences_json IS NULL"))

        if "follow_requests" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE follow_requests (
                    id INTEGER PRIMARY KEY,
                    requester_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT uq_follow_request_pair UNIQUE (requester_id, target_user_id)
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_follow_requests_requester_id ON follow_requests (requester_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_follow_requests_target_user_id ON follow_requests (target_user_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_follow_requests_status ON follow_requests (status)"))

        if "posts" in inspector.get_table_names():
            post_columns = {column["name"] for column in inspector.get_columns("posts")}
            if "club_id" not in post_columns:
                connection.execute(text("ALTER TABLE posts ADD COLUMN club_id INTEGER"))
            if "visible_in_feed" not in post_columns:
                connection.execute(text("ALTER TABLE posts ADD COLUMN visible_in_feed BOOLEAN DEFAULT 1"))
                connection.execute(text("UPDATE posts SET visible_in_feed = 1 WHERE visible_in_feed IS NULL"))
            if "contains_spoiler" not in post_columns:
                connection.execute(text("ALTER TABLE posts ADD COLUMN contains_spoiler BOOLEAN DEFAULT 0"))
                connection.execute(text("UPDATE posts SET contains_spoiler = 0 WHERE contains_spoiler IS NULL"))
            if "would_watch_again" not in post_columns:
                connection.execute(text("ALTER TABLE posts ADD COLUMN would_watch_again BOOLEAN"))

        if "notifications" in inspector.get_table_names():
            notification_columns = {column["name"] for column in inspector.get_columns("notifications")}
            if "club_id" not in notification_columns:
                connection.execute(text("ALTER TABLE notifications ADD COLUMN club_id INTEGER"))
            if "movie_id" not in notification_columns:
                connection.execute(text("ALTER TABLE notifications ADD COLUMN movie_id INTEGER"))
            if "conversation_id" not in notification_columns:
                connection.execute(text("ALTER TABLE notifications ADD COLUMN conversation_id INTEGER"))

        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_username_search ON users (username)"))
        if "clubs" in inspector.get_table_names():
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_clubs_name_search ON clubs (name)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_clubs_short_description_search ON clubs (short_description)"))
        if "lists" in inspector.get_table_names():
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_lists_title_search ON lists (title)"))
        if "notifications" in inspector.get_table_names():
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_club_id ON notifications (club_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_movie_id ON notifications (movie_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_conversation_id ON notifications (conversation_id)"))

        if "critic_applications" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE critic_applications (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    motivation TEXT NOT NULL,
                    portfolio_url VARCHAR,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    created_at DATETIME NOT NULL,
                    reviewed_at DATETIME,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_critic_applications_user_id ON critic_applications (user_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_critic_applications_status ON critic_applications (status)"))

        if "post_reports" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE post_reports (
                    id INTEGER PRIMARY KEY,
                    post_id INTEGER NOT NULL,
                    reporter_user_id INTEGER NOT NULL,
                    club_id INTEGER,
                    reason TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY (post_id) REFERENCES posts (id),
                    FOREIGN KEY (reporter_user_id) REFERENCES users (id),
                    FOREIGN KEY (club_id) REFERENCES clubs (id),
                    CONSTRAINT uq_post_report_user UNIQUE (post_id, reporter_user_id)
                )
            """))
        else:
            # Migration: if post_reports was created with club_id NOT NULL, recreate
            # table so reports on non-club posts (club_id = NULL) are allowed.
            pr_cols = {c["name"]: c for c in inspector.get_columns("post_reports")}
            if pr_cols.get("club_id", {}).get("nullable") is False:
                connection.execute(text("""
                    CREATE TABLE post_reports_new (
                        id INTEGER PRIMARY KEY,
                        post_id INTEGER NOT NULL,
                        reporter_user_id INTEGER NOT NULL,
                        club_id INTEGER,
                        reason TEXT NOT NULL,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY (post_id) REFERENCES posts (id),
                        FOREIGN KEY (reporter_user_id) REFERENCES users (id),
                        FOREIGN KEY (club_id) REFERENCES clubs (id),
                        CONSTRAINT uq_post_report_user UNIQUE (post_id, reporter_user_id)
                    )
                """))
                connection.execute(text(
                    "INSERT INTO post_reports_new "
                    "SELECT id, post_id, reporter_user_id, club_id, reason, created_at "
                    "FROM post_reports"
                ))
                connection.execute(text("DROP TABLE post_reports"))
                connection.execute(text("ALTER TABLE post_reports_new RENAME TO post_reports"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_post_reports_post_id ON post_reports (post_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_post_reports_reporter_user_id ON post_reports (reporter_user_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_post_reports_club_id ON post_reports (club_id)"))

        if "recommendation_cache" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE recommendation_cache (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    rows_json TEXT NOT NULL DEFAULT '[]',
                    computed_at DATETIME NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """))
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_recommendation_cache_user_id "
                "ON recommendation_cache (user_id)"
            ))

        if "movie_feature_cache" in inspector.get_table_names():
            mfc_cols = {c["name"] for c in inspector.get_columns("movie_feature_cache")}
            if "tmdb_vote_count" not in mfc_cols:
                connection.execute(text(
                    "ALTER TABLE movie_feature_cache ADD COLUMN tmdb_vote_count INTEGER"
                ))

        if "movie_feature_cache" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE movie_feature_cache (
                    id INTEGER PRIMARY KEY,
                    movie_id INTEGER NOT NULL UNIQUE,
                    title VARCHAR,
                    genres_json TEXT NOT NULL DEFAULT '[]',
                    cast_json TEXT NOT NULL DEFAULT '[]',
                    director VARCHAR,
                    release_year INTEGER,
                    tmdb_vote_avg FLOAT,
                    poster_path VARCHAR,
                    overview TEXT,
                    cached_at DATETIME NOT NULL
                )
            """))
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_movie_feature_cache_movie_id "
                "ON movie_feature_cache (movie_id)"
            ))

        if "device_tokens" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE device_tokens (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    token VARCHAR NOT NULL,
                    platform VARCHAR,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_user_device_token UNIQUE (user_id, token),
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_device_tokens_user_id ON device_tokens (user_id)"))

        if "rated_movies" in inspector.get_table_names():
            # Deduplicate first (keep the most recent rating per user+movie),
            # then enforce the unique constraint as an index (SQLite does not
            # support ALTER TABLE ADD CONSTRAINT, but a UNIQUE index is equivalent).
            connection.execute(text("""
                DELETE FROM rated_movies
                WHERE id NOT IN (
                    SELECT MAX(id) FROM rated_movies GROUP BY user_id, movie_id
                )
            """))
            connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_user_rated_movie
                ON rated_movies (user_id, movie_id)
            """))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
