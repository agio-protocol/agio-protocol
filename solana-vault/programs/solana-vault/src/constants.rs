// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
pub const VAULT_SEED: &[u8] = b"vault";
pub const AGENT_SEED: &[u8] = b"agent";
pub const BATCH_SEED: &[u8] = b"batch";

pub const INSTANT_WITHDRAW_LIMIT: u64 = 1_000_000_000; // $1,000 in 6-decimal tokens
pub const MEDIUM_WITHDRAW_LIMIT: u64 = 10_000_000_000; // $10,000
pub const MEDIUM_WITHDRAW_DELAY: i64 = 3600;           // 1 hour
pub const LARGE_WITHDRAW_DELAY: i64 = 86400;           // 24 hours

pub const CIRCUIT_BREAKER_THRESHOLD_BPS: u64 = 2000;   // 20%
pub const CIRCUIT_BREAKER_WINDOW: i64 = 3600;           // 1 hour
