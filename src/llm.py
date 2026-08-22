"""LLM layer for Alpha — wraps the Gemini API."""

import logging
from pathlib import Path

from config import GEMINI_API_KEY
from google import genai
from google.genai import errors, types
# ========================================
logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = "gemini-3.5-flash"
BASE_DIR = Path(__file__).resolve().parent.parent
FALLBACK = (
  "I'm having trouble thinking clearly right now. "
  "Please try again in a moment, or ask an admin if it's urgent."
)
# =====================================================================
def _load(relative_path: str) -> str:
  return (BASE_DIR / relative_path).read_text(encoding="utf-8")

# Read once at import — these files don't change while the bot runs.
SYSTEM_INSTRUCTION = (
  _load("resources/prompts/alpha_persona.md")
  + "\n\n---\n\n# ATL KNOWLEDGE\n\n"
  + _load("resources/knowledge/atl_core.md")
)

CLASSIFY_INSTRUCTION = """You classify one message posted in the Alpha Training Lab induction group. The person has tagged the bot.

Reply with exactly one word:

onboarding — they are signalling they have finished reading the induction material and want to move to the next phase, or are asking to be approved, onboarded or registered.
question — they are asking for information, help, a link, or anything else.

One word only. No punctuation, no explanation."""


# ----- Classification -----------------------------------------------------
async def ask_alpha(message: str, context_note: str | None = None) -> str:
  """Send a member's message to Alpha and return a reply."""
  system = SYSTEM_INSTRUCTION
  if context_note:
    system += (
      "\n\n---\n\n# CURRENT MEMBER CONTEXT\n\n"
      "The following is true of the member you are talking to right now. "
      "Use it to give direct, specific instructions. Do not read it out "
      "verbatim.\n\n" + context_note
    )

  try:
    response = await _client.aio.models.generate_content(
      model=MODEL,
      contents=message,
      config=types.GenerateContentConfig(
        system_instruction=system,
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
  

# ----- Classification -----------------------------------------------------
async def classify_induction_intent(message: str) -> str:
  """Return 'onboarding' or 'question'. Defaults to 'onboarding' on error."""
  try:
    response = await _client.aio.models.generate_content(
      model=MODEL,
      contents=message,
      config=types.GenerateContentConfig(
        system_instruction=CLASSIFY_INSTRUCTION,
        max_output_tokens=20,
        thinking_config=types.ThinkingConfig(thinking_level="low"),
      ),
    )
    return "question" if "question" in (response.text or "").lower() \
           else "onboarding"
  except Exception:
    logger.exception("Intent classification failed")
    return "onboarding"