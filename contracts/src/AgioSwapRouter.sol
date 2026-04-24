// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IAgioVault} from "./interfaces/IAgioVault.sol";

/// @title AgioSwapRouter — Cross-token settlement via DEX aggregator
/// @notice When sender pays in WETH but receiver wants USDC, this contract
///         handles the swap via Uniswap V3 (or any approved DEX router).
///         Charges a swap_fee on top of the normal AGIO fee.
contract AgioSwapRouter is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    ReentrancyGuard
{
    using SafeERC20 for IERC20;

    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant SETTLEMENT_ROLE = keccak256("SETTLEMENT_ROLE");

    IAgioVault public vault;
    address public dexRouter;
    uint256 public swapFeeBps; // 30 = 0.3%
    address public feeCollector;

    // Agent preferred receive token (default: address(0) means accept anything)
    mapping(address => address) public preferredToken;

    event SwapExecuted(
        address indexed fromToken,
        address indexed toToken,
        uint256 amountIn,
        uint256 amountOut,
        uint256 fee
    );
    event PreferredTokenSet(address indexed agent, address indexed token);
    event DexRouterUpdated(address oldRouter, address newRouter);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() { _disableInitializers(); }

    function initialize(
        address _vault,
        address _dexRouter,
        address _feeCollector,
        uint256 _swapFeeBps
    ) external initializer {
        __AccessControl_init();

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(UPGRADER_ROLE, msg.sender);
        _grantRole(SETTLEMENT_ROLE, msg.sender);

        vault = IAgioVault(_vault);
        dexRouter = _dexRouter;
        feeCollector = _feeCollector;
        swapFeeBps = _swapFeeBps;
    }

    /// @notice Set your preferred receive token (agents call this)
    function setPreferredToken(address token) external {
        preferredToken[msg.sender] = token;
        emit PreferredTokenSet(msg.sender, token);
    }

    /// @notice Get the effective receive token for an agent
    /// @param agent The agent's wallet address
    /// @param defaultToken Fallback if no preference is set
    function getReceiveToken(address agent, address defaultToken) public view returns (address) {
        address pref = preferredToken[agent];
        return pref == address(0) ? defaultToken : pref;
    }

    /// @notice Check if a swap is needed between sender token and receiver preference
    function needsSwap(address receiver, address senderToken) external view returns (bool) {
        address recvToken = preferredToken[receiver];
        if (recvToken == address(0)) return false;
        return recvToken != senderToken;
    }

    /// @notice Calculate the swap fee for a given amount
    function calculateSwapFee(uint256 amount) public view returns (uint256) {
        return (amount * swapFeeBps) / 10000;
    }

    /// @notice Execute a cross-token settlement
    /// @dev Called by the batch settlement system when tokens don't match.
    ///      1. Debit sender's token from vault
    ///      2. Swap via DEX
    ///      3. Credit receiver's preferred token in vault
    ///      4. Collect swap fee
    function settleWithSwap(
        address sender,
        address receiver,
        address fromToken,
        uint256 amount
    ) external onlyRole(SETTLEMENT_ROLE) nonReentrant returns (uint256 receivedAmount) {
        address toToken = getReceiveToken(receiver, fromToken);
        require(toToken != fromToken, "AgioSwap: no swap needed");

        uint256 fee = calculateSwapFee(amount);
        uint256 swapAmount = amount - fee;

        // Debit sender in fromToken
        vault.debit(sender, fromToken, amount);

        // In production: vault transfers fromToken to this contract,
        // we swap via dexRouter, then deposit toToken back to vault.
        // For now, emit the intent — actual DEX integration happens at mainnet.
        receivedAmount = swapAmount; // 1:1 placeholder for non-mainnet

        // Credit receiver in toToken
        vault.credit(receiver, toToken, receivedAmount);

        // Credit fee to collector
        if (fee > 0 && feeCollector != address(0)) {
            vault.credit(feeCollector, fromToken, fee);
        }

        emit SwapExecuted(fromToken, toToken, amount, receivedAmount, fee);
    }

    // --- Admin ---

    function setDexRouter(address _router) external onlyRole(DEFAULT_ADMIN_ROLE) {
        emit DexRouterUpdated(dexRouter, _router);
        dexRouter = _router;
    }

    function setSwapFeeBps(uint256 _bps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(_bps <= 100, "AgioSwap: fee too high"); // max 1%
        swapFeeBps = _bps;
    }

    function setFeeCollector(address _collector) external onlyRole(DEFAULT_ADMIN_ROLE) {
        feeCollector = _collector;
    }

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
