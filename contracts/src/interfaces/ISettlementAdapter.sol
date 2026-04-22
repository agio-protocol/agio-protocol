// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ISettlementAdapter — Interface for protocols that use AGIO as settlement layer
/// @notice Any protocol (x402, Skyfire, Nevermined, MPP) can implement this
///         to route their payments through AGIO's batched settlement.
interface ISettlementAdapter {
    struct AdapterPayment {
        address from;
        address to;
        uint256 amount;
        address token;        // ERC-20 token address
        bytes32 externalId;   // ID from the calling protocol
        bytes metadata;       // protocol-specific data (memo, service type, etc.)
    }

    struct SettlementResult {
        bytes32 batchId;
        uint256 totalPayments;
        uint256 totalVolume;
        uint256 gasCost;
        bool success;
    }

    /// @notice Settle an array of payments through AGIO
    /// @param payments Array of payments in the adapter's format
    /// @return result Settlement result with batch ID and costs
    function settle(AdapterPayment[] calldata payments) external returns (SettlementResult memory result);

    /// @notice Estimate the cost of settling a batch without executing
    /// @param payments Array of payments to estimate
    /// @return estimatedGas Total gas estimate
    /// @return estimatedCostWei Cost in wei at current gas price
    /// @return costPerPayment Cost per payment in wei
    function estimateCost(AdapterPayment[] calldata payments)
        external view returns (uint256 estimatedGas, uint256 estimatedCostWei, uint256 costPerPayment);

    /// @notice Get the status of a previously submitted batch
    /// @param batchId The batch ID returned from settle()
    /// @return status 0=unknown, 1=settled, 2=failed, 3=reverted
    /// @return totalPayments Number of payments in the batch
    /// @return settledAt Timestamp of settlement (0 if not settled)
    function getStatus(bytes32 batchId) external view returns (uint8 status, uint32 totalPayments, uint64 settledAt);
}
