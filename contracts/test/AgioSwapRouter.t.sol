// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {AgioVault} from "../src/AgioVault.sol";
import {AgioSwapRouter} from "../src/AgioSwapRouter.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

contract AgioSwapRouterTest is Test {
    AgioVault public vault;
    AgioSwapRouter public swapRouter;
    MockUSDC public usdc;
    MockUSDC public weth;
    MockUSDC public dai;

    address public admin = address(this);
    address public alice = address(0xA11CE);
    address public bob = address(0xB0B);
    address public feeCollector = address(0xFEE);

    uint256 constant MAX_CAP = 100_000e6;

    function setUp() public {
        usdc = new MockUSDC();
        weth = new MockUSDC();
        dai = new MockUSDC();

        // Deploy vault
        AgioVault vaultImpl = new AgioVault();
        vault = AgioVault(address(new ERC1967Proxy(
            address(vaultImpl),
            abi.encodeCall(vaultImpl.initialize, (MAX_CAP))
        )));
        vault.addWhitelistedToken(address(usdc));
        vault.addWhitelistedToken(address(weth));
        vault.addWhitelistedToken(address(dai));

        // Deploy swap router
        AgioSwapRouter swapImpl = new AgioSwapRouter();
        swapRouter = AgioSwapRouter(address(new ERC1967Proxy(
            address(swapImpl),
            abi.encodeCall(swapImpl.initialize, (
                address(vault), address(0), feeCollector, 30 // 0.3%
            ))
        )));

        // Grant settlement role to swap router
        vault.grantRole(vault.SETTLEMENT_ROLE(), address(swapRouter));

        // Fund agents
        _fundAgent(alice, address(weth), 1000e6);
        _fundAgent(bob, address(usdc), 100e6);
    }

    function _fundAgent(address agent, address token, uint256 amount) internal {
        MockUSDC(token).mint(agent, amount);
        vm.startPrank(agent);
        MockUSDC(token).approve(address(vault), amount);
        vault.deposit(token, amount);
        vm.stopPrank();
    }

    function test_set_preferred_token() public {
        vm.prank(bob);
        swapRouter.setPreferredToken(address(usdc));

        assertEq(swapRouter.preferredToken(bob), address(usdc));
        assertEq(swapRouter.getReceiveToken(bob, address(weth)), address(usdc));
    }

    function test_no_preference_returns_default() public {
        assertEq(swapRouter.getReceiveToken(alice, address(weth)), address(weth));
    }

    function test_needs_swap() public {
        vm.prank(bob);
        swapRouter.setPreferredToken(address(usdc));

        assertTrue(swapRouter.needsSwap(bob, address(weth)));
        assertFalse(swapRouter.needsSwap(bob, address(usdc)));
        assertFalse(swapRouter.needsSwap(alice, address(weth))); // no pref set
    }

    function test_swap_fee_calculation() public view {
        assertEq(swapRouter.calculateSwapFee(10000), 30); // 0.3% of 10000
        assertEq(swapRouter.calculateSwapFee(1000000), 3000);
        assertEq(swapRouter.calculateSwapFee(0), 0);
    }

    function test_settle_with_swap() public {
        vm.prank(bob);
        swapRouter.setPreferredToken(address(usdc));

        // Alice has WETH, pays Bob who wants USDC
        uint256 amount = 100e6;
        uint256 expectedFee = swapRouter.calculateSwapFee(amount);
        uint256 expectedReceived = amount - expectedFee;

        swapRouter.settleWithSwap(alice, bob, address(weth), amount);

        // Alice's WETH debited
        assertEq(vault.balanceOf(alice, address(weth)), 900e6);

        // Bob gets USDC (minus swap fee)
        assertEq(vault.balanceOf(bob, address(usdc)), 100e6 + expectedReceived);

        // Fee collector gets fee in WETH
        assertEq(vault.balanceOf(feeCollector, address(weth)), expectedFee);
    }

    function test_settle_no_swap_needed_reverts() public {
        // Bob has no preference, so fromToken == toToken
        vm.expectRevert("AgioSwap: no swap needed");
        swapRouter.settleWithSwap(alice, bob, address(weth), 100e6);
    }

    function test_settle_unauthorized_reverts() public {
        vm.prank(bob);
        swapRouter.setPreferredToken(address(usdc));

        vm.prank(alice);
        vm.expectRevert();
        swapRouter.settleWithSwap(alice, bob, address(weth), 100e6);
    }

    function test_swap_fee_too_high_reverts() public {
        vm.expectRevert("AgioSwap: fee too high");
        swapRouter.setSwapFeeBps(200); // 2% > max 1%
    }

    function test_multiple_swaps() public {
        vm.prank(bob);
        swapRouter.setPreferredToken(address(usdc));

        _fundAgent(alice, address(weth), 5000e6);

        for (uint256 i; i < 10; i++) {
            swapRouter.settleWithSwap(alice, bob, address(weth), 10e6);
        }

        uint256 totalSwapped = 100e6;
        uint256 totalFees = swapRouter.calculateSwapFee(totalSwapped);

        assertEq(vault.balanceOf(alice, address(weth)), 6000e6 - totalSwapped);
        assertEq(vault.balanceOf(feeCollector, address(weth)), totalFees);
    }

    function test_three_token_scenario() public {
        // Alice has WETH, Bob wants DAI
        vm.prank(bob);
        swapRouter.setPreferredToken(address(dai));

        swapRouter.settleWithSwap(alice, bob, address(weth), 50e6);

        uint256 fee = swapRouter.calculateSwapFee(50e6);
        assertEq(vault.balanceOf(alice, address(weth)), 950e6);
        assertEq(vault.balanceOf(bob, address(dai)), 50e6 - fee);
        assertEq(vault.balanceOf(feeCollector, address(weth)), fee);
    }
}
