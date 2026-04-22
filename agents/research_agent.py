"""Research Agent — The buyer. Pays other agents to answer questions."""
from base_agent import BaseAgent


class ResearchAgent(BaseAgent):
    def __init__(self):
        super().__init__("research-agent", service_type="research", price=0)
        self.oracle = None
        self.searcher = None
        self.summarizer = None

    def set_providers(self, oracle, searcher, summarizer):
        self.oracle = oracle
        self.searcher = searcher
        self.summarizer = summarizer

    async def research(self, question: str) -> dict:
        """Answer a question by paying other agents."""
        results = {}
        costs = {}

        # Pay oracle for price data
        if self.oracle and any(kw in question.lower() for kw in ["price", "cost", "worth", "eth", "btc"]):
            symbol = "eth" if "eth" in question.lower() else "btc"
            receipt = await self.pay(self.oracle, 0.001, f"price_query: {symbol}")
            results["price"] = await self.oracle.handle_query(symbol)
            costs["price_oracle"] = 0.001
            self.log(f"Paid oracle $0.001 → got {symbol.upper()} price")

        # Pay search agent
        if self.searcher:
            receipt = await self.pay(self.searcher, 0.005, f"web_search: {question}")
            results["search"] = await self.searcher.handle_query(question)
            costs["search"] = 0.005
            self.log(f"Paid search $0.005 → got {results['search']['count']} results")

        # Pay summarizer
        if self.summarizer and results.get("search"):
            text = " ".join(r.get("text", "") for r in results["search"]["results"])
            receipt = await self.pay(self.summarizer, 0.01, f"summarize: {text[:300]}")
            results["summary"] = await self.summarizer.handle_query(text)
            costs["summarizer"] = 0.01
            self.log(f"Paid summarizer $0.01 → got summary")

        total = sum(costs.values())
        return {
            "question": question,
            "results": results,
            "costs": costs,
            "total_cost": total,
        }
