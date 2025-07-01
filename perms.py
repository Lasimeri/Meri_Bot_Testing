"""
Permissions Command Module for Meri Bot

This module contains functionality to display bot permissions.
Helps debug permission issues and shows what the bot can do.

Commands included:
- perms: Display bot permissions in the current server/channel
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

# Set up logger for perms operations
perms_logger = logging.getLogger("MeriPerms")


class PermsCommands(commands.Cog):
    """Permissions Commands Cog"""
    
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="perms", description="Check bot permissions in this server/channel")
    @app_commands.describe(channel="The channel to check permissions for (defaults to current channel)")
    async def check_permissions(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Display bot permissions in the current or specified channel."""
        
        # Can't check permissions in DMs
        if not ctx.guild:
            return await ctx.send("❌ This command only works in servers!")
        
        # Default to current channel
        target_channel = channel or ctx.channel
        
        try:
            # Get bot member object
            bot_member = ctx.guild.me
            if not bot_member:
                return await ctx.send("❌ Couldn't get bot member information!")
            
            # Get permissions
            guild_perms = bot_member.guild_permissions
            channel_perms = target_channel.permissions_for(bot_member)
            
            # Create embed
            embed = discord.Embed(
                title="🔐 Bot Permissions",
                description=f"Permissions for **{self.bot.user.name}** in **{ctx.guild.name}**",
                color=0x00ff00 if channel_perms.administrator else 0x3498db
            )
            
            # Important permissions for bot functionality
            important_perms = {
                'administrator': ('👑', 'Administrator'),
                'send_messages': ('💬', 'Send Messages'),
                'embed_links': ('🔗', 'Embed Links'),
                'attach_files': ('📎', 'Attach Files'),
                'read_message_history': ('📜', 'Read Message History'),
                'add_reactions': ('😊', 'Add Reactions'),
                'use_external_emojis': ('😎', 'Use External Emojis'),
                'manage_messages': ('🗑️', 'Manage Messages'),
                'connect': ('🎤', 'Connect to Voice'),
                'speak': ('🔊', 'Speak in Voice'),
                'use_voice_activation': ('🎙️', 'Use Voice Activity'),
                'view_channel': ('👁️', 'View Channel')
            }
            
            # Check channel-specific permissions
            channel_perms_list = []
            for perm, (emoji, name) in important_perms.items():
                has_perm = getattr(channel_perms, perm, False)
                channel_perms_list.append(f"{emoji} {name}: {'✅' if has_perm else '❌'}")
            
            embed.add_field(
                name=f"📍 Channel Permissions ({target_channel.mention})",
                value='\n'.join(channel_perms_list[:6]),  # First half
                inline=True
            )
            
            embed.add_field(
                name="​",  # Invisible character for spacing
                value='\n'.join(channel_perms_list[6:]),  # Second half
                inline=True
            )
            
            # Add server-wide permissions summary
            server_perms_summary = []
            admin_status = "✅ Has Administrator" if guild_perms.administrator else "❌ No Administrator"
            server_perms_summary.append(f"👑 {admin_status}")
            
            # Count total permissions
            total_perms = sum(1 for perm, value in guild_perms if value)
            server_perms_summary.append(f"📊 Total Permissions: {total_perms}")
            
            embed.add_field(
                name="🏰 Server-Wide Status",
                value='\n'.join(server_perms_summary),
                inline=False
            )
            
            # Add role information
            bot_roles = [role.mention for role in bot_member.roles if role.name != "@everyone"]
            if bot_roles:
                embed.add_field(
                    name="👥 Bot Roles",
                    value=' '.join(bot_roles[:10]),  # Limit to prevent overflow
                    inline=False
                )
            
            # Add warnings for missing critical permissions
            warnings = []
            if not channel_perms.send_messages:
                warnings.append("⚠️ Cannot send messages in this channel!")
            if not channel_perms.embed_links:
                warnings.append("⚠️ Cannot send embeds (required for most commands)!")
            if not channel_perms.attach_files:
                warnings.append("⚠️ Cannot upload files (profile pictures won't work)!")
            
            if warnings:
                embed.add_field(
                    name="⚠️ Warnings",
                    value='\n'.join(warnings),
                    inline=False
                )
            
            # Set footer with additional info
            embed.set_footer(
                text=f"Channel ID: {target_channel.id} | Bot ID: {self.bot.user.id}",
                icon_url=self.bot.user.display_avatar.url
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            perms_logger.error(f"Error checking permissions: {e}")
            await ctx.send(f"❌ Error checking permissions: {e}")

    @commands.hybrid_command(name="allperms", description="Show all bot permissions in detail")
    async def all_permissions(self, ctx):
        """Display all bot permissions with detailed breakdown."""
        
        # Can't check permissions in DMs
        if not ctx.guild:
            return await ctx.send("❌ This command only works in servers!")
        
        try:
            # Get bot member object
            bot_member = ctx.guild.me
            if not bot_member:
                return await ctx.send("❌ Couldn't get bot member information!")
            
            # Get permissions
            guild_perms = bot_member.guild_permissions
            
            # Create embed
            embed = discord.Embed(
                title="📋 All Bot Permissions",
                description=f"Complete permission list for **{self.bot.user.name}**",
                color=0x00ff00 if guild_perms.administrator else 0x3498db
            )
            
            # Group permissions by category
            general_perms = []
            text_perms = []
            voice_perms = []
            
            # Define permission categories
            text_permissions = [
                'send_messages', 'send_tts_messages', 'manage_messages',
                'embed_links', 'attach_files', 'read_message_history',
                'mention_everyone', 'use_external_emojis', 'add_reactions',
                'use_slash_commands', 'create_public_threads', 'create_private_threads',
                'send_messages_in_threads', 'manage_threads'
            ]
            
            voice_permissions = [
                'connect', 'speak', 'mute_members', 'deafen_members',
                'move_members', 'use_voice_activation', 'priority_speaker',
                'stream', 'use_embedded_activities', 'use_soundboard',
                'use_external_sounds', 'request_to_speak'
            ]
            
            # Check all permissions
            for perm, value in guild_perms:
                perm_display = perm.replace('_', ' ').title()
                status = '✅' if value else '❌'
                
                if perm in text_permissions:
                    text_perms.append(f"{status} {perm_display}")
                elif perm in voice_permissions:
                    voice_perms.append(f"{status} {perm_display}")
                else:
                    general_perms.append(f"{status} {perm_display}")
            
            # Add fields (Discord has a limit of 25 fields)
            if general_perms:
                embed.add_field(
                    name="🔧 General Permissions",
                    value='\n'.join(general_perms[:10]) or "None",
                    inline=True
                )
            
            if text_perms:
                embed.add_field(
                    name="💬 Text Permissions",
                    value='\n'.join(text_perms[:10]) or "None",
                    inline=True
                )
            
            if voice_perms:
                embed.add_field(
                    name="🎤 Voice Permissions",
                    value='\n'.join(voice_perms[:10]) or "None",
                    inline=True
                )
            
            # Add summary
            total_granted = sum(1 for _, value in guild_perms if value)
            total_possible = len(list(guild_perms))
            
            embed.add_field(
                name="📊 Permission Summary",
                value=f"Granted: {total_granted}/{total_possible} permissions\n"
                      f"Administrator: {'✅ Yes' if guild_perms.administrator else '❌ No'}",
                inline=False
            )
            
            embed.set_footer(
                text=f"Use {ctx.prefix}perms for channel-specific permissions",
                icon_url=self.bot.user.display_avatar.url
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            perms_logger.error(f"Error listing all permissions: {e}")
            await ctx.send(f"❌ Error listing permissions: {e}")


def setup_perms_commands(bot):
    """Setup function to add permission commands to the bot"""
    perms_commands = PermsCommands(bot)
    return perms_commands 