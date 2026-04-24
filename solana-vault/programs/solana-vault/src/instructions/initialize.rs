// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use crate::state::VaultState;
use crate::constants::VAULT_SEED;

#[derive(Accounts)]
pub struct InitializeVault<'info> {
    #[account(
        init,
        payer = authority,
        space = 8 + VaultState::INIT_SPACE,
        seeds = [VAULT_SEED],
        bump,
    )]
    pub vault_state: Account<'info, VaultState>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

pub fn handle_initialize(
    ctx: Context<InitializeVault>,
    batch_signer: Pubkey,
    fee_collector: Pubkey,
) -> Result<()> {
    let vault = &mut ctx.accounts.vault_state;
    vault.authority = ctx.accounts.authority.key();
    vault.batch_signer = batch_signer;
    vault.fee_collector = fee_collector;
    vault.is_paused = false;
    vault.total_agents = 0;
    vault.total_batches = 0;
    vault.total_payments = 0;
    vault.bump = ctx.bumps.vault_state;
    vault.circuit_breaker_window_start = 0;
    vault.circuit_breaker_outflows = 0;
    msg!("AGIO Vault initialized");
    Ok(())
}
