"""
x402 Test Client — makes a real x402 payment against Agiotage API.
Triggers agentic.market auto-indexing when payment processes through facilitator.
"""
import asyncio
import os

from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from eth_account import Account

API_URL = "https://agio-protocol-production.up.railway.app"


async def main():
    # Get the deployer private key
    private_key = os.environ.get("BATCH_SUBMITTER_PRIVATE_KEY", "")
    if not private_key:
        print("Set BATCH_SUBMITTER_PRIVATE_KEY env var")
        return

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    account = Account.from_key(private_key)
    print(f"Wallet: {account.address}")

    # Create x402 client with EVM signer
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account))

    print(f"\nStep 1: Hitting {API_URL}/v1/jobs/post without payment...")

    async with x402HttpxClient(client) as http:
        # The x402HttpxClient automatically:
        # 1. Sends the request
        # 2. Gets 402 back
        # 3. Parses payment requirements from header
        # 4. Signs USDC payment
        # 5. Sends payment through facilitator
        # 6. Retries request with payment proof
        response = await http.post(
            f"{API_URL}/v1/jobs/post",
            json={
                "poster_agio_id": "0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                "title": "x402 test payment",
                "description": "Testing x402 payment flow for agentic.market indexing",
                "category": "custom",
                "budget": 0.01,
            },
        )

        print(f"\nStep 2: Response status: {response.status_code}")
        print(f"Response body: {response.text[:300]}")

        if response.status_code == 200:
            print("\n✓ SUCCESS — x402 payment processed!")
            print("Agiotage should appear on agentic.market within 24 hours.")

            # Check settle response
            try:
                settle = http.get_payment_settle_response(
                    lambda name: response.headers.get(name)
                )
                print(f"Settle response: {settle}")
            except Exception as e:
                print(f"Settle info: {e}")
        elif response.status_code == 402:
            print("\n✗ Still 402 — payment was not processed")
            print("The facilitator may not support this network yet")
        else:
            print(f"\n? Unexpected status: {response.status_code}")
            print(f"Body: {response.text[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
