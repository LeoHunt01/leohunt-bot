import os
import re
import math
import sqlite3
import discord
from discord import app_commands
from datetime import datetime, timezone

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))
DB_PATH = "leohunt.db"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------------- DATABASE ----------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            user_id INTEGER,
            lat REAL,
            lon REAL,
            timestamp TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS target (
            lat REAL,
            lon REAL
        )
    """)
    con.commit()
    con.close()

# ---------------- GEO ----------------
def dms_to_decimal(d, m, s, hemi):
    dec = abs(d) + m / 60 + s / 3600
    if hemi.upper() in ["S", "W"]:
        dec = -dec
    return dec

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

def parse_dms(text):
    regex = r"""(\d+)°(\d+)'(\d+)"([NS])\s+(\d+)°(\d+)'(\d+)"([EW])"""
    m = re.match(regex, text.strip())
    if not m:
        return None
    lat = dms_to_decimal(int(m[1]), int(m[2]), int(m[3]), m[4])
    lon = dms_to_decimal(int(m[5]), int(m[6]), int(m[7]), m[8])
    return lat, lon

# ---------------- EVENTS ----------------
@client.event
async def on_ready():
    init_db()
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("LeoHunt Bot online!")

# ---------------- COMMANDS ----------------
@tree.command(name="set_target", description="Set target coordinate (admin)", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def set_target(interaction: discord.Interaction, coord: str):
    parsed = parse_dms(coord)
    if not parsed:
        await interaction.response.send_message("❌ Invalid DMS format.", ephemeral=True)
        return
    lat, lon = parsed
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM target")
    cur.execute("INSERT INTO target VALUES (?,?)", (lat, lon))
    con.commit()
    con.close()
    await interaction.response.send_message("✅ Target set.", ephemeral=True)

@tree.command(name="leaderboard", description="Admin leaderboard", guild=discord.Object(id=GUILD_ID))
async def leaderboard(interaction: discord.Interaction, top: int = 10):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT lat, lon FROM target")
    target = cur.fetchone()
    if not target:
        await interaction.response.send_message("❌ No target set.", ephemeral=True)
        return

    cur.execute("SELECT user_id, lat, lon, timestamp FROM submissions")
    rows = cur.fetchall()
    con.close()

    scored = []
    for u, lat, lon, ts in rows:
        dist = haversine(lat, lon, target[0], target[1])
        scored.append((dist, ts, u))

    scored.sort(key=lambda x: (x[0], x[1]))
    msg = "**🏆 Leaderboard (Admin Only)**\n"
    for i, s in enumerate(scored[:top], 1):
        msg += f"{i}. <@{s[2]}> – {s[0]:.2f} km\n"

    await interaction.response.send_message(msg, ephemeral=True)

@client.event
async def on_message(message):
    if message.author.bot:
        return
    parsed = parse_dms(message.content)
    if not parsed:
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM submissions WHERE user_id=?", (message.author.id,))
    count = cur.fetchone()[0]
    if count >= 10:
        con.close()
        return

    lat, lon = parsed
    cur.execute(
        "INSERT INTO submissions VALUES (?,?,?,?)",
        (message.author.id, lat, lon, datetime.now(timezone.utc).isoformat())
    )
    con.commit()
    con.close()

# ---------------- START ----------------
client.run(TOKEN)
