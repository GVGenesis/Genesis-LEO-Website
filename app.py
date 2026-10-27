"""Website + CAD API. Starts the main Discord bot in the same process."""
import asyncio, os, secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
import db

ROOT = os.path.dirname(__file__)
API = "https://discord.com/api/v10"
LEO = {int(x) for x in os.getenv("LEO_ROLE_IDS", "").split(",") if x.strip()}
CID, SECRET, REDIR = os.getenv("DISCORD_CLIENT_ID"), os.getenv("DISCORD_CLIENT_SECRET"), os.getenv("OAUTH_REDIRECT_URI")
SEV = ("Low", "Medium", "High", "Felony")

@asynccontextmanager
async def lifespan(app):
    await db.init()
    task = None
    if os.getenv("PUBLIC_SERVICE_BOT_TOKEN"):  # role checker runs inside this service
        import public_service_bot as ps
        task = asyncio.create_task(ps.bot.start(os.environ["PUBLIC_SERVICE_BOT_TOKEN"]))
    yield
    if task: await ps.bot.close()
    await db.pool.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_hex(16)),
                   same_site="lax", https_only=bool(os.getenv("RENDER")), max_age=60 * 60 * 12)

async def leo(r: Request):
    """Dependency for every CAD route: signed in AND still holding a required role."""
    u = r.session.get("user")
    if not u: raise HTTPException(401, "Sign in with Discord to use the CAD.")
    if not await db.has_role(int(u["id"]), LEO):
        r.session.clear(); raise HTTPException(403, "Your account no longer has a required role.")
    return u

async def target(uid: str) -> dict:
    if not uid.isdigit(): raise HTTPException(400, "Invalid user ID.")
    u = await db.get_user(int(uid))
    if not u: raise HTTPException(404, "No profile for that user. They need to join the Discord server first.")
    return u

class CitationIn(BaseModel):
    user_id: str
    offense: str = Field(min_length=2, max_length=200)
    fine: int = Field(ge=0, le=10_000_000)
class WarrantIn(BaseModel):
    user_id: str
    reason: str = Field(min_length=2, max_length=300)
    bail: int = Field(ge=0, le=10_000_000)
    severity: str
class LicenseIn(BaseModel):
    user_id: str
    status: str
    note: str = Field("", max_length=300)

# ---- pages ----
page = lambda f: FileResponse(os.path.join(ROOT, "static", f))
@app.get("/")
def home(): return page("index.html")
@app.get("/penal-codes")
def penal(): return page("penal.html")
@app.get("/cad")
def cad(): return page("cad.html")
@app.get("/health")
def health(): return {"ok": True}

# ---- Discord login (LEO CAD only) ----
@app.get("/login")
def login(r: Request):
    r.session["state"] = state = secrets.token_urlsafe(16)
    q = urlencode(dict(client_id=CID, redirect_uri=REDIR, response_type="code", scope="identify", state=state))
    return RedirectResponse(f"{API}/oauth2/authorize?{q}")

@app.get("/callback")
async def callback(r: Request, code: str = "", state: str = ""):
    if not code or state != r.session.pop("state", None): return RedirectResponse("/cad?error=state")
    async with httpx.AsyncClient(timeout=10) as h:
        t = (await h.post(f"{API}/oauth2/token", data=dict(client_id=CID, client_secret=SECRET, grant_type="authorization_code", code=code, redirect_uri=REDIR))).json()
        if "access_token" not in t: return RedirectResponse("/cad?error=oauth")
        u = (await h.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {t['access_token']}"})).json()
    if not await db.has_role(int(u["id"]), LEO): return RedirectResponse("/cad?error=roles")
    av = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png" if u.get("avatar") else None
    name = u.get("global_name") or u["username"]
    await db.get_or_create_user(int(u["id"]), u["username"], av)
    r.session["user"] = {"id": u["id"], "name": name, "avatar": av}
    return RedirectResponse("/cad")

@app.get("/logout")
def logout(r: Request):
    r.session.clear(); return RedirectResponse("/")

# ---- API ----
@app.get("/api/me")
async def me(u=Depends(leo)): return u

@app.get("/api/penal-codes")
async def codes(): return await db.penal_codes()

@app.get("/api/cad/search")
async def search(q: str, u=Depends(leo)): return await db.search_users(q) if q.strip() else []

@app.get("/api/cad/profile/{uid}")
async def prof(uid: str, u=Depends(leo)):
    await target(uid); return await db.profile(int(uid))

@app.get("/api/cad/feed")
async def feed(u=Depends(leo)): return await db.feed()

@app.post("/api/cad/citations")
async def new_citation(b: CitationIn, u=Depends(leo)):
    await target(b.user_id); return await db.add_citation(int(b.user_id), u["name"], b.offense, b.fine, "web")

@app.post("/api/cad/warrants")
async def new_warrant(b: WarrantIn, u=Depends(leo)):
    if b.severity not in SEV: raise HTTPException(400, "Severity must be Low, Medium, High or Felony.")
    await target(b.user_id); return await db.add_warrant(int(b.user_id), u["name"], b.reason, b.bail, b.severity, "web")

@app.post("/api/cad/warrants/{wid}/clear")
async def clear(wid: int, u=Depends(leo)):
    if not await db.clear_warrant(wid): raise HTTPException(404, "No active warrant with that ID.")
    return {"ok": True}

@app.post("/api/cad/license")
async def lic(b: LicenseIn, u=Depends(leo)):
    if b.status not in ("valid", "suspended"): raise HTTPException(400, "Status must be valid or suspended.")
    await target(b.user_id); await db.set_license(int(b.user_id), b.status, b.note); return {"ok": True}

app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
