"""
Search Commands Module for Meri Bot

This module contains web search functionality with AI summarization.
Separated from main bot file for better code organization and maintainability.

Commands included:
- search: Search the web and get AI summary using LLaMA model
"""

import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import re
import requests
from html import unescape
from typing import List, Dict, Any

# Set up logger for search operations
search_logger = logging.getLogger("MeriSearch")


class SearchCommands(commands.Cog):
    """Search Commands Cog"""
    
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
        self.MODEL_TTL_SECONDS = int(getenv("MODEL_TTL_SECONDS", "60"))

    @commands.hybrid_command(name="search", description="Search the web and get AI summary")
    @app_commands.describe(query="What to search for")
    async def llama_search_summarize(self, ctx, *, query: str):
        """Search the web and generate a summary using the local LLaMA model (LM Studio)."""
        if not query.strip():
            return await ctx.send("❌ Please provide a search query.")
        async with ctx.typing():
            # 1) Perform DuckDuckGo search (reuse logic from `web_search`)
            raw_results: List[dict] = []
            search_successful = False
            
            try:
                loop = asyncio.get_running_loop()
                def run_search():
                    return self._ddg_search(query, max_results=15)  # Get more results to ensure we have enough good links
                raw_results = await loop.run_in_executor(None, run_search)
                if raw_results:
                    search_successful = True
                    search_logger.info(f"Search successful: found {len(raw_results)} results")
                else:
                    search_logger.warning("Search returned no results")
            except Exception as e:
                search_logger.error(f"Search failed: {e}")
            
            # If no results found, provide helpful feedback but continue with empty context
            if not raw_results:
                search_logger.warning(f"No search results found for query: {query[:100]}...")
                
                # Try a simplified version of the query for very long queries
                if len(query) > 100:
                    simplified_query = ' '.join(query.split()[:5])  # First 5 words
                    search_logger.info(f"Trying simplified search: {simplified_query}")
                    try:
                        def run_simple_search():
                            return self._ddg_search(simplified_query, max_results=5)
                        raw_results = await loop.run_in_executor(None, run_simple_search)
                        if raw_results:
                            search_successful = True
                            search_logger.info(f"Simplified search successful: found {len(raw_results)} results")
                    except Exception as e:
                        search_logger.error(f"Simplified search also failed: {e}")
                
                # If still no results, inform the user but continue
                if not raw_results:
                    await ctx.send("⚠️ Web search is currently unavailable. Generating response from AI knowledge base...")
                    # Create a mock summary without web results
                    lm_messages = [
                        {"role": "system", "content": "You are a helpful assistant with confident expertise. The user has asked a question but web search is currently unavailable. Provide the best definitive answer you can based on your training data, stating facts clearly and directly when you are confident in them. Mention that current web information is not available, but don't let this prevent you from giving a thorough, authoritative response when your knowledge allows."},
                        {"role": "user", "content": f"Question: {query}\n\nPlease provide a helpful, confident answer based on your knowledge. Note that current web search results are not available."}
                    ]
                else:
                    # We have some results from simplified search
                    await ctx.send(f"ℹ️ Using simplified search results for: {simplified_query}")
            else:
                # We have good results from the original search
                lm_messages = None  # Will be set below

            # 2) Compose context for the language model (limit to top 5 results)
            context_lines: List[str] = []
            for idx, item in enumerate(raw_results[:5], start=1):
                title = item.get("title") or item.get("heading") or "Untitled"
                snippet = (item.get("snippet") or item.get("body") or "")[:300]
                url = item.get("url") or item.get("href") or item.get("link") or ""
                context_lines.append(f"Result {idx}: {title}\nURL: {url}\nSnippet: {snippet}")

            if lm_messages is None:
                lm_messages = [
                    {"role": "system", "content": "You are a helpful assistant that reads search results and provides definitive, factual answers and summaries. Synthesize the search results confidently, making clear assertions when the evidence supports them. Present information decisively rather than with unnecessary hedging or uncertainty."},
                    {"role": "user", "content": f"Search query: {query}\n\nHere are the top search results:\n\n" + "\n\n".join(context_lines) + "\n\nPlease provide a confident, definitive summary (max 200 words) answering the user's query based on the results above. State facts clearly and make direct conclusions when the evidence supports them."}
                ]

            payload = {
                "model": "qwen/qwen3-4b",
                "messages": lm_messages,
                "stream": True,
                "max_tokens": 4096,
                "keep_alive": self.MODEL_TTL_SECONDS  # Unload model after configured TTL
            }

            try:
                timeout = aiohttp.ClientTimeout(total=None)
                headers = {"Accept": "text/event-stream"}
                accumulated = ""
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.LMSTUDIO_CHAT_URL, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            search_logger.error(f"Chat API HTTP {resp.status}: {err}")
                            return await ctx.send(f"❌ LLaMA API error {resp.status}")
                        while True:
                            raw = await resp.content.readline()
                            if not raw:
                                break
                            line = raw.decode('utf-8').strip()
                            if not line.startswith('data:'):
                                continue
                            data_str = line[len('data:'):].strip()
                            try:
                                obj = json.loads(data_str)
                                part = obj.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                if part:
                                    accumulated += part
                                if obj.get('choices', [{}])[0].get('finish_reason') == 'stop':
                                    break
                            except Exception as se:
                                # search_logger.error(f"SSE parse error: {se}")
                                continue

                # Strip reasoning
                accumulated = re.sub(r"<think>.*?</think>", "", accumulated, flags=re.DOTALL)
                # Also remove any unmatched <think> tag left over
                accumulated = re.sub(r"<think>.*", "", accumulated, flags=re.DOTALL)
                accumulated = accumulated.replace("<think>", "").replace("</think>", "")
                for marker in ["FINAL ANSWER:", "Final Answer:", "### Answer", "Answer:"]:
                    if marker in accumulated:
                        accumulated = accumulated.split(marker)[-1].strip()
                # Remove LaTeX boxed answers
                accumulated = re.sub(r"\\boxed\s*{([^}]+)}", r"\1", accumulated)
                accumulated = accumulated.replace("\\boxed", "").strip()

                summary_text = accumulated
                if not summary_text:
                    return await ctx.send("⚠️ Empty summary returned.")

                # 3) Send formatted embed with summary and source links
                embed = discord.Embed(title=f"Summary for '{query}'", description=summary_text[:2048], color=0x9b59b6)
                
                # Filter and validate results to ensure we have good links with titles and URLs
                valid_results = []
                for item in raw_results:
                    title = item.get("title") or item.get("heading") or ""
                    url = item.get("url") or item.get("href") or item.get("link") or ""
                    
                    # Only include results with both title and URL
                    if title.strip() and url.strip() and url.startswith(("http://", "https://")):
                        # Clean up title
                        title = title.strip()[:100]  # Limit title length
                        if title == "":
                            title = "Search Result"
                        valid_results.append({"title": title, "url": url})
                    
                    # Stop when we have enough valid results
                    if len(valid_results) >= 5:  # Get up to 5 good results
                        break
                
                # Display at least 3 links (or all if fewer than 3)
                links_to_show = max(3, min(len(valid_results), 5))  # Show 3-5 links
                for i, item in enumerate(valid_results[:links_to_show]):
                    embed.add_field(
                        name=f"🔗 {item['title']}", 
                        value=item['url'], 
                        inline=False
                    )
                
                # Add footer with result count
                result_count = len(valid_results)
                footer_text = f"Requested by {ctx.author.display_name}"
                if result_count > 0:
                    footer_text += f" • {result_count} sources found"
                embed.set_footer(text=footer_text)
                
                try:
                    await ctx.send(embed=embed)
                except discord.HTTPException as embed_error:
                    # If embed sending fails (e.g., due to message reference issues), send as plain text
                    search_logger.warning(f"Failed to send embed, falling back to plain text: {embed_error}")
                    
                    # Convert embed to plain text format
                    response_text = f"**Summary for '{query}'**\n\n{summary_text}\n\n"
                    
                    # Add source links as plain text
                    response_text += "**Sources:**\n"
                    for i, item in enumerate(valid_results[:links_to_show], 1):
                        response_text += f"{i}. {item['title']}\n{item['url']}\n\n"
                    
                    if result_count > 0:
                        response_text += f"*{result_count} sources found*"
                    
                    try:
                        await ctx.send(response_text)
                    except discord.HTTPException as text_error:
                        search_logger.error(f"Failed to send even plain text response: {text_error}")
                        # Try one more time with a minimal message
                        try:
                            await ctx.send(f"Search completed but failed to send full results. Summary: {summary_text[:500]}...")
                        except discord.HTTPException:
                            search_logger.error("Complete failure to send any response")
                            raise  # Let the main error handler deal with it

                # Store interaction in memory (optional)
                self._remember(ctx.author.id, "user", query)
                self._remember(ctx.author.id, "assistant", summary_text)
                # Use enhanced context storage for per-user isolation
                if self._set_user_context:
                    self._set_user_context(ctx.author.id, summary_text)
                else:
                    # Fallback to direct access if enhanced functions not available
                    self._user_context[ctx.author.id] = summary_text
            except Exception as e:
                search_logger.error("LLaMA summarization error", exc_info=e)
                try:
                    await ctx.send(f"⚠️ Failed to generate summary: {e}")
                except discord.HTTPException as send_error:
                    search_logger.error(f"Failed to send error message: {send_error}")
                    # Don't re-raise here to prevent cascading failures in error handler


def setup_search_commands(bot, memory_system, search_system):
    """Setup function to add search commands to the bot"""
    search_commands = SearchCommands(bot, memory_system, search_system)
    return search_commands 