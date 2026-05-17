# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Exit engine configuration — tiered scale-outs, ratcheting stops, trailing stop."""

EXIT_CONFIG = {
    "INITIAL_STOP_PCT": 0.40,         # 40% below entry
    "BREAKEVEN_TRIGGER": 1.40,        # at 1.4x, stop -> breakeven
    "LOCK_1R_TRIGGER": 1.80,          # at 1.8x, stop -> +40%
    "LOCK_2R_TRIGGER": 1.80,          # at 1.8x, stop -> +80% (was 2.2x — lock profits faster)
    "TRAILING_ACTIVATE": 3.00,        # at 3.0x, switch to trailing
    "TRAILING_DISTANCE_PCT": 0.12,    # trail 12% below high (was 15% — too much giveback)
    "TIER_1_TRIGGER": 1.50,           # sell 30% at 1.5x (was 25%)
    "TIER_1_SELL_PCT": 0.30,          # sell 30% at tier 1
    "TIER_2_TRIGGER": 2.00,           # sell 25% at 2x
    "TIER_3_TRIGGER": 3.00,           # sell 25% at 3x
    "MAX_HOLD_MS": 4 * 60 * 60 * 1000,  # 4h max hold (was 8h — timeouts avg -31%)
    "FLAT_EXIT_HOURS": 2,              # exit if <10% move after 2 hours (was 4h)
    "FLAT_EXIT_THRESHOLD_PCT": 10,     # "flat" = less than this % move
    "LIQUIDITY_RUG_THRESHOLD": 0.70,
    "SLIPPAGE_GUARD_PCT": 0.15,
    "CHUNK_EXIT_PIECES": 3,
    "CHUNK_EXIT_INTERVAL_MS": 10000,
}
