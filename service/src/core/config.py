"""Settings management. Non-secret config from env vars, secrets from macOS Keychain."""
from pydantic_settings import BaseSettings


def _get_keychain(name: str, default: str = "") -> str:
    try:
        import keyring
        val = keyring.get_password("agio-protocol", name)
        return val if val else default
    except Exception:
        return default


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://agio:agio_dev_password@localhost:5432/agio"
    redis_url: str = "redis://localhost:6379/0"

    rpc_url: str = "https://sepolia.base.org"
    vault_address: str = ""
    batch_settlement_address: str = ""
    registry_address: str = ""
    swap_router_address: str = ""

    api_secret_key: str = "change-me-in-production"
    batch_interval_seconds: int = 60
    max_batch_size: int = 500

    # These can still be set via env vars for testnet/dev,
    # but on mainnet they come from Keychain.
    batch_submitter_private_key: str = ""
    batch_signer_private_key: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False

    def get_batch_submitter_key(self) -> str:
        if self.batch_submitter_private_key:
            return self.batch_submitter_private_key
        return _get_keychain("BATCH_SUBMITTER_PRIVATE_KEY")

    def get_batch_signer_key(self) -> str:
        if self.batch_signer_private_key:
            return self.batch_signer_private_key
        return _get_keychain("BATCH_SIGNER_PRIVATE_KEY")

    def get_deployer_address(self) -> str:
        return _get_keychain("DEPLOYER_ADDRESS")

    def get_fee_collector_address(self) -> str:
        return _get_keychain("FEE_COLLECTOR_ADDRESS")


settings = Settings()
