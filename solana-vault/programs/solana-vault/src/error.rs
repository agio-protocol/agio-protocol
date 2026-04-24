// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;

#[error_code]
pub enum AgioError {
    #[msg("Vault is paused")]
    VaultPaused,
    #[msg("Insufficient balance")]
    InsufficientBalance,
    #[msg("Zero amount")]
    ZeroAmount,
    #[msg("Agent already registered")]
    AlreadyRegistered,
    #[msg("Self-payment not allowed")]
    SelfPayment,
    #[msg("Invalid batch signature")]
    InvalidSignature,
    #[msg("Batch already processed")]
    BatchAlreadyProcessed,
    #[msg("Batch is empty")]
    EmptyBatch,
    #[msg("Batch exceeds maximum size")]
    BatchTooLarge,
    #[msg("No free token slots")]
    NoFreeTokenSlots,
    #[msg("Unauthorized")]
    Unauthorized,
    #[msg("Circuit breaker triggered — outflows exceed threshold")]
    CircuitBreakerTriggered,
    #[msg("Withdrawal delay not elapsed")]
    WithdrawalDelayNotElapsed,
    #[msg("Invariant violated — tracked balance != actual balance")]
    InvariantViolation,
}
