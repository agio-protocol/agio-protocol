// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IAgioRegistry {
    enum AgentTier { NEW, ACTIVE, VERIFIED, TRUSTED, OPERATOR }

    function isRegistered(address wallet) external view returns (bool);
    function incrementStats(address wallet, uint256 paymentCount, uint256 volume) external;
}
