"""
SAP AI Agent for Credit Note Processing.

Receives credit charge requests from SAP Build Process Automation,
evaluates them using SAP Generative AI Hub (LLM), and returns a
structured decision for the workflow routing gateway.
"""

import os
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", "8080"))
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o")

SYSTEM_PROMPT = """\
You are a credit charge evaluation agent for a government tax agency (HMRC-style).
Your job is to evaluate incoming VAT/tax credit charge requests and decide whether to:

- "approve": Auto-approve. Use when the request is routine, the amount is reasonable,
  the duty type is standard, and there are no anomalies.
- "escalate": Send to a human approver. Use when the amount is large (above 1000),
  the notes are vague or missing, or when the request needs human judgement.
- "reject": Reject outright. Use for clearly invalid requests (negative amounts,
  missing charge type, or nonsensical data).

IMPORTANT: You MUST respond with ONLY a valid JSON object — no markdown, no explanation
outside the JSON. Use exactly this format:
{
  "decision": "approve" | "escalate" | "reject",
  "confidence": <number between 0.0 and 1.0>,
  "reasoning": "<one sentence explaining the decision>"
}"""


def _build_openai_client():
    """Build a gen_ai_hub OpenAI client, explicitly configured from env vars."""
    from ai_core_sdk.ai_core_v2_client import AICoreV2Client
    from gen_ai_hub.proxy.core.proxy_clients import GenAIHubProxyClient
    from gen_ai_hub.proxy.native.openai.clients import OpenAI

    base_url = os.environ.get("AICORE_BASE_URL", "https://api.ai.prod.us-east-1.aws.ml.hana.ondemand.com")
    auth_url = os.environ.get("AICORE_AUTH_URL")
    client_id = os.environ.get("AICORE_CLIENT_ID")
    client_secret = os.environ.get("AICORE_CLIENT_SECRET")
    resource_group = os.environ.get("AICORE_RESOURCE_GROUP", "default")

    if not all([auth_url, client_id, client_secret]):
        raise EnvironmentError(
            "Missing required env vars: AICORE_AUTH_URL, AICORE_CLIENT_ID, AICORE_CLIENT_SECRET"
        )

    ai_core_client = AICoreV2Client(
        base_url=base_url,
        auth_url=auth_url,
        client_id=client_id,
        client_secret=client_secret,
        resource_group=resource_group,
    )
    proxy_client = GenAIHubProxyClient(ai_core_client=ai_core_client)
    return OpenAI(proxy_client=proxy_client)


def evaluate_with_llm(charges: dict) -> dict:
    """Call SAP Generative AI Hub to evaluate the credit charge request."""
    try:
        client = _build_openai_client()
    except Exception as e:
        logger.error("Failed to initialise Generative AI Hub client: %s", e)
        raise

    amount = charges.get("amount", charges.get("Amount", 0))
    charge_tp = charges.get("charge_TP", charges.get("Charge_TP", "Unknown"))
    duty_tp = charges.get("duty_TP", charges.get("Duty_TP", "Unknown"))
    issue_dt = charges.get("issue_Dt", charges.get("Issue_Dt", "Unknown"))
    per_fr = charges.get("per_Fr_Dt", charges.get("Per_Fr_Dt", "Unknown"))
    per_to = charges.get("per_To_Dt", charges.get("Per_To_Dt", "Unknown"))
    notes = charges.get("loc_Ref_Notes", charges.get("Loc_Ref_Notes", "None provided"))

    user_message = f"""\
Evaluate this VAT/tax credit charge request:

  Charge Type   : {charge_tp}
  Duty Type     : {duty_tp}
  Amount        : {amount}
  Issue Date    : {issue_dt}
  Period From   : {per_fr}
  Period To     : {per_to}
  Reference Notes: {notes}

Return your decision as JSON."""

    logger.info("Sending request to Gen AI Hub model %s", AGENT_MODEL)
    response = client.chat.completions.create(
        model=AGENT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )

    raw = response.choices[0].message.content
    logger.info("LLM raw response: %s", raw)
    return json.loads(raw)


def evaluate_credit_request(request: dict) -> dict:
    """
    Main evaluation entry point called by /v1/evaluate.

    The SAP Build PA workflow sends either:
      { "charges": { amount, charge_TP, duty_TP, ... } }   ← workflow context shape
    or the OpenAPI schema fields directly:
      { "credit_note_id": ..., "amount": ..., "reason": ... }
    We handle both.
    """
    # Prefer the nested 'charges' object if present (workflow sends this)
    charges = request.get("charges") or request

    amount = charges.get("amount", charges.get("Amount", 0))
    logger.info("Evaluating credit charge: amount=%s, request keys=%s", amount, list(charges.keys()))

    try:
        llm_result = evaluate_with_llm(charges)
        decision = llm_result.get("decision", "escalate")
        confidence = float(llm_result.get("confidence", 0.5))
        reasoning = llm_result.get("reasoning", "No reasoning provided.")
    except Exception as e:
        logger.error("LLM evaluation failed: %s — falling back to escalate", e)
        decision = "escalate"
        confidence = 0.0
        reasoning = f"LLM evaluation failed ({e}); routing to human approver."

    # Validate decision value
    if decision not in ("approve", "escalate", "reject"):
        logger.warning("Unexpected decision '%s' from LLM — defaulting to escalate", decision)
        decision = "escalate"

    logger.info("Decision: %s (confidence=%.2f) — %s", decision, confidence, reasoning)

    return {
        "decision": decision,
        "confidence": confidence,
        "reasoning": reasoning,
        "actions": [],
    }


class AgentHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/v1/health":
            self._respond(200, {"status": "healthy"})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/v1/evaluate":
            body = self._read_body()
            result = evaluate_credit_request(body)
            self._respond(200, result)
            return
        self._respond(404, {"error": "not found"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _respond(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, fmt, *args):  # suppress default access log noise
        logger.debug(fmt, *args)


def main():
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    logger.info("Credit agent serving on port %d (model=%s)", PORT, AGENT_MODEL)
    server.serve_forever()


if __name__ == "__main__":
    main()
