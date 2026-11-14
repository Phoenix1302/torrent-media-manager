"""Unit tests for title parser, cleaner, and media type recommendation heuristics."""

import unittest
from pathlib import Path
import sys

# Ensure app directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from title_cleaner import (
    CandidateList,
    MediaRecommendation,
    clean_release_title,
    clean_segment,
    classify_language,
    extract_title_candidates,
    recommend_media_type,
    split_compound_title,
)


class TestTitleCleaner(unittest.TestCase):
    def test_compound_title_split_and_clean_example_parasyte(self):
        raw = (
            "Паразит: Учение о жизни (ТВ) / Kiseijuu: Sei no Kakuritsu (TV) / "
            "Parasyte: The Maxim [TV] [24 из 24] [RUS(int)] [2014, приключения, фантастика, ужасы...]"
        )
        candidates = extract_title_candidates(raw)

        # Verify candidate list properties
        self.assertIsInstance(candidates, list)
        self.assertIsInstance(candidates, CandidateList)
        self.assertGreaterEqual(len(candidates), 3)

        # Primary is Russian
        self.assertEqual(candidates[0], "Паразит: Учение о жизни")
        self.assertEqual(candidates.russian, "Паразит: Учение о жизни")

        # Secondary is English
        self.assertEqual(candidates[1], "Parasyte: The Maxim")
        self.assertEqual(candidates.english, "Parasyte: The Maxim")

        # Romaji / Original
        self.assertIn("Kiseijuu: Sei no Kakuritsu", candidates)
        self.assertEqual(candidates.romaji, "Kiseijuu: Sei no Kakuritsu")

        # Media type recommendation
        rec = recommend_media_type(raw, candidates)
        self.assertEqual(rec.category, "anime")
        self.assertIn("аниме", rec.reason.lower())

    def test_pipe_and_dash_separators(self):
        # Separated by pipe
        raw_pipe = "Атака титанов | Shingeki no Kyojin | Attack on Titan [01-25 из 25]"
        cands_pipe = extract_title_candidates(raw_pipe)
        self.assertEqual(cands_pipe.russian, "Атака титанов")
        self.assertEqual(cands_pipe.english, "Attack on Titan")
        self.assertEqual(cands_pipe.romaji, "Shingeki no Kyojin")

        rec_pipe = recommend_media_type(raw_pipe, cands_pipe)
        self.assertEqual(rec_pipe.category, "anime")

        # Separated by em-dash
        raw_dash = "Чернобыль — Chernobyl (2019) [LostFilm]"
        cands_dash = extract_title_candidates(raw_dash)
        self.assertEqual(cands_dash.russian, "Чернобыль")
        self.assertEqual(cands_dash.english, "Chernobyl")

        rec_dash = recommend_media_type(raw_dash, cands_dash)
        self.assertEqual(rec_dash.category, "serials")

    def test_tag_cleaning(self):
        # Cleaning TV tags, episode counts, seasons, groups, codecs
        seg1 = "Клинок, рассекающий демонов (ТВ-1) [01-26 из 26] [AniLibria] [1080p] [HEVC]"
        self.assertEqual(clean_segment(seg1), "Клинок, рассекающий демонов")

        seg2 = "Parasyte: The Maxim [TV] [24 из 24] [RUS(int)] [2014, приключения, фантастика]"
        self.assertEqual(clean_segment(seg2), "Parasyte: The Maxim")

        seg3 = "Breaking Bad [Сезон 1] (2008) WEB-DL 1080p [LostFilm]"
        self.assertEqual(clean_segment(seg3), "Breaking Bad")

        seg4 = "[AniDUB] Магическая битва / Jujutsu Kaisen [01-24 из 24] [BDRip 1080p]"
        cands4 = extract_title_candidates(seg4)
        self.assertIn("Магическая битва", cands4)
        self.assertIn("Jujutsu Kaisen", cands4)

    def test_language_classification(self):
        self.assertEqual(classify_language("Во все тяжкие"), "russian")
        self.assertEqual(classify_language("Breaking Bad"), "english")
        self.assertEqual(classify_language("Kiseijuu: Sei no Kakuritsu"), "romaji")
        self.assertEqual(classify_language("Shingeki no Kyojin"), "romaji")
        self.assertEqual(classify_language("Attack on Titan"), "english")
        self.assertEqual(classify_language("進撃の巨人"), "original")

    def test_media_type_recommendation_heuristics(self):
        # 1. Anime
        anime_raw = "[AniLibria] Человек-бензопила / Chainsaw Man [01-12 из 12] [WEBRip 1080p]"
        rec_anime = recommend_media_type(anime_raw)
        self.assertEqual(rec_anime.category, "anime")
        self.assertEqual(rec_anime.media_type, "anime")

        # 2. Cartoons / Mults
        cartoon_raw = "Король Лев / The Lion King (1994) BDRip [Мультфильм]"
        rec_cartoon = recommend_media_type(cartoon_raw)
        self.assertEqual(rec_cartoon.category, "mults")
        self.assertEqual(rec_cartoon.media_type, "cartoon")

        # 3. TV Series
        tv_raw = "Игра престолов / Game of Thrones [Сезон 1] [LostFilm]"
        rec_tv = recommend_media_type(tv_raw)
        self.assertEqual(rec_tv.category, "serials")
        self.assertEqual(rec_tv.media_type, "tv")

        # 4. Movies
        movie_raw = "Интерстеллар / Interstellar (2014) BDRip 1080p"
        rec_movie = recommend_media_type(movie_raw)
        self.assertEqual(rec_movie.category, "films")
        self.assertEqual(rec_movie.media_type, "movie")

    def test_clean_release_title(self):
        raw = "Паразит: Учение о жизни (ТВ) / Kiseijuu: Sei no Kakuritsu (TV) [24 из 24]"
        primary = clean_release_title(raw)
        self.assertEqual(primary, "Паразит: Учение о жизни")

    def test_media_recommendation_unpacking(self):
        rec = MediaRecommendation("anime", "because reasons", "anime")
        category, reason = rec
        self.assertEqual(category, "anime")
        self.assertEqual(reason, "because reasons")
        self.assertEqual(rec.category, "anime")
        self.assertEqual(rec.reason, "because reasons")
        self.assertEqual(rec.media_type, "anime")


if __name__ == "__main__":
    unittest.main()
