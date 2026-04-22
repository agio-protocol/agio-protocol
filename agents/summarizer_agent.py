"""Summarizer Agent — Text summarization for $0.01/query. 70% margin."""
import os
from base_agent import BaseAgent


class SummarizerAgent(BaseAgent):
    PRICE = 0.01

    def __init__(self):
        super().__init__("summarizer", service_type="summarization", price=self.PRICE)

    async def handle_query(self, text: str) -> dict:
        """Summarize text. Uses Claude if available, template if not."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "")

        if api_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": f"Summarize in 2 sentences:\n\n{text[:1000]}"}],
                )
                summary = resp.content[0].text
            except Exception:
                summary = self._template_summary(text)
        else:
            summary = self._template_summary(text)

        self.total_earned += self.PRICE
        return {
            "summary": summary,
            "original_length": len(text),
            "summary_length": len(summary),
        }

    def _template_summary(self, text: str) -> str:
        words = text.split()
        if len(words) > 20:
            return " ".join(words[:15]) + "... (summarized from " + str(len(words)) + " words)"
        return text
