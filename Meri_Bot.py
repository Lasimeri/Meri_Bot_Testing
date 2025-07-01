"""Meri Discord Bot

Core features:
• AI chat with web search and vision analysis
• YouTube audio streaming and video frame extraction
• Content summarization from URLs
• Memory-aware conversations
• Multimodal support (images, videos, text)

The code favors readability and robust error handling.
"""

import os
import sys
import logging
from typing import List, cast, Any, Optional, Dict
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands
import requests
import aiohttp
import json
import asyncio
from urllib.parse import quote_plus
import re
from html import unescape
import base64
import io
import tempfile
import subprocess
from pathlib import Path
try:
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover
    yt_dlp = None  # Will handle at runtime

# Try to import PIL for image processing
try:
    from PIL import Image  # type: ignore
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    _PIL_AVAILABLE = False

# Third-party web search helper (requires `pip install duckduckgo-search`)
try:
    from duckduckgo_search import ddg  # type: ignore
except ImportError:  # pragma: no cover
    ddg = None  # Will handle missing dependency at runtime

# Try to load youtube_transcript_api for transcripts
try:
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
except ImportError:  # pragma: no cover
    YouTubeTranscriptApi = None

# ─── Unified DuckDuckGo Search Helper ───────────────────────────────────────

def _ddg_search(query: str, max_results: int = 5) -> List[dict]:
    """Return DuckDuckGo search results using whichever API variant is available.

    Tries the legacy `ddg` function first; if unavailable, falls back to the
    newer `DDGS` class interface from `duckduckgo_search≥4`.
    Returns an empty list on any error.
    """
    # Sanitize and limit query length to prevent API failures
    if not query or not query.strip():
        logger.warning("Empty search query provided")
        return []
    
    # Limit query length to prevent API rejections (DuckDuckGo has practical limits)
    original_query = query.strip()
    if len(original_query) > 500:  # Reasonable limit for search queries
        query = original_query[:500] + "..."
        logger.info(f"Truncated long search query from {len(original_query)} to {len(query)} characters")
    else:
        query = original_query
    
    # Remove potentially problematic characters that might cause API issues
    query = re.sub(r'[^\w\s\-\.\,\?\!\:]', ' ', query)  # Keep only basic punctuation
    query = re.sub(r'\s+', ' ', query).strip()  # Normalize whitespace
    
    if not query:
        logger.warning("Query became empty after sanitization")
        return []
    
    logger.info(f"Searching DuckDuckGo for: {query[:100]}{'...' if len(query) > 100 else ''}")
    
    # Method 1: Try newer DDGS interface first (more reliable)
    try:
        from duckduckgo_search import DDGS  # type: ignore
        ddgs_results: List[dict] = []
        
        # Use different initialization methods for better compatibility
        try:
            # Try with default settings first
            with DDGS() as ddgs:
                search_results = ddgs.text(query, max_results=max_results)
                for r in search_results:
                    ddgs_results.append(r)
                    if len(ddgs_results) >= max_results:
                        break
        except Exception as ddgs_error:
            logger.warning(f"DDGS default initialization failed: {ddgs_error}")
            # Try with different settings
            try:
                with DDGS(proxy=None, timeout=20) as ddgs:
                    search_results = ddgs.text(query, max_results=max_results, region='wt-wt', safesearch='off')
                    for r in search_results:
                        ddgs_results.append(r)
                        if len(ddgs_results) >= max_results:
                            break
            except Exception as ddgs_alt_error:
                logger.warning(f"DDGS alternative initialization failed: {ddgs_alt_error}")
                raise ddgs_alt_error
        
        if ddgs_results:
            logger.info(f"DuckDuckGo DDGS API returned {len(ddgs_results)} results")
            return ddgs_results
        else:
            logger.warning(f"DuckDuckGo DDGS API returned no results for query")
            
    except ImportError:
        logger.warning("DDGS not available, trying legacy ddg interface")
    except Exception as e:
        logger.warning(f"DuckDuckGo DDGS API failed: {e}")
    
    # Method 2: Legacy simple function interface
    if ddg is not None:
        try:
            data = ddg(query, max_results=max_results)
            if data:
                logger.info(f"DuckDuckGo legacy API returned {len(data)} results")
                return cast(List[dict], data)
        except Exception as e:
            logger.warning(f"DuckDuckGo legacy API failed: {e}")
    
    # Method 3: Try simplified query if original fails
    if len(query) > 100:
        try:
            simplified_query = ' '.join(query.split()[:10])  # First 10 words only
            logger.info(f"Trying simplified query: {simplified_query}")
            
            from duckduckgo_search import DDGS  # type: ignore
            simplified_results: List[dict] = []
            with DDGS() as ddgs:
                search_results = ddgs.text(simplified_query, max_results=max_results)
                for r in search_results:
                    simplified_results.append(r)
                    if len(simplified_results) >= max_results:
                        break
            
            if simplified_results:
                logger.info(f"DuckDuckGo simplified query returned {len(simplified_results)} results")
                return simplified_results
                
        except Exception as e:
            logger.warning(f"Simplified query search failed: {e}")
    
    # If all methods failed, log and return empty list
    logger.error(f"All DuckDuckGo search methods failed for query '{query[:50]}...'")
    return []

# ─── Twitter/X Redirect URL Resolution ──────────────────────────────────────
async def _resolve_twitter_redirect(url: str) -> str:
    """Resolve Twitter/X redirect URLs to actual Twitter/X URLs.
    
    Handles redirect services like fxtwitter.com, vxtwitter.com, fixupx.com, etc.
    Returns the resolved Twitter/X URL or the original URL if not a redirect.
    """
    # Common Twitter/X redirect services with their conversion patterns
    redirect_conversions = [
        (r"https?://(fx|vx|fix)twitter\.com/", "https://twitter.com/"),
        (r"https?://fixupx\.com/", "https://twitter.com/"),
        (r"https?://nitter\.(net|it|namazso\.eu|privacydev\.net)/", "https://twitter.com/"),
        (r"https?://twstalker\.com/", "https://twitter.com/"),
        (r"https?://twittervideodownloader\.com/", "https://twitter.com/"),
    ]
    
    # Check if URL matches any redirect pattern and convert directly
    for pattern, replacement in redirect_conversions:
        if re.search(pattern, url, re.IGNORECASE):
            converted_url = re.sub(pattern, replacement, url, flags=re.IGNORECASE)
            if converted_url != url:
                logger.info(f"Converted Twitter/X redirect: {url} -> {converted_url}")
                
                # Validate the converted URL has a proper Twitter status format
                if re.search(r"(twitter\.com|x\.com)/[^/]+/status/\d+", converted_url):
                    return converted_url
                else:
                    # If conversion didn't result in proper Twitter URL, try HTTP resolution as fallback
                    logger.info(f"Direct conversion failed, trying HTTP resolution for: {url}")
                    break
    
    # If no direct conversion worked, try HTTP resolution as fallback
    try:
        logger.info(f"Attempting HTTP resolution for redirect: {url}")
        
        # Follow redirects to get final URL
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as resp:
                final_url = str(resp.url)
                
                # Check if final URL is a Twitter/X URL
                if re.search(r"(twitter\.com|x\.com)/[^/]+/status/\d+", final_url):
                    logger.info(f"HTTP resolution successful: {url} -> {final_url}")
                    return final_url
                
                # If redirect didn't lead to Twitter/X, try extracting from page content
                if resp.status == 200:
                    html = await resp.text()
                    
                    # Look for Twitter/X URLs in meta tags or links
                    twitter_patterns = [
                        r'<meta[^>]*property="og:url"[^>]*content="([^"]*(?:twitter\.com|x\.com)[^"]*)"',
                        r'<link[^>]*rel="canonical"[^>]*href="([^"]*(?:twitter\.com|x\.com)[^"]*)"',
                        r'href="(https?://(?:twitter\.com|x\.com)/[^/]+/status/\d+)"',
                        r'content="(https?://(?:twitter\.com|x\.com)/[^/]+/status/\d+)"'
                    ]
                    
                    for pattern in twitter_patterns:
                        match = re.search(pattern, html, re.IGNORECASE)
                        if match:
                            resolved_url = match.group(1)
                            logger.info(f"Extracted Twitter/X URL from page content: {resolved_url}")
                            return resolved_url
                
                logger.warning(f"Could not resolve Twitter/X redirect for: {url}")
                return url  # Return original if resolution fails
                
    except Exception as e:
        logger.error(f"Failed to resolve Twitter/X redirect {url}: {e}")
        return url  # Return original URL on error

# ─── Load Environment Configuration ─────────────────────────────────────────
# Load bot settings from .env file
env_path = os.path.join(os.path.dirname(__file__), "Meri_Token.env")
load_dotenv(env_path)

# Discord bot configuration
TOKEN = os.getenv("TOKEN") or ""  # Discord bot token (required)
PREFIX = os.getenv("PREFIX", "!")  # Command prefix (default: !)

# LM Studio/Ollama API endpoints
LMSTUDIO_COMPLETIONS_URL = os.getenv(
    "LMSTUDIO_COMPLETIONS_URL",
    "http://127.0.0.1:11434/v1/completions"  # Fallback to Ollama default
)
LMSTUDIO_CHAT_URL = os.getenv(
    "LMSTUDIO_CHAT_URL",
    "http://127.0.0.1:11434/v1/chat/completions"  # Fallback to Ollama default
)
LMSTUDIO_MAX_TOKENS = int(os.getenv("LMSTUDIO_MAX_TOKENS", "-1"))  # -1 = unlimited

# AI system prompts loaded from environment (with fallbacks)
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful, accurate, and knowledgeable AI assistant. Provide clear, concise responses while being informative and engaging. Always maintain a professional yet approachable tone.",
)

SYSTEM_PROMPT_QWEN = os.getenv("SYSTEM_PROMPT_QWEN", SYSTEM_PROMPT)  # Specialized prompt for Qwen model

# Model TTL (Time To Live) configuration - how long models stay loaded when idle
MODEL_TTL_SECONDS = int(os.getenv("MODEL_TTL_SECONDS", "60"))  # Default: 1 minute

# Video frame extraction configuration for optimal AI analysis
MIN_VIDEO_FRAMES = int(os.getenv("MIN_VIDEO_FRAMES", "5"))  # Minimum frames to extract for video analysis

# ─── Logging Configuration ──────────────────────────────────────────────────
# Clear any existing root handlers to avoid conflicts
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

# Basic logging setup (errors to file)
logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    filename="bot.log",
    filemode="a"
)

# Main bot logger - logs everything to file, only errors to console
logger = logging.getLogger("Meri")
logger.setLevel(logging.INFO)  # Capture all info+ messages

# Console handler - show INFO and above in terminal for full debugging
console = logging.StreamHandler()
console.setLevel(logging.INFO)  # Changed from ERROR to INFO for full logging
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s: %(message)s"))
logger.addHandler(console)

# File handler - detailed logging to bot.log
file_handler = logging.FileHandler("bot.log", mode="a")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s: %(message)s"))
logger.addHandler(file_handler)
logger.propagate = False  # Prevent double logging to root handlers

# Set up logger for AI streaming events (helps with debugging)
streaming_logger = logging.getLogger("MeriStreaming")
streaming_logger.setLevel(logging.INFO)
stream_handler = logging.FileHandler("streaming.log", mode="a")
stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s: %(message)s"))
streaming_logger.addHandler(stream_handler)

# Set up dedicated reason logger for full RAG debugging
reason_logger = logging.getLogger("MeriReason")
reason_logger.setLevel(logging.DEBUG)  # Capture all debug+ messages

# Console output for reason logger (full detail)
reason_console = logging.StreamHandler()
reason_console.setLevel(logging.DEBUG)
reason_console.setFormatter(logging.Formatter("%(asctime)s [RAG-DEBUG] %(levelname)s: %(message)s"))
reason_logger.addHandler(reason_console)

# File output for reason logger
reason_file = logging.FileHandler("reason.log", mode="a")
reason_file.setLevel(logging.DEBUG)
reason_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s: %(message)s"))
reason_logger.addHandler(reason_file)
reason_logger.propagate = False  # Prevent duplicate messages

# ─── Logging Utilities ──────────────────────────────────────────────────────

def _sanitize_for_log(text: str) -> str:
    """Sanitize text for safe logging on Windows by replacing problematic Unicode characters."""
    if not text:
        return ""
    
    # Convert to string if not already (handle edge cases)
    try:
        text = str(text)
    except Exception:
        return "[UNABLE TO CONVERT TO STRING]"
    
    # Replace common emojis and Unicode characters with ASCII alternatives
    emoji_replacements = {
        '🚨': '[ALERT]',
        '⚠️': '[WARNING]', 
        '✅': '[SUCCESS]',
        '❌': '[ERROR]',
        '🔄': '[PROCESSING]',
        '📄': '[DOCUMENT]',
        '🔗': '[LINK]',
        '🎬': '[VIDEO]',
        '🎵': '[MUSIC]',
        '📝': '[TEXT]',
        '💡': '[TIP]',
        '🤖': '[BOT]',
        '👤': '[USER]',
        '🌐': '[WEB]',
        '📊': '[DATA]',
        '🔍': '[SEARCH]',
        '📱': '[MOBILE]',
        '💬': '[CHAT]',
        '🎯': '[TARGET]',
        '📈': '[TRENDING]',
        '🚀': '[LAUNCH]',
        '⭐': '[STAR]',
        '🔥': '[FIRE]',
        '💯': '[100]',
        '👍': '[THUMBS_UP]',
        '👎': '[THUMBS_DOWN]',
        '❤️': '[HEART]',
        '💔': '[BROKEN_HEART]',
        '😂': '[LAUGHING]',
        '😭': '[CRYING]',
        '😍': '[LOVE_EYES]',
        '🤔': '[THINKING]',
        '😱': '[SHOCKED]',
        '🙄': '[EYE_ROLL]',
        '😴': '[SLEEPING]',
        '🤯': '[MIND_BLOWN]',
        '🎉': '[PARTY]',
        '🔔': '[BELL]',
        '📢': '[ANNOUNCEMENT]',
        '🎭': '[THEATER]',
        '🎪': '[CIRCUS]',
        '🏆': '[TROPHY]',
        '🎖️': '[MEDAL]',
        '🏅': '[GOLD_MEDAL]',
        '🎨': '[ART]',
        '📚': '[BOOKS]',
        '💰': '[MONEY]',
        '💸': '[MONEY_FLYING]',
        '📺': '[TV]',
        '📻': '[RADIO]',
        '☀️': '[SUN]',
        '🌙': '[MOON]',
        '🌟': '[SPARKLE]',
        '💫': '[DIZZY]',
        '🌈': '[RAINBOW]',
        '🔴': '[RED_CIRCLE]',
        '🟠': '[ORANGE_CIRCLE]',
        '🟡': '[YELLOW_CIRCLE]',
        '🟢': '[GREEN_CIRCLE]',
        '🔵': '[BLUE_CIRCLE]',
        '🟣': '[PURPLE_CIRCLE]',
        '⚫': '[BLACK_CIRCLE]',
        '⚪': '[WHITE_CIRCLE]',
        # Add more problematic characters
        ''': "'",  # Curly apostrophe
        ''': "'",  # Curly apostrophe
        '"': '"',  # Curly quote
        '"': '"',  # Curly quote
        '–': '-',  # En dash
        '—': '--', # Em dash
        '…': '...',  # Ellipsis
        '‚': ',',   # Single low quote
        '„': '"',   # Double low quote
        '‹': '<',   # Single left guillemet
        '›': '>',   # Single right guillemet
        '«': '<<',  # Left guillemet
        '»': '>>',  # Right guillemet
    }
    
    # Replace emojis and special characters with text alternatives
    sanitized = text
    try:
        for emoji, replacement in emoji_replacements.items():
            sanitized = sanitized.replace(emoji, replacement)
    except Exception:
        # If replacement fails, continue with basic sanitization
        pass
    
    # Remove any remaining high Unicode characters that might cause issues
    # Keep basic Latin, Latin-1 Supplement, and common punctuation
    try:
        sanitized = ''.join(char if ord(char) < 1000 else '[?]' for char in sanitized)
    except Exception:
        # If character processing fails, use basic ASCII filtering
        try:
            sanitized = ''.join(char if ord(char) < 128 else '[?]' for char in sanitized)
        except Exception:
            # Final fallback - return a safe string
            return "[UNICODE_ERROR_IN_LOG_SANITIZATION]"
    
    return sanitized

# ─── Discord Utilities ───────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 1900) -> List[str]:
    """Split long text into Discord-friendly chunks (under 2000 char limit)."""
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

async def _send_limited(ctx: commands.Context, text: str, max_posts: int = 5) -> None:
    """Send text in chunks, limiting total messages to prevent spam."""
    chunks = chunk_text(text)
    if not chunks:
        return
    
    # If too many chunks, merge overflow into the last allowed chunk
    if len(chunks) > max_posts:
        head = chunks[:max_posts]
        tail = chunks[max_posts:]
        head[-1] += "\n" + "\n".join(tail)  # Append overflow to last chunk
        chunks = head
    
    # Send each chunk wrapped in code blocks
    for chunk in chunks:
        await ctx.send(f"```{chunk}```")

async def _dynamic_prefix(bot_inst: commands.Bot, message: discord.Message) -> str:
    """Dynamic prefix: humans use normal prefix, other bots use ^^^ to avoid conflicts."""
    if message.author.bot and message.author != bot_inst.user:
        return "^^^"  # Other bots use triple caret
    return PREFIX  # Humans use configured prefix

# ─── Discord Bot Setup ───────────────────────────────────────────────────────

# Configure Discord permissions and intents - Enhanced for 4006 error prevention
# Enable all privileged intents to prevent voice session conflicts (4006 errors)
intents = discord.Intents.all()  # Use all intents instead of default for better voice stability

# Explicitly enable critical intents for voice functionality
intents.message_content = True  # Required to read message content
intents.guilds = True  # Required for guild information
intents.guild_messages = True  # Required for message commands
intents.voice_states = True  # CRITICAL: Required for voice channel state tracking (prevents 4006 errors)
intents.presences = True  # Helps with session management in large servers
intents.members = True  # Helps resolve session conflicts in populated servers

# Create bot instance with dynamic prefix and no default help command
bot = commands.Bot(command_prefix=_dynamic_prefix, intents=intents, help_command=None)

# ═══════════════════════════════════════════════════════════════════════════════
# ╔══ COMMAND MODULES LOCATION TAGS ══╗
# ║ For future AI model reference:    ║
# ║                                   ║
# ║ 🎵 Voice & Music Commands:        ║
# ║    Location: voice.py             ║
# ║    Commands: play, skip, pause,   ║
# ║             resume, stop, queue,  ║
# ║             volume, loop,         ║
# ║             nowplaying,           ║
# ║             musicsearch, leave    ║
# ║                                   ║
# ║ 🔧 Voice Connection Handling:     ║
# ║    Location: voice_handler.py     ║
# ║    Commands: join, voicecleanup   ║
# ║                                   ║
# ║ 💭 Reason Commands:                ║
# ║    Location: reason.py            ║
# ║    Commands: reason (AI chat)     ║
# ║                                   ║
# ║ 🔍 Search Commands:               ║
# ║    Location: search.py            ║
# ║    Commands: search (web search)  ║
# ║                                   ║
# ║ 👁️ Visual Analysis Commands:      ║
# ║    Location: vis.py               ║
# ║    Commands: vis (visual content) ║
# ║                                   ║
# ║ 📄 Sum Commands:                   ║
# ║    Location: sum.py               ║
# ║    Commands: sum (URL summarize)  ║
# ║                                   ║
# ║ 🤖 AI Commands:                   ║
# ║    Location: Meri_Bot.py          ║
# ║    Commands: help                 ║
# ║                                   ║
# ║ 🧠 LM Commands:                   ║
# ║    Location: lm.py                ║
# ║    Commands: lm (multimodal AI)   ║
# ║                                   ║
# ║ 🔧 Admin Commands:                ║
# ║    Location: Meri_Bot.py          ║
# ║    Commands: sync, debug, nettest ║
# ╚═══════════════════════════════════╝
# ═══════════════════════════════════════════════════════════════════════════════

# Import and create voice handler
from voice_handler import VoiceHandler
voice_handler = VoiceHandler(bot)

# Import voice commands module (will be registered in on_ready)
from voice import setup_voice_commands

# Import LM commands module (will be registered in on_ready)
from lm import setup_lm_commands

# Import reason commands module (will be registered in on_ready)
from reason import setup_reason_commands  # type: ignore

# Import search commands module (will be registered in on_ready)
from search import setup_search_commands

# Import visual analysis commands module (will be registered in on_ready)
from vis import setup_vis_commands

# Import sum commands module (will be registered in on_ready)
from sum import setup_sum_commands

# Import profile picture commands modules (will be registered in on_ready)
from serverpfp import setup_serverpfp_commands
from userpfp import setup_user_commands
from perms import setup_perms_commands

# ─── Command Queue System ───────────────────────────────────────────────────
# Prevents command conflicts by ensuring only one command runs at a time
# This avoids issues like simultaneous video processing or API calls

_COMMAND_LOCK = asyncio.Lock()

# ─── Voice Connection Lock ──────────────────────────────────────────────────
# Voice connection handling is now managed by voice_handler.py

# ─── Auto-Reply Context Patching ─────────────────────────────────────────────
# Make all bot responses automatically reply to the user's message
from discord.ext.commands import Context as _BaseContext

_orig_ctx_send = _BaseContext.send  # type: ignore[attr-defined]

async def _replying_send(self: _BaseContext, *args, **kwargs):  # type: ignore[override]
    """Override Context.send to automatically reply to the invoking message."""
    if "reference" not in kwargs and isinstance(self.message, discord.Message):
        # Try to add reply reference, but handle cases where message is invalid/deleted
        try:
            kwargs["reference"] = self.message  # Set message as reply target
            kwargs.setdefault("mention_author", False)  # Don't ping (they already get notification)
        except Exception:
            # If there's any issue with the message reference, just continue without it
            pass
    
    try:
        return await _orig_ctx_send(self, *args, **kwargs)
    except discord.HTTPException as e:
        error_str = str(e).lower()
        
        # Handle message reference errors (multiple variations)
        if any(phrase in error_str for phrase in ["unknown message", "invalid form body", "message_reference"]) and "reference" in kwargs:
            # Remove the problematic reference and try again
            kwargs.pop("reference", None)
            kwargs.pop("mention_author", None)
            try:
                return await _orig_ctx_send(self, *args, **kwargs)
            except discord.HTTPException as retry_e:
                # If it's a permissions error after removing reference, try plain text
                if retry_e.status == 403 and "embed" in kwargs:  # 403 = Forbidden
                    embed = kwargs.pop("embed", None)
                    if embed and hasattr(embed, 'title') and hasattr(embed, 'description'):
                        # Convert embed to plain text
                        text_content = ""
                        if embed.title:
                            text_content += f"**{embed.title}**\n\n"
                        if embed.description:
                            text_content += f"{embed.description}\n\n"
                        if hasattr(embed, 'fields') and embed.fields:
                            for field in embed.fields:
                                if hasattr(field, 'name') and hasattr(field, 'value'):
                                    text_content += f"**{field.name}**\n{field.value}\n\n"
                        if hasattr(embed, 'footer') and embed.footer and hasattr(embed.footer, 'text'):
                            text_content += f"_{embed.footer.text}_"
                        
                        # Replace the original content with embed text
                        if text_content.strip():
                            args = (text_content.strip(),) + (args[1:] if len(args) > 1 else ())
                            try:
                                return await _orig_ctx_send(self, *args, **kwargs)
                            except discord.HTTPException:
                                # Complete failure - log and re-raise
                                logger.error(f"Complete send failure in channel {self.channel.id}: Missing permissions")
                                raise
                # If it's not a permissions issue or plain text conversion failed, re-raise
                raise
                
        # Handle permissions errors directly  
        elif e.status == 403 and "embed" in kwargs:  # 403 = Forbidden
            embed = kwargs.pop("embed", None)
            if embed and hasattr(embed, 'title') and hasattr(embed, 'description'):
                # Convert embed to plain text
                text_content = ""
                if embed.title:
                    text_content += f"**{embed.title}**\n\n"
                if embed.description:
                    text_content += f"{embed.description}\n\n"
                if hasattr(embed, 'fields') and embed.fields:
                    for field in embed.fields:
                        if hasattr(field, 'name') and hasattr(field, 'value'):
                            text_content += f"**{field.name}**\n{field.value}\n\n"
                if hasattr(embed, 'footer') and embed.footer and hasattr(embed.footer, 'text'):
                    text_content += f"_{embed.footer.text}_"
                
                # Replace the original content with embed text
                if text_content.strip():
                    args = (text_content.strip(),) + (args[1:] if len(args) > 1 else ())
                    try:
                        return await _orig_ctx_send(self, *args, **kwargs)
                    except discord.HTTPException:
                        # Complete failure - log and re-raise
                        logger.error(f"Complete send failure in channel {self.channel.id}: Missing permissions")
                        raise
        
        # For any other HTTP exceptions, just re-raise them
        raise

_BaseContext.send = _replying_send  # type: ignore[assignment]

@bot.before_invoke
async def _queue_before_invoke(ctx) -> None:
    """Acquire command lock before any command starts."""
    await _COMMAND_LOCK.acquire()

@bot.after_invoke
async def _queue_after_invoke(ctx) -> None:
    """Release command lock after command completes."""
    if _COMMAND_LOCK.locked():
        _COMMAND_LOCK.release()

# ─── Enhanced Per-User Memory & Context System ──────────────────────────────
# Stores conversation history and context per user for context-aware responses
# Includes robust helper functions to prevent universal context access bugs

_user_memory: Dict[int, List[Dict[str, str]]] = {}  # {user_id: [{"role": "user/assistant", "content": "..."}]}
_user_context: Dict[int, str] = {}  # {user_id: "last_response"} for cross-command context

MEMORY_TURNS = 5  # Keep last 5 conversation turns per user (10 messages total)

# ─── Enhanced Per-User Context System ───────────────────────────────────────────
# Robust helper functions to ensure context is always properly scoped per user

def _get_user_context(user_id: int) -> str:
    """Safely get context for a specific user. Returns empty string if no context exists."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for context access: {type(user_id)} (expected int)")
        return ""
    
    context = _user_context.get(user_id, "")
    logger.debug(f"Retrieved context for user {user_id}: {len(context)} characters")
    return context

def _set_user_context(user_id: int, content: str) -> None:
    """Safely set context for a specific user."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for context storage: {type(user_id)} (expected int)")
        return
    
    if not isinstance(content, str):
        logger.warning(f"Converting non-string content to string for user {user_id}")
        content = str(content)
    
    _user_context[user_id] = content
    logger.debug(f"Stored context for user {user_id}: {len(content)} characters")

def _get_user_memory(user_id: int) -> List[Dict[str, str]]:
    """Safely get conversation history for a specific user."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for memory access: {type(user_id)} (expected int)")
        return []
    
    memory = _user_memory.get(user_id, [])
    logger.debug(f"Retrieved memory for user {user_id}: {len(memory)} messages")
    return memory

def _clear_user_context(user_id: int) -> None:
    """Clear context for a specific user."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for context clearing: {type(user_id)} (expected int)")
        return
    
    if user_id in _user_context:
        del _user_context[user_id]
        logger.info(f"Cleared context for user {user_id}")
    else:
        logger.debug(f"No context to clear for user {user_id}")

def _clear_user_memory(user_id: int) -> None:
    """Clear conversation history for a specific user."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for memory clearing: {type(user_id)} (expected int)")
        return
    
    if user_id in _user_memory:
        del _user_memory[user_id]
        logger.info(f"Cleared memory for user {user_id}")
    else:
        logger.debug(f"No memory to clear for user {user_id}")

def _has_user_context(user_id: int) -> bool:
    """Check if a user has any stored context."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for context check: {type(user_id)} (expected int)")
        return False
    
    return user_id in _user_context and bool(_user_context[user_id].strip())

def _get_context_stats() -> Dict[str, Any]:
    """Get statistics about stored contexts (for debugging)."""
    stats = {
        'total_users_with_context': len(_user_context),
        'total_users_with_memory': len(_user_memory),
        'context_sizes': {uid: len(content) for uid, content in _user_context.items()},
        'memory_sizes': {uid: len(history) for uid, history in _user_memory.items()}
    }
    return stats

# Enhanced remember function with better validation
def _remember(user_id: int, role: str, content: str) -> None:
    """Add a message to user's conversation memory, auto-trimming old entries."""
    if not isinstance(user_id, int):
        logger.error(f"Invalid user_id type for memory storage: {type(user_id)} (expected int)")
        return
    
    if role not in ["user", "assistant", "system"]:
        logger.warning(f"Invalid role '{role}' for user {user_id}, using 'user' instead")
        role = "user"
    
    if not isinstance(content, str):
        logger.warning(f"Converting non-string content to string for user {user_id}")
        content = str(content)
    
    history = _user_memory.setdefault(user_id, [])
    history.append({"role": role, "content": content})
    
    # Keep only the most recent conversations (MEMORY_TURNS * 2 = user+assistant pairs)
    excess = len(history) - MEMORY_TURNS * 2
    if excess > 0:
        del history[0:excess]  # Remove oldest messages
        logger.debug(f"Trimmed {excess} old messages from user {user_id} memory")
    
    logger.debug(f"Added {role} message to user {user_id} memory: {len(content)} characters")

# ─── Video/Audio Processing Helpers ──────────────────────────────────────────

def _get_ytdl_opts() -> Dict[str, Any]:
    """Get basic yt_dlp options without authentication"""
    return {
        "format": "bestaudio/best",  # Prefer best audio quality
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",  # Bind to all interfaces
        "extract_flat": "in_playlist",  # Don't download full playlist info
    }

def _yt_dl_extract(url: str) -> Any:
    """Extract video/audio info from YouTube URL using yt-dlp."""
    if yt_dlp is None:
        raise RuntimeError("yt_dlp not available")
    opts = _get_ytdl_opts()
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[attr-defined]
        return ydl.extract_info(url, download=False)  # Get info without downloading


async def _extract_video_frames_from_file(video_path: str, max_frames: int = 5) -> List[str]:
    """Extract representative frames from a local video file for AI analysis.
    
    Process:
    1. Use ffprobe to get video duration
    2. Calculate evenly spaced timestamps 
    3. Extract frames at those timestamps using ffmpeg
    4. Compress and encode frames as base64
    
    Returns list of base64-encoded JPEG images.
    """
    frames_base64: List[str] = []
    
    try:
        # Step 1: Validate video file and get video duration using ffprobe
        loop = asyncio.get_running_loop()
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json', 
            '-show_format', '-show_streams', video_path
        ]
        
        try:
            def run_probe():
                return subprocess.run(probe_cmd, capture_output=True, text=True)
            result = await loop.run_in_executor(None, run_probe)
            
            if result.returncode != 0:
                logger.error(f"ffprobe failed: {result.stderr}")
                return frames_base64
                
            probe_data = json.loads(result.stdout)
            
            # Check if file has video streams
            streams = probe_data.get('streams', [])
            has_video = any(stream.get('codec_type') == 'video' for stream in streams)
            
            if not has_video:
                logger.warning(f"Video file contains no video streams: {video_path}")
                return frames_base64
            
            duration = float(probe_data.get('format', {}).get('duration', 0))
            logger.info(f"Video validated: duration={duration}s, has_video={has_video}")
            
        except Exception as e:
            logger.error(f"Failed to get video duration: {e}")
            duration = 60.0  # Fallback assumption
        
        if duration <= 0:
            return frames_base64
        
        # Step 2: Calculate optimal timestamps for frame extraction
        timestamps = []
        # Always try to extract the requested number of frames, regardless of duration
        if duration <= 5:
            # Very short video: extract frames at regular intervals, minimum 0.5s apart
            interval = max(0.5, duration / max_frames)
            for i in range(max_frames):
                timestamp = (i + 0.5) * interval
                if timestamp < duration:
                    timestamps.append(timestamp)
                else:
                    break
        else:
            # Longer video: get evenly spaced frames (avoiding start/end)
            step = duration / (max_frames + 1)
            for i in range(1, max_frames + 1):
                timestamps.append(i * step)
        
        timestamps = timestamps[:max_frames]  # Ensure we don't exceed limit
        
        # Step 3: Extract frames using ffmpeg in temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Process each timestamp to extract a frame
            for idx, timestamp in enumerate(timestamps):
                frame_path = temp_path / f"frame_{idx}.jpg"
                
                # Build ffmpeg command for frame extraction
                cmd = [
                    'ffmpeg',
                    '-ss', str(timestamp),  # Seek to timestamp
                    '-i', video_path,  # Input video file
                    '-vframes', '1',  # Extract only 1 frame
                    '-q:v', '2',  # High quality JPEG
                    '-vf', 'scale=640:-1',  # Resize to max width 640px (maintain aspect ratio)
                    str(frame_path),  # Output file
                    '-y'  # Overwrite existing files
                ]
                
                try:
                    # Run ffmpeg asynchronously
                    def run_ffmpeg():
                        return subprocess.run(cmd, capture_output=True, text=True)
                    result = await loop.run_in_executor(None, run_ffmpeg)
                    
                    if result.returncode != 0:
                        logger.warning(f"ffmpeg failed for timestamp {timestamp}: {result.stderr}")
                        continue
                    
                    # Step 4: Read, compress, and encode the extracted frame
                    if frame_path.exists():
                        with open(frame_path, 'rb') as f:
                            frame_data = f.read()
                        
                        # Compress large images to reduce API payload size
                        if _PIL_AVAILABLE and Image is not None and len(frame_data) > 500000:  # > 500KB
                            try:
                                img = Image.open(io.BytesIO(frame_data))  # type: ignore
                                
                                # Convert RGBA to RGB (some codecs produce RGBA)
                                if img.mode == 'RGBA':
                                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))  # type: ignore
                                    rgb_img.paste(img, mask=img.split()[3])  # Use alpha for transparency
                                    img = rgb_img
                                
                                # Re-compress with better compression
                                buffer = io.BytesIO()
                                img.save(buffer, format='JPEG', quality=85, optimize=True)
                                frame_data = buffer.getvalue()
                            except Exception as e:
                                logger.warning(f"PIL compression failed: {e}")
                        
                        # Encode as base64 for API transmission
                        frame_base64 = base64.b64encode(frame_data).decode('utf-8')
                        frames_base64.append(frame_base64)
                        
                except Exception as e:
                    logger.error(f"Frame extraction failed at timestamp {timestamp}: {e}")
                    continue
        
        logger.info(f"Extracted {len(frames_base64)} frames from local video")
        return frames_base64
        
    except Exception as e:
        # Use Unicode-safe logging to prevent cp1252 encoding errors
        try:
            error_msg = f"Local video frame extraction failed: {_sanitize_for_log(str(e))}"
            logger.error(error_msg)
        except Exception:
            logger.error("Local video frame extraction failed - logging error occurred")
        return frames_base64


async def _extract_video_frames(url: str, max_frames: int = 5, interval: int = 30) -> List[str]:
    """Extract frames from a YouTube video at specified intervals.
    
    Returns a list of base64-encoded images.
    """
    frames_base64: List[str] = []
    
    if yt_dlp is None:
        logger.error("yt_dlp not available for frame extraction")
        return frames_base64
    
    try:
        # Get video info first
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _yt_dl_extract, url)
        
        if info is None:
            return frames_base64
            
        duration = info.get('duration', 0)
        if duration <= 0:
            return frames_base64
        
        # Calculate timestamps for frame extraction
        timestamps = []
        # Always try to extract the requested number of frames, regardless of duration
        if duration <= 5:
            # Very short video: extract frames at regular intervals, minimum 0.5s apart
            frame_interval = max(0.5, duration / max_frames)
            for i in range(max_frames):
                timestamp = (i + 0.5) * frame_interval
                if timestamp < duration:
                    timestamps.append(timestamp)
                else:
                    break
        else:
            # Longer video: get evenly spaced frames (avoiding start/end)
            step = duration / (max_frames + 1)
            for i in range(1, max_frames + 1):
                timestamps.append(i * step)
        
        # Limit to max_frames
        timestamps = timestamps[:max_frames]
        
        # Use temporary directory for frames
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Download video with yt-dlp (small format for speed)
            video_path = temp_path / "video.mp4"
            ydl_opts = {
                'format': 'worst[ext=mp4]/worst',  # Smallest video for speed
                'outtmpl': str(video_path),
                'quiet': True,
                'no_warnings': True,
            }
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[attr-defined]
                    await loop.run_in_executor(None, ydl.download, [url])
            except Exception as e:
                logger.error(f"Failed to download video for frame extraction: {e}")
                return frames_base64
            
            if not video_path.exists():
                logger.error("Video download failed - file not found")
                return frames_base64
            
            # Extract frames using ffmpeg
            for idx, timestamp in enumerate(timestamps):
                frame_path = temp_path / f"frame_{idx}.jpg"
                
                # ffmpeg command to extract frame at timestamp
                cmd = [
                    'ffmpeg',
                    '-ss', str(timestamp),
                    '-i', str(video_path),
                    '-vframes', '1',
                    '-q:v', '2',  # Quality setting
                    '-vf', 'scale=640:-1',  # Resize to max width 640px
                    str(frame_path),
                    '-y'  # Overwrite
                ]
                
                try:
                    # Run ffmpeg
                    def run_ffmpeg():
                        return subprocess.run(cmd, capture_output=True, text=True)
                    result = await loop.run_in_executor(None, run_ffmpeg)
                    
                    if result.returncode != 0:
                        logger.warning(f"ffmpeg failed for timestamp {timestamp}: {result.stderr}")
                        continue
                    
                    # Read and encode frame
                    if frame_path.exists():
                        with open(frame_path, 'rb') as f:
                            frame_data = f.read()
                        
                        # Optionally compress with PIL if available
                        if _PIL_AVAILABLE and Image is not None and len(frame_data) > 500000:  # If > 500KB
                            try:
                                img = Image.open(io.BytesIO(frame_data))  # type: ignore
                                # Convert RGBA to RGB if needed
                                if img.mode == 'RGBA':
                                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))  # type: ignore
                                    rgb_img.paste(img, mask=img.split()[3])
                                    img = rgb_img
                                
                                # Compress
                                buffer = io.BytesIO()
                                img.save(buffer, format='JPEG', quality=85, optimize=True)
                                frame_data = buffer.getvalue()
                            except Exception as e:
                                logger.warning(f"PIL compression failed: {e}")
                        
                        # Convert to base64
                        frame_base64 = base64.b64encode(frame_data).decode('utf-8')
                        frames_base64.append(frame_base64)
                        
                except Exception as e:
                    logger.error(f"Frame extraction failed at timestamp {timestamp}: {e}")
                    continue
        
        logger.info(f"Extracted {len(frames_base64)} frames from video")
        return frames_base64
        
    except Exception as e:
        # Use Unicode-safe logging to prevent cp1252 encoding errors
        try:
            error_msg = f"Video frame extraction failed: {_sanitize_for_log(str(e))}"
            logger.error(error_msg)
        except Exception:
            logger.error("Video frame extraction failed - logging error occurred")
        return frames_base64


async def _extract_gif_frames(gif_path: str, max_frames: int = 5) -> List[str]:
    """Extract frames from a GIF file for AI analysis.
    
    Process:
    1. Open GIF file with PIL
    2. Extract evenly spaced frames
    3. Convert to JPEG and encode as base64
    
    Returns list of base64-encoded JPEG images.
    """
    frames_base64: List[str] = []
    
    if not _PIL_AVAILABLE or Image is None:
        logger.error("PIL not available for GIF frame extraction")
        return frames_base64
    
    try:
        # Open the GIF file
        with Image.open(gif_path) as img:  # type: ignore
            if not getattr(img, 'is_animated', False):
                # Not an animated GIF, treat as single image
                buffer = io.BytesIO()
                # Convert to RGB if needed
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(buffer, format='JPEG', quality=85, optimize=True)
                frame_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                frames_base64.append(frame_base64)
                return frames_base64
            
            # Get total number of frames
            frame_count = getattr(img, 'n_frames', 1)
            
            # Calculate which frames to extract (evenly spaced)
            if frame_count <= max_frames:
                # Extract all frames if GIF has few frames
                frame_indices = list(range(frame_count))
            else:
                # Extract evenly spaced frames
                step = frame_count / max_frames
                frame_indices = [int(i * step) for i in range(max_frames)]
            
            # Extract the selected frames
            for frame_idx in frame_indices:
                try:
                    img.seek(frame_idx)
                    
                    # Convert frame to RGB (GIFs can have palettes)
                    frame = img.convert('RGB')
                    
                    # Resize if too large (keep aspect ratio)
                    if frame.width > 640:
                        ratio = 640 / frame.width
                        new_height = int(frame.height * ratio)
                        frame = frame.resize((640, new_height), Image.Resampling.LANCZOS)  # type: ignore
                    
                    # Convert to JPEG and encode as base64
                    buffer = io.BytesIO()
                    frame.save(buffer, format='JPEG', quality=85, optimize=True)
                    frame_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    frames_base64.append(frame_base64)
                    
                except Exception as e:
                    logger.warning(f"Failed to extract GIF frame {frame_idx}: {e}")
                    continue
        
        logger.info(f"Extracted {len(frames_base64)} frames from GIF")
        return frames_base64
        
    except Exception as e:
        # Use Unicode-safe logging to prevent cp1252 encoding errors
        try:
            error_msg = f"GIF frame extraction failed: {_sanitize_for_log(str(e))}"
            logger.error(error_msg)
        except Exception:
            logger.error("GIF frame extraction failed - logging error occurred")
        return frames_base64


async def _extract_twitter_media(url: str) -> tuple[List[str], List[str]]:
    """Extract images and videos from a Twitter/X post using yt-dlp.
    
    Returns:
        tuple: (list of image base64 strings, list of video URLs for frame extraction)
    """
    images_base64 = []
    video_urls = []
    
    try:
        logger.info(f"Extracting media from Twitter/X post: {url}")
        
        # Primary method: Use yt-dlp to extract Twitter media - bypasses header issues
        if yt_dlp is not None:
            loop = asyncio.get_running_loop()
            
            def _extract_twitter_info(twitter_url: str):
                """Extract info from Twitter URL using yt-dlp"""
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': False,
                    'writesubtitles': False,
                    'writeautomaticsub': False,
                    'skip_download': True,  # Only get info, don't download
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore
                    try:
                        info = ydl.extract_info(twitter_url, download=False)
                        return info
                    except Exception as e:
                        logger.warning(f"yt-dlp extraction failed for {twitter_url}: {e}")
                        return None
            
            # Try to extract info using yt-dlp first
            info = await loop.run_in_executor(None, _extract_twitter_info, url)
            
            if info:
                logger.info(f"yt-dlp successfully extracted info from Twitter post")
                
                # Debug: Log what fields are available
                available_fields = list(info.keys())
                logger.info(f"yt-dlp returned fields: {available_fields}")
                
                # Extract video URLs if available - try multiple possible structures
                video_found = False
                
                # Method 1: Direct URL field
                if 'url' in info and info['url']:
                    url_val = info['url']
                    if url_val.endswith('.mp4') or 'video' in url_val.lower():
                        video_urls.append(url_val)
                        logger.info(f"Found video URL (direct): {url_val}")
                        video_found = True
                
                # Method 2: Check formats array
                if 'formats' in info and info['formats']:
                    logger.info(f"Found {len(info['formats'])} formats")
                    for idx, fmt in enumerate(info['formats']):
                        logger.info(f"Format {idx}: ext={fmt.get('ext')}, protocol={fmt.get('protocol')}, url={fmt.get('url', 'N/A')[:50]}...")
                        
                        # Look for video formats
                        if (fmt.get('ext') in ['mp4', 'webm', 'mov'] or 
                            fmt.get('vcodec') not in [None, 'none'] or
                            'video' in str(fmt.get('format_note', '')).lower()):
                            
                            if fmt.get('url') and fmt['url'] not in video_urls:
                                video_urls.append(fmt['url'])
                                logger.info(f"Found video format: {fmt['url']}")
                                video_found = True
                
                # Method 3: Check for entries (playlist-like structure)
                if 'entries' in info and info['entries']:
                    logger.info(f"Found {len(info['entries'])} entries")
                    for idx, entry in enumerate(info['entries']):
                        if entry:
                            logger.info(f"Entry {idx}: type={entry.get('_type')}, id={entry.get('id')}, title={_sanitize_for_log(entry.get('title', 'N/A')[:50])}")
                            
                            # Check for direct video URL in entry
                            if 'url' in entry and entry['url']:
                                entry_url = entry['url']
                                if (entry_url.endswith('.mp4') or entry_url.endswith('.webm') or 
                                    'video' in entry_url.lower() or entry.get('ext') in ['mp4', 'webm', 'mov']):
                                    if entry_url not in video_urls:
                                        video_urls.append(entry_url)
                                        logger.info(f"Found video URL (entry): {entry_url}")
                                        video_found = True
                            
                            # Check for formats within the entry
                            if 'formats' in entry and entry['formats']:
                                logger.info(f"Entry {idx} has {len(entry['formats'])} formats")
                                for fmt_idx, fmt in enumerate(entry['formats']):
                                    logger.info(f"Entry {idx} Format {fmt_idx}: ext={fmt.get('ext')}, vcodec={fmt.get('vcodec')}, url={fmt.get('url', 'N/A')[:50]}...")
                                    
                                    # Look for video formats in entry
                                    if (fmt.get('ext') in ['mp4', 'webm', 'mov'] or 
                                        fmt.get('vcodec') not in [None, 'none'] or
                                        'video' in str(fmt.get('format_note', '')).lower()):
                                        
                                        if fmt.get('url') and fmt['url'] not in video_urls:
                                            video_urls.append(fmt['url'])
                                            logger.info(f"Found video format in entry: {fmt['url']}")
                                            video_found = True
                            
                            # Check for thumbnail in entry
                            if 'thumbnail' in entry and entry['thumbnail']:
                                try:
                                    logger.info(f"Downloading thumbnail from entry {idx}: {entry['thumbnail']}")
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(entry['thumbnail'], timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                            if resp.status == 200:
                                                image_data = await resp.read()
                                                base64_image = base64.b64encode(image_data).decode('utf-8')
                                                images_base64.append(base64_image)
                                                logger.info(f"Downloaded thumbnail from entry {idx}")
                                                image_found = True
                                except Exception as e:
                                    logger.warning(f"Failed to download thumbnail from entry {idx}: {e}")
                
                # Extract images from main post level - try multiple methods
                image_found = False
                
                # Method 4: Standard thumbnail (main post level)
                if 'thumbnail' in info and info['thumbnail']:
                    try:
                        logger.info(f"Downloading main thumbnail: {info['thumbnail']}")
                        async with aiohttp.ClientSession() as session:
                            async with session.get(info['thumbnail'], timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    image_data = await resp.read()
                                    base64_image = base64.b64encode(image_data).decode('utf-8')
                                    images_base64.append(base64_image)
                                    logger.info(f"Downloaded main thumbnail image")
                                    image_found = True
                    except Exception as e:
                        logger.warning(f"Failed to download main thumbnail: {e}")
                
                # Method 5: Multiple thumbnails (main post level)
                if 'thumbnails' in info and info['thumbnails']:
                    logger.info(f"Found {len(info['thumbnails'])} main thumbnails")
                    for idx, thumb in enumerate(info['thumbnails']):
                        if thumb.get('url') and thumb['url'] != info.get('thumbnail'):
                            try:
                                logger.info(f"Downloading main thumbnail {idx}: {thumb['url']}")
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(thumb['url'], timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                        if resp.status == 200:
                                            image_data = await resp.read()
                                            base64_image = base64.b64encode(image_data).decode('utf-8')
                                            images_base64.append(base64_image)
                                            logger.info(f"Downloaded additional main thumbnail")
                                            image_found = True
                                            if len(images_base64) >= 4:  # Limit to 4 images max
                                                break
                            except Exception as e:
                                logger.warning(f"Failed to download main thumbnail {idx}: {e}")
                                continue
                
                # Method 6: Look for media entries in the data structure
                if 'media' in info:
                    logger.info(f"Found media field in yt-dlp data")
                    # Handle media field if present
                
                # Method 7: Check if this is a Twitter Spaces or other special content
                if info.get('extractor') == 'twitter' and info.get('title'):
                    logger.info(f"Twitter extractor found content: {_sanitize_for_log(info.get('title', 'N/A'))}")
                
                # Log summary of what was found
                logger.info(f"yt-dlp extraction summary: {len(video_urls)} videos, {len(images_base64)} images found")
                
                # If we found content, don't try fallback
                if video_found or image_found:
                    logger.info("Skipping fallback APIs since yt-dlp found media")
                else:
                    logger.info("No media found by yt-dlp, will try fallback APIs")
        
        # Fallback: Try syndication API if yt-dlp didn't find anything or isn't available
        if not images_base64 and not video_urls:
            logger.info("Trying Twitter syndication API as fallback")
            
            # Extract tweet ID from any Twitter URL format
            tweet_id_match = re.search(r"/status/(\d+)", url)
            if tweet_id_match:
                tweet_id = tweet_id_match.group(1)
                syndication_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en"
                
                try:
                    async with aiohttp.ClientSession() as session:
                        headers = {
                            "User-Agent": "Mozilla/5.0 (compatible; MediaBot/1.0)",
                            "Accept": "application/json",
                        }
                        async with session.get(syndication_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                
                                # Extract photos
                                if 'photos' in data:
                                    for photo in data['photos']:
                                        photo_url = photo.get('url')
                                        if photo_url:
                                            try:
                                                async with session.get(photo_url) as img_resp:
                                                    if img_resp.status == 200:
                                                        image_data = await img_resp.read()
                                                        base64_image = base64.b64encode(image_data).decode('utf-8')
                                                        images_base64.append(base64_image)
                                                        logger.info(f"Downloaded image from syndication API")
                                            except Exception as e:
                                                logger.warning(f"Failed to download syndication image: {e}")
                                
                                # Extract videos
                                if 'video' in data:
                                    video_info = data['video']
                                    if 'variants' in video_info:
                                        # Find the best quality video variant
                                        best_variant = None
                                        best_bitrate = 0
                                        
                                        for variant in video_info['variants']:
                                            if variant.get('content_type') == 'video/mp4':
                                                bitrate = variant.get('bitrate', 0)
                                                if bitrate > best_bitrate:
                                                    best_bitrate = bitrate
                                                    best_variant = variant
                                        
                                        if best_variant and 'url' in best_variant:
                                            video_urls.append(best_variant['url'])
                                            logger.info(f"Found video from syndication API")
                
                except Exception as e:
                    logger.warning(f"Syndication API failed: {e}")
        
        logger.info(f"Twitter/X media extraction complete: {len(images_base64)} images, {len(video_urls)} videos")
        return images_base64, video_urls
        
    except Exception as e:
        logger.error(f"Failed to extract Twitter/X media from {url}: {e}")
        return images_base64, video_urls


# ─── Voice Commands ──────────────────────────────────────────────────────────
# NOTE: ALL Voice & Music commands have been moved to voice.py for better organization
# They are imported and registered above. Commands include:
# join, play, skip, pause, resume, stop, queue, volume, loop, nowplaying, musicsearch, leave, voice


# ─── AI Chat Commands ────────────────────────────────────────────────────────
# NOTE: Reason command has been moved to reason.py for better organization

# ─── LM (Language Model) Commands ───────────────────────────────────────────
# NOTE: LM command has been moved to lm.py for better organization

# ─── Search Commands ─────────────────────────────────────────────────────────
# NOTE: Search command has been moved to search.py for better organization

# ─── Summarize URL / YouTube Helper ─────────────────────────────────────────

async def _get_content_text(url: str) -> str:
    """Download and return text content from a web page or YouTube video."""
    if any(domain in url for domain in ("youtube.com", "youtu.be")):
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, _yt_dl_extract, url)
            if data is None:
                raise RuntimeError("yt_dlp returned nothing")

            # Helper: strip timestamps and tags from .vtt or .xml caption text
            def _clean_caption(raw: str) -> str:
                # Remove WEBVTT header
                raw = re.sub(r"WEBVTT.*?\n", "", raw, flags=re.IGNORECASE | re.DOTALL)
                # Remove timestamps lines
                raw = re.sub(r"\d{2}:\d{2}:\d{2}\.\d{3} --> .*\n", "", raw)
                # Remove XML tags
                raw = re.sub(r"<[^>]+>", "", raw)
                # Collapse whitespace
                raw = re.sub(r"\s+", " ", raw)
                return raw.strip()

            # 1) Use youtube_transcript_api or Google timedtext endpoint
            video_id = data.get("id")
            if video_id:
                if YouTubeTranscriptApi is not None:
                    try:
                        # First try English variants
                        transcript_entries = await loop.run_in_executor(
                            None,
                            lambda: cast(Any, YouTubeTranscriptApi).get_transcript(
                                video_id,
                                languages=["en", "en-US", "en-GB"],
                            ),
                        )
                        if transcript_entries:
                            return " ".join(seg["text"] for seg in transcript_entries)
                    except Exception:
                        # Fall back to any available language if English not present
                        try:
                            transcript_entries = await loop.run_in_executor(
                                None,
                                lambda: cast(Any, YouTubeTranscriptApi).get_transcript(video_id),
                            )
                            if transcript_entries:
                                return " ".join(seg["text"] for seg in transcript_entries)
                        except Exception:
                            pass  # continue to other methods
                # direct timedtext fetch
                timed_url = f"https://video.google.com/timedtext?lang=en&v={video_id}"
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(timed_url, timeout=aiohttp.ClientTimeout(total=10)) as tresp:
                            if tresp.status == 200:
                                xml_txt = await tresp.text()
                                if xml_txt.strip():
                                    cleaned = _clean_caption(xml_txt)
                                    if cleaned:
                                        return cleaned
                except Exception:
                    pass

            # 2) Try captions provided by yt_dlp
            captions = data.get("automatic_captions") or data.get("subtitles") or {}
            # Accept first suitable caption (English preferred but fallback allowed)
            preferred_tracks = []
            # prioritise english tracks
            for key in captions:
                if key.startswith("en"):
                    preferred_tracks.append((key, captions[key]))
            # add rest
            for key in captions:
                if not key.startswith("en"):
                    preferred_tracks.append((key, captions[key]))

            for _, tracks in preferred_tracks:
                if not tracks:
                    continue
                caption_url = tracks[0].get("url")
                if not caption_url:
                    continue
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(caption_url) as cresp:
                        if cresp.status == 200:
                            caption_raw = await cresp.text()
                            cleaned = _clean_caption(caption_raw)
                            if cleaned:
                                return cleaned
                    break

            # 3) Fallback to metadata
            title = data.get("title", "")
            uploader = data.get("uploader", "")
            description = data.get("description", "")
            view_count = data.get("view_count")
            duration = data.get("duration")  # seconds
            meta_lines = [f"Title: {title}", f"Uploader: {uploader}"]
            if view_count:
                meta_lines.append(f"Views: {view_count}")
            if duration:
                mins = int(duration) // 60
                meta_lines.append(f"Duration: {mins} minutes")
            if description:
                meta_lines.append("\nDescription:\n" + description)
            return "\n".join(meta_lines)
        except Exception as e:
            # Use Unicode-safe logging to prevent cp1252 encoding errors
            try:
                error_msg = f"Failed to fetch YouTube content: {_sanitize_for_log(str(e))}"
                logger.error(error_msg)
            except Exception:
                logger.error("Failed to fetch YouTube content - logging error occurred")
    
    # X (Twitter) single-post fetch
    # First resolve any Twitter/X redirect URLs
    resolved_url = await _resolve_twitter_redirect(url)
    if resolved_url != url:
        logger.info(f"Resolved Twitter/X redirect: {url} -> {resolved_url}")
        url = resolved_url  # Use the resolved URL for processing
    
    if re.search(r"(twitter\.com|x\.com)/[^/]+/status/\d+", url):
        try:
            tweet_id_match = re.search(r"/status/(\d+)", url)
            if tweet_id_match:
                tweet_id = tweet_id_match.group(1)
                logger.info(f"Attempting to fetch X post with ID: {tweet_id}")
                
                # Try syndication API first
                api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Accept": "application/json"
                }
                async with aiohttp.ClientSession(headers=headers) as sess:
                    async with sess.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        logger.info(f"X API response status: {resp.status}")
                        if resp.status == 200:
                            data = await resp.json()
                            # Try different possible field names
                            text = data.get("text") or data.get("full_text") or data.get("tweet_text")
                            if text:
                                logger.info(f"Successfully extracted X post text: {_sanitize_for_log(text[:50])}...")
                                return text
                            else:
                                logger.warning(f"X API returned data but no text field found. Keys: {list(data.keys())}")
                        else:
                            error_text = await resp.text()
                            logger.warning(f"X API returned status {resp.status}: {error_text[:200]}")
                
                # Try Twitter oEmbed API as second attempt
                try:
                    oembed_url = f"https://publish.twitter.com/oembed?url={quote_plus(url)}&omit_script=true"
                    logger.info(f"Trying Twitter oEmbed API: {oembed_url}")
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(oembed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                html = data.get("html", "")
                                if html:
                                    # Extract text from the HTML
                                    text_match = re.search(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
                                    if text_match:
                                        tweet_text = re.sub(r'<[^>]+>', '', text_match.group(1))
                                        tweet_text = unescape(tweet_text).strip()
                                        if tweet_text:
                                            logger.info(f"Successfully extracted via oEmbed: {_sanitize_for_log(tweet_text[:50])}...")
                                            return tweet_text
                except Exception as e:
                    logger.warning(f"oEmbed attempt failed: {e}")
                
                # If we get here, syndication API failed
                # Try nitter instance as fallback
                try:
                    nitter_instances = ["nitter.net", "nitter.it", "nitter.namazso.eu"]
                    username_match = re.search(r"(twitter\.com|x\.com)/([^/]+)/status", url)
                    if username_match:
                        username = username_match.group(2)
                        for nitter in nitter_instances:
                            try:
                                nitter_url = f"https://{nitter}/{username}/status/{tweet_id}"
                                logger.info(f"Trying nitter instance: {nitter_url}")
                                async with aiohttp.ClientSession() as sess:
                                    async with sess.get(nitter_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                        if resp.status == 200:
                                            html = await resp.text()
                                            # Extract tweet text from nitter HTML
                                            content_match = re.search(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                                            if content_match:
                                                tweet_text = re.sub(r'<[^>]+>', '', content_match.group(1)).strip()
                                                if tweet_text:
                                                    logger.info(f"Successfully extracted via nitter: {_sanitize_for_log(tweet_text[:50])}...")
                                                    return tweet_text
                            except Exception:
                                continue
                except Exception as e:
                    logger.error(f"Nitter fallback failed: {e}")
                
                # Try direct fetch from X with proper headers
                try:
                    logger.info("Attempting direct fetch from X URL")
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1"
                    }
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as resp:
                            if resp.status == 200:
                                html = await resp.text()
                                # Look for tweet text in meta tags
                                meta_match = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
                                if meta_match:
                                    tweet_text = unescape(meta_match.group(1))
                                    if tweet_text and not tweet_text.startswith("Log in to Twitter"):
                                        logger.info(f"Successfully extracted from meta tags: {_sanitize_for_log(tweet_text[:50])}...")
                                        return tweet_text
                except Exception as e:
                    logger.warning(f"Direct X fetch failed: {e}")
                
                # If all methods failed, try to get at least some metadata
                try:
                    # Extract username and provide context
                    username_match = re.search(r"(twitter\.com|x\.com)/([^/]+)/status", url)
                    username = username_match.group(2) if username_match else "unknown user"
                    return f"[X/Twitter post by @{username} (ID: {tweet_id}). Content extraction failed - the post may be private, deleted, or protected. URL: {url}]"
                except:
                    pass
                
                # Final fallback
                return f"[X/Twitter post from {url} - content could not be extracted. Please check if the post is public and accessible.]"
                
        except Exception as e:
            logger.error(f"Failed to fetch Twitter/X content from {url}", exc_info=e)
            return f"[X/Twitter post from {url} - extraction failed: {str(e)}]"

    
    # Wikipedia
    if re.search(r"wikipedia\.org/wiki/", url):
        try:
            title = url.split("/wiki/")[-1]
            api_url = f"https://en.wikipedia.org/api/rest_v1/page/plain/{title}"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.text()
        except Exception:
            pass  # fall back to generic fetch

    
    # Generic HTML fetch
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                html_text = await resp.text()

        # Use newspaper3k if available for better article extraction
        try:
            from newspaper import Article  # type: ignore
            article = Article(url)
            article.download(input_html=html_text)
            article.parse()
            if article.text:
                return article.text
        except Exception:
            pass  # fallback to manual strip

        # Manual stripping as last resort
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text
    except Exception as e:
        logger.error("Failed to fetch webpage content", exc_info=e)
    return ""

# ─── Sum Commands ───────────────────────────────────────────────────────────
# NOTE: Sum command has been moved to sum.py for better organization

# ─── Help Command ───────────────────────────────────────────────────────────
@bot.hybrid_command(name="help", description="Show bot commands and usage")
async def help_command(ctx):
    """Display available commands with clear, concise usage information."""
    
    help_text = f"""
🤖 **Meri Bot Commands**
**Prefix:** `{PREFIX}` or `/`

🧠 **AI Commands**
• `{PREFIX}reason <question>` - AI chat with web search
• `{PREFIX}lm <prompt>` - Vision AI (images/videos)
• `{PREFIX}sum <url>` - Summarize content
• `{PREFIX}search <query>` - Web search

🖼️ **Avatar Commands**
• `{PREFIX}userpfp [user]` - Get profile picture
• `{PREFIX}serverpfp [user]` - Get server avatar
• `{PREFIX}pfp` / `{PREFIX}avatar` - Shortcuts

🎵 **Music Commands**
• `{PREFIX}play <url>` - Play music
• `{PREFIX}skip` / `{PREFIX}pause` / `{PREFIX}resume` - Controls
• `{PREFIX}queue` / `{PREFIX}leave` - Queue/leave

🔧 **Utility Commands**
• `{PREFIX}perms [channel]` - Check bot permissions
• `{PREFIX}allperms` - Show all permissions

🔧 **Modifiers**
• `-m` - Reply to message for context
• `-s` - Add web search
• `-vis` - Analyze video frames

**Examples:**
• `{PREFIX}reason -s quantum computing` - Search & answer
• `{PREFIX}lm -vis` - Analyze video
• `{PREFIX}userpfp @user` - Get avatar
"""
    
    await ctx.send(help_text.strip())

@bot.hybrid_command(name="help2", description="Show troubleshooting commands and advanced features")
async def help2_command(ctx):
    """Display troubleshooting commands and tips."""
    
    help2_text = f"""
🤖 **Meri Bot - Troubleshooting & Tips**

🔧 **Troubleshooting**
• `{PREFIX}joinraw` - Fix voice connection issues
• `{PREFIX}contextstats` - Check memory stats
• `{PREFIX}clearcontext` - Clear your memory

💡 **Tips**
• Combine modifiers: `{PREFIX}lm -m -vis -s`
• React with ❌ to delete bot messages
• Models auto-unload after 1 minute

🎵 **More Music**
• `{PREFIX}stop` / `{PREFIX}loop` / `{PREFIX}nowplaying`
• `{PREFIX}volume <0-100>` / `{PREFIX}musicsearch`

ℹ️ **Supports**
• Images, videos, YouTube, Twitter, PDFs
• 5 conversation turns memory per user
"""
    
    await ctx.send(help2_text.strip())

# ─── Admin Commands ─────────────────────────────────────────────────────────

@bot.command(name="sync")
@commands.is_owner()  # Only bot owner can use this
async def sync_commands(ctx, guild_id: Optional[int] = None):
    """Manually sync slash commands to Discord (owner only)."""
    try:
        if guild_id:
            # Sync to specific guild (appears instantly)
            guild = bot.get_guild(guild_id)
            if guild:
                synced = await bot.tree.sync(guild=guild)
                await ctx.send(f"✅ Synced {len(synced)} commands to {guild.name}")
            else:
                await ctx.send("❌ Guild not found")
        else:
            # Sync globally (can take up to 1 hour to appear)
            synced = await bot.tree.sync()
            await ctx.send(f"✅ Synced {len(synced)} commands globally (may take up to an hour to appear)")
    except Exception as e:
        await ctx.send(f"❌ Failed to sync: {e}")

@bot.command(name="slashinfo")
async def slash_info(ctx):
    """Display information about registered slash commands."""
    # Collect all registered application commands
    command_list = []
    for cmd in bot.tree.get_commands():
        if isinstance(cmd, app_commands.Command):  # Only slash commands, not context menus
            command_list.append(f"/{cmd.name} - {cmd.description}")
    
    # Build response embed
    if command_list:
        embed = discord.Embed(
            title="Registered Slash Commands",
            description="\n".join(command_list),
            color=0x00ff00
        )
        embed.add_field(
            name="Note",
            value="Global commands can take up to 1 hour to appear in Discord.\nUse `/` in any channel to see available commands.",
            inline=False
        )
    else:
        embed = discord.Embed(
            title="No Slash Commands Found",
            description="No slash commands are currently registered.",
            color=0xff0000
        )
    
    await ctx.send(embed=embed)

@bot.command(name="debug")
@commands.is_owner()  # Only bot owner can use this
async def debug_permissions(ctx):
    """Debug bot permissions and guild information (owner only)."""
    embed = discord.Embed(title="🔍 Debug Information", color=0x0099ff)
    
    # Guild information
    embed.add_field(
        name="🏰 Guild Info",
        value=f"Guild: {ctx.guild.name if ctx.guild else 'None'}\n"
              f"Guild ID: {ctx.guild.id if ctx.guild else 'None'}\n"
              f"Guild.me: {ctx.guild.me if ctx.guild else 'None'}\n"
              f"Bot User: {bot.user}",
        inline=False
    )
    
    # Check if user is in voice
    if ctx.author.voice and ctx.author.voice.channel:
        channel = ctx.author.voice.channel
        embed.add_field(
            name="🎤 Voice Channel",
            value=f"Channel: {channel.mention}\n"
                  f"Channel ID: {channel.id}\n"
                  f"Type: {channel.type}",
            inline=False
        )
        
        # Try to get permissions
        try:
            if ctx.guild and ctx.guild.me:
                permissions = channel.permissions_for(ctx.guild.me)
                perm_list = []
                
                # Check all relevant permissions
                perm_checks = {
                    'view_channel': permissions.view_channel,
                    'connect': permissions.connect,
                    'speak': permissions.speak,
                    'use_voice_activation': permissions.use_voice_activation,
                    'priority_speaker': permissions.priority_speaker,
                    'stream': permissions.stream,
                    'manage_channels': permissions.manage_channels,
                    'manage_roles': permissions.manage_roles,
                    'administrator': permissions.administrator
                }
                
                for perm_name, has_perm in perm_checks.items():
                    perm_list.append(f"{perm_name}: {'✅' if has_perm else '❌'}")
                
                embed.add_field(
                    name="🔐 Detailed Permissions",
                    value="\n".join(perm_list),
                    inline=False
                )
                
                # Raw permission value
                embed.add_field(
                    name="📊 Raw Permissions",
                    value=f"Permissions value: {permissions.value}",
                    inline=False
                )
            else:
                embed.add_field(
                    name="❌ Permission Check Failed",
                    value=f"Guild: {ctx.guild}\nGuild.me: {ctx.guild.me if ctx.guild else 'No Guild'}",
                    inline=False
                )
        except Exception as e:
            embed.add_field(
                name="❌ Permission Error",
                value=f"Error: {str(e)}",
                inline=False
            )
    else:
        embed.add_field(
            name="❌ Voice Status",
            value="User not in voice channel",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.command(name="testlogs")
@commands.is_owner()  # Only bot owner can use this
async def test_logging(ctx):
    """Test the logging configuration to verify full output is enabled (owner only)."""
    logger.debug("DEBUG level test message")
    logger.info("INFO level test message")
    logger.warning("WARNING level test message")
    logger.error("ERROR level test message")
    
    # Test reason logger specifically
    import logging
    reason_test_logger = logging.getLogger("MeriReason")
    reason_test_logger.debug("Reason DEBUG test message")
    reason_test_logger.info("Reason INFO test message")
    reason_test_logger.warning("Reason WARNING test message")
    
    await ctx.send("✅ Logging tests complete. Check console and log files for output.")

@bot.command(name="contextstats")
async def context_stats(ctx):
    """Show context storage statistics to verify per-user isolation."""
    try:
        stats = _get_context_stats()
        
        embed = discord.Embed(
            title="🧠 Context Storage Statistics",
            color=0x00ff00,
            description="Per-user context and memory isolation status"
        )
        
        # Summary statistics
        embed.add_field(
            name="📊 Summary",
            value=f"Users with context: {stats['total_users_with_context']}\n"
                  f"Users with memory: {stats['total_users_with_memory']}",
            inline=False
        )
        
        # Context details (limit to prevent overflow)
        if stats['context_sizes']:
            context_details = []
            for user_id, size in list(stats['context_sizes'].items())[:5]:  # Show max 5 users
                context_details.append(f"User {user_id}: {size} chars")
            
            if len(stats['context_sizes']) > 5:
                context_details.append(f"... and {len(stats['context_sizes']) - 5} more users")
            
            embed.add_field(
                name="🔤 Context Sizes",
                value="\n".join(context_details) if context_details else "No contexts stored",
                inline=True
            )
        
        # Memory details
        if stats['memory_sizes']:
            memory_details = []
            for user_id, count in list(stats['memory_sizes'].items())[:5]:  # Show max 5 users
                memory_details.append(f"User {user_id}: {count} messages")
            
            if len(stats['memory_sizes']) > 5:
                memory_details.append(f"... and {len(stats['memory_sizes']) - 5} more users")
            
            embed.add_field(
                name="💬 Memory Sizes",
                value="\n".join(memory_details) if memory_details else "No memories stored",
                inline=True
            )
        
        # Current user's context status
        user_context = _get_user_context(ctx.author.id)
        user_memory = _get_user_memory(ctx.author.id)
        
        embed.add_field(
            name=f"👤 Your Context Status",
            value=f"Context: {len(user_context)} chars\n"
                  f"Memory: {len(user_memory)} messages\n"
                  f"Has context: {_has_user_context(ctx.author.id)}",
            inline=False
        )
        
        # Context isolation verification
        if len(stats['context_sizes']) > 1:
            embed.add_field(
                name="✅ Per-User Isolation",
                value=f"Context is properly isolated across {len(stats['context_sizes'])} users",
                inline=False
            )
        elif len(stats['context_sizes']) == 1:
            embed.add_field(
                name="⚠️ Single User Context",
                value="Only one user has context stored (normal for new bots)",
                inline=False
            )
        else:
            embed.add_field(
                name="ℹ️ No Context Data",
                value="No users have stored context yet",
                inline=False
            )
        
        await ctx.send(embed=embed)
        
        # Also provide raw data for detailed debugging
        await ctx.send(f"```json\n{json.dumps(stats, indent=2)[:1500]}...\n```")
        
    except Exception as e:
        logger.error(f"Context stats command failed: {e}")
        await ctx.send(f"❌ Failed to get context statistics: {e}")

@bot.command(name="clearcontext")
async def clear_context_admin(ctx, user_id: Optional[int] = None):
    """Clear context for yourself, or a specific user (owner only for others)."""
    try:
        # Check if user is trying to clear someone else's context
        if user_id is not None and user_id != ctx.author.id:
            # Only owner can clear other users' context
            if not await bot.is_owner(ctx.author):
                await ctx.send("❌ You can only clear your own context. Use `^clearcontext` without parameters.")
                return
            target_user_id = user_id
            is_clearing_other = True
        else:
            # Clearing own context (available to everyone)
            target_user_id = ctx.author.id
            is_clearing_other = False
        
        # Clear both context and memory
        _clear_user_context(target_user_id)
        _clear_user_memory(target_user_id)
        
        if is_clearing_other:
            await ctx.send(f"✅ Cleared context and memory for user {target_user_id}")
        else:
            await ctx.send(f"✅ Cleared your context and memory")
            
    except Exception as e:
        logger.error(f"Clear context command failed: {e}")
        await ctx.send(f"❌ Failed to clear context: {e}")

# ─── Event Handlers ──────────────────────────────────────────────────────────

@bot.event
async def on_command_error(ctx, error) -> None:
    """Handle command errors gracefully with user-friendly messages."""
    if isinstance(error, commands.CommandNotFound):
        return await ctx.send(f"❌ Unknown command. Use `{PREFIX}help`.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"❌ Missing arguments. Use `{PREFIX}help`.")
    
    # Log unexpected errors for debugging
    try:
        # Use Unicode-safe logging to prevent cp1252 encoding errors on Windows
        error_msg = f"Unexpected error: {_sanitize_for_log(str(error))}"
        logger.error(error_msg)
        
        # Try to log the full exception details safely
        import traceback
        tb_str = traceback.format_exception(type(error), error, error.__traceback__)
        tb_sanitized = _sanitize_for_log(''.join(tb_str))
        logger.error(f"Exception details: {tb_sanitized}")
    except Exception as log_error:
        # Final fallback if even sanitized logging fails
        logger.error(f"Failed to log error safely: {str(log_error)}")
    
    # Ensure command lock is released to prevent deadlock
    if _COMMAND_LOCK.locked():
        _COMMAND_LOCK.release()
    
    await ctx.send("⚠️ Internal error. Please try again later.")

@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle incoming messages and process commands."""
    # Ignore own messages to prevent infinite loops
    if message.author == bot.user:
        return

    # Skip auto-reason for replies to the bot
    if message.reference and getattr(message.reference, "resolved", None) and isinstance(message.reference.resolved, discord.Message) and message.reference.resolved.author == bot.user:
        return await bot.process_commands(message)
    
    # Check for bot mention in replies (alias for ^reason -s for auto-search)
    if bot.user and bot.user.mentioned_in(message) and message.reference:
        try:
            # Extract content after removing bot mention
            content = message.content
            
            # Remove bot mentions from content (handle various mention formats)
            mention_patterns = [
                f"<@{bot.user.id}>",  # Normal mention
                f"<@!{bot.user.id}>",  # Nickname mention
                f"@{bot.user.name}",  # Text mention (less common)
                f"@{bot.user.display_name}"  # Display name mention
            ]
            
            for pattern in mention_patterns:
                content = content.replace(pattern, "").strip()
            
            # If content is empty after removing mentions, skip auto-search trigger
            if not content:
                return await bot.process_commands(message)
            
            logger.info(f"Bot mention detected in reply from {message.author} - triggering reason -s (auto-search) with content: '{content[:100]}...'")
            
            # Create a new message object with the reason command
            # We'll modify the message content to simulate the reason -s command (auto-search)
            original_content = message.content
            message.content = f"{PREFIX}reason -s {content}"
            
            try:
                # Get context and invoke the reason command
                ctx = await bot.get_context(message)
                if ctx.valid:
                    await bot.invoke(ctx)
                else:
                    # Fallback if context creation fails
                    await message.reply("⚠️ Failed to process mention. Try using `^reason -s` instead.", mention_author=False)
            finally:
                # Restore original message content
                message.content = original_content
            
            return  # Don't process as regular command
                
        except Exception as e:
            logger.error(f"Failed to process bot mention as reason -s: {e}")
            await message.reply("⚠️ Failed to process mention. Try using `^reason -s` instead.", mention_author=False)
            return
    
    # Fun easter egg for bot interactions
    if message.content.strip() == "^^^":
        await message.reply("Cutie", mention_author=True)
        return
    
    # Process all other commands normally
    await bot.process_commands(message)

@bot.event
async def on_ready() -> None:
    """Bot startup sequence: sync commands and display connection info."""
    print(f"{bot.user} has connected to Discord!")
    print(f"Bot is in {len(bot.guilds)} guilds")
    
    try:
        # Register voice commands cog
        voice_commands = setup_voice_commands(bot, voice_handler)
        await bot.add_cog(voice_commands)
        print("Voice commands cog registered successfully")
        
        # Register LM commands cog
        memory_system = {
            'memory': _user_memory,  # Direct access for backward compatibility
            'context': _user_context,  # Direct access for backward compatibility  
            'remember': _remember,
            # Enhanced per-user helper functions
            'get_user_context': _get_user_context,
            'set_user_context': _set_user_context,
            'get_user_memory': _get_user_memory,
            'clear_user_context': _clear_user_context,
            'clear_user_memory': _clear_user_memory,
            'has_user_context': _has_user_context,
            'get_context_stats': _get_context_stats
        }
        video_system = {
            'extract_frames': _extract_video_frames_from_file,
            'min_frames': MIN_VIDEO_FRAMES
        }
        lm_commands = setup_lm_commands(bot, memory_system, _ddg_search, video_system)
        await bot.add_cog(lm_commands)
        print("LM commands cog registered successfully")
        
        # Register reason commands cog
        reason_commands = setup_reason_commands(bot, memory_system, _ddg_search)
        await bot.add_cog(reason_commands)
        print("Reason commands cog registered successfully")
        
        # Register search commands cog
        search_commands = setup_search_commands(bot, memory_system, _ddg_search)
        await bot.add_cog(search_commands)
        print("Search commands cog registered successfully")
        
        # Register visual analysis commands cog
        video_system = {
            'extract_video_frames': _extract_video_frames,
            'extract_video_frames_from_file': _extract_video_frames_from_file,
            'extract_gif_frames': _extract_gif_frames,
            'min_frames': MIN_VIDEO_FRAMES
        }
        twitter_system = {
            'extract_twitter_media': _extract_twitter_media
        }
        memory_system_with_send = {
            **memory_system,
            'send_limited': _send_limited
        }
        vis_commands = setup_vis_commands(bot, memory_system_with_send, video_system, twitter_system)
        await bot.add_cog(vis_commands)
        print("Visual analysis commands cog registered successfully")
        
        # Register sum commands cog
        content_system = {
            'get_content_text': _get_content_text
        }
        sum_commands = setup_sum_commands(bot, memory_system_with_send, content_system, video_system)
        await bot.add_cog(sum_commands)
        print("Sum commands cog registered successfully")
        
        # Register profile picture commands cogs
        serverpfp_commands = setup_serverpfp_commands(bot)
        await bot.add_cog(serverpfp_commands)
        print("Server profile picture commands cog registered successfully")
        
        user_commands = setup_user_commands(bot)
        await bot.add_cog(user_commands)
        print("Profile picture commands cog registered successfully")
        
        # Register permission commands cog
        perms_commands = setup_perms_commands(bot)
        await bot.add_cog(perms_commands)
        print("Permission commands cog registered successfully")
        
        # Sync slash commands globally (takes up to 1 hour to appear everywhere)
        print("Starting global slash command sync...")
        synced = await bot.tree.sync()
        print(f"Successfully synced {len(synced)} slash commands globally")
        
        # Show what commands were synced
        for cmd in synced:
            print(f"  - /{cmd.name}: {cmd.description}")
            
    except Exception as e:
        # Use Unicode-safe logging to prevent startup crashes
        try:
            error_msg = f"Failed to sync commands: {_sanitize_for_log(str(e))}"
            logger.error(error_msg)
        except Exception:
            logger.error("Failed to sync commands - logging error occurred")
        print(f"ERROR: Failed to sync slash commands: {e}")
    
    # Display bot invite link with required permissions
    if bot.user:
        print("\nInvite link with required permissions:")
        print(f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=3148864&scope=bot%20applications.commands")

@bot.event
async def on_reaction_add(reaction, user) -> None:
    """Handle reaction-based message deletion.
    
    If someone reacts with ❌ (:x:) to a bot message, delete that message.
    This allows users to clean up bot responses they don't need.
    """
    # Ignore reactions from bots (including self)
    if user.bot:
        return
    
    # Check if reaction is the X emoji (❌)
    if str(reaction.emoji) != "❌":
        return
    
    # Check if the message being reacted to is from this bot
    if reaction.message.author != bot.user:
        return
    
    try:
        # Delete the bot's message
        await reaction.message.delete()
        logger.info(f"Deleted bot message {reaction.message.id} due to ❌ reaction from user {user.id}")
    except discord.NotFound:
        # Message was already deleted
        pass
    except discord.Forbidden:
        # Bot doesn't have permission to delete messages
        logger.warning(f"Cannot delete message {reaction.message.id}: Missing permissions")
    except Exception as e:
        logger.error(f"Failed to delete message {reaction.message.id}: {e}")

# ─── Visual Analysis Commands ───────────────────────────────────────────────
# NOTE: Visual analysis command has been moved to vis.py for better organization

# ─── Bot Startup ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Verify Discord token is configured
    if not TOKEN:
        logger.error("Missing TOKEN in Meri_Token.env")
        sys.exit(1)
    
    # Display startup banner with important information
    print("\n" + "="*60)
    print("Starting Meri Bot...")
    print("="*60)
    print("\nIMPORTANT: For slash commands to work properly:")
    print("1. The bot must be invited with the 'applications.commands' scope")
    print("2. Global slash commands can take up to 1 hour to appear")
    print("3. Use ^sync <guild_id> for instant guild-specific sync (owner only)")
    print("4. The bot will print an invite link after connecting")
    print("="*60 + "\n")
    
    # Start the bot (this will block until the bot is stopped)
    bot.run(TOKEN)