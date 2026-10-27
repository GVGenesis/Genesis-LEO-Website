"""Public Service bot. Add it to your server; it keeps every member's roles in the database
so the website can check who may use the LEO CAD. Needs the SERVER MEMBERS INTENT enabled."""
import asyncio, os
import discord
from discord import app_commands
import db

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LEO = {int(x) for x in os.getenv("LEO_ROLE_IDS", "").split(",") if x.strip()}
intents = discord.Intents.default()
intents.members = True

class PublicServiceBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if db.pool is None: await db.init()
        if GUILD_ID:
            g = discord.Object(GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    def watching(self, guild):
        return not GUILD_ID or guild.id == GUILD_ID

    async def on_ready(self):
        for g in self.guilds:
            if self.watching(g):
                for m in g.members:
                    if not m.bot:
                        await db.get_or_create_user(m.id, m.name, m.display_avatar.url)
                        await db.set_roles(m.id, [r.id for r in m.roles])
        print(f"Role sync complete as {self.user}")

    async def on_member_join(self, m):
        if self.watching(m.guild) and not m.bot:
            await db.get_or_create_user(m.id, m.name, m.display_avatar.url)  # auto profile creation
            await db.set_roles(m.id, [r.id for r in m.roles])

    async def on_member_update(self, before, after):
        if self.watching(after.guild) and before.roles != after.roles:
            await db.set_roles(after.id, [r.id for r in after.roles])

    async def on_member_remove(self, m):
        if self.watching(m.guild): await db.set_roles(m.id, [])

bot = PublicServiceBot()

@bot.tree.command(name="rolecheck", description="Check whether a member can log in to the LEO CAD")
@app_commands.default_permissions(manage_roles=True)
async def rolecheck(i: discord.Interaction, member: discord.Member):
    ok = any(r.id in LEO for r in member.roles)
    await i.response.send_message(f"{member.mention} {'has' if ok else 'does not have'} a role that allows LEO CAD access.", ephemeral=True)

if __name__ == "__main__":
    bot.run(os.environ["PUBLIC_SERVICE_BOT_TOKEN"])
