// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {AgioVault} from "../src/AgioVault.sol";
import {AgioBatchSettlement} from "../src/AgioBatchSettlement.sol";
import {AgioRegistry} from "../src/AgioRegistry.sol";
import {AgioSwapRouter} from "../src/AgioSwapRouter.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

contract DeployAll is Script {
    // Base mainnet token addresses
    address constant USDC  = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant USDT  = 0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2;
    address constant DAI   = 0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb;
    address constant WETH  = 0x4200000000000000000000000000000000000006;
    address constant cbETH = 0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22;

    // Uniswap V3 SwapRouter02 on Base
    address constant UNISWAP_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;

    // Base mainnet chain ID
    uint256 constant BASE_MAINNET = 8453;

    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(deployerKey);
        bool isMainnet = block.chainid == BASE_MAINNET;

        console.log("========================================");
        console.log(isMainnet ? "  MAINNET DEPLOYMENT" : "  TESTNET DEPLOYMENT");
        console.log("========================================");
        console.log("Chain ID:", block.chainid);
        console.log("Deployer:", deployer);
        console.log("Balance:", deployer.balance);

        if (isMainnet) {
            require(deployer.balance >= 0.002 ether, "Need >= 0.002 ETH for deployment gas");
        }

        vm.startBroadcast(deployerKey);

        // On testnet: deploy MockUSDC. On mainnet: use real USDC.
        address usdcAddr;
        if (isMainnet) {
            usdcAddr = USDC;
            console.log("Using real USDC:", usdcAddr);
        } else {
            MockUSDC mockUsdc = new MockUSDC();
            usdcAddr = address(mockUsdc);
            console.log("MockUSDC deployed:", usdcAddr);
        }

        address vaultAddr = _deployVault(usdcAddr, isMainnet);
        address registryAddr = _deployRegistry();
        address batchAddr = _deployBatch(vaultAddr, registryAddr, deployer);
        address swapAddr = _deploySwapRouter(vaultAddr, deployer, isMainnet);

        AgioVault(vaultAddr).grantRole(
            AgioVault(vaultAddr).SETTLEMENT_ROLE(), swapAddr
        );

        vm.stopBroadcast();

        // Print deployment summary in a format the post-deploy script can parse
        console.log("");
        console.log("========================================");
        console.log("  DEPLOYMENT COMPLETE");
        console.log("========================================");
        console.log("DEPLOYED_VAULT=", vaultAddr);
        console.log("DEPLOYED_BATCH=", batchAddr);
        console.log("DEPLOYED_REGISTRY=", registryAddr);
        console.log("DEPLOYED_SWAP_ROUTER=", swapAddr);
        if (!isMainnet) {
            console.log("DEPLOYED_MOCK_USDC=", usdcAddr);
        }
        console.log("========================================");
    }

    function _deployVault(address usdc, bool isMainnet) internal returns (address) {
        AgioVault impl = new AgioVault();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl), abi.encodeCall(impl.initialize, (100_000e6))
        );
        AgioVault vault = AgioVault(address(proxy));

        // Whitelist tokens
        vault.addWhitelistedToken(usdc);
        if (isMainnet) {
            vault.addWhitelistedToken(USDT);
            vault.addWhitelistedToken(DAI);
            vault.addWhitelistedToken(WETH);
            vault.addWhitelistedToken(cbETH);
        }

        console.log("AgioVault:", address(vault));
        return address(vault);
    }

    function _deployRegistry() internal returns (address) {
        AgioRegistry impl = new AgioRegistry();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl), abi.encodeCall(impl.initialize, ())
        );
        console.log("AgioRegistry:", address(proxy));
        return address(proxy);
    }

    function _deployBatch(address vault, address registry, address deployer) internal returns (address) {
        AgioBatchSettlement impl = new AgioBatchSettlement();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl),
            abi.encodeCall(impl.initialize, (vault, registry, 500))
        );
        AgioBatchSettlement batch = AgioBatchSettlement(address(proxy));

        AgioVault(vault).grantRole(AgioVault(vault).SETTLEMENT_ROLE(), address(batch));
        AgioRegistry(registry).grantRole(AgioRegistry(registry).BATCH_SETTLEMENT_ROLE(), address(batch));
        batch.setBatchSigner(deployer);

        console.log("AgioBatchSettlement:", address(batch));
        return address(batch);
    }

    function _deploySwapRouter(address vault, address deployer, bool isMainnet) internal returns (address) {
        address dexRouter = isMainnet ? UNISWAP_ROUTER : address(0);
        AgioSwapRouter impl = new AgioSwapRouter();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl),
            abi.encodeCall(impl.initialize, (vault, dexRouter, deployer, 30))
        );
        console.log("AgioSwapRouter:", address(proxy));
        return address(proxy);
    }
}
