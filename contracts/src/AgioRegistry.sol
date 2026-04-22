// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {IAgioRegistry} from "./interfaces/IAgioRegistry.sol";

/// @title AgioRegistry — On-chain agent identity and reputation
/// @notice Agents register here to participate in AGIO. Stats and tiers
///         are updated by the batch settlement contract after each batch.
contract AgioRegistry is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    IAgioRegistry
{
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant BATCH_SETTLEMENT_ROLE = keccak256("BATCH_SETTLEMENT_ROLE");
    bytes32 public constant TIER_MANAGER_ROLE = keccak256("TIER_MANAGER_ROLE");

    struct AgentInfo {
        bytes32 agentId;
        address wallet;
        uint64 registeredAt;
        uint64 totalPayments;
        uint256 totalVolume;
        string metadata;
        AgentTier tier;
    }

    uint256 public registrationFee; // anti-spam fee in wei (default 0 for testnet)

    mapping(address => AgentInfo) private _agents;
    mapping(bytes32 => address) private _agentIdToWallet;

    event AgentRegistered(bytes32 indexed agentId, address indexed wallet, uint256 timestamp);
    event AgentTierUpdated(bytes32 indexed agentId, AgentTier oldTier, AgentTier newTier);
    event AgentMetadataUpdated(bytes32 indexed agentId);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize() external initializer {
        __AccessControl_init();

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(UPGRADER_ROLE, msg.sender);
        _grantRole(TIER_MANAGER_ROLE, msg.sender);
    }

    /// @notice Register a new agent identity
    /// @param agentId A unique identifier for this agent
    /// @param metadata JSON string with agent details
    /// @notice Register a new agent identity
    /// @dev Requires registrationFee in native token to prevent spam
    function registerAgent(bytes32 agentId, string calldata metadata) external payable {
        require(msg.value >= registrationFee, "AgioRegistry: insufficient registration fee");
        require(_agents[msg.sender].registeredAt == 0, "AgioRegistry: already registered");
        require(_agentIdToWallet[agentId] == address(0), "AgioRegistry: agentId taken");

        _agents[msg.sender] = AgentInfo({
            agentId: agentId,
            wallet: msg.sender,
            registeredAt: uint64(block.timestamp),
            totalPayments: 0,
            totalVolume: 0,
            metadata: metadata,
            tier: AgentTier.NEW
        });
        _agentIdToWallet[agentId] = msg.sender;

        emit AgentRegistered(agentId, msg.sender, block.timestamp);
    }

    /// @notice Update agent metadata
    function updateMetadata(string calldata metadata) external {
        require(_agents[msg.sender].registeredAt > 0, "AgioRegistry: not registered");
        _agents[msg.sender].metadata = metadata;
        emit AgentMetadataUpdated(_agents[msg.sender].agentId);
    }

    /// @notice Increment agent payment stats (called by batch settlement)
    function incrementStats(
        address wallet,
        uint256 paymentCount,
        uint256 volume
    ) external onlyRole(BATCH_SETTLEMENT_ROLE) {
        AgentInfo storage agent = _agents[wallet];
        if (agent.registeredAt == 0) return; // skip unregistered

        agent.totalPayments += uint64(paymentCount);
        agent.totalVolume += volume;

        // Auto-upgrade tier based on thresholds
        _checkTierUpgrade(agent);
    }

    function _checkTierUpgrade(AgentInfo storage agent) private {
        uint256 age = block.timestamp - agent.registeredAt;
        AgentTier oldTier = agent.tier;
        AgentTier newTier = oldTier;

        if (agent.tier == AgentTier.OPERATOR) return; // manually set, don't auto-change

        if (agent.totalPayments >= 10000 && age >= 90 days) {
            newTier = AgentTier.TRUSTED;
        } else if (agent.totalPayments >= 1000 && age >= 30 days) {
            newTier = AgentTier.VERIFIED;
        } else if (agent.totalPayments >= 100 && age >= 7 days) {
            newTier = AgentTier.ACTIVE;
        }

        if (newTier != oldTier) {
            agent.tier = newTier;
            emit AgentTierUpdated(agent.agentId, oldTier, newTier);
        }
    }

    /// @notice Manually set agent tier (admin only)
    function setTier(address wallet, AgentTier tier) external onlyRole(TIER_MANAGER_ROLE) {
        require(_agents[wallet].registeredAt > 0, "AgioRegistry: not registered");
        AgentTier oldTier = _agents[wallet].tier;
        _agents[wallet].tier = tier;
        emit AgentTierUpdated(_agents[wallet].agentId, oldTier, tier);
    }

    function getAgent(address wallet) external view returns (AgentInfo memory) {
        return _agents[wallet];
    }

    function getAgentById(bytes32 agentId) external view returns (AgentInfo memory) {
        return _agents[_agentIdToWallet[agentId]];
    }

    function isRegistered(address wallet) external view returns (bool) {
        return _agents[wallet].registeredAt > 0;
    }

    function setRegistrationFee(uint256 fee) external onlyRole(DEFAULT_ADMIN_ROLE) {
        registrationFee = fee;
    }

    function withdrawFees(address payable to) external onlyRole(DEFAULT_ADMIN_ROLE) {
        (bool ok,) = to.call{value: address(this).balance}("");
        require(ok, "AgioRegistry: fee withdrawal failed");
    }

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
