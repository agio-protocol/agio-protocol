use {
    anchor_lang::{prelude::Pubkey, solana_program::instruction::Instruction, InstructionData, ToAccountMetas, system_program},
    litesvm::LiteSVM,
    solana_message::{Message, VersionedMessage},
    solana_signer::Signer,
    solana_keypair::Keypair,
    solana_transaction::versioned::VersionedTransaction,
};

fn find_pda(seeds: &[&[u8]], program_id: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(seeds, program_id)
}

#[test]
fn test_initialize_vault() {
    let program_id = solana_vault::id();
    let payer = Keypair::new();
    let batch_signer = Pubkey::new_unique();
    let fee_collector = Pubkey::new_unique();
    let mut svm = LiteSVM::new();
    let bytes = include_bytes!("../../../target/deploy/solana_vault.so");
    svm.add_program(program_id, bytes).unwrap();
    svm.airdrop(&payer.pubkey(), 10_000_000_000).unwrap();

    let (vault_pda, _) = find_pda(&[b"vault"], &program_id);

    let instruction = Instruction::new_with_bytes(
        program_id,
        &solana_vault::instruction::InitializeVault {
            batch_signer,
            fee_collector,
        }.data(),
        solana_vault::accounts::InitializeVault {
            vault_state: vault_pda,
            authority: payer.pubkey(),
            system_program: system_program::ID,
        }.to_account_metas(None),
    );

    let blockhash = svm.latest_blockhash();
    let msg = Message::new_with_blockhash(&[instruction], Some(&payer.pubkey()), &blockhash);
    let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[&payer]).unwrap();

    let res = svm.send_transaction(tx);
    assert!(res.is_ok(), "Initialize vault failed: {:?}", res.err());
}
