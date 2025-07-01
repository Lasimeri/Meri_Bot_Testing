"""
Server Profile Picture Commands Module for Meri Bot

This module contains functionality to retrieve and display server-specific profile pictures.
Separated from main bot file for better code organization and maintainability.

Commands included:
- serverpfp: Get the server-specific profile picture of a user
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
import aiohttp
import io

# Set up logger for serverpfp operations
serverpfp_logger = logging.getLogger("MeriServerPfp")


class ServerPfpCommands(commands.Cog):
    """Server Profile Picture Commands Cog"""
    
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="serverpfp", description="Get a user's server-specific profile picture")
    @app_commands.describe(user="The user whose server avatar you want to see (defaults to yourself)")
    async def server_profile_picture(self, ctx, user: Optional[discord.Member] = None):
        """Get and display a user's server-specific profile picture.
        
        Shows their custom avatar for this server (if they have one set).
        """
        # Default to the command user if no user specified
        target_user = user or ctx.author
        
        # Ensure we have a Member object (not just User) to access guild-specific data
        if isinstance(target_user, discord.User) and not isinstance(target_user, discord.Member):
            # Try to get the member object from the guild
            if ctx.guild:
                try:
                    target_user = await ctx.guild.fetch_member(target_user.id)
                except discord.NotFound:
                    return await ctx.send(f"❌ {target_user.mention} isn't in this server!")
                except discord.HTTPException:
                    return await ctx.send("❌ Couldn't get their info right now.")
            else:
                return await ctx.send("❌ This command only works in servers! Use `/userpfp` for global avatars.")
        
        # Check if this is being used in DMs
        if not ctx.guild:
            return await ctx.send("❌ This command only works in servers! Use `/userpfp` for global avatars.")
        
        try:
            # Get the guild-specific avatar
            guild_avatar = target_user.guild_avatar
            
            if guild_avatar is None:
                # No server-specific avatar set, default to profile picture behavior
                global_avatar = target_user.avatar or target_user.default_avatar
                is_custom = target_user.avatar is not None
                
                # Create embed
                embed = discord.Embed(
                    title=f"🖼️ {target_user.display_name}'s Profile Picture",
                    color=0x5865f2 if is_custom else 0x99aab5  # Discord blurple for custom, grey for default
                )
                
                # Try to download and upload
                avatar_file = None
                try:
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
                                
                except (discord.HTTPException, discord.Forbidden) as e:
                    serverpfp_logger.warning(f"File upload failed, using direct embed: {e}")
                    avatar_file = None
                
                # If file upload failed or wasn't attempted, use direct URL
                if avatar_file is None:
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
                
            else:
                # User has a server-specific avatar
                embed = discord.Embed(
                    title=f"🖼️ {target_user.display_name}'s Server Picture",
                    description=f"Custom server avatar for **{ctx.guild.name}**",
                    color=0x00ff00  # Green color for success
                )
                
                # Try to download and upload
                avatar_file = None
                try:
                    avatar_url = guild_avatar.with_size(1024).url
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(avatar_url) as resp:
                            if resp.status == 200:
                                avatar_data = await resp.read()
                                avatar_file = discord.File(
                                    io.BytesIO(avatar_data), 
                                    filename=f"{target_user.name}_server_avatar.{'gif' if guild_avatar.is_animated() else 'png'}"
                                )
                                # Set the image to reference the uploaded file
                                embed.set_image(url=f"attachment://{avatar_file.filename}")
                                
                except (discord.HTTPException, discord.Forbidden) as e:
                    serverpfp_logger.warning(f"File upload failed, using direct embed: {e}")
                    avatar_file = None
                
                # If file upload failed, use direct URL
                if avatar_file is None:
                    embed.set_image(url=guild_avatar.with_size(1024).url)
                
                # Add download links
                embed.add_field(
                    name="📥 Download",
                    value=f"[HD]({guild_avatar.with_size(1024).url}) • [Full Size]({guild_avatar.with_size(4096).url})",
                    inline=False
                )
            
            # Set footer
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name}",
                icon_url=ctx.author.display_avatar.url
            )
            
            # Send embed with or without file
            if avatar_file:
                await ctx.send(embed=embed, file=avatar_file)
            else:
                await ctx.send(embed=embed)
            
        except discord.HTTPException as e:
            serverpfp_logger.error(f"Failed to fetch avatar for {target_user}: {e}")
            await ctx.send("❌ Couldn't get their avatar right now. Try again in a moment!")
        except Exception as e:
            serverpfp_logger.error(f"Unexpected error in serverpfp command: {e}")
            await ctx.send("❌ Something went wrong while getting that avatar!")


def setup_serverpfp_commands(bot):
    """Setup function to add server profile picture commands to the bot"""
    serverpfp_commands = ServerPfpCommands(bot)
    return serverpfp_commands 