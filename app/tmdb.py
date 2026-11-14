import httpx

import config

# Fallback reference
TMDB_API_KEY = getattr(config, "TMDB_API_KEY", "")


TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"


async def _execute_single_tmdb_search(query: str, media_type: str) -> list[dict]:
    """Executes a single TMDB search request for one query string."""
    api_key = getattr(config, "TMDB_API_KEY", "") or TMDB_API_KEY
    if not api_key:
        return []

    clean_query = str(query).strip() if query else ""
    if not clean_query:
        return []

    if media_type == "movie":
        endpoint = "/search/movie"
    else:
        endpoint = "/search/tv"

    params = {
        "api_key": api_key,
        "query": clean_query,
        "language": "ru-RU",
        "include_adult": "false",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(TMDB_BASE + endpoint, params=params)
            if response.status_code != 200:
                return []
            data = response.json()
    except Exception:
        return []

    results = []

    for item in data.get("results", [])[:12]:
        title = item.get("title") or item.get("name") or ""
        original_title = item.get("original_title") or item.get("original_name") or ""
        date = item.get("release_date") or item.get("first_air_date") or ""
        year = date[:4] if date else ""

        poster_path = item.get("poster_path")
        poster_url = TMDB_IMAGE_BASE + poster_path if poster_path else ""

        results.append(
            {
                "id": item.get("id"),
                "title": title,
                "original_title": original_title,
                "year": year,
                "overview": item.get("overview") or "",
                "poster_url": poster_url,
                "media_type": media_type,
                "source": "tmdb",
                "vote_average": item.get("vote_average"),
            }
        )

    return results


async def search_tmdb(
    query: str | list[str],
    media_type: str,
    candidates: list[str] | None = None,
) -> list[dict]:
    """Searches TMDB with multi-candidate search fallback.

    If searching primary cleaned title yields 0 results, tries secondary candidate
    (e.g. English title) before giving up.
    """
    if not (getattr(config, "TMDB_API_KEY", "") or TMDB_API_KEY):
        return []

    queries_to_try: list[str] = []

    if isinstance(query, (list, tuple)):
        for q in query:
            clean_q = str(q).strip()
            if clean_q and clean_q not in queries_to_try:
                queries_to_try.append(clean_q)
    elif query:
        clean_q = str(query).strip()
        if clean_q and clean_q not in queries_to_try:
            queries_to_try.append(clean_q)

    if candidates:
        for c in candidates:
            clean_c = str(c).strip()
            if clean_c and clean_c not in queries_to_try:
                queries_to_try.append(clean_c)

    if not queries_to_try:
        return []

    for q in queries_to_try:
        results = await _execute_single_tmdb_search(q, media_type)
        if results:
            return results

    return []
