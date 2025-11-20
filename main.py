import discord
from discord.ext import commands
from discord.ui import View, Button
import sqlite3
import aiohttp
import os
from io import BytesIO
from PIL import Image, ImageOps

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")

GUILD_ID = 1429807376298414252
SOURCE_CHANNEL_ID = 1430523739480658032
REVIEW_CHANNEL_ID = 1432005106534055948

WATERMARK_URL = "https://cdn.discordapp.com/attachments/1430523739480658032/1438937291052552433/F9C40A73-07D0-448A-9744-F63E4EC53213.png?ex=6918b248&is=691760c8&hm=d7067d9c03105e2ba0bbf551e4a5d9a5c9de9e65907a3223320c0c3e9780dacb&"

ALLOWED_ROLE_NAMES = ["Head Chef", "Chef🍳"]

DB_PATH = "vouch_points.db"
# ----------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- Role Check ----------------
def has_chef_role(member: discord.Member):
    return any(role.name in ALLOWED_ROLE_NAMES for role in member.roles)

# ---------------- SQLite helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS points (
            user_id INTEGER PRIMARY KEY,
            points INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()

def get_points(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def add_point(user_id: int, amount: int = 1) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO points(user_id, points) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET points = points + ?",
        (user_id, amount, amount)
    )
    conn.commit()
    c.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
    new = c.fetchone()[0]
    conn.close()
    return new

# ---------------- Utility ----------------
async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()

async def watermark_image(original_bytes: bytes, watermark_bytes: bytes) -> BytesIO:
    print(f"Applying watermark... Original image size: {len(original_bytes)} bytes, Watermark size: {len(watermark_bytes)} bytes")
    with Image.open(BytesIO(original_bytes)).convert("RGBA") as base:
        if getattr(base, "is_animated", False):
            base = base.convert("RGBA")

        with Image.open(BytesIO(watermark_bytes)).convert("RGBA") as wm:
            base_w, base_h = base.size
            target_w = int(base_w * 0.30)
            wm_ratio = wm.width / wm.height
            target_h = int(target_w / wm_ratio)
            wm_resized = wm.resize((target_w, target_h), Image.LANCZOS)
            print(f"Watermark positioned at center: ({(base_w - target_w) // 2}, {(base_h - target_h) // 2}), size: {target_w}x{target_h}")

            pos = ((base_w - target_w) // 2, (base_h - target_h) // 2)

            alpha = wm_resized.split()[3]
            alpha = ImageOps.autocontrast(alpha)
            alpha = alpha.point(lambda p: int(p * 0.55))
            wm_resized.putalpha(alpha)

            composite = Image.new("RGBA", base.size)
            composite.paste(base, (0, 0))
            composite.paste(wm_resized, pos, mask=wm_resized)

            output = BytesIO()
            composite = composite.convert("RGBA")
            composite.save(output, format="PNG")
            output.seek(0)
            print(f"✅ Watermark applied successfully, output size: {len(output.getvalue())} bytes")
            return output

# ---------------- Review Buttons ----------------
class ReviewView(View):
    def __init__(self, original_author_id: int, original_channel_id: int, image_bytes: bytes, watermark_bytes: bytes):
        super().__init__(timeout=None)
        self.original_author_id = original_author_id
        self.original_channel_id = original_channel_id
        self.image_bytes = image_bytes
        self.watermark_bytes = watermark_bytes

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if any(role.name in ALLOWED_ROLE_NAMES for role in interaction.user.roles):
            return True
        await interaction.response.send_message("❌ You don't have permission to approve/reject vouches.", ephemeral=True)
        return False

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="vouch_approve")
    async def approve(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()

        if self.watermark_bytes:
            try:
                image_buf = await watermark_image(self.image_bytes, self.watermark_bytes)
            except Exception as e:
                print(f"Failed to apply watermark: {e}, using original image")
                image_buf = BytesIO(self.image_bytes)
        else:
            print("No watermark available, using original image")
            image_buf = BytesIO(self.image_bytes)

        guild = interaction.guild
        target_channel = guild.get_channel(self.original_channel_id)
        if not target_channel:
            await interaction.followup.send("Original channel not found.", ephemeral=True)
            return

        mention = f"<@{self.original_author_id}>"
        new_points = add_point(self.original_author_id, 1)

        file = discord.File(fp=image_buf, filename="vouch.png")
        embed = discord.Embed(title="✅ Verified Dish Dynasty vouch", color=discord.Color.green())
        embed.set_image(url="attachment://vouch.png")
        embed.description = f"{mention}\nVerified Dish Dynasty vouch for {mention}! They now have **{new_points}** points."

        await target_channel.send(embed=embed, file=file)

        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="vouch_reject")
    async def reject(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        try:
            member = interaction.guild.get_member(self.original_author_id)
            if member:
                await member.send(f"❌ Your image submitted for vouch was rejected by {interaction.user.display_name}.")
        except Exception:
            pass

# ---------------- Events ----------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    init_db()
    bot.watermark_bytes = None
    async with aiohttp.ClientSession() as s:
        try:
            bot.watermark_bytes = await fetch_bytes(s, WATERMARK_URL)
            print("✅ Watermark downloaded")
        except Exception as e:
            print(f"⚠️ Failed to download watermark: {e}")
            print("⚠️ Images will be posted without watermark")
            bot.watermark_bytes = None

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    print(f"Message received from {message.author} in channel {message.channel.id}")

    if message.channel.id == SOURCE_CHANNEL_ID:
        image_attachment = None
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image"):
                image_attachment = att
                break
            if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                image_attachment = att
                break

        if image_attachment:
            async with aiohttp.ClientSession() as session:
                try:
                    original_bytes = await fetch_bytes(session, image_attachment.url)
                except Exception as e:
                    print("Failed to fetch image:", e)
                    return

            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                print("Missing permission to delete message in source channel.")

            review_channel = bot.get_channel(REVIEW_CHANNEL_ID)
            if not review_channel:
                print("❌ Review channel not found!")
                return

            watermark_bytes = bot.watermark_bytes or b""
            view = ReviewView(
                original_author_id=message.author.id,
                original_channel_id=SOURCE_CHANNEL_ID,
                image_bytes=original_bytes,
                watermark_bytes=watermark_bytes
            )

            preview_file = discord.File(BytesIO(original_bytes), filename="preview.png")
            review_embed = discord.Embed(
                title="🖼️ New vouch submitted",
                description=f"Submitted by <@{message.author.id}>",
                color=discord.Color.blurple()
            )
            review_embed.set_image(url="attachment://preview.png")
            msg = await review_channel.send(embed=review_embed, file=preview_file, view=view)
            print(f"Review message sent for image from {message.author}")
            return

    await bot.process_commands(message)

# ---------------- Commands ----------------
@bot.command()
async def rules(ctx):
    if not has_chef_role(ctx.author):
        return await ctx.reply("❌ You don't have permission to use this command.")
    embed = discord.Embed(
        title="📜 Dish Dynasty Rules",
        description="Please read and follow the rules to keep our community safe and fun!",
        color=0x5865F2
    )
    embed.add_field(name="1️⃣ Respect Everyone", value="No harassment, hate speech, or toxicity.")
    embed.add_field(name="2️⃣ No Self-Promo", value="No advertising without staff approval.")
    embed.add_field(name="3️⃣ Follow Channels", value="Post in the right channels & follow guidelines.")
    embed.add_field(name="4️⃣ Stay Safe", value="NSFW, illegal activity, or harmful content = 🚫")
    embed.add_field(name="5️⃣ Staff Has Final Say", value="Follow directions of staff members.")
    embed.set_thumbnail(url=WATERMARK_URL)
    embed.set_footer(text="Violation of rules may result in a warning or removal.")
    await ctx.send(embed=embed)

@bot.command()
async def total(ctx, amount=None, stripe_link=None):
    if not has_chef_role(ctx.author):
        return await ctx.reply("❌ You don't have permission to use this command.")
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.reply("⚠️ This command only works in **ticket channels**.")
    if not amount or not amount.replace('.', '', 1).isdigit():
        return await ctx.reply("⚠️ Usage: `!total <amount> <stripe link>`")
    if not stripe_link or not stripe_link.startswith("https://buy.stripe.com"):
        return await ctx.reply("⚠️ Provide a valid Stripe checkout link!")
    try:
        await ctx.message.delete()
    except:
        pass

    embed = discord.Embed(title="💳 Dish Dynasty Payment",
                          description=f"Your total is **${amount}**.\nClick below to pay now ✅",
                          color=0x5865F2)
    embed.set_thumbnail(url=WATERMARK_URL)
    embed.set_footer(text="Thank you for choosing Dish Dynasty!")
    button = discord.ui.Button(label="Pay Now", style=discord.ButtonStyle.link, url=stripe_link)
    view = discord.ui.View()
    view.add_item(button)
    await ctx.send(embed=embed, view=view)

@bot.command()
async def howto(ctx):
    if not has_chef_role(ctx.author):
        return await ctx.reply("❌ You don't have permission to use this command.")
    embed = discord.Embed(title="🍽️ How To Create a Group Order Link (Uber Eats)",
                          description="Follow these steps to create a group order that qualifies for the **$25 off $25** promo ✅",
                          color=0x5865F2)
    embed.set_thumbnail(url=WATERMARK_URL)
    embed.add_field(name="📍 Step 1",
                    value="Open Uber Eats, choose a restaurant **then select any items you want**.\nMake sure your subtotal **before taxes** is **at least $25** ✅", inline=False)
    embed.add_field(name="👥 Step 2",
                    value="View your cart, then tap the **icon in the top-right** that looks like a person with a ➕ sign.\nThis starts a **Group Order** ✅", inline=False)
    embed.set_image(url="https://cdn.discordapp.com/attachments/1430523739480658032/1438937291052552433/F9C40A73-07D0-448A-9744-F63E4EC53213.png?ex=6918b248&is=691760c8&hm=d7067d9c03105e2ba0bbf551e4a5d9a5c9de9e65907a3223320c0c3e9780dacb&")
    embed.set_footer(text="Dish Dynasty — Serving Better 🥘")
    await ctx.send(embed=embed)

@bot.command(name="points")
async def points_cmd(ctx, member: discord.Member = None):
    print(f"Points command called by {ctx.author} for member {member}")
    if not member:
        member = ctx.author
    pts = get_points(member.id)
    await ctx.send(f"{member.mention} has {pts} point{'s' if pts != 1 else ''}.")

# ---------------- NEW STATUS COMMAND (UPDATED WITH AUTO-DELETE) ----------------
@bot.command()
async def status(ctx):
    try:
        await ctx.message.delete()
    except:
        pass

    if not any(role.name == "Head Chef" for role in ctx.author.roles):
        return await ctx.reply("❌ You don't have permission to use this command.", delete_after=5)

    embed = discord.Embed(
        title="📦 Order Availability",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Status",
        value="Open - 🟢\nClosed - 🔴",
        inline=False
    )

    embed.add_field(
        name="————————————",
        value="\u200b",
        inline=False
    )

    promo = (
        "[click here](https://www.ubereats.com/marketing?"
        "mft=TARGETING_STORE_PROMO&pl=JTdCJTIyYWRkcmVzcyUyMiUzQSUyMjEyMzUlMjBOVyUyMDEwM3JkJTIwTG4lMjIlMkMlMjJyZWZlcmVuY2UlMjIlM0ElMjJkMjZhNjZkZC1lMDQwLWFkNzUtY2FhYi0yYWFiZTEyMzRjMDclMjIlMkMlMjJyZWZlcmVuY2VUeXBlJTIyJTNBJTIydWJlcl9wbGFjZXMlMjIlMkMlMjJsYXRpdHVkZSUyMiUzQTI1Ljg2OTY3NDElMkMlMjJsb25naXR1ZGUlMjIlM0EtODAuMjE4ODQzNCU3RA%3D%3D&"
        "promotionUuid=2d1831b0-f537-4e43-ae4a-ce647a3d6d65&ps=1&targetingStoreTag=restaurant_us_target_all)"
    )

    embed.add_field(
        name="$25 off $25 Promo",
        value=f"Selected store view → {promo}",
        inline=False
    )

    embed.add_field(
        name="Chef Fee 👨🏾‍🍳",
        value="$8",
        inline=False
    )

    embed.set_thumbnail(url=WATERMARK_URL)
    embed.set_footer(text="Dish Dynasty — Status Board")

    await ctx.send(embed=embed)

# ---------------- OPEN / CLOSED COMMANDS ----------------
STATUS_CHANNEL_ID = 1439755544448602283

@bot.command()
async def open(ctx):
    try:
        await ctx.message.delete()
    except:
        pass

    if not has_chef_role(ctx.author):
        return await ctx.reply("❌ You don't have permission to use this command.", delete_after=5)

    channel = ctx.guild.get_channel(STATUS_CHANNEL_ID)
    if not channel:
        return await ctx.reply("❌ Status channel not found.", delete_after=5)

    await channel.edit(name="🟢-open")
    await ctx.send("🟢 Status set to **OPEN**.", delete_after=5)

@bot.command()
async def closed(ctx):
    try:
        await ctx.message.delete()
    except:
        pass

    if not has_chef_role(ctx.author):
        return await ctx.reply("❌ You don't have permission to use this command.", delete_after=5)

    channel = ctx.guild.get_channel(STATUS_CHANNEL_ID)
    if not channel:
        return await ctx.reply("❌ Status channel not found.", delete_after=5)

    await channel.edit(name="🔴-closed")
    await ctx.send("🔴 Status set to **CLOSED**.", delete_after=5)

# ---------------- Run ----------------
bot.run(BOT_TOKEN)