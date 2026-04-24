// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
pub mod constants;
pub mod error;
pub mod instructions;
pub mod state;

use anchor_lang::prelude::*;

pub use instructions::*;
pub use state::*;

declare_id!("68RkssMLwfAWZ3Hf8TGF6poACgvo7ePPA8BzThqoMp6y");

#[program]
pub mod solana_vault {
    use super::*;

    pub fn initialize_vault(
        ctx: Context<InitializeVault>,
        batch_signer: Pubkey,
        fee_collector: Pubkey,
    ) -> Result<()> {
        handle_initialize(ctx, batch_signer, fee_collector)
    }

    pub fn register_agent(
        ctx: Context<RegisterAgent>,
        preferred_token: Pubkey,
    ) -> Result<()> {
        handle_register(ctx, preferred_token)
    }

    pub fn deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
        handle_deposit(ctx, amount)
    }

    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        handle_withdraw(ctx, amount)
    }

    pub fn settle_batch(
        ctx: Context<SettleBatch>,
        batch_id: [u8; 32],
        payments: Vec<state::BatchPayment>,
    ) -> Result<()> {
        handle_settle(ctx, batch_id, payments)
    }

    pub fn pause(ctx: Context<AdminAction>) -> Result<()> {
        instructions::admin::pause(ctx)
    }

    pub fn unpause(ctx: Context<AdminAction>) -> Result<()> {
        instructions::admin::unpause(ctx)
    }

    pub fn set_batch_signer(ctx: Context<AdminAction>, new_signer: Pubkey) -> Result<()> {
        instructions::admin::set_batch_signer(ctx, new_signer)
    }

    pub fn set_fee_collector(ctx: Context<AdminAction>, new_collector: Pubkey) -> Result<()> {
        instructions::admin::set_fee_collector(ctx, new_collector)
    }

    pub fn check_invariant(ctx: Context<CheckInvariant>, token_mint: Pubkey) -> Result<bool> {
        handle_check_invariant(ctx, token_mint)
    }
}
