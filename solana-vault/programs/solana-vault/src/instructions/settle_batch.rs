// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
use anchor_lang::prelude::*;
use crate::state::*;
use crate::constants::*;
use crate::error::AgioError;

#[derive(Accounts)]
#[instruction(batch_id: [u8; 32])]
pub struct SettleBatch<'info> {
    #[account(
        mut,
        seeds = [VAULT_SEED],
        bump = vault_state.bump,
    )]
    pub vault_state: Account<'info, VaultState>,
    #[account(
        init,
        payer = submitter,
        space = 8 + ProcessedBatch::INIT_SPACE,
        seeds = [BATCH_SEED, batch_id.as_ref()],
        bump,
    )]
    pub processed_batch: Account<'info, ProcessedBatch>,
    #[account(mut)]
    pub submitter: Signer<'info>,
    pub system_program: Program<'info, System>,
    // Remaining accounts: pairs of (sender_agent, receiver_agent) AccountInfos
}

pub fn handle_settle(
    ctx: Context<SettleBatch>,
    batch_id: [u8; 32],
    payments: Vec<BatchPayment>,
) -> Result<()> {
    let vault = &mut ctx.accounts.vault_state;
    require!(!vault.is_paused, AgioError::VaultPaused);
    require!(!payments.is_empty(), AgioError::EmptyBatch);
    require!(payments.len() <= MAX_BATCH_PAYMENTS, AgioError::BatchTooLarge);

    // Verify submitter is the authorized batch signer
    require!(
        ctx.accounts.submitter.key() == vault.batch_signer,
        AgioError::Unauthorized
    );

    // Process each payment using remaining_accounts
    // remaining_accounts layout: [sender_0, receiver_0, sender_1, receiver_1, ...]
    let remaining = &ctx.remaining_accounts;
    require!(
        remaining.len() >= payments.len() * 2,
        AgioError::EmptyBatch
    );

    let mut total_volume: u64 = 0;

    for (i, payment) in payments.iter().enumerate() {
        require!(payment.amount > 0, AgioError::ZeroAmount);
        require!(payment.from != payment.to, AgioError::SelfPayment);

        let sender_info = &remaining[i * 2];
        let receiver_info = &remaining[i * 2 + 1];

        // Deserialize sender agent account
        let mut sender_data = sender_info.try_borrow_mut_data()?;
        let mut sender: AgentAccount = AgentAccount::try_deserialize(&mut &sender_data[..])?;

        // Debit sender
        let sender_bal = sender.get_balance_mut(&payment.token_mint)
            .ok_or(AgioError::InsufficientBalance)?;
        let total_debit = payment.amount.checked_add(payment.fee).unwrap();
        require!(sender_bal.available >= total_debit, AgioError::InsufficientBalance);
        sender_bal.available = sender_bal.available.checked_sub(total_debit).unwrap();
        sender.total_payments += 1;
        sender.total_volume = sender.total_volume.checked_add(payment.amount).unwrap();

        // Serialize sender back
        let mut writer = std::io::Cursor::new(&mut sender_data[..]);
        sender.try_serialize(&mut writer)?;
        drop(sender_data);

        // Deserialize receiver agent account
        let mut receiver_data = receiver_info.try_borrow_mut_data()?;
        let mut receiver: AgentAccount = AgentAccount::try_deserialize(&mut &receiver_data[..])?;

        // Credit receiver
        let receiver_bal = receiver.get_or_init_balance(&payment.token_mint);
        receiver_bal.available = receiver_bal.available.checked_add(payment.amount).unwrap();
        receiver.total_payments += 1;
        receiver.total_volume = receiver.total_volume.checked_add(payment.amount).unwrap();

        // Serialize receiver back
        let mut writer = std::io::Cursor::new(&mut receiver_data[..]);
        receiver.try_serialize(&mut writer)?;
        drop(receiver_data);

        total_volume = total_volume.checked_add(payment.amount).unwrap();
    }

    // Record batch as processed (replay protection)
    let batch = &mut ctx.accounts.processed_batch;
    batch.batch_id = batch_id;
    batch.processed_at = Clock::get()?.unix_timestamp;
    batch.payment_count = payments.len() as u16;

    // Update vault stats
    vault.total_batches += 1;
    vault.total_payments += payments.len() as u64;

    msg!("Batch settled: {} payments, volume {}", payments.len(), total_volume);
    Ok(())
}
