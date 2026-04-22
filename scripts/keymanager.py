#!/usr/bin/env python3
"""
AGIO Key Manager — macOS Keychain wrapper for deployment secrets.

Keys are stored encrypted in macOS Keychain, never on disk.
Uses the 'keyring' library which delegates to Keychain.app.

Usage:
    from keymanager import get_key, list_keys

    deployer_key = get_key("DEPLOYER_PRIVATE_KEY")
"""
import sys

try:
    import keyring
except ImportError:
    print("ERROR: 'keyring' not installed. Run: pip3 install keyring")
    sys.exit(1)

SERVICE_NAME = "agio-protocol"

ALL_KEY_NAMES = [
    "DEPLOYER_PRIVATE_KEY",
    "DEPLOYER_ADDRESS",
    "BATCH_SIGNER_PRIVATE_KEY",
    "BATCH_SIGNER_ADDRESS",
    "BATCH_SUBMITTER_PRIVATE_KEY",
    "FEE_COLLECTOR_ADDRESS",
]

SECRET_KEYS = {
    "DEPLOYER_PRIVATE_KEY",
    "BATCH_SIGNER_PRIVATE_KEY",
    "BATCH_SUBMITTER_PRIVATE_KEY",
}


def store_key(name: str, value: str):
    keyring.set_password(SERVICE_NAME, name, value)


def get_key(name: str) -> str | None:
    return keyring.get_password(SERVICE_NAME, name)


def delete_key(name: str):
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except keyring.errors.PasswordDeleteError:
        pass


def list_keys() -> list[dict]:
    results = []
    for name in ALL_KEY_NAMES:
        val = get_key(name)
        is_secret = name in SECRET_KEYS
        if val:
            display = "********" if is_secret else val
            results.append({"name": name, "status": "SET", "value": display})
        else:
            results.append({"name": name, "status": "NOT SET", "value": ""})
    return results


def require_key(name: str) -> str:
    val = get_key(name)
    if not val:
        print(f"ERROR: '{name}' not found in Keychain.")
        print(f"Run: python3 scripts/setup_keys.py")
        sys.exit(1)
    return val


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AGIO Key Manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List stored keys (names only, no secret values)")
    del_parser = sub.add_parser("delete", help="Delete a key")
    del_parser.add_argument("name", help="Key name to delete")
    sub.add_parser("delete-all", help="Delete all AGIO keys from Keychain")

    args = parser.parse_args()

    if args.command == "list":
        keys = list_keys()
        print(f"\nAGIO Keychain entries (service: {SERVICE_NAME}):\n")
        for k in keys:
            status = k["status"]
            icon = "[SET]    " if status == "SET" else "[EMPTY]  "
            line = f"  {icon} {k['name']}"
            if k["value"] and k["value"] != "********":
                line += f" = {k['value']}"
            print(line)
        print()

    elif args.command == "delete":
        delete_key(args.name)
        print(f"Deleted '{args.name}' from Keychain.")

    elif args.command == "delete-all":
        confirm = input("Delete ALL AGIO keys from Keychain? Type 'YES': ")
        if confirm == "YES":
            for name in ALL_KEY_NAMES:
                delete_key(name)
            print("All AGIO keys deleted.")
        else:
            print("Aborted.")

    else:
        parser.print_help()
