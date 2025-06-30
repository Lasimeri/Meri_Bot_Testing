"""
Visual Analysis Commands Module for Meri Bot

This module contains comprehensive visual analysis functionality.
Separated from main bot file for better code organization and maintainability.

Commands included:
- vis: Analyze visual content including YouTube videos, images, GIFs, and video files
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
from typing import List, Any

# Set up logger for visual analysis operations
vis_logger = logging.getLogger("MeriVis")

# Try to import PIL for image processing
try:
    from PIL import Image  # type: ignore
    _PIL_AVAILABLE = True
    vis_logger.info("PIL (Pillow) successfully imported for image/GIF processing")
except ImportError as pil_error:  # pragma: no cover
    Image = None  # type: ignore
    _PIL_AVAILABLE = False
    vis_logger.warning(f"PIL (Pillow) not available: {pil_error}. GIF processing will use FFmpeg fallback.")

# Try to import yt-dlp with fallback
try:
    import yt_dlp  # type: ignore
except ImportError:
    yt_dlp = None


class VisCommands(commands.Cog):
    """Visual Analysis Commands Cog"""
    
    def __init__(self, bot, memory_system, video_system, twitter_system):
        self.bot = bot
        self._user_memory = memory_system['memory']
        self._user_context = memory_system['context']
        self._remember = memory_system['remember']
        self._send_limited = memory_system['send_limited']
        self._extract_video_frames = video_system['extract_video_frames']
        self._extract_video_frames_from_file = video_system['extract_video_frames_from_file']
        self._extract_gif_frames = video_system['extract_gif_frames']
        self.MIN_VIDEO_FRAMES = video_system['min_frames']
        self._extract_twitter_media = twitter_system['extract_twitter_media']
        
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

    @commands.hybrid_command(name="vis", description="Analyze visual content: YouTube videos, images, GIFs, and video files")
    @app_commands.describe(content="YouTube URL, or attach images/videos/GIFs. Optional: add a question about the content")
    async def visual_analysis(self, ctx, *, content: str = ""):
        """Comprehensive visual analysis for multiple media types.
        
        Supports:
        - YouTube videos: Extracts frames and analyzes content
        - Video files: mp4, webm, avi, mov, mkv (extracts frames)
        - GIF files: Extracts frames from animated GIFs
        - Images: png, jpg, jpeg, gif, webp, bmp (direct analysis)
        
        Examples:
        - ^vis https://youtu.be/xyz (analyze YouTube video)
        - ^vis https://youtube.com/shorts/xyz (analyze YouTube Short)
        - ^vis What colors are shown? [attach image]
        - ^vis Describe the action [attach video/GIF]
        - ^vis [just attach media for general analysis]
        """
        async with ctx.typing():
            user_prompt = content.strip() if content else "Analyze and describe what you see in this visual content. Be detailed and comprehensive."
            all_media_content = []  # Store all visual content for analysis
            media_descriptions = []  # Track what types of media we're processing
            
            # Parse command flags
            include_reply = "-m" in user_prompt  # Include replied message context
            if include_reply:
                user_prompt = user_prompt.replace("-m", "").strip()
                if not user_prompt:
                    user_prompt = "Analyze and describe what you see in this visual content. Be detailed and comprehensive."
                
                # Check if this message is a reply
                if ctx.message.reference and ctx.message.reference.message_id:
                    try:
                        # Fetch the referenced message
                        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                        
                        # Process attachments from replied message
                        if replied_msg.attachments:
                            for attachment in replied_msg.attachments:
                                filename_lower = attachment.filename.lower()
                                
                                # Handle images from replied message
                                if any(filename_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp']):
                                    try:
                                        processing_msg = await ctx.send(f"🖼️ Processing image from replied message: {attachment.filename}")
                                        
                                        async with aiohttp.ClientSession() as session:
                                            async with session.get(attachment.url) as resp:
                                                if resp.status == 200:
                                                    image_data = await resp.read()
                                                    base64_image = base64.b64encode(image_data).decode('utf-8')
                                                    
                                                    # Determine MIME type
                                                    mime_type = "image/jpeg"
                                                    if filename_lower.endswith('.png'):
                                                        mime_type = "image/png"
                                                    elif filename_lower.endswith('.webp'):
                                                        mime_type = "image/webp"
                                                    
                                                    all_media_content.append({
                                                        "type": "image_url",
                                                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                                                    })
                                                    media_descriptions.append(f"image from reply ({attachment.filename})")
                                                    
                                                    await processing_msg.edit(content=f"✅ Processed image from reply: {attachment.filename}")
                                                    await asyncio.sleep(1)
                                                    await processing_msg.delete()
                                        
                                    except Exception as e:
                                        vis_logger.error(f"Failed to process replied image {attachment.filename}: {e}")
                                        await ctx.send(f"⚠️ Failed to process image from reply: {attachment.filename}")
                                
                                # Handle GIF files from replied message
                                elif filename_lower.endswith('.gif'):
                                    try:
                                        processing_msg = await ctx.send(f"🎞️ Processing GIF from replied message: {attachment.filename}")
                                        
                                        # Download GIF to temporary file
                                        with tempfile.NamedTemporaryFile(suffix='.gif', delete=False) as temp_gif:
                                            async with aiohttp.ClientSession() as session:
                                                async with session.get(attachment.url) as resp:
                                                    if resp.status == 200:
                                                        temp_gif.write(await resp.read())
                                                        temp_gif_path = temp_gif.name
                                        
                                        # Extract frames from GIF
                                        try:
                                            gif_frames = await self._extract_gif_frames(temp_gif_path, max_frames=self.MIN_VIDEO_FRAMES)
                                            if gif_frames:
                                                for frame_base64 in gif_frames:
                                                    all_media_content.append({
                                                        "type": "image_url",
                                                        "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                                                    })
                                                media_descriptions.append(f"GIF from reply ({len(gif_frames)} frames)")
                                                
                                                await processing_msg.edit(content=f"✅ Extracted {len(gif_frames)} frames from replied GIF")
                                                await asyncio.sleep(1)
                                                await processing_msg.delete()
                                            else:
                                                await processing_msg.edit(content="⚠️ Could not extract frames from replied GIF")
                                                await asyncio.sleep(2)
                                                await processing_msg.delete()
                                        finally:
                                            # Clean up temporary file
                                            try:
                                                os.unlink(temp_gif_path)
                                            except:
                                                pass
                                        
                                    except Exception as e:
                                        vis_logger.error(f"Failed to process replied GIF {attachment.filename}: {e}")
                                        await ctx.send(f"⚠️ Failed to process GIF from reply: {attachment.filename}")
                                
                                # Handle video files from replied message
                                elif any(filename_lower.endswith(ext) for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv']):
                                    try:
                                        # Check if ffmpeg is available
                                        try:
                                            ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                                            if ffmpeg_check.returncode != 0:
                                                await ctx.send("⚠️ FFmpeg is not installed. Video analysis requires FFmpeg for frame extraction.")
                                                continue
                                        except FileNotFoundError:
                                            await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to analyze videos.")
                                            continue
                                        
                                        processing_msg = await ctx.send(f"🎬 Processing video from replied message: {attachment.filename}")
                                        
                                        # Download video to temporary file
                                        with tempfile.NamedTemporaryFile(suffix=f".{filename_lower.split('.')[-1]}", delete=False) as temp_video:
                                            async with aiohttp.ClientSession() as session:
                                                async with session.get(attachment.url) as resp:
                                                    if resp.status == 200:
                                                        temp_video.write(await resp.read())
                                                        temp_video_path = temp_video.name
                                        
                                        # Extract frames from video
                                        try:
                                            video_frames = await self._extract_video_frames_from_file(temp_video_path, max_frames=self.MIN_VIDEO_FRAMES)
                                            if video_frames:
                                                for frame_base64 in video_frames:
                                                    all_media_content.append({
                                                        "type": "image_url",
                                                        "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                                                    })
                                                media_descriptions.append(f"video from reply ({len(video_frames)} frames)")
                                                
                                                await processing_msg.edit(content=f"✅ Extracted {len(video_frames)} frames from replied video")
                                                await asyncio.sleep(1)
                                                await processing_msg.delete()
                                            else:
                                                await processing_msg.edit(content="⚠️ Could not extract frames from replied video")
                                                await asyncio.sleep(2)
                                                await processing_msg.delete()
                                        finally:
                                            # Clean up temporary file
                                            try:
                                                os.unlink(temp_video_path)
                                            except:
                                                pass
                                        
                                    except Exception as e:
                                        vis_logger.error(f"Failed to process replied video {attachment.filename}: {e}")
                                        await ctx.send(f"⚠️ Failed to process video from reply: {attachment.filename}")
                        
                        # Check for URLs in replied message content
                        if replied_msg.content:
                            # Check for YouTube URLs in the replied message (including Shorts)
                            youtube_url_pattern = r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]+)'
                            youtube_match = re.search(youtube_url_pattern, replied_msg.content)
                            
                            if youtube_match:
                                youtube_url = youtube_match.group(0)
                                vis_logger.info(f"Processing YouTube URL from replied message: {youtube_url}")
                                
                                # Check if ffmpeg is available for YouTube video processing
                                try:
                                    ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                                    if ffmpeg_check.returncode != 0:
                                        await ctx.send("⚠️ FFmpeg is not installed. YouTube video analysis requires FFmpeg for frame extraction.")
                                    else:
                                        # Extract frames from YouTube video
                                        processing_msg = await ctx.send("🎬 Extracting frames from YouTube video in replied message...")
                                        try:
                                            video_frames = await self._extract_video_frames(youtube_url, max_frames=self.MIN_VIDEO_FRAMES)
                                            if video_frames:
                                                for frame_base64 in video_frames:
                                                    all_media_content.append({
                                                        "type": "image_url",
                                                        "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                                                    })
                                                media_descriptions.append(f"YouTube video from reply ({len(video_frames)} frames)")
                                                await processing_msg.edit(content=f"✅ Extracted {len(video_frames)} frames from YouTube video in reply")
                                                await asyncio.sleep(2)
                                                await processing_msg.delete()
                                            else:
                                                await processing_msg.edit(content="⚠️ Could not extract frames from YouTube video in reply")
                                                await asyncio.sleep(2)
                                                await processing_msg.delete()
                                        except Exception as e:
                                            vis_logger.error(f"YouTube frame extraction from reply failed: {e}")
                                            await processing_msg.edit(content="⚠️ YouTube frame extraction from reply failed")
                                            await asyncio.sleep(2)
                                            await processing_msg.delete()
                                except FileNotFoundError:
                                    await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to analyze YouTube videos.")
                            
                            # Check for Twitter/X URLs in the replied message
                            twitter_urls = re.findall(r'https?://(?:www\.)?(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|nitter\.net|twstalker\.com)/[^\s]+', replied_msg.content)
                            for twitter_url in twitter_urls:
                                try:
                                    processing_msg = await ctx.send(f"🐦 Extracting media from Twitter/X post in replied message...")
                                    twitter_images, twitter_videos = await self._extract_twitter_media(twitter_url)
                                    
                                    extracted_images = 0
                                    extracted_video_frames = 0
                                    
                                    # Add extracted images
                                    for img_base64 in twitter_images:
                                        all_media_content.append({
                                            "type": "image_url",
                                            "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}
                                        })
                                        extracted_images += 1
                                    
                                    # Process video frames (limit to 2 videos)
                                    videos_to_process = twitter_videos[:2]
                                    for video_url in videos_to_process:
                                        # Skip audio-only formats
                                        if any(audio_indicator in video_url.lower() for audio_indicator in ['/mp4a/', '.m3u8', '/pl/mp4a/']):
                                            continue
                                        
                                        # Add successful frames to content
                                        try:
                                            # Note: This would need the full Twitter video processing logic
                                            # For simplicity, just noting that video was found
                                            pass
                                        except Exception:
                                            continue
                                    
                                    # Provide feedback
                                    total_extracted = extracted_images + extracted_video_frames
                                    if total_extracted > 0:
                                        if extracted_video_frames > 0:
                                            media_descriptions.append(f"Twitter/X post from reply ({extracted_images} images, {extracted_video_frames} video frames)")
                                            await processing_msg.edit(content=f"✅ Extracted {extracted_images} images and {extracted_video_frames} frames from Twitter/X post in reply")
                                        else:
                                            media_descriptions.append(f"Twitter/X post from reply ({extracted_images} images)")
                                            await processing_msg.edit(content=f"✅ Extracted {extracted_images} images from Twitter/X post in reply")
                                        
                                        await asyncio.sleep(1)
                                        await processing_msg.delete()
                                    else:
                                        await processing_msg.edit(content="⚠️ No media found in Twitter/X post from reply")
                                        await asyncio.sleep(2)
                                        await processing_msg.delete()
                                        
                                except Exception as e:
                                    vis_logger.error(f"Failed to extract Twitter/X media from reply: {e}")
                                    await ctx.send(f"⚠️ Failed to extract media from Twitter/X post in reply: {str(e)}")
                        
                    except Exception as e:
                        vis_logger.error(f"Failed to fetch replied message: {e}")
                        await ctx.send("⚠️ Could not fetch the referenced message.")
                        return
                else:
                    await ctx.send("⚠️ The -m flag requires replying to a message.")
                    return
            
            # Check for YouTube URLs in the content (including Shorts)
            youtube_url_pattern = r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]+)'
            youtube_match = re.search(youtube_url_pattern, content)
            
            if youtube_match:
                youtube_url = youtube_match.group(0)
                vis_logger.info(f"Processing YouTube URL: {youtube_url}")
                
                # Check if ffmpeg is available for YouTube video processing
                try:
                    ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                    if ffmpeg_check.returncode != 0:
                        return await ctx.send("⚠️ FFmpeg is not installed. YouTube video analysis requires FFmpeg for frame extraction.")
                except FileNotFoundError:
                    return await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to analyze YouTube videos.")
                
                # Extract frames from YouTube video
                processing_msg = await ctx.send("🎬 Extracting frames from YouTube video...")
                try:
                    video_frames = await self._extract_video_frames(youtube_url, max_frames=self.MIN_VIDEO_FRAMES)
                    if video_frames:
                        for frame_base64 in video_frames:
                            all_media_content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                            })
                        media_descriptions.append(f"YouTube video ({len(video_frames)} frames)")
                        await processing_msg.edit(content=f"✅ Extracted {len(video_frames)} frames from YouTube video")
                        await asyncio.sleep(2)
                        await processing_msg.delete()
                    else:
                        await processing_msg.edit(content="⚠️ Could not extract frames from YouTube video")
                        await asyncio.sleep(2)
                        await processing_msg.delete()
                        return await ctx.send("❌ Failed to extract frames from the YouTube video.")
                except Exception as e:
                    vis_logger.error(f"YouTube frame extraction failed: {e}")
                    await processing_msg.edit(content="⚠️ YouTube frame extraction failed")
                    await asyncio.sleep(2)
                    await processing_msg.delete()
                    return await ctx.send("❌ Failed to process the YouTube video.")
            
            # Check for Twitter/X URLs in the content and extract media
            twitter_urls = re.findall(r'https?://(?:www\.)?(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|nitter\.net|twstalker\.com)/[^\s]+', content)
            for twitter_url in twitter_urls:
                try:
                    processing_msg = await ctx.send(f"🐦 Extracting media from Twitter/X post...")
                    twitter_images, twitter_videos = await self._extract_twitter_media(twitter_url)
                    
                    extracted_images = 0
                    extracted_video_frames = 0
                    
                    # Add extracted images
                    for img_base64 in twitter_images:
                        all_media_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}
                        })
                        extracted_images += 1
                    
                    # Extract frames from videos if any (with proper Twitter video handling)
                    # Limit video processing to prevent session timeouts (max 2 videos)
                    videos_to_process = twitter_videos[:2]  # Only process first 2 videos to prevent timeouts
                    videos_skipped = len(twitter_videos) - len(videos_to_process)
                    
                    if videos_skipped > 0:
                        vis_logger.info(f"Processing {len(videos_to_process)} of {len(twitter_videos)} videos (skipped {videos_skipped} to prevent timeout)")
                        # Inform user about the limitation
                        try:
                            await processing_msg.edit(content=f"🎬 Found {len(twitter_videos)} videos. Processing first {len(videos_to_process)} to prevent timeout...")
                        except (discord.NotFound, discord.HTTPException, RuntimeError):
                            pass  # Ignore if message update fails
                    
                    for video_url in videos_to_process:
                        try:
                            # Skip audio-only formats (HLS playlists and m3u8 files are often audio-only)
                            if any(audio_indicator in video_url.lower() for audio_indicator in ['/mp4a/', '.m3u8', '/pl/mp4a/']):
                                vis_logger.info(f"Skipping audio-only format: {video_url}")
                                continue
                            
                            # Prioritize actual MP4 video files with resolution indicators
                            if not any(video_indicator in video_url.lower() for video_indicator in ['/vid/avc1/', '.mp4?tag=']):
                                vis_logger.info(f"Skipping non-video format: {video_url}")
                                continue
                            
                            await processing_msg.edit(content=f"🎬 Extracting frames from Twitter/X video...")
                            
                            # Twitter videos need special handling - use yt-dlp to re-download with proper auth
                            video_frames = []
                            
                            if yt_dlp is not None:
                                try:
                                    vis_logger.info(f"Using yt-dlp to download and extract frames from Twitter video: {video_url}")
                                    
                                    # Use yt-dlp to download the video with proper authentication
                                    loop = asyncio.get_running_loop()
                                    
                                    def _download_twitter_video_and_extract():
                                        with tempfile.TemporaryDirectory() as temp_dir:
                                            temp_path = Path(temp_dir)
                                            video_path = temp_path / "twitter_video.mp4"
                                            
                                            # Configure yt-dlp to download video (not audio)
                                            ydl_opts = {
                                                'format': 'best[height>=360][ext=mp4]/best[ext=mp4]',  # Prefer video with decent quality
                                                'outtmpl': str(video_path),
                                                'quiet': True,
                                                'no_warnings': True,
                                            }
                                            
                                            try:
                                                with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore
                                                    # For Twitter video URLs, we need to provide additional context
                                                    # Try to extract frames using the original Twitter post URL if possible
                                                    original_tweet_match = re.search(r'https://(?:twitter\.com|x\.com)/[^/]+/status/\d+', twitter_url)
                                                    if original_tweet_match:
                                                        # Use the original tweet URL - yt-dlp will find the video
                                                        ydl.download([original_tweet_match.group(0)])
                                                    else:
                                                        # Fallback to direct video URL (may not work)
                                                        ydl.download([video_url])
                                                    
                                                    # If download succeeded, validate and extract frames
                                                    if video_path.exists() and os.path.getsize(str(video_path)) > 1000:  # At least 1KB
                                                        # Validate the file has video streams before extraction
                                                        probe_cmd = [
                                                            'ffprobe', '-v', 'quiet', '-select_streams', 'v:0', 
                                                            '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', str(video_path)
                                                        ]
                                                        result = subprocess.run(probe_cmd, capture_output=True, text=True)
                                                        
                                                        if result.returncode == 0 and 'video' in result.stdout.strip():
                                                            vis_logger.info(f"Video streams confirmed, extracting frames from {video_path}")
                                                            return asyncio.run(self._extract_video_frames_from_file(str(video_path), max_frames=self.MIN_VIDEO_FRAMES))
                                                        else:
                                                            vis_logger.warning(f"No video streams found in downloaded file: {video_path}")
                                                            return []
                                                    else:
                                                        vis_logger.warning(f"Downloaded file is empty or doesn't exist: {video_path}")
                                                        return []
                                                    
                                            except Exception as e:
                                                vis_logger.warning(f"yt-dlp download failed: {e}")
                                                return []
                                        
                                        return []
                                    
                                    video_frames = await loop.run_in_executor(None, _download_twitter_video_and_extract)
                                    
                                except Exception as e:
                                    vis_logger.warning(f"yt-dlp Twitter video processing failed: {e}")
                                    video_frames = []
                            
                            # If yt-dlp failed or unavailable, try direct download as last resort
                            if not video_frames:
                                try:
                                    vis_logger.info(f"Trying direct download for Twitter video: {video_url}")
                                    
                                    # Check if ffmpeg is available
                                    ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                                    if ffmpeg_check.returncode != 0:
                                        vis_logger.warning("FFmpeg not available for Twitter video processing")
                                    else:
                                        # Try direct download with Twitter-specific headers
                                        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_video:
                                            try:
                                                async with aiohttp.ClientSession() as session:
                                                    headers = {
                                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                                        "Referer": "https://twitter.com/",
                                                        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8"
                                                    }
                                                    async with session.get(video_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                                        if resp.status == 200:
                                                            temp_video.write(await resp.read())
                                                            temp_video_path = temp_video.name
                                                        else:
                                                            vis_logger.warning(f"Failed to download Twitter video: HTTP {resp.status}")
                                                            continue
                                                
                                                # Extract frames from downloaded video
                                                if os.path.exists(temp_video_path) and os.path.getsize(temp_video_path) > 0:
                                                    video_frames = await self._extract_video_frames_from_file(temp_video_path, max_frames=self.MIN_VIDEO_FRAMES)
                                                else:
                                                    vis_logger.warning("Downloaded video file is empty or doesn't exist")
                                                    
                                            finally:
                                                try:
                                                    os.unlink(temp_video_path)
                                                except:
                                                    pass
                                                    
                                except Exception as direct_error:
                                    vis_logger.warning(f"Direct video download failed: {direct_error}")
                            
                            # Add successful frames to content
                            for frame_base64 in video_frames:
                                all_media_content.append({
                                    "type": "image_url", 
                                    "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                                })
                                extracted_video_frames += 1
                                
                            if video_frames:
                                vis_logger.info(f"Successfully extracted {len(video_frames)} frames from Twitter video")
                            else:
                                vis_logger.warning(f"Failed to extract frames from Twitter video: {video_url}")
                                
                        except Exception as e:
                            vis_logger.error(f"Failed to process Twitter video {video_url}: {e}")
                            continue
                    
                    # Provide accurate feedback
                    total_extracted = extracted_images + extracted_video_frames
                    original_items = len(twitter_images) + len(twitter_videos)
                    
                    if total_extracted > 0:
                        try:
                            if extracted_video_frames > 0:
                                media_descriptions.append(f"Twitter/X post ({extracted_images} images, {extracted_video_frames} video frames)")
                                await processing_msg.edit(content=f"✅ Extracted {extracted_images} images and {extracted_video_frames} frames from Twitter/X post")
                            else:
                                media_descriptions.append(f"Twitter/X post ({extracted_images} images)")
                                await processing_msg.edit(content=f"✅ Extracted {extracted_images} images from Twitter/X post")
                            
                            await asyncio.sleep(1)
                            await processing_msg.delete()
                        except (discord.NotFound, discord.HTTPException, RuntimeError) as msg_error:
                            # Handle cases where Discord session is closed or message is already deleted
                            vis_logger.warning(f"Failed to update status message: {msg_error}")
                            # Still add to media descriptions for analysis
                            if extracted_video_frames > 0:
                                media_descriptions.append(f"Twitter/X post ({extracted_images} images, {extracted_video_frames} video frames)")
                            else:
                                media_descriptions.append(f"Twitter/X post ({extracted_images} images)")
                            
                    elif original_items > 0:
                        try:
                            # Media was found but processing failed
                            await processing_msg.edit(content=f"⚠️ Found {original_items} media items but failed to process them")
                            await asyncio.sleep(2)
                            await processing_msg.delete()
                        except (discord.NotFound, discord.HTTPException, RuntimeError) as msg_error:
                            vis_logger.warning(f"Failed to update error message: {msg_error}")
                    else:
                        try:
                            # No media found at all
                            await processing_msg.edit(content="⚠️ No media found in Twitter/X post")
                            await asyncio.sleep(2)
                            await processing_msg.delete()
                        except (discord.NotFound, discord.HTTPException, RuntimeError) as msg_error:
                            vis_logger.warning(f"Failed to update no media message: {msg_error}")
                        
                except Exception as e:
                    vis_logger.error(f"Failed to extract Twitter/X media: {e}")
                    try:
                        await ctx.send(f"⚠️ Failed to extract media from Twitter/X post: {str(e)}")
                    except (discord.HTTPException, RuntimeError) as send_error:
                        # Handle case where Discord session is closed
                        vis_logger.warning(f"Failed to send error message due to session issue: {send_error}")
            
            # Process attachments with enhanced debugging and validation
            vis_logger.debug(f"Checking for attachments - found {len(ctx.message.attachments)} attachments")
            
            if ctx.message.attachments:
                vis_logger.info(f"Processing {len(ctx.message.attachments)} attachments")
                
                for idx, attachment in enumerate(ctx.message.attachments):
                    filename_lower = attachment.filename.lower()
                    vis_logger.debug(f"Processing attachment {idx+1}: {attachment.filename} (size: {attachment.size} bytes)")
                    
                    # Handle images with enhanced validation
                    if any(filename_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp']):
                        try:
                            vis_logger.info(f"Processing image attachment: {attachment.filename}")
                            processing_msg = await ctx.send(f"🖼️ Processing image: {attachment.filename}")
                            
                            # Download image with proper headers and validation
                            async with aiohttp.ClientSession() as session:
                                headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                }
                                async with session.get(attachment.url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                    if resp.status == 200:
                                        image_data = await resp.read()
                                        vis_logger.debug(f"Downloaded image data: {len(image_data)} bytes")
                                        
                                        # Validate image data
                                        if len(image_data) == 0:
                                            raise ValueError("Downloaded image data is empty")
                                        if len(image_data) > 50 * 1024 * 1024:  # 50MB limit
                                            raise ValueError("Image file too large (>50MB)")
                                        
                                        # Convert to base64 with validation
                                        try:
                                            base64_image = base64.b64encode(image_data).decode('utf-8')
                                            vis_logger.debug(f"Base64 encoded image: {len(base64_image)} characters")
                                            
                                            # Validate base64 encoding
                                            if not base64_image or len(base64_image) < 100:
                                                raise ValueError("Base64 encoding produced invalid result")
                                            
                                            # Determine MIME type
                                            mime_type = "image/jpeg"  # Default
                                            if filename_lower.endswith('.png'):
                                                mime_type = "image/png"
                                            elif filename_lower.endswith('.webp'):
                                                mime_type = "image/webp"
                                            elif filename_lower.endswith('.bmp'):
                                                mime_type = "image/bmp"
                                            
                                            # Add to media content with validation
                                            media_item = {
                                                "type": "image_url",
                                                "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                                            }
                                            all_media_content.append(media_item)
                                            media_descriptions.append(f"image ({attachment.filename})")
                                            
                                            vis_logger.info(f"Successfully processed image: {attachment.filename} -> {len(all_media_content)} total media items")
                                            
                                            await processing_msg.edit(content=f"✅ Processed image: {attachment.filename}")
                                            await asyncio.sleep(1)
                                            await processing_msg.delete()
                                            
                                        except Exception as encode_error:
                                            vis_logger.error(f"Base64 encoding failed for {attachment.filename}: {encode_error}")
                                            raise ValueError(f"Failed to encode image: {encode_error}")
                                            
                                    else:
                                        raise ValueError(f"Failed to download image: HTTP {resp.status}")
                            
                        except Exception as e:
                            vis_logger.error(f"Failed to process image {attachment.filename}: {e}")
                            try:
                                await processing_msg.edit(content=f"❌ Failed to process image: {attachment.filename}")
                                await asyncio.sleep(2)
                                await processing_msg.delete()
                            except:
                                pass
                            await ctx.send(f"⚠️ Failed to process image {attachment.filename}: {str(e)}")
                    
                    # Handle GIF files with enhanced validation
                    elif filename_lower.endswith('.gif'):
                        try:
                            vis_logger.info(f"Processing GIF attachment: {attachment.filename}")
                            processing_msg = await ctx.send(f"🎞️ Processing GIF: {attachment.filename}")
                            
                            # Download GIF to temporary file with validation
                            with tempfile.NamedTemporaryFile(suffix='.gif', delete=False) as temp_gif:
                                async with aiohttp.ClientSession() as session:
                                    headers = {
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                    }
                                    async with session.get(attachment.url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                        if resp.status == 200:
                                            gif_data = await resp.read()
                                            if len(gif_data) == 0:
                                                raise ValueError("Downloaded GIF data is empty")
                                            temp_gif.write(gif_data)
                                            temp_gif_path = temp_gif.name
                                            vis_logger.debug(f"Downloaded GIF to {temp_gif_path}: {len(gif_data)} bytes")
                                        else:
                                            raise ValueError(f"Failed to download GIF: HTTP {resp.status}")
                            
                            # Extract frames from GIF with validation
                            try:
                                gif_frames = await self._extract_gif_frames(temp_gif_path, max_frames=self.MIN_VIDEO_FRAMES)
                                vis_logger.debug(f"Extracted {len(gif_frames)} frames from GIF")
                                
                                if gif_frames:
                                    frames_added = 0
                                    for frame_idx, frame_base64 in enumerate(gif_frames):
                                        if frame_base64 and len(frame_base64) > 100:  # Validate frame
                                            all_media_content.append({
                                                "type": "image_url",
                                                "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                                            })
                                            frames_added += 1
                                    
                                    if frames_added > 0:
                                        media_descriptions.append(f"GIF ({frames_added} frames)")
                                        vis_logger.info(f"Successfully processed GIF: {attachment.filename} -> {frames_added} frames")
                                        
                                        await processing_msg.edit(content=f"✅ Extracted {frames_added} frames from GIF")
                                        await asyncio.sleep(1)
                                        await processing_msg.delete()
                                    else:
                                        raise ValueError("No valid frames extracted from GIF")
                                else:
                                    raise ValueError("GIF frame extraction returned no frames")
                                    
                            finally:
                                # Clean up temporary file
                                try:
                                    os.unlink(temp_gif_path)
                                    vis_logger.debug(f"Cleaned up temporary GIF file: {temp_gif_path}")
                                except Exception as cleanup_error:
                                    vis_logger.warning(f"Failed to cleanup temp GIF file: {cleanup_error}")
                            
                        except Exception as e:
                            vis_logger.error(f"Failed to process GIF {attachment.filename}: {e}")
                            try:
                                await processing_msg.edit(content=f"❌ Failed to process GIF: {attachment.filename}")
                                await asyncio.sleep(2)
                                await processing_msg.delete()
                            except:
                                pass
                            await ctx.send(f"⚠️ Failed to process GIF {attachment.filename}: {str(e)}")
                    
                    # Handle video files with enhanced validation
                    elif any(filename_lower.endswith(ext) for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv']):
                        try:
                            vis_logger.info(f"Processing video attachment: {attachment.filename}")
                            
                            # Check if ffmpeg is available
                            try:
                                ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                                if ffmpeg_check.returncode != 0:
                                    await ctx.send("⚠️ FFmpeg is not installed. Video analysis requires FFmpeg for frame extraction.")
                                    continue
                            except FileNotFoundError:
                                await ctx.send("⚠️ FFmpeg is not installed. Please install FFmpeg to analyze videos.")
                                continue
                            
                            processing_msg = await ctx.send(f"🎬 Processing video: {attachment.filename}")
                            
                            # Download video to temporary file with validation
                            with tempfile.NamedTemporaryFile(suffix=f".{filename_lower.split('.')[-1]}", delete=False) as temp_video:
                                async with aiohttp.ClientSession() as session:
                                    headers = {
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                    }
                                    async with session.get(attachment.url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                                        if resp.status == 200:
                                            video_data = await resp.read()
                                            if len(video_data) == 0:
                                                raise ValueError("Downloaded video data is empty")
                                            temp_video.write(video_data)
                                            temp_video_path = temp_video.name
                                            vis_logger.debug(f"Downloaded video to {temp_video_path}: {len(video_data)} bytes")
                                        else:
                                            raise ValueError(f"Failed to download video: HTTP {resp.status}")
                            
                            # Extract frames from video with validation
                            try:
                                video_frames = await self._extract_video_frames_from_file(temp_video_path, max_frames=self.MIN_VIDEO_FRAMES)
                                vis_logger.debug(f"Extracted {len(video_frames)} frames from video")
                                
                                if video_frames:
                                    frames_added = 0
                                    for frame_idx, frame_base64 in enumerate(video_frames):
                                        if frame_base64 and len(frame_base64) > 100:  # Validate frame
                                            all_media_content.append({
                                                "type": "image_url",
                                                "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                                            })
                                            frames_added += 1
                                    
                                    if frames_added > 0:
                                        media_descriptions.append(f"video ({frames_added} frames)")
                                        vis_logger.info(f"Successfully processed video: {attachment.filename} -> {frames_added} frames")
                                        
                                        await processing_msg.edit(content=f"✅ Extracted {frames_added} frames from video")
                                        await asyncio.sleep(1)
                                        await processing_msg.delete()
                                    else:
                                        raise ValueError("No valid frames extracted from video")
                                else:
                                    raise ValueError("Video frame extraction returned no frames")
                                    
                            finally:
                                # Clean up temporary file
                                try:
                                    os.unlink(temp_video_path)
                                    vis_logger.debug(f"Cleaned up temporary video file: {temp_video_path}")
                                except Exception as cleanup_error:
                                    vis_logger.warning(f"Failed to cleanup temp video file: {cleanup_error}")
                            
                        except Exception as e:
                            vis_logger.error(f"Failed to process video {attachment.filename}: {e}")
                            try:
                                await processing_msg.edit(content=f"❌ Failed to process video: {attachment.filename}")
                                await asyncio.sleep(2)
                                await processing_msg.delete()
                            except:
                                pass
                            await ctx.send(f"⚠️ Failed to process video {attachment.filename}: {str(e)}")
                    
                    else:
                        # Unsupported file type
                        vis_logger.warning(f"Unsupported file type: {attachment.filename}")
                        await ctx.send(f"⚠️ Unsupported file type: {attachment.filename}. Supported: images (png, jpg, jpeg, webp, bmp), GIFs, videos (mp4, webm, avi, mov, mkv)")
                
                vis_logger.info(f"Attachment processing complete - {len(all_media_content)} total media items")
            else:
                vis_logger.debug("No attachments found in message")
            
            # Enhanced validation for media content
            vis_logger.debug(f"Final media content check: {len(all_media_content)} items, descriptions: {media_descriptions}")
            
            # Check if we have any media to analyze
            if not all_media_content:
                vis_logger.warning("No visual content found for analysis")
                if youtube_match or twitter_urls:
                    return  # URL processing already handled the error
                
                # Provide helpful error message
                error_msg = "❌ No visual content found. Please:\n"
                error_msg += "• Attach images (PNG, JPG, JPEG, WebP, BMP)\n"
                error_msg += "• Attach GIF files\n" 
                error_msg += "• Attach videos (MP4, WebM, AVI, MOV, MKV)\n"
                error_msg += "• Provide YouTube URL\n"
                error_msg += "• Provide Twitter/X post URL"
                
                return await ctx.send(error_msg)
            
            # Build the analysis prompt with validation
            media_summary = ", ".join(media_descriptions) if media_descriptions else "visual content"
            analysis_prompt = f"Analyzing {media_summary}.\n\nUser request: {user_prompt}"
            
            vis_logger.info(f"Preparing vision model request with {len(all_media_content)} media items")
            vis_logger.debug(f"Analysis prompt: {analysis_prompt}")
            
            # Prepare messages for vision model with validation
            user_content = [{"type": "text", "text": analysis_prompt}]
            
            # Add all media content and validate structure
            for idx, media_item in enumerate(all_media_content):
                if not isinstance(media_item, dict) or "type" not in media_item:
                    vis_logger.error(f"Invalid media item at index {idx}: {media_item}")
                    continue
                    
                if media_item["type"] != "image_url" or "image_url" not in media_item:
                    vis_logger.error(f"Invalid image_url structure at index {idx}: {media_item}")
                    continue
                    
                user_content.append(media_item)
                vis_logger.debug(f"Added media item {idx+1} to vision model request")
            
            if len(user_content) == 1:  # Only text prompt, no media
                vis_logger.error("No valid media items found for vision model")
                return await ctx.send("❌ Failed to prepare media for analysis. Please try again.")
            
            messages = [
                {"role": "system", "content": "You are an expert visual analyst. Provide detailed, comprehensive descriptions of visual content with definitive assessments when possible. Confidently identify objects, people, actions, colors, composition, text, and other visual elements. When you can clearly observe something, state it directly. Only express uncertainty when visual elements are genuinely ambiguous or unclear. Be thorough but organized in your analysis, prioritizing clear, decisive observations."},
                {"role": "user", "content": user_content}
            ]
            
            vis_logger.debug(f"Vision model request prepared: {len(messages)} messages, {len(user_content)} content items")
            
            # Call vision model for analysis with enhanced error handling
            payload = {
                "model": "qwen/qwen2.5-vl-7b",
                "messages": messages,
                "max_tokens": self.LMSTUDIO_MAX_TOKENS,
                "stream": True,
                "keep_alive": self.MODEL_TTL_SECONDS
            }
            
            accumulated = ""
            try:
                analysis_msg = await ctx.send(f"🔍 Analyzing {media_summary}...")
                vis_logger.info("Starting vision model analysis...")
                
                timeout = aiohttp.ClientTimeout(total=None)
                headers = {"Accept": "text/event-stream"}
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.LMSTUDIO_CHAT_URL, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            vis_logger.error(f"Visual analysis API HTTP {resp.status}: {err}")
                            try:
                                await analysis_msg.edit(content="❌ Failed to connect to vision model")
                            except (discord.NotFound, discord.HTTPException, RuntimeError):
                                await ctx.send("❌ Failed to connect to vision model")
                            return
                        
                        chunk_count = 0
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
                                    chunk_count += 1
                                if obj.get('choices', [{}])[0].get('finish_reason') == 'stop':
                                    break
                            except Exception as parse_error:
                                vis_logger.warning(f"Failed to parse vision model response chunk: {parse_error}")
                                continue
                
                vis_logger.info(f"Vision model analysis complete: {len(accumulated)} characters from {chunk_count} chunks")
                
                try:
                    await analysis_msg.delete()
                except (discord.NotFound, discord.HTTPException, RuntimeError):
                    pass  # Ignore if message deletion fails
                
                if not accumulated:
                    vis_logger.warning("Vision model returned empty response")
                    return await ctx.send("❌ Empty analysis response from vision model.")
                
                # Clean up AI artifacts
                original_length = len(accumulated)
                accumulated = re.sub(r"<think>.*?</think>", "", accumulated, flags=re.DOTALL)
                accumulated = re.sub(r"<think>.*", "", accumulated, flags=re.DOTALL)
                accumulated = accumulated.replace("<think>", "").replace("</think>", "")
                
                vis_logger.debug(f"Cleaned response: {len(accumulated)} characters (was {original_length})")
                
                # Send the analysis in chunks
                await self._send_limited(ctx, accumulated)
                
                # Store in memory
                memory_prompt = f"Visual analysis of {media_summary}: {user_prompt}"
                self._remember(ctx.author.id, "user", memory_prompt)
                self._remember(ctx.author.id, "assistant", accumulated)
                # Use enhanced context storage for per-user isolation
                if self._set_user_context:
                    self._set_user_context(ctx.author.id, accumulated)
                else:
                    # Fallback to direct access if enhanced functions not available
                    self._user_context[ctx.author.id] = accumulated
                
                vis_logger.info("Visual analysis completed successfully")
                
            except Exception as e:
                vis_logger.error("Visual analysis error", exc_info=e)
                try:
                    await ctx.send(f"⚠️ Visual analysis failed: {e}")
                except (discord.HTTPException, RuntimeError) as send_error:
                    vis_logger.warning(f"Failed to send analysis error message: {send_error}")

    async def _detect_media_type(self, attachment):
        """Detect media type and processing method for an attachment.
        
        Returns:
            tuple: (media_type, processing_method, mime_type)
            media_type: 'image', 'video', 'gif', 'unsupported'
            processing_method: 'direct', 'frames', 'unsupported'
            mime_type: MIME type for the file
        """
        filename_lower = attachment.filename.lower()
        
        # Image types - direct base64 encoding
        if any(filename_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp']):
            mime_type = "image/jpeg"  # Default
            if filename_lower.endswith('.png'):
                mime_type = "image/png"
            elif filename_lower.endswith('.webp'):
                mime_type = "image/webp"
            elif filename_lower.endswith('.bmp'):
                mime_type = "image/bmp"
            return ('image', 'direct', mime_type)
        
        # GIF files - frame extraction
        elif filename_lower.endswith('.gif'):
            return ('gif', 'frames', 'image/gif')
        
        # Video files - frame extraction
        elif any(filename_lower.endswith(ext) for ext in ['.mp4', '.webm', '.avi', '.mov', '.mkv', '.m4v', '.flv']):
            return ('video', 'frames', 'video/mp4')
        
        # Unsupported
        else:
            return ('unsupported', 'unsupported', 'application/octet-stream')

    async def _download_and_validate_attachment(self, attachment, expected_type='any'):
        """Download and validate an attachment with comprehensive error handling.
        
        Args:
            attachment: Discord attachment object
            expected_type: 'image', 'video', 'gif', or 'any'
            
        Returns:
            bytes: Downloaded data, or None if failed
        """
        try:
            vis_logger.info(f"Downloading {expected_type} attachment: {attachment.filename} ({attachment.size} bytes)")
            
            # Size validation
            if attachment.size == 0:
                raise ValueError("Attachment is empty (0 bytes)")
            if attachment.size > 100 * 1024 * 1024:  # 100MB limit
                raise ValueError(f"File too large: {attachment.size / (1024*1024):.1f}MB (max 100MB)")
            
            # Download with proper headers and timeout
            timeout_seconds = 60 if expected_type == 'video' else 30
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive"
                }
                
                async with session.get(
                    attachment.url, 
                    headers=headers, 
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds)
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(f"Download failed: HTTP {resp.status}")
                    
                    # Stream download for large files
                    data = await resp.read()
                    
                    # Validate downloaded data
                    if len(data) == 0:
                        raise ValueError("Downloaded data is empty")
                    if len(data) != attachment.size:
                        vis_logger.warning(f"Size mismatch: expected {attachment.size}, got {len(data)} bytes")
                    
                    vis_logger.debug(f"Successfully downloaded: {len(data)} bytes")
                    return data
                    
        except asyncio.TimeoutError:
            raise ValueError(f"Download timeout after {timeout_seconds}s")
        except aiohttp.ClientError as e:
            raise ValueError(f"Network error: {str(e)}")
        except Exception as e:
            raise ValueError(f"Download failed: {str(e)}")

    async def _process_image_attachment(self, ctx, attachment):
        """Process image attachment and return base64-encoded data.
        
        Returns:
            dict: Media content item for vision model, or None if failed
        """
        try:
            processing_msg = await ctx.send(f"🖼️ Processing image: {attachment.filename}")
            
            # Detect media type and download
            media_type, processing_method, mime_type = await self._detect_media_type(attachment)
            if media_type != 'image':
                raise ValueError(f"Expected image, got {media_type}")
            
            image_data = await self._download_and_validate_attachment(attachment, 'image')
            if not image_data:
                raise ValueError("Failed to download image data")
            
            # Convert to base64 with validation
            base64_image = base64.b64encode(image_data).decode('utf-8')
            if not base64_image or len(base64_image) < 100:
                raise ValueError("Base64 encoding produced invalid result")
            
            # Create media content item
            media_item = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
            }
            
            vis_logger.info(f"Successfully processed image: {attachment.filename}")
            await processing_msg.edit(content=f"✅ Processed image: {attachment.filename}")
            await asyncio.sleep(1)
            await processing_msg.delete()
            
            return media_item
            
        except Exception as e:
            vis_logger.error(f"Failed to process image {attachment.filename}: {e}")
            try:
                await processing_msg.edit(content=f"❌ Failed to process image: {attachment.filename}")
                await asyncio.sleep(2)
                await processing_msg.delete()
            except:
                pass
            return None

    async def _process_video_attachment(self, ctx, attachment):
        """Process video attachment and return frame data.
        
        Returns:
            list: List of media content items (frames), or empty list if failed
        """
        try:
            processing_msg = await ctx.send(f"🎬 Processing video: {attachment.filename}")
            
            # Check FFmpeg availability
            try:
                ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                if ffmpeg_check.returncode != 0:
                    raise ValueError("FFmpeg is not working properly")
            except FileNotFoundError:
                raise ValueError("FFmpeg is not installed - required for video analysis")
            
            # Detect media type and download
            media_type, processing_method, mime_type = await self._detect_media_type(attachment)
            if media_type != 'video':
                raise ValueError(f"Expected video, got {media_type}")
            
            video_data = await self._download_and_validate_attachment(attachment, 'video')
            if not video_data:
                raise ValueError("Failed to download video data")
            
            # Save to temporary file
            file_extension = attachment.filename.lower().split('.')[-1]
            with tempfile.NamedTemporaryFile(suffix=f".{file_extension}", delete=False) as temp_video:
                temp_video.write(video_data)
                temp_video_path = temp_video.name
            
            try:
                # Extract frames
                video_frames = await self._extract_video_frames_from_file(temp_video_path, max_frames=self.MIN_VIDEO_FRAMES)
                
                if not video_frames:
                    raise ValueError("No frames could be extracted from video")
                
                # Convert frames to media content items
                media_items = []
                for frame_idx, frame_base64 in enumerate(video_frames):
                    if frame_base64 and len(frame_base64) > 100:  # Validate frame
                        media_items.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                        })
                
                if not media_items:
                    raise ValueError("No valid frames produced from video")
                
                vis_logger.info(f"Successfully processed video: {attachment.filename} -> {len(media_items)} frames")
                await processing_msg.edit(content=f"✅ Extracted {len(media_items)} frames from video")
                await asyncio.sleep(1)
                await processing_msg.delete()
                
                return media_items
                
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_video_path)
                except Exception as cleanup_error:
                    vis_logger.warning(f"Failed to cleanup temp video file: {cleanup_error}")
            
        except Exception as e:
            vis_logger.error(f"Failed to process video {attachment.filename}: {e}")
            try:
                await processing_msg.edit(content=f"❌ Failed to process video: {str(e)}")
                await asyncio.sleep(2)
                await processing_msg.delete()
            except:
                pass
            return []

    async def _process_attachments_enhanced(self, ctx):
        """Enhanced attachment processing with better error handling and validation.
        
        Returns:
            tuple: (all_media_content, media_descriptions, success_count, error_count)
        """
        all_media_content = []
        media_descriptions = []
        success_count = 0
        error_count = 0
        
        if not ctx.message.attachments:
            vis_logger.debug("No attachments found in message")
            return all_media_content, media_descriptions, success_count, error_count
        
        vis_logger.info(f"Processing {len(ctx.message.attachments)} attachments")
        
        for idx, attachment in enumerate(ctx.message.attachments):
            try:
                vis_logger.debug(f"Processing attachment {idx+1}/{len(ctx.message.attachments)}: {attachment.filename}")
                
                # Detect media type
                media_type, processing_method, mime_type = await self._detect_media_type(attachment)
                vis_logger.debug(f"Detected type: {media_type}, method: {processing_method}, mime: {mime_type}")
                
                if media_type == 'unsupported':
                    vis_logger.warning(f"Unsupported file type: {attachment.filename}")
                    await ctx.send(f"⚠️ Unsupported file type: {attachment.filename}. Supported types: images (PNG, JPG, JPEG, WebP, BMP), GIFs, videos (MP4, WebM, AVI, MOV, MKV)")
                    error_count += 1
                    continue
                
                # Process based on media type
                if media_type == 'image':
                    media_item = await self._process_image_attachment(ctx, attachment)
                    if media_item:
                        all_media_content.append(media_item)
                        media_descriptions.append(f"image ({attachment.filename})")
                        success_count += 1
                    else:
                        error_count += 1
                
                elif media_type == 'gif':
                    # Use existing GIF processing logic
                    media_items = await self._process_gif_attachment(ctx, attachment)
                    if media_items:
                        all_media_content.extend(media_items)
                        media_descriptions.append(f"GIF ({len(media_items)} frames)")
                        success_count += 1
                    else:
                        error_count += 1
                
                elif media_type == 'video':
                    media_items = await self._process_video_attachment(ctx, attachment)
                    if media_items:
                        all_media_content.extend(media_items)
                        media_descriptions.append(f"video ({len(media_items)} frames)")
                        success_count += 1
                    else:
                        error_count += 1
                
            except Exception as e:
                vis_logger.error(f"Unexpected error processing attachment {attachment.filename}: {e}")
                await ctx.send(f"⚠️ Unexpected error processing {attachment.filename}: {str(e)}")
                error_count += 1
                continue
        
        vis_logger.info(f"Attachment processing complete: {success_count} successful, {error_count} errors, {len(all_media_content)} total media items")
        return all_media_content, media_descriptions, success_count, error_count

    async def _extract_gif_frames_fallback(self, gif_path: str, max_frames: int = 5) -> List[str]:
        """Fallback GIF frame extraction using FFmpeg when PIL is not available.
        
        Returns list of base64-encoded JPEG images.
        """
        frames_base64: List[str] = []
        
        try:
            vis_logger.info(f"Using FFmpeg fallback for GIF frame extraction: {gif_path}")
            
            # Check if ffmpeg is available
            try:
                ffmpeg_check = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                if ffmpeg_check.returncode != 0:
                    vis_logger.error("FFmpeg not available for GIF fallback processing")
                    return frames_base64
            except FileNotFoundError:
                vis_logger.error("FFmpeg not installed - cannot process GIF without PIL")
                return frames_base64
            
            # Use FFmpeg to extract frames from GIF
            loop = asyncio.get_running_loop()
            
            def run_ffmpeg_gif_extraction():
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    
                    # Extract frames at regular intervals
                    extracted_frames = []
                    for i in range(max_frames):
                        frame_path = temp_path / f"frame_{i}.jpg"
                        
                        # Extract frame using FFmpeg
                        cmd = [
                            'ffmpeg', '-i', gif_path,
                            '-vf', f'select=eq(n\\,{i*2})',  # Select every 2nd frame
                            '-vframes', '1', '-q:v', '2',
                            '-vf', 'scale=640:-1',  # Resize to reasonable size
                            str(frame_path), '-y'
                        ]
                        
                        try:
                            result = subprocess.run(cmd, capture_output=True, text=True)
                            if result.returncode == 0 and frame_path.exists():
                                with open(frame_path, 'rb') as f:
                                    frame_data = f.read()
                                if len(frame_data) > 0:
                                    frame_base64 = base64.b64encode(frame_data).decode('utf-8')
                                    extracted_frames.append(frame_base64)
                        except Exception:
                            continue  # Skip failed frames
                    
                    return extracted_frames
            
            frames_base64 = await loop.run_in_executor(None, run_ffmpeg_gif_extraction)
            vis_logger.info(f"FFmpeg fallback extracted {len(frames_base64)} frames from GIF")
            return frames_base64
            
        except Exception as e:
            vis_logger.error(f"GIF fallback extraction failed: {e}")
            return frames_base64

    async def _process_gif_attachment(self, ctx, attachment):
        """Process GIF attachment and return frame data.
        
        Returns:
            list: List of media content items (frames), or empty list if failed  
        """
        try:
            processing_msg = await ctx.send(f"🎞️ Processing GIF: {attachment.filename}")
            
            # Detect media type and download
            media_type, processing_method, mime_type = await self._detect_media_type(attachment)
            if media_type != 'gif':
                raise ValueError(f"Expected GIF, got {media_type}")
            
            gif_data = await self._download_and_validate_attachment(attachment, 'gif')
            if not gif_data:
                raise ValueError("Failed to download GIF data")
            
            # Save to temporary file
            with tempfile.NamedTemporaryFile(suffix='.gif', delete=False) as temp_gif:
                temp_gif.write(gif_data)
                temp_gif_path = temp_gif.name
            
            try:
                # Try PIL first if available, otherwise use FFmpeg fallback
                if _PIL_AVAILABLE and Image is not None:
                    vis_logger.debug("Using PIL for GIF frame extraction")
                    gif_frames = await self._extract_gif_frames(temp_gif_path, max_frames=self.MIN_VIDEO_FRAMES)
                    extraction_method = "PIL"
                else:
                    # Fallback to FFmpeg
                    vis_logger.info("PIL not available, using FFmpeg fallback for GIF processing")
                    await processing_msg.edit(content=f"🎞️ Processing GIF (FFmpeg fallback): {attachment.filename}")
                    gif_frames = await self._extract_gif_frames_fallback(temp_gif_path, max_frames=self.MIN_VIDEO_FRAMES)
                    extraction_method = "FFmpeg"
                
                if not gif_frames:
                    error_msg = f"No frames could be extracted from GIF using {extraction_method}"
                    if not _PIL_AVAILABLE:
                        error_msg += ". Install Pillow for better GIF support: pip install Pillow"
                    raise ValueError(error_msg)
                
                # Convert frames to media content items
                media_items = []
                for frame_idx, frame_base64 in enumerate(gif_frames):
                    if frame_base64 and len(frame_base64) > 100:  # Validate frame
                        media_items.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}"}
                        })
                
                if not media_items:
                    raise ValueError(f"No valid frames produced from GIF using {extraction_method}")
                
                vis_logger.info(f"Successfully processed GIF using {extraction_method}: {attachment.filename} -> {len(media_items)} frames")
                await processing_msg.edit(content=f"✅ Extracted {len(media_items)} frames from GIF ({extraction_method})")
                await asyncio.sleep(1)
                await processing_msg.delete()
                
                return media_items
                
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_gif_path)
                except Exception as cleanup_error:
                    vis_logger.warning(f"Failed to cleanup temp GIF file: {cleanup_error}")
            
        except Exception as e:
            vis_logger.error(f"Failed to process GIF {attachment.filename}: {e}")
            
            # Provide helpful error message
            error_msg = f"❌ Failed to process GIF: {str(e)}"
            if not _PIL_AVAILABLE and "PIL" not in str(e):
                error_msg += "\n💡 For better GIF support, install: pip install Pillow"
            
            try:
                await processing_msg.edit(content=error_msg)
                await asyncio.sleep(3)
                await processing_msg.delete()
            except:
                pass
            return []


def setup_vis_commands(bot, memory_system, video_system, twitter_system):
    """Setup function to add visual analysis commands to the bot"""
    vis_commands = VisCommands(bot, memory_system, video_system, twitter_system)
    return vis_commands 