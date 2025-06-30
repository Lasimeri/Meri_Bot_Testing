"""
Voice and Music Commands Module for Meri Bot

This module contains all voice connection and music streaming functionality.
Separated from main bot file for better code organization and maintainability.

Commands included:
- Music playback: play, skip, pause, resume, stop
- Queue management: queue, nowplaying
- Playback controls: volume, loop
- Voice utilities: join, leave, voice status
- Search: musicsearch
"""

import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from typing import Dict, List, Any, Optional

# Import yt-dlp with fallback
try:
    import yt_dlp  # type: ignore
except ImportError:
    yt_dlp = None

# Set up logger for voice operations
voice_logger = logging.getLogger("MeriVoice")

# Music queue and playback state management
_MUSIC_QUEUES: Dict[int, List[Dict[str, Any]]] = {}  # {guild_id: [song_info]}
_MUSIC_STATE: Dict[int, Dict[str, Any]] = {}  # {guild_id: {current_song, volume, loop}}


class MusicSource(discord.PCMVolumeTransformer):
    """Custom audio source with volume control and metadata"""
    
    def __init__(self, source, *, volume=0.5, data=None):
        super().__init__(source, volume=volume)
        self.data = data or {}
        self.title = self.data.get('title', 'Unknown')
        self.url = self.data.get('webpage_url', '')
        self.duration = self.data.get('duration')
        self.requester = self.data.get('requester')


async def _get_youtube_info(url_or_query: str, search_mode: bool = False):
    """Extract YouTube video info or search for videos"""
    if yt_dlp is None:
        raise RuntimeError("yt_dlp not available")
    
    loop = asyncio.get_running_loop()
    
    def _extract():
        # Basic yt_dlp options without authentication
        ytdl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'extractflat': False,
        }
        
        if search_mode:
            # Search YouTube for the query
            ytdl_opts['default_search'] = 'ytsearch1:'
        
        with yt_dlp.YoutubeDL(ytdl_opts) as ydl:  # type: ignore[attr-defined]
            return ydl.extract_info(url_or_query, download=False)
    
    return await loop.run_in_executor(None, _extract)


async def _play_next_song(guild_id: int, voice_client):
    """Play the next song in the queue"""
    if guild_id not in _MUSIC_QUEUES or not _MUSIC_QUEUES[guild_id]:
        # Queue is empty
        if guild_id in _MUSIC_STATE:
            _MUSIC_STATE[guild_id]['current_song'] = None
        return
    
    # Get next song from queue
    next_song = _MUSIC_QUEUES[guild_id].pop(0)
    
    try:
        # Create audio source
        ffmpeg_opts = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }
        
        source = discord.FFmpegPCMAudio(next_song['url'], **ffmpeg_opts)  # type: ignore[arg-type]
        volume = _MUSIC_STATE.get(guild_id, {}).get('volume', 0.5)
        music_source = MusicSource(source, volume=volume, data=next_song)
        
        # Update current song state
        if guild_id not in _MUSIC_STATE:
            _MUSIC_STATE[guild_id] = {}
        _MUSIC_STATE[guild_id]['current_song'] = next_song
        
        # Play with callback to handle song end
        def after_playing(error):
            if error:
                voice_logger.error(f"Player error: {error}")
            
            # Check if loop is enabled
            loop_mode = _MUSIC_STATE.get(guild_id, {}).get('loop', False)
            if loop_mode and guild_id in _MUSIC_STATE and _MUSIC_STATE[guild_id]['current_song']:
                # Add current song back to queue for looping
                _MUSIC_QUEUES.setdefault(guild_id, []).insert(0, _MUSIC_STATE[guild_id]['current_song'])
            
            # Schedule next song
            asyncio.create_task(_play_next_song(guild_id, voice_client))
        
        voice_client.play(music_source, after=after_playing)
        
    except Exception as e:
        voice_logger.error(f"Failed to play song: {e}")
        # Try next song if this one failed
        asyncio.create_task(_play_next_song(guild_id, voice_client))


class VoiceCommands(commands.Cog):
    """Voice and Music Commands Cog"""
    
    def __init__(self, bot, voice_handler):
        self.bot = bot
        self.voice_handler = voice_handler
    
    @commands.hybrid_command(name="play", description="Play YouTube audio or search and play")
    @app_commands.describe(query="YouTube URL or search query")
    async def play_music(self, ctx, *, query: str):
        """Play YouTube audio with queue support and search functionality."""
        if not query.strip():
            return await ctx.send("❌ Please provide a YouTube URL or search query.")
        
        # Auto-join voice if not already connected, but be more careful about existing connections
        if ctx.voice_client is None:
            if ctx.author.voice is None or ctx.author.voice.channel is None:
                return await ctx.send("❌ You must be in a voice channel to play music.")
            
            try:
                success = await self.voice_handler.join_voice_channel(ctx)
                if not success:
                    return await ctx.send("❌ Failed to join voice channel. Please try `^join` first.")
            except Exception as e:
                voice_logger.error(f"Auto-join failed in play command: {e}")
                return await ctx.send("❌ Could not join voice channel. Please use `^join` command first.")
        elif ctx.voice_client.channel != ctx.author.voice.channel:
            # Bot is connected but to different channel - try to move
            try:
                await ctx.voice_client.move_to(ctx.author.voice.channel)
            except Exception as e:
                voice_logger.warning(f"Failed to move voice client: {e}")
                return await ctx.send("❌ Please use `^join` to connect to your voice channel first.")

        # Final check - make sure we have a voice client before proceeding
        if ctx.voice_client is None:
            return await ctx.send("❌ Not connected to voice. Please use `^join` command first.")

        # Check if yt-dlp is available
        if yt_dlp is None:
            return await ctx.send("❌ yt_dlp dependency missing. Please `pip install yt-dlp`.")

        # Determine if this is a URL or search query
        is_url = any(domain in query for domain in ['youtube.com', 'youtu.be', 'http://', 'https://'])
        
        processing_msg = await ctx.send("🔎 Searching for audio..." if not is_url else "🔎 Fetching audio info...")
        
        try:
            # Extract info from YouTube
            data = await _get_youtube_info(query, search_mode=not is_url)
            
            if data is None:
                raise RuntimeError("No results found")
            
            # Handle search results or playlists
            entries = []
            if 'entries' in data and data['entries']:
                if not is_url:  # Search result
                    entries = [data['entries'][0]]  # Take first search result
                else:  # Playlist
                    entries = data['entries'][:10]  # Limit to 10 songs from playlist
            else:
                entries = [data]  # Single video
            
            guild_id = ctx.guild.id if ctx.guild else 0
            
            # Initialize queue if needed
            if guild_id not in _MUSIC_QUEUES:
                _MUSIC_QUEUES[guild_id] = []
            
            added_songs = []
            for entry in entries:
                if not entry:  # Skip empty entries
                    continue
                    
                song_info = {
                    'url': entry['url'],
                    'title': entry.get('title', 'Unknown'),
                    'duration': entry.get('duration'),
                    'webpage_url': entry.get('webpage_url', ''),
                    'requester': ctx.author.display_name,
                    'requester_id': ctx.author.id
                }
                
                _MUSIC_QUEUES[guild_id].append(song_info)
                added_songs.append(song_info)
            
            if not added_songs:
                await processing_msg.edit(content="❌ No playable audio found.")
                return
            
            # Update message based on what was added
            if len(added_songs) == 1:
                song = added_songs[0]
                duration_str = f" ({song['duration']//60}:{song['duration']%60:02d})" if song['duration'] else ""
                await processing_msg.edit(content=f"✅ Added to queue: **{song['title']}**{duration_str}")
            else:
                await processing_msg.edit(content=f"✅ Added {len(added_songs)} songs to queue")
            
            # Start playing if nothing is currently playing
            if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                await _play_next_song(guild_id, ctx.voice_client)
                
        except Exception as e:
            voice_logger.error("Music play error", exc_info=e)
            await processing_msg.edit(content=f"❌ Failed to play audio: {str(e)}")

    @commands.hybrid_command(name="skip", description="Skip the current song")
    async def skip_song(self, ctx):
        """Skip the currently playing song."""
        if ctx.voice_client is None or not ctx.voice_client.is_playing():
            return await ctx.send("❌ Nothing is currently playing.")
        
        guild_id = ctx.guild.id if ctx.guild else 0
        current_song = _MUSIC_STATE.get(guild_id, {}).get('current_song')
        
        ctx.voice_client.stop()  # This will trigger the after callback to play next song
        
        if current_song:
            await ctx.send(f"⏭️ Skipped: **{current_song['title']}**")
        else:
            await ctx.send("⏭️ Skipped current song")

    @commands.hybrid_command(name="pause", description="Pause the current song")
    async def pause_song(self, ctx):
        """Pause the currently playing song."""
        if ctx.voice_client is None:
            return await ctx.send("❌ Not connected to voice.")
        
        if ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Paused playback")
        elif ctx.voice_client.is_paused():
            await ctx.send("ℹ️ Playback is already paused")
        else:
            await ctx.send("❌ Nothing is currently playing")

    @commands.hybrid_command(name="resume", description="Resume the current song")
    async def resume_song(self, ctx):
        """Resume the currently paused song."""
        if ctx.voice_client is None:
            return await ctx.send("❌ Not connected to voice.")
        
        if ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Resumed playback")
        elif ctx.voice_client.is_playing():
            await ctx.send("ℹ️ Playback is not paused")
        else:
            await ctx.send("❌ Nothing is currently playing")

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue")
    async def stop_music(self, ctx):
        """Stop music playback and clear the queue."""
        if ctx.voice_client is None:
            return await ctx.send("❌ Not connected to voice.")
        
        guild_id = ctx.guild.id if ctx.guild else 0
        
        # Clear queue and stop playback
        if guild_id in _MUSIC_QUEUES:
            _MUSIC_QUEUES[guild_id].clear()
        if guild_id in _MUSIC_STATE:
            _MUSIC_STATE[guild_id]['current_song'] = None
        
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            ctx.voice_client.stop()
            await ctx.send("⏹️ Stopped playback and cleared queue")
        else:
            await ctx.send("ℹ️ Nothing was playing")

    @commands.hybrid_command(name="queue", description="Show the current music queue")
    async def show_queue(self, ctx):
        """Display the current music queue."""
        guild_id = ctx.guild.id if ctx.guild else 0
        
        current_song = _MUSIC_STATE.get(guild_id, {}).get('current_song')
        queue = _MUSIC_QUEUES.get(guild_id, [])
        
        embed = discord.Embed(title="🎵 Music Queue", color=0x9b59b6)
        
        # Current song
        if current_song:
            duration_str = f" ({current_song['duration']//60}:{current_song['duration']%60:02d})" if current_song.get('duration') else ""
            embed.add_field(
                name="🎶 Now Playing",
                value=f"**{current_song['title']}**{duration_str}\nRequested by: {current_song.get('requester', 'Unknown')}",
                inline=False
            )
        else:
            embed.add_field(name="🎶 Now Playing", value="Nothing", inline=False)
        
        # Queue
        if queue:
            queue_text = []
            total_duration = 0
            
            for i, song in enumerate(queue[:10], 1):  # Show first 10 songs
                duration_str = ""
                if song.get('duration'):
                    duration_str = f" ({song['duration']//60}:{song['duration']%60:02d})"
                    total_duration += song['duration']
                
                queue_text.append(f"{i}. **{song['title']}**{duration_str}")
            
            if len(queue) > 10:
                queue_text.append(f"... and {len(queue) - 10} more songs")
            
            embed.add_field(
                name=f"📋 Up Next ({len(queue)} songs)",
                value="\n".join(queue_text) or "Empty",
                inline=False
            )
            
            if total_duration > 0:
                total_str = f"{total_duration//3600}:{(total_duration%3600)//60:02d}:{total_duration%60:02d}"
                embed.add_field(name="⏱️ Total Time", value=total_str, inline=True)
        else:
            embed.add_field(name="📋 Up Next", value="Empty", inline=False)
        
        # Playback settings
        volume = _MUSIC_STATE.get(guild_id, {}).get('volume', 0.5)
        loop_mode = _MUSIC_STATE.get(guild_id, {}).get('loop', False)
        embed.add_field(name="🔊 Volume", value=f"{int(volume * 100)}%", inline=True)
        embed.add_field(name="🔄 Loop", value="On" if loop_mode else "Off", inline=True)
        
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="volume", description="Set playback volume (0-100)")
    @app_commands.describe(volume="Volume level (0-100)")
    async def set_volume(self, ctx, volume: int):
        """Set the music playback volume."""
        if not 0 <= volume <= 100:
            return await ctx.send("❌ Volume must be between 0 and 100.")
        
        if ctx.voice_client is None:
            return await ctx.send("❌ Not connected to voice.")
        
        guild_id = ctx.guild.id if ctx.guild else 0
        volume_float = volume / 100.0
        
        # Update volume state
        if guild_id not in _MUSIC_STATE:
            _MUSIC_STATE[guild_id] = {}
        _MUSIC_STATE[guild_id]['volume'] = volume_float
        
        # Apply to current source if playing
        if ctx.voice_client.source and hasattr(ctx.voice_client.source, 'volume'):
            ctx.voice_client.source.volume = volume_float
        
        await ctx.send(f"🔊 Volume set to {volume}%")

    @commands.hybrid_command(name="loop", description="Toggle loop mode for the current song")
    async def toggle_loop(self, ctx):
        """Toggle loop mode on/off."""
        guild_id = ctx.guild.id if ctx.guild else 0
        
        if guild_id not in _MUSIC_STATE:
            _MUSIC_STATE[guild_id] = {}
        
        current_loop = _MUSIC_STATE[guild_id].get('loop', False)
        _MUSIC_STATE[guild_id]['loop'] = not current_loop
        
        status = "enabled" if not current_loop else "disabled"
        emoji = "🔄" if not current_loop else "➡️"
        await ctx.send(f"{emoji} Loop mode {status}")

    @commands.hybrid_command(name="nowplaying", description="Show current song info")
    async def now_playing(self, ctx):
        """Display information about the currently playing song."""
        guild_id = ctx.guild.id if ctx.guild else 0
        current_song = _MUSIC_STATE.get(guild_id, {}).get('current_song')
        
        if not current_song:
            return await ctx.send("❌ Nothing is currently playing.")
        
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"**{current_song['title']}**",
            color=0x1db954
        )
        
        if current_song.get('duration'):
            duration = current_song['duration']
            embed.add_field(
                name="⏱️ Duration",
                value=f"{duration//60}:{duration%60:02d}",
                inline=True
            )
        
        if current_song.get('requester'):
            embed.add_field(
                name="👤 Requested by",
                value=current_song['requester'],
                inline=True
            )
        
        if current_song.get('webpage_url'):
            embed.add_field(
                name="🔗 URL",
                value=f"[Link]({current_song['webpage_url']})",
                inline=True
            )
        
        # Playback status
        if ctx.voice_client:
            if ctx.voice_client.is_playing():
                status = "▶️ Playing"
            elif ctx.voice_client.is_paused():
                status = "⏸️ Paused"
            else:
                status = "⏹️ Stopped"
            embed.add_field(name="Status", value=status, inline=True)
        
        volume = _MUSIC_STATE.get(guild_id, {}).get('volume', 0.5)
        embed.add_field(name="🔊 Volume", value=f"{int(volume * 100)}%", inline=True)
        
        loop_mode = _MUSIC_STATE.get(guild_id, {}).get('loop', False)
        embed.add_field(name="🔄 Loop", value="On" if loop_mode else "Off", inline=True)
        
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="musicsearch", description="Search YouTube and show results")
    @app_commands.describe(query="Search query")
    async def search_youtube(self, ctx, *, query: str):
        """Search YouTube and display results without playing."""
        if not query.strip():
            return await ctx.send("❌ Please provide a search query.")
        
        if yt_dlp is None:
            return await ctx.send("❌ yt_dlp dependency missing. Please `pip install yt-dlp`.")
        
        processing_msg = await ctx.send("🔎 Searching YouTube...")
        
        try:
            # Search for multiple results
            loop = asyncio.get_running_loop()
            
            def _search():
                # Basic yt_dlp options without authentication
                ytdl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'default_search': 'ytsearch5:',  # Get 5 results
                    'extractflat': True,  # Don't extract full info, just metadata
                }
                
                with yt_dlp.YoutubeDL(ytdl_opts) as ydl:  # type: ignore[attr-defined]
                    return ydl.extract_info(query, download=False)
            
            data = await loop.run_in_executor(None, _search)
            
            if not data or 'entries' not in data or not data['entries']:
                await processing_msg.edit(content="❌ No results found.")
                return
            
            embed = discord.Embed(
                title=f"🔍 YouTube Search Results for: {query}",
                color=0xff0000
            )
            
            for i, entry in enumerate(data['entries'][:5], 1):
                if not entry:
                    continue
                    
                title = entry.get('title', 'Unknown')
                duration = entry.get('duration')
                uploader = entry.get('uploader', 'Unknown')
                url = entry.get('webpage_url', '')
                
                duration_str = f" • {duration//60}:{duration%60:02d}" if duration else ""
                
                embed.add_field(
                    name=f"{i}. {title}",
                    value=f"By: {uploader}{duration_str}\n[Watch]({url})",
                    inline=False
                )
            
            embed.set_footer(text="Use ^play <title or URL> to play a song")
            
            await processing_msg.edit(content="", embed=embed)
            
        except Exception as e:
            voice_logger.error("YouTube search error", exc_info=e)
            await processing_msg.edit(content=f"❌ Search failed: {str(e)}")

    @commands.hybrid_command(name="leave", description="Leave voice channel")
    async def leave_voice(self, ctx):
        """Disconnect from the current voice channel using the voice handler."""
        guild_id = ctx.guild.id if ctx.guild else 0
        
        # Clear music state when leaving
        if guild_id in _MUSIC_QUEUES:
            _MUSIC_QUEUES[guild_id].clear()
        if guild_id in _MUSIC_STATE:
            _MUSIC_STATE[guild_id]['current_song'] = None
        
        await self.voice_handler.leave_voice_channel(ctx)

    @commands.hybrid_command(name="voice", description="Check voice connection status")
    async def voice_status(self, ctx):
        """Display detailed voice connection status for debugging."""
        embed = discord.Embed(title="🎤 Voice Connection Status", color=0x00ff00 if ctx.voice_client else 0xff0000)
        
        # Bot's voice status with improved connection checking
        if ctx.voice_client:
            # More robust connection checking - don't rely solely on is_connected()
            is_connected = (
                ctx.voice_client.is_connected() or 
                (hasattr(ctx.voice_client, 'ws') and ctx.voice_client.ws and not ctx.voice_client.ws.closed) or
                (hasattr(ctx.voice_client, 'channel') and ctx.voice_client.channel is not None)
            )
            
            connection_status = "Active" if is_connected else "Connecting/Unstable"
            connection_color = "✅" if is_connected else "🔄"
            
            embed.add_field(
                name="🤖 Bot Status", 
                value=f"✅ Connected to: {ctx.voice_client.channel.mention}\n"
                      f"🔊 Playing: {'Yes' if ctx.voice_client.is_playing() else 'No'}\n"
                      f"⏸️ Paused: {'Yes' if ctx.voice_client.is_paused() else 'No'}\n"
                      f"{connection_color} Connection: {connection_status}\n"
                      f"📊 Latency: {ctx.voice_client.latency:.2f}ms",
                inline=False
            )
            
            # Add connection stability info
            if hasattr(ctx.voice_client, 'ws') and ctx.voice_client.ws:
                ws_status = "Open" if not ctx.voice_client.ws.closed else "Closed"
                embed.add_field(
                    name="🔗 WebSocket Status",
                    value=f"Status: {ws_status}",
                    inline=True
                )
        else:
            embed.add_field(
                name="🤖 Bot Status", 
                value="❌ Not connected to any voice channel",
                inline=False
            )
        
        # User's voice status
        if ctx.author.voice:
            user_channel = ctx.author.voice.channel
            embed.add_field(
                name="👤 Your Status",
                value=f"✅ Connected to: {user_channel.mention}\n"
                      f"🔇 Muted: {'Yes' if ctx.author.voice.mute or ctx.author.voice.self_mute else 'No'}\n"
                      f"🔕 Deafened: {'Yes' if ctx.author.voice.deaf or ctx.author.voice.self_deaf else 'No'}",
                inline=False
            )
            
            # Check permissions with error handling
            try:
                if ctx.guild and ctx.guild.me:
                    permissions = user_channel.permissions_for(ctx.guild.me)
                    perms_text = []
                    perms_text.append(f"Connect: {'✅' if permissions.connect else '❌'}")
                    perms_text.append(f"Speak: {'✅' if permissions.speak else '❌'}")
                    perms_text.append(f"Use Voice Activity: {'✅' if permissions.use_voice_activation else '❌'}")
                    
                    embed.add_field(
                        name="🔐 Bot Permissions",
                        value="\n".join(perms_text),
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="🔐 Bot Permissions",
                        value="❌ Could not check permissions (guild issue)",
                        inline=False
                    )
            except Exception as e:
                embed.add_field(
                    name="🔐 Bot Permissions",
                    value=f"❌ Permission check failed: {str(e)}",
                    inline=False
                )
        else:
            embed.add_field(
                name="👤 Your Status",
                value="❌ You are not in a voice channel",
                inline=False
            )
        
        # Add troubleshooting tips if there are issues
        if not ctx.voice_client or (ctx.author.voice and ctx.voice_client.channel != ctx.author.voice.channel):
            embed.add_field(
                name="💡 Troubleshooting",
                value="• Use `^join` to connect to your voice channel\n"
                      "• Make sure I have Connect and Speak permissions\n"
                      "• Try `^voicecleanup` if connection seems stuck\n"
                      "• Check if other voice bots work in this server",
                inline=False
            )
        
        await ctx.send(embed=embed)


def setup_voice_commands(bot, voice_handler):
    """Setup function to add voice commands to the bot"""
    voice_commands = VoiceCommands(bot, voice_handler)
    return voice_commands 