"""LLM layer for Alpha — wraps the Gemini API."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from config import GEMINI_API_KEY
from google import genai
from google.genai import errors, types
# ========================================

load_dotenv()
_client = genai.Client(api_key=GEMINI_API_KEY)

logger = logging.getLogger(__name__)

MODEL = "gemini-3.5-flash"
BASE_DIR = Path(__file__).resolve().parent.parent

FALLBACK = (
    "I'm having trouble thinking clearly right now. "
    "Please try again in a moment, or ask an admin if it's urgent."
)


def _load(relative_path: str) -> str:
    return (BASE_DIR / relative_path).read_text(encoding="utf-8")


# Read once at import — these files don't change while the bot runs.
SYSTEM_INSTRUCTION = (
    _load("prompts/alpha_persona.md")
    + "\n\n---\n\n# ATL KNOWLEDGE\n\n"
    + _load("knowledge/atl_core.md")
)

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def ask_alpha(message: str) -> str:
    """Send a member's message to Alpha and return a reply."""
    try:
        response = await _client.aio.models.generate_content(
            model=MODEL,
            contents=message,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                max_output_tokens=1000,
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
        return response.text or FALLBACK
    except errors.APIError as e:
        logger.error("Gemini API error %s: %s", e.code, e.message)
        return FALLBACK
    except Exception:
        logger.exception("Unexpected error calling Gemini")
        return FALLBACK