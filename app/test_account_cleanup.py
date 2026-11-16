"""Exclusão de conta e limpeza de dados fictícios, com chaves estrangeiras ATIVAS.

O SQLite só aplica FKs com PRAGMA foreign_keys=ON; sem isso, exclusões que quebram no
PostgreSQL de produção passariam nos testes.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-unit-tests-0123456789")

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud, models, schemas  # noqa: E402


class AccountCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.engine = create_engine(
            f"sqlite:///{os.path.join(cls.temp_dir.name, 'cleanup.db')}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(cls.engine, "connect")
        def _fk_on(dbapi_conn, _record):  # noqa: ARG001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.temp_dir.cleanup()

    def setUp(self):
        models.Base.metadata.drop_all(bind=self.engine)
        models.Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)

    def _user(self, username, email=None):
        return crud.create_user(
            self.db,
            schemas.UserCreate(
                username=username, email=email or f"{username}@example.com", password="secret123"
            ),
        )

    def _club(self, creator, name, members=()):
        club = models.Club(
            creator_user_id=creator.id, name=name, slug=name.lower().replace(" ", "-"),
            short_description="Clube de teste",
        )
        self.db.add(club)
        self.db.flush()
        self.db.add(models.ClubMembership(club_id=club.id, user_id=creator.id, role="owner"))
        for user, role in members:
            self.db.add(models.ClubMembership(club_id=club.id, user_id=user.id, role=role))
        self.db.commit()
        return club.id

    def test_signup_does_not_create_fake_ratings(self):
        user = self._user("novato")
        self.assertEqual(self.db.query(models.RatedMovie).filter_by(user_id=user.id).count(), 0)

    def test_deleting_club_creator_transfers_club_to_highest_role(self):
        owner, member, moderator = self._user("dono"), self._user("membro"), self._user("moderador")
        club_id = self._club(owner, "Clube Noir", members=[(member, "member"), (moderator, "moderator")])

        crud.delete_user_account(self.db, owner)

        club = self.db.get(models.Club, club_id)
        self.assertIsNotNone(club)
        self.assertEqual(club.creator_user_id, moderator.id)
        new_owner = self.db.query(models.ClubMembership).filter_by(club_id=club_id, user_id=moderator.id).one()
        self.assertEqual(new_owner.role, "owner")

    def test_deleting_sole_club_member_removes_club_and_its_posts(self):
        owner = self._user("solitario")
        club_id = self._club(owner, "Clube Vazio")
        self.db.add(models.Post(user_id=owner.id, post_type="text", club_id=club_id))
        self.db.commit()

        crud.delete_user_account(self.db, owner)

        self.assertIsNone(self.db.get(models.Club, club_id))
        self.assertEqual(self.db.query(models.Post).count(), 0)

    def test_deleting_account_with_comments_notifications_and_shared_posts(self):
        author, fan = self._user("autor"), self._user("fanzao")
        post = models.Post(user_id=author.id, post_type="text")
        self.db.add(post)
        self.db.flush()
        comment = models.Comment(post_id=post.id, user_id=fan.id, content="Que filme!")
        self.db.add(comment)
        self.db.flush()
        self.db.add(models.Notification(
            user_id=author.id, actor_user_id=fan.id, notification_type="post_comment",
            message="comentou", post_id=post.id, comment_id=comment.id,
        ))
        conversation = models.Conversation()
        self.db.add(conversation)
        self.db.flush()
        self.db.add(models.DirectMessage(
            conversation_id=conversation.id, sender_user_id=fan.id, content="olha", shared_post_id=post.id,
        ))
        self.db.commit()

        crud.delete_user_account(self.db, fan)      # comentário com notificação apontando para ele
        crud.delete_user_account(self.db, author)   # post compartilhado no Direct

        self.assertEqual(self.db.query(models.User).count(), 0)

    def test_purge_removes_demo_accounts_clubs_and_seeded_ratings_only(self):
        demo = self._user("screenqueen", email="screenqueen@moviz.demo")
        real = self._user("cinefilo_real")
        club_id = self._club(demo, "Clube Fake", members=[(real, "member")])
        self.db.add(models.Post(user_id=demo.id, post_type="text"))
        self.db.add(models.Follow(follower_id=real.id, following_id=demo.id))
        # Bloco de avaliações falsas que o cadastro antigo inseria + uma avaliação real.
        for movie in crud.DEMO_RATED_MOVIES:
            self.db.add(models.RatedMovie(
                user_id=real.id, movie_id=movie["movie_id"], title=movie["title"],
                rating=crud.normalize_rating_to_half_step(movie["rating"]),
            ))
        self.db.add(models.RatedMovie(user_id=real.id, movie_id=27205, title="A Origem", rating=4.5))
        self.db.commit()

        with patch.dict(os.environ, {"ENABLE_DEMO_DATA": "false"}):
            result = crud.purge_demo_data(self.db)
            again = crud.purge_demo_data(self.db)

        self.assertEqual(result["demo_users"], 1)
        self.assertEqual(result["demo_clubs"], 1)
        self.assertEqual(result["users_with_fake_ratings"], 1)
        self.assertFalse(any(again.values()), "segunda execução não deve apagar nada")
        self.assertIsNone(self.db.get(models.Club, club_id))
        self.assertIsNotNone(self.db.get(models.User, real.id))
        remaining = [r.title for r in self.db.query(models.RatedMovie).filter_by(user_id=real.id)]
        self.assertEqual(remaining, ["A Origem"])

    def test_purge_does_nothing_when_demo_mode_is_on(self):
        self._user("framehunter", email="framehunter@moviz.demo")
        with patch.dict(os.environ, {"ENABLE_DEMO_DATA": "true"}):
            self.assertEqual(crud.purge_demo_data(self.db), {})
        self.assertEqual(self.db.query(models.User).count(), 1)


if __name__ == "__main__":
    unittest.main()
