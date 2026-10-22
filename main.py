#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, subprocess, importlib, asyncio, json, base64, re, secrets, string, time, random
from datetime import datetime
import traceback

# ================= AUTO-INSTALL =================
def ensure(pkg, name=None):
    name = name or pkg
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"📦 Installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])

ensure("aiohttp")
ensure("python-telegram-bot", "telegram")
ensure("aiograpi")
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from aiograpi import Client as IGClient
from aiograpi.exceptions import (
    TwoFactorRequired, ChallengeRequired, BadPassword, UserNotFound,
    RateLimitError, PleaseWaitFewMinutes
)
# ================================================

# ================= HARDCODED =================
GITHUB_PAT       = "REDACTED"
MAIN_BOT_TOKEN   = "REDACTED"
OWNER_ID         = 8437109558

GITHUB_USER      = "opdjdnfnfnfnfng"
REPO_NAME        = "dota-bot-qk8ohqha"

# ================= LIMITS =================
MAX_TG_PER_USER  = 2
MAX_IG_PER_USER  = 4
MAX_GC_PER_TOKEN = 10
MAX_IG_PER_PROXY = 2

PULL_INTERVAL    = 300
PROXY_FETCH_INT  = 30
PROXY_CHECK_CONC = 400
# ==============================================

CONFIG_FILE = "config.txt"
MAIN_FILE   = "main.py"
GITHUB_API  = "https://api.github.com"

HEADERS = None

# ================= GLOBAL STATE =================
CONFIG = {}
PROXY_POOL = set()
PROXY_ASSIGN = {}           # {proxy: [ig_username]}
PROXY_LOCK = asyncio.Lock()
TG_APPS = {}                # {token: Application}
IG_TASKS = {}               # {ig_username: asyncio.Task}
STOP_FLAG = False
CONV_STATE = {}             # {user_id: {state_data}}

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/Jakee8718/Free-Proxies/main/residential.txt",
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/all.txt",
    "https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/proxies/fresh/data.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]


# ================= CONFIG =================
def default_config():
    return {
        "version": 1,
        "created_at": datetime.now().isoformat(),
        "last_sync": None,
        "tg_tokens": [],
        "ig_ids": [],
        "users": {}
    }


def load_config():
    global CONFIG
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            CONFIG = json.load(f)
    except Exception:
        CONFIG = default_config()
        save_config()


def save_config():
    CONFIG["last_sync"] = datetime.now().isoformat()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2)


def user_stats(uid):
    uid = str(uid)
    if uid not in CONFIG["users"]:
        CONFIG["users"][uid] = {"tg_count": 0, "ig_count": 0}
    return CONFIG["users"][uid]


# ================= GITHUB SYNC =================
async def gh_get(session, path):
    try:
        async with session.get(f"{GITHUB_API}{path}", timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.text()
    except Exception as e:
        return None, str(e)


async def gh_post(session, path, payload):
    try:
        async with session.post(f"{GITHUB_API}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.text()
    except Exception as e:
        return None, str(e)


async def gh_put(session, path, payload):
    try:
        async with session.put(f"{GITHUB_API}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.text()
    except Exception as e:
        return None, str(e)


async def resolve_user(session):
    global GITHUB_USER
    status, body = await gh_get(session, "/user")
    if status == 200:
        GITHUB_USER = json.loads(body).get("login")
        return True
    print(f"❌ PAT invalid ({status})")
    return False


async def repo_exists(session):
    status, _ = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}")
    return status == 200


async def create_repo(session):
    payload = {"name": REPO_NAME, "private": False, "auto_init": False,
               "description": "Dota Bot — auto-managed"}
    status, body = await gh_post(session, "/user/repos", payload)
    if status in (200, 201):
        print(f"✅ Created repo: {GITHUB_USER}/{REPO_NAME}")
        return True
    print(f"❌ Create failed ({status}): {body[:200]}")
    return False


async def pull_file(session, path):
    status, body = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}")
    if status == 200:
        try:
            return base64.b64decode(json.loads(body)["content"]).decode("utf-8")
        except Exception:
            return None
    return None


async def push_file(session, path, content, message="auto update"):
    status, body = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}")
    sha = None
    if status == 200:
        try:
            sha = json.loads(body).get("sha")
        except Exception:
            pass
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": "main"
    }
    if sha:
        payload["sha"] = sha
    status, body = await gh_put(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}", payload)
    if status in (200, 201):
        return True
    print(f"[PUSH] {path} failed {status}: {body[:100]}")
    return False


async def push_config(session):
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return False
    return await push_file(session, CONFIG_FILE, content, "config update")


async def push_main(session):
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return False
    content = re.sub(r'^(GITHUB_PAT\s*=\s*)".*?"', r'\1"REDACTED"', content, flags=re.MULTILINE)
    content = re.sub(r'^(MAIN_BOT_TOKEN\s*=\s*)".*?"', r'\1"REDACTED"', content, flags=re.MULTILINE)
    return await push_file(session, MAIN_FILE, content, "main.py update")


def self_modify(user, repo):
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception:
        return False
    src = re.sub(r'^GITHUB_USER\s*=\s*".*?"', f'GITHUB_USER      = "{user}"',
                 src, count=1, flags=re.MULTILINE)
    src = re.sub(r'^REPO_NAME\s*=\s*".*?"', f'REPO_NAME        = "{repo}"',
                 src, count=1, flags=re.MULTILINE)
    try:
        with open(__file__, "w", encoding="utf-8") as f:
            f.write(src)
        return True
    except Exception:
        return False


async def periodic_pull(session):
    while not STOP_FLAG:
        try:
            await asyncio.sleep(PULL_INTERVAL)
            remote = await pull_file(session, CONFIG_FILE)
            if remote:
                try:
                    rjson = json.loads(remote)
                    lts = CONFIG.get("last_sync") or "1970"
                    rts = rjson.get("last_sync") or "1970"
                    if rts > lts:
                        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                            f.write(remote)
                        load_config()
                        print("[SYNC] ⬇️ Pulled remote config")
                except Exception:
                    pass
        except asyncio.CancelledError:
            return
        except Exception:
            pass


def gen_repo_name():
    return f"dota-bot-{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"


async def gh_bootstrap():
    global HEADERS, GITHUB_USER, REPO_NAME
    HEADERS = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "DotaBot/1.0"
    }
    conn = aiohttp.TCPConnector(ssl=False)
    session = aiohttp.ClientSession(connector=conn, headers=HEADERS)

    try:
        if GITHUB_USER and REPO_NAME:
            print(f"[SYNC] Repo: {GITHUB_USER}/{REPO_NAME}")
            if await repo_exists(session):
                await push_config(session)
                await push_main(session)
            return session

        print("[INIT] Setting up repo...")
        if not await resolve_user(session):
            await session.close()
            return None
        REPO_NAME = gen_repo_name()
        if not await create_repo(session):
            await session.close()
            return None
        self_modify(GITHUB_USER, REPO_NAME)
        await asyncio.sleep(1)
        await push_config(session)
        await push_main(session)
        print(f"[INIT] ✅ https://github.com/{GITHUB_USER}/{REPO_NAME}")
        return session
    except Exception as e:
        print(f"❌ Bootstrap: {type(e).__name__}: {e}")
        await session.close()
        return None


# ================= PROXY MANAGER =================
def parse_proxy(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        line = line.split("://", 1)[1]
    if "@" in line:
        return None
    parts = line.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return f"http://{parts[0]}:{parts[1]}"


async def fetch_source(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                text = await r.text()
                return [p for p in (parse_proxy(l) for l in text.splitlines()) if p]
    except Exception:
        pass
    return []


async def ig_reachable(proxy):
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.get("https://i.instagram.com/",
                             proxy=proxy,
                             timeout=aiohttp.ClientTimeout(total=6)) as r:
                return r.status < 500
    except Exception:
        return False


async def proxy_fetcher():
    """Fetch every 30s, verify IG-reachable, add to pool."""
    while not STOP_FLAG:
        try:
            conn = aiohttp.TCPConnector(ssl=False, limit=200)
            async with aiohttp.ClientSession(connector=conn) as s:
                results = await asyncio.gather(*[fetch_source(s, u) for u in PROXY_SOURCES])
            all_p = set()
            for lst in results:
                all_p.update(lst)

            # Check IG reachability (concurrent, batched)
            sem = asyncio.Semaphore(PROXY_CHECK_CONC)
            async def chk(p):
                async with sem:
                    return p if await ig_reachable(p) else None

            checks = await asyncio.gather(*[chk(p) for p in all_p], return_exceptions=True)
            alive = {p for p in checks if p}

            async with PROXY_LOCK:
                before = len(PROXY_POOL)
                PROXY_POOL.update(alive)
                added = len(PROXY_POOL) - before

            if added > 0:
                print(f"[PROXY] +{added} → pool={len(PROXY_POOL)}")

        except Exception as e:
            print(f"[PROXY] fetch err: {type(e).__name__}")

        await asyncio.sleep(PROXY_FETCH_INT)


async def get_proxy_for_ig(ig_username):
    """Assign proxy with max 2 IG limit. Returns proxy or None."""
    async with PROXY_LOCK:
        # Already assigned?
        for proxy, ig_list in PROXY_ASSIGN.items():
            if ig_username in ig_list and proxy in PROXY_POOL:
                return proxy

        # Find new proxy
        for proxy in PROXY_POOL:
            assigned = PROXY_ASSIGN.get(proxy, [])
            if len(assigned) < MAX_IG_PER_PROXY:
                PROXY_ASSIGN.setdefault(proxy, []).append(ig_username)
                return proxy
    return None


async def release_proxy(ig_username):
    async with PROXY_LOCK:
        for proxy in list(PROXY_ASSIGN.keys()):
            if ig_username in PROXY_ASSIGN[proxy]:
                PROXY_ASSIGN[proxy].remove(ig_username)
                if not PROXY_ASSIGN[proxy]:
                    del PROXY_ASSIGN[proxy]
                return


# ================= IG MANAGER =================
def build_ig_client(ig_entry):
    cl = IGClient()
    if ig_entry.get("device_info"):
        try:
            cl.set_device(ig_entry["device_info"])
        except Exception:
            pass
    return cl


async def ig_login(ig_entry):
    """Login using session first, fallback to password."""
    cl = build_ig_client(ig_entry)
    session_id = ig_entry.get("session_id")

    try:
        if session_id:
            await cl.login_by_sessionid(session_id)
            print(f"[IG] {ig_entry['username']} logged (session)")
            return cl, None
    except Exception as e:
        print(f"[IG] session fail: {type(e).__name__}")

    # Password fallback
    try:
        await cl.login(ig_entry["username"], ig_entry["password"])
        return cl, "need_2fa"
    except TwoFactorRequired:
        return cl, "need_2fa"
    except BadPassword:
        return None, "bad_password"
    except UserNotFound:
        return None, "not_found"
    except ChallengeRequired:
        return None, "challenge"
    except Exception as e:
        return None, f"error:{type(e).__name__}"


async def fetch_gc_list(cl):
    try:
        threads = await cl.direct_threads(amount=20)
        out = []
        for t in threads:
            title = t.thread_title or f"Thread {t.id}"
            out.append({"id": str(t.id), "title": title})
        return out
    except Exception as e:
        print(f"[IG] GC fetch: {e}")
        return []


async def ig_send_loop(ig_entry, bot, owner_id):
    """Main message send loop with anti-ban."""
    username = ig_entry["username"]

    while not STOP_FLAG:
        try:
            cl, err = await ig_login(ig_entry)
            if not cl:
                await bot.send_message(owner_id, f"❌ IG @{username} login fail: {err}")
                return

            if err == "need_2fa":
                await bot.send_message(owner_id, f"🔐 @{username} needs 2FA. Use /cancel and re-add with 2FA.")
                return

            # Save fresh session
            ig_entry["session_id"] = cl.sessionid
            try:
                ig_entry["device_info"] = cl.get_settings()["device_settings"]
            except Exception:
                pass
            save_config()

            break  # login success
        except Exception as e:
            await bot.send_message(owner_id, f"⚠️ @{username}: {e}")
            await asyncio.sleep(60)

    proxy = await get_proxy_for_ig(username)
    if not proxy:
        await bot.send_message(owner_id, f"⚠️ No proxy available for @{username}")
        return

    # Try to set proxy on client
    try:
        cl.set_proxy(proxy)
    except Exception:
        pass

    gc_id = ig_entry["gc_id"]
    text = ig_entry["text"]
    msg_count = 0
    band_min, band_max = 30, 50

    while not STOP_FLAG:
        try:
            num = random.randint(1, 10**10)
            full = f"{num} {text}" if random.choice([True, False]) else f"{text} {num}"

            try:
                await cl.direct_send(full, thread_ids=[gc_id])
                msg_count += 1
                ig_entry["stats"]["msgs_sent"] = msg_count
                save_config()
                print(f"[IG] @{username} msg #{msg_count} via {proxy}")
            except RateLimitError:
                await bot.send_message(owner_id, f"⚠️ @{username} rate limited")
                await asyncio.sleep(120)
            except Exception as e:
                # Proxy might be dead → try migrating
                await bot.send_message(owner_id, f"🔀 @{username} proxy dead ({type(e).__name__}), migrating...")
                await release_proxy(username)
                proxy = await get_proxy_for_ig(username)
                if not proxy:
                    await bot.send_message(owner_id, f"❌ @{username} no proxy — stopping")
                    break
                try:
                    cl.set_proxy(proxy)
                except Exception:
                    pass
                await asyncio.sleep(5)
                continue

            # Random delay
            await asyncio.sleep(random.uniform(band_min, band_max))

            # Rotate band every 2-3 hrs
            if msg_count % 100 == 0:
                band_min = random.uniform(30, 50)
                band_max = band_min + random.uniform(8, 13)

        except asyncio.CancelledError:
            print(f"[IG] @{username} cancelled")
            return
        except Exception as e:
            print(f"[IG] @{username} loop: {e}")
            await asyncio.sleep(30)


# ================= TELEGRAM BOT =================
def is_owner(uid):
    return uid == OWNER_ID


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"💗 <b>Dota Bot</b>\n\n"
        f"Welcome <b>{update.effective_user.first_name}</b>!\n"
        f"Use /menu to see options.",
        parse_mode="HTML"
    )


async def tg_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats = user_stats(uid)

    keyboard = [
        [InlineKeyboardButton("➕ Add TG Token", callback_data="add_tg")],
        [InlineKeyboardButton("📸 Add IG ID", callback_data="add_ig")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats")],
        [InlineKeyboardButton("🧹 Clean All", callback_data="clean")],
    ]

    text = (
        f"🎛 <b>Dota Menu</b>\n\n"
        f"📱 TG Tokens: <b>{stats['tg_count']}/{MAX_TG_PER_USER}</b>\n"
        f"📸 IG IDs: <b>{stats['ig_count']}/{MAX_IG_PER_USER}</b>\n"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def tg_ping(update, context):
    t0 = time.time()
    m = await update.message.reply_text("🏓 ...")
    ms = int((time.time() - t0) * 1000)
    await m.edit_text(f"🏓 Pong! <b>{ms}ms</b>", parse_mode="HTML")


async def tg_myid(update, context):
    await update.message.reply_text(f"🆔 Your ID: <code>{update.effective_user.id}</code>", parse_mode="HTML")


async def tg_cancel(update, context):
    uid = update.effective_user.id
    CONV_STATE.pop(uid, None)
    await update.message.reply_text("🛑 Cancelled. /menu to return.")


async def tg_clean(update, context):
    uid = update.effective_user.id
    uid_s = str(uid)

    # Stop all IG tasks of this user
    for ig in list(CONFIG["ig_ids"]):
        if ig.get("owner_id") == uid:
            task = IG_TASKS.pop(ig["username"], None)
            if task:
                task.cancel()
            await release_proxy(ig["username"])
            CONFIG["ig_ids"].remove(ig)

    # Remove TG tokens of this user
    for tok in list(CONFIG["tg_tokens"]):
        if tok.get("owner_id") == uid:
            CONFIG["tg_tokens"].remove(tok)

    CONFIG["users"].pop(uid_s, None)
    save_config()

    await update.message.reply_text("🧹 All your data cleaned. Fresh start!")


async def tg_status(update, context):
    uid = update.effective_user.id
    my_ig = [ig for ig in CONFIG["ig_ids"] if ig.get("owner_id") == uid]
    my_tg = [t for t in CONFIG["tg_tokens"] if t.get("owner_id") == uid]

    text = f"📊 <b>Status</b>\n\n"
    text += f"📱 TG Tokens: {len(my_tg)}\n"
    for t in my_tg:
        text += f"  • <code>{t['token'][:20]}...</code>\n"
    text += f"\n📸 IG IDs: {len(my_ig)}\n"
    for ig in my_ig:
        running = "🟢" if ig["username"] in IG_TASKS else "🔴"
        text += f"  {running} @{ig['username']} (msgs: {ig.get('stats',{}).get('msgs_sent',0)})\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ---------- Add TG Token ----------
async def cb_add_tg(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    stats = user_stats(uid)
    if stats["tg_count"] >= MAX_TG_PER_USER:
        await q.message.reply_text(f"❌ <b>Dota Capacity Reached</b>\nMax {MAX_TG_PER_USER} TG tokens per user.", parse_mode="HTML")
        return
    CONV_STATE[uid] = {"step": "tg_token"}
    await q.message.reply_text("📱 Send your bot token:")


async def handle_tg_token(update, context):
    uid = update.effective_user.id
    state = CONV_STATE.get(uid)
    if not state or state.get("step") != "tg_token":
        return
    token = update.message.text.strip()
    if ":" not in token or len(token) < 30:
        await update.message.reply_text("❌ Invalid token.")
        return

    # Save
    CONFIG["tg_tokens"].append({
        "token": token,
        "owner_id": uid,
        "added_at": datetime.now().isoformat(),
        "stats": {"msgs_sent": 0}
    })
    stats = user_stats(uid)
    stats["tg_count"] = len([t for t in CONFIG["tg_tokens"] if t["owner_id"] == uid])
    save_config()

    CONV_STATE.pop(uid, None)
    await update.message.reply_text(f"✅ Token added! ({stats['tg_count']}/{MAX_TG_PER_USER})")

    # Start this bot
    asyncio.create_task(start_user_bot(token, uid))


# ---------- Add IG ID ----------
async def cb_add_ig(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    stats = user_stats(uid)
    if stats["ig_count"] >= MAX_IG_PER_USER:
        await q.message.reply_text(f"❌ <b>Dota Capacity Reached</b>\nMax {MAX_IG_PER_USER} IG IDs per user.", parse_mode="HTML")
        return
    CONV_STATE[uid] = {"step": "ig_username"}
    await q.message.reply_text("📸 Send IG username:")


async def handle_ig_username(update, context):
    uid = update.effective_user.id
    state = CONV_STATE.get(uid)
    if not state or state.get("step") != "ig_username":
        return
    state["ig_username"] = update.message.text.strip()
    state["step"] = "ig_password"
    await update.message.reply_text("🔑 Send IG password (will be hidden in next message, but sends as text — careful):")


async def handle_ig_password(update, context):
    uid = update.effective_user.id
    state = CONV_STATE.get(uid)
    if not state or state.get("step") != "ig_password":
        return
    state["ig_password"] = update.message.text.strip()
    # Delete password message for safety
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text("⏳ Logging in to Instagram...")

    # Try login
    test_entry = {
        "username": state["ig_username"],
        "password": state["ig_password"],
        "session_id": None,
        "device_info": None
    }
    cl, err = await ig_login(test_entry)

    if err == "need_2fa":
        state["step"] = "ig_2fa"
        state["ig_client"] = cl
        await update.message.reply_text("🔐 2FA required. Send your 2FA code:")
        return

    if err:
        CONV_STATE.pop(uid, None)
        await update.message.reply_text(f"❌ Login failed: {err}")
        return

    # Success
    state["step"] = "ig_gc"
    state["ig_client"] = cl
    await show_gc_list(update.message.chat_id, context, uid, cl)


async def handle_ig_2fa(update, context):
    uid = update.effective_user.id
    state = CONV_STATE.get(uid)
    if not state or state.get("step") != "ig_2fa":
        return
    code = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    cl = state["ig_client"]
    try:
        await cl.login(state["ig_username"], state["ig_password"], verification_code=code)
    except Exception as e:
        CONV_STATE.pop(uid, None)
        await update.message.reply_text(f"❌ 2FA login failed: {e}")
        return

    state["step"] = "ig_gc"
    await show_gc_list(update.message.chat_id, context, uid, cl)


async def show_gc_list(chat_id, context, uid, cl):
    gcs = await fetch_gc_list(cl)
    if not gcs:
        CONV_STATE.pop(uid, None)
        await context.bot.send_message(chat_id, "❌ No GCs found in this IG account.")
        return

    CONV_STATE[uid]["gcs"] = gcs[:MAX_GC_PER_TOKEN]
    buttons = [[InlineKeyboardButton(f"📁 {gc['title'][:40]}", callback_data=f"gc:{i}")]
               for i, gc in enumerate(gcs[:MAX_GC_PER_TOKEN])]
    await context.bot.send_message(
        chat_id,
        "📁 Choose a GC to send to:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cb_pick_gc(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    state = CONV_STATE.get(uid)
    if not state or "gcs" not in state:
        return

    idx = int(q.data.split(":")[1])
    gc = state["gcs"][idx]
    state["gc_id"] = gc["id"]
    state["gc_title"] = gc["title"]
    state["step"] = "ig_text"
    await q.message.reply_text(f"✅ GC: <b>{gc['title']}</b>\n\nNow send the text to spam:", parse_mode="HTML")


async def handle_ig_text(update, context):
    uid = update.effective_user.id
    state = CONV_STATE.get(uid)
    if not state or state.get("step") != "ig_text":
        return
    text = update.message.text.strip()
    if not text:
        return

    cl = state["ig_client"]
    session_id = cl.sessionid
    try:
        device_info = cl.get_settings()["device_settings"]
    except Exception:
        device_info = None

    ig_entry = {
        "username": state["ig_username"],
        "password": state["ig_password"],
        "session_id": session_id,
        "device_info": device_info,
        "user_id": str(cl.user_id),
        "owner_id": uid,
        "gc_id": state["gc_id"],
        "gc_name": state["gc_title"],
        "text": text,
        "running": True,
        "added_at": datetime.now().isoformat(),
        "stats": {"msgs_sent": 0}
    }
    CONFIG["ig_ids"].append(ig_entry)
    stats = user_stats(uid)
    stats["ig_count"] = len([ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid])
    save_config()
    CONV_STATE.pop(uid, None)

    await update.message.reply_text(
        f"✅ IG @{ig_entry['username']} added!\n"
        f"📁 GC: {ig_entry['gc_name']}\n"
        f"▶️ Starting loop..."
    )

    # Assign proxy & start
    proxy = await get_proxy_for_ig(ig_entry["username"])
    if not proxy:
        await update.message.reply_text("⚠️ No proxy available. Waiting for proxy...")

    bot = context.bot
    IG_TASKS[ig_entry["username"]] = asyncio.create_task(
        ig_send_loop(ig_entry, bot, uid)
    )


async def cb_stats(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    my_ig = [ig for ig in CONFIG["ig_ids"] if ig.get("owner_id") == uid]
    my_tg = [t for t in CONFIG["tg_tokens"] if t.get("owner_id") == uid]
    text = f"📊 <b>Your Stats</b>\n\nTG: {len(my_tg)}/{MAX_TG_PER_USER}\nIG: {len(my_ig)}/{MAX_IG_PER_USER}"
    await q.message.reply_text(text, parse_mode="HTML")


async def cb_clean(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    for ig in list(CONFIG["ig_ids"]):
        if ig.get("owner_id") == uid:
            t = IG_TASKS.pop(ig["username"], None)
            if t: t.cancel()
            await release_proxy(ig["username"])
            CONFIG["ig_ids"].remove(ig)
    for tok in list(CONFIG["tg_tokens"]):
        if tok.get("owner_id") == uid:
            CONFIG["tg_tokens"].remove(tok)
    CONFIG["users"].pop(str(uid), None)
    save_config()
    await q.message.reply_text("🧹 Cleaned!")


# ---------- User Bot Runner ----------
def build_user_app(token):
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", tg_start))
    app.add_handler(CommandHandler("menu", tg_menu))
    app.add_handler(CommandHandler("ping", tg_ping))
    app.add_handler(CommandHandler("myid", tg_myid))
    app.add_handler(CommandHandler("cancel", tg_cancel))
    app.add_handler(CommandHandler("clean", tg_clean))
    app.add_handler(CommandHandler("status", tg_status))
    app.add_handler(CallbackQueryHandler(cb_add_tg, pattern="^add_tg$"))
    app.add_handler(CallbackQueryHandler(cb_add_ig, pattern="^add_ig$"))
    app.add_handler(CallbackQueryHandler(cb_stats, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(cb_clean, pattern="^clean$"))
    app.add_handler(CallbackQueryHandler(cb_pick_gc, pattern="^gc:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    return app


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = CONV_STATE.get(uid)
    if not state:
        return
    step = state.get("step")
    if step == "tg_token":     await handle_tg_token(update, context)
    elif step == "ig_username": await handle_ig_username(update, context)
    elif step == "ig_password": await handle_ig_password(update, context)
    elif step == "ig_2fa":     await handle_ig_2fa(update, context)
    elif step == "ig_text":    await handle_ig_text(update, context)


async def start_user_bot(token, owner_id):
    """Start an Application for a user's bot token."""
    if token in TG_APPS:
        return
    try:
        app = build_user_app(token)
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        TG_APPS[token] = app
        print(f"✅ Started user bot: {token[:20]}...")
    except Exception as e:
        print(f"❌ Bot start fail {token[:20]}: {e}")


# ================= MAIN =================
async def main():
    global STOP_FLAG

    print("🚀 Dota Bot — Full System")
    print("=" * 55)

    # Config
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config(), f, indent=2)
        print(f"✅ Created {CONFIG_FILE}")
    load_config()

    # GitHub
    gh_session = await gh_bootstrap()
    if not gh_session:
        print("⚠️ GitHub failed — running without sync")
    else:
        asyncio.create_task(periodic_pull(gh_session))

    # Proxy manager
    asyncio.create_task(proxy_fetcher())
    print("[PROXY] Background fetcher started (30s)")

    # Start all saved user bots
    for tok in CONFIG["tg_tokens"]:
        await start_user_bot(tok["token"], tok["owner_id"])

    # Start all running IG IDs
    for ig in CONFIG["ig_ids"]:
        if ig.get("running"):
            IG_TASKS[ig["username"]] = asyncio.create_task(
                ig_send_loop(ig, None, ig["owner_id"])
            )

    # Main bot (owner control)
    if not MAIN_BOT_TOKEN or "REPLACE" in MAIN_BOT_TOKEN:
        print("⚠️ Set MAIN_BOT_TOKEN")
        return

    try:
        app = build_user_app(MAIN_BOT_TOKEN)
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        TG_APPS[MAIN_BOT_TOKEN] = app
        print(f"✅ Main bot started")
    except Exception as e:
        print(f"❌ Main bot: {e}")
        return

    print("\n🚀 All systems running!")
    try:
        while not STOP_FLAG:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        print("\n[!] Shutting down...")
        STOP_FLAG = True
        for task in list(IG_TASKS.values()):
            task.cancel()
        for app in TG_APPS.values():
            try:
                await app.updater.stop()
                await app.stop()
            except Exception:
                pass
        if gh_session:
            await gh_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[✅] Stopped.")