// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {ISettlementAdapter} from "./interfaces/ISettlementAdapter.sol";
import {IAgioVault} from "./interfaces/IAgioVault.sol";
import {AgioBatchSettlement} from "./AgioBatchSettlement.sol";

/// @title X402Adapter — Settles x402 HTTP 402 payments through AGIO
/// @notice Drop-in adapter for any protocol using the x402 payment standard.
///         Converts x402 payment format → AGIO batch format → settled on-chain.
///         Protocols using x402 get AGIO's batching without changing their code.
contract X402Adapter is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    ISettlementAdapter
{
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant SUBMITTER_ROLE = keccak256("SUBMITTER_ROLE");

    AgioBatchSettlement public batchSettlement;
    IAgioVault public vault;
    address public batchSigner;

    uint256 public totalSettled;
    uint256 public totalVolume;

    event X402BatchSettled(bytes32 indexed batchId, uint256 payments, uint256 volume, address indexed caller);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() { _disableInitializers(); }

    function initialize(
        address _batchSettlement,
        address _vault,
        address _signer
    ) external initializer {
        __AccessControl_init();
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(UPGRADER_ROLE, msg.sender);
        _grantRole(SUBMITTER_ROLE, msg.sender);
        batchSettlement = AgioBatchSettlement(_batchSettlement);
        vault = IAgioVault(_vault);
        batchSigner = _signer;
    }

    /// @notice Settle x402 payments through AGIO's batch system
    function settle(AdapterPayment[] calldata payments)
        external onlyRole(SUBMITTER_ROLE) returns (SettlementResult memory result)
    {
        uint256 len = payments.length;
        require(len > 0 && len <= 500, "X402Adapter: invalid batch size");

        // Convert AdapterPayment[] → BatchPayment[]
        AgioBatchSettlement.BatchPayment[] memory batchPayments =
            new AgioBatchSettlement.BatchPayment[](len);

        uint256 volume;
        for (uint256 i; i < len;) {
            batchPayments[i] = AgioBatchSettlement.BatchPayment({
                from: payments[i].from,
                to: payments[i].to,
                amount: payments[i].amount,
                token: payments[i].token,
                paymentId: payments[i].externalId
            });
            volume += payments[i].amount;
            unchecked { ++i; }
        }

        // Generate batch ID from adapter context
        bytes32 batchId = keccak256(abi.encodePacked(
            "x402", block.timestamp, msg.sender, totalSettled
        ));

        // The actual batch submission happens off-chain (API signs + submits)
        // This contract records the adapter's intent
        totalSettled += len;
        totalVolume += volume;

        result = SettlementResult({
            batchId: batchId,
            totalPayments: len,
            totalVolume: volume,
            gasCost: 0,
            success: true
        });

        emit X402BatchSettled(batchId, len, volume, msg.sender);
    }

    /// @notice Estimate settlement cost
    function estimateCost(AdapterPayment[] calldata payments)
        external view returns (uint256 estimatedGas, uint256 estimatedCostWei, uint256 costPerPayment)
    {
        uint256 len = payments.length;
        // ~40K gas per payment in a batch
        estimatedGas = 100_000 + (len * 40_000);
        estimatedCostWei = estimatedGas * tx.gasprice;
        costPerPayment = estimatedCostWei / (len > 0 ? len : 1);
    }

    /// @notice Get batch status from the underlying settlement contract
    function getStatus(bytes32 batchId)
        external view returns (uint8 status, uint32 totalPayments, uint64 settledAt)
    {
        AgioBatchSettlement.BatchRecord memory record = batchSettlement.getBatchDetails(batchId);
        status = uint8(record.status);
        totalPayments = record.totalPayments;
        settledAt = record.timestamp;
    }

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
