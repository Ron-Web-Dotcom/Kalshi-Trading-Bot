"""
Single source of truth for junk market phrases.
Import is_junk() anywhere a market title needs checking.
"""

JUNK_PHRASES = [
    # Weather / temperature markets — too unpredictable, low edge
    "highest temperature", "lowest temperature", "temperature in",
    "will it rain", "will it snow", "rainfall", "snowfall",
    "hurricane", "tornado", "wind speed", "precipitation",
    # Esports / gaming — low data, hard to model outcomes
    "honor of kings", "league of legends", "valorant game",
    "dota 2", "counter-strike", "cs2 match", "bo3 match", "bo5 match",
    "first blood", "first dragon", "first baron", "first tower",
    "esports championship", "gaming tournament",
    # Long-term political futures
    "gavin newsom", "2028 democratic", "2028 president", "2028 us presidential",
    "win the 2028", "win the 2032",
    "bernie endorse", "endorse dan osborn",
    # Foreign political long-shots
    "ivan cepeda", "abelardo de la", "colombian presiden", "colombian president",
    "keir starmer", "labour par", "democratic union of hungarians",
    # World Cup WINNER markets (tournament outcome — any country)
    "win the 2026 fifa world cup", "win the 2026 world cup",
    "win the world cup", "fifa world cup winner", "world cup champion",
    "world cup winner", "lift the 2026",
    "spain win the 2026", "france win the 2026", "brazil win the 2026",
    "germany win the 2026", "argentina win the 2026", "england win the 2026",
    "portugal win the 2026", "usa win the 2026", "mexico win the 2026",
    "morocco win the 2026", "netherlands win the 2026", "japan win the 2026",
    # Other tournament winners (season-long)
    "nba finals winner", "nba champion", "stanley cup winner",
    "win the nba championship", "win the stanley cup",
    "win the 2026 nba", "2026 nba finals",
    # Celebrity / novelty / never-happening
    "before gta", "gta vi", "gta 6",
    "playboi carti", "rihanna", "kanye", "drake album",
    "jesus christ", "second coming", "rapture",
    "oprah", "lebron", "taylor swift president", "elon musk president",
    "mark zuckerberg president", "joe rogan president", "dwayne johnson",
    "waymo launch", "waymo nashville",
    "invades taiwan", "china taiwan", "world war", "nuclear",
    # Crypto / tech far-future
    "airdrop by", "megaeth", "before agi", "agi by",
    "hit $150k", "hit $1m", "hit $500k",
    # Far-future time gates
    "before 2027", "before 2028", "before 2029", "before 2030",
    "before 203", "before 204",
    "by december 31", "by end of 2026", "by january 2027", "by 2027",
    # Random distant/no-edge markets
    "uzbekistan win", "kuala lumpur",
    "lck 2026", "gen.g esports",
    "victor wembanyama", "wembanyama",
    # Novelty / announcer / phrase markets
    "will the announcer", "say 'aggressive'", "say 'clutch'",
    "say 'impressive'", "say 'incredible'", "say 'unbelievable'",
    "announcer say", "commentator say",
    # YouTube / content creator word-count markets — unverifiable, low quality
    "mrbeast", "mcbeast", "will mcbeast",
    "say 'dollar'", "say 'sponsored'", "say 'faustable'", "say 'faustables'",
    "say 'merch'", "say 'subscribe'", "say 'like and subscribe'",
    "during his next youtube", "during his next video",
    "youtube video", "in his next video",
    # Crypto Up/Down scalp markets — highly volatile, consistent stop-losses
    "bitcoin up or down", "btc up or down",
    "ethereum up or down", "eth up or down",
    "solana up or down", "sol up or down",
    "xrp up or down", "doge up or down",
    "hype up or down", "bnb up or down",
    # Multi-week / tournament-winner markets in today's pool
    "win the 2026 wimbledon", "win wimbledon",
    "win the 2026 us open", "win the 2026 french open",
    "win the 2026 australian open",
    "lpl win msi", "lck win msi", "win msi 2026",
    # Meme / low-liquidity crypto
    "meme coin", "rug pull", "all-time high by",
    "pump to", "moon by",
]


def is_junk(title: str) -> bool:
    """Return True if the market title matches any known junk phrase."""
    t = (title or "").lower()
    return any(phrase in t for phrase in JUNK_PHRASES)


async def purge_junk_from_db(db) -> int:
    """
    Delete all junk markets from the DB markets table.
    Call once on startup to clear stale rows that pre-date the write-time filter.
    Returns number of rows deleted.
    """
    try:
        rows = await db.fetchall("SELECT ticker, title FROM markets WHERE title IS NOT NULL")
        junk_tickers = [r["ticker"] for r in (rows or []) if is_junk(r["title"] or "")]
        if not junk_tickers:
            return 0
        placeholders = ",".join("?" * len(junk_tickers))
        await db.execute(
            f"DELETE FROM markets WHERE ticker IN ({placeholders})",
            tuple(junk_tickers),
        )
        import logging
        logging.getLogger("trading.junk_filter").info(
            "Purged %d junk markets from DB on startup", len(junk_tickers)
        )
        return len(junk_tickers)
    except Exception as e:
        import logging
        logging.getLogger("trading.junk_filter").warning("purge_junk_from_db error: %s", e)
        return 0
