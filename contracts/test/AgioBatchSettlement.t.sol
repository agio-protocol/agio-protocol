// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {AgioVault} from "../src/AgioVault.sol";
import {AgioBatchSettlement} from "../src/AgioBatchSettlement.sol";
import {AgioRegistry} from "../src/AgioRegistry.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

contract AgioBatchSettlementTest is Test {
    using MessageHashUtils for bytes32;

    AgioVault public vault;
    AgioBatchSettlement public batch;
    AgioRegistry public registry;
    MockUSDC public usdc;
    MockUSDC public dai;

    uint256 public signerPrivateKey = 0xA11CE;
    address public signer;

    uint256 constant MAX_CAP = 100_000e6;

    function setUp() public {
        signer = vm.addr(signerPrivateKey);

        usdc = new MockUSDC();
        dai = new MockUSDC();

        AgioVault vaultImpl = new AgioVault();
        vault = AgioVault(address(new ERC1967Proxy(
            address(vaultImpl),
            abi.encodeCall(vaultImpl.initialize, (MAX_CAP))
        )));

        vault.addWhitelistedToken(address(usdc));
        vault.addWhitelistedToken(address(dai));

        AgioRegistry regImpl = new AgioRegistry();
        registry = AgioRegistry(address(new ERC1967Proxy(
            address(regImpl),
            abi.encodeCall(regImpl.initialize, ())
        )));

        AgioBatchSettlement batchImpl = new AgioBatchSettlement();
        batch = AgioBatchSettlement(address(new ERC1967Proxy(
            address(batchImpl),
            abi.encodeCall(batchImpl.initialize, (address(vault), address(registry), 500))
        )));

        vault.grantRole(vault.SETTLEMENT_ROLE(), address(batch));
        registry.grantRole(registry.BATCH_SETTLEMENT_ROLE(), address(batch));

        batch.setBatchSigner(signer);
    }

    function _fundAgent(address agent, address token, uint256 amount) internal {
        MockUSDC(token).mint(agent, amount);
        vm.startPrank(agent);
        MockUSDC(token).approve(address(vault), amount);
        vault.deposit(token, amount);
        vm.stopPrank();
    }

    function _makePayment(address from, address to, uint256 amount, address token, bytes32 pid)
        internal pure returns (AgioBatchSettlement.BatchPayment memory)
    {
        return AgioBatchSettlement.BatchPayment(from, to, amount, token, pid);
    }

    function _signBatch(
        AgioBatchSettlement.BatchPayment[] memory payments,
        bytes32 batchId
    ) internal view returns (bytes memory) {
        bytes32 payloadHash = keccak256(abi.encode(batchId));
        for (uint256 i; i < payments.length; i++) {
            payloadHash = keccak256(abi.encodePacked(
                payloadHash,
                payments[i].from,
                payments[i].to,
                payments[i].amount,
                payments[i].token,
                payments[i].paymentId
            ));
        }
        bytes32 ethSignedHash = payloadHash.toEthSignedMessageHash();
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerPrivateKey, ethSignedHash);
        return abi.encodePacked(r, s, v);
    }

    function test_single_payment_batch() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 100e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](1);
        payments[0] = _makePayment(alice, bob, 10e6, address(usdc), keccak256("pay1"));

        bytes32 batchId = keccak256("batch1");
        bytes memory sig = _signBatch(payments, batchId);

        batch.submitBatch(payments, batchId, sig);

        assertEq(vault.balanceOf(alice, address(usdc)), 90e6);
        assertEq(vault.balanceOf(bob, address(usdc)), 10e6);

        (bool ok,,) = vault.checkInvariant(address(usdc));
        assertTrue(ok, "Invariant violated after batch");
    }

    function test_multi_token_batch() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 100e6);
        _fundAgent(alice, address(dai), 100e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](2);
        payments[0] = _makePayment(alice, bob, 10e6, address(usdc), keccak256("pay1"));
        payments[1] = _makePayment(alice, bob, 20e6, address(dai), keccak256("pay2"));

        bytes32 batchId = keccak256("batch_multi");
        bytes memory sig = _signBatch(payments, batchId);

        batch.submitBatch(payments, batchId, sig);

        assertEq(vault.balanceOf(alice, address(usdc)), 90e6);
        assertEq(vault.balanceOf(bob, address(usdc)), 10e6);
        assertEq(vault.balanceOf(alice, address(dai)), 80e6);
        assertEq(vault.balanceOf(bob, address(dai)), 20e6);

        (bool ok1,,) = vault.checkInvariant(address(usdc));
        (bool ok2,,) = vault.checkInvariant(address(dai));
        assertTrue(ok1);
        assertTrue(ok2);
    }

    function test_invalid_signature_reverts() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 100e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](1);
        payments[0] = _makePayment(alice, bob, 10e6, address(usdc), keccak256("pay1"));

        bytes32 batchId = keccak256("batch1");

        uint256 wrongKey = 0xDEAD;
        bytes32 payloadHash = keccak256(abi.encode(batchId));
        payloadHash = keccak256(abi.encodePacked(payloadHash, alice, bob, uint256(10e6), address(usdc), keccak256("pay1")));
        bytes32 ethSignedHash = payloadHash.toEthSignedMessageHash();
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(wrongKey, ethSignedHash);
        bytes memory badSig = abi.encodePacked(r, s, v);

        vm.expectRevert("AgioBatch: invalid batch signature");
        batch.submitBatch(payments, batchId, badSig);
    }

    function test_tampered_payment_reverts() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        address mallory = address(0xBAD);
        _fundAgent(alice, address(usdc), 100e6);

        AgioBatchSettlement.BatchPayment[] memory originalPayments = new AgioBatchSettlement.BatchPayment[](1);
        originalPayments[0] = _makePayment(alice, bob, 10e6, address(usdc), keccak256("pay1"));
        bytes32 batchId = keccak256("batch1");
        bytes memory sig = _signBatch(originalPayments, batchId);

        AgioBatchSettlement.BatchPayment[] memory tamperedPayments = new AgioBatchSettlement.BatchPayment[](1);
        tamperedPayments[0] = _makePayment(alice, mallory, 10e6, address(usdc), keccak256("pay1"));

        vm.expectRevert("AgioBatch: invalid batch signature");
        batch.submitBatch(tamperedPayments, batchId, sig);
    }

    function test_tampered_token_reverts() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 100e6);
        _fundAgent(alice, address(dai), 100e6);

        AgioBatchSettlement.BatchPayment[] memory originalPayments = new AgioBatchSettlement.BatchPayment[](1);
        originalPayments[0] = _makePayment(alice, bob, 10e6, address(usdc), keccak256("pay1"));
        bytes32 batchId = keccak256("batch1");
        bytes memory sig = _signBatch(originalPayments, batchId);

        AgioBatchSettlement.BatchPayment[] memory tamperedPayments = new AgioBatchSettlement.BatchPayment[](1);
        tamperedPayments[0] = _makePayment(alice, bob, 10e6, address(dai), keccak256("pay1"));

        vm.expectRevert("AgioBatch: invalid batch signature");
        batch.submitBatch(tamperedPayments, batchId, sig);
    }

    function test_100_payment_batch_with_sig() public {
        uint256 numPayments = 100;
        address[] memory agents = new address[](numPayments + 1);

        for (uint256 i; i < numPayments + 1; i++) {
            agents[i] = address(uint160(0x1000 + i));
            _fundAgent(agents[i], address(usdc), 1000e6);
        }

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](numPayments);
        for (uint256 i; i < numPayments; i++) {
            payments[i] = _makePayment(
                agents[i], agents[i + 1], 1e6, address(usdc),
                keccak256(abi.encodePacked("pay", i))
            );
        }

        bytes32 batchId = keccak256("batch100");
        bytes memory sig = _signBatch(payments, batchId);

        batch.submitBatch(payments, batchId, sig);

        AgioBatchSettlement.BatchRecord memory record = batch.getBatchDetails(batchId);
        assertEq(record.totalPayments, 100);

        (bool ok,,) = vault.checkInvariant(address(usdc));
        assertTrue(ok);
    }

    function test_batch_with_insufficient_balance_reverts() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 10e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](1);
        payments[0] = _makePayment(alice, bob, 100e6, address(usdc), keccak256("pay1"));

        bytes32 batchId = keccak256("batch_fail");
        bytes memory sig = _signBatch(payments, batchId);

        vm.expectRevert("AgioVault: insufficient balance for debit");
        batch.submitBatch(payments, batchId, sig);
    }

    function test_duplicate_paymentId_reverts() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 100e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](1);
        payments[0] = _makePayment(alice, bob, 10e6, address(usdc), keccak256("pay1"));

        bytes32 batchId1 = keccak256("batch1");
        bytes memory sig1 = _signBatch(payments, batchId1);
        batch.submitBatch(payments, batchId1, sig1);

        bytes32 batchId2 = keccak256("batch2");
        bytes memory sig2 = _signBatch(payments, batchId2);
        vm.expectRevert("AgioBatch: duplicate payment ID");
        batch.submitBatch(payments, batchId2, sig2);
    }

    function test_unauthorized_submitter_reverts() public {
        address mallory = address(0xBAD);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](1);
        payments[0] = _makePayment(address(1), address(2), 1e6, address(usdc), keccak256("pay1"));

        bytes32 batchId = keccak256("batch1");
        bytes memory sig = _signBatch(payments, batchId);

        vm.prank(mallory);
        vm.expectRevert();
        batch.submitBatch(payments, batchId, sig);
    }

    function test_empty_batch_reverts() public {
        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](0);
        vm.expectRevert("AgioBatch: empty batch");
        batch.submitBatch(payments, keccak256("batch_empty"), "");
    }

    function test_self_payment_reverts() public {
        address alice = address(0xA11CE0);
        _fundAgent(alice, address(usdc), 100e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](1);
        payments[0] = _makePayment(alice, alice, 10e6, address(usdc), keccak256("pay1"));

        bytes32 batchId = keccak256("batch1");
        bytes memory sig = _signBatch(payments, batchId);

        vm.expectRevert("AgioBatch: self-payment");
        batch.submitBatch(payments, batchId, sig);
    }

    function test_invariant_holds_after_batch() public {
        address alice = address(0xA11CE0);
        address bob = address(0xB0B);
        _fundAgent(alice, address(usdc), 500e6);
        _fundAgent(bob, address(usdc), 500e6);

        (bool okBefore, uint256 trackedBefore, uint256 actualBefore) = vault.checkInvariant(address(usdc));
        assertTrue(okBefore);
        assertEq(trackedBefore, 1000e6);
        assertEq(actualBefore, 1000e6);

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](2);
        payments[0] = _makePayment(alice, bob, 100e6, address(usdc), keccak256("p1"));
        payments[1] = _makePayment(bob, alice, 50e6, address(usdc), keccak256("p2"));

        bytes32 batchId = keccak256("inv_batch");
        bytes memory sig = _signBatch(payments, batchId);
        batch.submitBatch(payments, batchId, sig);

        (bool okAfter, uint256 trackedAfter, uint256 actualAfter) = vault.checkInvariant(address(usdc));
        assertTrue(okAfter);
        assertEq(trackedAfter, 1000e6);
        assertEq(actualAfter, 1000e6);

        assertEq(vault.balanceOf(alice, address(usdc)), 450e6);
        assertEq(vault.balanceOf(bob, address(usdc)), 550e6);
    }

    function test_gas_usage_100_payments() public {
        uint256 numPayments = 100;
        address[] memory agents = new address[](numPayments + 1);

        for (uint256 i; i < numPayments + 1; i++) {
            agents[i] = address(uint160(0x1000 + i));
            _fundAgent(agents[i], address(usdc), 1000e6);
        }

        AgioBatchSettlement.BatchPayment[] memory payments = new AgioBatchSettlement.BatchPayment[](numPayments);
        for (uint256 i; i < numPayments; i++) {
            payments[i] = _makePayment(
                agents[i], agents[i + 1], 1e6, address(usdc),
                keccak256(abi.encodePacked("gas", i))
            );
        }

        bytes32 batchId = keccak256("gas_batch_100");
        bytes memory sig = _signBatch(payments, batchId);

        uint256 gasBefore = gasleft();
        batch.submitBatch(payments, batchId, sig);
        uint256 gasUsed = gasBefore - gasleft();

        console.log("Gas used for 100 payments (with sig + invariant):", gasUsed);
        assertLt(gasUsed, 5_000_000, "Gas too high for 100-payment batch");
    }
}
