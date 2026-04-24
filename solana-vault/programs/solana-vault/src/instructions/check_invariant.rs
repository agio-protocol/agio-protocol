// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use anchor_spl::token_interface::TokenAccount;
use crate::state::VaultState;
use crate::constants::VAULT_SEED;

#[derive(Accounts)]
pub struct CheckInvariant<'info> {
    #[account(
        seeds = [VAULT_SEED],
        bump = vault_state.bump,
    )]
    pub vault_state: Account<'info, VaultState>,
    pub vault_token_account: InterfaceAccount<'info, TokenAccount>,
}

pub fn handle_check_invariant(ctx: Context<CheckInvariant>, token_mint: Pubkey) -> Result<bool> {
    let vault = &ctx.accounts.vault_state;
    let actual = ctx.accounts.vault_token_account.amount;

    let tracked = vault.tracked_balances.iter()
        .find(|t| t.mint == token_mint)
        .map(|t| t.total)
        .unwrap_or(0);

    let ok = tracked == actual;
    if ok {
        msg!("Invariant OK: tracked={} actual={}", tracked, actual);
    } else {
        msg!("INVARIANT VIOLATED: tracked={} actual={}", tracked, actual);
    }
    Ok(ok)
}
