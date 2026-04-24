// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use crate::state::{VaultState, AgentAccount};
use crate::constants::{VAULT_SEED, AGENT_SEED};
use crate::error::AgioError;

#[derive(Accounts)]
pub struct RegisterAgent<'info> {
    #[account(
        mut,
        seeds = [VAULT_SEED],
        bump = vault_state.bump,
    )]
    pub vault_state: Account<'info, VaultState>,
    #[account(
        init,
        payer = wallet,
        space = 8 + AgentAccount::INIT_SPACE,
        seeds = [AGENT_SEED, wallet.key().as_ref()],
        bump,
    )]
    pub agent_account: Account<'info, AgentAccount>,
    #[account(mut)]
    pub wallet: Signer<'info>,
    pub system_program: Program<'info, System>,
}

pub fn handle_register(
    ctx: Context<RegisterAgent>,
    preferred_token: Pubkey,
) -> Result<()> {
    require!(!ctx.accounts.vault_state.is_paused, AgioError::VaultPaused);

    let agent = &mut ctx.accounts.agent_account;
    agent.wallet = ctx.accounts.wallet.key();
    agent.registered_at = Clock::get()?.unix_timestamp;
    agent.total_payments = 0;
    agent.total_volume = 0;
    agent.preferred_token = preferred_token;
    agent.tier = 0; // SPARK
    agent.bump = ctx.bumps.agent_account;

    let vault = &mut ctx.accounts.vault_state;
    vault.total_agents += 1;

    msg!("Agent registered: {}", ctx.accounts.wallet.key());
    Ok(())
}
