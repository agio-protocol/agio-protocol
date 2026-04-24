// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {PausableUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/PausableUpgradeable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EnumerableSet} from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";
import {IAgioVault} from "./interfaces/IAgioVault.sol";

/// @title AgioVault — Multi-token agent deposit/withdrawal vault
/// @notice Supports any whitelisted ERC-20 token (USDC, USDT, DAI, WETH).
///         Balances tracked per agent per token. Invariant checked per token.
contract AgioVault is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    PausableUpgradeable,
    ReentrancyGuard,
    IAgioVault
{
    using SafeERC20 for IERC20;
    using EnumerableSet for EnumerableSet.AddressSet;

    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant SETTLEMENT_ROLE = keccak256("SETTLEMENT_ROLE");

    uint256 public maxDepositCap;

    // Multi-token: agent → token → balance
    mapping(address => mapping(address => uint256)) private _balances;
    mapping(address => mapping(address => uint256)) private _lockedBalances;

    // Per-token tracked total (for invariant check)
    mapping(address => uint256) public totalTrackedBalance;

    // Token whitelist
    EnumerableSet.AddressSet private _whitelistedTokens;

    event InvariantViolation(address indexed token, uint256 tracked, uint256 actual);
    event TokenWhitelisted(address indexed token);
    event TokenRemoved(address indexed token);

    // Tiered withdrawal delays
    uint256 public instantWithdrawLimit;
    uint256 public mediumWithdrawLimit;
    uint256 public mediumWithdrawDelay;
    uint256 public largeWithdrawDelay;

    struct PendingWithdrawal {
        address token;
        uint256 amount;
        uint64 requestedAt;
        uint64 availableAt;
    }
    mapping(address => PendingWithdrawal) public pendingWithdrawals;

    event WithdrawalRequested(address indexed agent, address indexed token, uint256 amount, uint256 availableAt);
    event WithdrawalCancelled(address indexed agent, address indexed token, uint256 amount);

    // Circuit breaker (per-token)
    uint256 public circuitBreakerThresholdBps;
    uint256 public circuitBreakerWindow;
    mapping(address => uint256) private _windowStart;
    mapping(address => uint256) private _windowOutflows;

    event CircuitBreakerTriggered(address indexed token, uint256 outflows, uint256 threshold);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(uint256 _maxDepositCap) external initializer {
        __AccessControl_init();
        __Pausable_init();

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(PAUSER_ROLE, msg.sender);
        _grantRole(UPGRADER_ROLE, msg.sender);

        maxDepositCap = _maxDepositCap;
        instantWithdrawLimit = 1_000e6;
        mediumWithdrawLimit = 10_000e6;
        mediumWithdrawDelay = 1 hours;
        largeWithdrawDelay = 24 hours;
        circuitBreakerThresholdBps = 2000;
        circuitBreakerWindow = 1 hours;
    }

    // --- Token Whitelist ---

    function addWhitelistedToken(address token) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(token != address(0), "AgioVault: zero address");
        _whitelistedTokens.add(token);
        emit TokenWhitelisted(token);
    }

    function removeWhitelistedToken(address token) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(totalTrackedBalance[token] == 0, "AgioVault: token has balance");
        _whitelistedTokens.remove(token);
        emit TokenRemoved(token);
    }

    function isWhitelistedToken(address token) public view returns (bool) {
        return _whitelistedTokens.contains(token);
    }

    function getWhitelistedTokens() external view returns (address[] memory) {
        return _whitelistedTokens.values();
    }

    // --- Deposit / Withdraw ---

    function deposit(address token, uint256 amount) external nonReentrant whenNotPaused {
        require(amount > 0, "AgioVault: zero amount");
        require(isWhitelistedToken(token), "AgioVault: token not whitelisted");
        require(
            _balances[msg.sender][token] + amount <= maxDepositCap,
            "AgioVault: exceeds deposit cap"
        );

        // CEI: state before transfer
        _balances[msg.sender][token] += amount;
        totalTrackedBalance[token] += amount;

        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);

        emit Deposited(msg.sender, token, amount, block.timestamp);
    }

    function withdraw(address token, uint256 amount) external nonReentrant whenNotPaused {
        require(amount > 0, "AgioVault: zero amount");
        require(_balances[msg.sender][token] >= amount, "AgioVault: insufficient balance");

        if (amount <= instantWithdrawLimit) {
            _executeWithdraw(msg.sender, token, amount);
        } else {
            uint256 delay = amount <= mediumWithdrawLimit ? mediumWithdrawDelay : largeWithdrawDelay;
            _balances[msg.sender][token] -= amount;
            _lockedBalances[msg.sender][token] += amount;

            pendingWithdrawals[msg.sender] = PendingWithdrawal({
                token: token,
                amount: amount,
                requestedAt: uint64(block.timestamp),
                availableAt: uint64(block.timestamp + delay)
            });

            emit WithdrawalRequested(msg.sender, token, amount, block.timestamp + delay);
        }
    }

    function executeDelayedWithdrawal() external nonReentrant whenNotPaused {
        PendingWithdrawal memory pw = pendingWithdrawals[msg.sender];
        require(pw.amount > 0, "AgioVault: no pending withdrawal");
        require(block.timestamp >= pw.availableAt, "AgioVault: withdrawal not yet available");

        address token = pw.token;
        uint256 amount = pw.amount;
        delete pendingWithdrawals[msg.sender];
        _lockedBalances[msg.sender][token] -= amount;
        totalTrackedBalance[token] -= amount;

        _checkCircuitBreaker(token, amount);

        IERC20(token).safeTransfer(msg.sender, amount);
        emit Withdrawn(msg.sender, token, amount, block.timestamp);
    }

    function cancelDelayedWithdrawal() external {
        PendingWithdrawal memory pw = pendingWithdrawals[msg.sender];
        require(pw.amount > 0, "AgioVault: no pending withdrawal");

        delete pendingWithdrawals[msg.sender];
        _lockedBalances[msg.sender][pw.token] -= pw.amount;
        _balances[msg.sender][pw.token] += pw.amount;

        emit WithdrawalCancelled(msg.sender, pw.token, pw.amount);
    }

    function _executeWithdraw(address agent, address token, uint256 amount) private {
        _checkCircuitBreaker(token, amount);
        _balances[agent][token] -= amount;
        totalTrackedBalance[token] -= amount;
        IERC20(token).safeTransfer(agent, amount);
        emit Withdrawn(agent, token, amount, block.timestamp);
    }

    function _checkCircuitBreaker(address token, uint256 outflow) private {
        if (block.timestamp > _windowStart[token] + circuitBreakerWindow) {
            _windowStart[token] = block.timestamp;
            _windowOutflows[token] = 0;
        }
        _windowOutflows[token] += outflow;
        uint256 totalBalance = IERC20(token).balanceOf(address(this));
        uint256 threshold = (totalBalance + _windowOutflows[token]) * circuitBreakerThresholdBps / 10000;
        if (_windowOutflows[token] > threshold) {
            _pause();
            emit CircuitBreakerTriggered(token, _windowOutflows[token], threshold);
        }
    }

    // --- Balance Queries ---

    function balanceOf(address agent, address token) external view returns (uint256) {
        return _balances[agent][token];
    }

    function lockedBalanceOf(address agent, address token) external view returns (uint256) {
        return _lockedBalances[agent][token];
    }

    // --- Settlement Interface ---

    function debit(address agent, address token, uint256 amount) external onlyRole(SETTLEMENT_ROLE) {
        require(_balances[agent][token] >= amount, "AgioVault: insufficient balance for debit");
        _balances[agent][token] -= amount;
    }

    function credit(address agent, address token, uint256 amount) external onlyRole(SETTLEMENT_ROLE) {
        _balances[agent][token] += amount;
    }

    // --- Per-Token Invariant ---

    function checkInvariant(address token) public view returns (bool ok, uint256 tracked, uint256 actual) {
        tracked = totalTrackedBalance[token];
        actual = IERC20(token).balanceOf(address(this));
        ok = (tracked == actual);
    }

    function enforceInvariant(address token) external {
        (bool ok, uint256 tracked, uint256 actual) = checkInvariant(token);
        if (!ok) {
            _pause();
            emit InvariantViolation(token, tracked, actual);
        }
    }

    // --- Admin ---

    function pause() external onlyRole(PAUSER_ROLE) { _pause(); }
    function unpause() external onlyRole(PAUSER_ROLE) { _unpause(); }

    function setMaxDepositCap(uint256 newCap) external onlyRole(DEFAULT_ADMIN_ROLE) {
        maxDepositCap = newCap;
    }

    function setWithdrawLimits(uint256 _instant, uint256 _medium, uint256 _mDelay, uint256 _lDelay)
        external onlyRole(DEFAULT_ADMIN_ROLE)
    {
        instantWithdrawLimit = _instant;
        mediumWithdrawLimit = _medium;
        mediumWithdrawDelay = _mDelay;
        largeWithdrawDelay = _lDelay;
    }

    function setCircuitBreaker(uint256 _bps, uint256 _window) external onlyRole(DEFAULT_ADMIN_ROLE) {
        circuitBreakerThresholdBps = _bps;
        circuitBreakerWindow = _window;
    }

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
