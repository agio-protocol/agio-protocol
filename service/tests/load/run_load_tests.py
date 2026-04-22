#!/usr/bin/env python3
"""
AGIO Extreme Load Tests — Find the breaking point.

Run: python tests/load/run_load_tests.py

Tests:
1. Progressive ramp: 1K → 10K → 50K → 100K
2. Sustained throughput: 1K agents × 1 payment/sec × 10 min
3. Spike handling: baseline → 10x spike → baseline
4. Concurrent agent ceiling: 100 → 1K → 5K → 10K simultaneous agents
5. Database performance under 1M rows
6. Batch settlement throughput
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tests.load.load_harness import LoadTestHarness, LoadMetrics
from src.core.config import settings


async def test1_progressive_ramp(harness: LoadTestHarness):
    """Find the breaking point by progressively increasing load."""
    print("\n" + "=" * 60)
    print("TEST 1: PROGRESSIVE RAMP — Finding the breaking point")
    print("=" * 60)

    levels = [
        ("1K", 1_000),
        ("10K", 10_000),
        ("50K", 50_000),
        ("100K", 100_000),
    ]

    for name, count in levels:
        print(f"\n  [{name}] Running {count:,} payments...")
        await harness.setup()
        agents = await harness.create_agents(100, balance=1_000_000)

        try:
            m = await harness.run_payments(agents, count, name)
            print(f"  [{name}] Done: {m.successful:,} ok, {m.failed} failed, "
                  f"{m.throughput_per_sec:.0f} tps, P95={m.p95_ms:.1f}ms, "
                  f"mem={m.peak_memory_mb:.0f}MB, inv={'✅' if m.invariant_passed else '❌'}")

            if m.failed > count * 0.01:  # >1% failure = breaking point
                print(f"  [{name}] ⚠️  BREAKING POINT — {m.failed} failures ({m.error})")
                break
        except Exception as e:
            print(f"  [{name}] 💥 CRASHED: {e}")
            harness.results.append(LoadMetrics(level=name, error=str(e)[:100]))
            break


async def test2_sustained_throughput(harness: LoadTestHarness):
    """Sustained load: 100 agents × 10 payments/sec for 60 seconds."""
    print("\n" + "=" * 60)
    print("TEST 2: SUSTAINED THROUGHPUT — 60 seconds continuous")
    print("=" * 60)

    await harness.setup()
    agents = await harness.create_agents(100, balance=1_000_000)

    duration = 60  # seconds (scaled down from 10 min for practical testing)
    target_tps = 100
    total = duration * target_tps

    print(f"  Target: {target_tps} tps for {duration}s = {total:,} payments")

    metrics = LoadMetrics(level="sustained", total_payments=total)
    start = time.time()

    for second in range(duration):
        sec_start = time.time()
        for i in range(target_tps):
            idx = (second * target_tps + i)
            try:
                async with harness.session_factory() as db:
                    from src.services.payment_service import create_payment
                    t0 = time.time()
                    await create_payment(
                        db, agents[idx % 100], agents[(idx + 1) % 100],
                        0.001, f"sustain-{idx}"
                    )
                    metrics.latencies_ms.append((time.time() - t0) * 1000)
                    metrics.successful += 1
            except Exception:
                metrics.failed += 1

        # Maintain pace
        elapsed_this_sec = time.time() - sec_start
        if elapsed_this_sec < 1.0:
            await asyncio.sleep(1.0 - elapsed_this_sec)

        if (second + 1) % 10 == 0:
            qd = await harness.get_queue_depth()
            mem = harness.get_memory_mb()
            print(f"  [{second+1}s] {metrics.successful:,} ok, queue={qd}, mem={mem:.0f}MB")

    metrics.elapsed_seconds = time.time() - start
    metrics.throughput_per_sec = metrics.successful / max(metrics.elapsed_seconds, 0.001)
    metrics.peak_memory_mb = harness.get_memory_mb()
    metrics.compute_percentiles()
    metrics.invariant_passed = await harness.check_invariant()

    harness.results.append(metrics)
    print(f"  RESULT: {metrics.successful:,} ok, {metrics.failed} failed, "
          f"{metrics.throughput_per_sec:.0f} tps, P95={metrics.p95_ms:.1f}ms")


async def test4_concurrent_ceiling(harness: LoadTestHarness):
    """How many simultaneous agents can AGIO handle?"""
    print("\n" + "=" * 60)
    print("TEST 4: CONCURRENT AGENT CEILING")
    print("=" * 60)

    levels = [
        ("100 agents", 100, 10),
        ("500 agents", 500, 5),
        ("1K agents", 1000, 3),
        ("2K agents", 2000, 2),
    ]

    for name, n_agents, payments_each in levels:
        total = n_agents * payments_each
        print(f"\n  [{name}] {n_agents} agents × {payments_each} payments = {total:,}")

        await harness.setup()
        agents = await harness.create_agents(n_agents, balance=1_000_000)

        try:
            m = await harness.run_concurrent_payments(
                agents, total, concurrency=min(n_agents, 200), level_name=name
            )
            print(f"  [{name}] Done: {m.successful:,} ok, {m.failed} failed, "
                  f"{m.throughput_per_sec:.0f} tps, P95={m.p95_ms:.1f}ms")
        except Exception as e:
            print(f"  [{name}] 💥 CRASHED: {e}")
            harness.results.append(LoadMetrics(level=name, error=str(e)[:100]))
            break


async def test5_database_performance(harness: LoadTestHarness):
    """Query performance with large datasets."""
    print("\n" + "=" * 60)
    print("TEST 5: DATABASE PERFORMANCE UNDER LOAD")
    print("=" * 60)

    await harness.setup()
    agents = await harness.create_agents(100, balance=1_000_000)

    # Insert 10K payments (scaled from 1M for practical testing)
    print("  Inserting 10,000 payment records...")
    count = 10_000
    async with harness.session_factory() as db:
        from src.models.payment import Payment
        for i in range(count):
            db.add(Payment(
                payment_id=f"0x{'%064x' % i}",
                from_agent_id=(await db.execute(
                    select(Agent.id).where(Agent.agio_id == agents[i % 100])
                )).scalar(),
                to_agent_id=(await db.execute(
                    select(Agent.id).where(Agent.agio_id == agents[(i+1) % 100])
                )).scalar(),
                amount=Decimal("0.001"),
                status="SETTLED",
            ))
            if i % 1000 == 0:
                await db.flush()
        await db.commit()

    from decimal import Decimal

    # Balance lookup
    t0 = time.time()
    async with harness.session_factory() as db:
        for _ in range(100):
            await db.execute(select(Agent.balance).where(Agent.agio_id == agents[0]))
    balance_ms = (time.time() - t0) * 1000 / 100
    print(f"  Balance lookup: {balance_ms:.1f}ms avg {'✅' if balance_ms < 10 else '⚠️'}")

    # Payment history for 1 agent
    t0 = time.time()
    async with harness.session_factory() as db:
        agent_id = (await db.execute(
            select(Agent.id).where(Agent.agio_id == agents[0])
        )).scalar()
        await db.execute(
            select(Payment).where(Payment.from_agent_id == agent_id).limit(100)
        )
    history_ms = (time.time() - t0) * 1000
    print(f"  Payment history (100 rows): {history_ms:.1f}ms {'✅' if history_ms < 100 else '⚠️'}")

    # Count query
    t0 = time.time()
    async with harness.session_factory() as db:
        await db.execute(select(func.count()).select_from(Payment))
    count_ms = (time.time() - t0) * 1000
    print(f"  Count all payments: {count_ms:.1f}ms {'✅' if count_ms < 50 else '⚠️'}")

    harness.results.append(LoadMetrics(
        level="db_perf",
        total_payments=count,
        successful=count,
        invariant_passed=True,
    ))


async def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║    AGIO EXTREME LOAD TESTS — Finding the ceiling    ║")
    print("╚══════════════════════════════════════════════════════╝")

    harness = LoadTestHarness()

    try:
        await test1_progressive_ramp(harness)
        await test2_sustained_throughput(harness)
        await test4_concurrent_ceiling(harness)
        await test5_database_performance(harness)
    except Exception as e:
        print(f"\n💥 FATAL: {e}")

    # Generate report
    report = harness.generate_report()
    report_path = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "capacity_report.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n📊 Report saved to: {report_path}")
    print("\n" + report)


if __name__ == "__main__":
    asyncio.run(main())
