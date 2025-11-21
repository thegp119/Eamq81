import os
import sys

# Force UTF-8 encoding globally
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['LANG'] = 'C.UTF-8'
os.environ['LC_ALL'] = 'C.UTF-8'

# Reload the standard library with UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import sqlite3
import aiohttp
import stripe
import asyncio
import time
from io import BytesIO
from PIL import Image, ImageOps

# Disable urllib3 warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Get config FIRST
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "").strip()

# Aggressive whitespace removal for token
if BOT_TOKEN:
    BOT_TOKEN = BOT_TOKEN.replace('\r', '').replace('\n', '').replace('\t', '').strip()
    
if STRIPE_API_KEY:
    STRIPE_API_KEY = STRIPE_API_KEY.replace('\r', '').replace('\n', '').replace('\t', '').strip()

# Debug: Log what we found (without exposing the actual secrets)
print(f"📋 BOT_TOKEN set: {bool(BOT_TOKEN)} (length: {len(BOT_TOKEN)})")
print(f"📋 STRIPE_API_KEY set: {bool(STRIPE_API_KEY)} (length: {len(STRIPE_API_KEY)})")

# Configure Stripe with UTF-8 headers
stripe.api_key = STRIPE_API_KEY
stripe.api_base = "https://api.stripe.com"

if not STRIPE_API_KEY:
    print("⚠️  WARNING: STRIPE_API_KEY not found in environment variables!")
if not BOT_TOKEN:
    print("⚠️  WARNING: BOT_TOKEN not found in environment variables!")

# Patch requests to force UTF-8
import requests
original_request = requests.adapters.HTTPAdapter.send

def patched_send(self, request, **kwargs):
    # Ensure all headers are ASCII-safe
    if hasattr(request, 'headers'):
        for key in list(request.headers.keys()):
            try:
                request.headers[key].encode('ascii')
            except (UnicodeEncodeError, AttributeError):
                del request.headers[key]
    return original_request(self, request, **kwargs)

requests.adapters.HTTPAdapter.send = patched_send

import requests.utils
original_to_native = requests.utils.to_native_string

def safe_to_native(string, encoding='utf-8'):
    if isinstance(string, bytes):
        return string.decode(encoding, errors='ignore')
    return str(string)

requests.utils.to_native_string = safe_to_native

GUILD_ID = 1429807376298414252
SOURCE_CHANNEL_ID = 1430523739480658032
REVIEW_CHANNEL_ID = 1432005106534055948

WATERMARK_URL = "https://cdn.discordapp.com/attachments/1430523739480658032/1438937291052552433/F9C40A73-07D0-448A-9744-F63E4EC53213.png?ex=6918b248&is=691760c8&hm=d7067d9c03105e2ba0bbf551e4a5d9a5c9de9e65907a3223320c0c3e9780dacb&"

ALLOWED_ROLE_NAMES = ["Head Chef", "Chef🍳"]

DB_PATH = "vouch_points.db"
ORDER_TRACKING = {}
PAYMENT_SESSIONS = {}
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_test")
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

# ---------------- Payment Confirmation View ----------------
class PaymentConfirmView(View):
    def __init__(self, channel_id: int, amount: str):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.amount = amount

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if any(role.name in ALLOWED_ROLE_NAMES for role in interaction.user.roles):
            return True
        await interaction.response.send_message("❌ You don't have permission to confirm payments.", ephemeral=True)
        return False

    @discord.ui.button(label="✅ Payment Received", style=discord.ButtonStyle.success, custom_id="payment_confirm")
    async def confirm_payment(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        
        embed = discord.Embed(
            title="✅ Payment Received",
            description=f"Payment of **${self.amount}** has been successfully received!",
            color=discord.Color.green()
        )
        embed.add_field(name="Amount", value=f"${self.amount}", inline=True)
        embed.add_field(name="Status", value="✅ Confirmed", inline=True)
        embed.set_footer(text="Thank you for your payment!")
        embed.set_thumbnail(url=WATERMARK_URL)
        
        await interaction.channel.send(embed=embed)
        print(f"✅ Payment of ${self.amount} confirmed by {interaction.user}")

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
async def webhook_server():
    from aiohttp import web
    
    async def handle_webhook(request):
        payload = await request.text()
        sig_header = request.headers.get('stripe-signature')
        
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
        except Exception as e:
            print(f"⚠️ Webhook signature verification failed: {e}")
            return web.Response(status=400)
        
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            session_id = session['id']
            
            if session_id in PAYMENT_SESSIONS:
                payment_info = PAYMENT_SESSIONS[session_id]
                channel_id = payment_info['channel_id']
                amount = payment_info['amount']
                
                try:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        embed = discord.Embed(
                            title="✅ Payment Received",
                            description=f"Payment of **${amount}** has been successfully received!",
                            color=discord.Color.green()
                        )
                        embed.add_field(name="Amount paid", value=f"${amount}", inline=False)
                        embed.add_field(name="Status", value="✅ Confirmed", inline=False)
                        embed.set_footer(text="Thank you for your payment!")
                        embed.set_thumbnail(url=WATERMARK_URL)
                        
                        await channel.send(embed=embed)
                        print(f"✅ Automatic payment confirmation sent for ${amount} to channel {channel_id}")
                        del PAYMENT_SESSIONS[session_id]
                except Exception as e:
                    print(f"❌ Error sending payment confirmation: {e}")
        
        return web.Response(status=200)
    
    app = web.Application()
    app.router.add_post('/webhook', handle_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("✅ Stripe webhook server started on port 8080")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    init_db()
    bot.watermark_bytes = None
    if not bot.loop.is_running():
        bot.loop.create_task(update_order_tracking())
        bot.loop.create_task(webhook_server())
    async with aiohttp.ClientSession() as s:
        try:
            bot.watermark_bytes = await fetch_bytes(s, WATERMARK_URL)
            print("✅ Watermark downloaded")
        except Exception as e:
            print(f"⚠️ Failed to download watermark: {e}")
            print("⚠️ Images will be posted without watermark")
            bot.watermark_bytes = None
    print("✅ Order tracking and webhook systems initialized")

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
async def total(ctx, amount=None):
    if not has_chef_role(ctx.author):
        return await ctx.reply("❌ You don't have permission to use this command.")
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.reply("⚠️ This command only works in **ticket channels**.")
    if not amount or not amount.replace('.', '', 1).isdigit():
        return await ctx.reply("⚠️ Usage: `!total <amount>`")
    
    try:
        amount_cents = int(float(amount) * 100)
        
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": "Dish Dynasty Order",
                        },
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url="https://discord.com",
            cancel_url="https://discord.com",
        )
        
        stripe_link = checkout_session.url
        
        embed = discord.Embed(title="💳 Dish Dynasty Payment",
                              description=f"Your total is **${amount}**.\nClick below to pay now ✅",
                              color=0x5865F2)
        embed.set_thumbnail(url=WATERMARK_URL)
        embed.set_footer(text="Thank you for choosing Dish Dynasty!")
        
        PAYMENT_SESSIONS[checkout_session.id] = {
            "channel_id": ctx.channel.id,
            "amount": amount,
            "created_at": time.time()
        }
        
        view = PaymentConfirmView(channel_id=ctx.channel.id, amount=amount)
        button = discord.ui.Button(label="Pay Now", style=discord.ButtonStyle.link, url=stripe_link)
        view.add_item(button)
        
        await ctx.send(embed=embed, view=view)
        print(f"✅ Payment link created by {ctx.author}: ${amount} (Session: {checkout_session.id})")
        
        try:
            await ctx.message.delete()
        except:
            pass
            
    except Exception as e:
        await ctx.send("❌ Failed to create payment link. Please try again.")
        print(f"Error creating payment link: {e}")

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

async def fetch_order_status(link):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(link, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    if "PREPARING" in html or "preparing" in html.lower():
                        return {"status": "Preparing", "progress": 20, "emoji": "🍳"}
                    elif "CONFIRMED" in html or "confirmed" in html.lower():
                        return {"status": "Confirmed", "progress": 35, "emoji": "✅"}
                    elif "DELIVERING" in html or "delivering" in html.lower():
                        return {"status": "On the Way", "progress": 70, "emoji": "🚗"}
                    elif "DELIVERED" in html or "delivered" in html.lower():
                        return {"status": "Delivered", "progress": 100, "emoji": "📦"}
                    return {"status": "Preparing", "progress": 20, "emoji": "🍳"}
    except Exception as e:
        print(f"Error fetching order status: {e}")
    return {"status": "Preparing", "progress": 20, "emoji": "🍳"}

def get_progress_bar(progress):
    filled = int(progress / 10)
    empty = 10 - filled
    return "▓" * filled + "░" * empty + f" {progress}%"

async def update_order_tracking():
    while True:
        try:
            await asyncio.sleep(30)
            for msg_id, order_data in list(ORDER_TRACKING.items()):
                try:
                    msg = order_data["message"]
                    link = order_data["link"]
                    status_info = await fetch_order_status(link)
                    
                    progress = status_info["progress"]
                    status = status_info["status"]
                    emoji = status_info["emoji"]
                    
                    current_time = time.time()
                    
                    if progress == 100 and order_data["delivered_time"] is None:
                        order_data["delivered_time"] = current_time
                        print(f"✅ Order {msg_id} marked as delivered at {current_time}")
                    
                    time_since_delivery = None
                    if order_data["delivered_time"] is not None:
                        time_since_delivery = current_time - order_data["delivered_time"]
                    
                    if time_since_delivery and time_since_delivery >= 420:
                        try:
                            await msg.delete()
                            print(f"✅ Order {msg_id} message deleted after 7 minutes")
                        except:
                            pass
                        del ORDER_TRACKING[msg_id]
                        continue
                    
                    driver_info = "🔍 Not yet assigned"
                    if progress >= 70:
                        driver_info = "🚗 Driver arriving soon"
                    if progress >= 100:
                        driver_info = "✅ Order delivered!"
                    
                    embed = discord.Embed(
                        title="🍕 Order Tracking",
                        description=f"Real-time Uber Eats order tracking",
                        color=discord.Color.green() if progress == 100 else discord.Color.blurple()
                    )
                    embed.add_field(name=f"{emoji} Status", value=status, inline=False)
                    embed.add_field(name="🚗 Driver", value=driver_info, inline=False)
                    embed.add_field(name="Progress", value=get_progress_bar(progress), inline=False)
                    
                    if progress == 100 and time_since_delivery:
                        time_left = int(420 - time_since_delivery)
                        embed.set_footer(text=f"Message will disappear in {time_left} seconds")
                    else:
                        embed.set_footer(text="Updates every 30 seconds")
                    
                    await msg.edit(embed=embed)
                except Exception as e:
                    print(f"Error updating order {msg_id}: {e}")
        except Exception as e:
            print(f"Error in update_order_tracking: {e}")

@bot.command(name="order")
async def order_cmd(ctx, uber_link=None):
    if not uber_link or "ubereats" not in uber_link.lower():
        return await ctx.reply("⚠️ Usage: `!order <uber eats tracking link>`")
    
    try:
        await ctx.message.delete()
    except:
        pass
    
    embed = discord.Embed(
        title="🍕 Order Tracking",
        description="Fetching order details...",
        color=discord.Color.blurple()
    )
    embed.add_field(name="🍳 Status", value="Initializing...", inline=False)
    embed.add_field(name="🔍 Driver", value="Not yet assigned", inline=False)
    embed.add_field(name="Progress", value="▓░░░░░░░░░ 10%", inline=False)
    embed.set_footer(text="Updates every 30 seconds")
    msg = await ctx.send(embed=embed)
    
    ORDER_TRACKING[msg.id] = {
        "link": uber_link,
        "message": msg,
        "status": "preparing",
        "progress": 10,
        "driver_name": None,
        "driver_car": None,
        "driver_plate": None,
        "last_update": 0,
        "delivered_time": None
    }
    print(f"✅ Order tracking started for {ctx.author}: {uber_link}")

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