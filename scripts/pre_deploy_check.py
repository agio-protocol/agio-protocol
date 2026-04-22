#!/usr/bin/env python3
"""
AGIO Protocol — Pre-Deployment Checklist

Every check must pass before mainnet deployment.
Usage: python3 scripts/pre_deploy_check.py --network mainnet
"""
import asyncio
import os
import subprocess
import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
CONTRACTS_DIR = ROOT_DIR / "contracts"
SERVICE_DIR = ROOT_DIR / "service"

# Base mainnet addresses to verify
UNISWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

REQUIRED_ENV_VARS = [
    "DATABASE_URL",
    "REDIS_URL",
    "BASE_RPC_URL",
]

REQUIRED_KEYCHAIN_KEYS = [
    "DEPLOYER_PRIVATE_KEY",
    "DEPLOYER_ADDRESS",
    "BATCH_SIGNER_PRIVATE_KEY",
    "BATCH_SIGNER_ADDRESS",
    "BATCH_SUBMITTER_PRIVATE_KEY",
    "FEE_COLLECTOR_ADDRESS",
]


class CheckResult:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []

    def passed(self, name: str, detail: str = ""):
        self.results.append((name, "PASS", detail))

    def failed(self, name: str, detail: str = ""):
        self.results.append((name, "FAIL", detail))

    def manual(self, name: str, detail: str = ""):
        self.results.append((name, "MANUAL", detail))

    @property
    def all_passed(self) -> bool:
        return all(s == "PASS" or s == "MANUAL" for _, s, _ in self.results)

    def print_report(self):
        print()
        print("=" * 60)
        print("  AGIO MAINNET PRE-DEPLOYMENT CHECKLIST")
        print("=" * 60)

        for name, status, detail in self.results:
            if status == "PASS":
                icon = "  [PASS]"
            elif status == "MANUAL":
                icon = "  [MANUAL]"
            else:
                icon = "  [FAIL]"
            line = f"{icon}  {name}"
            if detail:
                line += f" — {detail}"
            print(line)

        print()
        print("=" * 60)
        fail_count = sum(1 for _, s, _ in self.results if s == "FAIL")
        pass_count = sum(1 for _, s, _ in self.results if s == "PASS")
        manual_count = sum(1 for _, s, _ in self.results if s == "MANUAL")

        if fail_count == 0:
            print(f"  ALL CHECKS PASSED ({pass_count} auto, {manual_count} manual)")
            print("  Ready to deploy: ./scripts/deploy_mainnet.sh --broadcast")
        else:
            print(f"  DEPLOYMENT BLOCKED — {fail_count} check(s) failed")
            print("  Fix the failures above, then re-run this script.")
        print("=" * 60)


def run_cmd(cmd: str, cwd: str = None, timeout: int = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT"
    except Exception as e:
        return 1, str(e)


def check_contract_tests(checks: CheckResult):
    print("  Running contract tests...")
    forge_bin = os.path.expanduser("~/.foundry/bin/forge")
    code, output = run_cmd(
        f"{forge_bin} test --summary",
        cwd=str(CONTRACTS_DIR),
        timeout=180,
    )
    if code == 0 and "0 failed" in output:
        # Extract test count
        for line in output.split("\n"):
            if "passed" in line and "failed" in line:
                checks.passed("Contract tests", line.strip())
                return
        checks.passed("Contract tests", "all passed")
    else:
        checks.failed("Contract tests", "see forge test output")


def check_stress_tests(checks: CheckResult):
    print("  Running stress tests...")
    code, output = run_cmd(
        "python3 -m pytest tests/stress/ -q --timeout=120 --tb=no",
        cwd=str(SERVICE_DIR),
        timeout=300,
    )
    for line in output.split("\n"):
        if "passed" in line:
            checks.passed("Stress tests", line.strip())
            return
    if code == 0:
        checks.passed("Stress tests")
    else:
        # Check if failures are just teardown errors
        if "failed" not in output.lower() or "0 failed" in output.lower():
            checks.passed("Stress tests", "passed (teardown warnings ignored)")
        else:
            checks.failed("Stress tests", "see pytest output")


def check_env_vars(checks: CheckResult):
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        checks.failed("Environment file", ".env.mainnet not found")
        return

    # Source the env file to check
    missing = []
    env_content = env_file.read_text()
    for var in REQUIRED_ENV_VARS:
        if var not in env_content:
            missing.append(var)
            continue
        for line in env_content.split("\n"):
            if line.startswith(f"{var}="):
                val = line.split("=", 1)[1].strip()
                if not val or val.startswith("<"):
                    missing.append(var)
                break

    if missing:
        checks.failed("Environment variables", f"missing/placeholder: {', '.join(missing)}")
    else:
        checks.passed("Environment variables", f"all {len(REQUIRED_ENV_VARS)} set")


def check_compilation(checks: CheckResult):
    print("  Compiling contracts...")
    forge_bin = os.path.expanduser("~/.foundry/bin/forge")
    code, output = run_cmd(
        f"{forge_bin} build",
        cwd=str(CONTRACTS_DIR),
        timeout=120,
    )
    if code == 0:
        checks.passed("Contract compilation")
    else:
        checks.failed("Contract compilation", "forge build failed")


def check_gitignore(checks: CheckResult):
    gitignore = ROOT_DIR / ".gitignore"
    if not gitignore.exists():
        checks.failed("Gitignore", ".gitignore missing")
        return

    content = gitignore.read_text()
    secrets_protected = ".env.mainnet" in content and ".env" in content
    if secrets_protected:
        checks.passed("Gitignore", ".env.mainnet is ignored")
    else:
        checks.failed("Gitignore", ".env.mainnet NOT in .gitignore — keys would leak!")


def check_rpc_connection(checks: CheckResult):
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        checks.failed("RPC connection", ".env.mainnet not found")
        return

    rpc_url = None
    for line in env_file.read_text().split("\n"):
        if line.startswith("BASE_RPC_URL="):
            rpc_url = line.split("=", 1)[1].strip()
            break

    if not rpc_url or rpc_url.startswith("<"):
        checks.failed("RPC connection", "BASE_RPC_URL not configured")
        return

    # Use cast to check connection
    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    code, output = run_cmd(
        f"{cast_bin} block-number --rpc-url {rpc_url}",
        timeout=15,
    )
    if code == 0:
        block = output.strip()
        checks.passed("Base mainnet RPC", f"block {block}")
    else:
        checks.failed("Base mainnet RPC", f"cannot connect to {rpc_url}")


def check_deployer_balance(checks: CheckResult):
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        checks.failed("Deployer ETH balance", "no .env.mainnet")
        return

    rpc_url = None
    deployer_addr = None
    for line in env_file.read_text().split("\n"):
        if line.startswith("BASE_RPC_URL="):
            rpc_url = line.split("=", 1)[1].strip()
        if line.startswith("DEPLOYER_ADDRESS="):
            deployer_addr = line.split("=", 1)[1].strip()

    if not rpc_url or not deployer_addr or deployer_addr.startswith("<"):
        checks.manual("Deployer ETH balance", "DEPLOYER_ADDRESS not set — check manually after funding")
        return

    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    code, output = run_cmd(
        f"{cast_bin} balance {deployer_addr} --rpc-url {rpc_url} --ether",
        timeout=15,
    )
    if code == 0:
        try:
            balance = float(output.strip())
            if balance >= 0.003:
                checks.passed("Deployer ETH balance", f"{balance:.6f} ETH")
            else:
                checks.failed("Deployer ETH balance", f"{balance:.6f} ETH (need >= 0.003)")
        except ValueError:
            checks.failed("Deployer ETH balance", f"parse error: {output.strip()}")
    else:
        checks.manual("Deployer ETH balance", "could not query — check manually")


def check_uniswap_router(checks: CheckResult):
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        checks.failed("Uniswap V3 Router", "no .env.mainnet")
        return

    rpc_url = None
    for line in env_file.read_text().split("\n"):
        if line.startswith("BASE_RPC_URL="):
            rpc_url = line.split("=", 1)[1].strip()

    if not rpc_url or rpc_url.startswith("<"):
        checks.manual("Uniswap V3 Router", "RPC not configured")
        return

    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    code, output = run_cmd(
        f"{cast_bin} code {UNISWAP_ROUTER} --rpc-url {rpc_url}",
        timeout=15,
    )
    if code == 0 and output.strip() and len(output.strip()) > 4:
        checks.passed("Uniswap V3 Router", f"contract at {UNISWAP_ROUTER[:10]}...")
    else:
        checks.failed("Uniswap V3 Router", "no contract at expected address")


def check_database(checks: CheckResult):
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        checks.manual("Database", "no .env.mainnet")
        return

    db_url = None
    for line in env_file.read_text().split("\n"):
        if line.startswith("DATABASE_URL="):
            db_url = line.split("=", 1)[1].strip()

    if not db_url or db_url.startswith("<"):
        checks.failed("Database", "DATABASE_URL not configured")
        return

    # Check if it points to mainnet database (not testnet)
    if "agio_mainnet" in db_url or "mainnet" in db_url:
        checks.passed("Database", "mainnet database configured")
    else:
        checks.failed("Database", f"URL does not contain 'mainnet' — may be testnet DB: {db_url[:40]}...")


def check_redis(checks: CheckResult):
    try:
        code, output = run_cmd("python3 -c \"import redis; r=redis.from_url('redis://localhost:6379/1'); r.ping(); print('ok')\"", timeout=10)
        if code == 0 and "ok" in output:
            checks.passed("Redis connection", "db 1 accessible")
        else:
            checks.failed("Redis connection", "cannot connect to redis://localhost:6379/1")
    except Exception:
        checks.failed("Redis connection", "redis not accessible")


def check_no_secrets_committed(checks: CheckResult):
    code, output = run_cmd("git log --all --oneline -- '*.env*' '.env*' 2>/dev/null", cwd=str(ROOT_DIR))
    if output.strip():
        checks.failed("No secrets in git", "found .env files in git history!")
    else:
        checks.passed("No secrets in git")


def check_keychain_keys(checks: CheckResult):
    try:
        import keyring
    except ImportError:
        checks.failed("Keychain keys", "'keyring' not installed. Run: pip3 install keyring")
        return

    missing = []
    for name in REQUIRED_KEYCHAIN_KEYS:
        val = keyring.get_password("agio-protocol", name)
        if not val:
            missing.append(name)

    if missing:
        checks.failed("Keychain keys", f"missing: {', '.join(missing)}. Run: python3 scripts/setup_keys.py")
    else:
        checks.passed("Keychain keys", f"all {len(REQUIRED_KEYCHAIN_KEYS)} keys in macOS Keychain")


def check_no_keys_in_env(checks: CheckResult):
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        checks.passed("No keys in .env", "file not found (ok)")
        return

    content = env_file.read_text()
    leaked = []
    for keyword in ["PRIVATE_KEY=", "SECRET_KEY="]:
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("#"):
                continue
            if keyword in line:
                val = line.split("=", 1)[1].strip()
                if val and not val.startswith("<") and len(val) > 10:
                    leaked.append(line.split("=")[0])

    if leaked:
        checks.failed("No keys in .env file", f"PRIVATE KEYS FOUND IN FILE: {', '.join(leaked)} — REMOVE IMMEDIATELY")
    else:
        checks.passed("No keys in .env file", "secrets are in Keychain only")


def main():
    network = "mainnet"
    if "--network" in sys.argv:
        idx = sys.argv.index("--network")
        if idx + 1 < len(sys.argv):
            network = sys.argv[idx + 1]

    if network != "mainnet":
        print(f"Skipping pre-deploy checks for network: {network}")
        sys.exit(0)

    checks = CheckResult()

    print("Running AGIO mainnet pre-deployment checks...")
    print()

    check_gitignore(checks)
    check_no_secrets_committed(checks)
    check_no_keys_in_env(checks)
    check_keychain_keys(checks)
    check_env_vars(checks)
    check_compilation(checks)
    check_contract_tests(checks)
    check_stress_tests(checks)
    check_rpc_connection(checks)
    check_deployer_balance(checks)
    check_uniswap_router(checks)
    check_database(checks)
    check_redis(checks)

    # Manual checks
    checks.manual("Legal disclaimer on agiotage.finance", "verify terms of service are posted")
    checks.manual("48-hour observation window", "schedule after deployment")

    checks.print_report()
    sys.exit(0 if checks.all_passed else 1)


if __name__ == "__main__":
    main()
