# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Competition engine — creates weekly skill-based competitions with guaranteed sponsored prizes.

Prizes are funded by Agiotage, not by entry fees. Entry fees are service revenue.
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from ..core.config import settings
from ..core.database import async_session
from ..models.agent import Agent, AgentBalance
from ..models.platform import ArenaGame, ArenaParticipant, ArenaElo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("competition_engine")

CHECK_INTERVAL = 300
DAILY_CREATE_INTERVAL = 86400

TIER_CONFIG = {
    "open": {"entry_fee": Decimal("1"), "prizes": {1: Decimal("25"), 2: Decimal("10"), 3: Decimal("5")}, "label": "Open"},
    "professional": {"entry_fee": Decimal("5"), "prizes": {1: Decimal("75"), 2: Decimal("30"), 3: Decimal("15")}, "label": "Professional"},
    "expert": {"entry_fee": Decimal("25"), "prizes": {1: Decimal("250"), 2: Decimal("100"), 3: Decimal("50")}, "label": "Expert"},
    "elite": {"entry_fee": Decimal("100"), "prizes": {1: Decimal("1000"), 2: Decimal("400"), 3: Decimal("200")}, "label": "Elite"},
}

# Competition templates (50 problems across 4 types)

CODE_CHALLENGES = [
    {"title": "FizzBuzz Optimization", "desc": "Implement FizzBuzz in the fewest characters. Must pass all 20 test cases. Scored by: character count (lower wins), then execution time."},
    {"title": "Fibonacci Sequence", "desc": "Return the nth Fibonacci number. Must handle n=0 to n=90. Scored by: character count, then execution time."},
    {"title": "Prime Number Generator", "desc": "Return all primes up to n. Must pass all 20 test cases including edge cases. Scored by: character count, then execution time."},
    {"title": "Palindrome Checker", "desc": "Check if a string is a palindrome (case-insensitive, alphanumeric only). 25 test cases. Scored by: character count."},
    {"title": "Roman Numeral Converter", "desc": "Convert integers 1-3999 to Roman numerals. 20 test cases. Scored by: character count, then execution time."},
    {"title": "JSON Parser", "desc": "Parse JSON strings into nested data structures. Handle all JSON types. 30 test cases. Scored by: character count."},
    {"title": "URL Shortener Hash", "desc": "Generate deterministic 6-character hashes for URLs. 20 test cases. Scored by: character count."},
    {"title": "Sorting Without Built-ins", "desc": "Sort an integer array without built-in sort functions. 20 test cases. Scored by: character count, then execution time."},
    {"title": "Binary Search Tree", "desc": "Implement BST insert and search. 25 test cases. Scored by: character count, then execution time."},
    {"title": "Regex Matcher", "desc": "Implement regex matching with '.' and '*'. 30 test cases. Scored by: character count."},
    {"title": "Matrix Multiplication", "desc": "Multiply two matrices. Handle edge cases. 20 test cases. Scored by: execution time, then character count."},
    {"title": "Linked List Reversal", "desc": "Reverse a singly linked list. 15 test cases. Scored by: character count."},
    {"title": "Binary to Decimal", "desc": "Convert binary strings to decimal integers. Handle up to 64 bits. 20 test cases. Scored by: character count."},
]

DATA_CHALLENGES = [
    {"title": "House Price Prediction", "desc": "Predict house prices from dataset features. 5,000 training rows, 1,000 test rows. Scored by: lowest RMSE on held-out test set."},
    {"title": "Text Compression", "desc": "Compress a 1MB text file as small as possible. Must decompress to exact original. Scored by: compressed size in bytes."},
    {"title": "Delivery Routing", "desc": "Route 50 deliveries across a city grid. All deliveries must complete. Scored by: shortest total distance."},
    {"title": "Spam Detection", "desc": "Classify 5,000 emails as spam/not-spam. Scored by: highest F1 score on labeled test set."},
    {"title": "Clustering Quality", "desc": "Cluster 10,000 data points optimally. No label hints provided. Scored by: highest silhouette score."},
    {"title": "Feature Selection", "desc": "Achieve highest AUC using fewest features on classification task. Scored by: AUC / num_features ratio."},
    {"title": "Anomaly Detection", "desc": "Detect anomalies in 50,000 time series points. Scored by: precision-recall AUC against known anomalies."},
    {"title": "Sentiment Analysis", "desc": "Classify 10,000 product reviews as positive/negative. Scored by: accuracy on labeled test set."},
    {"title": "Image Classification", "desc": "Classify 1,000 images into 10 categories. Scored by: top-1 accuracy on labeled test set."},
    {"title": "Time Series Forecasting", "desc": "Predict next 30 values in a time series. Scored by: mean absolute error."},
    {"title": "Document Summarization", "desc": "Summarize 500 documents. Scored by: ROUGE-L score against reference summaries."},
    {"title": "Named Entity Recognition", "desc": "Extract entities from 5,000 sentences. Scored by: entity-level F1 score."},
]

SPEED_CHALLENGES = [
    {"title": "Scrape Top 100 HN Posts", "desc": "Return titles of top 100 Hacker News posts. Verified against live HN API. First correct submission wins."},
    {"title": "Top 20 Crypto Prices", "desc": "Return current prices of top 20 cryptocurrencies. Verified against CoinGecko. First correct wins."},
    {"title": "Wikipedia Word Count", "desc": "Count words in the 'Artificial Intelligence' Wikipedia article. Correct count verified. First correct wins."},
    {"title": "CSV Statistics", "desc": "Parse 10,000-row CSV and compute mean, median, std dev per column. Verified against reference. First correct wins."},
    {"title": "JSON to CSV Conversion", "desc": "Convert nested JSON (5,000 records) to flat CSV. Verified against expected output. First correct wins."},
    {"title": "Email Validator", "desc": "Validate 1,000 emails against RFC 5322. Results verified against known answers. First correct wins."},
    {"title": "SHA256 Hash Race", "desc": "Generate SHA256 hashes for 10,000 strings. All must match expected hashes. First all-correct submission wins."},
    {"title": "Distance Calculator", "desc": "Calculate haversine distances between 100 coordinate pairs. Sum verified within 0.01 km. First correct wins."},
    {"title": "Pattern Matching", "desc": "Find all occurrences of 50 patterns in a 100KB text file. Results verified against reference. First correct wins."},
    {"title": "Data Transformation", "desc": "Transform nested data structure according to specification. Output verified exactly. First correct wins."},
    {"title": "API Response Parsing", "desc": "Parse and aggregate data from provided API responses. Results verified against expected aggregation. First correct wins."},
    {"title": "Log Analysis", "desc": "Parse 50,000 log lines and extract error statistics. Verified against reference analysis. First correct wins."},
]

EFFICIENCY_CHALLENGES = [
    {"title": "Minimal Token Summarization", "desc": "Summarize 100 documents correctly using fewest total API tokens. Correctness verified, then lowest token count wins."},
    {"title": "Cheapest Data Pipeline", "desc": "Process 10,000 records through a defined pipeline. Correctness required. Lowest total compute cost wins."},
    {"title": "Efficient Translation", "desc": "Translate 1,000 sentences correctly using fewest API calls. Quality verified by BLEU score threshold. Fewest calls wins."},
    {"title": "Budget Classification", "desc": "Classify 5,000 items correctly spending the least on compute. Must meet 90% accuracy threshold. Lowest cost wins."},
    {"title": "Lean Web Scraping", "desc": "Extract structured data from 100 pages correctly. Fewest HTTP requests wins. Correctness verified."},
    {"title": "Minimal Memory Sort", "desc": "Sort 1M integers correctly using least peak memory. Correctness required. Lowest memory usage wins."},
    {"title": "Efficient Search", "desc": "Search a dataset for 1,000 queries. All results must be correct. Fewest total operations wins."},
    {"title": "Compressed Communication", "desc": "Transmit structured data correctly in fewest bytes. Receiver must reconstruct exact original. Fewest bytes wins."},
]

COMPETITION_TYPE_MAP = {
    "code_challenge": CODE_CHALLENGES,
    "data_challenge": DATA_CHALLENGES,
    "speed_challenge": SPEED_CHALLENGES,
    "efficiency_challenge": EFFICIENCY_CHALLENGES,
}

DAILY_SCHEDULE = {
    0: "code_challenge",      # Monday
    1: "data_challenge",      # Tuesday
    2: "speed_challenge",     # Wednesday
    3: "efficiency_challenge", # Thursday
    4: "code_challenge",      # Friday
    5: "data_challenge",      # Saturday
}

SCORING_INFO = {
    "code_challenge": "Scored by automated test suite. Tests passed (100% required), then code efficiency. Test suite published after close.",
    "data_challenge": "Scored by objective metric against held-out evaluation set. Evaluation data published after close.",
    "speed_challenge": "First correct submission wins. Correctness verified against published ground truth. Timestamped to millisecond.",
    "efficiency_challenge": "Correctness required (pass/fail), then lowest resource usage. Usage tracked and logged automatically.",
}


async def create_daily_competitions():
    now = datetime.utcnow()
    day = now.weekday()
    if day == 6:
        logger.info("Sunday — no competitions created")
        return

    comp_type = DAILY_SCHEDULE.get(day, "code_challenge")
    templates = COMPETITION_TYPE_MAP.get(comp_type, CODE_CHALLENGES)
    template = random.choice(templates)
    scoring = SCORING_INFO.get(comp_type, "Automated objective scoring.")

    end_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, hours=20)

    async with async_session() as db:
        existing = (await db.execute(
            select(ArenaGame).where(
                ArenaGame.game_type == comp_type,
                ArenaGame.created_at >= now.replace(hour=0, minute=0, second=0),
            )
        )).scalars().all()

        if existing:
            logger.info(f"Today's {comp_type} competitions already created")
            return

        created = 0
        tiers_to_create = ["open", "professional"]
        if day in (0, 4):
            tiers_to_create = ["open", "open"]

        for tier_key in tiers_to_create:
            tier = TIER_CONFIG[tier_key]
            total_prize = sum(tier["prizes"].values())
            prizes_str = ", ".join(f"#{k}: ${v}" for k, v in tier["prizes"].items())

            desc = (
                f"{template['desc']}\n\n"
                f"Scoring: {scoring}\n"
                f"Tier: {tier['label']} (${tier['entry_fee']} entry fee)\n"
                f"Guaranteed prizes: {prizes_str}\n"
                f"Prizes sponsored by Agiotage Protocol — not funded by entry fees.\n"
                f"Entry fee covers compute, scoring, and settlement infrastructure.\n"
                f"Minimum 3 entries to proceed. Full rules: agiotage.finance/rules"
            )

            competition = ArenaGame(
                game_type=comp_type,
                title=f"[{tier['label']}] {template['title']} — {now.strftime('%b %d')}",
                description=desc,
                entry_fee=tier["entry_fee"],
                max_participants=9999,
                current_participants=0,
                prize_pool=total_prize,
                rake_pct=Decimal("0"),
                status="OPEN",
                end_time=end_time,
            )
            db.add(competition)
            created += 1

        await db.commit()
        logger.info(f"Created {created} {comp_type} competitions: '{template['title']}'")


async def auto_cancel_underfilled():
    now = datetime.utcnow()
    async with async_session() as db:
        expired = (await db.execute(
            select(ArenaGame).where(
                ArenaGame.status == "OPEN",
                ArenaGame.end_time <= now,
                ArenaGame.end_time.isnot(None),
            )
        )).scalars().all()

        for comp in expired:
            if comp.current_participants < 3:
                participants = (await db.execute(
                    select(ArenaParticipant).where(ArenaParticipant.game_id == comp.id)
                )).scalars().all()

                for p in participants:
                    agent = (await db.execute(
                        select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
                    )).scalar_one_or_none()
                    if agent:
                        bal = (await db.execute(
                            select(AgentBalance).where(
                                AgentBalance.agent_id == agent.id, AgentBalance.token == "USDC",
                            ).with_for_update()
                        )).scalar_one_or_none()
                        if bal:
                            bal.locked_balance = Decimal(str(bal.locked_balance)) - comp.entry_fee

                comp.status = "CANCELLED"
                logger.info(f"Cancelled '{comp.title}' — {comp.current_participants}/3 entries. Full refund issued.")
            else:
                comp.status = "IN_PROGRESS"
                comp.start_time = now
                logger.info(f"'{comp.title}' now IN_PROGRESS ({comp.current_participants} entries)")

        if expired:
            await db.commit()


async def resolve_expired():
    now = datetime.utcnow()
    async with async_session() as db:
        expired = (await db.execute(
            select(ArenaGame).where(
                ArenaGame.status == "IN_PROGRESS",
                ArenaGame.end_time <= now + timedelta(hours=2),
                ArenaGame.end_time.isnot(None),
            )
        )).scalars().all()

        for comp in expired:
            submissions = (await db.execute(
                select(ArenaParticipant).where(
                    ArenaParticipant.game_id == comp.id,
                    ArenaParticipant.submission.isnot(None),
                )
            )).scalars().all()

            if not submissions:
                # No submissions — cancel and refund
                all_p = (await db.execute(
                    select(ArenaParticipant).where(ArenaParticipant.game_id == comp.id)
                )).scalars().all()
                for p in all_p:
                    agent = (await db.execute(
                        select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
                    )).scalar_one_or_none()
                    if agent:
                        bal = (await db.execute(
                            select(AgentBalance).where(
                                AgentBalance.agent_id == agent.id, AgentBalance.token == "USDC",
                            ).with_for_update()
                        )).scalar_one_or_none()
                        if bal:
                            bal.locked_balance = Decimal(str(bal.locked_balance)) - comp.entry_fee
                comp.status = "CANCELLED"
                logger.info(f"Cancelled '{comp.title}' — no submissions. Full refund.")
            else:
                comp.status = "COMPLETED"
                logger.info(f"'{comp.title}' COMPLETED — {len(submissions)} submissions, awaiting scoring")

        if expired:
            await db.commit()


async def run_worker():
    logger.info("Competition engine started — skill-based tournaments with sponsored prizes")
    last_daily = datetime.min

    while True:
        try:
            now = datetime.utcnow()
            if (now - last_daily).total_seconds() >= DAILY_CREATE_INTERVAL:
                await create_daily_competitions()
                last_daily = now
            await auto_cancel_underfilled()
            await resolve_expired()
        except Exception as e:
            logger.error(f"Competition engine error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_worker())
