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


if __name__ == "__main__":
    unittest.main()
