"""Unit tests for TMDB multi-candidate fallback search and /api/analyze integration."""

import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

# Ensure app directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import main
import tmdb


@pytest.mark.asyncio
async def test_tmdb_fallback_secondary_candidate(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "dummy_key")

    searched_queries = []

    async def fake_single_search(query: str, media_type: str) -> list[dict]:
        searched_queries.append(query)
        # Primary returns 0 results, secondary returns match
        if query == "Паразит: Учение о жизни":
            return []
        elif query == "Parasyte: The Maxim":
            return [{"id": 61459, "title": "Паразит: Учение о жизни", "original_title": "Parasyte: The Maxim"}]
        return []

    monkeypatch.setattr(tmdb, "_execute_single_tmdb_search", fake_single_search)

    candidates = ["Паразит: Учение о жизни", "Parasyte: The Maxim", "Kiseijuu: Sei no Kakuritsu"]
    results = await tmdb.search_tmdb(candidates[0], "tv", candidates=candidates)

    assert len(results) == 1
    assert results[0]["id"] == 61459
    # Both primary and secondary were attempted in order
    assert searched_queries == ["Паразит: Учение о жизни", "Parasyte: The Maxim"]


@pytest.mark.asyncio
async def test_tmdb_primary_candidate_success_skips_secondary(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "dummy_key")

    searched_queries = []

    async def fake_single_search(query: str, media_type: str) -> list[dict]:
        searched_queries.append(query)
        if query == "Во все тяжкие":
            return [{"id": 1396, "title": "Во все тяжкие", "original_title": "Breaking Bad"}]
        return []

    monkeypatch.setattr(tmdb, "_execute_single_tmdb_search", fake_single_search)

    candidates = ["Во все тяжкие", "Breaking Bad"]
    results = await tmdb.search_tmdb(candidates[0], "tv", candidates=candidates)

    assert len(results) == 1
    assert results[0]["id"] == 1396
    # Only primary was searched because it succeeded
    assert searched_queries == ["Во все тяжкие"]


@pytest.mark.asyncio
async def test_tmdb_all_candidates_empty(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "dummy_key")

    searched_queries = []

    async def fake_single_search(query: str, media_type: str) -> list[dict]:
        searched_queries.append(query)
        return []

    monkeypatch.setattr(tmdb, "_execute_single_tmdb_search", fake_single_search)

    candidates = ["Nonexistent 1", "Nonexistent 2"]
    results = await tmdb.search_tmdb(candidates[0], "movie", candidates=candidates)

    assert results == []
    assert searched_queries == ["Nonexistent 1", "Nonexistent 2"]


def test_api_analyze_returns_candidates_and_recommendation(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "dummy_key")

    async def fake_search(query, media_type, candidates=None):
        return [{"id": 61459, "title": "Parasyte", "original_title": "Parasyte"}]

    monkeypatch.setattr(main, "search_tmdb", fake_search)

    client = TestClient(main.app)
    magnet = (
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn="
        "Паразит%3A%20Учение%20о%20жизни%20%28ТВ%29%20%2F%20Kiseijuu%3A%20Sei%20no%20Kakuritsu%20%28TV%29%20%2F%20"
        "Parasyte%3A%20The%20Maxim%20%5BTV%5D%20%5B24%20из%2024%5D%20%5BRUS%28int%29%5D%20%5B2014%2C%20приключения%5D"
    )

    resp = client.post("/api/analyze", data={"magnet": magnet, "media_type": "anime"})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert data["recommended_media_type"] == "anime"
    assert "аниме" in data["recommendation_reason"].lower()
    assert isinstance(data["title_candidates"], list)
    assert len(data["title_candidates"]) >= 3
    assert data["title_candidates"][0] == "Паразит: Учение о жизни"
    assert data["title_candidates"][1] == "Parasyte: The Maxim"


def test_api_health_version_1_1_1():
    client = TestClient(main.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "1.1.1"
    assert data["app"] == "torrent-media-manager"
