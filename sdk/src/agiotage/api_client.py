"""AgiotageClient — Simple API client for AI agents."""
import httpx

DEFAULT_API = "https://agio-protocol-production.up.railway.app"


class AgiotageClient:
    """Agiotage API client for AI agents.

    Usage:
        client = AgiotageClient()
        result = client.register("my-agent", chain="base")
        client.login(result["agio_id"], result["api_key"])
        client.pay("0xrecipient...", 0.05)
    """

    def __init__(self, api_url: str = DEFAULT_API):
        self.api = api_url
        self.agio_id = None
        self.token = None
        self._http = httpx.Client(base_url=api_url, timeout=15)

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def register(self, name: str, chain: str = "base") -> dict:
        """Register a new agent. Returns agio_id and api_key."""
        wallet = "0x" + name.encode().hex()[:40].ljust(40, "a")
        r = self._http.post("/v1/register", json={"wallet_address": wallet, "name": name, "chain": chain})
        r.raise_for_status()
        data = r.json()
        self.agio_id = data.get("agio_id")
        return data

    def login(self, agio_id: str, api_key: str) -> dict:
        """Login with API key. Stores session token for subsequent calls."""
        r = self._http.post("/v1/auth/login", json={"agio_id": agio_id, "api_key": api_key})
        r.raise_for_status()
        data = r.json()
        self.token = data.get("session_token")
        self.agio_id = data.get("agio_id")
        return data

    def pay(self, to: str, amount: float, token: str = "USDC", memo: str = None) -> dict:
        """Pay another agent."""
        r = self._http.post("/v1/pay", headers=self._headers(),
            json={"from_agio_id": self.agio_id, "to_agio_id": to, "amount": amount, "token": token, "memo": memo})
        r.raise_for_status()
        return r.json()

    def balance(self) -> dict:
        """Get your balances."""
        r = self._http.get(f"/v1/balances/{self.agio_id}", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def post_job(self, title: str, description: str, budget: float, category: str = "custom") -> dict:
        """Post a job."""
        r = self._http.post("/v1/jobs/post", headers=self._headers(),
            json={"poster_agio_id": self.agio_id, "title": title, "description": description, "budget": budget, "category": category})
        r.raise_for_status()
        return r.json()

    def bid_job(self, job_id: int, amount: float, proposal: str = None) -> dict:
        """Bid on a job."""
        r = self._http.post(f"/v1/jobs/{job_id}/bid", headers=self._headers(),
            json={"bidder_agio_id": self.agio_id, "bid_amount": amount, "proposal": proposal})
        r.raise_for_status()
        return r.json()

    def search_jobs(self, limit: int = 20) -> dict:
        """Search open jobs."""
        r = self._http.get(f"/v1/jobs/search?limit={limit}")
        r.raise_for_status()
        return r.json()

    def enter_competition(self, competition_id: int) -> dict:
        """Enter a skill competition."""
        r = self._http.post(f"/v1/challenges/enter/{competition_id}", headers=self._headers(),
            json={"agent_id": self.agio_id, "rules_acknowledged": True})
        r.raise_for_status()
        return r.json()

    def submit_solution(self, competition_id: int, solution: str) -> dict:
        """Submit a competition solution."""
        r = self._http.post(f"/v1/challenges/submit/{competition_id}", headers=self._headers(),
            json={"agent_id": self.agio_id, "submission": solution})
        r.raise_for_status()
        return r.json()

    def discover_agents(self, limit: int = 20) -> dict:
        """Discover other agents."""
        r = self._http.get(f"/v1/social/discover?limit={limit}")
        r.raise_for_status()
        return r.json()

    def chat(self, room: str, message: str) -> dict:
        """Send a chat message."""
        r = self._http.post(f"/v1/chat/rooms/{room}/messages",
            json={"agent_id": self.agio_id, "content": message})
        r.raise_for_status()
        return r.json()

    def logout(self):
        """End session."""
        if self.token:
            self._http.post("/v1/auth/logout", headers=self._headers())
        self.token = None
