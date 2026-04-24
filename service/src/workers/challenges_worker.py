"""Challenges worker — creates weekly contests from template library, auto-cancels, resolves."""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.database import async_session
from ..models.agent import Agent, AgentBalance
from ..models.platform import ArenaGame, ArenaParticipant, ArenaElo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("challenges_worker")

CHECK_INTERVAL = 300
DAILY_CREATE_INTERVAL = 86400
SERVICE_FEE_PCT = Decimal("5.0")

TIERS = [
    {"name": "Starter", "entry": Decimal("1"), "min_entries": 3},
    {"name": "Pro", "entry": Decimal("5"), "min_entries": 3},
    {"name": "Elite", "entry": Decimal("25"), "min_entries": 3},
    {"name": "Champion", "entry": Decimal("100"), "min_entries": 3},
]

# === CONTEST TEMPLATE LIBRARY (50 problems) ===

CODE_GOLF_PROBLEMS = [
    {"id": 1, "title": "FizzBuzz", "desc": "Print numbers 1-100. For multiples of 3 print 'Fizz', multiples of 5 print 'Buzz', both print 'FizzBuzz'. Fewest characters wins.", "tests": 20},
    {"id": 2, "title": "Fibonacci Sequence", "desc": "Return the nth Fibonacci number. F(0)=0, F(1)=1. Fewest characters wins.", "tests": 25},
    {"id": 3, "title": "Prime Generator", "desc": "Return all primes up to n. Fewest characters wins.", "tests": 20},
    {"id": 4, "title": "Palindrome Checker", "desc": "Return true if the input string is a palindrome (case-insensitive, alphanumeric only). Fewest characters wins.", "tests": 25},
    {"id": 5, "title": "Roman Numeral Converter", "desc": "Convert integer (1-3999) to Roman numeral string. Fewest characters wins.", "tests": 20},
    {"id": 6, "title": "JSON Parser", "desc": "Parse a JSON string into a nested data structure. Handle strings, numbers, booleans, arrays, objects. Fewest characters wins.", "tests": 30},
    {"id": 7, "title": "URL Shortener Hash", "desc": "Generate a unique 6-character hash for a given URL. Must be deterministic. Fewest characters wins.", "tests": 20},
    {"id": 8, "title": "Sorting Algorithm", "desc": "Sort an array of integers in ascending order. No built-in sort. Fewest characters wins.", "tests": 20},
    {"id": 9, "title": "Binary Search Tree", "desc": "Implement insert and search for a BST. Return true/false for search. Fewest characters wins.", "tests": 25},
    {"id": 10, "title": "Regex Matcher", "desc": "Implement basic regex matching with '.' (any char) and '*' (zero or more). Fewest characters wins.", "tests": 30},
]

SPEED_RACE_TASKS = [
    {"id": 11, "title": "Scrape Top 100 HN Posts", "desc": "Return the titles of the top 100 posts on Hacker News right now. Verified against live HN API."},
    {"id": 12, "title": "Top 20 Crypto Prices", "desc": "Return current USD prices of the top 20 cryptocurrencies by market cap. Verified against CoinGecko."},
    {"id": 13, "title": "Wikipedia Word Count", "desc": "Count all words in the English Wikipedia article for 'Artificial Intelligence'. Correct count wins."},
    {"id": 14, "title": "Find Broken Links", "desc": "Find all broken links (HTTP 4xx/5xx) on a specified webpage. Verified by re-checking each reported link."},
    {"id": 15, "title": "CSV Statistics", "desc": "Parse the provided CSV (10,000 rows) and compute mean, median, std dev, min, max for each numeric column."},
    {"id": 16, "title": "JSON to CSV", "desc": "Convert the provided nested JSON (5,000 records) to flat CSV format. Verified against expected output."},
    {"id": 17, "title": "Most Common Word", "desc": "Find the 10 most common words (excluding stop words) in the provided 50,000-word document."},
    {"id": 18, "title": "Distance Calculator", "desc": "Calculate haversine distances between 100 coordinate pairs. Sum must match expected total within 0.01 km."},
    {"id": 19, "title": "Email Validator", "desc": "Validate 1,000 email addresses against RFC 5322. Return valid/invalid for each. Verified against known list."},
    {"id": 20, "title": "SHA256 Hash Race", "desc": "Generate SHA256 hashes for 10,000 provided strings. All must match expected hashes. Fastest correct set wins."},
]

OPTIMIZATION_PROBLEMS = [
    {"id": 21, "title": "House Price Prediction", "desc": "Predict house prices from the provided dataset (5,000 training, 1,000 test). Lowest RMSE on test set wins.", "metric": "RMSE", "direction": "minimize"},
    {"id": 22, "title": "Text Compression", "desc": "Compress the provided 1MB text file. Smallest output wins. Must decompress to exact original.", "metric": "bytes", "direction": "minimize"},
    {"id": 23, "title": "Delivery Routing", "desc": "Route 50 deliveries across a city grid. Shortest total distance wins. All deliveries must be completed.", "metric": "distance_km", "direction": "minimize"},
    {"id": 24, "title": "Portfolio Optimization", "desc": "Select portfolio weights for 20 assets to maximize Sharpe ratio. Based on 1 year of historical data.", "metric": "sharpe_ratio", "direction": "maximize"},
    {"id": 25, "title": "Image Classification", "desc": "Classify 1,000 images into 10 categories. Highest accuracy wins. Pre-labeled test set for verification.", "metric": "accuracy", "direction": "maximize"},
    {"id": 26, "title": "Spam Detection", "desc": "Classify 5,000 emails as spam/not-spam. Highest F1 score wins. Labeled test set provided.", "metric": "f1_score", "direction": "maximize"},
    {"id": 27, "title": "Clustering Quality", "desc": "Cluster 10,000 data points into optimal groups. Highest silhouette score wins. No label hints.", "metric": "silhouette", "direction": "maximize"},
    {"id": 28, "title": "Feature Selection", "desc": "Achieve highest AUC on classification task using fewest features. Score = AUC / num_features.", "metric": "efficiency", "direction": "maximize"},
    {"id": 29, "title": "Hyperparameter Tuning", "desc": "Tune a model on the provided dataset. Best validation score wins. Limited to 100 training runs.", "metric": "val_score", "direction": "maximize"},
    {"id": 30, "title": "Anomaly Detection", "desc": "Detect anomalies in 50,000 time series points. Best precision-recall AUC wins. Known anomalies in test set.", "metric": "pr_auc", "direction": "maximize"},
]

DATA_HUNT_QUESTIONS = [
    {"id": 31, "title": "Bitcoin Hash Rate", "desc": "What was the total Bitcoin network hash rate (in EH/s) at 00:00 UTC today? Verified against blockchain.com and mempool.space."},
    {"id": 32, "title": "Most Traded Stock", "desc": "What was the most traded stock by volume on NYSE yesterday? Verified against NYSE official data."},
    {"id": 33, "title": "US City Populations", "desc": "What are the current estimated populations of the top 10 US cities? Verified against Census Bureau data."},
    {"id": 34, "title": "GitHub Trending", "desc": "List the top 10 trending GitHub repos by stars gained today. Verified against GitHub trending page."},
    {"id": 35, "title": "US Gas Prices", "desc": "What is the average gas price per gallon in each US state today? Verified against AAA and GasBuddy."},
    {"id": 36, "title": "Exchange Rates", "desc": "What are the current exchange rates for 20 major currency pairs vs USD? Verified against ECB and xe.com."},
    {"id": 37, "title": "Top AI Papers", "desc": "List the 10 most-cited AI papers published this month on arXiv. Verified against Semantic Scholar API."},
    {"id": 38, "title": "Ethereum Validators", "desc": "How many active Ethereum validators are there right now? Verified against beaconcha.in and etherscan.io."},
    {"id": 39, "title": "Solana TPS", "desc": "What is Solana's average TPS over the last 24 hours? Verified against Solana Explorer and validators.app."},
    {"id": 40, "title": "DeFi TVL", "desc": "What is the total TVL across the top 10 DeFi protocols? Verified against DefiLlama."},
]

STRESS_TEST_DATASETS = [
    {"id": 41, "title": "Sentiment Analysis 100K", "desc": "Classify 100,000 tweets as positive/negative sentiment. Score = correctly classified tweets.", "records": 100000},
    {"id": 42, "title": "Entity Extraction 50K", "desc": "Extract named entities from 50,000 news article snippets. Score = correctly extracted entities.", "records": 50000},
    {"id": 43, "title": "Translation 10K", "desc": "Translate 10,000 English sentences to Spanish. Score = BLEU score x sentences completed.", "records": 10000},
    {"id": 44, "title": "Summarization 5K", "desc": "Summarize 5,000 documents into 2-sentence summaries. Score = ROUGE score x documents completed.", "records": 5000},
    {"id": 45, "title": "Code Linting 20K", "desc": "Lint 20,000 Python files and report issues. Score = correctly identified issues.", "records": 20000},
    {"id": 46, "title": "Data Cleaning 100K", "desc": "Clean 100,000 messy CSV records (fix types, remove duplicates, fill missing). Score = correctly cleaned records.", "records": 100000},
    {"id": 47, "title": "Image Captioning 10K", "desc": "Generate captions for 10,000 images. Score = CIDEr score x images captioned.", "records": 10000},
    {"id": 48, "title": "QA 50K", "desc": "Answer 50,000 factual questions. Score = correct answers. Verified against known answer key.", "records": 50000},
    {"id": 49, "title": "Deduplication 200K", "desc": "Find and mark all duplicate records in a 200,000-row dataset. Score = correctly identified duplicates.", "records": 200000},
    {"id": 50, "title": "Text Classification 500K", "desc": "Classify 500,000 short texts into 20 categories. Score = correctly classified texts.", "records": 500000},
]

CONTEST_TYPE_MAP = {
    "code_golf": CODE_GOLF_PROBLEMS,
    "speed_race": SPEED_RACE_TASKS,
    "optimization": OPTIMIZATION_PROBLEMS,
    "data_hunt": DATA_HUNT_QUESTIONS,
    "stress_test": STRESS_TEST_DATASETS,
}

DAILY_SCHEDULE = {
    0: "code_golf",
    1: "optimization",
    2: "data_hunt",
    3: "speed_race",
    4: "stress_test",
    5: "code_golf",
}

SCORING_DESCRIPTIONS = {
    "code_golf": "CODE GOLF: Fewest characters that pass 100% of tests. Must pass all test cases to qualify. Tiebreaker: execution time, then submission time.",
    "speed_race": "SPEED RACE: First correct submission wins. Verified against ground truth. Timestamped to the millisecond.",
    "optimization": "OPTIMIZATION: Best score on the objective metric. Tiebreaker: earliest submission time.",
    "data_hunt": "DATA HUNT: Most accurate answer verified against 2+ public data sources. Tiebreaker: earliest submission time.",
    "stress_test": "STRESS TEST: Most records processed correctly within time limit. Score = total_processed x accuracy_rate.",
    "cost_efficiency": "COST EFFICIENCY: Cheapest correct solution as tracked by AGIO payments. Must meet all success criteria. Tiebreaker: completion time.",
}


async def create_daily_contests():
    now = datetime.utcnow()
    day = now.weekday()
    if day == 6:
        logger.info("Sunday — no new contests created")
        return

    contest_type = DAILY_SCHEDULE.get(day, "code_golf")
    templates = CONTEST_TYPE_MAP.get(contest_type, CODE_GOLF_PROBLEMS)
    template = random.choice(templates)
    scoring = SCORING_DESCRIPTIONS.get(contest_type, "Automated scoring.")

    end_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, hours=20)

    async with async_session() as db:
        existing = (await db.execute(
            select(ArenaGame).where(
                ArenaGame.game_type == contest_type,
                ArenaGame.created_at >= now.replace(hour=0, minute=0, second=0),
            )
        )).scalars().all()

        if existing:
            logger.info(f"Today's {contest_type} contests already created ({len(existing)} found)")
            return

        created = 0
        for tier in TIERS:
            desc_parts = [
                template["desc"],
                f"\nScoring: {scoring}",
                f"\nTier: {tier['name']} (${tier['entry']} entry)",
                f"Minimum {tier['min_entries']} entries to run. No maximum.",
                "Service fee: 5%. All submissions final and public after close.",
                f"\nFull rules: agiotage.finance/rules",
            ]

            challenge = ArenaGame(
                game_type=contest_type,
                title=f"[{tier['name']}] {template['title']} — {now.strftime('%b %d')}",
                description="\n".join(desc_parts),
                entry_fee=tier["entry"],
                max_participants=None,
                current_participants=0,
                prize_pool=Decimal("0"),
                rake_pct=SERVICE_FEE_PCT,
                status="OPEN",
                end_time=end_time,
            )
            db.add(challenge)
            created += 1

        await db.commit()
        logger.info(f"Created {created} {contest_type} contests: '{template['title']}' ({', '.join(t['name'] for t in TIERS)})")


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

        cancelled = 0
        for challenge in expired:
            min_entries = 3
            if challenge.current_participants < min_entries:
                participants = (await db.execute(
                    select(ArenaParticipant).where(ArenaParticipant.game_id == challenge.id)
                )).scalars().all()

                for p in participants:
                    agent = (await db.execute(
                        select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
                    )).scalar_one_or_none()
                    if agent:
                        bal = (await db.execute(
                            select(AgentBalance).where(
                                AgentBalance.agent_id == agent.id,
                                AgentBalance.token == "USDC",
                            ).with_for_update()
                        )).scalar_one_or_none()
                        if bal:
                            bal.locked_balance = Decimal(str(bal.locked_balance)) - challenge.entry_fee
                    p.prize_amount = challenge.entry_fee

                challenge.status = "CANCELLED"
                cancelled += 1
                logger.info(f"Auto-cancelled '{challenge.title}' — only {challenge.current_participants}/{min_entries} entries. Full refund.")
            else:
                challenge.status = "IN_PROGRESS"
                challenge.start_time = now
                logger.info(f"'{challenge.title}' now IN_PROGRESS ({challenge.current_participants} entries)")

        if cancelled or expired:
            await db.commit()


async def resolve_expired_challenges():
    now = datetime.utcnow()

    async with async_session() as db:
        expired = (await db.execute(
            select(ArenaGame).where(
                ArenaGame.status == "IN_PROGRESS",
                ArenaGame.end_time <= now + timedelta(hours=2),
                ArenaGame.end_time.isnot(None),
            )
        )).scalars().all()

        for challenge in expired:
            participants = (await db.execute(
                select(ArenaParticipant).where(
                    ArenaParticipant.game_id == challenge.id,
                    ArenaParticipant.submission.isnot(None),
                )
            )).scalars().all()

            if not participants:
                challenge.status = "CANCELLED"
                all_p = (await db.execute(
                    select(ArenaParticipant).where(ArenaParticipant.game_id == challenge.id)
                )).scalars().all()
                for p in all_p:
                    agent = (await db.execute(
                        select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
                    )).scalar_one_or_none()
                    if agent:
                        bal = (await db.execute(
                            select(AgentBalance).where(
                                AgentBalance.agent_id == agent.id,
                                AgentBalance.token == "USDC",
                            ).with_for_update()
                        )).scalar_one_or_none()
                        if bal:
                            bal.locked_balance = Decimal(str(bal.locked_balance)) - challenge.entry_fee
                    p.prize_amount = challenge.entry_fee
                logger.info(f"Cancelled '{challenge.title}' — no submissions. Full refund.")
            else:
                challenge.status = "COMPLETED"
                logger.info(f"'{challenge.title}' COMPLETED — {len(participants)} submissions, awaiting scoring")

        if expired:
            await db.commit()


async def run_worker():
    logger.info("Challenges worker started — 6 contest types, 50 problem templates")
    last_daily_creation = datetime.min

    while True:
        try:
            now = datetime.utcnow()

            if (now - last_daily_creation).total_seconds() >= DAILY_CREATE_INTERVAL:
                await create_daily_contests()
                last_daily_creation = now

            await auto_cancel_underfilled()
            await resolve_expired_challenges()

        except Exception as e:
            logger.error(f"Challenges worker error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_worker())
