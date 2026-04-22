// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {AgioVault} from "../src/AgioVault.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

contract AgioVaultTest is Test {
    AgioVault public vault;
    MockUSDC public usdc;
    MockUSDC public dai;
    address public admin = address(this);
    address public alice = address(0xA11CE);
    address public bob = address(0xB0B);

    uint256 constant MAX_CAP = 10_000e6;

    function setUp() public {
        usdc = new MockUSDC();
        dai = new MockUSDC();

        AgioVault impl = new AgioVault();
        bytes memory initData = abi.encodeCall(impl.initialize, (MAX_CAP));
        ERC1967Proxy proxy = new ERC1967Proxy(address(impl), initData);
        vault = AgioVault(address(proxy));

        vault.addWhitelistedToken(address(usdc));
        vault.addWhitelistedToken(address(dai));

        usdc.mint(alice, 1_000e6);
        usdc.mint(bob, 1_000e6);
        dai.mint(alice, 1_000e6);

        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(bob);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(alice);
        dai.approve(address(vault), type(uint256).max);
    }

    function test_deposit() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        assertEq(vault.balanceOf(alice, address(usdc)), 100e6);
        assertEq(usdc.balanceOf(alice), 900e6);
        assertEq(usdc.balanceOf(address(vault)), 100e6);
    }

    function test_deposit_multiple_tokens() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);
        vm.prank(alice);
        vault.deposit(address(dai), 200e6);

        assertEq(vault.balanceOf(alice, address(usdc)), 100e6);
        assertEq(vault.balanceOf(alice, address(dai)), 200e6);
    }

    function test_withdraw_instant() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        vm.prank(alice);
        vault.withdraw(address(usdc), 50e6);

        assertEq(vault.balanceOf(alice, address(usdc)), 50e6);
        assertEq(usdc.balanceOf(alice), 950e6);
    }

    function test_withdraw_insufficient_balance() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        vm.prank(alice);
        vm.expectRevert("AgioVault: insufficient balance");
        vault.withdraw(address(usdc), 200e6);
    }

    function test_deposit_exceeds_cap() public {
        usdc.mint(alice, MAX_CAP);

        vm.prank(alice);
        vault.deposit(address(usdc), MAX_CAP);

        vm.prank(alice);
        vm.expectRevert("AgioVault: exceeds deposit cap");
        vault.deposit(address(usdc), 1);
    }

    function test_deposit_zero_reverts() public {
        vm.prank(alice);
        vm.expectRevert("AgioVault: zero amount");
        vault.deposit(address(usdc), 0);
    }

    function test_deposit_non_whitelisted_reverts() public {
        address fakeToken = address(0xF00D);
        vm.prank(alice);
        vm.expectRevert("AgioVault: token not whitelisted");
        vault.deposit(fakeToken, 100e6);
    }

    function test_pause_blocks_deposits() public {
        vault.pause();

        vm.prank(alice);
        vm.expectRevert();
        vault.deposit(address(usdc), 100e6);
    }

    function test_pause_blocks_withdrawals() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        vault.pause();

        vm.prank(alice);
        vm.expectRevert();
        vault.withdraw(address(usdc), 50e6);
    }

    function test_debit_credit_by_settlement_role() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        vault.grantRole(vault.SETTLEMENT_ROLE(), address(this));

        vault.debit(alice, address(usdc), 30e6);
        assertEq(vault.balanceOf(alice, address(usdc)), 70e6);

        vault.credit(bob, address(usdc), 30e6);
        assertEq(vault.balanceOf(bob, address(usdc)), 30e6);
    }

    function test_debit_unauthorized_reverts() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        vm.prank(bob);
        vm.expectRevert();
        vault.debit(alice, address(usdc), 30e6);
    }

    function test_whitelist_management() public {
        assertTrue(vault.isWhitelistedToken(address(usdc)));
        assertTrue(vault.isWhitelistedToken(address(dai)));

        address[] memory tokens = vault.getWhitelistedTokens();
        assertEq(tokens.length, 2);
    }

    function test_remove_whitelisted_token() public {
        vault.removeWhitelistedToken(address(dai));
        assertFalse(vault.isWhitelistedToken(address(dai)));
    }

    function test_remove_token_with_balance_reverts() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        vm.expectRevert("AgioVault: token has balance");
        vault.removeWhitelistedToken(address(usdc));
    }

    function test_per_token_invariant() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 100e6);

        (bool ok, uint256 tracked, uint256 actual) = vault.checkInvariant(address(usdc));
        assertTrue(ok);
        assertEq(tracked, 100e6);
        assertEq(actual, 100e6);

        (bool ok2,,) = vault.checkInvariant(address(dai));
        assertTrue(ok2);
    }

    function test_delayed_withdrawal() public {
        vm.prank(alice);
        vault.deposit(address(usdc), 1_000e6);

        // 5000e6 > instantWithdrawLimit (1000e6), triggers delay
        usdc.mint(alice, 9_000e6);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(alice);
        vault.deposit(address(usdc), 9_000e6);

        vm.prank(alice);
        vault.withdraw(address(usdc), 5_000e6);

        // Should be locked, not withdrawn yet
        assertEq(vault.balanceOf(alice, address(usdc)), 5_000e6);
        assertEq(vault.lockedBalanceOf(alice, address(usdc)), 5_000e6);

        // Fast forward past medium delay
        vm.warp(block.timestamp + 2 hours);

        vm.prank(alice);
        vault.executeDelayedWithdrawal();

        assertEq(vault.lockedBalanceOf(alice, address(usdc)), 0);
    }

    function test_cancel_delayed_withdrawal() public {
        usdc.mint(alice, 9_000e6);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(alice);
        vault.deposit(address(usdc), 5_000e6);

        vm.prank(alice);
        vault.withdraw(address(usdc), 5_000e6);

        vm.prank(alice);
        vault.cancelDelayedWithdrawal();

        assertEq(vault.balanceOf(alice, address(usdc)), 5_000e6);
        assertEq(vault.lockedBalanceOf(alice, address(usdc)), 0);
    }

    event Deposited(address indexed agent, address indexed token, uint256 amount, uint256 timestamp);

    function test_events() public {
        vm.prank(alice);
        vm.expectEmit(true, true, false, true);
        emit Deposited(alice, address(usdc), 100e6, block.timestamp);
        vault.deposit(address(usdc), 100e6);
    }
}
