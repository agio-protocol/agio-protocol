// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{self, Mint, TokenAccount, TokenInterface, TransferChecked};
use crate::state::{VaultState, AgentAccount};
use crate::constants::{VAULT_SEED, AGENT_SEED};
use crate::error::AgioError;

#[derive(Accounts)]
pub struct Deposit<'info> {
    #[account(
        mut,
        seeds = [VAULT_SEED],
        bump = vault_state.bump,
    )]
    pub vault_state: Account<'info, VaultState>,
    #[account(
        mut,
        seeds = [AGENT_SEED, wallet.key().as_ref()],
        bump = agent_account.bump,
    )]
    pub agent_account: Account<'info, AgentAccount>,
    #[account(mut)]
    pub agent_token_account: InterfaceAccount<'info, TokenAccount>,
    #[account(mut)]
    pub vault_token_account: InterfaceAccount<'info, TokenAccount>,
    pub mint: InterfaceAccount<'info, Mint>,
    #[account(mut)]
    pub wallet: Signer<'info>,
    pub token_program: Interface<'info, TokenInterface>,
}

pub fn handle_deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
    require!(!ctx.accounts.vault_state.is_paused, AgioError::VaultPaused);
    require!(amount > 0, AgioError::ZeroAmount);

    let mint_key = ctx.accounts.mint.key();
    let decimals = ctx.accounts.mint.decimals;

    let cpi_accounts = TransferChecked {
        from: ctx.accounts.agent_token_account.to_account_info(),
        to: ctx.accounts.vault_token_account.to_account_info(),
        authority: ctx.accounts.wallet.to_account_info(),
        mint: ctx.accounts.mint.to_account_info(),
    };
    let cpi_ctx = CpiContext::new(ctx.accounts.token_program.key(), cpi_accounts);
    token_interface::transfer_checked(cpi_ctx, amount, decimals)?;

    let agent = &mut ctx.accounts.agent_account;
    let balance = agent.get_or_init_balance(&mint_key);
    balance.available = balance.available.checked_add(amount).unwrap();

    let vault = &mut ctx.accounts.vault_state;
    let tracked = vault.get_tracked_mut(&mint_key);
    tracked.total = tracked.total.checked_add(amount).unwrap();

    msg!("Deposited {} of mint {}", amount, mint_key);
    Ok(())
}
