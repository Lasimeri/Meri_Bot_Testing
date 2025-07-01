"""
User Profile Picture Commands Module for Meri Bot

This module contains functionality to retrieve and display user profile pictures.
Separated from main bot file for better code organization and maintainability.

Commands included:
- userpfp: Get the global profile picture of a user
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Union
import aiohttp
import io

# Set up logger for userpfp operations
userpfp_logger = logging.getLogger("MeriUserPfp")


class UserPfpCommands(commands.Cog):
    """User Profile Picture Commands Cog"""
    
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="userpfp", description="Get a user's global profile picture")
    @app_commands.describe(user="The user whose global avatar you want to see (defaults to yourself)")
    async def user_picture(self, ctx, user: Optional[Union[discord.Member, discord.User]] = None):
        """Get and display a user's global profile picture.
        
        Shows the user's main Discord avatar (appears everywhere on Discord).
        """
        # Default to the command user if no user specified
        target_user = user or ctx.author
        
        try:
            # If we have a user ID string instead of a user object, try to fetch it
            if isinstance(target_user, str):
                try:
                    user_id = int(target_user)
                    target_user = await self.bot.fetch_user(user_id)
                except (ValueError, discord.NotFound):
                    return await ctx.send("❌ Invalid user ID or user not found.")
                except discord.HTTPException:
                    return await ctx.send("❌ Failed to fetch user information.")
            
            # Get the global avatar
            global_avatar = target_user.avatar or target_user.default_avatar
            
            # Determine if this is a custom avatar or default
            is_custom = target_user.avatar is not None
            
            # Create embed first
            embed = discord.Embed(
                title=f"🖼️ {target_user.display_name}'s Profile Picture",
                color=0x5865f2 if is_custom else 0x99aab5  # Discord blurple for custom, grey for default
            )
            
            # Try to download and upload the avatar
            try:
                # Use the same URL approach as before
                avatar_url = global_avatar.with_size(1024).url
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(avatar_url) as resp:
                        if resp.status == 200:
                            avatar_data = await resp.read()
                            avatar_file = discord.File(
                                io.BytesIO(avatar_data), 
                                filename=f"{target_user.name}_avatar.{'gif' if global_avatar.is_animated() else 'png'}"
                            )
                            
                            # Set the image to reference the uploaded file
                            embed.set_image(url=f"attachment://{avatar_file.filename}")
                            
                            # Add simple download links
                            if is_custom:
                                embed.add_field(
                                    name="📥 Download",
                                    value=f"[HD]({global_avatar.with_size(1024).url}) • [Full Size]({global_avatar.with_size(4096).url})",
                                    inline=False
                                )
                            else:
                                embed.add_field(
                                    name="📥 Download",
                                    value=f"[Download]({global_avatar.url})",
                                    inline=False
                                )
                            
                            # Set footer
                            embed.set_footer(
                                text=f"Requested by {ctx.author.display_name}",
                                icon_url=ctx.author.display_avatar.url
                            )
                            
                            # Try to send with file attachment
                            await ctx.send(embed=embed, file=avatar_file)
                            return
                        
            except (discord.HTTPException, discord.Forbidden) as e:
                # If file upload fails, fall back to direct embed
                userpfp_logger.warning(f"File upload failed, falling back to direct embed: {e}")
            
            # Fallback: Use direct embed without file upload
            embed.set_image(url=global_avatar.with_size(1024).url)
            
            # Add simple download links
            if is_custom:
                embed.add_field(
                    name="📥 Download",
                    value=f"[HD]({global_avatar.with_size(1024).url}) • [Full Size]({global_avatar.with_size(4096).url})",
                    inline=False
                )
            else:
                embed.add_field(
                    name="📥 Download",
                    value=f"[Download]({global_avatar.url})",
                    inline=False
                )
            
            # Set footer
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name}",
                icon_url=ctx.author.display_avatar.url
            )
            
            # Send embed without file
            await ctx.send(embed=embed)
            
        except discord.NotFound:
            await ctx.send("❌ User not found. Make sure they exist and I can see their profile!")
        except discord.HTTPException as e:
            userpfp_logger.error(f"Failed to fetch avatar for {target_user}: {e}")
            await ctx.send("❌ Couldn't get their avatar right now. Try again in a moment!")
        except Exception as e:
            userpfp_logger.error(f"Unexpected error in userpfp command: {e}")
            await ctx.send("❌ Something went wrong while getting that avatar!")

    @commands.hybrid_command(name="pfp", description="Alias for userpfp - get a user's global profile picture")
    @app_commands.describe(user="The user whose global avatar you want to see (defaults to yourself)")
    async def pfp_alias(self, ctx, user: Optional[Union[discord.Member, discord.User]] = None):
        """Alias for the userpfp command - get a user's global profile picture."""
        await self.user_picture(ctx, user)

    @commands.hybrid_command(name="avatar", description="Another alias for userpfp - get a user's global profile picture")
    @app_commands.describe(user="The user whose global avatar you want to see (defaults to yourself)")
    async def avatar_alias(self, ctx, user: Optional[Union[discord.Member, discord.User]] = None):
        """Another alias for the userpfp command - get a user's global profile picture."""
        await self.user_picture(ctx, user)


def setup_user_commands(bot):
    """Setup function to add user commands to the bot"""
    user_commands = UserPfpCommands(bot)
    return user_commands 