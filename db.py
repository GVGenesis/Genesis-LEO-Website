"""Shared database layer. The website, the main bot and the Public Service bot all use this file."""
import json, os, pathlib
import asyncpg

pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  discord_id BIGINT PRIMARY KEY, username TEXT NOT NULL, avatar_url TEXT,
  bank_balance BIGINT NOT NULL DEFAULT 0, cash_balance BIGINT NOT NULL DEFAULT 0,
  job TEXT NOT NULL DEFAULT 'Unemployed', is_wanted BOOLEAN NOT NULL DEFAULT FALSE,
  license_status TEXT NOT NULL DEFAULT 'valid', license_note TEXT);
CREATE TABLE IF NOT EXISTS citations(
  citation_id SERIAL PRIMARY KEY, user_discord_id BIGINT NOT NULL REFERENCES users(discord_id),
  officer_name TEXT NOT NULL, offense TEXT NOT NULL, fine_amount BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid','paid')),
  source TEXT NOT NULL DEFAULT 'discord', timestamp TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS warrants(
  warrant_id SERIAL PRIMARY KEY, user_discord_id BIGINT NOT NULL REFERENCES users(discord_id),
  officer_name TEXT NOT NULL, reason TEXT NOT NULL, bail_amount BIGINT NOT NULL DEFAULT 0,
  severity TEXT NOT NULL CHECK (severity IN ('Low','Medium','High','Felony')),
  active BOOLEAN NOT NULL DEFAULT TRUE, source TEXT NOT NULL DEFAULT 'discord',
  timestamp TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS penal_codes(
  code TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'General',
  fine BIGINT NOT NULL DEFAULT 0, jail_minutes INT NOT NULL DEFAULT 0, severity TEXT NOT NULL DEFAULT 'Low');
CREATE TABLE IF NOT EXISTS member_roles(
  discord_id BIGINT PRIMARY KEY, role_ids BIGINT[] NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""

def row(r):
    """Record -> dict. Discord IDs become strings so browsers don't round big numbers."""
    return None if r is None else {k: (str(v) if k.endswith("discord_id") else v) for k, v in r.items()}

async def init():
    global pool
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        p = pathlib.Path(__file__).parent / "data" / "penal_codes.json"
        if p.exists():
            for x in json.loads(p.read_text()):
                await c.execute(
                    """INSERT INTO penal_codes(code,title,category,fine,jail_minutes,severity) VALUES($1,$2,$3,$4,$5,$6)
                       ON CONFLICT (code) DO UPDATE SET title=EXCLUDED.title, category=EXCLUDED.category,
                       fine=EXCLUDED.fine, jail_minutes=EXCLUDED.jail_minutes, severity=EXCLUDED.severity""",
                    str(x["code"]), x["title"], x.get("category", "General"), int(x.get("fine", 0)),
                    int(x.get("jail_minutes", 0)), x.get("severity", "Low"))

# ---- profiles ----
async def get_or_create_user(uid: int, username: str, avatar: str | None = None):
    return row(await pool.fetchrow(
        """INSERT INTO users(discord_id,username,avatar_url) VALUES($1,$2,$3)
           ON CONFLICT (discord_id) DO UPDATE SET username=EXCLUDED.username,
           avatar_url=COALESCE(EXCLUDED.avatar_url, users.avatar_url) RETURNING *""", uid, username, avatar))

async def get_user(uid: int):
    return row(await pool.fetchrow("SELECT * FROM users WHERE discord_id=$1", uid))

async def search_users(q: str):
    q = q.strip()
    rs = await pool.fetch("SELECT * FROM users WHERE username ILIKE $1 OR discord_id::text=$2 ORDER BY username LIMIT 15", f"%{q}%", q)
    return [row(r) for r in rs]

async def profile(uid: int):
    u = await get_user(uid)
    if not u: return None
    u["citations"] = [row(r) for r in await pool.fetch("SELECT * FROM citations WHERE user_discord_id=$1 ORDER BY timestamp DESC LIMIT 50", uid)]
    u["warrants"] = [row(r) for r in await pool.fetch("SELECT * FROM warrants WHERE user_discord_id=$1 ORDER BY timestamp DESC LIMIT 50", uid)]
    return u

# ---- economy ----
async def deposit(uid: int, amt: int):
    return row(await pool.fetchrow("UPDATE users SET cash_balance=cash_balance-$2, bank_balance=bank_balance+$2 WHERE discord_id=$1 AND cash_balance>=$2 RETURNING *", uid, amt))

async def withdraw(uid: int, amt: int):
    return row(await pool.fetchrow("UPDATE users SET bank_balance=bank_balance-$2, cash_balance=cash_balance+$2 WHERE discord_id=$1 AND bank_balance>=$2 RETURNING *", uid, amt))

async def add_cash(uid: int, amt: int):
    return row(await pool.fetchrow("UPDATE users SET cash_balance=cash_balance+$2 WHERE discord_id=$1 RETURNING *", uid, amt))

async def set_job(uid: int, job: str):
    await pool.execute("UPDATE users SET job=$2 WHERE discord_id=$1", uid, job)

# ---- law enforcement ----
async def _wanted(c, uid):
    await c.execute("UPDATE users SET is_wanted=EXISTS(SELECT 1 FROM warrants WHERE user_discord_id=$1 AND active) WHERE discord_id=$1", uid)

async def add_citation(uid, officer, offense, fine, source="discord"):
    return row(await pool.fetchrow("INSERT INTO citations(user_discord_id,officer_name,offense,fine_amount,source) VALUES($1,$2,$3,$4,$5) RETURNING *", uid, officer, offense, fine, source))

async def add_warrant(uid, officer, reason, bail, severity, source="discord"):
    async with pool.acquire() as c, c.transaction():
        r = await c.fetchrow("INSERT INTO warrants(user_discord_id,officer_name,reason,bail_amount,severity,source) VALUES($1,$2,$3,$4,$5,$6) RETURNING *", uid, officer, reason, bail, severity, source)
        await _wanted(c, uid)
    return row(r)

async def clear_warrant(wid: int):
    async with pool.acquire() as c, c.transaction():
        uid = await c.fetchval("UPDATE warrants SET active=FALSE WHERE warrant_id=$1 AND active RETURNING user_discord_id", wid)
        if uid: await _wanted(c, uid)
    return uid

async def set_license(uid: int, status: str, note: str):
    await pool.execute("UPDATE users SET license_status=$2, license_note=$3 WHERE discord_id=$1", uid, status, note or None)

async def unpaid(uid: int):
    return [row(r) for r in await pool.fetch("SELECT * FROM citations WHERE user_discord_id=$1 AND status='unpaid' ORDER BY citation_id", uid)]

async def pay_citation(uid: int, cid: int):
    """Pays from bank first, then cash. Returns (ok, message)."""
    async with pool.acquire() as c, c.transaction():
        ct = await c.fetchrow("SELECT * FROM citations WHERE citation_id=$1 AND user_discord_id=$2 FOR UPDATE", cid, uid)
        if not ct: return False, "That citation isn't yours or doesn't exist."
        if ct["status"] == "paid": return False, "That citation is already paid."
        u = await c.fetchrow("SELECT * FROM users WHERE discord_id=$1 FOR UPDATE", uid)
        fine = ct["fine_amount"]
        if u["bank_balance"] + u["cash_balance"] < fine: return False, f"You need ${fine:,} but only have ${u['bank_balance'] + u['cash_balance']:,}."
        from_bank = min(u["bank_balance"], fine)
        await c.execute("UPDATE users SET bank_balance=bank_balance-$2, cash_balance=cash_balance-$3 WHERE discord_id=$1", uid, from_bank, fine - from_bank)
        await c.execute("UPDATE citations SET status='paid' WHERE citation_id=$1", cid)
        return True, f"Paid ${fine:,} for citation #{cid}."

async def feed(limit=30):
    rs = await pool.fetch(
        """SELECT * FROM (
             SELECT 'citation' AS kind, c.citation_id AS ref, u.username, c.officer_name, c.offense AS detail, c.fine_amount AS amount, c.source, c.timestamp
             FROM citations c JOIN users u ON u.discord_id=c.user_discord_id
             UNION ALL
             SELECT 'warrant', w.warrant_id, u.username, w.officer_name, w.severity || ': ' || w.reason, w.bail_amount, w.source, w.timestamp
             FROM warrants w JOIN users u ON u.discord_id=w.user_discord_id) t
           ORDER BY timestamp DESC LIMIT $1""", limit)
    return [row(r) for r in rs]

async def penal_codes():
    return [row(r) for r in await pool.fetch("SELECT * FROM penal_codes ORDER BY category, code")]

# ---- roles (written by the Public Service bot, read by the website) ----
async def set_roles(uid: int, roles: list[int]):
    await pool.execute(
        "INSERT INTO member_roles(discord_id,role_ids) VALUES($1,$2) ON CONFLICT (discord_id) DO UPDATE SET role_ids=EXCLUDED.role_ids, updated_at=now()", uid, roles)

async def has_role(uid: int, allowed: set[int]) -> bool:
    if not allowed: return False
    return bool(await pool.fetchval("SELECT role_ids && $2::bigint[] FROM member_roles WHERE discord_id=$1", uid, list(allowed)))
