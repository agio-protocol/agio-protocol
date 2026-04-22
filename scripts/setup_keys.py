#!/usr/bin/env python3
"""
AGIO Key Setup — One-time interactive key entry into macOS Keychain.

Private keys are entered via getpass (hidden on screen).
Addresses are entered normally (visible — they're public).

Run once: python3 scripts/setup_keys.py
Keys persist in macOS Keychain until you delete them.
"""
import getpass
import sys

try:
    import keyring
except ImportError:
    print("ERROR: 'keyring' not installed. Run: pip3 install keyring")
    sys.exit(1)

SERVICE_NAME = "agio-protocol"


def prompt_secret(label: str) -> str:
    while True:
        val = getpass.getpass(f"  {label}: ")
        if not val.strip():
            print("    (empty — skipping)")
            return ""
        val = val.strip()
        if label.endswith("PRIVATE_KEY") and not val.startswith("0x"):
            val_check = val
        else:
            val_check = val
        confirm = getpass.getpass(f"  Confirm {label}: ")
        if val == confirm.strip():
            return val
        print("    Values don't match. Try again.")


def prompt_address(label: str) -> str:
    while True:
        val = input(f"  {label}: ").strip()
        if not val:
            print("    (empty — skipping)")
            return ""
        if not val.startswith("0x") or len(val) != 42:
            print("    Invalid address format. Must be 0x + 40 hex chars.")
            continue
        return val


def main():
    print()
    print("=" * 56)
    print("  AGIO KEY SETUP — macOS Keychain")
    print("=" * 56)
    print()
    print("  Private keys will be HIDDEN as you type.")
    print("  Addresses will be visible (they're public).")
    print("  All values stored encrypted in macOS Keychain.")
    print()

    # Check for existing keys
    existing = []
    for name in ["DEPLOYER_PRIVATE_KEY", "BATCH_SIGNER_PRIVATE_KEY"]:
        if keyring.get_password(SERVICE_NAME, name):
            existing.append(name)

    if existing:
        print(f"  Found existing keys: {', '.join(existing)}")
        choice = input("  Overwrite? (y/N): ").strip().lower()
        if choice != "y":
            print("  Keeping existing keys. Run 'python3 scripts/keymanager.py list' to check.")
            return

    print()
    print("  --- WALLET 1: DEPLOYER ---")
    print("  Deploys contracts, submits batches. Needs ETH for gas.")
    deployer_key = prompt_secret("DEPLOYER_PRIVATE_KEY")
    deployer_addr = prompt_address("DEPLOYER_ADDRESS")

    print()
    print("  --- WALLET 2: BATCH SIGNER ---")
    print("  Signs batch hashes. No ETH needed.")
    signer_key = prompt_secret("BATCH_SIGNER_PRIVATE_KEY")
    signer_addr = prompt_address("BATCH_SIGNER_ADDRESS")

    print()
    print("  --- BATCH SUBMITTER ---")
    print("  Submits batches on-chain. Usually same as deployer.")
    use_deployer = input("  Use deployer key as submitter? (Y/n): ").strip().lower()
    if use_deployer == "n":
        submitter_key = prompt_secret("BATCH_SUBMITTER_PRIVATE_KEY")
    else:
        submitter_key = deployer_key
        print("  Using deployer key as submitter.")

    print()
    print("  --- WALLET 3: FEE COLLECTOR ---")
    print("  Receives protocol fees. No private key needed.")
    fee_addr = prompt_address("FEE_COLLECTOR_ADDRESS")

    # Store in Keychain
    print()
    print("  Storing in macOS Keychain...")

    entries = [
        ("DEPLOYER_PRIVATE_KEY", deployer_key),
        ("DEPLOYER_ADDRESS", deployer_addr),
        ("BATCH_SIGNER_PRIVATE_KEY", signer_key),
        ("BATCH_SIGNER_ADDRESS", signer_addr),
        ("BATCH_SUBMITTER_PRIVATE_KEY", submitter_key),
        ("FEE_COLLECTOR_ADDRESS", fee_addr),
    ]

    stored = 0
    for name, val in entries:
        if val:
            keyring.set_password(SERVICE_NAME, name, val)
            stored += 1

    print(f"  Stored {stored} entries in Keychain.")

    # Verify
    print()
    print("  Verifying...")
    all_ok = True
    for name, val in entries:
        if val:
            retrieved = keyring.get_password(SERVICE_NAME, name)
            if retrieved == val:
                is_secret = "PRIVATE_KEY" in name
                display = "********" if is_secret else retrieved
                print(f"    [OK] {name} = {display}")
            else:
                print(f"    [FAIL] {name} — stored value doesn't match!")
                all_ok = False

    print()
    print("=" * 56)
    if all_ok:
        print("  ALL KEYS STORED AND VERIFIED")
        print()
        print("  Keys are encrypted in macOS Keychain.")
        print("  They persist across reboots and terminal sessions.")
        print("  To check: python3 scripts/keymanager.py list")
        print("  To delete: python3 scripts/keymanager.py delete-all")
    else:
        print("  SOME KEYS FAILED VERIFICATION")
        print("  Run setup again: python3 scripts/setup_keys.py")
    print("=" * 56)


if __name__ == "__main__":
    main()
