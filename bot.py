import os
import re
import math
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands

# -----------------------------
# Config via Environment Vars
# -----------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
ADMIN_CHANNEL_ID = int(os.environ.get("ADMIN_CHANNEL_ID", "0"))  # optional but recommended

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var")
if GUILD_ID == 0:
    raise RuntimeError("Missing/invalid GUILD_ID env var")

DB_PATH = os.environ.get("DB_PATH", "leohunt.db")

# -----------------------------
# Geo helpers (DMS + distance)
# -----------------------------
def dms_to_decimal(deg: int, minutes: int, seconds: float, hemi: str) -> float:
    dec = abs(deg) + minutes / 60.0 + seconds / 3600.0
    if hemi.upper() in ("S", "W"):
        dec = -dec
    return dec

# Accepts: 18°24'56"N 13°01'56"E (flexible separators)
DMS_LAT_RE = re.compile(r"""(\d+)\D+(\d+)\D+(\d+(?:\.\d+)?)\D*([NS])""", re.IGNORECASE)
DMS_LON_RE = re.compile(r"""(\d+)\D+(\d+)\D+(\d+(?:\.\d+)?)\D*([EW])""", re.IGNORECASE)

def parse_dms_pair(text: str) -> tuple[float, float]:
    t = text.strip().replace(",", " ")
    parts = [p for p in t.split() if p]
    if len(parts) < 2:
        raise ValueError('Please provide latitude and longitude in DMS. Example: 18°24\'56"N 13°01\'56"E')

    lat_raw, lon_raw = parts[0], parts[1]
    m1 = DMS_LAT_RE.search(lat_raw)
    m2 = DMS_LON_RE.search(lon_raw)
    if not m1 or not m2:
        raise ValueError('Invalid DMS format. Example: 18°24\'56"N 13°01\'56"E')

    lat = dms_to_decimal(int(m1.group(1)), int(m1.group(2)), float(m1.group(3)), m1.group(4))
    lon = dms_to_decimal(int(m2.group(1)), int(m2.group(2)), float(m2.group(3)), m2.group(4))
    return lat, lon

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

# -----------------------------
# Database
# -----------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS state (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_week_user ON submissions(week_id, user_id)")
    con.commit()
    con.close()

def get_state(key: str, default: str | None = None) -> str | None:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT v FROM state WHERE k=?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else default

def set_state(key: str, value: str):
    con = db()
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    con.commit()
    con.close()

def current_week_id() -> str:
    # Default week id if not set:
    return get_state("week_id", "week-1") or "week-1"

def set_week_id(week_id: str):
    set_state("week_id", week_id)

def set_target_dms(dms: str):
    # validate before saving
    parse_dms_pair(dms)
    set_state("target_dms", dms)

def get_target() -> tuple[float, float] | None:
    dms = get_state("target_dms", None)
    if not dms:
        return None
    return parse_dms_pair(dms)

def count_attempts(week_id: str, user_id: int) -> int:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM submissions WHERE week_id=? AND user_id=?", (week_id, user_id))
    n = cur.fetchone()[0]
    con.close()
    return int(n)

def add_submission(week_id: str, user_id: int, lat: float, lon: float, ts: int):
    con = db()
    con.execute("INSERT INTO submissions(week_id,user_id,lat,lon,ts) VALUES(?,?,?,?,?)", (week_id, user_id, lat, lon, ts))
    con.commit()
    con.close()

def best_per_user(week_id: str, target: tuple[float,float]) -> list[tuple[int, float, int]]:
    tlat, tlon = target
    con = db()
    cur = con.cursor()
    cur.execute("SELECT user_id, lat, lon, ts FROM submissions WHERE week_id=?", (week_id,))
    rows = cur.fetchall()
    con.close()

    # Compute best distance per user; tie-break by earliest ts
    best: dict[int, tuple[float, int]] = {}
    for user_id, lat, lon, ts in rows:
        dist = haversine_m(lat, lon, tlat, tlon)
        if user_id not in best:
            best[user_id] = (dist, ts)
        else:
            cur_dist, cur_ts = best[user_id]
            if dist < cur_dist or (abs(dist-cur_dist) < 1e-9 and ts < cur_ts):
                best[user_id] = (dist, ts)

    result = [(uid, best[uid][0], best[uid][1]) for uid in best]
    result.sort(key=lambda x: (x[1], x[2]))  # distance asc, timestamp asc
    return result

# -----------------------------
# Discord Bot
# -----------------------------
intents = discord.Intents.none()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def is_admin(interaction: discord.Interaction) -> bool:
    # Admin if they have Administrator permission
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.administrator

class SubmitModal(discord.ui.Modal, title="LEOHUNT Submission"):
    coords = discord.ui.TextInput(
        label="Coordinates (Google Earth DMS)",
        placeholder='Example: 18°24\'56"N 13°01\'56"E',
        required=True,
        max_length=80
    )

    async def on_submit(self, interaction: discord.Interaction):
        week_id = current_week_id()
        target = get_target()
        if not target:
            await interaction.response.send_message(
                "The weekly target is not set yet. Please wait for the challenge to start.",
                ephemeral=True
            )
            return

        # Attempts limit
        used = count_attempts(week_id, interaction.user.id)
        if used >= 10:
            await interaction.response.send_message(
                "You have reached the maximum of **10 submissions** for this week.",
                ephemeral=True
            )
            return

        # Parse DMS
        try:
            lat, lon = parse_dms_pair(str(self.coords.value))
        except Exception as e:
            await interaction.response.send_message(
                f"Invalid format. Please copy coordinates directly from Google Earth.\n"
                f"Example: `18°24'56\"N 13°01'56\"E`",
                ephemeral=True
            )
            return

        # Save submission with timestamp (seconds)
        ts = int(datetime.now(timezone.utc).timestamp())
        add_submission(week_id, interaction.user.id, lat, lon, ts)

        used_after = used + 1
        # We DO NOT show their distance (prevents meta-gaming)
        await interaction.response.send_message(
            f"✅ Submission received for **{week_id}**.\n"
            f"Attempts used: **{used_after}/10**.\n"
            f"Good luck 🧭",
            ephemeral=True
        )

class SubmitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Submit Coordinates", style=discord.ButtonStyle.primary, emoji="🧭", custom_id="leohunt_submit")
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SubmitModal())

@tree.command(name="setup_panel", description="Post the submission panel (admin only).")
async def setup_panel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    embed = discord.Embed(
        title="LEOHUNT – Coordinate Submissions",
        description=(
            "**THIS CHANNEL IS ONLY FOR COORDINATES.**\n\n"
            "📍 **Format (Google Earth):** DMS\n"
            "Example: `18°24'56\"N 13°01'56\"E`\n\n"
            "📌 **Rules:**\n"
            "• Maximum **10** submissions per week\n"
            "• No editing or deleting submissions\n"
            "• Only coordinates – no text\n\n"
            "🏆 **Winner:** Closest coordinate wins\n"
            "⏱️ If equally close → earliest timestamp wins"
        ),
    )
    await interaction.channel.send(embed=embed, view=SubmitView())
    await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

@tree.command(name="start_week", description="Start a new week and reset attempts (admin only).")
@app_commands.describe(week_id="Example: week-1 or 2026-01-01")
async def start_week(interaction: discord.Interaction, week_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    set_week_id(week_id.strip())
    # Optionally clear target for safety:
    set_state("target_dms", "")
    await interaction.response.send_message(f"✅ New week started: **{week_id}**. Target cleared (set it with /set_target).", ephemeral=True)

@tree.command(name="set_target", description="Set the weekly target in DMS (admin only).")
@app_commands.describe(dms="Example: 18°24'56\"N 13°01'56\"E")
async def set_target(interaction: discord.Interaction, dms: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    try:
        set_target_dms(dms.strip())
    except Exception:
        await interaction.response.send_message("Invalid DMS format. Example: `18°24'56\"N 13°01'56\"E`", ephemeral=True)
        return
    await interaction.response.send_message("✅ Target set for this week.", ephemeral=True)

@tree.command(name="leaderboard", description="Show Top 10 leaderboard (admin only).")
@app_commands.describe(top_n="How many (max 100)")
async def leaderboard(interaction: discord.Interaction, top_n: int = 10):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    if top_n < 1:
        top_n = 10
    top_n = min(top_n, 100)

    week_id = current_week_id()
    target = get_target()
    if not target:
        await interaction.response.send_message("Target not set yet. Use /set_target first.", ephemeral=True)
        return

    rows = best_per_user(week_id, target)[:top_n]
    if not rows:
        await interaction.response.send_message("No submissions yet.", ephemeral=True)
        return

    lines = []
    for i, (uid, dist_m, ts) in enumerate(rows, start=1):
        t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"**#{i}** <@{uid}> — **{dist_m:.2f} m** — `{t}`")

    msg = f"🏆 **LEOHUNT Leaderboard** — **{week_id}** (Top {len(rows)})\n" + "\n".join(lines)

    # If you set ADMIN_CHANNEL_ID, we also drop it there
    if ADMIN_CHANNEL_ID and interaction.guild:
        ch = interaction.guild.get_channel(ADMIN_CHANNEL_ID)
        if ch and isinstance(ch, discord.TextChannel):
            await ch.send(msg)

    await interaction.response.send_message(msg, ephemeral=True)

@client.event
async def on_ready():
    init_db()
    # persistent view for button
    client.add_view(SubmitView())
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"Logged in as {client.user} | Commands synced to guild {GUILD_ID}")

# Restrict commands to your guild for instant updates
@tree.command(name="ping", description="Health check.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong 🧭", ephemeral=True)

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
