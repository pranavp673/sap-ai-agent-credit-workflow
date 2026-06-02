"""
SAP AI Agent for Credit Note Processing.

Receives credit charge requests from SAP Build Process Automation,
evaluates them using SAP Generative AI Hub (GPT-4o via orchestration),
and returns a structured decision for the workflow routing gateway.
"""

import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", "8080"))
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o")

# AI Core / Gen AI Hub connection settings (injected via serving template env vars)
AICORE_BASE_URL = os.environ.get("AICORE_BASE_URL", "https://api.ai.prod.us-east-1.aws.ml.hana.ondemand.com")
AICORE_AUTH_URL = os.environ.get("AICORE_AUTH_URL", "")
AICORE_CLIENT_ID = os.environ.get("AICORE_CLIENT_ID", "")
AICORE_CLIENT_SECRET = os.environ.get("AICORE_CLIENT_SECRET", "")
AICORE_RESOURCE_GROUP = os.environ.get("AICORE_RESOURCE_GROUP", "default")
# Gen AI Hub orchestration deployment ID (the foundation-models orchestration service)
GENAI_HUB_DEPLOYMENT_ID = os.environ.get("GENAI_HUB_DEPLOYMENT_ID", "dfbb456bbb32fd15")

SYSTEM_PROMPT = """\
You are a credit charge evaluation agent for a government tax agency (HMRC-style).
Evaluate incoming VAT/tax credit charge requests and decide:

- "approve": Routine request, reasonable amount (under 1000), clear reference notes, standard duty type.
- "escalate": Large amount (1000 or above), vague/missing notes, or needs human judgement.
- "reject": Invalid data — negative amount, missing charge type, or clearly nonsensical.

Respond ONLY with a valid JSON object, no markdown:
{"decision": "approve"|"escalate"|"reject", "confidence": 0.0-1.0, "reasoning": "one sentence"}"""

# Cache the token to avoid re-requesting on every evaluation
_token_cache: dict = {"token": None, "expires_at": 0}


def _get_aicore_token() -> str:
    """Get OAuth2 client_credentials token, reusing cached value if valid."""
    import time
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["token"]

    auth_url = AICORE_AUTH_URL.rstrip("/") + "/oauth/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": AICORE_CLIENT_ID,
        "client_secret": AICORE_CLIENT_SECRET,
    }).encode()

    req = urllib.request.Request(auth_url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())

    _token_cache["token"] = body["access_token"]
    _token_cache["expires_at"] = now + int(body.get("expires_in", 3600))
    logger.info("Obtained new AI Core access token (expires in %ds)", body.get("expires_in", 3600))
    return _token_cache["token"]


def _call_genai_hub(user_message: str) -> str:
    """Call the Gen AI Hub orchestration API and return the LLM response text."""
    token = _get_aicore_token()
    url = f"{AICORE_BASE_URL}/v2/inference/deployments/{GENAI_HUB_DEPLOYMENT_ID}/completion"

    payload = {
        "orchestration_config": {
            "module_configurations": {
                "templating_module_config": {
                    "template": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "{{?user_input}}"},
                    ]
                },
                "llm_module_config": {
                    "model_name": AGENT_MODEL,
                    "model_params": {"max_tokens": 300, "temperature": 0.1},
                },
            }
        },
        "input_params": {"user_input": user_message},
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("AI-Resource-Group", AICORE_RESOURCE_GROUP)
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())

    choices = body.get("orchestration_result", {}).get("choices", [])
    if not choices:
        raise ValueError(f"No choices in orchestration response: {body}")
    return choices[0]["message"]["content"]


def evaluate_with_llm(charges: dict) -> dict:
    """Call SAP Generative AI Hub to evaluate the credit charge request."""
    amount = charges.get("amount", charges.get("Amount", 0))
    charge_tp = charges.get("charge_TP", charges.get("Charge_TP", "Unknown"))
    duty_tp = charges.get("duty_TP", charges.get("Duty_TP", "Unknown"))
    issue_dt = charges.get("issue_Dt", charges.get("Issue_Dt", "Unknown"))
    per_fr = charges.get("per_Fr_Dt", charges.get("Per_Fr_Dt", "Unknown"))
    per_to = charges.get("per_To_Dt", charges.get("Per_To_Dt", "Unknown"))
    notes = charges.get("loc_Ref_Notes", charges.get("Loc_Ref_Notes", "None provided"))

    user_message = (
        f"Evaluate this VAT/tax credit charge request:\n"
        f"  Charge Type   : {charge_tp}\n"
        f"  Duty Type     : {duty_tp}\n"
        f"  Amount        : {amount}\n"
        f"  Issue Date    : {issue_dt}\n"
        f"  Period        : {per_fr} to {per_to}\n"
        f"  Reference Notes: {notes}\n\n"
        f"Return your decision as JSON."
    )

    logger.info("Calling Gen AI Hub (model=%s, dep=%s)", AGENT_MODEL, GENAI_HUB_DEPLOYMENT_ID)
    raw = _call_genai_hub(user_message)
    logger.info("LLM response: %s", raw)

    # Strip markdown fences if present
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
        clean = clean.strip()

    return json.loads(clean)


def evaluate_credit_request(request: dict) -> dict:
    """
    Main evaluation entry point.

    Accepts either:
      { "charges": { amount, charge_TP, duty_TP, ... } }   ← SAP Build PA shape
    or flat fields directly.
    """
    charges = request.get("charges") or request
    amount = charges.get("amount", charges.get("Amount", 0))
    logger.info("Evaluating credit charge: amount=%s, keys=%s", amount, list(charges.keys()))

    try:
        result = evaluate_with_llm(charges)
        decision = result.get("decision", "escalate")
        confidence = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "No reasoning provided.")
    except Exception as e:
        logger.error("LLM evaluation failed: %s — falling back to escalate", e)
        decision = "escalate"
        confidence = 0.0
        reasoning = f"LLM evaluation failed ({e}); routing to human approver."

    if decision not in ("approve", "escalate", "reject"):
        logger.warning("Unexpected decision '%s' — defaulting to escalate", decision)
        decision = "escalate"

    logger.info("Decision: %s (confidence=%.2f) — %s", decision, confidence, reasoning)
    return {"decision": decision, "confidence": confidence, "reasoning": reasoning, "actions": []}


class AgentHandler:
    def __init__(self, request, client_address, server):
        from http.server import BaseHTTPRequestHandler
        # Handled below via standard library

    pass


from http.server import HTTPServer, BaseHTTPRequestHandler


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

    def log_message(self, fmt, *args):
        logger.debug(fmt, *args)


def main():
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    logger.info("Credit agent serving on port %d (model=%s, dep=%s)", PORT, AGENT_MODEL, GENAI_HUB_DEPLOYMENT_ID)
    server.serve_forever()


if __name__ == "__main__":
    main()
