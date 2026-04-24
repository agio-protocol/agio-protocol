// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;

pub const MAX_TOKENS: usize = 8;
pub const MAX_AGENT_TOKENS: usize = 4;
pub const MAX_BATCH_PAYMENTS: usize = 25;

#[account]
#[derive(InitSpace)]
pub struct VaultState {
    pub authority: Pubkey,
    pub batch_signer: Pubkey,
    pub fee_collector: Pubkey,
    pub is_paused: bool,
    pub total_agents: u64,
    pub total_batches: u64,
    pub total_payments: u64,
    pub bump: u8,
    pub tracked_balances: [TrackedToken; MAX_TOKENS],
    pub circuit_breaker_window_start: i64,
    pub circuit_breaker_outflows: u64,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Default, InitSpace)]
pub struct TrackedToken {
    pub mint: Pubkey,
    pub total: u64,
}

#[account]
#[derive(InitSpace)]
pub struct AgentAccount {
    pub wallet: Pubkey,
    pub registered_at: i64,
    pub total_payments: u64,
    pub total_volume: u64,
    pub preferred_token: Pubkey,
    pub tier: u8,
    pub bump: u8,
    pub balances: [TokenBalance; MAX_AGENT_TOKENS],
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Default, InitSpace)]
pub struct TokenBalance {
    pub mint: Pubkey,
    pub available: u64,
    pub locked: u64,
}

#[account]
#[derive(InitSpace)]
pub struct ProcessedBatch {
    pub batch_id: [u8; 32],
    pub processed_at: i64,
    pub payment_count: u16,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone)]
pub struct BatchPayment {
    pub from: Pubkey,
    pub to: Pubkey,
    pub amount: u64,
    pub token_mint: Pubkey,
    pub payment_id: [u8; 32],
    pub fee: u64,
}

impl AgentAccount {
    pub fn get_balance_mut(&mut self, mint: &Pubkey) -> Option<&mut TokenBalance> {
        self.balances.iter_mut().find(|b| b.mint == *mint)
    }

    pub fn get_or_init_balance(&mut self, mint: &Pubkey) -> &mut TokenBalance {
        if let Some(idx) = self.balances.iter().position(|b| b.mint == *mint) {
            return &mut self.balances[idx];
        }
        let idx = self.balances.iter().position(|b| b.mint == Pubkey::default())
            .expect("No free token slots");
        self.balances[idx].mint = *mint;
        &mut self.balances[idx]
    }
}

impl VaultState {
    pub fn get_tracked_mut(&mut self, mint: &Pubkey) -> &mut TrackedToken {
        if let Some(idx) = self.tracked_balances.iter().position(|t| t.mint == *mint) {
            return &mut self.tracked_balances[idx];
        }
        let idx = self.tracked_balances.iter().position(|t| t.mint == Pubkey::default())
            .expect("No free tracked token slots");
        self.tracked_balances[idx].mint = *mint;
        &mut self.tracked_balances[idx]
    }
}
