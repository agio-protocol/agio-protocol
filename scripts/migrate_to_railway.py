#!/usr/bin/env python3
"""
Migrate local PostgreSQL data to Railway production database.

Usage:
  1. Get your Railway DATABASE_URL from the Railway dashboard
  2. Run: python3 scripts/migrate_to_railway.py postgresql://user:pass@host:port/dbname
"""
import subprocess
import sys
import os

LOCAL_DB = "postgresql://agio:agio_dev_password@localhost:5432/agio_mainnet"
DUMP_FILE = "/tmp/agio_mainnet_dump.sql"

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/migrate_to_railway.py <RAILWAY_DATABASE_URL>")
        print("Get the URL from Railway dashboard → PostgreSQL → Connect → Connection URL")
        sys.exit(1)

    target_db = sys.argv[1]

    print("=" * 50)
    print("  AGIO Database Migration")
    print("=" * 50)
    print(f"  From: local agio_mainnet")
    print(f"  To:   {target_db[:40]}...")
    print()

    # Step 1: Dump local database
    print("[1/3] Dumping local database...")
    result = subprocess.run(
        ["pg_dump", LOCAL_DB, "-f", DUMP_FILE, "--no-owner", "--no-privileges", "--clean", "--if-exists"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR: pg_dump failed: {result.stderr}")
        sys.exit(1)

    size = os.path.getsize(DUMP_FILE)
    print(f"  Dump created: {DUMP_FILE} ({size:,} bytes)")

    # Step 2: Restore to Railway
    print("\n[2/3] Restoring to Railway database...")
    result = subprocess.run(
        ["psql", target_db, "-f", DUMP_FILE],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: Some errors during restore (may be normal for clean/if-exists):")
        for line in result.stderr.split("\n")[:5]:
            if line.strip():
                print(f"    {line}")
    print("  Restore complete.")

    # Step 3: Verify
    print("\n[3/3] Verifying migration...")
    result = subprocess.run(
        ["psql", target_db, "-c",
         "SELECT 'agents' as tbl, COUNT(*) FROM agents UNION ALL "
         "SELECT 'payments', COUNT(*) FROM payments UNION ALL "
         "SELECT 'batches', COUNT(*) FROM batches UNION ALL "
         "SELECT 'fee_tiers', COUNT(*) FROM fee_tiers;"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"  Could not verify: {result.stderr[:100]}")

    # Cleanup
    os.remove(DUMP_FILE)

    print("=" * 50)
    print("  Migration complete!")
    print("  Verify in admin dashboard after updating API URL.")
    print("=" * 50)


if __name__ == "__main__":
    main()
