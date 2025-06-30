"""
Reason Commands Module for Meri Bot

This module contains AI chat functionality with web search context and conversation memory.
Separated from main bot file for better code organization and maintainability.

Commands included:
- reason: AI chat with automatic web search context and conversation memory
"""

import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import re
from typing import List, Dict, Any

# Set up logger for reason operations
reason_logger = logging.getLogger("MeriReason")
reason_logger.setLevel(logging.DEBUG)  # Enable debug logging for full RAG tracing

# Export the setup function
__all__ = ['setup_reason_commands']


class ReasonCommands(commands.Cog):
    """Reason Commands Cog"""
    
    def __init__(self, bot, memory_system, search_system):
        self.bot = bot
        self._user_memory = memory_system['memory']
        self._user_context = memory_system['context']
        self._remember = memory_system['remember']
        self._ddg_search = search_system
        
        # Store enhanced per-user helper functions if available
        self._get_user_context = memory_system.get('get_user_context')
        self._set_user_context = memory_system.get('set_user_context')
        self._get_user_memory = memory_system.get('get_user_memory')
        self._clear_user_context = memory_system.get('clear_user_context')
        self._clear_user_memory = memory_system.get('clear_user_memory')
        self._has_user_context = memory_system.get('has_user_context')
        self._get_context_stats = memory_system.get('get_context_stats')
        
        # Get configuration from environment
        from os import getenv
        self.LMSTUDIO_CHAT_URL = getenv(
            "LMSTUDIO_CHAT_URL",
            "http://127.0.0.1:11434/v1/chat/completions"
        )
        self.LMSTUDIO_MAX_TOKENS = int(getenv("LMSTUDIO_MAX_TOKENS", "-1"))
        self.MODEL_TTL_SECONDS = int(getenv("MODEL_TTL_SECONDS", "60"))
        self.SYSTEM_PROMPT_QWEN = getenv(
            "SYSTEM_PROMPT_QWEN",
            "You are a helpful, accurate, and knowledgeable AI assistant."
        )

    async def _send_limited(self, ctx, text: str, max_posts: int = 5):
        """Send text in chunks, limiting total messages to prevent spam."""
        chunk_size = 1900
        chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
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

    def _extract_search_terms(self, user_prompt: str) -> str:
        """Extract key search terms from a user's question or prompt for RAG context."""
        # Remove common question words and phrases
        question_patterns = [
            r'^(what|how|why|when|where|who|which|can|could|would|should|do|does|did|is|are|was|were|will|tell me|explain|describe)\s+',
            r'\b(do you think|your opinion|thoughts on|what about|how about|can you|could you|please)\b',
            r'\?$'  # Remove trailing question marks
        ]
        
        search_query = user_prompt.lower()
        for pattern in question_patterns:
            search_query = re.sub(pattern, '', search_query, flags=re.IGNORECASE).strip()
        
        # Remove filler words but keep important context
        filler_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by']
        words = search_query.split()
        
        # Keep words that aren't filler words, but don't remove if it makes the query too short
        important_words = [word for word in words if word not in filler_words or len(words) <= 3]
        
        # If we filtered too much, keep more words
        if len(important_words) < 2 and len(words) > 2:
            important_words = words[:5]  # Just take first 5 words
        
        final_query = ' '.join(important_words).strip()
        
        # Fallback to original if extraction failed
        if not final_query or len(final_query) < 3:
            final_query = user_prompt
            
        return final_query

    async def _ai_extract_search_terms(self, content: str) -> str:
        """Use AI model to intelligently extract search terms for any topic."""
        if not content or len(content.strip()) < 5:
            reason_logger.warning("Content too short for AI extraction")
            return ""
        
        # Create a general-purpose prompt for any topic
        extraction_prompt = f"""Extract 2-4 key search terms from this message that would help find current, factual information about the topic. Focus on:
- Main subjects, entities, or concepts mentioned
- Specific names, places, events, or claims that can be verified
- Key facts, dates, or statements that might need fact-checking
- Important terms that would help research the topic

MESSAGE: {content}

Output ONLY search terms in this format:
<<<SEARCH_TERMS>>>term1, term2, term3<<<END_SEARCH_TERMS>>>

Examples:
- For tech: <<<SEARCH_TERMS>>>RTX 4070 Ti price 2024, GPU performance comparison<<<END_SEARCH_TERMS>>>
- For history: <<<SEARCH_TERMS>>>Battle of Waterloo 1815, Napoleon Bonaparte exile<<<END_SEARCH_TERMS>>>
- For science: <<<SEARCH_TERMS>>>quantum computing advances 2024, quantum entanglement research<<<END_SEARCH_TERMS>>>
- For current events: <<<SEARCH_TERMS>>>climate change Paris Agreement, renewable energy statistics 2024<<<END_SEARCH_TERMS>>>"""

        # Use a simple, fast model with very strict output requirements
        extraction_payload = {
            "model": "qwen/qwen3-4b",
            "messages": [
                {
                    "role": "system", 
                    "content": "You extract search terms for web verification. Output ONLY the terms between <<<SEARCH_TERMS>>> and <<<END_SEARCH_TERMS>>> flags. No explanations, no thinking, no other text."
                },
                {"role": "user", "content": extraction_prompt}
            ],
            "max_tokens": 100,
            "stream": False,
            "temperature": 0.1,
            "stop": ["<<<END_SEARCH_TERMS>>>", "\n\n", "<think>", "explanation", "reasoning"],
            "keep_alive": self.MODEL_TTL_SECONDS
        }
        
        try:
            reason_logger.info(f"Requesting AI search term extraction for content: '{content[:100]}...'")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.LMSTUDIO_CHAT_URL, json=extraction_payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        reason_logger.warning(f"AI search term extraction failed: HTTP {resp.status} - {error_text}")
                        return ""
                    
                    result = await resp.json()
                    raw_response = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                    
                    reason_logger.debug(f"Raw AI response: '{raw_response}'")
                    
                    # Extract only content between the unique flags
                    start_flag = "<<<SEARCH_TERMS>>>"
                    end_flag = "<<<END_SEARCH_TERMS>>>"
                    
                    if start_flag in raw_response:
                        # Find the start of search terms
                        start_pos = raw_response.find(start_flag) + len(start_flag)
                        
                        # Find the end of search terms
                        if end_flag in raw_response:
                            end_pos = raw_response.find(end_flag, start_pos)
                        else:
                            # If no end flag, take everything after start flag until newline
                            end_pos = raw_response.find('\n', start_pos)
                            if end_pos == -1:
                                end_pos = len(raw_response)
                        
                        # Extract and clean the search terms
                        extracted_terms = raw_response[start_pos:end_pos].strip()
                        
                        # Additional cleanup
                        extracted_terms = extracted_terms.strip('"\'.,!?()[]{}')
                        extracted_terms = re.sub(r'\s+', ' ', extracted_terms)  # Normalize whitespace
                        
                        # Validate the result - must be substantial and useful
                        if (extracted_terms and 
                            len(extracted_terms) > 8 and  # At least 8 characters
                            not extracted_terms.lower().startswith(('here', 'the search', 'i would', 'you should', 'based on', 'search for')) and
                            ' ' in extracted_terms):  # Must contain spaces (multiple words)
                            
                            reason_logger.info(f"AI successfully extracted search terms: '{extracted_terms}'")
                            return extracted_terms
                        else:
                            reason_logger.warning(f"Extracted terms failed validation: '{extracted_terms}'")
                    else:
                        reason_logger.warning(f"No search term flags found in response: '{raw_response}'")
                    
                    # Enhanced fallback extraction for failed flagging
                    reason_logger.warning("Flagged extraction failed, attempting enhanced fallback extraction")
                    
                    # Remove thinking tags and common AI artifacts
                    fallback_terms = raw_response
                    fallback_terms = re.sub(r'<think>.*?</think>', '', fallback_terms, flags=re.DOTALL)
                    fallback_terms = re.sub(r'<think>.*', '', fallback_terms, flags=re.DOTALL)
                    fallback_terms = re.sub(r'</think>', '', fallback_terms)
                    
                    # Remove common AI response patterns more aggressively
                    cleanup_patterns = [
                        r'^(okay,?\s*let\'s\s*(see|tackle this).*?\.)(.*)$',
                        r'based on.*?message',
                        r'^(search terms?:?\s*)',
                        r'^(here are.*?:)',
                        r'first,?\s*i\s*need\s*to.*?\.\s*',
                        r'the\s*(user|message)\s*(wants|mentions).*?\.\s*',
                        r'i would suggest.*?\.\s*',
                        r'to verify.*?\.\s*'
                    ]
                    
                    for pattern in cleanup_patterns:
                        fallback_terms = re.sub(pattern, r'\3' if '(.*)' in pattern else '', fallback_terms, flags=re.IGNORECASE | re.DOTALL)
                    
                    # Extract meaningful phrases manually as last resort
                    if not fallback_terms.strip() or len(fallback_terms.strip()) < 10:
                        reason_logger.warning("Fallback cleaning failed, trying manual phrase extraction")
                        manual_terms = self._extract_meaningful_phrases(content)
                        if manual_terms:
                            reason_logger.info(f"Manual phrase extraction successful: '{manual_terms}'")
                            return manual_terms
                        else:
                            reason_logger.warning("Manual phrase extraction also failed")
                            return ""
                    else:
                        # Clean up remaining artifacts
                        fallback_terms = fallback_terms.strip('"\'.,!?()\n ')
                        fallback_terms = re.sub(r'\s+', ' ', fallback_terms)  # Normalize whitespace
                    
                    if fallback_terms and len(fallback_terms) > 8 and ' ' in fallback_terms:
                        reason_logger.info(f"Fallback extraction successful: '{fallback_terms}'")
                        return fallback_terms
                    
                    reason_logger.warning(f"All extraction methods failed. Raw output: '{raw_response}'")
                    return ""
                    
        except Exception as e:
            reason_logger.error(f"AI search term extraction error: {e}")
            return ""

    def _extract_meaningful_phrases(self, content: str) -> str:
        """Extract meaningful phrases and entities from content for search - general purpose for any topic."""
        if not content:
            return ""
        
        search_phrases = []
        content_lower = content.lower()
        
        # General entity patterns (names, places, organizations)
        entity_patterns = [
            # Proper nouns and names (capitalized words)
            r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b',
            # Years and dates  
            r'\b(?:19|20)\d{2}\b',
            r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+(?:19|20)?\d{2}\b',
            # Numbers with units that might be important
            r'\b\d+(?:\.\d+)?\s*(?:million|billion|thousand|percent|%|degrees?|miles?|km|meters?|feet|inches?)\b',
        ]
        
        for pattern in entity_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                if len(match.strip()) > 2:
                    search_phrases.append(match.strip())
        
        # Extract important phrases based on context
        # Look for quoted text or phrases in quotes
        quoted_phrases = re.findall(r'"([^"]+)"', content)
        for phrase in quoted_phrases:
            if len(phrase.strip()) > 3:
                search_phrases.append(phrase.strip())
        
        # Look for titles or names followed by common indicators
        title_patterns = [
            r'(?:called|named|titled|known as)\s+"?([^".\n]+)"?',
            r'(?:the|a)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:Act|Agreement|Treaty|Law|Bill)',
            r'(?:President|King|Queen|Emperor|Prime Minister)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        ]
        
        for pattern in title_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                if len(match.strip()) > 3:
                    search_phrases.append(match.strip())
        
        # Extract important numerical facts
        fact_patterns = [
            # Prices, costs, values
            r'\$[\d,]+(?:\.\d{2})?(?:\s*(?:million|billion|thousand))?',
            # Percentages and statistics
            r'\d+(?:\.\d+)?%',
            # Measurements and specifications
            r'\d+(?:\.\d+)?\s*(?:mph|kmh|tons?|pounds?|kg|gb|tb|mb|ghz|mhz)',
        ]
        
        for pattern in fact_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                search_phrases.append(match.strip())
        
        # Clean and deduplicate phrases
        unique_phrases = []
        for phrase in search_phrases:
            phrase = phrase.strip()
            # Skip very short phrases or common words
            if (len(phrase) > 3 and 
                phrase.lower() not in ['the', 'and', 'or', 'but', 'with', 'for', 'from', 'this', 'that'] and
                phrase not in unique_phrases):
                unique_phrases.append(phrase)
        
        # Prioritize longer, more specific phrases and limit to top results
        if unique_phrases:
            unique_phrases.sort(key=len, reverse=True)
            top_phrases = unique_phrases[:3]  # Take top 3 phrases
            return ', '.join(top_phrases)
        
        return ""

    @commands.hybrid_command(name="reason", description="AI chat with optional web search context and conversation memory")
    @app_commands.describe(prompt="Your question or topic (-s for search, -m for auto-search from replied message)")
    async def lms_chat_generate(self, ctx, *, prompt: str):
        """AI chat with optional web search context and conversation memory.
        
        Features:
        - Conversation memory (5 turns per user)
        - Streaming responses for real-time output
        
        Modes:
        - Standard: ^reason <question> - Direct AI response, no search
        - Manual search: ^reason -s "search terms" <question> - Search specified terms
        - Auto search: ^reason -s <question> - AI extracts search terms from your question automatically
        - Reply search: ^reason -m <question> - Auto-search based on replied message content
        """
        if not prompt.strip():
            return await ctx.send("❌ Please provide a question or prompt.")
        
        async with ctx.typing():
            # Check for attachments first - if present, ignore reply processing
            has_attachments = bool(ctx.message.attachments)
            if has_attachments:
                reason_logger.info(f"Attachments detected ({len(ctx.message.attachments)} files) - switching to visual analysis mode")
                
                # Import and use visual analysis functionality
                try:
                    from vis import setup_vis_commands
                    
                    # Create a temporary vis commands instance with enhanced memory system
                    memory_system = {
                        'memory': self._user_memory,  # Direct access for backward compatibility
                        'context': self._user_context,  # Direct access for backward compatibility
                        'remember': self._remember,
                        'send_limited': self._send_limited,
                        # Enhanced per-user helper functions
                        'get_user_context': self._get_user_context,
                        'set_user_context': self._set_user_context,
                        'get_user_memory': self._get_user_memory,
                        'clear_user_context': self._clear_user_context,
                        'clear_user_memory': self._clear_user_memory,
                        'has_user_context': self._has_user_context,
                        'get_context_stats': self._get_context_stats
                    }
                    
                    # Import video and Twitter systems from main bot
                    from Meri_Bot import _extract_video_frames, _extract_video_frames_from_file, _extract_gif_frames, _extract_twitter_media
                    
                    video_system = {
                        'extract_video_frames': _extract_video_frames,
                        'extract_video_frames_from_file': _extract_video_frames_from_file,
                        'extract_gif_frames': _extract_gif_frames,
                        'min_frames': 5
                    }
                    
                    twitter_system = {
                        'extract_twitter_media': _extract_twitter_media
                    }
                    
                    # Create vis commands instance
                    vis_commands = setup_vis_commands(self.bot, memory_system, video_system, twitter_system)
                    
                    # Call the visual analysis command directly
                    await vis_commands.visual_analysis(ctx, content=prompt)
                    return  # Exit early after processing attachments
                    
                except Exception as e:
                    reason_logger.error(f"Failed to process attachments: {e}")
                    await ctx.send("⚠️ Failed to process attachments. Please try the `^vis` command directly.")
                    return
            
            # Parse flags FIRST (before cleaning prompt)
            original_prompt = prompt
            include_reply = prompt.strip().startswith("-m ") or " -m " in prompt or prompt.strip() == "-m"
            manual_search = prompt.strip().startswith("-s ")
            
            # Debug: Log flag detection BEFORE cleaning
            reason_logger.debug(f"Original prompt: '{original_prompt}'")
            reason_logger.debug(f"Flag detection - include_reply: {include_reply}, manual_search: {manual_search}")
            reason_logger.debug(f"Has reply reference: {ctx.message.reference is not None}")
            reason_logger.debug(f"Has attachments: {has_attachments}")
            
            # NOW clean the prompt after flag detection
            if include_reply:
                # Remove -m flag (handle different positions)
                prompt = prompt.strip()
                if prompt.startswith("-m "):
                    prompt = prompt[3:].strip()
                elif prompt.startswith("-m"):
                    prompt = prompt[2:].strip()
                else:
                    prompt = prompt.replace(" -m ", " ").replace(" -m", "").strip()
                    
                if not prompt:  # If prompt becomes empty after removing -m
                    prompt = "Is this accurate? Please analyze the content."
                    
            if manual_search:
                prompt = prompt[3:].strip()  # Remove "-s " prefix
            
            reason_logger.debug(f"Cleaned prompt: '{prompt}'")
            
            replied_context = ""
            replied_message_content = ""
            search_results_text = ""
            search_terms = ""
            
            # Handle manual search flag first (-s takes precedence)
            if manual_search:
                reason_logger.debug("Taking MANUAL SEARCH branch (-s flag detected)")
                
                # Check if this is a reply to a message AND -m is NOT used (new behavior)
                reply_context_for_search = ""
                if ctx.message.reference and ctx.message.reference.message_id and not include_reply:
                    reason_logger.debug("Manual search with reply detected - will also search replied message content")
                    try:
                        # Fetch the referenced message for additional search context
                        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                        
                        # Get message text content for search term extraction
                        if replied_msg.content:
                            reply_context_for_search = replied_msg.content
                        
                        # Include embed content for search
                        if replied_msg.embeds:
                            for embed in replied_msg.embeds:
                                if embed.title:
                                    if not reply_context_for_search:
                                        reply_context_for_search = embed.title
                                    else:
                                        reply_context_for_search += f" {embed.title}"
                                if embed.description:
                                    if reply_context_for_search:
                                        reply_context_for_search += f" {embed.description}"
                                    else:
                                        reply_context_for_search = embed.description
                        
                        reason_logger.info(f"Will extract additional search terms from replied message: '{reply_context_for_search[:100]}...'")
                        
                    except Exception as e:
                        reason_logger.warning(f"Failed to fetch replied message for additional search context: {e}")
                        reply_context_for_search = ""
                
                # Check if user provided quoted search terms or if we should auto-extract
                if prompt.startswith('"'):
                    # Manual search terms provided: "search terms" user question
                    end_quote = prompt.find('"', 1)
                    if end_quote != -1:
                        search_terms = prompt[1:end_quote]
                        user_question = prompt[end_quote+1:].strip() or search_terms
                        search_mode = "manual_quoted"
                    else:
                        # No closing quote, treat everything as search terms
                        search_terms = prompt
                        user_question = prompt
                        search_mode = "manual_unquoted"
                else:
                    # No quotes - check if this looks like it has manual search terms
                    words = prompt.split()
                    question_starters = ['what', 'how', 'why', 'when', 'where', 'who', 'which', 'can', 'could', 'should', 'is', 'are', 'do', 'does', 'will']
                    
                    split_point = None
                    for i, word in enumerate(words):
                        if word.lower() in question_starters and i > 0:
                            split_point = i
                            break
                    
                    if split_point:
                        # Found question starter after some words - treat as manual search terms + question
                        search_terms = ' '.join(words[:split_point])
                        user_question = ' '.join(words[split_point:])
                        search_mode = "manual_split"
                    else:
                        # No clear split - use auto-extraction like -m mode
                        reason_logger.info("No manual search terms detected with -s flag, using AI auto-extraction")
                        user_question = prompt
                        search_mode = "auto_extract"
                        
                        # Use AI to extract search terms from the user's question
                        search_terms = await self._ai_extract_search_terms(prompt)
                        
                        if not search_terms or len(search_terms.strip()) < 8:
                            # Fallback to manual phrase extraction
                            reason_logger.warning("AI extraction failed, trying manual phrase extraction from user prompt")
                            search_terms = self._extract_meaningful_phrases(prompt)
                            
                            if not search_terms or len(search_terms.strip()) < 8:
                                # Final fallback to simple keyword extraction
                                reason_logger.warning("Manual phrase extraction failed, using simple keyword extraction")
                                search_terms = self._extract_search_terms(prompt)
                        
                        reason_logger.info(f"Auto-extracted search terms from user prompt: '{search_terms}'")
                
                # If we have reply context, extract additional search terms and combine
                additional_search_terms = ""
                if reply_context_for_search:
                    # Use AI to extract search terms from replied message
                    additional_search_terms = await self._ai_extract_search_terms(reply_context_for_search)
                    
                    if not additional_search_terms or len(additional_search_terms.strip()) < 8:
                        # Fallback to manual phrase extraction
                        additional_search_terms = self._extract_meaningful_phrases(reply_context_for_search)
                    
                    if additional_search_terms:
                        # Combine manual search terms with extracted terms from reply
                        combined_search_terms = f"{search_terms}, {additional_search_terms}"
                        reason_logger.info(f"MANUAL SEARCH + REPLY RAG MODE: Manual terms: '{search_terms}' + Reply terms: '{additional_search_terms}'")
                        search_terms = combined_search_terms
                    else:
                        reason_logger.warning("Failed to extract additional terms from replied message")
                
                # Log the search mode and terms
                if search_mode == "auto_extract":
                    reason_logger.info(f"AUTO SEARCH MODE (-s): Using AI-extracted search terms: '{search_terms}'")
                else:
                    reason_logger.info(f"MANUAL SEARCH MODE (-s): Using {search_mode} search terms: '{search_terms}'")
                
                # Perform search (manual or auto-extracted terms)
                try:
                    reason_logger.debug(f"Starting search with terms: '{search_terms}'")
                    data = self._ddg_search(search_terms, max_results=5)
                    reason_logger.debug(f"Search returned {len(data) if data else 0} results")
                    if data:
                        snippets = []
                        for itm in data:
                            title = itm.get('title') or 'No title'
                            snippet = itm.get('body') or itm.get('snippet') or 'No description'
                            url = itm.get('href') or itm.get('url') or 'No URL'
                            snippets.append(f"{title}: {snippet} ({url})")
                        
                        if reply_context_for_search:
                            # Enhanced search results text when reply context is included
                            search_results_text = f"""Search results for '{search_terms}':
{chr(10).join(snippets)}

REPLIED MESSAGE CONTEXT: "{reply_context_for_search}"

TASK: Use the search results above to provide context and answer the user's question while also analyzing the replied message content."""
                        else:
                            if search_mode == "auto_extract":
                                search_results_text = f"Auto-extracted search results for '{search_terms}':\n" + "\n".join(snippets)
                            else:
                                search_results_text = f"Manual search results for '{search_terms}':\n" + "\n".join(snippets)
                        
                        reason_logger.info(f"Search successful: found {len(data)} results")
                    else:
                        search_results_text = f"Note: Search for '{search_terms}' returned no results. Please provide the best answer based on your training data."
                        reason_logger.warning(f"Search returned no results for: {search_terms}")
                except Exception as e:
                    search_results_text = f"Note: Search for '{search_terms}' failed. Please provide the best answer based on your training data."
                    reason_logger.error(f"Search failed: {e}")
                
                final_prompt = user_question
                
                # Send brief status update
                try:
                    if reply_context_for_search:
                        status_msg = await ctx.send(f"🔍 **Search + Reply RAG:** {search_terms[:100]}{'...' if len(search_terms) > 100 else ''}")
                    elif search_mode == "auto_extract":
                        status_msg = await ctx.send(f"🧠 **Auto search:** {search_terms[:100]}{'...' if len(search_terms) > 100 else ''}")
                    else:
                        status_msg = await ctx.send(f"🔍 **Manual search:** {search_terms}")
                    await asyncio.sleep(1.5)
                    await status_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
                    
            elif include_reply:
                reason_logger.debug("Taking INCLUDE_REPLY branch (-m flag detected)")
                
                # Check if this message is a reply
                if not ctx.message.reference or not ctx.message.reference.message_id:
                    await ctx.send("⚠️ The -m flag requires replying to a message.")
                    return
                
                try:
                    # Fetch the referenced message
                    replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                    context_parts = []
                    
                    # Get message text content and store it for search term extraction
                    if replied_msg.content:
                        replied_message_content = replied_msg.content  # Store for search term extraction
                        context_parts.append(f"Message content: {replied_msg.content}")
                    
                    # Document any attachments
                    if replied_msg.attachments:
                        for attachment in replied_msg.attachments:
                            if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']):
                                context_parts.append(f"[Image attachment: {attachment.filename}]")
                            else:
                                context_parts.append(f"[File attachment: {attachment.filename}]")
                    
                    # Include embed information (links, previews)
                    if replied_msg.embeds:
                        for embed in replied_msg.embeds:
                            if embed.title:
                                context_parts.append(f"Embed title: {embed.title}")
                                # Also include embed content for search term extraction
                                if embed.title and not replied_message_content:
                                    replied_message_content = embed.title
                            if embed.description:
                                context_parts.append(f"Embed description: {embed.description}")
                                # Include description for search term extraction
                                if embed.description:
                                    replied_message_content += f" {embed.description}" if replied_message_content else embed.description
                            if embed.url:
                                context_parts.append(f"Embed URL: {embed.url}")
                    
                    if context_parts:
                        replied_context = "Context from referenced message:\n" + "\n".join(context_parts)
                        
                except Exception as e:
                    reason_logger.error(f"Failed to fetch replied message: {e}")
                    await ctx.send("⚠️ Could not fetch the referenced message.")
                    return
                
                # Perform automatic web search using extracted terms from replied message
                final_prompt = prompt.strip()  # Keep the original user question
                
                reason_logger.info(f"USING -m FLAG: Analyzing replied message content for search terms")
                reason_logger.info(f"Replied message content: '{replied_message_content[:200]}...'")
                reason_logger.info(f"User prompt: '{final_prompt}'")
                
                # Use AI model to intelligently extract search terms from replied message
                search_terms = await self._ai_extract_search_terms(replied_message_content)
                search_strategy = "ai_extraction"
                
                reason_logger.info(f"AI extracted terms: '{search_terms}'")
                
                if not search_terms or len(search_terms.strip()) < 8:
                    # Fallback: try manual phrase extraction from replied message
                    reason_logger.warning(f"AI extraction failed or returned insufficient terms. Trying manual phrase extraction from replied message.")
                    fallback_terms = self._extract_meaningful_phrases(replied_message_content)
                    
                    if fallback_terms and len(fallback_terms.strip()) >= 8:
                        search_terms = fallback_terms
                        search_strategy = "manual_phrase_extraction"
                        reason_logger.info(f"Manual phrase extraction from replied message successful: '{search_terms}'")
                    else:
                        # Final fallback to simple keyword extraction from reply
                        reason_logger.warning(f"Manual phrase extraction also failed. Trying simple keyword extraction.")
                        simple_terms = self._extract_search_terms(replied_message_content)
                        if simple_terms and len(simple_terms.strip()) >= 3:
                            search_terms = simple_terms
                            search_strategy = "simple_keyword_extraction"
                            reason_logger.info(f"Simple keyword extraction successful: '{search_terms}'")
                        else:
                            # Absolute final fallback to user prompt
                            reason_logger.warning(f"All extraction methods failed. Final fallback to user prompt.")
                            search_terms = self._extract_search_terms(final_prompt)
                            search_strategy = "user_prompt_fallback"
                            reason_logger.info(f"User prompt fallback search terms: '{search_terms}'")
                else:
                    reason_logger.info(f"Successfully using AI-extracted search terms: '{search_terms}'")
                
                # Perform web search with extracted terms
                snippets = []
                try:
                    reason_logger.info(f"Performing web search for context: {search_terms}")
                    
                    data = self._ddg_search(search_terms, max_results=5)
                    
                    if data:
                        for itm in data:
                            title = itm.get('title') or 'No title'
                            snippet = itm.get('body') or itm.get('snippet') or 'No description'
                            url = itm.get('href') or itm.get('url') or 'No URL'
                            snippets.append(f"{title}: {snippet} ({url})")
                        reason_logger.info(f"Web search successful: found {len(data)} results for '{search_terms}'")
                    else:
                        reason_logger.warning(f"Web search returned no results for terms: {search_terms}")
                        
                        # Try simplified search for long queries
                        if len(search_terms) > 50:
                            simplified_terms = ' '.join(search_terms.split()[:3])  # First 3 words only
                            reason_logger.info(f"Trying simplified search terms: {simplified_terms}")
                            try:
                                simplified_data = self._ddg_search(simplified_terms, max_results=3)
                                if simplified_data:
                                    for itm in simplified_data:
                                        title = itm.get('title') or 'No title'
                                        snippet = itm.get('body') or itm.get('snippet') or 'No description'
                                        url = itm.get('href') or itm.get('url') or 'No URL'
                                        snippets.append(f"{title}: {snippet} ({url})")
                                    reason_logger.info(f"Simplified search successful: found {len(simplified_data)} results")
                            except Exception as simplified_error:
                                reason_logger.warning(f"Simplified search failed: {simplified_error}")
                        
                except Exception as e:
                    reason_logger.error(f"Web search failed for terms '{search_terms}': {e}")
                
                # Build search results text for -m flag RAG
                if snippets:
                    search_results_text = f"""RESEARCH CONTEXT (for analyzing the replied message):
Search terms used: {search_terms}
Current web information:

{chr(10).join(snippets)}

TASK: Use this current web information to analyze and verify the claims in the replied message that the user is asking about."""
                else:
                    # Inform the AI that web search is unavailable
                    search_results_text = f"RESEARCH CONTEXT: Unable to search for '{search_terms}' due to web search unavailability. Please analyze the replied message based on your training data and clearly indicate information limitations."
                
                # Brief status update to user about RAG search process
                try:
                    if search_strategy == "ai_extraction":
                        status_msg = await ctx.send(f"🧠 **Step 1/3:** AI extracted search terms: `{search_terms}`\n🔍 **Step 2/3:** Searching web for current information...\n🤖 **Step 3/3:** Processing with RAG...")
                    elif search_strategy == "manual_phrase_extraction":
                        status_msg = await ctx.send(f"🔍 **Step 1/3:** Extracted key phrases: `{search_terms}`\n🔍 **Step 2/3:** Searching web for current information...\n🤖 **Step 3/3:** Processing with RAG...")
                    else:
                        status_msg = await ctx.send(f"🔍 **Searching:** {search_terms}\n⚠️ Fallback: using simplified extraction")
                    await asyncio.sleep(3.0)  # Show longer for RAG process explanation
                    await status_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass  # Ignore if status message fails
            
            else:
                # Standard mode: NO automatic search, just AI response
                reason_logger.debug("Taking STANDARD MODE branch (no flags detected)")
                reason_logger.debug(f"Message has reply reference: {ctx.message.reference is not None}")
                
                final_prompt = prompt.strip()
                reason_logger.info(f"STANDARD MODE: No automatic search, direct AI response")

            # Build final prompt with proper RAG structure
            if manual_search:
                # For manual search, structure as search + question
                reason_logger.info(f"Manual search mode: prompt='{final_prompt}'")
                
            elif include_reply:
                # For -m flag: RAG flow - search results will analyze the replied message
                final_prompt = f"""Analyze this message that I'm replying to using the current web research:

REPLIED MESSAGE: "{replied_message_content}"

MY QUESTION: {final_prompt}

Please use the provided research context to verify, fact-check, or provide current information about the claims and topics in the replied message, then answer my question accurately."""
                reason_logger.info(f"RAG mode: analyzing replied message with web research")
                
            else:
                # Standard mode: direct question
                reason_logger.info(f"Standard mode: direct response")

            # Prepare conversation with memory and search results
            history = self._user_memory.get(ctx.author.id, [])  # Get user's conversation history
            
            # Choose appropriate system prompt based on mode
            if include_reply and search_results_text:
                # RAG mode system prompt
                system_prompt = """You are an intelligent research assistant with confident analytical abilities. When provided with RESEARCH CONTEXT, use that current web information as your primary source to analyze, verify, and fact-check content with definitive conclusions.

Key guidelines:
1. ALWAYS prioritize the provided research context over your training data
2. Use the current web information to make confident assessments and provide definitive answers when evidence supports them
3. When research context contradicts claims, state this clearly and decisively
4. Provide direct, conclusive statements about what the research demonstrates rather than hedging unnecessarily
5. Structure your response with clear facts from research and confident analysis/conclusions
6. Express uncertainty only when the research context is genuinely ambiguous or contradictory

Focus on being thorough, accurate, and decisively evidence-based using the provided research."""
            else:
                # Standard mode
                system_prompt = self.SYSTEM_PROMPT_QWEN
            
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add conversation history first to maintain conversation flow
            messages += history
            
            # Add search results as RAG context (assistant message) ONLY if we have them
            if search_results_text and search_results_text.strip():
                messages.append({"role": "assistant", "content": search_results_text})
                reason_logger.info(f"Added search results as RAG context")
            else:
                reason_logger.info(f"No search results - direct AI response mode")
            
            # Add current user question
            messages.append({"role": "user", "content": final_prompt})
            
            # Debug: Log the complete message structure for RAG verification
            if include_reply and search_results_text:
                reason_logger.info("=== RAG MESSAGE FLOW ===")
                reason_logger.info(f"1. System prompt: {system_prompt[:100]}...")
                reason_logger.info(f"2. History messages: {len(history)}")
                reason_logger.info(f"3. Research context length: {len(search_results_text)}")
                reason_logger.info(f"4. Final user prompt length: {len(final_prompt)}")
                reason_logger.info(f"5. Total messages to AI: {len(messages)}")
                reason_logger.info("=== END RAG FLOW ===")
            
            # Configure API request for streaming
            payload = {
                "model": "qwen/qwen3-4b",
                "messages": messages,
                "max_tokens": self.LMSTUDIO_MAX_TOKENS,
                "stream": True,  # Enable real-time streaming
                "keep_alive": self.MODEL_TTL_SECONDS  # Auto-unload model after configured TTL
            }
            
            # Initialize response accumulator with explicit cleanup
            accumulated = ""
            stream_complete = False
            
            try:
                # Set up streaming connection to AI API
                timeout = aiohttp.ClientTimeout(total=None)  # No timeout for streaming
                headers = {"Accept": "text/event-stream"}  # Request streaming format
                
                reason_logger.debug("Starting streaming request to AI API")
                
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.LMSTUDIO_CHAT_URL, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            reason_logger.error(f"Chat API HTTP {resp.status}: {err}")
                            return await ctx.send(f"❌ API error {resp.status}")
                        
                        # Process Server-Sent Events (SSE) stream with complete consumption
                        reason_logger.debug("Processing SSE stream")
                        chunk_count = 0
                        
                        while True:
                            try:
                                raw = await resp.content.readline()
                                if not raw:
                                    reason_logger.debug("Stream ended - no more data")
                                    stream_complete = True
                                    break  # Stream ended
                                    
                                line = raw.decode('utf-8').strip()
                                if not line.startswith('data:'):
                                    continue  # Skip non-data lines
                                    
                                data_str = line[len('data:'):].strip()
                                
                                # Handle special SSE termination markers
                                if data_str == '[DONE]':
                                    reason_logger.debug("Received [DONE] marker - stream complete")
                                    stream_complete = True
                                    break
                                    
                                try:
                                    # Parse JSON chunk from stream
                                    obj = json.loads(data_str)
                                    delta = obj.get('choices', [{}])[0].get('delta', {})
                                    part = delta.get('content', '')
                                    
                                    if part:
                                        accumulated += part  # Build response incrementally
                                        chunk_count += 1
                                    
                                    # Check if streaming is complete
                                    finish_reason = obj.get('choices', [{}])[0].get('finish_reason')
                                    if finish_reason == 'stop':
                                        reason_logger.debug(f"Received stop signal - stream complete after {chunk_count} chunks")
                                        stream_complete = True
                                        break
                                        
                                except json.JSONDecodeError as je:
                                    reason_logger.warning(f"Failed to parse JSON chunk: {data_str[:100]}...")
                                    continue
                                except Exception as parse_error:
                                    reason_logger.warning(f"Error parsing SSE data: {parse_error}")
                                    continue
                                    
                            except Exception as read_error:
                                reason_logger.error(f"Error reading from stream: {read_error}")
                                break
                        
                        # Ensure we've consumed the entire stream
                        if not stream_complete:
                            reason_logger.warning("Stream did not complete properly - attempting to drain remaining data")
                            try:
                                # Read any remaining data to fully close the stream
                                remaining_data = await resp.read()
                                if remaining_data:
                                    reason_logger.debug(f"Drained {len(remaining_data)} bytes of remaining stream data")
                            except Exception as drain_error:
                                reason_logger.warning(f"Failed to drain remaining stream data: {drain_error}")
                        
                        reason_logger.debug(f"Stream processing complete - accumulated {len(accumulated)} characters from {chunk_count} chunks")
                
                # Validate response before processing
                if not accumulated:
                    reason_logger.warning("Empty response accumulated from stream")
                    return await ctx.send("❌ Empty chat response.")
                
                reason_logger.debug(f"Processing accumulated response: {len(accumulated)} characters")
                
                # Clean up AI reasoning artifacts and formatting
                original_length = len(accumulated)
                accumulated = re.sub(r"<think>.*?</think>", "", accumulated, flags=re.DOTALL)  # Remove thinking tags
                accumulated = re.sub(r"<think>.*", "", accumulated, flags=re.DOTALL)  # Remove incomplete thinking
                accumulated = accumulated.replace("<think>", "").replace("</think>", "")  # Remove stray tags
                
                # Remove common chain-of-thought markers
                for marker in ["FINAL ANSWER:", "Final Answer:", "### Answer", "Answer:"]:
                    if marker in accumulated:
                        accumulated = accumulated.split(marker)[-1].strip()
                
                # Clean up LaTeX formatting
                accumulated = re.sub(r"\\boxed\s*{([^}]+)}", r"\1", accumulated)
                accumulated = accumulated.replace("\\boxed", "").strip()
                
                # Final validation after cleanup
                if not accumulated.strip():
                    reason_logger.warning(f"Response became empty after cleanup (was {original_length} chars)")
                    return await ctx.send("❌ Response was empty after processing.")
                
                reason_logger.debug(f"Cleaned response: {len(accumulated)} characters (was {original_length})")
                
                # Send response in Discord-friendly chunks
                await self._send_limited(ctx, accumulated)

                # Save conversation to memory for context
                self._remember(ctx.author.id, "user", final_prompt)
                self._remember(ctx.author.id, "assistant", accumulated)
                self._user_context[ctx.author.id] = accumulated  # Store for cross-command context
                
                reason_logger.debug("Response sent and saved to memory successfully")
                
            except Exception as e:
                reason_logger.error("Streaming chat API error", exc_info=e)
                await ctx.send(f"⚠️ Chat API request failed: {e}")
            finally:
                # Explicit cleanup to prevent data leakage between commands
                accumulated = ""
                stream_complete = False
                reason_logger.debug("Cleanup complete - response cache cleared")


def setup_reason_commands(bot, memory_system, search_system):
    """Setup function to add reason commands to the bot"""
    reason_commands = ReasonCommands(bot, memory_system, search_system)
    return reason_commands 