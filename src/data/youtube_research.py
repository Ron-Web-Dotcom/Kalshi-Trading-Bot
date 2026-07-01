"""
Deep YouTube research — goes beyond titles into actual video content.

For every market query this module:
  1. Searches YouTube for top relevant videos (sorted by upload date)
  2. Pulls auto-generated transcripts via youtube-transcript-api (free, no key)
  3. Extracts video descriptions + comment counts via yt-dlp metadata
  4. Scores each video for relevance and recency
  5. Returns a rich context block: transcript excerpts, key quotes,
     channel credibility signals, live stream detection

Especially useful for:
  - Live sports events  → "LeBron injury update" press conference transcript
  - Politics            → senator speech transcript, press conference quotes
  - Crypto/Finance      → analyst breakdown, earnings call clips
  - SpaceX / NASA       → launch commentary, official channel updates
  - Breaking news       → eyewitness clips, reporter live updates

All free — no YouTube Data API key required.
Runs in parallel with other web search sources.
"""

from __future__ import annotations

import asyncio
import logging
import re
import json
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("trading.youtube_research")

_TIMEOUT = httpx.Timeout(12.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*",
}

# Channels that signal high-credibility content for trading decisions
_CREDIBLE_CHANNELS = {
    # Sports
    "espn", "nfl", "nba", "mlb", "nhl", "bleacher report", "sky sports",
    "btsport", "bt sport", "bein sports", "athletic", "sportscenter",
    # News
    "cnn", "fox news", "msnbc", "bbc news", "reuters", "ap", "associated press",
    "cnbc", "bloomberg", "the hill", "politico", "c-span", "cspan",
    "abc news", "nbc news", "cbs news", "npr",
    # Finance / Crypto
    "financial times", "wsj", "wall street journal",
    "coindesk", "cointelegraph", "crypto", "bankless",
    # Science / Tech
    "spacex", "nasa", "verge", "techcrunch", "wired",
}

# Keywords that signal live/breaking content — high value for predictions
_LIVE_SIGNALS = [
    "live", "breaking", "just in", "update", "right now", "happening",
    "press conference", "speech", "announcement", "official", "confirmed",
    "injury report", "game day", "pre-game", "post-game", "halftime",
    "earnings", "result", "verdict", "ruling", "decision",
]


# ── Video ID extraction ───────────────────────────────────────────────────────

def _extract_video_ids(html: str, max_ids: int = 8) -> List[Tuple[str, str, str, str]]:
    """
    Parse ytInitialData from YouTube search HTML.
    Returns list of (video_id, title, channel, views).
    """
    videos = []
    try:
        m = re.search(r'var ytInitialData\s*=\s*(\{.+?\});\s*</script>', html, re.DOTALL)
        if not m:
            # Try alternate pattern
            m = re.search(r'ytInitialData\s*=\s*(\{.+?\})(?:;|\n)', html, re.DOTALL)
        if not m:
            return []

        data = json.loads(m.group(1))
        sections = (
            data
            .get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )
        for section in sections:
            for item in section.get("itemSectionRenderer", {}).get("contents", []):
                vr = item.get("videoRenderer", {})
                if not vr:
                    continue
                vid_id  = vr.get("videoId", "")
                title   = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", [])).strip()
                channel = "".join(
                    r.get("text", "")
                    for r in (
                        vr.get("ownerText", {}).get("runs", [])
                        or vr.get("longBylineText", {}).get("runs", [])
                    )
                ).strip()
                views   = (
                    vr.get("viewCountText", {}).get("simpleText", "")
                    or vr.get("shortViewCountText", {}).get("simpleText", "")
                ).strip()
                pub     = vr.get("publishedTimeText", {}).get("simpleText", "").strip()
                is_live = bool(vr.get("badges")) and any(
                    "LIVE" in str(b) for b in vr.get("badges", [])
                )

                if vid_id and title and len(title) > 5:
                    videos.append((vid_id, title, channel, views, pub, is_live))
                if len(videos) >= max_ids:
                    break
            if len(videos) >= max_ids:
                break
    except Exception as e:
        logger.debug("ytInitialData parse error: %s", e)

    return videos


# ── Transcript fetcher ────────────────────────────────────────────────────────

async def _get_transcript(video_id: str, max_chars: int = 1200) -> Optional[str]:
    """
    Fetch auto-generated transcript for a YouTube video.
    Uses youtube-transcript-api in a thread executor (it's sync).
    Returns a cleaned excerpt up to max_chars, or None.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

        def _fetch():
            try:
                fetcher    = YouTubeTranscriptApi()
                transcript = fetcher.fetch(video_id, languages=["en", "en-US", "en-GB"])
                # Join all text segments (v1.x returns FetchedTranscript iterable)
                full = " ".join(seg.text if hasattr(seg, "text") else seg["text"] for seg in transcript)
                # Clean up auto-caption artifacts
                full = re.sub(r'\[.*?\]', '', full)       # remove [Music] [Applause] etc
                full = re.sub(r'\s+', ' ', full).strip()
                return full[:max_chars]
            except (NoTranscriptFound, TranscriptsDisabled):
                return None
            except Exception:
                return None

        result = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return result
    except ImportError:
        logger.debug("youtube-transcript-api not installed")
        return None
    except Exception as e:
        logger.debug("Transcript fetch error for %s: %s", video_id, e)
        return None


# ── Video description fetcher ─────────────────────────────────────────────────

async def _get_video_description(video_id: str) -> Optional[str]:
    """
    Scrape the video description from the watch page.
    Returns first 600 chars of description or None.
    """
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as c:
            r = await c.get(url)
            html = r.text

        # Description is in ytInitialData under videoSecondaryInfoRenderer
        m = re.search(r'"description":\{"runs":\[(\[.*?\]|\{"text":"[^"]*".*?\})\]', html)
        if m:
            try:
                runs = json.loads("[" + m.group(1) + "]") if not m.group(1).startswith("[") else json.loads(m.group(1))
                desc = "".join(r.get("text", "") for r in (runs if isinstance(runs, list) else []))
                if desc:
                    return desc[:600].strip()
            except Exception:
                pass

        # Fallback: og:description meta tag
        m2 = re.search(r'<meta name="description" content="([^"]{20,})"', html)
        if m2:
            return m2.group(1)[:600]
    except Exception as e:
        logger.debug("Description fetch error for %s: %s", video_id, e)
    return None


# ── Relevance scorer ──────────────────────────────────────────────────────────

def _relevance_score(title: str, channel: str, pub: str, is_live: bool, query_terms: List[str]) -> float:
    """Score 0-1 for how relevant/valuable this video is for a trading decision."""
    score = 0.0
    title_low   = title.lower()
    channel_low = channel.lower()

    # Query term matches
    hits = sum(1 for t in query_terms if t.lower() in title_low)
    score += min(hits * 0.15, 0.45)

    # Live stream bonus — highest value for live events
    if is_live:
        score += 0.30

    # Breaking/live keywords in title
    live_hits = sum(1 for kw in _LIVE_SIGNALS if kw in title_low)
    score += min(live_hits * 0.08, 0.24)

    # Credible channel
    if any(ch in channel_low for ch in _CREDIBLE_CHANNELS):
        score += 0.15

    # Recency bonus
    if pub:
        p = pub.lower()
        if "hour" in p or "minute" in p or "just" in p:
            score += 0.20
        elif "day" in p and any(d in p for d in ["1 day", "2 day", "3 day"]):
            score += 0.10

    return min(score, 1.0)


# ── Main entry ────────────────────────────────────────────────────────────────

async def deep_youtube_research(
    query: str,
    title: str = "",
    max_videos: int = 4,
    timeout: float = 18.0,
) -> str:
    """
    Full deep YouTube research for a market question.

    Steps:
      1. Search YouTube for top videos (date-sorted)
      2. Score for relevance
      3. For top 3: fetch transcript + description in parallel
      4. Return rich context block

    Returns formatted string for injection into AI/rule engine context.
    """
    if not query:
        return ""

    query_terms = [w for w in re.findall(r"[a-zA-Z0-9]{3,}", query) if len(w) > 2]

    # ── Step 1: Search ────────────────────────────────────────────────────────
    try:
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=CAI%253D"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as c:
            r = await c.get(search_url)
            raw_videos = _extract_video_ids(r.text, max_ids=12)
    except Exception as e:
        logger.debug("YouTube search fetch failed: %s", e)
        return ""

    if not raw_videos:
        logger.debug("YouTube: no videos found for '%s'", query[:60])
        return ""

    # ── Step 2: Score and pick top videos ────────────────────────────────────
    scored = []
    for vid_id, vid_title, channel, views, pub, is_live in raw_videos:
        s = _relevance_score(vid_title, channel, pub, is_live, query_terms)
        scored.append((s, vid_id, vid_title, channel, views, pub, is_live))
    scored.sort(key=lambda x: x[0], reverse=True)

    top = scored[:max_videos]

    # ── Step 3: Fetch transcripts + descriptions in parallel ─────────────────
    async def _enrich(score, vid_id, vid_title, channel, views, pub, is_live):
        transcript, description = await asyncio.gather(
            _get_transcript(vid_id, max_chars=1000),
            _get_video_description(vid_id),
            return_exceptions=True,
        )
        transcript  = transcript  if isinstance(transcript,  str) else None
        description = description if isinstance(description, str) else None
        return score, vid_id, vid_title, channel, views, pub, is_live, transcript, description

    try:
        enriched = await asyncio.wait_for(
            asyncio.gather(*[_enrich(*v) for v in top], return_exceptions=False),
            timeout=timeout - 4,
        )
    except asyncio.TimeoutError:
        logger.warning("YouTube deep research timed out — using titles only")
        enriched = [(s, vid, t, ch, vw, pb, lv, None, None) for s, vid, t, ch, vw, pb, lv in top]

    # ── Step 4: Build context block ───────────────────────────────────────────
    if not enriched:
        return ""

    lines = [f"=== YOUTUBE DEEP RESEARCH ({len(enriched)} videos) ==="]
    has_content = False

    for i, row in enumerate(enriched, 1):
        score, vid_id, vid_title, channel, views, pub, is_live, transcript, description = row

        live_tag = " 🔴 LIVE" if is_live else ""
        lines.append(
            f"\n[{i}] {vid_title}{live_tag}"
            + (f" | {channel}" if channel else "")
            + (f" | {views}" if views else "")
            + (f" | {pub}" if pub else "")
        )

        if description:
            desc_clean = re.sub(r'\s+', ' ', description).strip()
            lines.append(f"    Description: {desc_clean[:300]}")
            has_content = True

        if transcript:
            # Extract most relevant excerpt — find sentences containing query terms
            sentences = re.split(r'[.!?]+', transcript)
            relevant = [s.strip() for s in sentences if any(t.lower() in s.lower() for t in query_terms)]
            if relevant:
                excerpt = " ... ".join(relevant[:4])
            else:
                excerpt = transcript[:500]
            lines.append(f"    Transcript excerpt: {excerpt[:700]}")
            has_content = True

        if not description and not transcript:
            lines.append("    (title only — transcript unavailable)")

    if not has_content and len(enriched) == 0:
        return ""

    result = "\n".join(lines)
    total_with_transcript = sum(1 for row in enriched if row[7])
    total_with_desc       = sum(1 for row in enriched if row[8])

    logger.info(
        "YouTube deep research: %d videos | %d transcripts | %d descriptions | query='%s'",
        len(enriched), total_with_transcript, total_with_desc, query[:60],
    )
    return result
