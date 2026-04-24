// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{self, Mint, TokenAccount, TokenInterface, TransferChecked};
use crate::state::{VaultState, AgentAccount};
use crate::constants::*;
use crate::error::AgioError;

#[derive(Accounts)]
pub struct Withdraw<'info> {
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

pub fn handle_withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
    require!(!ctx.accounts.vault_state.is_paused, AgioError::VaultPaused);
    require!(amount > 0, AgioError::ZeroAmount);

    let mint_key = ctx.accounts.mint.key();
    let decimals = ctx.accounts.mint.decimals;
    let agent = &mut ctx.accounts.agent_account;

    let balance = agent.get_balance_mut(&mint_key).ok_or(AgioError::InsufficientBalance)?;
    require!(balance.available >= amount, AgioError::InsufficientBalance);
    require!(amount <= INSTANT_WITHDRAW_LIMIT, AgioError::WithdrawalDelayNotElapsed);

    let vault = &mut ctx.accounts.vault_state;
    let now = Clock::get()?.unix_timestamp;
    if now > vault.circuit_breaker_window_start + CIRCUIT_BREAKER_WINDOW {
        vault.circuit_breaker_window_start = now;
        vault.circuit_breaker_outflows = 0;
    }
    vault.circuit_breaker_outflows = vault.circuit_breaker_outflows.checked_add(amount).unwrap();

    let tracked_total = vault.tracked_balances.iter()
        .find(|t| t.mint == mint_key)
        .map(|t| t.total)
        .unwrap_or(0);
    let threshold = (tracked_total as u128)
        .checked_mul(CIRCUIT_BREAKER_THRESHOLD_BPS as u128).unwrap()
        .checked_div(10_000).unwrap() as u64;
    require!(vault.circuit_breaker_outflows <= threshold, AgioError::CircuitBreakerTriggered);

    balance.available = balance.available.checked_sub(amount).unwrap();
    let tracked = vault.get_tracked_mut(&mint_key);
    tracked.total = tracked.total.checked_sub(amount).unwrap();

    let seeds = &[VAULT_SEED, &[vault.bump]];
    let signer_seeds = &[&seeds[..]];
    let cpi_accounts = TransferChecked {
        from: ctx.accounts.vault_token_account.to_account_info(),
        to: ctx.accounts.agent_token_account.to_account_info(),
        authority: ctx.accounts.vault_state.to_account_info(),
        mint: ctx.accounts.mint.to_account_info(),
    };
    let cpi_ctx = CpiContext::new_with_signer(
        ctx.accounts.token_program.key(),
        cpi_accounts,
        signer_seeds,
    );
    token_interface::transfer_checked(cpi_ctx, amount, decimals)?;

    msg!("Withdrew {} of mint {}", amount, mint_key);
    Ok(())
}
