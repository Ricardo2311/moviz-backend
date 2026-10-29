"""Testes do CineBot que não chamam a Anthropic nem o TMDB."""

import unittest

from app import chatbot


class RecommendationIntentTests(unittest.TestCase):
    def test_detects_recommendation_requests(self):
        for message in [
            "Me recomenda algo pra ver hoje à noite",
            "Qual o melhor Kurosawa pra começar?",
            "Tô a fim de uma comédia",
            "Quero um filme parecido com Seven",
            "Me indica uns filmes do Fincher",
        ]:
            with self.subTest(message=message):
                self.assertTrue(chatbot._wants_recommendation(message))

    def test_questions_about_cinema_do_not_force_search(self):
        for message in [
            "O que é um plano-sequência e qual filme usa isso muito bem?",
            "Quem dirigiu Cidade de Deus?",
            "Por que o Kubrick é considerado perfeccionista?",
        ]:
            with self.subTest(message=message):
                self.assertFalse(chatbot._wants_recommendation(message))


class MovieCardMatchingTests(unittest.TestCase):
    FOUND = [
        {"movie_id": 680, "title": "Pulp Fiction: Tempo de Violência"},
        {"movie_id": 106646, "title": "O Lobo de Wall Street"},
        {"movie_id": 1, "title": "Up: Altas Aventuras"},
        {"movie_id": 680, "title": "Pulp Fiction: Tempo de Violência"},
    ]

    def test_matches_full_and_shortened_titles_once(self):
        text = "Vai de **O Lobo de Wall Street** ou **Pulp Fiction**, do Tarantino."
        ids = [m["movie_id"] for m in chatbot._mentioned_movies(text, self.FOUND)]
        self.assertEqual(ids, [680, 106646])

    def test_short_prefix_does_not_match_common_words(self):
        # "Up" (antes de ':') é curto demais: não pode casar com "up" no meio de outra palavra.
        text = "Esse filme é um setup perfeito para a noite."
        self.assertEqual(chatbot._mentioned_movies(text, self.FOUND), [])


if __name__ == "__main__":
    unittest.main()
