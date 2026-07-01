PYTHON := python3.12

.PHONY: test lint install ci

install:
	$(PYTHON) -m pip install pytest pytest-asyncio aiosqlite python-dotenv httpx cryptography openai --break-system-packages -q

lint:
	ruff check src/ bot.py

test:
	KALSHI_API_KEY=dummy OPENAI_API_KEY=dummy DISCORD_WEBHOOK_URL=dummy \
	POLY_API_KEY=dummy POLY_API_SECRET=dummy \
	$(PYTHON) -m pytest tests/test_bugs_regression.py -v --tb=short

ci: lint test
