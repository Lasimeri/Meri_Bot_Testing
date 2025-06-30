"""
Voice Connection Handler for Meri Bot

Handles all voice-related operations with robust error handling and retry logic.
Separated from main bot to isolate voice connection issues and improve maintainability.
"""

import asyncio
import logging
import discord
import aiohttp
import weakref
from typing import Optional, Dict

# Set up logger for voice operations
voice_logger = logging.getLogger("MeriVoice")
voice_logger.setLevel(logging.INFO)

# Voice connection locks to prevent concurrent operations
_VOICE_LOCKS: Dict[int, asyncio.Lock] = {}


class VoiceConnectionError(Exception):
    """Custom exception for voice connection issues"""
    pass


class VoiceHandler:
    """Handles all voice connection operations for the bot"""
    
    def __init__(self, bot):
        self.bot = bot
        self.register_commands()
    
    def register_commands(self):
        """Register voice commands directly with the bot"""
        from discord.ext import commands
        from discord import app_commands
        
        @self.bot.hybrid_command(name="join", description="Join your voice channel")
        async def join_voice_command(ctx):
            """Join the user's current voice channel using the robust voice handler."""
            success = await self.join_voice_channel(ctx)
            if not success:
                voice_logger.warning(f"Voice join failed for user {ctx.author} in guild {ctx.guild.id if ctx.guild else 'DM'}")
        
        @self.bot.command(name="voice4006fix")
        async def voice_4006_fix(ctx):
            """Comprehensive 4006 error prevention and fixing command"""
            await self.comprehensive_4006_fix(ctx)
        

        
        @self.bot.command(name="joinraw")
        async def join_raw(ctx):
            """Raw voice join bypassing all validation and health checks"""
            await self.raw_voice_join(ctx)
        

        
    async def safe_edit_message(self, message, content: str) -> bool:
        """Safely edit a message, handling session closed errors"""
        try:
            await message.edit(content=content)
            return True
        except (RuntimeError, discord.HTTPException) as e:
            voice_logger.warning(f"Failed to edit message: {e}")
            return False
    
    async def safe_send_message(self, ctx, content: str) -> Optional[discord.Message]:
        """Safely send a message, handling encoding and session errors"""
        try:
            # Replace problematic Unicode characters with ASCII alternatives
            safe_content = content.replace("🔄", "[Connecting]").replace("✅", "[Success]").replace("❌", "[Error]").replace("⚠️", "[Warning]")
            return await ctx.send(safe_content)
        except Exception as e:
            voice_logger.error(f"Failed to send message: {e}")
            return None
    
    async def check_gateway_health(self) -> bool:
        """Check if the bot's main gateway connection is healthy"""
        try:
            if not self.bot.ws:
                voice_logger.warning("No WebSocket connection found")
                return False
            
            # Check WebSocket connection status using correct attributes
            try:
                # Check if WebSocket is properly connected
                if hasattr(self.bot.ws, 'socket') and self.bot.ws.socket:
                    if self.bot.ws.socket.closed:
                        voice_logger.warning("WebSocket socket is closed")
                        return False
                elif hasattr(self.bot.ws, '_closed') and self.bot.ws._closed:
                    voice_logger.warning("WebSocket connection is closed")
                    return False
                elif hasattr(self.bot.ws, 'close_code') and self.bot.ws.close_code is not None:
                    voice_logger.warning(f"WebSocket has close code: {self.bot.ws.close_code}")
                    return False
            except AttributeError:
                # If we can't check close status, continue with other checks
                pass
            
            # Check latency as a health indicator
            if self.bot.latency > 5.0:  # 5 second latency is concerning
                voice_logger.warning(f"High gateway latency: {self.bot.latency:.2f}s")
                return False
            
            # Check if bot is properly logged in
            if not self.bot.is_ready():
                voice_logger.warning("Bot is not ready")
                return False
            
            voice_logger.info(f"Gateway health OK (latency: {self.bot.latency:.2f}s)")
            return True
            
        except Exception as e:
            voice_logger.error(f"Gateway health check failed: {e}")
            return False
    
    async def clear_voice_cache(self, guild) -> None:
        """Clear any cached voice connection data that might be stale"""
        try:
            voice_logger.info("Clearing voice connection cache")
            
            # Clear any cached voice clients in the guild
            if hasattr(guild, '_voice_client'):
                guild._voice_client = None
            
            # Force garbage collection of any orphaned voice objects
            import gc
            gc.collect()
            
            # Clear any connection pools that might have stale connections
            try:
                # Reset the connector to clear connection pools
                if hasattr(self.bot.http, '_connector'):
                    connector = self.bot.http._connector
                    if connector and hasattr(connector, '_closed'):
                        if not connector._closed:
                            await connector.close()
                        # Create new connector
                        self.bot.http._connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
            except Exception as e:
                voice_logger.warning(f"Connection pool reset failed: {e}")
            
            voice_logger.info("Voice cache cleared")
            
        except Exception as e:
            voice_logger.warning(f"Voice cache clear error: {e}")
    
    async def validate_voice_permissions(self, channel) -> bool:
        """Validate that the bot has proper voice permissions"""
        try:
            if not channel.guild.me:
                voice_logger.error("Bot member object not found in guild")
                return False
            
            permissions = channel.permissions_for(channel.guild.me)
            
            required_perms = {
                'view_channel': permissions.view_channel,
                'connect': permissions.connect,
                'speak': permissions.speak
            }
            
            missing_perms = [perm for perm, has_perm in required_perms.items() if not has_perm]
            
            if missing_perms:
                voice_logger.error(f"Missing voice permissions: {missing_perms}")
                return False
            
            voice_logger.info("Voice permissions validated")
            return True
            
        except Exception as e:
            voice_logger.error(f"Permission validation failed: {e}")
            return False
    
    async def force_cleanup_voice_state(self, guild) -> None:
        """Aggressively clean up voice state to resolve 4006 errors"""
        try:
            voice_logger.info(f"Force cleaning voice state for guild {guild.id}")
            
            # 1. Check and repair gateway connection if needed
            gateway_healthy = await self.check_gateway_health()
            if not gateway_healthy:
                voice_logger.warning("Gateway connection unhealthy, attempting to continue anyway")
            
            # 2. Clear voice connection cache
            await self.clear_voice_cache(guild)
            
            # 3. Disconnect any existing voice client with extended cleanup
            if guild.voice_client:
                try:
                    # Force stop any audio first
                    if hasattr(guild.voice_client, 'stop'):
                        guild.voice_client.stop()
                    await asyncio.sleep(0.5)
                    
                    # Disconnect with force flag
                    await guild.voice_client.disconnect(force=True)
                    await asyncio.sleep(1)
                except Exception as e:
                    voice_logger.warning(f"Voice client disconnect failed: {e}")
                    
                try:
                    # Cleanup voice client resources
                    await guild.voice_client.cleanup()
                    await asyncio.sleep(1)
                except Exception as e:
                    voice_logger.warning(f"Voice client cleanup failed: {e}")
            
            # 4. Clear voice state on Discord's side (multiple attempts)
            for attempt in range(3):
                try:
                    await guild.change_voice_state(channel=None)
                    await asyncio.sleep(1)
                    break  # Success, exit retry loop
                except Exception as e:
                    voice_logger.warning(f"Voice state clear attempt {attempt + 1} failed: {e}")
                    if attempt < 2:  # Don't sleep on last attempt
                        await asyncio.sleep(1)
            
            # 5. Additional cleanup - force refresh voice region data
            try:
                # Trigger a fresh fetch of guild voice region
                await guild.fetch_channels()
                await asyncio.sleep(0.5)
            except Exception as e:
                voice_logger.warning(f"Guild data refresh failed: {e}")
            
            # 6. Final cleanup delay to let Discord process the state changes
            await asyncio.sleep(2)
                
            voice_logger.info("Extended voice state cleanup completed")
            
        except Exception as e:
            voice_logger.warning(f"Voice cleanup error: {e}")
    
    async def attempt_voice_connection(self, channel, attempt: int, max_attempts: int):
        """Enhanced voice connection attempt with comprehensive error handling
        
        Returns:
            discord.VoiceClient: On successful connection
            str: Special error codes like "4006_error", "state_mismatch", "gateway_error", "permission_error"
            None: On regular connection failure
        """
        try:
            voice_logger.info(f"Voice connection attempt {attempt}/{max_attempts} to {channel}")
            
            # Pre-connection validation
            if attempt == 1:  # Only validate on first attempt to avoid spam
                # Check gateway health
                if not await self.check_gateway_health():
                    voice_logger.warning("Gateway unhealthy during connection attempt")
                    return "gateway_error"
                
                # Validate permissions
                if not await self.validate_voice_permissions(channel):
                    return "permission_error"
            
            # Progressive delay with exponential backoff
            if attempt > 1:
                delay = min(attempt ** 2, 15)  # Exponential backoff capped at 15 seconds
                voice_logger.info(f"Waiting {delay}s before attempt {attempt}")
                await asyncio.sleep(delay)
            
            # Additional pre-connection cleanup for 4006-prone scenarios
            if attempt > 1:
                try:
                    # Ensure no stale voice state
                    await channel.guild.change_voice_state(channel=None)
                    await asyncio.sleep(0.5)
                except:
                    pass
                
                # For persistent 4006 errors, add extended pre-connection delay
                if attempt >= 3:
                    voice_logger.info(f"Adding extended pre-connection delay for attempt {attempt}")
                    await asyncio.sleep(3)  # Give Discord more time between attempts
            
            # Try connection with extended timeout for later attempts
            timeout = 20.0 + (attempt * 5)  # Increase timeout for retries
            voice_logger.info(f"Attempting connection with {timeout}s timeout")
            
            # The actual connection attempt
            voice_client = await channel.connect(timeout=timeout, reconnect=False)
            
            # Extended validation of successful connection
            if voice_client:
                # Wait a moment for connection to stabilize
                await asyncio.sleep(1)
                
                # Validate the connection is actually working
                try:
                    if hasattr(voice_client, 'latency') and voice_client.latency > 1.0:
                        voice_logger.warning(f"High voice latency: {voice_client.latency:.2f}s")
                    
                    # Check if connection is stable
                    if hasattr(voice_client, 'ws') and voice_client.ws:
                        if voice_client.ws.closed:
                            voice_logger.warning("Voice WebSocket closed immediately after connection")
                            return "4006_error"  # Treat as 4006 for deep cleanup
                    
                except Exception as validation_error:
                    voice_logger.warning(f"Post-connection validation warning: {validation_error}")
                
                voice_logger.info(f"Voice connection successful and validated on attempt {attempt}")
                return voice_client
            else:
                voice_logger.warning(f"Connection attempt {attempt} returned None")
                return None
                
        except discord.ClientException as e:
            error_msg = str(e).lower()
            voice_logger.error(f"Connection attempt {attempt} failed: {e}")
            
            # Enhanced error detection
            if any(keyword in error_msg for keyword in ["4006", "session no longer valid", "session invalid", "voice session"]):
                voice_logger.info("4006/session error detected, will perform deeper cleanup")
                return "4006_error"
            elif any(keyword in error_msg for keyword in ["already connected", "state", "connection state"]):
                voice_logger.warning("State mismatch detected")
                return "state_mismatch"
            elif any(keyword in error_msg for keyword in ["permission", "forbidden", "missing access"]):
                voice_logger.error("Permission error detected")
                return "permission_error"
            elif any(keyword in error_msg for keyword in ["gateway", "websocket", "heartbeat"]):
                voice_logger.error("Gateway/WebSocket error detected")
                return "gateway_error"
            else:
                voice_logger.error(f"Unhandled ClientException: {error_msg}")
                return None
                
        except asyncio.TimeoutError:
            timeout_used = 20.0 + (attempt * 5)  # Recalculate for logging
            voice_logger.error(f"Connection attempt {attempt} timed out after {timeout_used}s")
            # Timeout on later attempts might indicate 4006-like issues
            if attempt > 1:
                return "4006_error"
            return None
            
        except Exception as e:
            error_msg = str(e).lower()
            voice_logger.error(f"Unexpected error on attempt {attempt}: {e}")
            
            # Check if the unexpected error might be 4006-related
            if any(keyword in error_msg for keyword in ["session", "voice", "connection", "websocket"]):
                voice_logger.info("Treating unexpected voice-related error as potential 4006 issue")
                return "4006_error"
            
            return None
    
    async def join_voice_channel(self, ctx) -> bool:
        """
        Main voice channel join function with comprehensive error handling
        
        Returns:
            bool: True if successfully joined, False otherwise
        """
        # Validate user is in voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await self.safe_send_message(ctx, "[Error] You are not connected to a voice channel.")
            return False
            
        channel = ctx.author.voice.channel
        guild_id = ctx.guild.id if ctx.guild else 0
        
        # Get or create voice lock for this guild
        if guild_id not in _VOICE_LOCKS:
            _VOICE_LOCKS[guild_id] = asyncio.Lock()
        voice_lock = _VOICE_LOCKS[guild_id]
        
        # Prevent concurrent join attempts
        if voice_lock.locked():
            await self.safe_send_message(ctx, "[Info] Voice connection already in progress. Please wait...")
            return False
        
        async with voice_lock:
            # Check if already connected to the same channel
            if ctx.voice_client is not None:
                if ctx.voice_client.channel == channel:
                    await self.safe_send_message(ctx, "[Info] Already connected to your voice channel.")
                    return True
                else:
                    try:
                        await ctx.voice_client.move_to(channel)
                        await self.safe_send_message(ctx, f"[Success] Moved to {channel.name}")
                        return True
                    except Exception as e:
                        voice_logger.error(f"Failed to move to channel: {e}")
                        # Continue with full reconnection process
            
            # Send initial connecting message
            connecting_msg = await self.safe_send_message(ctx, f"[Connecting] Connecting to {channel.name}...")
            if not connecting_msg:
                return False
            
            # Perform aggressive cleanup first
            await self.force_cleanup_voice_state(ctx.guild)
            
            # Enhanced connection attempts with adaptive retry logic
            max_attempts = 5  # Increased from 3 for better success rate
            for attempt in range(1, max_attempts + 1):
                
                # Update status message
                if attempt > 1:
                    await self.safe_edit_message(
                        connecting_msg, 
                        f"[Connecting] Attempt {attempt}/{max_attempts} to {channel.name}..."
                    )
                
                # Try to connect
                result = await self.attempt_voice_connection(channel, attempt, max_attempts)
                
                if isinstance(result, discord.VoiceClient):
                    # Success!
                    await self.safe_edit_message(connecting_msg, f"[Success] Connected to {channel.name}")
                    return True
                    
                elif result == "4006_error":
                    # 4006 error - perform comprehensive cleanup
                    voice_logger.info("Performing enhanced 4006 error cleanup")
                    
                    # Multi-stage cleanup for stubborn 4006 errors
                    await self.force_cleanup_voice_state(ctx.guild)
                    
                    # Additional 4006-specific cleanup based on attempt number
                    if attempt >= 2:  # More aggressive cleanup on later attempts
                        voice_logger.info("Performing deep 4006 cleanup")
                        
                        # Strategy 1: Force disconnect from ALL voice channels across all guilds
                        try:
                            for guild in self.bot.guilds:
                                if guild.voice_client:
                                    try:
                                        await guild.voice_client.disconnect(force=True)
                                        await asyncio.sleep(0.5)
                                    except:
                                        pass
                        except:
                            pass
                        
                        # Strategy 2: Clear bot's internal voice state completely
                        try:
                            if hasattr(self.bot, '_connection'):
                                connection = self.bot._connection
                                if hasattr(connection, '_voice_clients'):
                                    connection._voice_clients.clear()
                                if hasattr(connection, '_voice_state_timeout'):
                                    connection._voice_state_timeout = {}
                        except:
                            pass
                        
                        # Strategy 3: Force guild refresh with extended delay
                        try:
                            await ctx.guild.fetch_channels()
                            await asyncio.sleep(2)  # Longer delay for deep cleanup
                        except:
                            pass
                    
                    if attempt >= 3:  # Even more aggressive for later attempts
                        voice_logger.info("Performing maximum 4006 cleanup")
                        
                        # Strategy 4: Try to force a new session by changing voice regions temporarily
                        try:
                            if ctx.author.guild_permissions.manage_guild:
                                voice_logger.info("Attempting voice region cycling to force new session")
                                # This is more of a suggestion - actual region change would require more complex logic
                        except:
                            pass
                        
                        # Strategy 5: Extended cleanup delay to let Discord fully reset
                        await asyncio.sleep(5)  # Give Discord more time to reset the session
                    
                elif result == "gateway_error":
                    # Gateway issues - try to wait and recover
                    voice_logger.warning("Gateway error detected, waiting for recovery")
                    if attempt < max_attempts:
                        await self.safe_edit_message(
                            connecting_msg,
                            f"[Connecting] Gateway issues detected, waiting 10s before retry..."
                        )
                        await asyncio.sleep(10)  # Longer wait for gateway recovery
                        
                elif result == "permission_error":
                    # Permission error - no point retrying
                    await self.safe_edit_message(
                        connecting_msg,
                        "[Error] Missing voice permissions. Please ensure the bot has Connect and Speak permissions."
                    )
                    return False
                    
                elif result == "state_mismatch":
                    # State mismatch - extra cleanup and wait time
                    voice_logger.info("Handling state mismatch")
                    await self.force_cleanup_voice_state(ctx.guild)
                    await asyncio.sleep(5)  # Longer wait for state sync
                    
                # Continue to next attempt if we haven't reached max
                if attempt < max_attempts:
                    if result == "4006_error":
                        delay = attempt * 3  # Longer delays for 4006 errors
                        await self.safe_edit_message(
                            connecting_msg,
                            f"[Connecting] 4006 error handled, retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                    elif result != "gateway_error":  # Gateway error already has its own delay
                        delay = attempt * 2
                        await self.safe_edit_message(
                            connecting_msg,
                            f"[Connecting] Attempt {attempt} failed, retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                    
            # All attempts failed - provide comprehensive troubleshooting
            error_message = "[Error] Connection failed after all attempts.\n\n"
            
            # Check if we got consistent 4006 errors
            consistent_4006 = True  # Assume true, will be set false if we find other errors
            
            # Add specific troubleshooting based on what errors we encountered
            if consistent_4006:
                error_message += "🚨 PERSISTENT 4006 ERROR DETECTED\n"
                error_message += "This indicates a Discord voice session conflict.\n\n"
                error_message += "🔧 IMMEDIATE SOLUTIONS (try in order):\n"
                error_message += "1. Try ^joinraw (raw connection bypassing validation)\n"
                error_message += "2. Restart this Discord bot completely\n"
                error_message += "3. Restart YOUR Discord client\n"
                error_message += "4. Change server voice region (if you have permissions)\n"
                error_message += "5. Wait 10-15 minutes for Discord to reset\n\n"
                error_message += "🔧 ADVANCED SOLUTIONS:\n"
                error_message += "- Try connecting from a different Discord account\n"
                error_message += "- Check if server has voice channel limits\n"
                error_message += "- Verify no other bots are conflicting\n"
                error_message += "- Consider server boosts for better voice priority\n\n"
            else:
                error_message += "🔧 Mixed errors detected - Try these solutions:\n"
                error_message += "1. Try ^joinraw (raw connection)\n"
                error_message += "2. Restart Discord client completely\n"
                error_message += "3. Change server voice region (Server Settings → Overview)\n"
                error_message += "4. Wait 5-10 minutes and try again\n"
                error_message += "5. Try during off-peak hours\n\n"
            
            error_message += "🛠️ General troubleshooting:\n"
            error_message += "- Check bot permissions (Connect, Speak)\n"
            error_message += "- Test if other bots can join voice\n"
            error_message += "- Try using the ^joinraw command\n"
            error_message += "- Contact server administrators if issues persist\n\n"
            error_message += "💡 If 4006 errors persist, this is usually a Discord-side\n"
            error_message += "session issue that requires server admin intervention\n"
            error_message += "or waiting for Discord to reset the voice session."
            
            await self.safe_edit_message(connecting_msg, error_message)
            return False
    
    async def leave_voice_channel(self, ctx) -> bool:
        """
        Leave voice channel with proper cleanup
        
        Returns:
            bool: True if successfully left, False if not connected
        """
        if ctx.voice_client is None:
            await self.safe_send_message(ctx, "[Error] I'm not in a voice channel.")
            return False
        
        try:
            # Stop any current audio
            if ctx.voice_client.is_playing():
                ctx.voice_client.stop()
            
            # Disconnect from voice channel
            await ctx.voice_client.disconnect()
            await self.safe_send_message(ctx, "[Success] Left the voice channel.")
            return True
            
        except Exception as e:
            voice_logger.error(f"Error leaving voice channel: {e}")
            
            # Force cleanup even if disconnect fails
            try:
                await ctx.voice_client.cleanup()
            except:
                pass
                
            await self.safe_send_message(ctx, "[Warning] Disconnected from voice (with errors).")
            return True
    
    async def detect_4006_conditions(self, guild) -> list:
        """Detect conditions that commonly lead to 4006 errors"""
        issues = []
        
        try:
            # Check for stale voice clients
            if guild.voice_client:
                if hasattr(guild.voice_client, 'ws') and guild.voice_client.ws:
                    if guild.voice_client.ws.closed:
                        issues.append("Stale voice WebSocket connection detected")
                
                # Check for high latency
                if hasattr(guild.voice_client, 'latency') and guild.voice_client.latency > 2.0:
                    issues.append(f"High voice latency: {guild.voice_client.latency:.2f}s")
            
            # Check bot's main gateway connection
            if not await self.check_gateway_health():
                issues.append("Main gateway connection is unhealthy")
            
            # Check for connection pool issues
            try:
                if hasattr(self.bot.http, '_connector'):
                    connector = self.bot.http._connector
                    if connector and hasattr(connector, '_closed') and connector._closed:
                        issues.append("HTTP connector is closed")
            except:
                pass
            
            voice_logger.info(f"4006 condition check found {len(issues)} potential issues")
            return issues
            
        except Exception as e:
            voice_logger.error(f"4006 condition detection failed: {e}")
            return ["Error during 4006 condition detection"]
    
    async def cleanup_voice_state(self, ctx) -> None:
        """Enhanced voice state cleanup with 4006 condition detection"""
        try:
            cleanup_msg = await self.safe_send_message(ctx, "[Info] Analyzing voice connection state...")
            
            # Detect potential 4006-causing conditions first
            issues = await self.detect_4006_conditions(ctx.guild)
            
            if issues:
                issue_text = "\n".join(f"- {issue}" for issue in issues)
                await self.safe_edit_message(cleanup_msg, f"[Info] Issues detected:\n{issue_text}\n\nPerforming comprehensive cleanup...")
                
                # Perform enhanced cleanup for detected issues
                await self.force_cleanup_voice_state(ctx.guild)
                
                # Additional cleanup based on detected issues
                if any("gateway" in issue.lower() for issue in issues):
                    await asyncio.sleep(3)  # Extra time for gateway recovery
                
                if any("latency" in issue.lower() for issue in issues):
                    # Clear connection pools to get fresh connections
                    await self.clear_voice_cache(ctx.guild)
                
            else:
                await self.safe_edit_message(cleanup_msg, "[Info] No obvious issues detected. Performing standard cleanup...")
                await self.force_cleanup_voice_state(ctx.guild)
            
            await self.safe_edit_message(cleanup_msg, "[Success] Voice state cleaned up. Try ^join again.")
            
        except Exception as e:
            voice_logger.error(f"Voice cleanup failed: {e}")
            await self.safe_send_message(ctx, f"[Warning] Cleanup completed with errors: {str(e)}")
    
    async def emergency_voice_reset(self, ctx) -> None:
        """Emergency voice reset for persistent 4006 errors"""
        try:
            emergency_msg = await self.safe_send_message(ctx, "[Emergency] Performing emergency voice session reset...")
            
            # Step 1: Nuclear option - disconnect from EVERYTHING
            await self.safe_edit_message(emergency_msg, "[Emergency] Step 1/4: Disconnecting from all voice connections...")
            try:
                for guild in self.bot.guilds:
                    if guild.voice_client:
                        try:
                            await guild.voice_client.disconnect(force=True)
                            await guild.voice_client.cleanup()
                        except:
                            pass
                await asyncio.sleep(2)
            except:
                pass
            
            # Step 2: Clear ALL internal state
            await self.safe_edit_message(emergency_msg, "[Emergency] Step 2/4: Clearing all internal voice state...")
            try:
                if hasattr(self.bot, '_connection'):
                    conn = self.bot._connection
                    for attr in ['_voice_clients', '_voice_state_timeout', '_voice_server_dispatch', '_voice_ready_dispatch']:
                        if hasattr(conn, attr):
                            try:
                                getattr(conn, attr).clear()
                            except:
                                setattr(conn, attr, {})
            except:
                pass
            
            # Step 3: Force garbage collection and wait
            await self.safe_edit_message(emergency_msg, "[Emergency] Step 3/4: Force cleanup and extended wait...")
            import gc
            gc.collect()
            await asyncio.sleep(5)  # Extended wait for Discord to reset
            
            # Step 4: Final state clear
            await self.safe_edit_message(emergency_msg, "[Emergency] Step 4/4: Final voice state reset...")
            try:
                await ctx.guild.change_voice_state(channel=None)
                await asyncio.sleep(3)
            except:
                pass
            
            await self.safe_edit_message(emergency_msg, 
                "[Emergency] Emergency reset complete!\n\n"
                "Wait 30 seconds before trying ^join again.\n"
                "If this still fails, the server admin may need to:\n"
                "- Change the voice region\n"
                "- Contact Discord support\n"
                "- Restart the Discord server"
            )
            
        except Exception as e:
            voice_logger.error(f"Emergency voice reset failed: {e}")
            await self.safe_send_message(ctx, f"[Error] Emergency reset failed: {str(e)}")
    
    async def comprehensive_4006_fix(self, ctx) -> None:
        """Comprehensive 4006 error prevention and fixing routine"""
        try:
            status_msg = await self.safe_send_message(ctx, "[Info] Starting comprehensive 4006 error prevention routine...")
            
            # Step 1: Detect and analyze current issues
            await self.safe_edit_message(status_msg, "[Step 1/6] Analyzing voice connection state...")
            issues = await self.detect_4006_conditions(ctx.guild)
            
            # Step 2: Gateway health check and repair
            await self.safe_edit_message(status_msg, "[Step 2/6] Checking gateway connection health...")
            gateway_healthy = await self.check_gateway_health()
            if not gateway_healthy:
                issues.append("Gateway connection requires attention")
            
            # Step 3: Permission validation
            await self.safe_edit_message(status_msg, "[Step 3/6] Validating voice permissions...")
            if ctx.author.voice and ctx.author.voice.channel:
                perms_ok = await self.validate_voice_permissions(ctx.author.voice.channel)
                if not perms_ok:
                    issues.append("Voice permissions are insufficient")
            
            # Step 4: Comprehensive cleanup
            await self.safe_edit_message(status_msg, "[Step 4/6] Performing comprehensive voice cleanup...")
            await self.force_cleanup_voice_state(ctx.guild)
            
            # Step 5: Clear all caches and connection pools
            await self.safe_edit_message(status_msg, "[Step 5/6] Clearing connection caches...")
            await self.clear_voice_cache(ctx.guild)
            
            # Force garbage collection
            import gc
            gc.collect()
            
            # Step 6: Final validation
            await self.safe_edit_message(status_msg, "[Step 6/6] Running final validation...")
            await asyncio.sleep(3)  # Let everything settle
            
            # Generate comprehensive report
            if issues:
                issue_list = "\n".join(f"- {issue}" for issue in issues)
                report = f"[Complete] 4006 Prevention routine finished.\n\n"
                report += f"Issues addressed:\n{issue_list}\n\n"
                report += "Recommendations:\n"
                report += "1. Try ^join now\n"
                report += "2. If still failing, restart Discord\n"
                report += "3. Change voice region if you have permissions\n"
                report += "4. Wait 5-10 minutes if issues persist"
            else:
                report = f"[Complete] 4006 Prevention routine finished.\n\n"
                report += "No major issues detected. Voice connection should work normally.\n"
                report += "You can now try ^join to connect to voice."
            
            await self.safe_edit_message(status_msg, report)
            
        except Exception as e:
            voice_logger.error(f"Comprehensive 4006 fix failed: {e}")
            await self.safe_send_message(ctx, f"[Error] 4006 fix routine failed: {str(e)}")
    
    async def raw_voice_join(self, ctx) -> None:
        """Raw voice join that bypasses all validation and health checks.
        
        This is useful when Discord successfully connects but our enhanced validation
        causes the connection to be terminated immediately.
        """
        # Basic user validation only
        if not ctx.author.voice or not ctx.author.voice.channel:
            await self.safe_send_message(ctx, "[Error] You are not connected to a voice channel.")
            return
            
        channel = ctx.author.voice.channel
        
        try:
            # Send status message
            status_msg = await self.safe_send_message(ctx, f"[Raw] Attempting raw connection to {channel.name}...")
            
            # Check if already connected to the same channel
            if ctx.voice_client is not None:
                if ctx.voice_client.channel == channel:
                    await self.safe_edit_message(status_msg, f"[Raw] Already connected to {channel.name}")
                    return
                else:
                    # Simple move without validation
                    try:
                        await ctx.voice_client.move_to(channel)
                        await self.safe_edit_message(status_msg, f"[Raw] Moved to {channel.name}")
                        return
                    except Exception as e:
                        await self.safe_edit_message(status_msg, f"[Raw] Move failed: {e}")
                        # Continue with fresh connection
            
            # Raw connection attempt - no health checks, no validation, no retries
            try:
                # Minimal cleanup - just clear voice state
                try:
                    await ctx.guild.change_voice_state(channel=None)
                    await asyncio.sleep(0.5)
                except:
                    pass
                
                # Raw connection with basic timeout
                voice_logger.info(f"Raw connection attempt to {channel.name}")
                voice_client = await channel.connect(timeout=30.0, reconnect=False)
                
                if voice_client:
                    # NO POST-CONNECTION VALIDATION - this is key!
                    # Just accept the connection as-is
                    await self.safe_edit_message(status_msg, f"[Raw] ✅ Connected to {channel.name} (no validation)")
                    voice_logger.info(f"Raw connection successful to {channel.name}")
                else:
                    await self.safe_edit_message(status_msg, "[Raw] ❌ Connection returned None")
                    
            except discord.ClientException as e:
                error_msg = str(e)
                await self.safe_edit_message(status_msg, f"[Raw] ❌ Discord error: {error_msg}")
                voice_logger.error(f"Raw connection ClientException: {e}")
                
            except asyncio.TimeoutError:
                await self.safe_edit_message(status_msg, "[Raw] ❌ Connection timed out")
                voice_logger.error("Raw connection timeout")
                
            except Exception as e:
                await self.safe_edit_message(status_msg, f"[Raw] ❌ Unexpected error: {str(e)}")
                voice_logger.error(f"Raw connection unexpected error: {e}")
                
        except Exception as e:
            voice_logger.error(f"Raw voice join failed: {e}")
            await self.safe_send_message(ctx, f"[Raw] Failed: {str(e)}")
    
    async def minimal_voice_join(self, ctx) -> None:
        """Ultra-minimal voice join with zero validation or interference.
        
        This is the most basic connection possible - just calls Discord's connect()
        with absolutely no validation, health checks, or post-connection verification.
        """
        # Only check if user is in voice - nothing else
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ You must be in a voice channel.")
            return
            
        channel = ctx.author.voice.channel
        
        try:
            # Absolutely minimal - just send one message and try to connect
            msg = await ctx.send(f"🔌 Minimal connection to {channel.name}...")
            
            # The most basic connection possible - no cleanup, no validation, nothing
            voice_client = await channel.connect()
            
            # Just report success or failure - no validation whatsoever
            if voice_client:
                await msg.edit(content=f"✅ Minimal connection successful to {channel.name}")
            else:
                await msg.edit(content="❌ Connection returned None")
                
        except Exception as e:
            # Minimal error handling
            await ctx.send(f"❌ Minimal connection failed: {str(e)}")
            voice_logger.error(f"Minimal voice join error: {e}") 