"""Title cleaner and media type recommendation heuristics for Torrent Media Manager.

Handles compound release titles separated by '/', '|', '—', extracts title candidates
(Russian, English, Romaji/Original), cleans tags, and provides heuristic media type recommendations.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class CandidateList(list):
    """List of cleaned title candidates with helper properties for language variants."""

    def __init__(
        self,
        items: list[str],
        russian: str | None = None,
        english: str | None = None,
        romaji: str | None = None,
        original: str | None = None,
    ) -> None:
        super().__init__(items)
        self.russian: str = russian or ""
        self.english: str = english or ""
        self.romaji: str = romaji or ""
        self.original: str = original or ""

    @property
    def primary(self) -> str:
        """Primary search candidate."""
        return self[0] if self else ""

    @property
    def secondary(self) -> str:
        """Secondary fallback candidate."""
        return self[1] if len(self) > 1 else ""


class MediaRecommendation(tuple):
    """Media recommendation containing category ('films', 'serials', 'anime', 'mults') and reason.

    Inherits from tuple for (category, reason) unpacking compatibility while providing attribute access.
    """

    def __new__(cls, category: str, reason: str, media_type: str = ""):
        return super().__new__(cls, (category, reason))

    def __init__(self, category: str, reason: str, media_type: str = "") -> None:
        self.category: str = category
        self.reason: str = reason
        self.media_type: str = media_type or category


# Release and quality tokens to strip when they appear as words in title segments
RELEASE_TOKENS: set[str] = {
    "2160p", "1080p", "1080i", "720p", "480p", "4k", "uhd",
    "web-dl", "webdl", "webrip", "bluray", "blu-ray", "bdrip", "brrip",
    "hdrip", "dvdrip", "hdtv", "remux", "bdrip-hevc", "bdrip-avc",
    "x264", "x265", "h264", "h265", "hevc", "avc", "10bit", "8bit", "hi10p",
    "aac", "ac3", "dts", "dts-hd", "truehd", "flac", "ddp", "dd", "dd5.1",
    "hdr", "hdr10", "hdr10+", "dv", "dolby", "sdr",
    "lostfilm", "newstudio", "alexfilm", "jaskier", "kubik", "hdrezka", "baibako",
    "anilibria", "anidub", "shiza", "animevost", "studio", "wink", "amediateka", "kinopoisk",
    "mvo", "dvo", "dub", "sub", "softsub", "hardsub", "raw", "extended", "remastered", "unrated",
}

# Known Japanese romaji vocabulary, particles, and common titles
ROMAJI_KEYWORDS: set[str] = {
    "no", "wa", "ga", "wo", "ni", "de", "to", "mo", "ka", "kara", "made", "e", "na",
    "sei", "kakuritsu", "shin", "kyojin", "shingeki", "monogatari", "tensei", "isekai",
    "taizai", "tsundere", "maou", "senki", "biyori", "yuusha", "gakuen", "kuroko",
    "haikyuu", "kimetsu", "yaiba", "jujutsu", "kaisen", "boku", "watashi", "ore",
    "kimi", "densetsu", "chou", "hen", "gekijouban", "gakuin", "musume", "sensei",
    "san", "kun", "chan", "sama", "dono", "da", "desu", "kiseijuu", "naruto", "boruto",
    "bleach", "clannad", "toradora", "evangelion", "inuyasha", "ranma", "shippuuden",
    "ouji", "hime", "tenshi", "akuma", "mahou", "shoujo", "shounen", "seinen", "josei",
    "zankoku", "uchuu", "senshi", "yusha", "maho", "kage", "tsuki", "sakura", "hana",
    "kokoro", "sora", "hikari", "yoru", "yami", "ai", "koi", "sekai", "toki", "kami",
    "ryu", "tora", "inu", "neko", "ookami", "mahouka", "koukou", "rettousei", "overlord",
    "danmachi", "gintama", "shokugeki", "soma", "chainsaw", "vinland", "dorohedoro",
    "hajimeru", "re:zero", "honzuki", "geass", "hagane", "renkinjutsushi", "chihiro",
    "kamikakushi", "sen",
}

# Common English vocabulary to distinguish English titles from Romaji
ENGLISH_COMMON_WORDS: set[str] = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "by", "from",
    "up", "about", "into", "over", "after", "maxim", "parasyte", "attack", "titan",
    "season", "movie", "hero", "adventures", "man", "world", "life", "chronicles",
    "brotherhood", "detective", "slayer", "rebellion", "zero", "war", "king", "queen",
    "prince", "princess", "lord", "ghost", "shell", "neon", "genesis", "city",
    "black", "white", "red", "blue", "dead", "death", "gate", "blood", "game",
    "thrones", "breaking", "bad", "better", "call", "saul", "matrix", "rings",
    "fellowship", "ring", "dark", "knight", "stranger", "things", "witcher",
    "spirited", "away", "inception", "interstellar",
}


def classify_language(text: str) -> str:
    """Classifies a title string as 'russian', 'romaji', 'english', or 'original'."""
    # Check for East Asian scripts (Kana, CJK, Hangul)
    if re.search(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af]', text):
        return "original"

    # Check for Cyrillic letters
    if re.search(r'[а-яА-ЯёЁ]', text):
        return "russian"

    # Latin text: score Romaji vs English
    words = [w.lower() for w in re.findall(r'[a-zA-Z]+', text)]
    if not words:
        return "english"

    romaji_score = 0
    english_score = 0

    for w in words:
        if w in ROMAJI_KEYWORDS:
            romaji_score += 4
        if w in ENGLISH_COMMON_WORDS:
            english_score += 4

        # Japanese double vowels
        if any(dv in w for dv in ("uu", "ou", "ei", "ii", "aa", "oo")):
            romaji_score += 2
        # Japanese geminated consonants
        if re.search(r'(kk|tt|pp|ss|tch|cch)', w):
            romaji_score += 1
        # Syllable endings (almost all Japanese romaji words end in a vowel or 'n')
        if w.endswith(('a', 'i', 'u', 'e', 'o', 'n')):
            romaji_score += 0.5
        elif len(w) > 2:
            english_score += 1

        # Common English morphological suffixes
        if w.endswith(('tion', 'sion', 'ment', 'ness', 'able', 'ible', 'ing', 'ed', 'est', 'er', 'ly', 'th')):
            english_score += 2

    return "romaji" if romaji_score > english_score else "english"


def clean_segment(segment: str) -> str:
    """Cleans tags, bracketed metadata, and release tokens from a single title segment."""
    val = segment.strip()
    if not val:
        return ""

    # 1. Repeatedly strip leading brackets [Group] or (Group)
    while True:
        m = re.match(r'^(?:\[[^\]]+\]|\([^\)]+\))\s*', val)
        if m:
            val = val[m.end():].strip()
        else:
            break

    # 2. Specific TV / Episode / Tag removals inside the segment
    # Remove TV tags like (ТВ), (TV), [TV], (ТВ-1), [TV Series], etc.
    val = re.sub(
        r'\s*[\(\[](?:ТВ(?:-\d+)?|TV(?:-\d+)?|TV\s*Series|ТВ\s*сериал|Сериал|OVA|ONA|OAD|Movie|Фильм|Special|Specials)[\)\]]',
        '', val, flags=re.IGNORECASE
    )

    # Remove season / episode brackets like [24 из 24], [Сезон 1], [01-24], (1-12)
    val = re.sub(
        r'\s*[\(\[](?:(?:\d+-)?\d+\s*из\s*\d+(?:\s*\+\s*\w+)?|\d+/\d+|\d+-\d+|\d+\s*сери[ияй]|\d+-\d+\s*сери[ияй]|эпизод[ы]?\s*\d+(?:-\d+)?)[\)\]]',
        '', val, flags=re.IGNORECASE
    )
    val = re.sub(
        r'\s*[\(\[](?:сезон\s*\d+(?:-\d+)?|season\s*\d+(?:-\d+)?|s\d+(?:-s?\d+)?)[\)\]]',
        '', val, flags=re.IGNORECASE
    )

    # Remove audio/language/release group tags
    val = re.sub(
        r'\s*\[(?:rus(?:\(int\))?|eng|jap|kor|raw|vo|mvo|dvo|avo|dub|sub|multi-sub|softsub|hardsub|субтитры|озвучка|дубляж|лицензия|lostfilm|newstudio|alexfilm|hdrezka|кубик\s*в\s*кубе|anilibria|anidub|shiza(?:\s*project)?|animevost|studio\s*band)[^\]]*\]',
        '', val, flags=re.IGNORECASE
    )

    # Remove format/codec tags in brackets
    val = re.sub(
        r'\s*\[(?:2160p|1080p|1080i|720p|480p|4k|uhd|bdrip(?:-hevc)?|brrip|hdrip|dvdrip|webrip|web-dl|webdl|hdtv|remux|x264|x265|h\.?264|h\.?265|hevc|avc|10bit|8bit|hi10p|aac|ac3|flac|dts(?:-hd)?|truehd|ddp|dd5\.1)[^\]]*\]',
        '', val, flags=re.IGNORECASE
    )

    # Remove year & genre bracket blocks e.g. [2014, приключения, ...]
    val = re.sub(r'\s*[\(\[](?:19|20)\d{2}(?:[^\s\]\)]*)?(?:,[^\]\)]+)?[\)\]]', '', val)

    # 3. Repeatedly strip trailing bracket blocks [Anything]
    while True:
        m = re.search(r'\s*\[[^\]]+\]$', val)
        if m:
            val = val[:m.start()].strip()
        else:
            break

    # 4. If string contains dots or underscores instead of spaces, convert
    if "." in val and " " not in val:
        val = val.replace(".", " ")
    val = val.replace("_", " ")

    # 5. Clean out trailing scene release tokens, season/episode tokens, and trailing years
    words = val.split()
    cleaned_words = []
    for w in words:
        clean_w = w.lower().strip("[](){}.,;:_-")
        if clean_w in RELEASE_TOKENS:
            break
        if re.fullmatch(r"(19|20)\d{2}", clean_w):
            break
        if re.fullmatch(r"s\d+(?:e\d+.*)?|e\d+(?:-e?\d+)?|ep\d+", clean_w):
            break
        cleaned_words.append(w)

    val = " ".join(cleaned_words).strip()

    # 6. Clean trailing comma, hyphen, colon, parentheses, dots
    val = re.sub(r'\s*[\(\[](?:19|20)\d{2}[\)\]]', '', val).strip()
    val = re.sub(r'[,:\-—–~/\.]+$', '', val).strip()
    val = re.sub(r'^[,:\-—–~/\.]+', '', val).strip()

    return " ".join(val.split())


def split_compound_title(raw_name: str) -> list[str]:
    """Splits compound title on '/', '|', '—', '–' while avoiding splitting titles with internal slashes."""
    text = raw_name.strip()

    # Delimiters:
    # 1. Pipe: |
    # 2. Em-dash / En-dash: — or –
    # 3. Slash with whitespace: \s+/\s* or \s*/\s+
    # 4. Slash between Cyrillic and Latin: (?<=[а-яА-ЯёЁ])/(?=[a-zA-Z]) or vice versa
    pattern = r'(?:\s*\|\s*|\s*[—–]\s*|\s+/\s*|\s*/\s+|(?<=[а-яА-ЯёЁ])/(?=[a-zA-Z])|(?<=[a-zA-Z])/(?=[а-яА-ЯёЁ]))'
    parts = re.split(pattern, text)

    # Check for spaced dash splitting if not split yet
    if len(parts) == 1 and " - " in parts[0]:
        dash_parts = parts[0].split(" - ")
        if len(dash_parts) == 2:
            c1 = classify_language(dash_parts[0])
            c2 = classify_language(dash_parts[1])
            if (c1 == "russian" and c2 in ("english", "romaji")) or (c2 == "russian" and c1 in ("english", "romaji")):
                parts = dash_parts

    return [p.strip() for p in parts if p.strip()]


def extract_title_candidates(raw_name: str) -> CandidateList:
    """Extracts cleaned title candidates (Russian, English, Romaji/Original).

    Returns a CandidateList where candidate[0] is the primary candidate, candidate[1] is secondary, etc.,
    and properties .russian, .english, .romaji, .original provide classified access.
    """
    raw_parts = split_compound_title(raw_name)

    cleaned_parts: list[str] = []
    seen: set[str] = set()

    for part in raw_parts:
        c = clean_segment(part)
        if c and c.lower() not in seen and len(c) > 1:
            cleaned_parts.append(c)
            seen.add(c.lower())

    if not cleaned_parts:
        fallback = clean_segment(raw_name) or raw_name.strip()
        cleaned_parts = [fallback]

    # Classify candidates
    russian_candidate: str | None = None
    english_candidate: str | None = None
    romaji_candidate: str | None = None
    original_candidate: str | None = None

    for c in cleaned_parts:
        lang = classify_language(c)
        if lang == "russian" and not russian_candidate:
            russian_candidate = c
        elif lang == "romaji" and not romaji_candidate:
            romaji_candidate = c
        elif lang == "english" and not english_candidate:
            english_candidate = c
        elif lang == "original" and not original_candidate:
            original_candidate = c

    # Build prioritized candidate list:
    # 1. Primary: Russian if present, else first available candidate
    # 2. Secondary: English (or Romaji if no English)
    # 3. Tertiary: Romaji / Original
    # 4. Any remaining candidates
    ordered: list[str] = []
    if russian_candidate:
        ordered.append(russian_candidate)
    if english_candidate and english_candidate not in ordered:
        ordered.append(english_candidate)
    if romaji_candidate and romaji_candidate not in ordered:
        ordered.append(romaji_candidate)
    if original_candidate and original_candidate not in ordered:
        ordered.append(original_candidate)

    for c in cleaned_parts:
        if c not in ordered:
            ordered.append(c)

    return CandidateList(
        ordered,
        russian=russian_candidate,
        english=english_candidate,
        romaji=romaji_candidate,
        original=original_candidate,
    )


def recommend_media_type(raw_name: str, candidates: list[str] | None = None) -> MediaRecommendation:
    """Heuristic recommendation of media type: 'films', 'serials', 'anime', 'mults'."""
    lower_raw = raw_name.lower()
    cand_list = candidates or extract_title_candidates(raw_name)

    # 1. Anime Detection
    anime_tracker_groups = [
        "anilibria", "anidub", "shiza project", "shiza", "animevost",
        "studio band", "anistar", "yummyanime", "sovet romantica",
        "subsplease", "horriblesubs", "judas", "eraiv-raws", "tokyo mx",
    ]
    anime_tags = [
        "(тв)", "[tv]", "(tv)", "(тв-1)", "(тв-2)", "(tv-1)", "(tv-2)",
        "(ova)", "[ova]", "(ona)", "[ona]", "(oad)", "[oad]",
    ]
    anime_keywords = [
        "аниме", "anime", "manga", "манга", "ранобэ", "light novel",
        "gekijouban", "shoujo", "shounen", "isekai",
    ]

    has_anime_group = any(g in lower_raw for g in anime_tracker_groups)
    has_anime_tag = any(t in lower_raw for t in anime_tags)
    has_anime_kw = any(k in lower_raw for k in anime_keywords)
    has_romaji = bool(getattr(cand_list, "romaji", None)) or any(classify_language(c) == "romaji" for c in cand_list)

    if has_anime_group or has_anime_tag or has_anime_kw or has_romaji:
        reasons = []
        if has_anime_group:
            matched_g = [g for g in anime_tracker_groups if g in lower_raw]
            reasons.append(f"аниме-группа '{matched_g[0]}'")
        if has_anime_tag:
            matched_t = [t for t in anime_tags if t in lower_raw]
            reasons.append(f"тег '{matched_t[0]}'")
        if has_anime_kw:
            matched_k = [k for k in anime_keywords if k in lower_raw]
            reasons.append(f"ключевое слово '{matched_k[0]}'")
        if has_romaji:
            rom_title = getattr(cand_list, "romaji", "") or "Romaji"
            reasons.append(f"ромадзи/оригинальное название '{rom_title}'")

        return MediaRecommendation(
            category="anime",
            reason=f"Определено как аниме ({', '.join(reasons)})",
            media_type="anime",
        )

    # 2. Cartoon / Mults Detection
    cartoon_keywords = [
        "мультфильм", "мультсериал", "мультик", "анимация", "анимационный",
        "мультипликационный", "cartoon", "disney", "pixar", "dreamworks",
        "nickelodeon", "cartoon network", "looney tunes", "смешарики",
        "три богатыря", "лунтик", "маша и медведь", "animation",
    ]
    matched_cartoons = [k for k in cartoon_keywords if k in lower_raw]
    if matched_cartoons:
        return MediaRecommendation(
            category="mults",
            reason=f"Определено как мультфильм/анимация (найдено: '{matched_cartoons[0]}')",
            media_type="cartoon",
        )

    # 3. TV Series / Serials Detection
    tv_groups = [
        "lostfilm", "newstudio", "alexfilm", "hdrezka", "кубик в кубе",
        "good people", "baibako", "coldfilm", "jaskier",
    ]
    tv_keywords = ["сериал", "мини-сериал", "tv series", "tv show"]
    has_tv_group = any(g in lower_raw for g in tv_groups)
    has_tv_kw = any(k in lower_raw for k in tv_keywords)

    has_season_pattern = bool(re.search(
        r'(?:\[(?:\d+-)?\d+\s*из\s*\d+|\bсезон\s*\d+|\bseason\s*\d+|\bs\d{1,2}(?:e\d{1,2})?|\bсери[ияй]\s*\d+|\bэпизод\w*\s*\d+)',
        lower_raw
    ))

    if has_tv_group or has_tv_kw or has_season_pattern:
        reasons = []
        if has_tv_group:
            matched_g = [g for g in tv_groups if g in lower_raw]
            reasons.append(f"релиз-группа '{matched_g[0]}'")
        if has_tv_kw:
            matched_k = [k for k in tv_keywords if k in lower_raw]
            reasons.append(f"ключевое слово '{matched_k[0]}'")
        if has_season_pattern:
            reasons.append("паттерн сезона/серий")

        return MediaRecommendation(
            category="serials",
            reason=f"Определено как сериал ({', '.join(reasons)})",
            media_type="tv",
        )

    # 4. Movie / Films Detection (Default)
    movie_keywords = ["фильм", "х/ф", "movie", "cinema", "полнометражный"]
    matched_movie = [k for k in movie_keywords if k in lower_raw]
    reason = f"найдено '{matched_movie[0]}'" if matched_movie else "полнометражный релиз без признаков сериала или анимации"

    return MediaRecommendation(
        category="films",
        reason=f"Определено как фильм ({reason})",
        media_type="movie",
    )


def clean_release_title(raw_name: str) -> str:
    """Cleans title to single best primary candidate."""
    candidates = extract_title_candidates(raw_name)
    return candidates.primary if candidates else clean_segment(raw_name)
