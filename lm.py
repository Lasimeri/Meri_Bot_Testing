"""
LM (Language Model) Commands Module for Meri Bot

This module contains multimodal AI chat functionality with vision, video, and web search capabilities.
Separated from main bot file for better code organization and maintainability.

Commands included:
- lm: Advanced multimodal AI chat with vision, video analysis, and web search
"""

import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import re
import base64
import io
import tempfile
import subprocess
from pathlib import Path
import os
from typing import List, Any, Dict

# Set up logger for LM operations
lm_logger = logging.getLogger("MeriLM")

# Try to import PIL for image processing
try:
    from PIL import Image  # type: ignore
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    _PIL_AVAILABLE = False

# Try to import yt-dlp with fallback
try:
    import yt_dlp  # type: ignore
except ImportError:
    yt_dlp = None


class LMCommands(commands.Cog):
    """Language Model Commands Cog"""
    
    def __init__(self, bot, memory_system, search_system, video_system):
        self.bot = bot
        self._user_memory = memory_system['memory']
        self._user_context = memory_system['context']
        self._remember = memory_system['remember']
        self._ddg_search = search_system
        self._extract_video_frames_from_file = video_system['extract_frames']
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
        self.SYSTEM_PROMPT = getenv(
            "SYSTEM_PROMPT",
            "You are a helpful, accurate, and knowledgeable AI assistant."
        )

    @commands.hybrid_command(name="lm", description="Multimodal AI chat (images/videos, -m for reply, -vis for video analysis)")
    @app_commands.describe(prompt="Your prompt (use '-s query' for web search, '-m' for replied context, '-vis' for video analysis, attach media)")
    async def lms_chat_ns(self, ctx, *, prompt: str):
        """Advanced multimodal AI chat with vision, video, and web search capabilities.
        
        Features:
        - Image analysis: Attach images for visual understanding
        - Video analysis: Use -vis flag to extract and analyze video frames  
        - Reply context: Use -m flag to reference replied messages
        - Web search: Use -s query to add search results
        - Memory: Remembers conversation context
        
        Examples:
        - ^lm describe this image [attach image]
        - ^lm -vis analyze this video [attach video]  
        - ^lm -m -vis what's in this? [reply to message with media]
        - ^lm -s cats tell me about cats
        """
        async with ctx.typing():
            from os import getenv
            search_results = ""
            replied_images = []  # Store base64 images from replied message
            replied_context = ""
            video_frames = []  # Store video frames for analysis
            
            # Parse command flags
            visual_mode = "-vis" in prompt  # Enable video frame extraction
            if visual_mode:
                prompt = prompt.replace("-vis", "").strip()
            
            include_reply = "-m" in prompt  # Include replied message context
            if include_reply:
                prompt = prompt.replace("-m", "").strip()
                
                # Check if this message is a reply
                if ctx.message.reference and ctx.message.reference.message_id:
                    try:
                        # Fetch the referenced message
                        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                        
                        context_parts = []
                        
                        # Add message content
                        if replied_msg.content:
                            context_parts.append(f"Referenced message: {replied_msg.content}")
                        
                        # Process attachments - images and videos for vision model
                        if replied_msg.attachments:
                            for attachment in replied_msg.attachments:
                                filename_lower = attachment.filename.lower()
                                
                                # Handle images from replied message
                                if any(filename_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']):
                                    try:
                                        # Download and encode image from replied message
                                        async with aiohttp.ClientSession() as session:
                                            async with session.get(attachment.url) as resp:
                                                if resp.status == 200:
                                                    image_data = await resp.read()
                                                    base64_image = base64.b64encode(image_data).decode('utf-8')
                                                    mime_type = "image/jpeg"
                                                    if filename_lower.endswith('.png'):
                                                        mime_type = "image/png"
                                                    elif filename_lower.endswith('.gif'):
                                                        mime_type = "image/gif"
                                                    elif filename_lower.endswith('.webp'):
                                                        mime_type = "image/webp"
                                                    
                                                    replied_images.append({
                                                        "type": "image_url",
                                                        "image_url": {
                                                            "url": f"data:{mime_type};base64,{base64_image}"
                                                        }
                                                    })
                                                    context_parts.append(f"[Image from replied message: {attachment.filename}]")
                                    except Exception as e:
                                        lm_logger.error(f"Failed to process replied image: {e}")
                                        context_parts.append(f"[Failed to load image: {attachment.filename}]")
                                
                                # Handle videos from replied message if -vis is enabled
                                elif visual_mode and any(filename_lower.endswith(ext) for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv']):
                                    try:
                                        # Check if ffmpeg is available
                                        try:
                                            ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                                            if ffmpeg_check.returncode != 0:
                                                await ctx.send("⚠️ FFmpeg is not installed. Video analysis requires FFmpeg for frame extraction.")
                                                context_parts.append(f"[Video from replied message (FFmpeg required): {attachment.filename}]")
                                                continue
                                        except FileNotFoundError:
                                            await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to use video analysis.")
                                            context_parts.append(f"[Video from replied message (FFmpeg required): {attachment.filename}]")
                                            continue
                                        
                                        processing_msg = await ctx.send("🎬 Processing video from replied message...")
                                        
                                        # Download video to temporary file
                                        with tempfile.NamedTemporaryFile(suffix=f".{filename_lower.split('.')[-1]}", delete=False) as temp_video:
                                            async with aiohttp.ClientSession() as session:
                                                async with session.get(attachment.url) as resp:
                                                    if resp.status == 200:
                                                        temp_video.write(await resp.read())
                                                        temp_video_path = temp_video.name
                                            
                                            # Extract frames from video
                                            try:
                                                replied_video_frames = await self._extract_video_frames_from_file(temp_video_path, max_frames=self.MIN_VIDEO_FRAMES)
                                                if replied_video_frames:
                                                    # Add frames to replied_images so they get processed with other replied media
                                                    for frame_base64 in replied_video_frames:
                                                        replied_images.append({
                                                            "type": "image_url",
                                                            "image_url": {
                                                                "url": f"data:image/jpeg;base64,{frame_base64}"
                                                            }
                                                        })
                                                    # Update and then delete processing message
                                                    await processing_msg.edit(content=f"✅ Extracted {len(replied_video_frames)} frames from replied video")
                                                    await asyncio.sleep(2)  # Show success for 2 seconds
                                                    try:
                                                        await processing_msg.delete()
                                                    except:
                                                        pass  # Message might have been deleted already
                                                    context_parts.append(f"[Video frames from replied message: {attachment.filename} ({len(replied_video_frames)} frames)]")
                                                else:
                                                    # Update and then delete processing message with error
                                                    await processing_msg.edit(content="⚠️ Could not extract frames from replied video")
                                                    await asyncio.sleep(2)  # Show error for 2 seconds
                                                    try:
                                                        await processing_msg.delete()
                                                    except:
                                                        pass  # Message might have been deleted already
                                                    context_parts.append(f"[Failed to extract frames from replied video: {attachment.filename}]")
                                            finally:
                                                # Clean up temporary file
                                                try:
                                                    os.unlink(temp_video_path)
                                                except:
                                                    pass
                                    except Exception as e:
                                        lm_logger.error(f"Failed to process replied video: {e}")
                                        context_parts.append(f"[Failed to process replied video: {attachment.filename}]")
                                
                                # Handle other file types
                                else:
                                    context_parts.append(f"[File attachment: {attachment.filename}]")
                        
                        # Check for embeds
                        if replied_msg.embeds:
                            for embed in replied_msg.embeds:
                                if embed.url and any(domain in embed.url for domain in ("youtube.com", "youtu.be")):
                                    context_parts.append(f"YouTube video: {embed.url}")
                                elif embed.url:
                                    context_parts.append(f"Link: {embed.url}")
                                if embed.title:
                                    context_parts.append(f"Title: {embed.title}")
                                if embed.description:
                                    context_parts.append(f"Description: {embed.description[:200]}...")
                        
                        if context_parts:
                            replied_context = "\n".join(context_parts)
                            
                    except Exception as e:
                        lm_logger.error(f"Failed to fetch replied message: {e}")
                        await ctx.send("⚠️ Could not fetch the referenced message.")
                        return
                else:
                    await ctx.send("⚠️ The -m flag requires replying to a message.")
                    return
            
            # Check for YouTube URLs if -vis flag is used
            youtube_frames = []
            if visual_mode:
                # Look for YouTube URLs in the prompt
                youtube_url_pattern = r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]+)'
                youtube_match = re.search(youtube_url_pattern, prompt)
                
                if youtube_match:
                    youtube_url = youtube_match.group(0)
                    lm_logger.info(f"Processing YouTube URL in lm -vis: {youtube_url}")
                    
                    # Check if ffmpeg is available for YouTube video processing
                    try:
                        ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                        if ffmpeg_check.returncode != 0:
                            await ctx.send("⚠️ FFmpeg is not installed. YouTube video analysis requires FFmpeg for frame extraction.")
                        else:
                            # Import the YouTube processing function from main bot
                            from Meri_Bot import _extract_video_frames
                            
                            # Extract frames from YouTube video
                            processing_msg = await ctx.send("🎬 Extracting frames from YouTube video...")
                            try:
                                youtube_frames = await _extract_video_frames(youtube_url, max_frames=self.MIN_VIDEO_FRAMES)
                                if youtube_frames:
                                    await processing_msg.edit(content=f"✅ Extracted {len(youtube_frames)} frames from YouTube video")
                                    await asyncio.sleep(2)
                                    try:
                                        await processing_msg.delete()
                                    except:
                                        pass
                                else:
                                    await processing_msg.edit(content="⚠️ Could not extract frames from YouTube video")
                                    await asyncio.sleep(2)
                                    try:
                                        await processing_msg.delete()
                                    except:
                                        pass
                            except Exception as e:
                                lm_logger.error(f"YouTube frame extraction failed in lm -vis: {e}")
                                await processing_msg.edit(content="⚠️ YouTube frame extraction failed")
                                await asyncio.sleep(2)
                                try:
                                    await processing_msg.delete()
                                except:
                                    pass
                    except FileNotFoundError:
                        await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to analyze YouTube videos.")

            # Then check for search flag
            if prompt.startswith("-s "):
                query = prompt[3:].strip()
                snips = []
                data = self._ddg_search(query, max_results=5)
                for itm in data:
                    snips.append(f"{itm.get('title')}: {itm.get('body') or itm.get('snippet')} ({itm.get('href') or itm.get('url')})")
                if snips:
                    search_results = "Top web search results:\n" + "\n".join(snips)
                prompt_q = query
            else:
                prompt_q = prompt

            # Use enhanced memory access for per-user isolation
            if self._get_user_memory:
                history = self._get_user_memory(ctx.author.id)
            else:
                # Fallback to direct access if enhanced functions not available
                history = self._user_memory.get(ctx.author.id, [])
            SYS_DARK = getenv("SYSTEM_PROMPT_DARK", self.SYSTEM_PROMPT)
            
            # Build the user message content
            user_content = []
            
            # Add text prompt with replied context if available
            if replied_context:
                text_content = f"Context from referenced message:\n{replied_context}\n\nUser question: {prompt_q}"
            else:
                text_content = prompt_q
            
            user_content.append({"type": "text", "text": text_content})
            
            # Add images from replied message first
            for img in replied_images:
                user_content.append(img)
            
            # Check for attachments and process images/videos
            if ctx.message.attachments:
                for attachment in ctx.message.attachments:
                    filename_lower = attachment.filename.lower()
                    
                    # Check if attachment is an image
                    if any(filename_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']):
                        try:
                            # Download the image
                            async with aiohttp.ClientSession() as session:
                                async with session.get(attachment.url) as resp:
                                    if resp.status == 200:
                                        image_data = await resp.read()
                                        # Convert to base64
                                        base64_image = base64.b64encode(image_data).decode('utf-8')
                                        # Determine MIME type
                                        mime_type = "image/jpeg"  # default
                                        if filename_lower.endswith('.png'):
                                            mime_type = "image/png"
                                        elif filename_lower.endswith('.gif'):
                                            mime_type = "image/gif"
                                        elif filename_lower.endswith('.webp'):
                                            mime_type = "image/webp"
                                        
                                        user_content.append({
                                            "type": "image_url",
                                            "image_url": {
                                                "url": f"data:{mime_type};base64,{base64_image}"
                                            }
                                        })
                        except Exception as e:
                            lm_logger.error(f"Failed to download/encode image: {e}")
                            await ctx.send(f"⚠️ Failed to process image: {attachment.filename}")
                    
                    # Check if attachment is a video and -vis flag is used
                    elif visual_mode and any(filename_lower.endswith(ext) for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv']):
                        try:
                            # Check if ffmpeg is available
                            try:
                                ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                                if ffmpeg_check.returncode != 0:
                                    await ctx.send("⚠️ FFmpeg is not installed. Video analysis requires FFmpeg for frame extraction.")
                                    continue
                            except FileNotFoundError:
                                await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to use video analysis.")
                                continue
                            
                            processing_msg = await ctx.send("🎬 Processing video for frame extraction...")
                            
                            # Download video to temporary file
                            with tempfile.NamedTemporaryFile(suffix=f".{filename_lower.split('.')[-1]}", delete=False) as temp_video:
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(attachment.url) as resp:
                                        if resp.status == 200:
                                            temp_video.write(await resp.read())
                                            temp_video_path = temp_video.name
                                
                                # Extract frames from video
                                try:
                                    video_frames_extracted = await self._extract_video_frames_from_file(temp_video_path, max_frames=self.MIN_VIDEO_FRAMES)
                                    if video_frames_extracted:
                                        video_frames.extend(video_frames_extracted)
                                        # Update and then delete processing message
                                        await processing_msg.edit(content=f"✅ Extracted {len(video_frames_extracted)} frames from video")
                                        await asyncio.sleep(2)  # Show success for 2 seconds
                                        try:
                                            await processing_msg.delete()
                                        except:
                                            pass  # Message might have been deleted already
                                    else:
                                        # Update and then delete processing message with error
                                        await processing_msg.edit(content="⚠️ Could not extract frames from video")
                                        await asyncio.sleep(2)  # Show error for 2 seconds
                                        try:
                                            await processing_msg.delete()
                                        except:
                                            pass  # Message might have been deleted already
                                finally:
                                    # Clean up temporary file
                                    try:
                                        os.unlink(temp_video_path)
                                    except:
                                        pass
                        except Exception as e:
                            lm_logger.error(f"Failed to process video: {e}")
                            await ctx.send(f"⚠️ Failed to process video: {attachment.filename}")
            
            # Add video frames to user content if any were extracted
            for frame_base64 in video_frames:
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{frame_base64}"
                    }
                })
            
            # Add YouTube frames to user content if any were extracted
            for frame_base64 in youtube_frames:
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{frame_base64}"
                    }
                })
            
            # If -vis was used but no video frames were extracted, inform the user
            if visual_mode and not video_frames and not youtube_frames and not any(replied_images):
                has_current_attachments = any(ctx.message.attachments)
                has_replied_videos = False
                has_youtube_url = bool(re.search(r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]+)', prompt))
                
                # Check if replied message has videos (when using -m)
                if include_reply and ctx.message.reference:
                    try:
                        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                        has_replied_videos = any(
                            attachment.filename.lower().endswith(ext) 
                            for attachment in replied_msg.attachments 
                            for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv']
                        )
                    except:
                        pass
                
                if not has_current_attachments and not has_replied_videos and not has_youtube_url:
                    help_msg = "ℹ️ The `-vis` flag is for video analysis. Please attach a video file (mp4, webm, avi, mov, mkv), provide a YouTube URL, "
                    if include_reply:
                        help_msg += "or reply to a message containing a video."
                    else:
                        help_msg += "to analyze."
                    await ctx.send(help_msg)
                elif has_current_attachments:
                    # Check if there were only non-video attachments in current message
                    has_videos = any(attachment.filename.lower().endswith(ext) 
                                   for attachment in ctx.message.attachments 
                                   for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv'])
                    if not has_videos:
                        await ctx.send("ℹ️ The `-vis` flag extracts frames from videos. Your attachments appear to be images (which are processed automatically). Remove `-vis` for image analysis only.")
            
            # Determine if we need vision model (has images/video frames)
            has_visual_content = any(item.get("type") == "image_url" for item in user_content)
            
            # Initialize messages array
            messages: List[Dict[str, Any]] = [{"role": "system", "content": SYS_DARK}]
            if search_results:
                messages.append({"role": "assistant", "content": search_results})
            # Add history messages
            for msg in history:
                messages.append(msg)
            
            # Choose model and message format based on content type
            if has_visual_content:
                # Use vision model for images/videos
                model_name = "qwen/qwen2.5-vl-7b"
                # Add the user message with proper content format for vision models
                messages.append({"role": "user", "content": user_content})
            else:
                # Use darkidol model for text-only (including search)
                model_name = "darkidol-llama-3.1-8b-instruct-1.2-uncensored@q2_k"
                # Extract text content only for text model
                text_content = user_content[0]["text"] if user_content and user_content[0].get("type") == "text" else prompt_q
                # Add the user message as simple text
                messages.append({"role": "user", "content": text_content})
            
            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": self.LMSTUDIO_MAX_TOKENS,
                "stream": True,
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
                            lm_logger.error(f"ChatNS API HTTP {resp.status}: {err}")
                            return await ctx.send(f"❌ API error {resp.status}")
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
                                delta = obj.get('choices', [{}])[0].get('delta', {})
                                part = delta.get('content', '')
                                if part:
                                    accumulated += part
                                if obj.get('choices', [{}])[0].get('finish_reason') == 'stop':
                                    break
                            except Exception:
                                continue
                if not accumulated:
                    return await ctx.send("❌ Empty chat response.")
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

                # Send response in chunks
                chunk_size = 1900
                chunks = [accumulated[i:i+chunk_size] for i in range(0, len(accumulated), chunk_size)]
                for chunk in chunks:
                    await ctx.send(f"```{chunk}```")

                # Store interaction with text-only representation for memory
                # Count total images and video frames (both from reply and attachments)
                total_images = len([item for item in user_content if item.get("type") == "image_url"])
                total_video_frames = len(video_frames)
                total_youtube_frames = len(youtube_frames)
                
                # Count replied media
                replied_media_count = len(replied_images)
                
                if total_images == 0:
                    memory_prompt = prompt_q
                else:
                    media_count = total_images
                    media_parts = []
                    
                    if replied_media_count > 0:
                        media_parts.append(f"{replied_media_count} from replied message")
                    
                    if total_video_frames > 0:
                        media_parts.append(f"{total_video_frames} video frame(s)")
                    
                    if total_youtube_frames > 0:
                        media_parts.append(f"{total_youtube_frames} YouTube frame(s)")
                    
                    if media_parts:
                        media_detail = " (" + ", ".join(media_parts) + ")"
                    else:
                        media_detail = ""
                    
                    memory_prompt = f"{prompt_q} [with {media_count} image(s){media_detail}]"
                
                self._remember(ctx.author.id, "user", memory_prompt)
                self._remember(ctx.author.id, "assistant", accumulated)
                # Use enhanced context storage for per-user isolation
                if self._set_user_context:
                    self._set_user_context(ctx.author.id, accumulated)
                else:
                    # Fallback to direct access if enhanced functions not available
                    self._user_context[ctx.author.id] = accumulated
            except Exception as e:
                lm_logger.error("Streaming chatNS API error", exc_info=e)
                await ctx.send(f"⚠️ Chat API request failed: {e}")


def setup_lm_commands(bot, memory_system, search_system, video_system):
    """Setup function to add LM commands to the bot"""
    lm_commands = LMCommands(bot, memory_system, search_system, video_system)
    return lm_commands 