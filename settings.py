"""LLM provider settings, read from environment / .env.

Any OpenAI-compatible endpoint works:
  OpenAI      LLM_BASE_URL=https://api.openai.com/v1
  Ollama      LLM_BASE_URL=http://localhost:11434/v1   (LLM_API_KEY=ollama)
  LiteLLM     LLM_BASE_URL=http://localhost:4000/v1
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
# Keys never contain whitespace; drop any stray spaces/line breaks from copy-paste.
LLM_API_KEY = "".join(os.environ.get("LLM_API_KEY", "").split())
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip() or "gpt-4o-mini"
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "180"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))

# Hosting / public-site protection (all optional).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()        # if set, the site asks for it
MAX_CVS_PER_DAY = int(os.environ.get("MAX_CVS_PER_DAY", "0"))      # 0 = unlimited
RUN_RETENTION_HOURS = float(os.environ.get("RUN_RETENTION_HOURS", "0"))  # 0 = keep runs forever


def missing():
    """Return a list of required settings that are not configured."""
    out = []
    if not LLM_BASE_URL:
        out.append("LLM_BASE_URL")
    if not LLM_API_KEY:
        out.append("LLM_API_KEY")
    return out


def config_error():
    m = missing()
    if not m:
        return None
    return (
        "LLM not configured: missing " + ", ".join(m) + ". "
        "Copy .env.example to .env and set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
        "(any OpenAI-compatible endpoint, e.g. OpenAI or local Ollama at http://localhost:11434/v1)."
    )
