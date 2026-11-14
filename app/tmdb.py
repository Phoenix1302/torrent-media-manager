import httpx

from config import TMDB_API_KEY


TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"


async def search_tmdb(query: str, media_type: str):
    if not TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY is empty")

    if media_type == "movie":
        endpoint = "/search/movie"
    else:
        endpoint = "/search/tv"

    params = {
        "api_key": TMDB_API_KEY,
        "query": query,
        "language": "ru-RU",
        "include_adult": "false",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(TMDB_BASE + endpoint, params=params)
        response.raise_for_status()
        data = response.json()

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
