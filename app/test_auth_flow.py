import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database, models
from app import main as main_module
from app.main import app


class AuthFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = os.path.join(cls.temp_dir.name, 'auth_flow.db')
        cls.test_engine = create_engine(
            f"sqlite:///{cls.test_db_path}",
            connect_args={"check_same_thread": False},
        )
        cls.TestingSessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.test_engine,
        )

        def override_get_db():
            db = cls.TestingSessionLocal()
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
        cls.test_engine.dispose()
        cls.temp_dir.cleanup()

    def setUp(self):
        models.Base.metadata.drop_all(bind=self.test_engine)
        models.Base.metadata.create_all(bind=self.test_engine)

    def test_register_login_and_me(self):
        register = self.client.post(
            '/auth/register',
            json={
                'username': 'tester',
                'email': 'tester@example.com',
                'password': 'secret123',
            },
        )
        self.assertEqual(register.status_code, 200)

        login = self.client.post(
            '/auth/login',
            json={'username': 'tester', 'password': 'secret123'},
        )
        self.assertEqual(login.status_code, 200)
        token = login.json()['access_token']

        me = self.client.get(
            '/auth/me',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['username'], 'tester')

        lists = self.client.get(
            '/api/lists/me',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(lists.status_code, 200)
        self.assertEqual(len(lists.json()), 1)
        self.assertEqual(lists.json()[0]['title'], 'Filmes para assistir depois')
        self.assertEqual(lists.json()[0]['item_count'], 0)

        deleted = self.client.delete(
            f"/api/lists/{lists.json()[0]['id']}",
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(deleted.status_code, 200)

        lists_after_delete = self.client.get(
            '/api/lists/me',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(lists_after_delete.status_code, 200)
        self.assertEqual(len(lists_after_delete.json()), 0)

    def test_login_with_email_and_password(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'tester',
                'email': 'tester@example.com',
                'password': 'secret123',
            },
        )

        login = self.client.post(
            '/auth/login',
            json={'username': 'tester@example.com', 'password': 'secret123'},
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn('access_token', login.json())

    def test_movies_requires_token(self):
        response = self.client.get('/movies')
        self.assertEqual(response.status_code, 401)

    def test_cors_preflight_allows_localhost_port(self):
        response = self.client.options(
            '/auth/register',
            headers={
                'Origin': 'http://localhost:3002',
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'content-type',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get('access-control-allow-origin'),
            'http://localhost:3002',
        )

    def test_add_friend_is_not_automatic_two_way(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'tester',
                'email': 'tester@example.com',
                'password': 'secret123',
            },
        )
        self.client.post(
            '/auth/register',
            json={
                'username': 'ricardo',
                'email': 'ricardo@example.com',
                'password': 'secret123',
            },
        )

        login = self.client.post(
            '/auth/login',
            json={'username': 'ricardo', 'password': 'secret123'},
        )
        token = login.json()['access_token']

        add_friend = self.client.post(
            '/friends',
            json={'username': 'tester'},
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(add_friend.status_code, 200)

        db = self.TestingSessionLocal()
        try:
            tester = db.query(models.User).filter(models.User.username == 'tester').first()
            ricardo = db.query(models.User).filter(models.User.username == 'ricardo').first()
            ricardo_to_tester = db.query(models.Friendship).filter(
                models.Friendship.user_id == ricardo.id,
                models.Friendship.friend_id == tester.id,
            ).first()
            tester_to_ricardo = db.query(models.Friendship).filter(
                models.Friendship.user_id == tester.id,
                models.Friendship.friend_id == ricardo.id,
            ).first()
            self.assertIsNotNone(ricardo_to_tester)
            self.assertIsNone(tester_to_ricardo)
        finally:
            db.close()

    def test_password_reset_flow(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'tester',
                'email': 'tester@example.com',
                'password': 'secret123',
            },
        )

        sent = {}

        def fake_send_password_reset_email(recipient_email, token):
            sent['email'] = recipient_email
            sent['token'] = token

        with patch('app.main.email_service.is_smtp_configured', return_value=True), patch(
            'app.main.email_service.send_password_reset_email',
            fake_send_password_reset_email,
        ):
            response = self.client.post(
                '/auth/forgot-password',
                json={'email': 'tester@example.com'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent['email'], 'tester@example.com')
        self.assertTrue(sent['token'])

        reset = self.client.post(
            '/auth/reset-password',
            json={
                'token': sent['token'],
                'password': 'newsecret456',
            },
        )
        self.assertEqual(reset.status_code, 200)

        login = self.client.post(
            '/auth/login',
            json={'username': 'tester@example.com', 'password': 'newsecret456'},
        )
        self.assertEqual(login.status_code, 200)

    def test_password_reset_fallback_link_without_smtp(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'tester',
                'email': 'tester@example.com',
                'password': 'secret123',
            },
        )

        with patch('app.main.email_service.is_smtp_configured', return_value=False), patch(
            'app.main.email_service.PASSWORD_RESET_DEV_FALLBACK',
            True,
        ):
            response = self.client.post(
                '/auth/forgot-password',
                json={'email': 'tester@example.com'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('reset_link', response.json())
        self.assertIn('/reset-password?token=', response.json()['reset_link'])

    def test_onboarding_persists_preferences_and_marks_user_complete(self):
        register = self.client.post(
            '/auth/register',
            json={
                'username': 'onboarded',
                'email': 'onboarded@example.com',
                'password': 'secret123',
            },
        )
        self.assertEqual(register.status_code, 200)
        self.assertFalse(register.json()['onboarding_completed'])

        login = self.client.post(
            '/auth/login',
            json={'username': 'onboarded', 'password': 'secret123'},
        )
        token = login.json()['access_token']

        response = self.client.post(
            '/api/onboarding',
            json={
                'favorite_genres': ['Drama', 'Sci-Fi'],
                'favorite_movies': [
                    {
                        'movie_id': 11,
                        'title': 'Star Wars',
                        'poster_path': '/poster.jpg',
                        'release_date': '1977-05-25',
                    }
                ],
                'preferred_decades': ['1990s', '2000s'],
                'preferred_languages': ['English', 'Portuguese'],
                'preferred_countries': ['USA', 'Brazil'],
                'favorite_people': [
                    {
                        'person_id': 100,
                        'name': 'Denis Villeneuve',
                        'profile_path': '/person.jpg',
                        'role': 'Director',
                    }
                ],
                'watch_frequency': 'weekly',
            },
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 200)

        onboarding_state = self.client.get(
            '/api/onboarding',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(onboarding_state.status_code, 200)
        self.assertEqual(onboarding_state.json()['favorite_genres'], ['Drama', 'Sci-Fi'])
        self.assertEqual(onboarding_state.json()['preferred_decades'], ['1990s', '2000s'])
        self.assertEqual(onboarding_state.json()['preferred_languages'], ['English', 'Portuguese'])
        self.assertEqual(onboarding_state.json()['preferred_countries'], ['USA', 'Brazil'])
        self.assertEqual(onboarding_state.json()['watch_frequency'], 'weekly')
        self.assertEqual(onboarding_state.json()['favorite_movies'][0]['title'], 'Star Wars')
        self.assertEqual(onboarding_state.json()['favorite_people'][0]['name'], 'Denis Villeneuve')

        me = self.client.get(
            '/auth/me',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()['onboarding_completed'])

        db = self.TestingSessionLocal()
        try:
            user = db.query(models.User).filter(models.User.username == 'onboarded').first()
            preference = db.query(models.UserPreference).filter(models.UserPreference.user_id == user.id).first()
            favorite_movie = db.query(models.FavoriteMovie).filter(models.FavoriteMovie.user_id == user.id).first()
            favorite_person = db.query(models.FavoritePerson).filter(models.FavoritePerson.user_id == user.id).first()
            self.assertIsNotNone(preference)
            self.assertIsNotNone(favorite_movie)
            self.assertIsNotNone(favorite_person)
            self.assertEqual(preference.watch_frequency, 'weekly')
            self.assertEqual(favorite_movie.title, 'Star Wars')
            self.assertEqual(favorite_person.name, 'Denis Villeneuve')
        finally:
            db.close()

    def test_feed_like_comment_and_follow_flow(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'viewer',
                'email': 'viewer@example.com',
                'password': 'secret123',
            },
        )

        login = self.client.post(
            '/auth/login',
            json={'username': 'viewer', 'password': 'secret123'},
        )
        token = login.json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}

        discover_feed = self.client.get('/api/feed?mode=discover', headers=headers)
        self.assertEqual(discover_feed.status_code, 200)
        self.assertGreater(len(discover_feed.json()['items']), 0)

        first_post = discover_feed.json()['items'][0]
        post_id = first_post['id']
        author_id = first_post['author']['id']

        follow = self.client.post(f'/api/users/{author_id}/follow', headers=headers)
        self.assertEqual(follow.status_code, 200)
        self.assertTrue(follow.json()['following'])

        db = self.TestingSessionLocal()
        try:
            viewer = db.query(models.User).filter(models.User.username == 'viewer').first()
            friendship = db.query(models.Friendship).filter(
                models.Friendship.user_id == viewer.id,
                models.Friendship.friend_id == author_id,
            ).first()
            self.assertIsNotNone(friendship)
        finally:
            db.close()

        following_feed = self.client.get('/api/feed?mode=following', headers=headers)
        self.assertEqual(following_feed.status_code, 200)
        self.assertGreater(len(following_feed.json()['items']), 0)

        liked = self.client.post(f'/api/posts/{post_id}/like', headers=headers)
        self.assertEqual(liked.status_code, 200)
        self.assertTrue(liked.json()['liked'])

        commented = self.client.post(
            f'/api/posts/{post_id}/comment',
            json={'content': 'Curti bastante esse post.'},
            headers=headers,
        )
        self.assertEqual(commented.status_code, 200)
        self.assertEqual(commented.json()['content'], 'Curti bastante esse post.')

        comments = self.client.get(f'/api/posts/{post_id}/comments', headers=headers)
        self.assertEqual(comments.status_code, 200)
        self.assertGreaterEqual(len(comments.json()), 1)

        self.client.post(
            '/auth/register',
            json={
                'username': 'bestfriend',
                'email': 'bestfriend@example.com',
                'password': 'secret123',
            },
        )
        self.client.post(
            '/friends',
            json={'username': 'bestfriend'},
            headers=headers,
        )

        saved = self.client.post(f'/api/posts/{post_id}/save', headers=headers)
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()['saved'])

        shared = self.client.post(
            f'/api/posts/{post_id}/share',
            json={'friend_username': 'bestfriend'},
            headers=headers,
        )
        self.assertEqual(shared.status_code, 200)
        self.assertIn('@bestfriend', shared.json()['message'])

        conversations = self.client.get('/api/direct/conversations', headers=headers)
        self.assertEqual(conversations.status_code, 200)
        self.assertGreaterEqual(len(conversations.json()), 1)

        conversation_id = conversations.json()[0]['id']
        detail = self.client.get(f'/api/direct/conversations/{conversation_id}', headers=headers)
        self.assertEqual(detail.status_code, 200)
        self.assertGreaterEqual(len(detail.json()['messages']), 1)

        message = self.client.post(
            f'/api/direct/conversations/{conversation_id}/messages',
            json={'content': 'Te mandei esse post agora.'},
            headers=headers,
        )
        self.assertEqual(message.status_code, 200)
        self.assertEqual(message.json()['content'], 'Te mandei esse post agora.')

        direct = self.client.post(
            '/api/direct/conversations/direct',
            json={'friend_username': 'bestfriend'},
            headers=headers,
        )
        self.assertEqual(direct.status_code, 200)
        self.assertTrue(direct.json()['participants'])

    def test_create_post_hashtag_suggestions_and_mention_notifications(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'writer',
                'email': 'writer@example.com',
                'password': 'secret123',
            },
        )
        self.client.post(
            '/auth/register',
            json={
                'username': 'moviefriend',
                'email': 'moviefriend@example.com',
                'password': 'secret123',
            },
        )
        self.client.post(
            '/auth/register',
            json={
                'username': 'commentfriend',
                'email': 'commentfriend@example.com',
                'password': 'secret123',
            },
        )

        login = self.client.post(
            '/auth/login',
            json={'username': 'writer', 'password': 'secret123'},
        )
        token = login.json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}

        created = self.client.post(
            '/api/posts',
            json={
                'movie_id': 603,
                'movie_title': 'The Matrix',
                'movie_poster_path': '/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg',
                'movie_release_date': '1999-03-30',
                'text_content': 'Revi hoje e segue absurdo. #mindgame #SciFi com @moviefriend',
                'sentiment': 'impactado',
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)
        post_id = created.json()['id']
        self.assertIn('mindgame', created.json()['tags'])
        self.assertIn('scifi', created.json()['tags'])

        hashtag_suggestions = self.client.get(
            '/api/hashtags/suggest?q=sci',
            headers=headers,
        )
        self.assertEqual(hashtag_suggestions.status_code, 200)
        self.assertIn('scifi', [item['tag'] for item in hashtag_suggestions.json()])

        mention_suggestions = self.client.get(
            '/api/mentions/suggest?q=movie',
            headers=headers,
        )
        self.assertEqual(mention_suggestions.status_code, 200)
        self.assertIn('moviefriend', [item['username'] for item in mention_suggestions.json()])

        no_match_mention_suggestions = self.client.get(
            '/api/mentions/suggest?q=usuarioinexistente',
            headers=headers,
        )
        self.assertEqual(no_match_mention_suggestions.status_code, 200)
        self.assertEqual(no_match_mention_suggestions.json(), [])

        mentioned_login = self.client.post(
            '/auth/login',
            json={'username': 'moviefriend', 'password': 'secret123'},
        )
        mentioned_headers = {'Authorization': f"Bearer {mentioned_login.json()['access_token']}"}
        mentioned_profile = self.client.get('/profile', headers=mentioned_headers)
        self.assertEqual(mentioned_profile.status_code, 200)
        self.assertTrue(
            any(
                notification['notification_type'] == 'post_mention'
                and notification['post_id'] == post_id
                for notification in mentioned_profile.json()['notifications']
            )
        )

        writer_profile = self.client.get('/profile', headers=headers)
        self.assertEqual(writer_profile.status_code, 200)
        self.assertTrue(
            any(post['id'] == post_id for post in writer_profile.json()['posts'])
        )

        commented = self.client.post(
            f'/api/posts/{post_id}/comment',
            json={'content': 'Concordo com @commentfriend nessa leitura.'},
            headers=headers,
        )
        self.assertEqual(commented.status_code, 200)

        commentfriend_login = self.client.post(
            '/auth/login',
            json={'username': 'commentfriend', 'password': 'secret123'},
        )
        commentfriend_headers = {'Authorization': f"Bearer {commentfriend_login.json()['access_token']}"}
        notifications = self.client.get('/api/notifications', headers=commentfriend_headers)
        self.assertEqual(notifications.status_code, 200)
        self.assertTrue(
            any(
                item['notification_type'] == 'comment_mention' and item['post_id'] == post_id
                for item in notifications.json()
            )
        )

    def test_my_lists_endpoint_orders_counts_and_enforces_delete_permission(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'listowner',
                'email': 'listowner@example.com',
                'password': 'secret123',
            },
        )
        self.client.post(
            '/auth/register',
            json={
                'username': 'intruder',
                'email': 'intruder@example.com',
                'password': 'secret123',
            },
        )

        owner_login = self.client.post(
            '/auth/login',
            json={'username': 'listowner', 'password': 'secret123'},
        )
        intruder_login = self.client.post(
            '/auth/login',
            json={'username': 'intruder', 'password': 'secret123'},
        )
        owner_headers = {'Authorization': f"Bearer {owner_login.json()['access_token']}"}
        intruder_headers = {'Authorization': f"Bearer {intruder_login.json()['access_token']}"}

        db = self.TestingSessionLocal()
        try:
            owner = db.query(models.User).filter(models.User.username == 'listowner').first()
            older = models.MovieList(
                user_id=owner.id,
                title='Clássicos da madrugada',
                description='Lista antiga',
                visibility='PRIVATE',
            )
            newer = models.MovieList(
                user_id=owner.id,
                title='Favoritos para compartilhar',
                description='Lista mais recente',
                visibility='PUBLIC',
            )
            db.add(older)
            db.add(newer)
            db.flush()
            db.add(models.MovieListItem(list_id=older.id, movie_id=11, position=1))
            db.add(models.MovieListItem(list_id=older.id, movie_id=22, position=2))
            db.add(models.MovieListItem(list_id=newer.id, movie_id=33, position=1))
            older.updated_at = older.created_at
            newer.updated_at = newer.created_at + timedelta(seconds=1)
            db.commit()
            older_id = older.id
            newer_id = newer.id
        finally:
            db.close()

        response = self.client.get('/api/lists/me', headers=owner_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload[0]['id'], newer_id)
        self.assertEqual(payload[0]['item_count'], 1)
        self.assertEqual(payload[1]['id'], older_id)
        self.assertEqual(payload[1]['item_count'], 2)

        forbidden = self.client.delete(f'/api/lists/{older_id}', headers=intruder_headers)
        self.assertEqual(forbidden.status_code, 403)

        detail = self.client.get(f'/api/lists/{older_id}', headers=owner_headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['id'], older_id)
        self.assertEqual(len(detail.json()['items']), 2)

        added = self.client.post(
            f'/api/lists/{older_id}/items',
            json={
                'movies': [
                    {
                        'movie_id': 44,
                        'title': 'Arrival',
                        'poster_path': '/arrival.jpg',
                        'release_date': '2016-11-10',
                    }
                ]
            },
            headers=owner_headers,
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()['item_count'], 3)
        self.assertEqual(added.json()['items'][-1]['title'], 'Arrival')

        added_item_id = added.json()['items'][-1]['id']
        removed = self.client.delete(f'/api/lists/{older_id}/items/{added_item_id}', headers=owner_headers)
        self.assertEqual(removed.status_code, 200)

        forbidden_detail = self.client.get(f'/api/lists/{older_id}', headers=intruder_headers)
        self.assertEqual(forbidden_detail.status_code, 403)

        deleted = self.client.delete(f'/api/lists/{older_id}', headers=owner_headers)
        self.assertEqual(deleted.status_code, 200)

        check = self.client.get('/api/lists/me', headers=owner_headers)
        self.assertEqual(check.status_code, 200)
        self.assertEqual(len(check.json()), 2)
        self.assertTrue(
            any(item['title'] == 'Filmes para assistir depois' for item in check.json())
        )

    def test_create_and_update_list_validates_and_enforces_owner(self):
        self.client.post(
            '/auth/register',
            json={
                'username': 'listmaker',
                'email': 'listmaker@example.com',
                'password': 'secret123',
            },
        )
        self.client.post(
            '/auth/register',
            json={
                'username': 'othermaker',
                'email': 'othermaker@example.com',
                'password': 'secret123',
            },
        )

        owner_login = self.client.post(
            '/auth/login',
            json={'username': 'listmaker', 'password': 'secret123'},
        )
        other_login = self.client.post(
            '/auth/login',
            json={'username': 'othermaker', 'password': 'secret123'},
        )
        owner_headers = {'Authorization': f"Bearer {owner_login.json()['access_token']}"}
        other_headers = {'Authorization': f"Bearer {other_login.json()['access_token']}"}

        invalid = self.client.post(
            '/api/lists',
            json={'title': '', 'description': '', 'visibility': 'PRIVATE', 'allow_social_sharing': False},
            headers=owner_headers,
        )
        self.assertEqual(invalid.status_code, 400)

        created = self.client.post(
            '/api/lists',
            json={
                'title': 'Favoritos da semana',
                'description': 'Uma selecao enxuta',
                'visibility': 'PUBLIC',
                'allow_social_sharing': True,
            },
            headers=owner_headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()['title'], 'Favoritos da semana')
        self.assertTrue(created.json()['allow_social_sharing'])
        self.assertEqual(created.json()['slug'], 'favoritos-da-semana')

        list_id = created.json()['id']
        updated = self.client.put(
            f'/api/lists/{list_id}',
            json={
                'title': 'Favoritos da semana editada',
                'description': 'Descricao ajustada',
                'visibility': 'PRIVATE',
                'allow_social_sharing': False,
            },
            headers=owner_headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['title'], 'Favoritos da semana editada')
        self.assertFalse(updated.json()['allow_social_sharing'])

        forbidden = self.client.put(
            f'/api/lists/{list_id}',
            json={
                'title': 'Tentativa externa',
                'description': '',
                'visibility': 'PUBLIC',
                'allow_social_sharing': True,
            },
            headers=other_headers,
        )
        self.assertEqual(forbidden.status_code, 403)


if __name__ == '__main__':
    unittest.main()
