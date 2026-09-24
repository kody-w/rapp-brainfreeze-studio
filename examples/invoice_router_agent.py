import os
from agents.basic_agent import BasicAgent


class InvoiceRouterAgent(BasicAgent):
    def __init__(self):
        self.name = "InvoiceRouter"
        self.metadata = {
            "name": self.name,
            "description": "Routes one invoice: returns the queue it goes to and whether it needs manager approval. Call for every invoice the user asks to route or triage.",
            "parameters": {"type": "object", "properties": {
                "vendor": {"type": "string", "description": "Vendor name"},
                "amount": {"type": "number", "description": "Invoice total in dollars"}},
                "required": ["vendor", "amount"]},
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, vendor="", amount=0, **kwargs):
        limit = float(os.getenv("INVOICE_APPROVAL_LIMIT", "10000"))
        if float(amount) > limit:
            return f"{vendor} ${float(amount):,.2f}: queue APPROVAL, needs AP manager sign-off (limit ${limit:,.0f})."
        return f"{vendor} ${float(amount):,.2f}: queue AUTO-PAY, no approval needed."
