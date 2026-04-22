// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IAgioVault — Multi-token vault interface
interface IAgioVault {
    function balanceOf(address agent, address token) external view returns (uint256);
    function lockedBalanceOf(address agent, address token) external view returns (uint256);
    function debit(address agent, address token, uint256 amount) external;
    function credit(address agent, address token, uint256 amount) external;
    function enforceInvariant(address token) external;
    function isWhitelistedToken(address token) external view returns (bool);

    event Deposited(address indexed agent, address indexed token, uint256 amount, uint256 timestamp);
    event Withdrawn(address indexed agent, address indexed token, uint256 amount, uint256 timestamp);
}
