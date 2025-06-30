"""
Sum Commands Module for Meri Bot

This module contains URL content summarization functionality with video frame analysis.
Separated from main bot file for better code organization and maintainability.

Commands included:
- sum: Summarize web content from URLs, text files, and PDF files with optional visual analysis and reply context
"""

import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import re
import subprocess
import tempfile
import os
from typing import List, Any, Dict

# Try to import PDF processing library
try:
    import PyPDF2  # type: ignore
    _PDF_AVAILABLE = True
except ImportError:
    try:
        import pypdf  # type: ignore
        import pypdf as PyPDF2  # Use pypdf as PyPDF2 for compatibility
        _PDF_AVAILABLE = True
    except ImportError:
        PyPDF2 = None  # type: ignore
        _PDF_AVAILABLE = False

# Set up logger for sum operations
sum_logger = logging.getLogger("MeriSum")


class SumCommands(commands.Cog):
    """Sum Commands Cog"""
    
    def __init__(self, bot, memory_system, content_system, video_system):
        self.bot = bot
        self._user_memory = memory_system['memory']
        self._user_context = memory_system['context']
        self._remember = memory_system['remember']
        self._send_limited = memory_system['send_limited']
        self._get_content_text = content_system['get_content_text']
        self._extract_video_frames = video_system['extract_video_frames']
        self.MIN_VIDEO_FRAMES = video_system['min_frames']
        
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
            "You are an intelligent assistant specialized in analyzing and synthesizing information."
        )

    async def _extract_text_from_pdf(self, pdf_data: bytes) -> str:
        """Extract text content from PDF file data.
        
        Args:
            pdf_data: Raw PDF file bytes
            
        Returns:
            Extracted text content or error message
        """
        if not _PDF_AVAILABLE:
            return "[PDF processing not available - PyPDF2 or pypdf library required]"
        
        try:
            # Save PDF data to temporary file
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
                temp_file.write(pdf_data)
                temp_file_path = temp_file.name
            
            try:
                # Extract text using PyPDF2/pypdf
                with open(temp_file_path, 'rb') as pdf_file:
                    pdf_reader = PyPDF2.PdfReader(pdf_file)  # type: ignore
                    
                    if not pdf_reader.pages:
                        return "[Empty PDF - no pages found]"
                    
                    # Limit to first 50 pages to avoid memory issues
                    max_pages = min(len(pdf_reader.pages), 50)
                    text_content = []
                    
                    for page_num in range(max_pages):
                        try:
                            page = pdf_reader.pages[page_num]
                            page_text = page.extract_text()
                            if page_text.strip():
                                text_content.append(f"--- Page {page_num + 1} ---\n{page_text.strip()}")
                        except Exception as e:
                            sum_logger.warning(f"Failed to extract text from page {page_num + 1}: {e}")
                            continue
                    
                    if not text_content:
                        return "[PDF text extraction failed - no readable text found]"
                    
                    extracted_text = "\n\n".join(text_content)
                    
                    # Limit total text length
                    max_chars = 50000  # ~50KB limit
                    if len(extracted_text) > max_chars:
                        extracted_text = extracted_text[:max_chars] + f"\n\n[Content truncated - showing first {max_pages} pages, ~{max_chars} characters]"
                    
                    sum_logger.info(f"Successfully extracted {len(extracted_text)} characters from PDF ({max_pages} pages)")
                    return extracted_text
                    
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
                    
        except Exception as e:
            sum_logger.error(f"PDF text extraction failed: {e}")
            return f"[PDF processing error: {str(e)}]"

    async def _extract_text_from_file(self, file_data: bytes, filename: str) -> str:
        """Extract text content from text file data.
        
        Args:
            file_data: Raw file bytes
            filename: Original filename for encoding detection
            
        Returns:
            Extracted text content or error message
        """
        try:
            # Try different encodings
            encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252', 'ascii']
            
            for encoding in encodings:
                try:
                    text_content = file_data.decode(encoding)
                    
                    # Basic validation - check if content looks reasonable
                    if len(text_content.strip()) == 0:
                        continue
                        
                    # Limit text length
                    max_chars = 50000  # ~50KB limit
                    if len(text_content) > max_chars:
                        text_content = text_content[:max_chars] + f"\n\n[Content truncated at {max_chars} characters]"
                    
                    sum_logger.info(f"Successfully extracted {len(text_content)} characters from text file using {encoding} encoding")
                    return text_content
                    
                except UnicodeDecodeError:
                    continue
            
            # If all encodings failed
            return f"[Text file processing failed - unable to decode {filename} with common encodings]"
            
        except Exception as e:
            sum_logger.error(f"Text file processing failed: {e}")
            return f"[Text file processing error: {str(e)}]"

    @commands.max_concurrency(1, per=commands.BucketType.default, wait=True)
    @commands.hybrid_command(name="sum", description="Summarize web content, PDF files, text files")
    @app_commands.describe(prompt_and_url="URL to summarize, attach files, or use '-vis' for video, '-m' for replied msg, and/or question")
    async def summarize_url(self, ctx, *, prompt_and_url: str = ""):
        """Summarize a YouTube link, webpage, text file, or PDF with optional question.\n
        You can ask a specific question before the URL, e.g.:\n
            ^sum [prompt] https://youtu.be/xyz\n
            ^sum -vis https://youtu.be/xyz (analyzes video frames)\n
            ^sum -vis What techniques are shown? https://youtu.be/xyz\n
            ^sum -m (reply to a message with a URL/file to summarize it)\n
            ^sum What are the key points? [attach PDF or text file]\n
        If no question is provided, the bot will summarize normally."""
        
        # Check if we have file attachments or URL/prompt
        has_attachments = bool(ctx.message.attachments)
        has_text_input = bool(prompt_and_url.strip())
        
        if not has_attachments and not has_text_input:
            return await ctx.send("❌ Please provide a URL to summarize or attach a text/PDF file.")
        
        async with ctx.typing():
            # Parse all flags before processing
            original_input = prompt_and_url
            
            # Check for -m flag to get URL from replied message
            include_reply = "-m" in prompt_and_url
            if include_reply:
                prompt_and_url = prompt_and_url.replace("-m", "").strip()
            
            # Check for -vis flag
            visual_mode = "-vis" in prompt_and_url
            if visual_mode:
                prompt_and_url = prompt_and_url.replace("-vis", "").strip()
            
            replied_url = ""
            replied_context = ""
            file_content = ""
            content_source = ""
            
            # Handle file attachments first
            if has_attachments:
                processed_files = []
                
                for attachment in ctx.message.attachments:
                    filename_lower = attachment.filename.lower()
                    
                    # Check if it's a supported file type
                    if filename_lower.endswith('.pdf'):
                        if not _PDF_AVAILABLE:
                            await ctx.send("⚠️ PDF processing not available. Please install PyPDF2 or pypdf library.")
                            continue
                            
                        try:
                            processing_msg = await ctx.send(f"📄 Processing PDF file: {attachment.filename}...")
                            pdf_data = await attachment.read()
                            extracted_text = await self._extract_text_from_pdf(pdf_data)
                            processed_files.append(f"=== PDF: {attachment.filename} ===\n{extracted_text}")
                            
                            await processing_msg.edit(content=f"✅ Successfully processed PDF: {attachment.filename}")
                            await asyncio.sleep(1)
                            try:
                                await processing_msg.delete()
                            except:
                                pass
                                
                        except Exception as e:
                            sum_logger.error(f"Failed to process PDF {attachment.filename}: {e}")
                            await ctx.send(f"❌ Failed to process PDF {attachment.filename}: {str(e)}")
                            continue
                    
                    elif any(filename_lower.endswith(ext) for ext in ['.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml', '.csv', '.log']):
                        try:
                            processing_msg = await ctx.send(f"📝 Processing text file: {attachment.filename}...")
                            file_data = await attachment.read()
                            extracted_text = await self._extract_text_from_file(file_data, attachment.filename)
                            processed_files.append(f"=== Text File: {attachment.filename} ===\n{extracted_text}")
                            
                            await processing_msg.edit(content=f"✅ Successfully processed text file: {attachment.filename}")
                            await asyncio.sleep(1)
                            try:
                                await processing_msg.delete()
                            except:
                                pass
                                
                        except Exception as e:
                            sum_logger.error(f"Failed to process text file {attachment.filename}: {e}")
                            await ctx.send(f"❌ Failed to process text file {attachment.filename}: {str(e)}")
                            continue
                    
                    else:
                        await ctx.send(f"⚠️ Unsupported file type: {attachment.filename}. Supported types: PDF, TXT, MD, PY, JS, HTML, CSS, JSON, XML, CSV, LOG")
                        continue
                
                if processed_files:
                    file_content = "\n\n".join(processed_files)
                    content_source = f"{len(processed_files)} file(s)"
                else:
                    return await ctx.send("❌ No supported files were successfully processed.")
            
            # Handle replied messages if -m flag is used
            if include_reply:
                # Check if this message is a reply
                if ctx.message.reference and ctx.message.reference.message_id:
                    try:
                        # Fetch the referenced message
                        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                        
                        # Extract URLs from message content
                        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
                        urls_in_content = re.findall(url_pattern, replied_msg.content) if replied_msg.content else []
                        
                        # Extract URLs from embeds
                        urls_from_embeds = []
                        if replied_msg.embeds:
                            for embed in replied_msg.embeds:
                                if embed.url:
                                    urls_from_embeds.append(embed.url)
                        
                        # Combine all URLs found
                        all_urls = urls_in_content + urls_from_embeds
                        
                        if all_urls:
                            # Use the first URL found
                            replied_url = all_urls[0]
                            
                            # Build context from the message (without URL since we're summarizing it)
                            context_parts = []
                            if replied_msg.content:
                                # Remove the URL from content to avoid duplication
                                clean_content = replied_msg.content
                                for url in all_urls:
                                    clean_content = clean_content.replace(url, "").strip()
                                if clean_content.strip():
                                    context_parts.append(f"Original message: {clean_content.strip()}")
                            
                            if replied_msg.embeds:
                                for embed in replied_msg.embeds:
                                    if embed.title:
                                        context_parts.append(f"Link title: {embed.title}")
                                    if embed.description:
                                        context_parts.append(f"Link description: {embed.description[:200]}...")
                            
                            if context_parts:
                                replied_context = "\n".join(context_parts)
                                
                            # Notify user that we're processing the link from the replied message
                            processing_msg = await ctx.send("🔗 Found link in replied message\n📄 Fetching content for summarization...")
                        
                        # Check for file attachments in replied message
                        elif replied_msg.attachments and not file_content:
                            await ctx.send("ℹ️ Found attachments in replied message, but file processing from replies is not yet supported. Please use the files directly.")
                            return
                        
                        else:
                            if not file_content:
                                await ctx.send("⚠️ No URL or supported content found in the referenced message.")
                                return
                            
                    except Exception as e:
                        sum_logger.error(f"Failed to fetch replied message: {e}")
                        await ctx.send("⚠️ Could not fetch the referenced message.")
                        return
                else:
                    await ctx.send("⚠️ The -m flag requires replying to a message containing a URL.")
                    return
            
            # Parse optional prompt and URL
            prompt_and_url = prompt_and_url.strip()
            
            # Determine what content we're working with
            url = ""
            user_prompt = ""
            content = ""
            
            if file_content:
                # We have file content to summarize
                content = file_content
                user_prompt = prompt_and_url  # Everything is the user prompt
                item_label = "document"
                
                # Delete any processing messages
                if include_reply and 'processing_msg' in locals():
                    try:
                        await processing_msg.delete()
                    except:
                        pass
                
            elif replied_url:
                # We got URL from replied message, use it and fetch its content
                url = replied_url
                user_prompt = prompt_and_url  # Everything remaining is the user prompt
                content = await self._get_content_text(url)
                item_label = "video" if any(domain in url for domain in ("youtube.com", "youtu.be")) else ("tweet" if any(domain in url for domain in ("twitter.com", "x.com")) else "article")
                
                # Delete the processing message if it exists (from -m flag usage)
                if 'processing_msg' in locals():
                    try:
                        await processing_msg.delete()
                    except:
                        pass
                
            else:
                # Look for URL in the input text
                words = prompt_and_url.split()
                
                # Find the last URL in the input
                for i in range(len(words) - 1, -1, -1):
                    if words[i].startswith(("http://", "https://")):
                        url = words[i]
                        user_prompt = " ".join(words[:i])
                        break
                
                if not url:
                    # No URL found, treat entire input as URL
                    url = prompt_and_url
                    user_prompt = ""
                
                if not url:
                    return await ctx.send("❌ Please provide a URL to summarize or attach a supported file.")
                
                content = await self._get_content_text(url)
                item_label = "video" if any(domain in url for domain in ("youtube.com", "youtu.be")) else ("tweet" if any(domain in url for domain in ("twitter.com", "x.com")) else "article")
            
            # Extract video frames if visual mode is enabled for YouTube videos
            video_frames: List[str] = []
            if visual_mode and url and any(domain in url for domain in ("youtube.com", "youtu.be")):
                # Check if ffmpeg is available
                try:
                    ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                    if ffmpeg_check.returncode != 0:
                        await ctx.send("⚠️ FFmpeg is not installed. Visual mode requires FFmpeg for frame extraction.")
                        visual_mode = False
                except FileNotFoundError:
                    await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to use visual mode.")
                    visual_mode = False
                
                if visual_mode:  # Only proceed if ffmpeg is available
                    try:
                        video_processing_msg = await ctx.send("🎬 Extracting video frames for visual analysis... This may take a moment.")
                        video_frames = await self._extract_video_frames(url, max_frames=self.MIN_VIDEO_FRAMES)
                        if video_frames:
                            sum_logger.info(f"Successfully extracted {len(video_frames)} frames for visual analysis")
                            # Update and then delete processing message
                            await video_processing_msg.edit(content=f"✅ Extracted {len(video_frames)} frames. Analyzing with vision model...")
                            await asyncio.sleep(2)  # Show success for 2 seconds
                            try:
                                await video_processing_msg.delete()
                            except:
                                pass  # Message might have been deleted already
                        else:
                            # Update and then delete processing message with error
                            await video_processing_msg.edit(content="⚠️ Could not extract video frames. Falling back to transcript-only summary.")
                            await asyncio.sleep(2)  # Show error for 2 seconds
                            try:
                                await video_processing_msg.delete()
                            except:
                                pass  # Message might have been deleted already
                            visual_mode = False
                    except Exception as e:
                        sum_logger.error(f"Frame extraction failed: {e}")
                        # Update and then delete processing message with error if it exists
                        if 'video_processing_msg' in locals():
                            try:
                                await video_processing_msg.edit(content="⚠️ Frame extraction failed. Falling back to transcript-only summary.")
                                await asyncio.sleep(2)  # Show error for 2 seconds
                                await video_processing_msg.delete()
                            except:
                                pass  # Message might have been deleted already
                        else:
                            await ctx.send("⚠️ Frame extraction failed. Falling back to transcript-only summary.")
                        visual_mode = False
            
            # Check if we got an error placeholder for URL content
            if url and (not content or content.startswith("[") or "could not be extracted" in content):
                sum_logger.warning(f"Content unavailable for {url}, will notify LLM of extraction failure.")
                # Provide context to the LLM about the extraction failure
                extraction_failed = True
                is_tweet = any(domain in url for domain in ("twitter.com", "x.com"))
                if is_tweet:
                    content = f"I need to summarize an X/Twitter post from {url}, but I was unable to extract its content. The post may be private, deleted, or the extraction API may have changed."
                else:
                    content = f"I need to summarize content from {url}, but I was unable to extract it. The page may be inaccessible, require authentication, or be blocked."
            else:
                extraction_failed = False

            # Build the AI prompt based on content type and user input
            if extraction_failed:
                if user_prompt:
                    base_prompt = f"The user asked: {user_prompt}\n\nHowever, I couldn't access the content. Please provide a helpful response explaining the situation."
                else:
                    base_prompt = f"I was asked to summarize content from {url} but couldn't access it. Please provide a helpful response explaining what might have gone wrong and suggest alternatives."
                    
                if replied_context:
                    prompt_text = f"Context from the original message that contained this link:\n{replied_context}\n\n{base_prompt}"
                else:
                    prompt_text = base_prompt
            else:
                if user_prompt:
                    if file_content:
                        base_prompt = f"Based on the following {item_label}, answer or elaborate on: {user_prompt}.\nKeep the response detailed and informative, ≤300 words."
                    else:
                        base_prompt = f"Based on the following {item_label}, answer or elaborate on: {user_prompt}.\nKeep the response oblique and metaphor-laden, ≤200 words."
                else:
                    if visual_mode and video_frames:
                        base_prompt = f"Analyze both the transcript and visual content of this video. Provide a comprehensive summary that includes what's shown visually and what's discussed. Use bullet points and keep it under 250 words."
                    elif file_content:
                        base_prompt = f"Create a comprehensive summary with detailed bullet points (max 300 words) of this {item_label}. Focus on key points, main themes, and important information."
                    else:
                        base_prompt = f"Create an oblique summary with detailed bullet points (max 200 words) of this {item_label}."
                
                # Add replied context if available
                if replied_context:
                    prompt_text = f"Context from the original message that shared this content:\n{replied_context}\n\n{base_prompt}"
                else:
                    prompt_text = base_prompt

            # Build messages based on whether we're in visual mode
            messages: List[dict[str, Any]]
            if visual_mode and video_frames:
                # Use vision model with both transcript and frames
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT_QWEN}
                ]
                
                # Build user content with transcript and images
                user_content: List[dict[str, Any]] = [
                    {"type": "text", "text": f"Video Transcript:\n{content}\n\n{prompt_text}"}
                ]
                
                # Add video frames
                for idx, frame_base64 in enumerate(video_frames):
                    user_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{frame_base64}"
                        }
                    })
                
                messages.append({"role": "user", "content": user_content})
                
                # Use vision model
                model = "qwen/qwen2.5-vl-7b"
            else:
                # Regular text-only mode
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT_QWEN},
                    {"role": "assistant", "content": content},  # context
                    {"role": "user", "content": prompt_text}
                ]
                model = "qwen/qwen3-4b"
            
            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                "max_tokens": self.LMSTUDIO_MAX_TOKENS if self.LMSTUDIO_MAX_TOKENS > 0 else 1024,
                "keep_alive": self.MODEL_TTL_SECONDS  # Unload model after configured TTL
            }
            accumulated = ""
            try:
                timeout = aiohttp.ClientTimeout(total=None)
                headers = {"Accept": "text/event-stream"}
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.LMSTUDIO_CHAT_URL, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            sum_logger.error(f"Summ. API HTTP {resp.status}: {err}")
                            return await ctx.send(f"❌ Summarization API error {resp.status}")
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
                                # streaming_logger.error(f"SSE parse error: {se}")
                                continue
                if not accumulated:
                    return await ctx.send("⚠️ Empty summary returned.")
                # Strip reasoning
                accumulated = re.sub(r"<think>.*?</think>", "", accumulated, flags=re.DOTALL)
                # Also remove any unmatched <think> tag left over
                accumulated = re.sub(r"<think>.*", "", accumulated, flags=re.DOTALL)
                accumulated = accumulated.replace("<think>", "").replace("</think>", "")
                # Remove LaTeX boxed answers
                accumulated = re.sub(r"\\boxed\s*{([^}]+)}", r"\1", accumulated)
                accumulated = accumulated.replace("\\boxed", "").strip()
                await self._send_limited(ctx, accumulated)
                
                # Store interaction in memory with appropriate source identification
                if file_content:
                    memory_input = f"File content: {content_source}"
                elif url:
                    memory_input = url
                else:
                    memory_input = "file upload"
                    
                self._remember(ctx.author.id, "user", memory_input)
                self._remember(ctx.author.id, "assistant", accumulated)
                # Use enhanced context storage for per-user isolation
                if self._set_user_context:
                    self._set_user_context(ctx.author.id, accumulated)
                else:
                    # Fallback to direct access if enhanced functions not available
                    self._user_context[ctx.author.id] = accumulated
            except Exception as e:
                sum_logger.error("Streaming summary error", exc_info=e)
                await ctx.send(f"⚠️ Failed to generate summary: {e}")
            return


def setup_sum_commands(bot, memory_system, content_system, video_system):
    """Setup function to add sum commands to the bot"""
    sum_commands = SumCommands(bot, memory_system, content_system, video_system)
    return sum_commands 