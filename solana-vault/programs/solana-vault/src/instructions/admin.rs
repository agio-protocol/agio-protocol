// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use crate::state::VaultState;
use crate::constants::VAULT_SEED;
use crate::error::AgioError;

#[derive(Accounts)]
pub struct AdminAction<'info> {
    #[account(
        mut,
        seeds = [VAULT_SEED],
        bump = vault_state.bump,
        has_one = authority @ AgioError::Unauthorized,
    )]
    pub vault_state: Account<'info, VaultState>,
    pub authority: Signer<'info>,
}

pub fn pause(ctx: Context<AdminAction>) -> Result<()> {
    ctx.accounts.vault_state.is_paused = true;
    msg!("Vault PAUSED");
    Ok(())
}

pub fn unpause(ctx: Context<AdminAction>) -> Result<()> {
    ctx.accounts.vault_state.is_paused = false;
    msg!("Vault UNPAUSED");
    Ok(())
}

pub fn set_batch_signer(ctx: Context<AdminAction>, new_signer: Pubkey) -> Result<()> {
    ctx.accounts.vault_state.batch_signer = new_signer;
    msg!("Batch signer updated to {}", new_signer);
    Ok(())
}

pub fn set_fee_collector(ctx: Context<AdminAction>, new_collector: Pubkey) -> Result<()> {
    ctx.accounts.vault_state.fee_collector = new_collector;
    msg!("Fee collector updated to {}", new_collector);
    Ok(())
}
