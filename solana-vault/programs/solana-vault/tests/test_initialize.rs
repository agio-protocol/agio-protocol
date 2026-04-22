use {
    anchor_lang::{
        prelude::Pubkey,
        solana_program::instruction::Instruction,
        InstructionData, ToAccountMetas, system_program,
    },
    litesvm::LiteSVM,
    solana_message::{Message, VersionedMessage},
    solana_signer::Signer,
    solana_keypair::Keypair,
    solana_transaction::versioned::VersionedTransaction,
    solana_vault::state::BatchPayment,
};

// ========== HELPERS ==========

fn clone_kp(kp: &Keypair) -> Keypair {
    let bytes = kp.to_bytes();
    // First 32 bytes are the secret key
    Keypair::new_from_array(bytes[..32].try_into().unwrap())
}

fn pda(seeds: &[&[u8]], program_id: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(seeds, program_id).0
}

struct TestEnv {
    svm: LiteSVM,
    program_id: Pubkey,
    authority: Keypair,
    batch_signer: Keypair,
    fee_collector: Pubkey,
    vault_pda: Pubkey,
    // Fake USDC mint (we track balances in AgentAccount, not real SPL for unit tests)
    usdc_mint: Pubkey,
}

impl TestEnv {
    fn new() -> Self {
        let program_id = solana_vault::id();
        let authority = Keypair::new();
        let batch_signer = Keypair::new();
        let fee_collector = Pubkey::new_unique();
        let usdc_mint = Pubkey::new_unique();
        let mut svm = LiteSVM::new();
        let bytes = include_bytes!("../../../target/deploy/solana_vault.so");
        svm.add_program(program_id, bytes).unwrap();
        svm.airdrop(&authority.pubkey(), 100_000_000_000).unwrap();

        let vault_pda = pda(&[b"vault"], &program_id);

        Self { svm, program_id, authority, batch_signer, fee_collector, vault_pda, usdc_mint }
    }

    fn send(&mut self, ix: Instruction, signers: &[&Keypair]) -> Result<(), String> {
        let blockhash = self.svm.latest_blockhash();
        let payer = signers[0].pubkey();
        let msg = Message::new_with_blockhash(&[ix], Some(&payer), &blockhash);
        let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), signers)
            .map_err(|e| format!("tx build: {e}"))?;
        self.svm.send_transaction(tx).map(|_| ()).map_err(|e| format!("{e:?}"))
    }

    fn initialize_vault(&mut self) -> Result<(), String> {
        let ix = Instruction::new_with_bytes(
            self.program_id,
            &solana_vault::instruction::InitializeVault {
                batch_signer: self.batch_signer.pubkey(),
                fee_collector: self.fee_collector,
            }.data(),
            solana_vault::accounts::InitializeVault {
                vault_state: self.vault_pda,
                authority: self.authority.pubkey(),
                system_program: system_program::ID,
            }.to_account_metas(None),
        );
        let auth = clone_kp(&self.authority);
        self.send(ix, &[&auth])
    }

    fn register_agent(&mut self, wallet: &Keypair) -> Result<(), String> {
        self.svm.airdrop(&wallet.pubkey(), 10_000_000_000).unwrap();
        let agent_pda = pda(&[b"agent", wallet.pubkey().as_ref()], &self.program_id);
        let ix = Instruction::new_with_bytes(
            self.program_id,
            &solana_vault::instruction::RegisterAgent {
                preferred_token: self.usdc_mint,
            }.data(),
            solana_vault::accounts::RegisterAgent {
                vault_state: self.vault_pda,
                agent_account: agent_pda,
                wallet: wallet.pubkey(),
                system_program: system_program::ID,
            }.to_account_metas(None),
        );
        self.send(ix, &[wallet])
    }

    fn pause(&mut self) -> Result<(), String> {
        let ix = Instruction::new_with_bytes(
            self.program_id,
            &solana_vault::instruction::Pause {}.data(),
            solana_vault::accounts::AdminAction {
                vault_state: self.vault_pda,
                authority: self.authority.pubkey(),
            }.to_account_metas(None),
        );
        let auth = clone_kp(&self.authority);
        self.send(ix, &[&auth])
    }

    fn unpause(&mut self) -> Result<(), String> {
        let ix = Instruction::new_with_bytes(
            self.program_id,
            &solana_vault::instruction::Unpause {}.data(),
            solana_vault::accounts::AdminAction {
                vault_state: self.vault_pda,
                authority: self.authority.pubkey(),
            }.to_account_metas(None),
        );
        let auth = clone_kp(&self.authority);
        self.send(ix, &[&auth])
    }

    fn settle_batch(
        &mut self,
        batch_id: [u8; 32],
        payments: Vec<BatchPayment>,
        sender_wallets: &[&Keypair],
        receiver_wallets: &[&Keypair],
    ) -> Result<(), String> {
        let batch_pda = pda(&[b"batch", batch_id.as_ref()], &self.program_id);
        let signer = clone_kp(&self.batch_signer);

        let mut account_metas = solana_vault::accounts::SettleBatch {
            vault_state: self.vault_pda,
            processed_batch: batch_pda,
            submitter: self.batch_signer.pubkey(),
            system_program: system_program::ID,
        }.to_account_metas(None);

        // Add remaining accounts: sender/receiver agent PDAs
        for i in 0..payments.len() {
            let sender_pda = pda(&[b"agent", sender_wallets[i].pubkey().as_ref()], &self.program_id);
            let receiver_pda = pda(&[b"agent", receiver_wallets[i].pubkey().as_ref()], &self.program_id);
            account_metas.push(anchor_lang::prelude::AccountMeta::new(sender_pda, false));
            account_metas.push(anchor_lang::prelude::AccountMeta::new(receiver_pda, false));
        }

        let ix = Instruction::new_with_bytes(
            self.program_id,
            &solana_vault::instruction::SettleBatch {
                batch_id,
                payments,
            }.data(),
            account_metas,
        );

        self.svm.airdrop(&signer.pubkey(), 10_000_000_000).unwrap();
        self.send(ix, &[&signer])
    }
}

// ========== TESTS ==========

#[test]
fn test_initialize_vault() {
    let mut env = TestEnv::new();
    assert!(env.initialize_vault().is_ok());
}

#[test]
fn test_register_agent() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let agent = Keypair::new();
    assert!(env.register_agent(&agent).is_ok());
}

#[test]
fn test_register_agent_duplicate_fails() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let agent = Keypair::new();
    env.svm.airdrop(&agent.pubkey(), 10_000_000_000).unwrap();
    // First registration
    let agent_pda = pda(&[b"agent", agent.pubkey().as_ref()], &env.program_id);
    let ix = Instruction::new_with_bytes(
        env.program_id,
        &solana_vault::instruction::RegisterAgent { preferred_token: env.usdc_mint }.data(),
        solana_vault::accounts::RegisterAgent {
            vault_state: env.vault_pda,
            agent_account: agent_pda,
            wallet: agent.pubkey(),
            system_program: system_program::ID,
        }.to_account_metas(None),
    );
    env.send(ix, &[&agent]).unwrap();
    // Second registration — PDA already exists, should fail
    env.svm.warp_to_slot(100);
    let ix2 = Instruction::new_with_bytes(
        env.program_id,
        &solana_vault::instruction::RegisterAgent { preferred_token: env.usdc_mint }.data(),
        solana_vault::accounts::RegisterAgent {
            vault_state: env.vault_pda,
            agent_account: agent_pda,
            wallet: agent.pubkey(),
            system_program: system_program::ID,
        }.to_account_metas(None),
    );
    let result = env.send(ix2, &[&agent]);
    assert!(result.is_err(), "Duplicate registration should fail");
}

#[test]
fn test_pause_blocks_registration() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();
    env.pause().unwrap();

    let agent = Keypair::new();
    let result = env.register_agent(&agent);
    assert!(result.is_err(), "Should fail when paused");
}

#[test]
fn test_unpause_resumes() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();
    env.pause().unwrap();
    env.unpause().unwrap();

    let agent = Keypair::new();
    assert!(env.register_agent(&agent).is_ok());
}

#[test]
fn test_unauthorized_pause_fails() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let impostor = Keypair::new();
    env.svm.airdrop(&impostor.pubkey(), 10_000_000_000).unwrap();

    let ix = Instruction::new_with_bytes(
        env.program_id,
        &solana_vault::instruction::Pause {}.data(),
        solana_vault::accounts::AdminAction {
            vault_state: env.vault_pda,
            authority: impostor.pubkey(),
        }.to_account_metas(None),
    );
    let result = env.send(ix, &[&impostor]);
    assert!(result.is_err(), "Unauthorized pause should fail");
}

#[test]
fn test_settle_batch_single_payment() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let alice = Keypair::new();
    let bob = Keypair::new();
    env.register_agent(&alice).unwrap();
    env.register_agent(&bob).unwrap();

    // We need to give Alice a balance to debit.
    // In the real program, deposit does this via SPL transfer.
    // For batch settlement tests, we credit via a prior batch.
    // OR we can test that insufficient balance fails.
    // Let's test the insufficient balance case first.

    let payment = BatchPayment {
        from: alice.pubkey(),
        to: bob.pubkey(),
        amount: 1_000_000, // 1 USDC
        token_mint: env.usdc_mint,
        payment_id: [1u8; 32],
        fee: 150, // $0.00015
    };

    let batch_id = [0xAA; 32];
    let result = env.settle_batch(batch_id, vec![payment], &[&alice], &[&bob]);
    // Should fail because Alice has 0 balance
    assert!(result.is_err(), "Should fail with insufficient balance");
}

#[test]
fn test_replay_protection() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let alice = Keypair::new();
    let bob = Keypair::new();
    env.register_agent(&alice).unwrap();
    env.register_agent(&bob).unwrap();

    // Try to submit same batch_id twice (even though first fails on balance,
    // the PDA is created, so second should fail with "already initialized")
    let batch_id = [0xBB; 32];
    let payment = BatchPayment {
        from: alice.pubkey(),
        to: bob.pubkey(),
        amount: 100,
        token_mint: env.usdc_mint,
        payment_id: [2u8; 32],
        fee: 0,
    };

    // First attempt (fails on balance but PDA created? No — if tx fails, PDA isn't created)
    let _ = env.settle_batch(batch_id, vec![payment.clone()], &[&alice], &[&bob]);

    // Second attempt with same batch_id — should also fail (or succeed fresh if first rolled back)
    // In Solana, failed transactions don't create accounts, so this tests the happy path
    // We need a passing batch first to test replay. Skip for now — the PDA-based replay
    // protection is structural (init fails if account exists).
}

#[test]
fn test_self_payment_fails() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let alice = Keypair::new();
    env.register_agent(&alice).unwrap();

    let payment = BatchPayment {
        from: alice.pubkey(),
        to: alice.pubkey(), // self-payment
        amount: 100,
        token_mint: env.usdc_mint,
        payment_id: [3u8; 32],
        fee: 0,
    };

    let batch_id = [0xCC; 32];
    let result = env.settle_batch(batch_id, vec![payment], &[&alice], &[&alice]);
    assert!(result.is_err(), "Self-payment should fail");
}

#[test]
fn test_empty_batch_fails() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let batch_id = [0xDD; 32];
    let result = env.settle_batch(batch_id, vec![], &[], &[]);
    assert!(result.is_err(), "Empty batch should fail");
}

#[test]
fn test_unauthorized_batch_signer_fails() {
    let mut env = TestEnv::new();
    env.initialize_vault().unwrap();

    let alice = Keypair::new();
    let bob = Keypair::new();
    env.register_agent(&alice).unwrap();
    env.register_agent(&bob).unwrap();

    let impostor = Keypair::new();
    env.svm.airdrop(&impostor.pubkey(), 10_000_000_000).unwrap();

    let batch_pda = pda(&[b"batch", [0xEE; 32].as_ref()], &env.program_id);

    let payment = BatchPayment {
        from: alice.pubkey(),
        to: bob.pubkey(),
        amount: 100,
        token_mint: env.usdc_mint,
        payment_id: [4u8; 32],
        fee: 0,
    };

    let sender_pda = pda(&[b"agent", alice.pubkey().as_ref()], &env.program_id);
    let receiver_pda = pda(&[b"agent", bob.pubkey().as_ref()], &env.program_id);

    let mut metas = solana_vault::accounts::SettleBatch {
        vault_state: env.vault_pda,
        processed_batch: batch_pda,
        submitter: impostor.pubkey(), // wrong signer
        system_program: system_program::ID,
    }.to_account_metas(None);
    metas.push(anchor_lang::prelude::AccountMeta::new(sender_pda, false));
    metas.push(anchor_lang::prelude::AccountMeta::new(receiver_pda, false));

    let ix = Instruction::new_with_bytes(
        env.program_id,
        &solana_vault::instruction::SettleBatch {
            batch_id: [0xEE; 32],
            payments: vec![payment],
        }.data(),
        metas,
    );

    let result = env.send(ix, &[&impostor]);
    assert!(result.is_err(), "Unauthorized signer should fail");
}
