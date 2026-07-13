"""
═══════════════════════════════════════════════════════════════════════════════
Julia - Medicare Allowance Card Eligibility Agent
═══════════════════════════════════════════════════════════════════════════════

A patient and polite voice assistant that guides senior citizen callers through
eligibility verification (name, ZIP code, age) and then transfers them to a
live agent. Built from prompt.txt.

Key patterns (from signalwire-demos/example and personal-assistant):
- AgentServer + AgentBase with SWML handler auto-registration on startup
- SWAIG tools via @self.tool() decorator
- ALL collected data persisted to global_data via result.update_global_data()
- Live-agent transfer via result.connect() (SWML connect verb)
- post_process=True on the transfer so Julia speaks the mandated goodbye line
  BEFORE the connect action executes

Usage:
    python app.py                    # Run locally
    gunicorn app:app ...             # Run in production (see Procfile)

Environment Variables (see .env.example):
    SIGNALWIRE_SPACE_NAME           # Required: Your SignalWire space
    SIGNALWIRE_PROJECT_ID           # Required: Your project ID
    SIGNALWIRE_TOKEN                # Required: Your API token
    TRANSFER_DESTINATION            # Required: live agent number/SIP to connect to
    SWML_PROXY_URL_BASE or APP_URL  # Auto-detected on Dokku/Heroku, set for local

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import re
import time
import logging
import threading
import warnings
from dotenv import load_dotenv
from fastapi.responses import JSONResponse

# Distribution is `signalwire-sdk`; the import name is `signalwire`.
from signalwire import AgentBase, AgentServer
from signalwire.core.function_result import SwaigFunctionResult
from signalwire.rest import RestClient

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Global State (SWML handler registration info, shared with /get_token)
# ─────────────────────────────────────────────────────────────────────────────
swml_handler_info = {
    "id": None,           # Handler resource ID
    "address_id": None,   # Address resource ID (used to scope tokens)
    "address": None       # The address clients dial to reach the agent
}

# Why registration hasn't happened yet (surfaced by /get_token)
swml_setup_error = None

# Guards the lazy setup retry from /get_token
swml_setup_lock = threading.Lock()

HOST = "0.0.0.0"
PORT = int(os.environ.get('PORT', 5000))

# Where transfer_to_agent connects the caller (phone number or SIP address)
TRANSFER_DESTINATION = os.environ.get("TRANSFER_DESTINATION", "")
# Optional caller ID override for the transfer leg
TRANSFER_CALLER_ID = os.environ.get("TRANSFER_CALLER_ID") or None


# ═══════════════════════════════════════════════════════════════════════════════
# SWML Handler Registration (same pattern as signalwire-demos/example)
# ═══════════════════════════════════════════════════════════════════════════════

def get_signalwire_host():
    """Get the full SignalWire API host from the space name."""
    space = os.getenv("SIGNALWIRE_SPACE_NAME", "")
    if not space:
        return None
    if "." in space:
        return space
    return f"{space}.signalwire.com"


def get_rest_client():
    """Build a SignalWire RestClient from environment configuration."""
    sw_host = get_signalwire_host()
    project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
    token = os.getenv("SIGNALWIRE_TOKEN", "")
    if not all([sw_host, project, token]):
        return None
    return RestClient(project=project, token=token, host=sw_host)


def find_resource_address(addresses, agent_name):
    """Find the resource address matching /public/{agent_name}."""
    expected_address = f"/public/{agent_name}"

    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel == expected_address:
            return addr

    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel.startswith("/public/") and not any(c.isdigit() for c in audio_channel.split("/")[-1][:3]):
            return addr

    return addresses[0] if addresses else None


def find_existing_handler(client, agent_name):
    """Find an existing SWML handler by name (avoids duplicates per deploy)."""
    try:
        handlers = client.fabric.swml_webhooks.list().get("data", [])

        for handler in handlers:
            swml_webhook = handler.get("swml_webhook", {})
            handler_name = swml_webhook.get("name") or handler.get("display_name")

            if handler_name == agent_name:
                handler_id = handler.get("id")
                handler_url = swml_webhook.get("primary_request_url", "")

                addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
                resource_addr = find_resource_address(addresses, agent_name)
                if resource_addr:
                    return {
                        "id": handler_id,
                        "name": handler_name,
                        "url": handler_url,
                        "address_id": resource_addr["id"],
                        "address": resource_addr["channels"]["audio"]
                    }
    except Exception as e:
        logger.error(f"Error finding existing handler: {e}")
    return None


def setup_swml_handler():
    """Register (or update) the SWML handler for this agent on startup."""
    global swml_setup_error

    client = get_rest_client()
    agent_name = os.getenv("AGENT_NAME", "julia")

    proxy_url = os.getenv("SWML_PROXY_URL_BASE", os.getenv("APP_URL", ""))
    auth_user = os.getenv("SWML_BASIC_AUTH_USER", "signalwire")
    auth_pass = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")

    if client is None:
        swml_setup_error = ("SIGNALWIRE_SPACE_NAME / SIGNALWIRE_PROJECT_ID / "
                            "SIGNALWIRE_TOKEN not set")
        logger.warning(f"{swml_setup_error} - skipping SWML handler setup")
        return

    if not proxy_url:
        swml_setup_error = ("SWML_PROXY_URL_BASE (or APP_URL) not set - it must be "
                            "the public URL SignalWire can fetch SWML from "
                            "(e.g. your ngrok URL)")
        logger.warning(f"{swml_setup_error} - skipping SWML handler setup")
        return

    # Build SWML URL with basic auth credentials embedded
    if auth_user and auth_pass and "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        swml_url = f"{scheme}://{auth_user}:{auth_pass}@{rest}/{agent_name}"
    else:
        swml_url = f"{proxy_url}/{agent_name}"

    existing = find_existing_handler(client, agent_name)

    if existing:
        swml_handler_info["id"] = existing["id"]
        swml_handler_info["address_id"] = existing["address_id"]
        swml_handler_info["address"] = existing["address"]
        swml_setup_error = None

        try:
            client.fabric.swml_webhooks.update(
                existing["id"],
                primary_request_url=swml_url,
                primary_request_method="POST"
            )
            logger.info(f"Updated SWML handler: {existing['name']}")
        except Exception as e:
            logger.error(f"Failed to update handler URL: {e}")

        logger.info(f"Call address: {existing['address']}")
    else:
        try:
            # A standalone dialable handler (not bound to a phone number) is
            # intentional, so silence the SDK's phone-number-setup warning
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                handler_resp = client.fabric.swml_webhooks.create(
                    name=agent_name,
                    used_for="calling",
                    primary_request_url=swml_url,
                    primary_request_method="POST"
                )
            handler_id = handler_resp.get("id")
            swml_handler_info["id"] = handler_id

            addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
            resource_addr = find_resource_address(addresses, agent_name)
            if resource_addr:
                swml_handler_info["address_id"] = resource_addr["id"]
                swml_handler_info["address"] = resource_addr["channels"]["audio"]
                swml_setup_error = None
            else:
                swml_setup_error = f"handler '{agent_name}' created but no dialable address found"

            logger.info(f"Created SWML handler '{agent_name}' with address: {swml_handler_info.get('address')}")
        except Exception as e:
            logger.error(f"Failed to create SWML handler: {e}")
            # Retry finding existing handler (another worker may have just created it)
            time.sleep(0.5)
            existing = find_existing_handler(client, agent_name)
            if existing:
                swml_handler_info["id"] = existing["id"]
                swml_handler_info["address_id"] = existing["address_id"]
                swml_handler_info["address"] = existing["address"]
                swml_setup_error = None
                logger.info(f"Found existing SWML handler after retry: {existing['name']}")
                logger.info(f"Call address: {existing['address']}")
            else:
                swml_setup_error = f"failed to create handler '{agent_name}': {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Definition
# ═══════════════════════════════════════════════════════════════════════════════

class JuliaAgent(AgentBase):
    """
    Julia: eligibility verification and data collection for senior citizen
    callers, then transfer to a live agent.

    Every piece of collected data (name, inquiry type, ZIP, age, transfer
    status) is persisted to global_data.
    """

    def __init__(self):
        super().__init__(
            name="Julia",
            route="/julia"  # SWML endpoint path (matches AGENT_NAME default)
        )

        self._setup_prompts()
        self._setup_functions()

    def _setup_prompts(self):
        """Configure Julia's personality and conversation flow (from prompt.txt)."""

        self.prompt_add_section(
            "Identity",
            "You are Julia, a patient and polite voice assistant tasked with "
            "guiding senior citizen callers through eligibility verification "
            "and data collection before connecting them to a live agent."
        )

        self.prompt_add_section(
            "Style",
            bullets=[
                "Be polite, warm, and friendly throughout the entire interaction.",
                "Speak clearly and patiently, considering the needs of senior citizens.",
                "If asked about the allowance card benefit, tell them we only need "
                "to know their age and zipcode, then we'll connect them with a live "
                "agent who can answer any questions.",
            ]
        )

        self.prompt_add_section(
            "Response Guidelines",
            bullets=[
                "Do not verify answers unless they seem incomplete, unclear, or "
                "affected by background noise.",
                "Address background noise by politely asking the caller to move to "
                "a quieter location.",
                "Do not repeat the caller's name back to them, since we do not need "
                "it to be accurate or correct, and we don't want to spend time "
                "correcting it if it was heard or recorded incorrectly.",
                "Never mention tools, functions, or systems to the caller.",
            ]
        )

        self.prompt_add_section(
            "Task and Goals",
            "Follow these steps in order:",
            bullets=[
                "Greet the caller warmly and ask for their name. When they give it, "
                "call save_caller_name to store it. Do not repeat the name back to them.",
                "Say: \"Nice to meet you, and thank you for calling today. Are you "
                "calling about the allowance card available with Medicare?\"",
                "Call save_inquiry_type with their answer. If the answer is yes or "
                "positive, proceed with the call. If the answer is no or negative, "
                "quickly explain that this is a hotline to get qualified for the "
                "Part B giveback benefit provided by Medicare, then continue.",
                "Ask for the caller's zip code and wait for their response, then "
                "call save_zip_code to store it. The zip code must contain 5 digits; "
                "otherwise ask for repetition. The caller may give the number as "
                "two-digit or three-digit groups (for example \"two-thirty, "
                "forty-seven\" when they mean 23047), so attempt to interpret the "
                "response that way before asking for confirmation. Do not explain to "
                "the caller how they may say the zipcode; just interpret their "
                "response. If the caller includes the word \"dash\" or gives more "
                "than 5 digits, let them know that we only need their 5 digit "
                "zipcode. If the caller says \"Oh\" or \"O\" as part of the "
                "response, interpret that as a zero. Re-ask the question if needed.",
                "Say: \"Thank you. Can I get your current age?\"",
                "Wait for the caller's age and call save_age to store it. Ensure the "
                "age recorded is a two-digit number. If the answer is unclear or "
                "ambiguous, ask for clarification.",
                "Once the ZIP code and age are both saved, immediately call "
                "transfer_to_agent. The system will speak the goodbye message and "
                "transfer the call automatically - do NOT say any goodbye or "
                "transfer message yourself.",
            ],
            numbered_bullets=True
        )

        self.prompt_add_section(
            "Error Handling and Fallback",
            bullets=[
                "If a response is unclear or garbled, gently inform the caller of "
                "the difficulty in understanding and ask them to repeat their answer.",
                "If affected by background noise, apologize for the inconvenience "
                "and ask the caller to find a quieter space to continue.",
                "If a save tool reports the data is invalid, politely re-ask the "
                "question.",
            ]
        )

        self.prompt_add_section(
            "Mandatory Final Order",
            "This final order is mandatory:",
            bullets=[
                "Collect ZIP code and age.",
                "Call transfer_to_agent. The system speaks the goodbye message and "
                "transfers the call - never speak a goodbye or transfer message "
                "yourself.",
            ],
            numbered_bullets=True
        )

        # Voice configuration (one-time agent state, so it belongs in __init__,
        # NOT in on_swml_request - add_language/add_hints append per call)
        self.add_language(
            name="English",
            code="en-US",
            voice="elevenlabs.rachel"
        )

        # Speech hints for better recognition of domain-specific terms
        self.add_hints([
            "Medicare",
            "allowance card",
            "Part B",
            "giveback",
            "zip code",
            "eligibility"
        ])

        # Seed global_data so every field we collect has a known home
        self.set_global_data({
            "agent": "julia",
            "caller_name": None,
            "about_allowance_card": None,
            "caller_zip": None,
            "caller_age": None,
            "data_complete": False,
            "transfer_status": "pending"
        })

        # Post-prompt for conversation summaries (sent to webhook if configured)
        post_prompt_url = os.environ.get("POST_PROMPT_URL")
        if post_prompt_url:
            self.set_post_prompt(
                "Summarize this call in JSON with the caller's name, whether they "
                "were calling about the allowance card, their zip code, their age, "
                "and whether the transfer to a live agent completed."
            )
            self.set_post_prompt_url(post_prompt_url)

    def _setup_functions(self):
        """Register SWAIG tools. Every tool persists its data to global_data."""

        # ─────────────────────────────────────────────────────────────────────
        # save_caller_name
        # ─────────────────────────────────────────────────────────────────────
        @self.tool(
            name="save_caller_name",
            description="Store the caller's name as soon as they say it. Call this "
                        "once at the start of the call. The name does not need to "
                        "be spelled or verified - store whatever was heard.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The caller's name exactly as heard"
                    }
                },
                "required": ["name"]
            }
        )
        def save_caller_name(args, raw_data):
            name = (args.get("name") or "").strip()

            global_data = raw_data.get("global_data", {}) or {}
            global_data["caller_name"] = name
            global_data["caller_name_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            result = SwaigFunctionResult(
                "Name stored. Do not repeat the name back to the caller. "
                "Continue to the next step."
            )
            result.update_global_data(global_data)
            return result

        # ─────────────────────────────────────────────────────────────────────
        # save_inquiry_type
        # ─────────────────────────────────────────────────────────────────────
        @self.tool(
            name="save_inquiry_type",
            description="Store whether the caller is calling about the Medicare "
                        "allowance card. Call this right after asking if they are "
                        "calling about the allowance card available with Medicare.",
            parameters={
                "type": "object",
                "properties": {
                    "about_allowance_card": {
                        "type": "boolean",
                        "description": "true if the caller answered yes or positively, "
                                       "false if no or negative"
                    }
                },
                "required": ["about_allowance_card"]
            }
        )
        def save_inquiry_type(args, raw_data):
            about_card = bool(args.get("about_allowance_card"))

            global_data = raw_data.get("global_data", {}) or {}
            global_data["about_allowance_card"] = about_card
            global_data["inquiry_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            if about_card:
                response = "Stored. The caller is calling about the allowance card. Proceed with the call."
            else:
                response = ("Stored. Quickly explain that this is a hotline to get "
                            "qualified for the Part B giveback benefit provided by "
                            "Medicare, then continue to the zip code step.")

            result = SwaigFunctionResult(response)
            result.update_global_data(global_data)
            return result

        # ─────────────────────────────────────────────────────────────────────
        # save_zip_code (validates 5 digits server-side)
        # ─────────────────────────────────────────────────────────────────────
        @self.tool(
            name="save_zip_code",
            description="Store the caller's 5-digit zip code after interpreting "
                        "their spoken response (grouped digits like 'two-thirty "
                        "forty-seven' mean 23047; 'oh' means zero). Call this as "
                        "soon as you have interpreted a 5-digit zip code.",
            parameters={
                "type": "object",
                "properties": {
                    "zip_code": {
                        "type": "string",
                        "description": "The interpreted zip code as exactly 5 digits, "
                                       "e.g. '23047'"
                    }
                },
                "required": ["zip_code"]
            }
        )
        def save_zip_code(args, raw_data):
            raw_zip = str(args.get("zip_code") or "")
            digits = re.sub(r"\D", "", raw_zip)

            if len(digits) != 5:
                return SwaigFunctionResult(
                    f"Invalid zip code '{raw_zip}': it must be exactly 5 digits. "
                    "Politely ask the caller to repeat their 5 digit zipcode, "
                    "then call this tool again."
                )

            global_data = raw_data.get("global_data", {}) or {}
            global_data["caller_zip"] = digits
            global_data["zip_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if global_data.get("caller_age"):
                global_data["data_complete"] = True

            result = SwaigFunctionResult(
                "Zip code stored. Now say: \"Thank you. Can I get your current age?\""
            )
            result.update_global_data(global_data)
            return result

        # ─────────────────────────────────────────────────────────────────────
        # save_age (validates two-digit number server-side)
        # ─────────────────────────────────────────────────────────────────────
        @self.tool(
            name="save_age",
            description="Store the caller's current age. Call this as soon as the "
                        "caller states their age clearly. The age must be a "
                        "two-digit number.",
            parameters={
                "type": "object",
                "properties": {
                    "age": {
                        "type": "integer",
                        "description": "The caller's age as a two-digit number, e.g. 67"
                    }
                },
                "required": ["age"]
            }
        )
        def save_age(args, raw_data):
            try:
                age = int(args.get("age"))
            except (TypeError, ValueError):
                age = -1

            if not (10 <= age <= 99):
                return SwaigFunctionResult(
                    f"Invalid age '{args.get('age')}': it must be a two-digit "
                    "number. Gently ask the caller to clarify their age, then "
                    "call this tool again."
                )

            global_data = raw_data.get("global_data", {}) or {}
            global_data["caller_age"] = age
            global_data["age_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if global_data.get("caller_zip"):
                global_data["data_complete"] = True

            result = SwaigFunctionResult(
                "Age stored. All required data is collected. Immediately call "
                "transfer_to_agent now."
            )
            result.update_global_data(global_data)
            return result

        # ─────────────────────────────────────────────────────────────────────
        # transfer_to_agent (pattern from personal-assistant's transfer_to_owner)
        # ─────────────────────────────────────────────────────────────────────
        @self.tool(
            name="transfer_to_agent",
            description="Transfer the caller to a live agent. Only call this AFTER "
                        "the zip code and age have both been saved successfully.",
            parameters={
                "type": "object",
                "properties": {},
                "required": []
            },
            fillers={
                "en-US": [
                    "One moment please...",
                    "Just a moment...",
                ]
            }
        )
        def transfer_to_agent(args, raw_data):
            global_data = raw_data.get("global_data", {}) or {}

            # Guard: verify all collected details before transferring.
            # ZIP and age are the mandatory final-order items.
            missing = []
            if not global_data.get("caller_name"):
                missing.append("name")
            if not global_data.get("caller_zip"):
                missing.append("zip code")
            if not global_data.get("caller_age"):
                missing.append("age")
            if missing:
                return SwaigFunctionResult(
                    f"Cannot transfer yet: the caller's {' and '.join(missing)} "
                    "must be collected and saved first. Politely ask for the "
                    "missing information."
                )

            if not TRANSFER_DESTINATION:
                logger.error("TRANSFER_DESTINATION not configured - cannot transfer")
                global_data["transfer_status"] = "failed_no_destination"
                result = SwaigFunctionResult(
                    "Transfer is not available right now. Apologize to the caller "
                    "and let them know a live agent will call them back shortly."
                )
                result.update_global_data(global_data)
                return result

            global_data["transfer_status"] = "transferred"
            global_data["transferred_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            global_data["data_complete"] = True

            # Actions execute in order: save data, speak the mandated goodbye
            # line verbatim (deterministic - not left to the LLM), then connect.
            result = SwaigFunctionResult("Transferring the caller now.")
            result.update_global_data(global_data)
            result.say("Transferring you now. Please hold the line, and have a great day.")
            # final=True: permanent transfer - the call exits the agent
            result.connect(TRANSFER_DESTINATION, final=True, from_addr=TRANSFER_CALLER_ID)
            return result


# ═══════════════════════════════════════════════════════════════════════════════
# Server Creation
# ═══════════════════════════════════════════════════════════════════════════════

def create_server(port=None):
    """Create the AgentServer with the Julia agent and support endpoints."""
    server = AgentServer(host=HOST, port=port or PORT)

    agent = JuliaAgent()
    server.register(agent, "/julia")

    @server.app.get("/health")
    def health_check():
        """Health check endpoint for deployment verification."""
        return {"status": "healthy", "agent": "julia"}

    @server.app.get("/ready")
    def ready_check():
        """Readiness check - verifies SWML handler is configured."""
        if swml_handler_info.get("address"):
            return {"status": "ready", "address": swml_handler_info["address"]}
        return {"status": "initializing"}

    @server.app.get("/get_token")
    def get_token():
        """Generate a scoped guest token so a WebRTC client can call Julia."""
        client = get_rest_client()

        if client is None:
            return JSONResponse(status_code=500, content={"error": "SignalWire credentials not configured (SIGNALWIRE_SPACE_NAME / SIGNALWIRE_PROJECT_ID / SIGNALWIRE_TOKEN)"})

        # Registration happens at startup, but retry lazily here so a
        # transient failure heals itself
        if not swml_handler_info.get("address_id"):
            with swml_setup_lock:
                if not swml_handler_info.get("address_id"):
                    setup_swml_handler()

        if not swml_handler_info.get("address_id"):
            reason = swml_setup_error or "unknown error - check server logs"
            return JSONResponse(status_code=500, content={"error": f"SWML handler not registered: {reason}"})

        try:
            expire_at = int(time.time()) + 3600 * 24  # 24 hours

            guest = client.fabric.tokens.create_guest_token(
                allowed_addresses=[swml_handler_info["address_id"]],
                expire_at=expire_at
            )
            guest_token = guest.get("token", "")

            return {
                "token": guest_token,
                "address": swml_handler_info["address"]
            }
        except Exception as e:
            logger.error(f"Token request failed: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

    @server.app.on_event("startup")
    async def on_startup():
        """Register SWML handler on application startup."""
        setup_swml_handler()

    return server


# ═══════════════════════════════════════════════════════════════════════════════
# Module-Level Exports (required for gunicorn: `gunicorn app:app`)
# ═══════════════════════════════════════════════════════════════════════════════

server = create_server()
app = server.app


if __name__ == "__main__":
    server.run()
