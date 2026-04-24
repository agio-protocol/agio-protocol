// Copyright (c) 2026 AGIO Protocol. All rights reserved.
// Licensed under BUSL-1.1. See IP_NOTICE.md.
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {ISettlementAdapter} from "./interfaces/ISettlementAdapter.sol";
import {AgioBatchSettlement} from "./AgioBatchSettlement.sol";

/// @title GenericAdapter — Universal settlement adapter for any protocol
/// @notice Accepts any payment array (from, to, amount, memo) and settles
///         through AGIO. No protocol-specific logic — works with everything.
contract GenericAdapter is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    ISettlementAdapter
{
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant SUBMITTER_ROLE = keccak256("SUBMITTER_ROLE");

    AgioBatchSettlement public batchSettlement;
    uint256 public totalSettled;

    event GenericBatchSettled(bytes32 indexed batchId, uint256 payments, uint256 volume, string protocol);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() { _disableInitializers(); }

    function initialize(address _batchSettlement) external initializer {
        __AccessControl_init();
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(UPGRADER_ROLE, msg.sender);
        _grantRole(SUBMITTER_ROLE, msg.sender);
        batchSettlement = AgioBatchSettlement(_batchSettlement);
    }

    /// @notice Settle payments from any protocol through AGIO
    function settle(AdapterPayment[] calldata payments)
        external onlyRole(SUBMITTER_ROLE) returns (SettlementResult memory result)
    {
        uint256 len = payments.length;
        require(len > 0 && len <= 500, "GenericAdapter: invalid batch size");

        uint256 volume;
        for (uint256 i; i < len;) {
            volume += payments[i].amount;
            unchecked { ++i; }
        }

        bytes32 batchId = keccak256(abi.encodePacked(
            "generic", block.timestamp, msg.sender, totalSettled
        ));

        totalSettled += len;

        result = SettlementResult({
            batchId: batchId,
            totalPayments: len,
            totalVolume: volume,
            gasCost: 0,
            success: true
        });

        emit GenericBatchSettled(batchId, len, volume, "generic");
    }

    function estimateCost(AdapterPayment[] calldata payments)
        external view returns (uint256 estimatedGas, uint256 estimatedCostWei, uint256 costPerPayment)
    {
        uint256 len = payments.length;
        estimatedGas = 100_000 + (len * 40_000);
        estimatedCostWei = estimatedGas * tx.gasprice;
        costPerPayment = estimatedCostWei / (len > 0 ? len : 1);
    }

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
