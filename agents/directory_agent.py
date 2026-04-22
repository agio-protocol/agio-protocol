"""Directory Agent — Helps agents find services. FREE. Drives adoption."""
from base_agent import BaseAgent


class DirectoryAgent(BaseAgent):
    def __init__(self):
        super().__init__("directory", service_type="directory", price=0)
        self.services: dict[str, list[dict]] = {}

    async def register_provider(self, agio_id: str, service_type: str, price: float, description: str):
        if service_type not in self.services:
            self.services[service_type] = []
        self.services[service_type].append({
            "agio_id": agio_id, "service_type": service_type,
            "price": price, "description": description,
        })

    async def find_service(self, service_type: str) -> list[dict]:
        return self.services.get(service_type, [])

    async def list_all(self) -> dict:
        return {k: len(v) for k, v in self.services.items()}
