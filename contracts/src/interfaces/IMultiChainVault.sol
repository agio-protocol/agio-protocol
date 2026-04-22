// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IMultiChainVault — Chain-agnostic vault interface
/// @notice Every chain-specific vault (Base EVM, Solana, Polygon, L1) implements
///         this interface. The pattern is identical — only the token standards differ.
///
/// LEVEL 2 ARCHITECTURE (interface only — build when adding new chains):
///
/// Base Vault (AgioVault.sol — LIVE):
///   Tokens: USDC, USDT, DAI, WETH, cbETH (ERC-20)
///   deposit/withdraw use SafeERC20
///
/// Solana Vault (future):
///   Tokens: SOL, USDC-SPL, other SPL tokens
///   deposit/withdraw use SPL token program
///   Account model: PDA per agent per token
///
/// Polygon Vault (future):
///   Tokens: MATIC, USDC, WETH (ERC-20, same as Base)
///   Deploy identical AgioVault.sol contract
///
/// Ethereum L1 Vault (future):
///   Tokens: ETH, USDC, WETH (ERC-20)
///   Same contract, higher gas costs — larger batch sizes recommended
///
/// Cross-chain payment flow:
///   1. Agent A (Solana, paying SOL) calls pay(to="agio:base:0x1234", amount=5)
///   2. AGIO off-chain service routes: debit SOL from Solana vault
///   3. Check Base reserves for receiver's preferred token (USDC)
///   4. Credit USDC to receiver from Base vault reserves
///   5. Background rebalancer: convert SOL → USDC via DEX, bridge via CCTP
///
/// Adding a new chain:
///   1. Deploy AgioVault.sol (EVM) or equivalent (non-EVM)
///   2. Whitelist supported tokens on that chain
///   3. Add chain to SupportedChain table with reserve balance
///   4. Fund initial reserves from treasury
///   5. Configure CCTP domain for bridging
///   6. Add chain prefix to router_service CHAIN_PREFIXES
interface IMultiChainVault {
    /// @notice Deposit tokens into the vault
    function deposit(address token, uint256 amount) external;

    /// @notice Withdraw tokens from the vault
    function withdraw(address token, uint256 amount) external;

    /// @notice Get agent's available balance for a token
    function balanceOf(address agent, address token) external view returns (uint256);

    /// @notice Get agent's locked balance for a token
    function lockedBalanceOf(address agent, address token) external view returns (uint256);

    /// @notice Settlement debit (called by batch settlement)
    function debit(address agent, address token, uint256 amount) external;

    /// @notice Settlement credit (called by batch settlement)
    function credit(address agent, address token, uint256 amount) external;

    /// @notice Check token whitelist
    function isWhitelistedToken(address token) external view returns (bool);

    /// @notice Per-token balance invariant check
    function checkInvariant(address token) external view returns (bool ok, uint256 tracked, uint256 actual);

    /// @notice Enforce invariant — pauses vault on violation
    function enforceInvariant(address token) external;

    event Deposited(address indexed agent, address indexed token, uint256 amount, uint256 timestamp);
    event Withdrawn(address indexed agent, address indexed token, uint256 amount, uint256 timestamp);
}
