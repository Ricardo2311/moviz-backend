"""Bloqueio, denúncia e moderação (exigências das lojas para conteúdo gerado por usuários)."""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-unit-tests-0123456789")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud, database, models  # noqa: E402
from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402

ADMIN = {"X-Admin-Key": "chave-admin-de-teste"}


class ModerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.engine = create_engine(
            f"sqlite:///{os.path.join(cls.temp_dir.name, 'moderation.db')}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(cls.engine, "connect")
        def _fk_on(dbapi_conn, _record):  # noqa: ARG001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

        def override_get_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[main_module.get_db] = override_get_db
        app.dependency_overrides[database.get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.temp_dir.cleanup()

    def setUp(self):
        main_module.limiter.reset()
        env = patch.dict(os.environ, {"ENABLE_DEMO_DATA": "false", "ADMIN_SECRET_KEY": ADMIN["X-Admin-Key"]})
        env.start()
        self.addCleanup(env.stop)
        models.Base.metadata.drop_all(bind=self.engine)
        models.Base.metadata.create_all(bind=self.engine)
        self.ana = self._register("ana")
        self.bruno = self._register("bruno")
        self.carla = self._register("carla")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _register(self, username):
        self.client.post(
            "/auth/register",
            json={"username": username, "email": f"{username}@example.com", "password": "secret123"},
        )
        token = self.client.post("/auth/login", json={"username": username, "password": "secret123"}).json()[
            "access_token"
        ]
        with self.Session() as db:
            user_id = db.query(models.User).filter_by(username=username).one().id
        return {"id": user_id, "username": username, "h": {"Authorization": f"Bearer {token}"}}

    def _post(self, user, text="Que filme!"):
        response = self.client.post(
            "/api/posts",
            json={"movie_id": 27205, "movie_title": "A Origem", "text_content": text},
            headers=user["h"],
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def _discover_authors(self, user):
        items = self.client.get("/api/feed?mode=discover", headers=user["h"]).json()["items"]
        return {item["author"]["username"] for item in items}

    # ── bloqueio ────────────────────────────────────────────────────────────

    def test_block_hides_content_and_interaction_both_ways(self):
        post_id = self._post(self.bruno)
        self.client.post(f"/api/users/{self.bruno['id']}/follow", headers=self.ana["h"])
        self.client.post(f"/api/posts/{post_id}/comment", json={"content": "Concordo"}, headers=self.carla["h"])
        self.assertIn("bruno", self._discover_authors(self.carla))

        blocked = self.client.post(f"/api/users/{self.bruno['id']}/block", headers=self.ana["h"])
        self.assertEqual(blocked.status_code, 200)

        # Vale nos dois sentidos: nenhum vê o conteúdo do outro.
        self.assertNotIn("bruno", self._discover_authors(self.ana))
        self.assertEqual(self.client.get(f"/api/posts/{post_id}/comments", headers=self.ana["h"]).status_code, 404)
        self.assertEqual(
            self.client.post(f"/api/posts/{post_id}/comment", json={"content": "oi"}, headers=self.ana["h"]).status_code,
            404,
        )
        # O follow que existia foi desfeito e não pode ser refeito por nenhum lado.
        with self.Session() as db:
            self.assertEqual(db.query(models.Follow).filter_by(follower_id=self.ana["id"]).count(), 0)
        self.assertEqual(self.client.post(f"/api/users/{self.ana['id']}/follow", headers=self.bruno["h"]).status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/direct/conversations/direct", json={"friend_username": "ana"}, headers=self.bruno["h"]
            ).status_code,
            404,
        )
        # Busca e menções não mostram a outra pessoa.
        users = self.client.get("/api/search?q=bru&type=users", headers=self.ana["h"]).json()["users"]
        self.assertEqual(users, [])
        mentions = self.client.get("/api/mentions/suggest?q=an", headers=self.bruno["h"]).json()
        self.assertNotIn("ana", [m["username"] for m in mentions])

        # Perfil: quem foi bloqueado não acha quem bloqueou; quem bloqueou vê só "desbloquear".
        self.assertEqual(self.client.get("/api/users/ana/profile", headers=self.bruno["h"]).status_code, 404)
        own_view = self.client.get("/api/users/bruno/profile", headers=self.ana["h"]).json()
        self.assertTrue(own_view["is_blocked"])
        self.assertEqual(own_view["posts"], [])

        self.assertEqual(
            [u["username"] for u in self.client.get("/api/blocks", headers=self.ana["h"]).json()], ["bruno"]
        )
        self.assertEqual(self.client.get("/api/blocks", headers=self.bruno["h"]).json(), [])  # não revela

        self.client.delete(f"/api/users/{self.bruno['id']}/block", headers=self.ana["h"])
        self.assertIn("bruno", self._discover_authors(self.ana))

    def test_cannot_block_yourself(self):
        response = self.client.post(f"/api/users/{self.ana['id']}/block", headers=self.ana["h"])
        self.assertEqual(response.status_code, 400)

    def test_following_feed_without_follows_shows_only_own_posts(self):
        self._post(self.bruno)
        own_post = self._post(self.ana)
        items = self.client.get("/api/feed?mode=following", headers=self.ana["h"]).json()["items"]
        self.assertEqual([item["id"] for item in items], [own_post])

    # ── denúncias e moderação ─────────────────────────────────────────────

    def _report(self, user, target_type, target_id, reason="spam"):
        return self.client.post(
            "/api/reports",
            json={"target_type": target_type, "target_id": target_id, "reason": reason},
            headers=user["h"],
        )

    def test_report_rules(self):
        post_id = self._post(self.bruno)
        self.assertEqual(self._report(self.ana, "post", post_id).status_code, 200)
        self.assertEqual(self._report(self.ana, "post", post_id).status_code, 200)  # repetir não duplica
        self.assertEqual(self._report(self.bruno, "post", post_id).status_code, 400)  # o próprio conteúdo
        self.assertEqual(self._report(self.ana, "post", 999999).status_code, 404)
        self.assertEqual(self._report(self.ana, "user", self.bruno["id"], reason="harassment").status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.query(models.ContentReport).count(), 2)

    def test_admin_endpoints_require_key(self):
        self.assertEqual(self.client.get("/api/admin/reports").status_code, 403)
        self.assertEqual(
            self.client.get("/api/admin/reports", headers={"X-Admin-Key": "errada"}).status_code, 403
        )
        self.assertEqual(self.client.get("/api/admin/reports", headers=ADMIN).status_code, 200)
        self.assertEqual(self.client.get("/admin/moderacao").status_code, 200)

    def test_three_reports_hide_post_and_dismiss_restores_it(self):
        post_id = self._post(self.bruno)
        dani = self._register("dani")
        for reporter in (self.ana, self.carla, dani):
            self._report(reporter, "post", post_id)
        self.assertNotIn("bruno", self._discover_authors(self._register("edu")))

        report_id = self.client.get("/api/admin/reports", headers=ADMIN).json()[0]["id"]
        resolved = self.client.post(f"/api/admin/reports/{report_id}/resolve", json={"action": "dismiss"}, headers=ADMIN)
        self.assertEqual(resolved.status_code, 200)
        self.assertIn("bruno", self._discover_authors(self.carla))
        self.assertEqual(self.client.get("/api/admin/reports", headers=ADMIN).json(), [])  # todas resolvidas juntas

    def test_dismiss_never_publishes_a_post_the_author_kept_private(self):
        response = self.client.post(
            "/api/posts",
            json={"movie_id": 1, "movie_title": "Filme", "text_content": "só no perfil", "publish_to_feed": False},
            headers=self.bruno["h"],
        )
        post_id = response.json()["id"]
        self._report(self.ana, "post", post_id)
        report_id = self.client.get("/api/admin/reports", headers=ADMIN).json()[0]["id"]
        self.client.post(f"/api/admin/reports/{report_id}/resolve", json={"action": "dismiss"}, headers=ADMIN)
        with self.Session() as db:
            self.assertFalse(db.get(models.Post, post_id).visible_in_feed)

    def test_remove_deletes_reported_comment(self):
        post_id = self._post(self.ana)
        comment = self.client.post(
            f"/api/posts/{post_id}/comment", json={"content": "comentário ofensivo"}, headers=self.bruno["h"]
        ).json()
        self._report(self.ana, "comment", comment["id"], reason="harassment")
        report_id = self.client.get("/api/admin/reports", headers=ADMIN).json()[0]["id"]
        self.client.post(f"/api/admin/reports/{report_id}/resolve", json={"action": "remove"}, headers=ADMIN)
        comments = self.client.get(f"/api/posts/{post_id}/comments", headers=self.ana["h"]).json()
        self.assertEqual(comments, [])

    def test_ban_removes_content_and_locks_the_account(self):
        post_id = self._post(self.bruno)
        self._report(self.ana, "post", post_id, reason="hate")
        report_id = self.client.get("/api/admin/reports", headers=ADMIN).json()[0]["id"]
        self.client.post(f"/api/admin/reports/{report_id}/resolve", json={"action": "ban"}, headers=ADMIN)

        # Token antigo deixa de valer e o login é recusado.
        self.assertEqual(self.client.get("/auth/me", headers=self.bruno["h"]).status_code, 403)
        login = self.client.post("/auth/login", json={"username": "bruno", "password": "secret123"})
        self.assertEqual(login.status_code, 403)
        self.assertEqual(self.client.get("/api/users/bruno/profile", headers=self.ana["h"]).status_code, 404)
        with self.Session() as db:
            self.assertIsNone(db.get(models.Post, post_id))

    def test_cinebot_answer_can_be_reported_and_reviewed(self):
        # Política do Google para IA generativa: denunciar resposta ofensiva dentro do app.
        answer = "Resposta do CineBot que alguém achou ofensiva."
        body = {"target_type": "cinebot", "reason": "hate", "content": answer}
        self.assertEqual(self.client.post("/api/reports", json=body, headers=self.ana["h"]).status_code, 200)
        self.assertEqual(self.client.post("/api/reports", json=body, headers=self.ana["h"]).status_code, 200)
        missing = self.client.post("/api/reports", json={"target_type": "cinebot", "reason": "hate"}, headers=self.ana["h"])
        self.assertEqual(missing.status_code, 400)

        reports = self.client.get("/api/admin/reports", headers=ADMIN).json()
        self.assertEqual(len(reports), 1)  # mesma resposta denunciada duas vezes = uma denúncia
        self.assertEqual(reports[0]["target_type"], "cinebot")
        self.assertEqual(reports[0]["content_snapshot"], answer)
        resolved = self.client.post(
            f"/api/admin/reports/{reports[0]['id']}/resolve", json={"action": "dismiss"}, headers=ADMIN
        )
        self.assertEqual(resolved.status_code, 200)

    def test_no_fake_sponsored_row_without_real_sponsor(self):
        async def fake_rows(db, user_id):
            return [{"row_id": "para-voce", "title": "Para você", "reason": "teste", "movies": []}]

        with patch.object(main_module, "_SPONSOR_LABEL", ""), \
                patch("app.main.recommender.get_recommendation_rows", side_effect=fake_rows):
            rows = self.client.get("/api/recommendations/rows", headers=self.ana["h"]).json()
        self.assertEqual([r["row_id"] for r in rows], ["para-voce"])

    def test_account_with_blocks_and_reports_can_be_deleted(self):
        post_id = self._post(self.bruno)
        self._report(self.ana, "post", post_id)
        self.client.post(f"/api/users/{self.bruno['id']}/block", headers=self.ana["h"])
        self.assertEqual(self.client.delete("/api/profile", headers=self.ana["h"]).status_code, 200)
        self.assertEqual(self.client.delete("/api/profile", headers=self.bruno["h"]).status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.query(models.UserBlock).count(), 0)


if __name__ == "__main__":
    unittest.main()
