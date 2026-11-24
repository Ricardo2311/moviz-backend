"""Página web de exclusão de conta (exigência do Google Play: funcionar sem o app)."""

import os
import tempfile
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-unit-tests-0123456789")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import database, models  # noqa: E402
from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402


class AccountDeletionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.engine = create_engine(
            f"sqlite:///{os.path.join(cls.temp_dir.name, 'deletion.db')}",
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
        models.Base.metadata.drop_all(bind=self.engine)
        models.Base.metadata.create_all(bind=self.engine)
        self.client.post(
            "/auth/register",
            json={"username": "saindo", "email": "saindo@example.com", "password": "secret123"},
        )

    def _users(self):
        with self.Session() as db:
            return db.query(models.User).count()

    def test_page_is_public_and_explains_what_is_deleted(self):
        page = self.client.get("/excluir-conta")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Excluir minha conta", page.text)
        self.assertIn("não pode ser desfeita", page.text)

    def test_deletes_with_email_and_password(self):
        response = self.client.post(
            "/excluir-conta",
            data={"identifier": "saindo@example.com", "password": "secret123", "confirm": "sim"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Conta excluída", response.text)
        self.assertEqual(self._users(), 0)

    def test_wrong_password_keeps_account_with_generic_message(self):
        response = self.client.post(
            "/excluir-conta", data={"identifier": "saindo", "password": "errada123", "confirm": "sim"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("Usuário, e-mail ou senha incorretos.", response.text)
        self.assertEqual(self._users(), 1)

    def test_requires_explicit_confirmation(self):
        response = self.client.post("/excluir-conta", data={"identifier": "saindo", "password": "secret123"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._users(), 1)

    def test_rate_limit_blocks_password_guessing(self):
        codes = [
            self.client.post(
                "/excluir-conta", data={"identifier": "saindo", "password": f"chute{i}", "confirm": "sim"}
            ).status_code
            for i in range(12)
        ]
        self.assertIn(429, codes)
        self.assertEqual(self._users(), 1)

    def test_privacy_and_terms_link_to_the_page(self):
        for path in ("/privacy", "/terms"):
            with self.subTest(path=path):
                self.assertIn('href="/excluir-conta"', self.client.get(path).text)


if __name__ == "__main__":
    unittest.main()
