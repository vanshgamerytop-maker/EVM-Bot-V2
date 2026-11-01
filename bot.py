import os
import sqlite3
import asyncio
import secrets
import string
import re
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
PREFIX = os.getenv("BOT_PREFIX", "$").strip() or "$"
BOT_NAME = "EVM Bot V1"
BRAND = "Made by EVIL SAAD"
DB_PATH = os.getenv("DB_PATH", "evm_bot.db")
PINGGY_HOST = os.getenv("PINGGY_HOST", "free.pinggy.io").strip() or "free.pinggy.io"
PINGGY_PORT = int(os.getenv("PINGGY_PORT", "443"))
PINGGY_PROCESSES = {}  # claim_id -> live SSH tunnel process
ENABLE_LXD_PROVISIONING = os.getenv("ENABLE_LXD_PROVISIONING", "false").lower() == "true"
LXD_IMAGE = os.getenv("LXD_IMAGE", "ubuntu:24.04").strip() or "ubuntu:24.04"
PORT_RANGE_START = int(os.getenv("PORT_RANGE_START", "20000"))
PORT_RANGE_END = int(os.getenv("PORT_RANGE_END", "30000"))

DEFAULT_PLANS = [
    ("XS", 6, 2, 25, 6),
    ("S", 8, 3, 30, 8),
    ("M", 12, 4, 40, 10),
    ("L", 16, 5, 50, 15),
    ("XL", 20, 6, 70, 20),
]

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.invites = True
intents.message_content = True

bot = commands.Bot(command_prefix=lambda _bot, _msg: PREFIX, intents=intents, help_command=None)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

def init_db():
    global PREFIX
    db.executescript("""
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS invites (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id));
    CREATE TABLE IF NOT EXISTS invite_uses (guild_id INTEGER NOT NULL, code TEXT NOT NULL, inviter_id INTEGER, uses INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,code));
    CREATE TABLE IF NOT EXISTS claims (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, plan TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS vps (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, name TEXT, status TEXT DEFAULT 'registered');
    CREATE TABLE IF NOT EXISTS vps_details (
      claim_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      instance_name TEXT NOT NULL, container_ip TEXT, public_ip TEXT, ssh_port INTEGER,
      game_port INTEGER, username TEXT NOT NULL DEFAULT 'evmuser', password TEXT NOT NULL,
      FOREIGN KEY(claim_id) REFERENCES claims(id)
    );
    CREATE TABLE IF NOT EXISTS port_mappings (
      id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      claim_id INTEGER NOT NULL, instance_name TEXT NOT NULL, public_port INTEGER NOT NULL UNIQUE,
      container_port INTEGER NOT NULL, protocol TEXT NOT NULL DEFAULT 'tcp',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    claim_cols = {r["name"] for r in db.execute("PRAGMA table_info(claims)").fetchall()}
    if "expires_at" not in claim_cols:
        db.execute("ALTER TABLE claims ADD COLUMN expires_at TEXT")
    for name, ram, cpu, disk, required in DEFAULT_PLANS:
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                   (f"plan_{name}", f"{ram},{cpu},{disk},{required}"))
    db.commit()
    saved_prefix = db.execute("SELECT value FROM settings WHERE key='prefix'").fetchone()
    if saved_prefix and saved_prefix["value"]:
        PREFIX = saved_prefix["value"]

def get_setting(key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()

def is_admin(ctx):
    return bool(ctx.author.guild_permissions.administrator or (OWNER_ID and ctx.author.id == OWNER_ID))

def plans():
    result = []
    for row in db.execute("SELECT key,value FROM settings WHERE key LIKE 'plan_%' ORDER BY key"):
        name = row["key"][5:]
        try:
            ram, cpu, disk, invites = map(int, row["value"].split(","))
            result.append((name, ram, cpu, disk, invites))
        except (ValueError, TypeError):
            continue
    return sorted(result, key=lambda p: p[4])

def invite_count(guild_id, user_id):
    row = db.execute("SELECT count FROM invites WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    return int(row["count"]) if row else 0

def add_invites(guild_id, user_id, amount):
    db.execute("""INSERT INTO invites(guild_id,user_id,count) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET count=count+excluded.count""",
      (guild_id, user_id, amount))
    db.commit()

def reserve_plan_claim(guild_id, user_id, plan_name, required_invites):
    """Atomically spend invite credits and create a provisioning claim.

    Credits are reserved before provisioning so simultaneous claims cannot reuse them.
    Call refund_plan_invites() if deployment fails.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT count FROM invites WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        available = int(row["count"]) if row else 0
        if available < required_invites:
            db.rollback()
            return None, available
        db.execute(
            "UPDATE invites SET count=count-? WHERE guild_id=? AND user_id=? AND count>=?",
            (required_invites, guild_id, user_id, required_invites),
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.rollback()
            return None, available
        cur = db.execute(
            "INSERT INTO claims(guild_id,user_id,plan,status) VALUES(?,?,?,'provisioning')",
            (guild_id, user_id, plan_name),
        )
        claim_id = cur.lastrowid
        db.commit()
        return claim_id, available - required_invites
    except Exception:
        db.rollback()
        raise

def refund_plan_invites(guild_id, user_id, amount):
    """Refund reserved invite credits if VPS provisioning fails."""
    if amount <= 0:
        return
    add_invites(guild_id, user_id, amount)

def embed(title, description="", color=0x7C3AED):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=f"{BOT_NAME} • {BRAND}")
    return e

def command_lines(category):
    data = {
      "🏠 Home": [
        (f"{PREFIX}home", "Open the EVM Bot V1 home screen"),
        (f"{PREFIX}help", "Browse all commands and categories"),
        (f"{PREFIX}quickhelp", "One-screen cheat sheet"),
        (f"{PREFIX}status", "Bot and service status"),
      ],
      "⚙️ General": [
        (f"{PREFIX}help", "Browse or search every command"),
        (f"{PREFIX}quickhelp", "One-screen cheat sheet"),
        (f"{PREFIX}ping", "Check bot latency"),
        (f"{PREFIX}about", "Bot version and statistics"),
        (f"{PREFIX}faq", "VPS and invite FAQ"),
        (f"{PREFIX}info [@user]", "Info about a user"),
        (f"{PREFIX}invite", "Get the bot invite link"),
        (f"{PREFIX}developer", "Show developer / main owner"),
        (f"{PREFIX}setprefix <prefix>", "Change command prefix (admin)"),
      ],
      "🖥️ VPS": [
        (f"{PREFIX}vps create", "Start an invite-based VPS claim request"),
        (f"{PREFIX}vps info", "View your registered VPS/claim requests"),
        (f"{PREFIX}manage [id] [action]", "View credentials privately; start/stop/restart/reinstall/sshx VPS"),
        (f"{PREFIX}vps start <id>", "Request VPS start (provider integration required)"),
        (f"{PREFIX}vps stop <id>", "Request VPS stop (provider integration required)"),
        (f"{PREFIX}vps restart <id>", "Request VPS restart (provider integration required)"),
        (f"{PREFIX}vps delete <id>", "Delete a registered VPS record (admin)"),
        (f"{PREFIX}ssh", "Show SSH setup guidance"),
        (f"{PREFIX}manage <id> sshx", "Start an SSHX browser terminal and DM its private session link"),
        (f"{PREFIX}console", "Show console integration status"),
        (f"{PREFIX}web", "Show web terminal integration status"),
        (f"{PREFIX}password", "Show password-rotation guidance"),
        (f"{PREFIX}pin", "Show web terminal PIN guidance"),
        (f"{PREFIX}upgrade", "Request a plan upgrade"),
        (f"{PREFIX}changeos", "Show OS reinstall integration status"),
        (f"{PREFIX}share", "Show VPS sharing integration status"),
        (f"{PREFIX}unshare", "Show VPS sharing integration status"),
      ],
      "🎟️ Invites": [
        (f"{PREFIX}invites [@user]", "Check invite count; add top for leaderboard"),
        (f"{PREFIX}invites top", "Show invite leaderboard"),
        (f"{PREFIX}invited", "Show invitees (requires invite tracking data)"),
        (f"{PREFIX}inviter @user", "Show inviter (when tracked)"),
        (f"{PREFIX}claim", "Claim a VPS plan using invite count"),
        (f"{PREFIX}addinvite @user <amount>", "Grant invite credit (admin; prefix configurable)"),
        (f"{PREFIX}plans", "Show current invite plans"),
      ],
      "📈 Monitoring": [
        (f"{PREFIX}dashboard", "Overview of registered VPS records"),
        (f"{PREFIX}live", "Refresh status overview"),
        (f"{PREFIX}top", "Top users by invite count"),
        (f"{PREFIX}graph", "Show a simple resource-monitoring status"),
        (f"{PREFIX}status", "Bot/database status"),
        (f"{PREFIX}serverstats", "Aggregated server statistics"),
        (f"{PREFIX}vps-stats", "Registered VPS counts"),
        (f"{PREFIX}vpsusage", "Admin usage overview"),
      ],
      "🔢 Ports": [
        (f"{PREFIX}ports", "Port forwarding help"),
        (f"{PREFIX}ports list", "List saved port rules (provider integration required)"),
        (f"{PREFIX}ports add", "Port publishing guidance"),
        (f"{PREFIX}ports remove", "Port removal guidance"),
      ],
      "🐋 Nodes": [
        (f"{PREFIX}node", "Admin node management status"),
      ],
      "🛠️ Admin": [
        (f"{PREFIX}setplan <name> <ram> <cpu> <disk> <invites>", "Create/update a plan"),
        (f"{PREFIX}delplan <name>", "Remove a plan"),
        (f"{PREFIX}setprefix <prefix>", "Change the bot prefix"),
        (f"{PREFIX}addinvite @user <amount>", "Add invite credits"),
        (f"{PREFIX}claimstatus <id> <status>", "Update claim status"),
      ]
    }
    return data.get(category, [])

CATEGORIES = ["🏠 Home", "⚙️ General", "🖥️ VPS", "🎟️ Invites", "📈 Monitoring", "🔢 Ports", "🐋 Nodes", "🛠️ Admin"]

def categories_embed(category):
    lines = command_lines(category)
    desc = "\n".join(f"`{cmd}` — {description}" for cmd, description in lines)
    if category == "🎟️ Invites":
        desc += "\n\n**Invite plans**\n" + "\n".join(
            f"✨ **{n} Plan — {req} Invites**\n↳ {ram} GB DDR4 ECC RAM · {cpu} vCore CPU · {disk} GB NVMe"
            for n, ram, cpu, disk, req in plans()
        )
    e = embed(category, desc or "No commands in this category.")
    e.add_field(name="Category", value=f"{CATEGORIES.index(category)+1}/{len(CATEGORIES)} • {BOT_NAME}", inline=False)
    return e

class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=c.replace(" #", "#"), value=c, emoji=c.split()[0]) for c in CATEGORIES]
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        await interaction.response.edit_message(embed=categories_embed(category), view=self.view)

class HomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(CategorySelect())

    @discord.ui.button(label="📊 Quick Reference", style=discord.ButtonStyle.secondary, row=1)
    async def quick_reference(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = embed("📑 EVM Bot V1 Command Reference",
                  "Premium LXD/KVM VPS Deploy Platform\n"
                  f"Prefix: `{PREFIX}`\n\n" +
                  "\n".join(f"{c} — **{len(command_lines(c))} commands**" for c in CATEGORIES) +
                  f"\n\nℹ️ Use `{PREFIX}help <query>` to search commands.\n\n👨‍💻 **Developer**\nMade by **EVIL SAAD**")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🏠 Home", style=discord.ButtonStyle.primary, row=1)
    async def home_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=home_embed(), view=self)

    @discord.ui.button(label="🎟️ Plans", style=discord.ButtonStyle.success, row=1)
    async def plans_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=plans_embed(), view=self)

def home_embed():
    e = embed("🏠 EVM Bot V1 — Home",
      "Welcome to **EVM Bot V1** — Premium LXD/KVM VPS Deploy Platform\n"
      "**Made by EVIL SAAD**\n\n"
      "⚙️ **Total Commands**: 40+\n"
      "👥 **Invite-based VPS claims**\n"
      "🖥️ **VPS requests**: tracked in database\n"
      "🐋 **Nodes**: provider integration configurable\n\n"
      f"Use `{PREFIX}help` to browse commands or choose a category below.")
    e.set_thumbnail(url="https://cdn.discordapp.com/embed/avatars/5.png")
    return e

def plans_embed():
    e = embed("🎟️ Select a plan...", "Choose a plan that matches your invite count.")
    for name, ram, cpu, disk, required in plans():
        e.add_field(name=f"✨ {name} Plan — {required} Invites",
                    value=f"↳ {ram} GB DDR4 ECC RAM · {cpu} vCore CPU @ 2.099 GHz · {disk} GB NVMe",
                    inline=False)
    e.set_footer(text=f"{BOT_NAME} • Use {PREFIX}claim after reaching the invite requirement")
    return e

@bot.event
async def on_ready():
    init_db()
    if not getattr(bot, "_expiry_worker_started", False):
        bot._expiry_worker_started = True
        async def expiry_worker():
            from datetime import datetime, timezone
            while not bot.is_closed():
                try:
                    now = datetime.now(timezone.utc).isoformat()
                    expired = db.execute("SELECT c.id,d.instance_name FROM claims c JOIN vps_details d ON d.claim_id=c.id WHERE c.expires_at IS NOT NULL AND c.expires_at<=? AND c.status NOT IN ('expired','deleted','failed')", (now,)).fetchall()
                    for item in expired:
                        if item["instance_name"]:
                            await asyncio.to_thread(run_lxc, ["stop", item["instance_name"]], 60)
                        db.execute("UPDATE claims SET status='expired' WHERE id=?", (item["id"],))
                    db.commit()
                except Exception as exc:
                    print(f"Expiry worker warning: {exc}")
                await asyncio.sleep(600)
        bot.loop.create_task(expiry_worker())
    print(f"Logged in as {bot.user} | prefix={PREFIX}")

@bot.event
async def on_invite_create(invite):
    db.execute("INSERT OR IGNORE INTO invite_uses(guild_id,code,inviter_id,uses) VALUES(?,?,?,?)",
               (invite.guild.id, invite.code, invite.inviter.id if invite.inviter else None, invite.uses or 0))
    db.commit()

@bot.event
async def on_member_join(member):
    try:
        current = await member.guild.invites()
        for inv in current:
            row = db.execute("SELECT uses,inviter_id FROM invite_uses WHERE guild_id=? AND code=?",
                             (member.guild.id, inv.code)).fetchone()
            previous = int(row["uses"]) if row else 0
            if (inv.uses or 0) > previous:
                inviter_id = inv.inviter.id if inv.inviter else (row["inviter_id"] if row else None)
                if inviter_id and inviter_id != member.id:
                    add_invites(member.guild.id, inviter_id, 1)
                db.execute("""INSERT INTO invite_uses(guild_id,code,inviter_id,uses) VALUES(?,?,?,?)
                    ON CONFLICT(guild_id,code) DO UPDATE SET inviter_id=excluded.inviter_id,uses=excluded.uses""",
                    (member.guild.id, inv.code, inviter_id, inv.uses or 0))
                db.commit()
                break
    except (discord.Forbidden, discord.HTTPException):
        pass

@bot.command(name="home")
async def home(ctx):
    """Open the bot's home panel."""
    e = home_embed()
    e.description = (
        "Welcome to **EVM Bot V1** — VPS management panel.\\n"
        "Use the buttons below to browse Home and Plans.\\n\\n"
        "🏠 **Home:** `$home`\\n"
        "📚 **Commands:** `$help`\\n"
        "🖥️ **Your VPS:** `$vps`\\n"
        "🎟️ **Plans:** `$plans`\\n"
        "⚡ **Manage your VPS:** `$manage`"
    )
    await ctx.send(embed=e, view=HomeView())

@bot.command(name="help")
async def help_command(ctx, *, query=None):
    """Show command help, optionally search for a command."""
    if query:
        matches = [(cat, item) for cat in CATEGORIES for item in command_lines(cat)
                   if query.lower() in item[0].lower() or query.lower() in item[1].lower()]
        # Also search command names directly, including admin command groups.
        if not matches:
            matches = [(cat, item) for cat in CATEGORIES for item in command_lines(cat)
                       if query.lower() in item[0].lower() or query.lower() in item[1].lower()]
        if not matches:
            return await ctx.reply(f"No command found for `{query}`. Try `$help` to see all categories.")
        e = embed(f"🔎 Help Search: {query}", "\\n".join(f"`{cmd}` — {desc}" for _, (cmd, desc) in matches[:25]))
        return await ctx.reply(embed=e)
    e = embed("📚 EVM Bot V1 — Help",
        "**Main commands**\\n"
        "`$home` — Open home panel\\n"
        "`$help` — Show this help menu\\n"
        "`$plans` — View VPS plans\\n"
        "`$vps` — View your VPS / claim status\\n"
        "`$claim` — Request a VPS claim\\n"
        "`$manage` — Open VPS management panel\\n"
        "`$ping` — Check bot latency\\n"
        "`$faq` — Frequently asked questions\\n\\n"
        "**Admin commands**\\n"
        "`$vpsadmin help` — VPS admin commands\\n"
        "`$setprefix $` — Set command prefix to `$`\\n"
        "`$setplan` — Configure a plan (admin)\\n"
        "`$status` — Bot status\\n\\n"
        "**Search help:** `$help <keyword>`\\n"
        "**Browse categories:** use the buttons below.")
    await ctx.send(embed=e, view=HomeView())

@bot.command()
async def quickhelp(ctx):
    e = embed("📑 Quick Reference", "\n".join(f"{c} — **{len(command_lines(c))} commands**" for c in CATEGORIES) +
              f"\n\nUse `{PREFIX}help <query>` to search.")
    await ctx.send(embed=e, view=HomeView())

@bot.command()
async def ping(ctx):
    await ctx.reply(embed=embed("🏓 Pong!", f"Latency: **{round(bot.latency*1000)} ms**"))

@bot.command()
async def about(ctx):
    await ctx.reply(embed=embed("ℹ️ EVM Bot V1", f"Made by **EVIL SAAD**\nPrefix: `{PREFIX}`\nPython Discord bot with invite tracking and VPS claim requests."))

@bot.command()
async def developer(ctx):
    owner = f"<@{OWNER_ID}>" if OWNER_ID else "EVIL SAAD"
    await ctx.reply(embed=embed("👨‍💻 Developer", owner))

@bot.command()
async def info(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.reply(embed=embed("👤 User Info", f"User: {member.mention}\nID: `{member.id}`\nInvites: **{invite_count(ctx.guild.id, member.id)}**"))

@bot.command()
async def invites(ctx, member: discord.Member = None, *, mode=None):
    if mode == "top":
        rows = db.execute("SELECT user_id,count FROM invites WHERE guild_id=? ORDER BY count DESC LIMIT 10", (ctx.guild.id,)).fetchall()
        text = "\n".join(f"**{i}.** <@{r['user_id']}> — **{r['count']}**" for i, r in enumerate(rows, 1)) or "No invite data yet."
        return await ctx.reply(embed=embed("🎟️ Invite Leaderboard", text))
    member = member or ctx.author
    await ctx.reply(embed=embed("🎟️ Invite Count", f"{member.mention} has **{invite_count(ctx.guild.id, member.id)}** tracked invites."))

@bot.command()
async def invited(ctx):
    await ctx.reply("Invitee history is not available for past joins; tracking starts after the bot is installed and has Manage Server permission.")

@bot.command()
async def inviter(ctx, member: discord.Member):
    await ctx.reply("Inviter lookup is available only for joins tracked after the bot was installed.")

@bot.command(name="plans")
@commands.guild_only()
async def plans_command(ctx):
    await ctx.reply(embed=plans_embed())

bot.remove_command("help")


def run_lxc(args, timeout=180):
    """Run a fixed-argument LXD command without a shell."""
    import subprocess, shutil
    if not shutil.which("lxc"):
        return None, "The `lxc` command is not installed."
    try:
        result = subprocess.run(["lxc", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, "LXD command timed out."
    except OSError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout or "LXD command failed")[-1200:]
    return result.stdout, None

async def deploy_lxd_and_publish(guild_id, user_id, claim_id, selected_plan, progress_queue=None):
    """Provision an Ubuntu LXD container and publish SSH/game ports on different host ports."""
    if not ENABLE_LXD_PROVISIONING:
        return False, "LXD deployment is disabled. Configure LXD first, then set ENABLE_LXD_PROVISIONING=true in .env.", None
    if PORT_RANGE_START < 1024 or PORT_RANGE_END <= PORT_RANGE_START or PORT_RANGE_END > 65535:
        return False, "Invalid PORT_RANGE_START/PORT_RANGE_END in .env.", None

    import json, shutil, time, subprocess, threading, queue
    loop = asyncio.get_running_loop()
    def report(percent, stage):
        if progress_queue is not None:
            loop.call_soon_threadsafe(progress_queue.put_nowait, (percent, stage))
    report(3, "Validating configuration")
    if not shutil.which("ssh"):
        return False, "OpenSSH client is missing on the host. Install it with: apt-get install -y openssh-client", None
    if not shutil.which("lxc"):
        return False, "LXD is not installed (lxc command missing).", None

    name, ram, cpu, disk, _required = selected_plan
    instance = f"evm-{guild_id}-{user_id}-{claim_id}"
    # Read SQLite on the event-loop thread; the connection is not shared across threads.
    used = {int(r["public_port"]) for r in db.execute(
        "SELECT public_port FROM port_mappings WHERE public_port BETWEEN ? AND ?",
        (PORT_RANGE_START, PORT_RANGE_END)
    ).fetchall()}
    free_ports = [p for p in range(PORT_RANGE_START, PORT_RANGE_END + 1) if p not in used]
    def worker():
        free = free_ports
        report(8, "Checking available ports")
        if len(free) < 2:
            return False, "Not enough free ports remain in the configured range.", None

        existing, _ = run_lxc(["info", instance], timeout=20)
        if existing is not None:
            return False, "A container with this name already exists.", None

        report(15, "Creating Ubuntu 24.04 container")
        _, err = run_lxc(["launch", LXD_IMAGE, instance,
                          "--config", f"limits.memory={ram}GB",
                          "--config", f"limits.cpu={cpu}"], timeout=240)
        if err:
            return False, f"Container launch failed: {err}", None

        report(35, "Applying CPU, RAM and disk limits")
        _, err = run_lxc(["config", "device", "override", instance, "root", f"size={disk}GiB"], timeout=30)
        if err:
            run_lxc(["delete", "--force", instance], timeout=60)
            return False, f"Could not enforce {disk} GiB root disk quota; container removed. {err}", None

        report(45, "Waiting for container network")
        container_ip = None
        for _ in range(12):
            out, _ = run_lxc(["list", instance, "--format", "json"], timeout=20)
            if out:
                try:
                    rows = json.loads(out)
                    for nic in rows[0].get("state", {}).get("network", {}).values():
                        for addr in nic.get("addresses", []):
                            if addr.get("family") == "inet" and not addr.get("address", "").startswith("127."):
                                container_ip = addr["address"]
                                break
                        if container_ip:
                            break
                except Exception:
                    pass
            if container_ip:
                break
            time.sleep(2)
        if not container_ip:
            run_lxc(["delete", "--force", instance], timeout=60)
            return False, "Container got no IPv4 address; check LXD networking. Container removed.", None

        # Configure SSH and a dedicated login user inside the fresh Ubuntu container.
        report(58, "Installing SSH and generating login password")
        username = "root"
        password = "".join(secrets.choice(string.ascii_letters + string.digits + "!@#%+-_") for _ in range(18))
        setup_cmd = (
            "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && "
            "apt-get install -y openssh-server sudo curl ca-certificates && "
            f"echo '{username}:{password}' | chpasswd && "
            "mkdir -p /run/sshd /etc/ssh/sshd_config.d && "
            "printf '%s\\n' 'PasswordAuthentication yes' 'KbdInteractiveAuthentication yes' "
            "'PubkeyAuthentication yes' 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/99-evm-password-auth.conf && "
            "sshd -t && "
            "(systemctl enable ssh >/dev/null 2>&1 && systemctl restart ssh || service ssh restart) && "
            "ss -lnt | grep -q ':22 '"
        )
        _, err = run_lxc(["exec", instance, "--", "bash", "-lc", setup_cmd], timeout=600)
        if err:
            run_lxc(["delete", "--force", instance], timeout=60)
            return False, f"Ubuntu SSH/user setup failed: {err}", None

        report(78, "Opening local SSH proxy and starting Pinggy TCP tunnel")
        candidates = list(free)
        secrets.SystemRandom().shuffle(candidates)
        local_port = None
        last_proxy_error = "No available local port."
        for candidate in candidates:
            _, proxy_err = run_lxc([
                "config", "device", "add", instance, "evm-ssh", "proxy",
                f"listen=tcp:127.0.0.1:{candidate}",
                f"connect=tcp:{container_ip}:22", "bind=host"
            ], timeout=30)
            if not proxy_err:
                local_port = candidate
                break
            last_proxy_error = proxy_err
        if local_port is None:
            run_lxc(["delete", "--force", instance], timeout=60)
            return False, f"Could not create local SSH proxy; container removed. {last_proxy_error}", None

        # Pinggy allocates a public hostname and TCP port; no public IPv4 is required.
        try:
            proc = subprocess.Popen(
                ["ssh", "-p", str(PINGGY_PORT), "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null", "-o", "ExitOnForwardFailure=yes",
                 "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                 "-R", f"0:localhost:{local_port}", f"tcp@{PINGGY_HOST}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            output_lines = queue.Queue()
            def read_pinggy_output():
                for line in proc.stdout:
                    output_lines.put(line.rstrip())
            threading.Thread(target=read_pinggy_output, daemon=True).start()
            tunnel_host = None
            tunnel_port = None
            deadline = time.time() + 45
            collected = []
            endpoint_pattern = re.compile(r"([A-Za-z0-9.-]+\.a\.free\.pinggy\.online|[A-Za-z0-9.-]+\.pinggy\.online|[A-Za-z0-9.-]+):([0-9]{2,5})")
            while time.time() < deadline and proc.poll() is None:
                try:
                    line = output_lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                collected.append(line)
                match = endpoint_pattern.search(line)
                if match and "pinggy" in match.group(1).lower():
                    tunnel_host, tunnel_port = match.group(1), int(match.group(2))
                    break
            if not tunnel_host:
                proc.terminate()
                run_lxc(["delete", "--force", instance], timeout=60)
                details = "\n".join(collected[-8:]) or "Pinggy did not return a TCP hostname/port."
                return False, f"Could not establish Pinggy tunnel; container removed. Check outbound SSH access and Pinggy availability. {details}", None
        except Exception as exc:
            run_lxc(["delete", "--force", instance], timeout=60)
            return False, f"Could not start Pinggy tunnel; container removed: {exc}", None

        report(94, "Finalizing deployment")
        # Keep the tunnel alive in the host process; public_ip field now stores hostname.
        PINGGY_PROCESSES[claim_id] = proc
        return True, "Container and Pinggy SSH tunnel configured.", {
            "name": instance, "ip": container_ip, "public_ip": tunnel_host,
            "ssh_port": tunnel_port, "local_ssh_port": local_port, "game_port": None,
            "username": username, "password": password
        }
    return await asyncio.to_thread(worker)

class PrefixPlanSelect(discord.ui.Select):
    def __init__(self, eligible, owner_id):
        self.owner_id = owner_id
        options = [
            discord.SelectOption(label=f"{name} Plan — {required} Invites", value=name,
                                 description=f"{ram} GB RAM • {cpu} vCore • {disk} GB NVMe",
                                 emoji="✨")
            for name, ram, cpu, disk, required in eligible[:25]
        ]
        super().__init__(placeholder="Select a plan...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This menu belongs to another user.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        selected = next((p for p in plans() if p[0].lower() == self.values[0].lower()), None)
        if not selected:
            return await interaction.response.send_message("Plan not found. Run the prefix create command again.", ephemeral=True)
        claim_id, remaining_invites = reserve_plan_claim(
            interaction.guild.id, interaction.user.id, selected[0], selected[4])
        if claim_id is None:
            current = invite_count(interaction.guild.id, interaction.user.id)
            return await interaction.response.send_message(
                f"❌ {selected[0]} requires {selected[4]} invites; you have {current}.", ephemeral=True)
        await interaction.response.edit_message(
            embed=embed("⏳ VPS Deployment Started",
                        f"Plan: **{selected[0]}**\nRAM: **{selected[1]} GB** · CPU: **{selected[2]} vCore** · Disk: **{selected[3]} GB**\nClaim ID: `#{claim_id}`\nStatus: **Provisioning**"),
            view=None)
        progress_queue = asyncio.Queue()
        deploy_task = asyncio.create_task(deploy_lxd_and_publish(
            interaction.guild.id, interaction.user.id, claim_id, selected, progress_queue))
        progress = 0
        stage = "Starting deployment"
        def progress_bar(value, width=18):
            filled = max(0, min(width, round(width * value / 100)))
            return "🟦" * filled + "⬛" * (width - filled)
        # Update the original deployment message from real milestones emitted by the LXD worker.
        while not deploy_task.done():
            try:
                new_progress, new_stage = await asyncio.wait_for(progress_queue.get(), timeout=1.5)
                progress = max(progress, min(94, int(new_progress)))
                stage = new_stage
            except asyncio.TimeoutError:
                pass
            try:
                await interaction.edit_original_response(embed=embed(
                    "🚀 VPS Deployment In Progress",
                    f"Plan: **{selected[0]}**\nRAM: **{selected[1]} GB** · CPU: **{selected[2]} vCore** · Disk: **{selected[3]} GB**\n"
                    f"Claim ID: `#{claim_id}`\n\n`{progress_bar(progress)}` **{progress}%**\n"
                    f"🔄 **{stage}…**\n\nPlease wait; credentials will be sent by DM after successful deployment.", color=0x3B82F6), view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        ok, message, details = await deploy_task
        final_progress = 100 if ok else progress
        final_title = "✅ VPS Deployment Complete" if ok else "❌ VPS Deployment Failed"
        final_desc = (f"Claim ID: `#{claim_id}`\n`{progress_bar(final_progress)}` **{final_progress}%**\n\n" +
                      ("Deployment completed successfully! Check your DMs for credentials." if ok else f"{message}"))
        try:
            await interaction.edit_original_response(embed=embed(final_title, final_desc,
                color=0x22C55E if ok else 0xEF4444), view=None)
        except (discord.HTTPException, discord.NotFound):
            pass
        if ok:
            db.execute("UPDATE claims SET status='provisioned' WHERE id=?", (claim_id,))
            db.execute("INSERT INTO vps(guild_id,user_id,name,status) VALUES(?,?,?,'running')",
                       (interaction.guild.id, interaction.user.id, details["name"]))
            db.execute("INSERT OR REPLACE INTO vps_details(claim_id,guild_id,user_id,instance_name,container_ip,public_ip,ssh_port,game_port,username,password) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (claim_id, interaction.guild.id, interaction.user.id, details["name"], details["ip"], details["public_ip"], details["ssh_port"], details["game_port"], details["username"], details["password"]))
            db.commit()
            result = embed("✅ VPS Container Deployed",
                f"Plan: **{selected[0]}**\nContainer: `{details['name']}`\nContainer IPv4: `{details['ip']}`\n\n"
                f"🔐 SSH: `{details['public_ip']}:{details['ssh_port']}` → `22/tcp`\n"
                "Pinggy tunnel publishes SSH only. Free tunnel endpoints can change after reconnecting or expire.",
                color=0x22C55E)
        else:
            db.execute("UPDATE claims SET status='failed' WHERE id=?", (claim_id,))
            db.commit()
            refund_plan_invites(interaction.guild.id, interaction.user.id, selected[4])
            result = embed("❌ VPS Deployment Failed — Invites Refunded",
                f"Claim ID: `#{claim_id}`\nReason: {message}\n\n"
                f"Your **{selected[4]} invite credits** have been refunded because deployment failed.", color=0xEF4444)
        await interaction.followup.send(embed=result, ephemeral=True)
        if ok:
            try:
                login = embed(
                    f"👤 SSH — {details['name']}",
                    "Your SSH credentials and connection details:",
                    color=0x7C3AED
                )
                login.add_field(name="🌐 Pinggy Hostname", value=f"`{details['public_ip']}`", inline=True)
                login.add_field(name="🔌 SSH Port", value=f"`{details['ssh_port']}`", inline=True)
                login.add_field(name="👤 User", value=f"`{details['username']}`", inline=True)
                login.add_field(name="📱 Termux SSH Command", value=f"`ssh {details['username']}@{details['public_ip']} -p {details['ssh_port']}`", inline=False)
                login.add_field(name="🔑 Password", value=f"||`{details['password']}`||", inline=False)
                login.add_field(name="🧩 Container IPv4", value=f"`{details['ip']}` (private network)", inline=False)
                login.add_field(name="🖥️ OS", value="Ubuntu 24.04", inline=True)
                login.add_field(name="🆔 Claim ID", value=f"`#{claim_id}`", inline=True)
                login.description += "\nKeep these credentials private."
                await interaction.user.send(embed=login)
            except discord.Forbidden:
                pass

class PrefixPlanView(discord.ui.View):
    def __init__(self, eligible, owner_id):
        super().__init__(timeout=180)
        self.add_item(PrefixPlanSelect(eligible, owner_id))

@bot.command()
@commands.guild_only()
async def claim(ctx, plan_name=None):
    user_invites = invite_count(ctx.guild.id, ctx.author.id)
    available = plans()
    if not plan_name:
        eligible = [p for p in available if user_invites >= p[4]]
        if not eligible:
            return await ctx.reply(embed=embed(
                "❌ Not enough invites",
                f"You have **{user_invites}** tracked invites.\n\n" +
                "\n".join(f"✨ **{n}** — requires {req} invites" for n, _, _, _, req in available)))
        menu = embed("▶️ Deploy New VPS — Select Plan",
            "Choose a plan for your new server. Select one below.\n\n" +
            "\n".join(f"✨ **{n} Plan — {req} Invites**\n↳ {ram} GB DDR4 ECC RAM\n↳ {cpu} vCore CPU\n↳ {disk} GB NVMe"
                      for n, ram, cpu, disk, req in eligible) +
            f"\n\nMenu times out in 3 minutes.\n{BOT_NAME} • {BRAND}")
        return await ctx.reply(embed=menu, view=PrefixPlanView(eligible, ctx.author.id))

    selected = next((p for p in available if p[0].lower() == plan_name.lower()), None)
    if not selected:
        return await ctx.reply("Plan not found. Use the plans command.")
    claim_id, remaining_invites = reserve_plan_claim(
        ctx.guild.id, ctx.author.id, selected[0], selected[4])
    if claim_id is None:
        current = invite_count(ctx.guild.id, ctx.author.id)
        return await ctx.reply(f"❌ `{selected[0]}` requires {selected[4]} invites; you have {current}.")
    await ctx.reply(embed=embed("⏳ VPS Deployment Started", f"Plan: **{selected[0]}**\nClaim ID: `#{claim_id}`\nStatus: **Provisioning**"))
    ok, message, details = await deploy_lxd_and_publish(ctx.guild.id, ctx.author.id, claim_id, selected)
    if ok:
        db.execute("UPDATE claims SET status='provisioned' WHERE id=?", (claim_id,))
        db.execute("INSERT INTO vps(guild_id,user_id,name,status) VALUES(?,?,?,'running')",
                   (ctx.guild.id, ctx.author.id, details["name"]))
        db.execute("INSERT OR REPLACE INTO vps_details(claim_id,guild_id,user_id,instance_name,container_ip,public_ip,ssh_port,game_port,username,password) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (claim_id, ctx.guild.id, ctx.author.id, details["name"], details["ip"], details["public_ip"], details["ssh_port"], details["game_port"], details["username"], details["password"]))
        db.commit()
        # Send credentials only by DM, never in the public server channel.
        try:
            dm = embed("🔐 Your VPS Login Details",
                f"Claim ID: `#{claim_id}`\nPlan: **{selected[0]}**\nContainer: `{details['name']}`\n"
                f"Container IPv4: `{details['ip']}`\nSSH endpoint: `{details['public_ip']}:{details['ssh_port']}`\n"
                f"Username: `{details['username']}`\nPassword: ||`{details['password']}`||", color=0x22C55E)
            await ctx.author.send(embed=dm)
        except discord.Forbidden:
            await ctx.send(f"⚠️ I couldn't DM you. Enable server-member DMs, then contact an admin. Your credentials are saved; use `{PREFIX}manage {claim_id}` in DMs/server to retrieve them privately.")
        await ctx.send(embed=embed("✅ VPS Container Deployed",
            f"Plan: **{selected[0]}**\nContainer: `{details['name']}`\nContainer IPv4: `{details['ip']}`\n"
            f"🔐 SSH: `{details['public_ip']}:{details['ssh_port']}` → `22/tcp`\n"
            "Pinggy tunnel publishes SSH only. Free tunnel endpoints can change after reconnecting or expire."))
    else:
        db.execute("UPDATE claims SET status='failed' WHERE id=?", (claim_id,))
        db.commit()
        refund_plan_invites(ctx.guild.id, ctx.author.id, selected[4])
        await ctx.send(embed=embed("❌ VPS Deployment Failed — Invites Refunded",
            f"Claim ID: `#{claim_id}`\nReason: {message}\n\n"
            f"Your **{selected[4]} invite credits** have been refunded because deployment failed.", color=0xEF4444))

@bot.command(name="vpsadmin")
@commands.guild_only()
async def vpsadmin(ctx, action=None, *, args: str = ""):
    """Admin VPS lifecycle: create for a member, delete, suspend, unsuspend, set expiry."""
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    action = (action or "help").lower()
    tokens = args.split()
    claim_id = 0
    member = None
    plan_name = None
    days = 30
    if action == "create":
        if tokens:
            match = re.match(r"<@!?(\\d+)>", tokens[0])
            if match:
                member = ctx.guild.get_member(int(match.group(1)))
            if member is None and tokens[0].isdigit():
                member = ctx.guild.get_member(int(tokens[0]))
            if len(tokens) > 1:
                plan_name = tokens[1]
            if len(tokens) > 2 and tokens[2].isdigit():
                days = int(tokens[2])
    elif action in ("delete", "suspend", "unsuspend", "expiry"):
        if tokens and tokens[0].isdigit():
            claim_id = int(tokens[0])
        if action == "expiry" and len(tokens) > 1 and tokens[1].isdigit():
            days = int(tokens[1])
    if action in ("help", "commands"):
        return await ctx.reply(embed=embed("🛠️ Admin VPS Commands",
            f"`{PREFIX}vpsadmin create @member PLAN [days]` — create VPS for anyone, bypass invites\n"
            f"`{PREFIX}vpsadmin delete <claim_id>` — delete VPS and data\n"
            f"`{PREFIX}vpsadmin suspend <claim_id>` — stop/suspend VPS\n"
            f"`{PREFIX}vpsadmin unsuspend <claim_id>` — start VPS again\n"
            f"`{PREFIX}vpsadmin expiry <claim_id> <days>` — set expiry from now\n"
            f"`{PREFIX}vpsadmin expiry <claim_id> 0` — remove expiry"))
    if action == "create":
        # Syntax is: vpsadmin create @member PLAN [days]
        if member is None or not plan_name:
            return await ctx.reply(f"Usage: `{PREFIX}vpsadmin create @member PLAN [days]`")
        selected = next((p for p in plans() if p[0].lower() == plan_name.lower()), None)
        if not selected:
            return await ctx.reply("Plan not found. Check plans command.")
        new_id = db.execute("INSERT INTO claims(guild_id,user_id,plan,status) VALUES(?,?,?,'provisioning')",
                             (ctx.guild.id, member.id, selected[0])).lastrowid
        db.commit()
        await ctx.reply(f"⏳ Creating **{selected[0]}** VPS for {member.mention} (claim `#{new_id}`)…")
        ok, message, details = await deploy_lxd_and_publish(ctx.guild.id, member.id, new_id, selected)
        if not ok:
            db.execute("UPDATE claims SET status='failed' WHERE id=?", (new_id,)); db.commit()
            return await ctx.send(embed=embed("❌ Admin VPS Creation Failed", f"Claim `#{new_id}`\n{message}", 0xEF4444))
        from datetime import datetime, timedelta, timezone
        expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days > 0 else None
        db.execute("UPDATE claims SET status='provisioned', expires_at=? WHERE id=?", (expiry, new_id))
        db.execute("INSERT INTO vps(guild_id,user_id,name,status) VALUES(?,?,?,'running')",
                   (ctx.guild.id, member.id, details["name"]))
        db.execute("""INSERT OR REPLACE INTO vps_details
            (claim_id,guild_id,user_id,instance_name,container_ip,public_ip,ssh_port,game_port,username,password)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (new_id, ctx.guild.id, member.id, details["name"], details["ip"], details["public_ip"],
             details["ssh_port"], details.get("game_port"), details["username"], details["password"]))
        if details.get("local_ssh_port"):
            db.execute("INSERT INTO port_mappings(guild_id,user_id,claim_id,instance_name,public_port,container_port,protocol) VALUES(?,?,?,?,?,?,?)",
                       (ctx.guild.id, member.id, new_id, details["name"], details["local_ssh_port"], 22, "tcp"))
        db.commit()
        try:
            exp_text = f"\nExpiry: <t:{int(datetime.fromisoformat(expiry).timestamp())}:F>" if expiry else "\nExpiry: Never"
            await member.send(embed=embed("🔐 Your Admin-Created VPS",
                f"Claim: `#{new_id}`\nPlan: **{selected[0]}**\n"
                f"Termux: `ssh {details['username']}@{details['public_ip']} -p {details['ssh_port']}`\n"
                f"Username: `{details['username']}`\nPassword: ||`{details['password']}`||" + exp_text))
        except discord.Forbidden:
            await ctx.send(f"⚠️ Could not DM {member.mention}; ask them to enable server DMs.")
        return await ctx.send(f"✅ VPS `#{new_id}` created for {member.mention}.")
    row = db.execute("SELECT c.*,d.instance_name FROM claims c LEFT JOIN vps_details d ON d.claim_id=c.id WHERE c.id=? AND c.guild_id=?",
                     (claim_id, ctx.guild.id)).fetchone()
    if action in ("delete", "suspend", "unsuspend", "expiry") and not row:
        return await ctx.reply("❌ Claim/VPS not found in this server.")
    if action == "delete":
        name = row["instance_name"]
        if name:
            proc = PINGGY_PROCESSES.pop(claim_id, None)
            if proc and proc.poll() is None:
                proc.terminate()
            _, err = await asyncio.to_thread(run_lxc, ["delete", "--force", name], 120)
            if err and "not found" not in err.lower():
                return await ctx.reply(f"❌ Could not delete container: {err}")
        db.execute("DELETE FROM port_mappings WHERE claim_id=?", (claim_id,))
        db.execute("DELETE FROM vps_details WHERE claim_id=?", (claim_id,))
        db.execute("UPDATE claims SET status='deleted', expires_at=NULL WHERE id=?", (claim_id,))
        db.commit()
        return await ctx.reply(f"🗑️ VPS `#{claim_id}` deleted and tunnel stopped.")
    if action in ("suspend", "unsuspend"):
        name = row["instance_name"]
        if not name:
            return await ctx.reply("❌ No container is linked to this claim.")
        cmd = "stop" if action == "suspend" else "start"
        _, err = await asyncio.to_thread(run_lxc, [cmd, name], 90)
        if err:
            return await ctx.reply(f"❌ LXD {cmd} failed: {err}")
        status = "suspended" if action == "suspend" else "provisioned"
        db.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id)); db.commit()
        return await ctx.reply(f"✅ VPS `#{claim_id}` {'suspended/stopped' if action == 'suspend' else 'unsuspended/started'}.")
    if action == "expiry":
        if days < 0:
            return await ctx.reply("Days must be 0 or greater.")
        if days == 0:
            db.execute("UPDATE claims SET expires_at=NULL WHERE id=?", (claim_id,)); db.commit()
            return await ctx.reply(f"✅ Expiry removed for VPS `#{claim_id}`.")
        from datetime import datetime, timedelta, timezone
        expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        db.execute("UPDATE claims SET expires_at=? WHERE id=?", (expiry, claim_id)); db.commit()
        return await ctx.reply(f"✅ VPS `#{claim_id}` expires <t:{int(datetime.fromisoformat(expiry).timestamp())}:F>.")
    return await ctx.reply(f"Unknown action. Use `{PREFIX}vpsadmin help`.")

@bot.command()
@commands.guild_only()
async def vps(ctx, action=None, *, argument=None):
    if action == "create":
        return await claim(ctx)
    if action in (None, "info"):
        rows = db.execute("SELECT id,plan,status,created_at,expires_at FROM claims WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                          (ctx.guild.id, ctx.author.id)).fetchall()
        text = "\n".join(f"`#{r['id']}` — **{r['plan']}** — {r['status']} — expiry: {r['expires_at'] or 'never'}" for r in rows) or "No VPS claims registered."
        return await ctx.reply(embed=embed("🖥️ Your VPS / Claims", text))
    if action == "delete" and is_admin(ctx) and argument and argument.isdigit():
        db.execute("UPDATE claims SET status='cancelled' WHERE id=? AND guild_id=?", (int(argument), ctx.guild.id))
        db.commit()
        return await ctx.reply("Claim record marked cancelled. This does not delete a real VPS.")
    await ctx.reply("This VPS action needs a configured LXD/KVM provider backend. Supported record actions: `vps info`, `vps create`, and admin `vps delete <claim_id>`.")




async def start_sshx_session(user, claim_id: int, name: str):
    """Start SSHX as root inside the user's container and DM the private URL."""
    warning = (
        "⚠️ This is a privileged, shareable terminal link. Anyone who has it may control "
        "this VPS as root. Keep it private and stop the session when finished."
    )
    # LXD exec runs as root by default. Install to root's home without sudo or interactive prompts.
    script = r"""set -eu
export HOME=/root
export PATH="/root/.local/bin:/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
command -v curl >/dev/null 2>&1 || {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl ca-certificates
}
mkdir -p /root/.local/bin
if ! command -v sshx >/dev/null 2>&1 && [ ! -x /root/.local/bin/sshx ]; then
  curl -fsSL https://sshx.io/get | sh
fi
SSHX_BIN="$(command -v sshx 2>/dev/null || true)"
if [ -z "$SSHX_BIN" ] || [ ! -x "$SSHX_BIN" ]; then
  for candidate in /root/.local/bin/sshx /usr/local/bin/sshx /root/.cargo/bin/sshx; do
    if [ -x "$candidate" ]; then SSHX_BIN="$candidate"; break; fi
  done
fi
[ -n "$SSHX_BIN" ] && [ -x "$SSHX_BIN" ] || { echo "SSHX_BINARY_NOT_FOUND: installer did not create an executable"; exit 23; }
pkill -x sshx 2>/dev/null || true
rm -f /root/evm-sshx.log
nohup "$SSHX_BIN" > /root/evm-sshx.log 2>&1 < /dev/null &
sleep 8
cat /root/evm-sshx.log 2>/dev/null || true
"""
    out, err = await asyncio.to_thread(run_lxc, ["exec", name, "--", "bash", "-lc", script], 150)
    combined = (out or "") + "\n" + (err or "")
    match = re.search(r"https?://sshx\.io/(?:s/)?[^\s`<>\"']+", combined)
    if err or out is None:
        return False, embed("❌ SSHX failed to start", (err or out or "No output from container")[-1200:], 0xEF4444)
    if not match:
        log, logerr = await asyncio.to_thread(
            run_lxc,
            ["exec", name, "--", "bash", "-lc",
             "sleep 2; cat /root/evm-sshx.log 2>/dev/null || true"],
            15
        )
        combined += "\n" + (log or "") + "\n" + (logerr or "")
        match = re.search(r"https?://sshx\.io/(?:s/)?[^\s`<>\"']+", combined)
    if not match:
        return False, embed(
            "⚠️ SSHX link not detected",
            f"SSHX ran, but no session URL was found. Log:\\n```text\\n{combined[-900:]}\\n```\\n"
            "Check outbound internet/DNS from the container and SSHX installer output.",
            0xF59E0B
        )
    url = match.group(0).rstrip(".,)")
    try:
        await user.send(embed=embed(
            f"🖥️ SSHX Terminal — VPS #{claim_id}",
            f"**Browser terminal:** {url}\\n\\n{warning}\\nRuns as `root` in `/root`.",
            0x7C3AED
        ))
        return True, embed("✅ SSHX started", "The private root terminal link has been sent to your DM.", 0x22C55E)
    except discord.Forbidden:
        return False, embed(
            "❌ Could not DM SSHX link",
            "Enable DMs from server members and click SSHX again. The terminal may be running; stop it when no longer needed.",
            0xEF4444
        )

class ConfirmReinstallView(discord.ui.View):
    def __init__(self, claim_id: int, owner_id: int, guild_id: int):
        super().__init__(timeout=60)
        self.claim_id, self.owner_id, self.guild_id = claim_id, owner_id, guild_id

    @discord.ui.button(label="Confirm Reinstall", emoji="♻️", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id and not (OWNER_ID and interaction.user.id == OWNER_ID):
            return await interaction.response.send_message("❌ These controls belong to another user.", ephemeral=True)
        row = db.execute("""SELECT c.id,c.guild_id,c.user_id,c.plan,c.status,d.* FROM claims c
            LEFT JOIN vps_details d ON d.claim_id=c.id WHERE c.id=? AND c.guild_id=?""",
            (self.claim_id, self.guild_id)).fetchone()
        if not row or not row["instance_name"]:
            return await interaction.response.send_message("❌ VPS/container not found.", ephemeral=True)
        plan = next((p for p in plans() if p[0].lower() == (row["plan"] or "").lower()), None)
        if not plan:
            return await interaction.response.send_message("❌ Plan configuration not found.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = row["instance_name"]
        _, err = await asyncio.to_thread(run_lxc, ["delete", "--force", name], 120)
        if err:
            return await interaction.followup.send(embed=embed("❌ Reinstall failed", err, 0xEF4444), ephemeral=True)
        db.execute("DELETE FROM port_mappings WHERE claim_id=?", (self.claim_id,))
        db.commit()
        ok, message, details = await deploy_lxd_and_publish(self.guild_id, interaction.user.id, self.claim_id, plan)
        if not ok:
            db.execute("UPDATE claims SET status='failed' WHERE id=?", (self.claim_id,))
            db.commit()
            return await interaction.followup.send(embed=embed("❌ Reinstall failed", message, 0xEF4444), ephemeral=True)
        db.execute("UPDATE claims SET status='provisioned' WHERE id=?", (self.claim_id,))
        db.execute("INSERT OR REPLACE INTO vps_details(claim_id,guild_id,user_id,instance_name,container_ip,public_ip,ssh_port,game_port,username,password) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.claim_id,self.guild_id,interaction.user.id,details['name'],details['ip'],details['public_ip'],details['ssh_port'],details['game_port'],details['username'],details['password']))
        db.commit()
        try:
            await interaction.user.send(embed=embed("🔐 VPS Reinstalled — New Credentials",
                f"SSH: `{details['public_ip']}:{details['ssh_port']}`\\nUsername: `{details['username']}`\\nPassword: ||`{details['password']}`||\\nContainer IPv4: `{details['ip']}`\\nPrevious container data was erased.", 0x22C55E))
        except discord.Forbidden:
            pass
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send("✅ Reinstall completed. New credentials were DM'd if DMs are open.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id and not (OWNER_ID and interaction.user.id == OWNER_ID):
            return await interaction.response.send_message("❌ These controls belong to another user.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Reinstall cancelled.", view=self)


class ManageVPSView(discord.ui.View):
    """Owner-bound interactive controls for a deployed VPS."""
    def __init__(self, claim_id: int, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.claim_id = claim_id
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and not (
            OWNER_ID and interaction.user.id == OWNER_ID
        ):
            await interaction.response.send_message("❌ These VPS controls belong to another user.", ephemeral=True)
            return False
        return True

    def _row(self):
        return db.execute(
            """SELECT c.id,c.guild_id,c.user_id,c.plan,c.status,d.* FROM claims c
               LEFT JOIN vps_details d ON d.claim_id=c.id
               WHERE c.id=? AND c.guild_id=?""",
            (self.claim_id, self.guild_id),
        ).fetchone()

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "stop")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "restart")

    async def _power(self, interaction: discord.Interaction, action: str):
        row = self._row()
        if not row or not row["instance_name"]:
            return await interaction.response.send_message("❌ VPS record/container not found.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, err = await asyncio.to_thread(run_lxc, [action, row["instance_name"]], 120)
        if err:
            return await interaction.followup.send(embed=embed(f"❌ {action.title()} failed", err, 0xEF4444), ephemeral=True)
        status = "stopped" if action == "stop" else "running"
        db.execute("UPDATE claims SET status=? WHERE id=?", (status, self.claim_id))
        db.execute("UPDATE vps SET status=? WHERE name=?", (status, row["instance_name"]))
        db.commit()
        await interaction.followup.send(embed=embed(f"✅ VPS {action.title()} successful",
            f"VPS `#{self.claim_id}` is now **{status}**.", 0x22C55E), ephemeral=True)

    @discord.ui.button(label="Reinstall", emoji="♻️", style=discord.ButtonStyle.danger, row=1)
    async def reinstall_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"⚠️ Reinstalling VPS `#{self.claim_id}` permanently deletes its files and settings. Confirm only if you want to erase it.",
            view=ConfirmReinstallView(self.claim_id, self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="SSHX", emoji="🌐", style=discord.ButtonStyle.primary, row=1)
    async def sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = self._row()
        if not row or not row["instance_name"]:
            return await interaction.response.send_message("❌ VPS/container not found.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await start_sshx_session(interaction.user, self.claim_id, row["instance_name"])
        await interaction.followup.send(embed=message, ephemeral=True)

    @discord.ui.button(label="Credentials", emoji="🔐", style=discord.ButtonStyle.secondary, row=1)
    async def credentials_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = self._row()
        if not row or not row["instance_name"]:
            return await interaction.response.send_message("❌ VPS credentials not found.", ephemeral=True)
        try:
            details = embed(f"🔐 VPS #{self.claim_id} — Credentials",
                f"**Pinggy Host:** `{row['public_ip']}`\n**SSH Port:** `{row['ssh_port']}`\n"
                f"**Username:** `{row['username']}`\n"
                f"**Password:** ||`{row['password']}`||\n\nKeep this message private.", 0x22C55E)
            await interaction.user.send(embed=details)
            await interaction.response.send_message("📩 Credentials sent to your DM.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I couldn't DM you. Enable DMs and try again.", ephemeral=True)

    @discord.ui.button(label="Refresh Info", emoji="🔃", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = self._row()
        if not row:
            return await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
        e = embed(f"🖥️ VPS #{self.claim_id} — Manage",
            f"**Plan:** {row['plan']}\n**Status:** {row['status']}\n"
            f"**Pinggy Host:** `{row['public_ip']}`\n**SSH Port:** `{row['ssh_port']}`\n"
            f"**Username:** `{row['username']}`\n"
            f"**OS:** Ubuntu 24.04", 0x7C3AED)
        await interaction.response.edit_message(embed=e, view=self)

class ManageVPSSelect(discord.ui.Select):
    def __init__(self, rows, owner_id: int, guild_id: int):
        self.owner_id = owner_id
        self.guild_id = guild_id
        options = []
        for r in rows[:25]:
            label = f"VPS #{r['id']} — {r['plan']}"
            desc = (r["status"] or "unknown")[:100]
            options.append(discord.SelectOption(label=label[:100], value=str(r["id"]), description=desc))
        super().__init__(placeholder="Select a VPS to manage...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id and not (OWNER_ID and interaction.user.id == OWNER_ID):
            return await interaction.response.send_message("❌ This menu belongs to another user.", ephemeral=True)
        claim_id = int(self.values[0])
        row = db.execute("""SELECT c.id,c.guild_id,c.user_id,c.plan,c.status,d.* FROM claims c
            LEFT JOIN vps_details d ON d.claim_id=c.id WHERE c.id=? AND c.guild_id=?""",
            (claim_id, self.guild_id)).fetchone()
        if not row or row["user_id"] != interaction.user.id and not (OWNER_ID and interaction.user.id == OWNER_ID):
            return await interaction.response.send_message("❌ VPS not found or not yours.", ephemeral=True)
        e = embed(f"🖥️ VPS #{claim_id} — Manage",
            f"**Plan:** {row['plan']}\n**Status:** {row['status']}\n**Container:** `{row['instance_name'] or 'not deployed'}`\n"
            f"**IP:** `{row['public_ip'] or 'not assigned'}`\n**SSH Port:** `{row['ssh_port'] or '—'}`\n"
            f"**Username:** `{row['username'] or '—'}`\n"
            f"**OS:** Ubuntu 24.04", 0x7C3AED)
        await interaction.response.edit_message(embed=e, view=ManageVPSView(claim_id, interaction.user.id, self.guild_id))

class ManageVPSListView(discord.ui.View):
    def __init__(self, rows, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.add_item(ManageVPSSelect(rows, owner_id, guild_id))

@bot.command(name="manage")
@commands.guild_only()
async def manage(ctx, claim_id: int = 0, action: str = "info", *, confirm: str = ""):
    """Manage a user's own deployed LXD container. Destructive reinstall requires explicit confirmation."""
    if claim_id <= 0:
        rows = db.execute("SELECT c.id,c.plan,c.status,d.instance_name,d.public_ip,d.ssh_port FROM claims c LEFT JOIN vps_details d ON d.claim_id=c.id WHERE c.guild_id=? AND c.user_id=? ORDER BY c.id DESC LIMIT 15",
                          (ctx.guild.id, ctx.author.id)).fetchall()
        if not rows:
            return await ctx.reply("No deployed VPS found. Create one first with `" + PREFIX + "vps create`.")
        body = "\n".join(f"`#{r['id']}` **{r['plan']}** — {r['status']} — `{r['instance_name'] or 'not deployed'}`" for r in rows)
        return await ctx.reply(embed=embed("🖥️ Manage Your VPS", body + "\n\nSelect a VPS below to open its management buttons."),
                               view=ManageVPSListView(rows, ctx.author.id, ctx.guild.id))
    row = db.execute("SELECT c.id,c.guild_id,c.user_id,c.plan,c.status,d.* FROM claims c LEFT JOIN vps_details d ON d.claim_id=c.id WHERE c.id=? AND c.guild_id=?",
                     (claim_id, ctx.guild.id)).fetchone()
    if not row or (row["user_id"] != ctx.author.id and not is_admin(ctx)):
        return await ctx.reply("❌ VPS not found or you don't have permission to manage it.")
    name = row["instance_name"] if "instance_name" in row.keys() else None
    if not name:
        return await ctx.reply("No live container details are saved for this claim.")
    action = (action or "info").lower()
    if action in ("info", "password"):
        details = embed(f"👤 SSH — {name}",
            "Your SSH credentials and VPS connection details:", color=0x7C3AED)
        details.add_field(name="🌐 Pinggy Hostname", value=f"`{row['public_ip']}`", inline=True)
        details.add_field(name="🔌 SSH Port", value=f"`{row['ssh_port']}`", inline=True)
        details.add_field(name="👤 User", value=f"`{row['username']}`", inline=True)
        details.add_field(name="📱 Termux SSH Command", value=f"`ssh {row['username']}@{row['public_ip']} -p {row['ssh_port']}`", inline=False)
        details.add_field(name="🔑 Password", value=f"||`{row['password']}`||", inline=False)
        details.add_field(name="🧩 Container IPv4", value=f"`{row['container_ip']}` (private network)", inline=False)
        details.add_field(name="📦 Plan / Status", value=f"**{row['plan']}** / `{row['status']}`", inline=True)
        details.add_field(name="🆔 Claim ID", value=f"`#{claim_id}`", inline=True)
        details.set_footer(text=f"{BOT_NAME} • {BRAND} • Keep credentials private")
        try:
            await ctx.author.send(embed=details, view=ManageVPSView(claim_id, ctx.author.id, ctx.guild.id))
            if ctx.guild: await ctx.reply("📩 VPS details and management buttons sent to your DM.")
        except discord.Forbidden:
            await ctx.reply("❌ I couldn't DM you. Enable DMs from server members and try again.")
        return
    if action == "sshx":
        await ctx.reply("🔄 Starting SSHX and preparing a private link...")
        ok, message = await start_sshx_session(ctx.author, claim_id, name)
        return await ctx.reply(embed=message)

    if action not in ("start", "stop", "restart", "reinstall"):
        return await ctx.reply(f"Usage: `{PREFIX}manage <id> <info|password|sshx|start|stop|restart|reinstall CONFIRM>`")
    if action == "reinstall" and confirm.strip().upper() != "CONFIRM":
        return await ctx.reply(f"⚠️ Reinstall deletes the container's current data. Confirm with `{PREFIX}manage {claim_id} reinstall CONFIRM`.")
    if action == "reinstall":
        # Reinstall is destructive; release saved port mappings, rebuild the same plan, and issue fresh credentials.
        plan = next((p for p in plans() if p[0].lower() == (row['plan'] or '').lower()), None)
        if not plan:
            return await ctx.reply("Plan config not found; ask an admin to restore it.")
        await ctx.reply("♻️ Reinstall started. This erases the container's data; wait for the result.")
        await asyncio.to_thread(run_lxc, ["delete", "--force", name], 120)
        db.execute("DELETE FROM port_mappings WHERE claim_id=?", (claim_id,)); db.commit()
        ok, message, details = await deploy_lxd_and_publish(ctx.guild.id, ctx.author.id, claim_id, plan)
        if not ok:
            db.execute("UPDATE claims SET status='failed' WHERE id=?", (claim_id,)); db.commit()
            return await ctx.send(embed=embed("❌ Reinstall failed", message, color=0xEF4444))
        db.execute("UPDATE claims SET status='provisioned' WHERE id=?", (claim_id,))
        db.execute("UPDATE vps SET name=?,status='running' WHERE guild_id=? AND user_id=?", (details['name'],ctx.guild.id,ctx.author.id))
        db.execute("INSERT OR REPLACE INTO vps_details(claim_id,guild_id,user_id,instance_name,container_ip,public_ip,ssh_port,game_port,username,password) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (claim_id,ctx.guild.id,ctx.author.id,details['name'],details['ip'],details['public_ip'],details['ssh_port'],details['game_port'],details['username'],details['password']))
        db.commit()
        try:
            await ctx.author.send(embed=embed("🔐 VPS Reinstalled — New Credentials", f"Pinggy SSH: `{details['public_ip']}:{details['ssh_port']}`\nTermux: `ssh {details['username']}@{details['public_ip']} -p {details['ssh_port']}`\nUsername: `{details['username']}`\nPassword: ||`{details['password']}`||\nContainer IPv4: `{details['ip']}`\nYour previous container data was erased."))
        except discord.Forbidden: pass
        return await ctx.send("✅ Reinstall completed. Credentials were sent by DM if your DMs are open.")
    _, err = await asyncio.to_thread(run_lxc, [{"start":"start","stop":"stop","restart":"restart"}[action], name], 120)
    if err:
        return await ctx.reply(embed=embed("❌ VPS action failed", err, color=0xEF4444))
    db.execute("UPDATE claims SET status=? WHERE id=?", ("running" if action in ("start","restart") else "stopped", claim_id)); db.commit()
    await ctx.reply(f"✅ VPS `#{claim_id}` {action} command completed.")

@bot.command()
@commands.guild_only()
async def addinvite(ctx, member: discord.Member, amount: int = 1):
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    if amount < 1 or amount > 10000:
        return await ctx.reply("Amount must be between 1 and 10000.")
    add_invites(ctx.guild.id, member.id, amount)
    await ctx.reply(embed=embed("🎟️ Invite Credits Added", f"Added **{amount}** invite credit(s) to {member.mention}.\nNew total: **{invite_count(ctx.guild.id, member.id)}**"))

@bot.command()
async def setprefix(ctx, new_prefix=None):
    global PREFIX
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    if not new_prefix or len(new_prefix) > 3 or any(ch.isspace() for ch in new_prefix):
        return await ctx.reply(f"Usage: `{PREFIX}setprefix <prefix>` (1–3 non-space characters)")
    PREFIX = new_prefix
    set_setting("prefix", PREFIX)
    await ctx.reply(f"✅ Prefix changed to `{PREFIX}` and saved to the database.")

@bot.command()
async def setplan(ctx, name=None, ram: int = 0, cpu: int = 0, disk: int = 0, required: int = 0):
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    if not name or min(ram, cpu, disk, required) <= 0 or len(name) > 12:
        return await ctx.reply(f"Usage: `{PREFIX}setplan <name> <ram_GB> <cpu_cores> <disk_GB> <required_invites>`")
    set_setting(f"plan_{name.upper()}", f"{ram},{cpu},{disk},{required}")
    await ctx.reply(embed=embed("✅ Plan Saved", f"**{name.upper()}** — {ram} GB RAM · {cpu} CPU · {disk} GB disk · {required} invites"))

@bot.command()
async def delplan(ctx, name=None):
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    if not name:
        return await ctx.reply(f"Usage: `{PREFIX}delplan <name>`")
    db.execute("DELETE FROM settings WHERE key=?", (f"plan_{name.upper()}",))
    db.commit()
    await ctx.reply(f"Plan `{name.upper()}` removed if it existed.")

@bot.command()
async def status(ctx):
    claims = db.execute("SELECT COUNT(*) AS n FROM claims WHERE guild_id=?", (ctx.guild.id,)).fetchone()["n"] if ctx.guild else 0
    await ctx.reply(embed=embed("📈 Status", f"Bot: 🟢 Online\nDatabase: 🟢 Connected\nTracked claims: **{claims}**\nProvider backend: **Not configured**"))

@bot.command()
async def dashboard(ctx):
    await ctx.reply(embed=home_embed(), view=HomeView())

@bot.command()
async def live(ctx):
    await status(ctx)

@bot.command()
async def top(ctx):
    await invites(ctx, mode="top")

@bot.command()
async def graph(ctx):
    await ctx.reply(embed=embed("📉 Resource Graph", "Live CPU/RAM graphs require a configured node monitoring backend."))

@bot.command()
async def serverstats(ctx):
    row = db.execute("SELECT COUNT(*) AS n FROM claims WHERE guild_id=?", (ctx.guild.id,)).fetchone()
    await ctx.reply(embed=embed("📊 Server Statistics", f"Registered claims: **{row['n']}**\nInvite credits tracked: **{db.execute('SELECT COALESCE(SUM(count),0) AS n FROM invites WHERE guild_id=?',(ctx.guild.id,)).fetchone()['n']}**"))

@bot.command(name="vps-stats")
async def vps_stats(ctx):
    await serverstats(ctx)

@bot.command()
async def vpsusage(ctx):
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    rows = db.execute("SELECT plan,status,COUNT(*) AS n FROM claims WHERE guild_id=? GROUP BY plan,status", (ctx.guild.id,)).fetchall()
    await ctx.reply(embed=embed("📊 VPS Usage", "\n".join(f"{r['plan']} / {r['status']}: {r['n']}" for r in rows) or "No claims yet."))

@bot.command()
async def faq(ctx):
    await ctx.reply(embed=embed("❔ FAQ", "Invite counts track joins observed after setup. Admins can create, delete, suspend, unsuspend, and set expiry for VPS with vpsadmin commands."))

@bot.command()
async def ssh(ctx):
    await ctx.reply(embed=embed("🔐 SSH", "SSH credential delivery is not enabled until a VPS provider backend is configured. Never share root passwords publicly."))

@bot.command()
async def console(ctx):
    await ctx.reply("Direct LXD console is not configured in this starter bot.")

@bot.command()
async def web(ctx):
    await ctx.reply("Private web terminal is not configured in this starter bot.")

@bot.command()
async def password(ctx):
    await ctx.reply("Password rotation requires a configured VPS provider backend.")

@bot.command()
async def pin(ctx):
    await ctx.reply("Web terminal PIN feature requires a configured web terminal backend.")

@bot.command()
async def upgrade(ctx):
    await ctx.reply(f"Use `{PREFIX}claim <plan>` after meeting the invite requirement; upgrades need provider integration.")

@bot.command()
async def changeos(ctx):
    await ctx.reply("OS reinstall requires a configured provider backend and would erase VPS data.")

@bot.command()
async def share(ctx):
    await ctx.reply("VPS sharing requires a configured provider backend.")

@bot.command()
async def unshare(ctx):
    await ctx.reply("VPS sharing requires a configured provider backend.")

@bot.command()
@commands.guild_only()
async def ports(ctx, action=None, *args):
    if action in (None, "list"):
        rows = db.execute("SELECT id,user_id,claim_id,public_port,container_port,protocol,instance_name FROM port_mappings WHERE guild_id=? ORDER BY public_port",
                          (ctx.guild.id,)).fetchall()
        desc = "\n".join(
            f"`#{r['id']}` • <@{r['user_id']}> • `Pinggy tunnel (SSH-only)` → `{r['instance_name']}:{r['container_port']}/{r['protocol']}` (claim `#{r['claim_id']}`)"
            for r in rows) or "No port mappings saved yet."
        return await ctx.reply(embed=embed("🔢 Shared IPv4 Port Mappings", desc))
    if action == "remove":
        if not is_admin(ctx):
            return await ctx.reply("❌ Administrator permission required.")
        if not args or not str(args[0]).isdigit():
            return await ctx.reply(f"Usage: `{PREFIX}ports remove <mapping_id>`")
        mapping_id = int(args[0])
        row = db.execute("SELECT * FROM port_mappings WHERE id=? AND guild_id=?", (mapping_id, ctx.guild.id)).fetchone()
        if not row:
            return await ctx.reply("Port mapping not found.")
        device = "evm-ssh" if int(row["container_port"]) == 22 else ("evm-game" if int(row["container_port"]) == 25565 else None)
        if device:
            _, err = await asyncio.to_thread(run_lxc, ["config", "device", "remove", row["instance_name"], device], 30)
            if err and "not found" not in err.lower():
                return await ctx.reply(f"Could not remove live proxy device: {err}")
        db.execute("DELETE FROM port_mappings WHERE id=?", (mapping_id,))
        db.commit()
        return await ctx.reply(f"✅ Removed port mapping `#{mapping_id}`.")
    if action == "add":
        return await ctx.reply(f"Ports are allocated automatically by `{PREFIX}vps create`. Each container receives separate public ports for SSH (22/tcp) and game (25565/tcp).")
    await ctx.reply(f"Usage: `{PREFIX}ports list`, `{PREFIX}ports add`, `{PREFIX}ports remove <mapping_id>`")
@bot.command()
async def node(ctx):
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    await ctx.reply(embed=embed("🐋 Nodes", "No node provider configured. Connect an authorized LXD/KVM API before enabling node management."))

@bot.command()
async def invite(ctx):
    app = await bot.application_info()
    url = f"https://discord.com/oauth2/authorize?client_id={app.id}&permissions=8&scope=bot%20applications.commands"
    await ctx.reply(f"🤖 Add {BOT_NAME}: {url}")

@bot.command()
async def claimstatus(ctx, claim_id: int = 0, *, new_status=None):
    if not is_admin(ctx):
        return await ctx.reply("❌ Administrator permission required.")
    allowed = {"pending", "approved", "rejected", "provisioned", "cancelled"}
    if not claim_id or new_status not in allowed:
        return await ctx.reply(f"Usage: `{PREFIX}claimstatus <claim_id> <pending|approved|rejected|provisioned|cancelled>`")
    cur = db.execute("UPDATE claims SET status=? WHERE id=? AND guild_id=?", (new_status, claim_id, ctx.guild.id))
    db.commit()
    await ctx.reply("✅ Claim status updated." if cur.rowcount else "Claim ID not found.")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply(f"Missing argument: `{error.param.name}`. Use `{PREFIX}help`.")
    if isinstance(error, commands.BadArgument):
        return await ctx.reply("Invalid argument. Check the command format with the help menu.")
    if isinstance(error, commands.CommandInvokeError):
        print("Command error:", repr(error.original))
        return await ctx.reply("⚠️ Something went wrong. Check the bot console logs.")
    print("Command error:", repr(error))

if __name__ == "__main__":
    init_db()
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.")
    bot.run(TOKEN)
