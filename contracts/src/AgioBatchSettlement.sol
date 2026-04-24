// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {PausableUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/PausableUpgradeable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {IAgioVault} from "./interfaces/IAgioVault.sol";
import {IAgioRegistry} from "./interfaces/IAgioRegistry.sol";

/// @title AgioBatchSettlement — Atomic batch payment processing for AGIO
/// @notice Security: batch hash verification with ECDSA signature, max batch value,
///         per-submitter rate limiting, replay protection, balance invariant enforcement.
contract AgioBatchSettlement is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    PausableUpgradeable,
    ReentrancyGuard
{
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    bytes32 public constant BATCH_SUBMITTER_ROLE = keccak256("BATCH_SUBMITTER_ROLE");
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");

    struct BatchPayment {
        address from;
        address to;
        uint256 amount;
        address token;
        bytes32 paymentId;
    }

    struct BatchRecord {
        bytes32 batchId;
        uint64 timestamp;
        uint32 totalPayments;
        uint256 totalVolume;
        address submitter;
        BatchStatus status;
    }

    enum BatchStatus { Pending, Settled, Failed, Reverted }

    IAgioVault public vault;
    IAgioRegistry public registry;
    uint256 public maxBatchSize;
    uint256 public maxBatchValue;

    mapping(bytes32 => BatchRecord) private _batches;
    mapping(bytes32 => bool) private _processedPayments;

    // Rate limiting
    uint256 public maxBatchesPerHour;
    mapping(address => uint256) private _submitterWindowStart;
    mapping(address => uint256) private _submitterBatchCount;

    // Batch hash verification: authorized API signer
    address public batchSigner;

    event BatchSettled(bytes32 indexed batchId, uint256 totalPayments, uint256 totalVolume, uint256 timestamp);
    event PaymentSettled(bytes32 indexed batchId, bytes32 indexed paymentId, address indexed from, address to, address token, uint256 amount);
    event BatchFailed(bytes32 indexed batchId, string reason);
    event BatchSignerUpdated(address oldSigner, address newSigner);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(
        address _vault,
        address _registry,
        uint256 _maxBatchSize
    ) external initializer {
        __AccessControl_init();
        __Pausable_init();

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(BATCH_SUBMITTER_ROLE, msg.sender);
        _grantRole(UPGRADER_ROLE, msg.sender);

        vault = IAgioVault(_vault);
        registry = IAgioRegistry(_registry);
        maxBatchSize = _maxBatchSize;
        maxBatchValue = 50_000e6;
        maxBatchesPerHour = 60;
        batchSigner = msg.sender; // default: deployer is the signer
    }

    /// @notice Compute the hash of a batch for signature verification
    /// @dev Deterministic: sorted by paymentId, tightly packed
    function computeBatchHash(
        BatchPayment[] calldata payments,
        bytes32 batchId
    ) public pure returns (bytes32) {
        // Hash each payment individually, then hash the concatenation
        // This ensures the entire batch content is committed
        bytes32 payloadHash = keccak256(abi.encode(batchId));
        uint256 len = payments.length;
        for (uint256 i; i < len;) {
            payloadHash = keccak256(abi.encodePacked(
                payloadHash,
                payments[i].from,
                payments[i].to,
                payments[i].amount,
                payments[i].token,
                payments[i].paymentId
            ));
            unchecked { ++i; }
        }
        return payloadHash;
    }

    /// @notice Submit a batch with signature verification
    /// @param payments Array of payments to process
    /// @param batchId Unique batch identifier
    /// @param signature ECDSA signature of the batch hash, signed by batchSigner
    function submitBatch(
        BatchPayment[] calldata payments,
        bytes32 batchId,
        bytes calldata signature
    ) external nonReentrant whenNotPaused onlyRole(BATCH_SUBMITTER_ROLE) {
        uint256 len = payments.length;
        require(len > 0, "AgioBatch: empty batch");
        require(len <= maxBatchSize, "AgioBatch: exceeds max batch size");
        require(_batches[batchId].timestamp == 0, "AgioBatch: duplicate batch ID");

        // BATCH HASH VERIFICATION: Prevents compromised submitter from modifying payments.
        // The API server signs the batch hash. The contract verifies the signature
        // matches the authorized batchSigner address.
        if (batchSigner != address(0)) {
            bytes32 batchHash = computeBatchHash(payments, batchId);
            bytes32 ethSignedHash = batchHash.toEthSignedMessageHash();
            address recovered = ethSignedHash.recover(signature);
            require(recovered == batchSigner, "AgioBatch: invalid batch signature");
        }

        // Rate limit
        _checkRateLimit(msg.sender);

        uint256 totalVolume;

        for (uint256 i; i < len;) {
            BatchPayment calldata p = payments[i];

            require(p.amount > 0, "AgioBatch: zero amount");
            require(p.from != p.to, "AgioBatch: self-payment");
            require(!_processedPayments[p.paymentId], "AgioBatch: duplicate payment ID");

            _processedPayments[p.paymentId] = true;
            vault.debit(p.from, p.token, p.amount);
            vault.credit(p.to, p.token, p.amount);

            totalVolume += p.amount;
            emit PaymentSettled(batchId, p.paymentId, p.from, p.to, p.token, p.amount);

            unchecked { ++i; }
        }

        require(totalVolume <= maxBatchValue, "AgioBatch: exceeds max batch value");

        _batches[batchId] = BatchRecord({
            batchId: batchId,
            timestamp: uint64(block.timestamp),
            totalPayments: uint32(len),
            totalVolume: totalVolume,
            submitter: msg.sender,
            status: BatchStatus.Settled
        });

        if (address(registry) != address(0)) {
            _updateRegistryStats(payments);
        }

        // BALANCE INVARIANT: verify the vault's books still balance for each token used
        // Debit/credit are internal transfers, so invariant should always hold,
        // but we check as a defense-in-depth measure.
        for (uint256 i; i < len;) {
            vault.enforceInvariant(payments[i].token);
            unchecked { ++i; }
        }

        emit BatchSettled(batchId, len, totalVolume, block.timestamp);
    }

    function _checkRateLimit(address submitter) private {
        if (block.timestamp > _submitterWindowStart[submitter] + 1 hours) {
            _submitterWindowStart[submitter] = block.timestamp;
            _submitterBatchCount[submitter] = 0;
        }
        _submitterBatchCount[submitter]++;
        require(
            _submitterBatchCount[submitter] <= maxBatchesPerHour,
            "AgioBatch: rate limit exceeded"
        );
    }

    function _updateRegistryStats(BatchPayment[] calldata payments) private {
        uint256 len = payments.length;
        for (uint256 i; i < len;) {
            try registry.incrementStats(payments[i].from, 1, payments[i].amount) {} catch {}
            try registry.incrementStats(payments[i].to, 1, payments[i].amount) {} catch {}
            unchecked { ++i; }
        }
    }

    // --- View functions ---

    function getBatchStatus(bytes32 batchId) external view returns (BatchStatus) {
        return _batches[batchId].status;
    }

    function getBatchDetails(bytes32 batchId) external view returns (BatchRecord memory) {
        return _batches[batchId];
    }

    function isPaymentProcessed(bytes32 paymentId) external view returns (bool) {
        return _processedPayments[paymentId];
    }

    // --- Admin ---

    function setBatchSigner(address newSigner) external onlyRole(DEFAULT_ADMIN_ROLE) {
        emit BatchSignerUpdated(batchSigner, newSigner);
        batchSigner = newSigner;
    }

    function setMaxBatchSize(uint256 newMax) external onlyRole(DEFAULT_ADMIN_ROLE) {
        maxBatchSize = newMax;
    }

    function setMaxBatchValue(uint256 newMax) external onlyRole(DEFAULT_ADMIN_ROLE) {
        maxBatchValue = newMax;
    }

    function setMaxBatchesPerHour(uint256 newMax) external onlyRole(DEFAULT_ADMIN_ROLE) {
        maxBatchesPerHour = newMax;
    }

    function pause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _unpause();
    }

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
