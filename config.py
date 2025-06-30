"""
Configuration module for Meri Bot

This module centralizes all configuration settings and provides defaults
that can be overridden by environment variables.
"""

import os
from pathlib import Path

# Bot Configuration
BOT_PREFIX = os.getenv("PREFIX", "!")
BOT_TOKEN = os.getenv("TOKEN", "")

# API Endpoints (with defaults for local LM Studio/Ollama)
LMSTUDIO_COMPLETIONS_URL = os.getenv(
    "LMSTUDIO_COMPLETIONS_URL",
    "http://127.0.0.1:11434/v1/completions"
)
LMSTUDIO_CHAT_URL = os.getenv(
    "LMSTUDIO_CHAT_URL",
    "http://127.0.0.1:11434/v1/chat/completions"
)

# Model Configuration
LMSTUDIO_MAX_TOKENS = int(os.getenv("LMSTUDIO_MAX_TOKENS", "-1"))
MODEL_TTL_SECONDS = int(os.getenv("MODEL_TTL_SECONDS", "60"))

# Default Models
DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_CHAT_MODEL", "darkidol-llama-3.1-8b-instruct-1.2-uncensored@q2_k")
DEFAULT_VISION_MODEL = os.getenv("DEFAULT_VISION_MODEL", "qwen/qwen2.5-vl-7b")
DEFAULT_SEARCH_MODEL = os.getenv("DEFAULT_SEARCH_MODEL", "qwen/qwen3-4b")

# System Prompts
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful, accurate, and knowledgeable AI assistant. Provide clear, concise responses while being informative and engaging. Always maintain a professional yet approachable tone."
)

SYSTEM_PROMPT_QWEN = os.getenv("SYSTEM_PROMPT_QWEN", SYSTEM_PROMPT)
SYSTEM_PROMPT_DARK = os.getenv("SYSTEM_PROMPT_DARK", SYSTEM_PROMPT)

# Video Processing Configuration
MIN_VIDEO_FRAMES = int(os.getenv("MIN_VIDEO_FRAMES", "5"))
MAX_VIDEO_DURATION = int(os.getenv("MAX_VIDEO_DURATION", "600"))  # 10 minutes default
VIDEO_FRAME_INTERVAL = int(os.getenv("VIDEO_FRAME_INTERVAL", "30"))  # Extract frame every 30 seconds

# File Size Limits
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))  # Discord limit
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "50"))
MAX_TEXT_FILE_CHARS = int(os.getenv("MAX_TEXT_FILE_CHARS", "50000"))

# Search Configuration
MAX_SEARCH_RESULTS = int(os.getenv("MAX_SEARCH_RESULTS", "5"))
SEARCH_TIMEOUT_SECONDS = int(os.getenv("SEARCH_TIMEOUT_SECONDS", "10"))

# Memory Configuration
MAX_MEMORY_TURNS = int(os.getenv("MAX_MEMORY_TURNS", "5"))
MAX_CONTEXT_LENGTH = int(os.getenv("MAX_CONTEXT_LENGTH", "4000"))

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
STREAMING_LOG_FILE = os.getenv("STREAMING_LOG_FILE", "streaming.log")
REASON_LOG_FILE = os.getenv("REASON_LOG_FILE", "reason.log")

# Discord Configuration
MAX_MESSAGE_LENGTH = 2000  # Discord's message character limit
MAX_EMBED_FIELDS = 25  # Discord's embed field limit
DEFAULT_EMBED_COLOR = int(os.getenv("DEFAULT_EMBED_COLOR", "0x9b59b6"), 16)  # Purple

# Voice Configuration
VOICE_TIMEOUT_SECONDS = int(os.getenv("VOICE_TIMEOUT_SECONDS", "20"))
VOICE_RECONNECT_ATTEMPTS = int(os.getenv("VOICE_RECONNECT_ATTEMPTS", "5"))
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "0.5"))

# Rate Limiting
COMMANDS_PER_MINUTE = int(os.getenv("COMMANDS_PER_MINUTE", "30"))
SEARCH_COOLDOWN_SECONDS = int(os.getenv("SEARCH_COOLDOWN_SECONDS", "5"))

# Feature Flags
ENABLE_VOICE_COMMANDS = os.getenv("ENABLE_VOICE_COMMANDS", "true").lower() == "true"
ENABLE_VISION_ANALYSIS = os.getenv("ENABLE_VISION_ANALYSIS", "true").lower() == "true"
ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "true").lower() == "true"
ENABLE_DEBUG_COMMANDS = os.getenv("ENABLE_DEBUG_COMMANDS", "false").lower() == "true"

# Paths
PROJECT_ROOT = Path(__file__).parent
ENV_FILE = PROJECT_ROOT / "Meri_Token.env"
TEMP_DIR = PROJECT_ROOT / "temp"

# Ensure temp directory exists
TEMP_DIR.mkdir(exist_ok=True)

# Validation
def validate_config():
    """Validate critical configuration values"""
    errors = []
    
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is not set. Please set TOKEN in your .env file.")
    
    if LMSTUDIO_MAX_TOKENS < -1 or LMSTUDIO_MAX_TOKENS == 0:
        errors.append("LMSTUDIO_MAX_TOKENS must be -1 (unlimited) or a positive number.")
    
    if MODEL_TTL_SECONDS < 0:
        errors.append("MODEL_TTL_SECONDS must be a non-negative number.")
    
    if MIN_VIDEO_FRAMES < 1 or MIN_VIDEO_FRAMES > 20:
        errors.append("MIN_VIDEO_FRAMES should be between 1 and 20.")
    
    if DEFAULT_VOLUME < 0 or DEFAULT_VOLUME > 1:
        errors.append("DEFAULT_VOLUME must be between 0 and 1.")
    
    return errors

# Run validation on import
config_errors = validate_config()
if config_errors:
    print("Configuration errors detected:")
    for error in config_errors:
        print(f"  - {error}") 