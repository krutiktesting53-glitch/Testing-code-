
import os
import re
import sys
import json
import time
import shutil
import signal
import sqlite3
import asyncio
import zipfile
import tempfile
import subprocess
from pathlib import Path
from threading import Thread, Lock

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================================================
# ⚡ KRUTIK CYBER EXPERT — ULTIMATE BOT HOSTING
# Render Web Service + Telegram polling
# ============================================================

BOT_TOKEN = "YAHAN_APNA_BOT_TOKEN_DALO"
OWNER_CHAT_ID = 7272787842

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "hosting.db"
PROJECTS_DIR = BASE_DIR / "projects"
TEMP_DIR = BASE_DIR / "temp"
LOGS_DIR = BASE_DIR / "logs"

for d in (PROJECTS_DIR, TEMP_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app_web = Flask(__name__)
processes = {}
process_locks = {}
global_lock = Lock()

# -------------------- DATABASE --------------------

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        username TEXT,
        joined_at INTEGER NOT NULL,
        last_active INTEGER NOT NULL,
        blocked INTEGER DEFAULT 0,
        access INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        path TEXT NOT NULL,
        startup_file TEXT,
        status TEXT DEFAULT 'stopped',
        auto_restart INTEGER DEFAULT 1,
        current_version INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        version_no INTEGER NOT NULL,
        path TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        note TEXT
    );

    CREATE TABLE IF NOT EXISTS deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        version_no INTEGER,
        status TEXT,
        error TEXT,
        started_at INTEGER,
        finished_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        project_id INTEGER,
        action TEXT NOT NULL,
        result TEXT,
        details TEXT,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    defaults = {
        "maintenance": "0",
        "max_projects_per_user": "10",
        "log_retention": "5000",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.commit()
    con.close()

def setting(key, default=None):
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else default

def set_setting(key, value):
    con = db()
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    con.commit()
    con.close()

def log_activity(user_id, action, result="OK", project_id=None, details=""):
    con = db()
    con.execute(
        "INSERT INTO activity(user_id,project_id,action,result,details,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, project_id, action, result, details[:2000], int(time.time()))
    )
    con.commit()
    con.close()

def upsert_user(tg_user):
    now = int(time.time())
    con = db()
    con.execute("""
        INSERT INTO users(user_id,name,username,joined_at,last_active)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,
            username=excluded.username,
            last_active=excluded.last_active
    """, (tg_user.id, tg_user.full_name, tg_user.username, now, now))
    con.commit()
    con.close()

def get_user(uid):
    con = db()
    row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return row

def owner(uid):
    return uid == OWNER_CHAT_ID

def user_allowed(uid):
    row = get_user(uid)
    if not row:
        return False
    return bool(row["access"]) and not bool(row["blocked"])

def maintenance_on():
    return setting("maintenance", "0") == "1"

# -------------------- SAFE PATHS / HELPERS --------------------

def safe_slug(name):
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return s[:40] or "project"

def unique_slug(name):
    base = safe_slug(name)
    slug = base
    n = 2
    con = db()
    while con.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    con.close()
    return slug

def project_dir(project):
    return Path(project["path"]).resolve()

def valid_project_path(p):
    try:
        return p.resolve().is_relative_to(PROJECTS_DIR.resolve())
    except AttributeError:
        return str(p.resolve()).startswith(str(PROJECTS_DIR.resolve()))

def startup_candidates(path):
    preferred = ["bot.py", "main.py", "app.py", "bot1.py", "run.py"]
    for n in preferred:
        if (path / n).is_file():
            return n
    py = sorted(path.glob("*.py"))
    return py[0].name if py else None

def project_log_path(pid):
    return LOGS_DIR / f"project_{pid}.log"

def append_log(pid, text):
    p = project_log_path(pid)
    with p.open("a", encoding="utf-8", errors="replace") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")

def clear_log(pid):
    project_log_path(pid).write_text("", encoding="utf-8")

def syntax_check(pyfile):
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(pyfile)],
        capture_output=True, text=True, timeout=60
    )
    return r.returncode == 0, (r.stdout + r.stderr).strip()

def requirements_install(path):
    req = path / "requirements.txt"
    if not req.exists():
        return True, "requirements.txt not found; skipped."
    # NOTE: Render Web Service installs into the same runtime environment.
    # For untrusted multi-user hosting, a separate sandbox/container is required.
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req)],
        cwd=str(path), capture_output=True, text=True, timeout=600
    )
    return r.returncode == 0, (r.stdout + "\n" + r.stderr)[-8000:]

# -------------------- PROJECT PROCESS CONTROL --------------------

def start_project_sync(pid):
    con = db()
    project = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    con.close()
    if not project:
        return False, "Project not found."

    p = project_dir(project)
    if not valid_project_path(p) or not p.exists():
        return False, "Project path is invalid or missing."

    lock = process_locks.setdefault(pid, Lock())
    with lock:
        old = processes.get(pid)
        if old and old.poll() is None:
            return True, "Already running."

        startup = project["startup_file"] or startup_candidates(p)
        if not startup:
            return False, "No Python startup file found."

        pyfile = p / startup
        if not pyfile.exists():
            return False, f"Startup file not found: {startup}"

        ok, err = syntax_check(pyfile)
        if not ok:
            append_log(pid, "SYNTAX ERROR:\n" + err)
            con = db()
            con.execute("UPDATE projects SET status='failed',updated_at=? WHERE id=?", (int(time.time()), pid))
            con.commit(); con.close()
            return False, err[-4000:]

        logf = project_log_path(pid)
        lf = logf.open("a", encoding="utf-8")
        lf.write(f"\n--- START {time.ctime()} ---\n")
        lf.flush()

        try:
            proc = subprocess.Popen(
                [sys.executable, startup],
                cwd=str(p),
                stdout=lf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy()
            )
        except Exception as e:
            lf.close()
            return False, str(e)

        processes[pid] = proc
        con = db()
        con.execute("UPDATE projects SET status='running',updated_at=? WHERE id=?", (int(time.time()), pid))
        con.commit(); con.close()
        return True, f"Started PID {proc.pid}"

def stop_project_sync(pid):
    lock = process_locks.setdefault(pid, Lock())
    with lock:
        proc = processes.get(pid)
        if not proc or proc.poll() is not None:
            processes.pop(pid, None)
            con = db()
            con.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?", (int(time.time()), pid))
            con.commit(); con.close()
            return True, "Already stopped."

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            append_log(pid, "STOP ERROR: " + str(e))

        processes.pop(pid, None)
        con = db()
        con.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?", (int(time.time()), pid))
        con.commit(); con.close()
        return True, "Stopped."

def monitor_processes():
    while True:
        try:
            con = db()
            rows = con.execute("SELECT * FROM projects WHERE status='running'").fetchall()
            con.close()
            for row in rows:
                pid = row["id"]
                proc = processes.get(pid)
                if proc and proc.poll() is not None:
                    code = proc.returncode
                    append_log(pid, f"\n--- PROCESS EXITED code={code} {time.ctime()} ---\n")
                    processes.pop(pid, None)
                    con = db()
                    con.execute("UPDATE projects SET status='failed',updated_at=? WHERE id=?", (int(time.time()), pid))
                    con.commit(); con.close()
                    if row["auto_restart"]:
                        time.sleep(2)
                        start_project_sync(pid)
        except Exception:
            pass
        time.sleep(5)

# -------------------- DEPLOYMENT --------------------

def create_version_from_current(pid, note=""):
    con = db()
    project = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not project:
        con.close()
        return None
    p = project_dir(project)
    version_no = int(project["current_version"]) + 1
    vdir = p / ".versions" / f"v{version_no}"
    vdir.parent.mkdir(parents=True, exist_ok=True)
    if vdir.exists():
        shutil.rmtree(vdir)
    shutil.copytree(p, vdir, ignore=shutil.ignore_patterns(".versions"))
    con.execute(
        "INSERT INTO versions(project_id,version_no,path,created_at,note) VALUES(?,?,?,?,?)",
        (pid, version_no, str(vdir), int(time.time()), note[:500])
    )
    con.execute(
        "UPDATE projects SET current_version=?,updated_at=? WHERE id=?",
        (version_no, int(time.time()), pid)
    )
    con.commit(); con.close()
    return version_no

def deploy_path(pid, staging):
    con = db()
    project = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    con.close()
    if not project:
        return False, "Project not found."

    p = project_dir(project)
    staging = Path(staging).resolve()
    if not staging.exists():
        return False, "Staging directory missing."

    startup = startup_candidates(staging)
    if not startup:
        return False, "No .py startup file found in upload."

    ok, err = syntax_check(staging / startup)
    if not ok:
        return False, "Syntax check failed:\n" + err[-5000:]

    ok, reqout = requirements_install(staging)
    if not ok:
        return False, "requirements.txt installation failed:\n" + reqout[-6000:]

    was_running = processes.get(pid)
    running_before = bool(was_running and was_running.poll() is None)
    if running_before:
        stop_project_sync(pid)

    # Preserve current tree as a version before replacement.
    create_version_from_current(pid, "pre-deploy snapshot")

    tmp_old = p.parent / (p.name + ".old")
    if tmp_old.exists():
        shutil.rmtree(tmp_old, ignore_errors=True)

    # Keep .versions outside the uploaded staging content.
    versions_dir = p / ".versions"
    if versions_dir.exists():
        saved_versions = tempfile.mkdtemp(dir=str(p.parent))
        shutil.move(str(versions_dir), str(Path(saved_versions) / ".versions"))
    else:
        saved_versions = None

    try:
        shutil.move(str(p), str(tmp_old))
        shutil.move(str(staging), str(p))
        if saved_versions:
            shutil.move(str(Path(saved_versions) / ".versions"), str(p / ".versions"))
            shutil.rmtree(saved_versions, ignore_errors=True)
        shutil.rmtree(tmp_old, ignore_errors=True)

        con = db()
        con.execute(
            "UPDATE projects SET startup_file=?,status='stopped',updated_at=? WHERE id=?",
            (startup, int(time.time()), pid)
        )
        con.commit(); con.close()
        if running_before:
            start_project_sync(pid)
        return True, "Deployment successful."
    except Exception as e:
        # Best-effort rollback to old tree.
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        if tmp_old.exists():
            shutil.move(str(tmp_old), str(p))
        return False, "Deployment replacement failed: " + str(e)

# -------------------- TELEGRAM UI --------------------

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 My Projects", callback_data="projects"),
         InlineKeyboardButton("➕ New Project", callback_data="newproject")],
        [InlineKeyboardButton("📊 My Stats", callback_data="mystats"),
         InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ])

def owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="adashboard"),
         InlineKeyboardButton("👥 Users", callback_data="ausers")],
        [InlineKeyboardButton("📦 All Projects", callback_data="aprojects"),
         InlineKeyboardButton("🚀 Deployments", callback_data="adeployments")],
        [InlineKeyboardButton("📜 Activity", callback_data="aactivity"),
         InlineKeyboardButton("⚙️ Settings", callback_data="asettings")],
        [InlineKeyboardButton("🛠 Maintenance", callback_data="amaint"),
         InlineKeyboardButton("📢 Broadcast", callback_data="abroadcast")]
    ])

async def deny(update, text):
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)

async def ensure_user(update):
    u = update.effective_user
    upsert_user(u)
    row = get_user(u.id)
    if row and row["blocked"]:
        await deny(update, "🚫 You are blocked by owner.")
        return False
    if owner(u.id):
        return True
    if maintenance_on():
        await deny(update, "🛠 Hosting is in maintenance mode.")
        return False
    if not row or not row["access"]:
        await deny(update, "⛔ Access not granted yet.\nYour Chat ID: " + str(u.id))
        return False
    return True

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    u = update.effective_user
    row = get_user(u.id)
    if owner(u.id):
        await update.message.reply_text(
            "⚡ KRUTIK CYBER EXPERT — ULTIMATE BOT HOSTING\n\n"
            "👑 Owner Panel ready.",
            reply_markup=owner_menu()
        )
        return
    await update.message.reply_text(
        "⚡ KRUTIK CYBER EXPERT — ULTIMATE BOT HOSTING\n\n"
        f"👤 Name: {u.full_name}\n"
        f"🔹 Username: @{u.username}" if u.username else
        f"👤 Name: {u.full_name}",
    )
    await update.message.reply_text(
        f"🆔 Your Chat ID: `{u.id}`\n\n"
        + ("✅ Access granted." if row and row["access"] else
           "⏳ Access pending.\nOwner must approve your Chat ID before you can use hosting."),
        parse_mode="Markdown",
        reply_markup=main_menu() if row and row["access"] else None
    )
    try:
        uname = f"@{u.username}" if u.username else "Not set"
        await context.bot.send_message(
            OWNER_CHAT_ID,
            "🆕 New User Started\n\n"
            f"👤 Name: {u.full_name}\n"
            f"🔹 Username: {uname}\n"
            f"🆔 Chat ID: {u.id}\n"
            f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except Exception:
        pass

async def admin_cmd(update, context):
    if not owner(update.effective_user.id):
        return await deny(update, "Owner only.")
    await update.message.reply_text("👑 Owner Control Panel", reply_markup=owner_menu())

async def approve_cmd(update, context):
    if not owner(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /approve CHAT_ID")
    uid = int(context.args[0])
    con = db()
    con.execute("UPDATE users SET access=1,blocked=0 WHERE user_id=?", (uid,))
    con.commit(); con.close()
    log_activity(OWNER_CHAT_ID, "approve_user", "OK", details=str(uid))
    await update.message.reply_text(f"✅ Access granted: {uid}")
    try:
        await context.bot.send_message(uid, "✅ Owner granted you hosting access.\nSend /start to open panel.")
    except Exception:
        pass

async def revoke_cmd(update, context):
    if not owner(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("Usage: /revoke CHAT_ID")
    uid = int(context.args[0])
    con = db(); con.execute("UPDATE users SET access=0 WHERE user_id=?", (uid,)); con.commit(); con.close()
    await update.message.reply_text(f"⛔ Access revoked: {uid}")

async def block_cmd(update, context):
    if not owner(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("Usage: /block CHAT_ID")
    uid = int(context.args[0])
    con = db(); con.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,)); con.commit(); con.close()
    await update.message.reply_text(f"🚫 Blocked: {uid}")

async def unblock_cmd(update, context):
    if not owner(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("Usage: /unblock CHAT_ID")
    uid = int(context.args[0])
    con = db(); con.execute("UPDATE users SET blocked=0 WHERE user_id=?", (uid,)); con.commit(); con.close()
    await update.message.reply_text(f"✅ Unblocked: {uid}")

async def maintenance_cmd(update, context):
    if not owner(update.effective_user.id): return
    if not context.args or context.args[0].lower() not in ("on","off"):
        return await update.message.reply_text("Usage: /maintenance on|off")
    val = context.args[0].lower() == "on"
    set_setting("maintenance", int(val))
    await update.message.reply_text("🛠 Maintenance " + ("ON" if val else "OFF"))

async def status_cmd(update, context):
    if not owner(update.effective_user.id): return
    con = db()
    users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    access = con.execute("SELECT COUNT(*) c FROM users WHERE access=1 AND blocked=0").fetchone()["c"]
    projects = con.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
    running = con.execute("SELECT COUNT(*) c FROM projects WHERE status='running'").fetchone()["c"]
    con.close()
    await update.message.reply_text(
        f"📊 Users: {users}\n✅ Allowed: {access}\n📦 Projects: {projects}\n🟢 Running: {running}"
    )

async def text_handler(update, context):
    # Conversation-lite project creation flow.
    uid = update.effective_user.id
    if not await ensure_user(update):
        return
    state = context.user_data.get("state")
    if state == "new_project_name":
        name = update.message.text.strip()
        if not name:
            return await update.message.reply_text("Invalid name.")
        con = db()
        count = con.execute("SELECT COUNT(*) c FROM projects WHERE user_id=?", (uid,)).fetchone()["c"]
        con.close()
        if count >= int(setting("max_projects_per_user", "10")) and not owner(uid):
            return await update.message.reply_text("Project limit reached.")
        slug = unique_slug(name)
        p = PROJECTS_DIR / slug
        p.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        con = db()
        cur = con.execute(
            "INSERT INTO projects(user_id,name,slug,path,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (uid, name, slug, str(p), now, now)
        )
        pid = cur.lastrowid
        con.commit(); con.close()
        context.user_data["state"] = None
        log_activity(uid, "create_project", "OK", pid, name)
        await update.message.reply_text(
            f"✅ Project created.\n📦 {name}\n🆔 Project ID: {pid}\n\n"
            "Now upload a .zip or .py file.",
            reply_markup=project_buttons(pid, owner(uid))
        )
        return
    await update.message.reply_text("Use the buttons below.", reply_markup=main_menu())

async def document_handler(update, context):
    if not await ensure_user(update):
        return
    doc = update.message.document
    if not doc:
        return
    uid = update.effective_user.id
    pid = context.user_data.get("upload_pid")
    if not pid:
        return await update.message.reply_text("Open a project first, then choose Upload.")
    con = db()
    project = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    con.close()
    if not project or (project["user_id"] != uid and not owner(uid)):
        return await update.message.reply_text("Project access denied.")
    filename = Path(doc.file_name or "upload.bin").name
    if not filename.lower().endswith((".zip", ".py")):
        return await update.message.reply_text("Only .zip and .py are supported.")
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    tgfile = await doc.get_file()
    temp = Path(tempfile.mkdtemp(dir=TEMP_DIR))
    raw = temp / filename
    await tgfile.download_to_drive(str(raw))
    staging = Path(tempfile.mkdtemp(dir=TEMP_DIR))
    try:
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(raw) as z:
                for info in z.infolist():
                    member = Path(info.filename)
                    if member.is_absolute() or ".." in member.parts:
                        raise ValueError("Unsafe ZIP path detected.")
                z.extractall(staging)
            children = list(staging.iterdir())
            if len(children) == 1 and children[0].is_dir():
                real_staging = children[0]
            else:
                real_staging = staging
        else:
            shutil.copy2(raw, staging / filename)
            real_staging = staging

        # deploy_path moves staging, so do not delete it afterward.
        ok, msg = deploy_path(pid, real_staging)
        if ok:
            await update.message.reply_text("🚀 Deployment successful!\n\n" + msg,
                                            reply_markup=project_buttons(pid, owner(uid)))
        else:
            await update.message.reply_text("❌ Deployment failed.\n\n" + msg)
    except Exception as e:
        await update.message.reply_text("❌ Upload/deployment error:\n" + str(e))
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        # If deploy_path failed, staging may still exist.
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

def project_buttons(pid, is_owner=False):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Start", callback_data=f"pstart:{pid}"),
         InlineKeyboardButton("🔴 Stop", callback_data=f"pstop:{pid}")],
        [InlineKeyboardButton("🔄 Restart", callback_data=f"prestart:{pid}"),
         InlineKeyboardButton("📜 Logs", callback_data=f"plogs:{pid}")],
        [InlineKeyboardButton("📁 Files", callback_data=f"files:{pid}"),
         InlineKeyboardButton("🕘 Versions", callback_data=f"versions:{pid}")],
        [InlineKeyboardButton("📤 Upload", callback_data=f"upload:{pid}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"pdelete:{pid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="aprojects" if is_owner else "projects")]
    ])

async def show_projects(q, uid, admin=False):
    con = db()
    if admin:
        rows = con.execute("SELECT * FROM projects ORDER BY id DESC LIMIT 50").fetchall()
    else:
        rows = con.execute("SELECT * FROM projects WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    con.close()
    if not rows:
        return await q.edit_message_text("📦 No projects.", reply_markup=owner_menu() if admin else main_menu())
    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(
            f"{'🟢' if r['status']=='running' else '🔴'} {r['name']} | #{r['id']}",
            callback_data=f"showp:{r['id']}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adashboard" if admin else "home")])
    await q.edit_message_text("📦 Projects", reply_markup=InlineKeyboardMarkup(buttons))

async def callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    upsert_user(q.from_user)
    data = q.data

    if data == "home":
        if await ensure_user(update): await q.edit_message_text("Main Panel", reply_markup=main_menu())
        return
    if data == "projects":
        if await ensure_user(update): await show_projects(q, uid, False)
        return
    if data == "newproject":
        if await ensure_user(update):
            context.user_data["state"] = "new_project_name"
            await q.edit_message_text("✍️ Send the new project name:")
        return
    if data == "mystats":
        if not await ensure_user(update): return
        con = db()
        n = con.execute("SELECT COUNT(*) c FROM projects WHERE user_id=?", (uid,)).fetchone()["c"]
        d = con.execute("SELECT COUNT(*) c FROM deployments WHERE user_id=?", (uid,)).fetchone()["c"]
        con.close()
        await q.edit_message_text(f"📊 Your Stats\n\nProjects: {n}\nDeployments: {d}",
                                  reply_markup=main_menu())
        return
    if data == "help":
        await q.edit_message_text(
            "⚡ Ultimate Hosting\n\n"
            "1. Create project\n2. Upload ZIP/PY\n3. Requirements install\n"
            "4. Syntax check\n5. Deploy\n6. Start/Stop/Restart\n"
            "7. Logs\n8. Versions/Rollback\n\n"
            "Owner must approve your Chat ID first.",
            reply_markup=main_menu())
        return

    if not owner(uid) and not await ensure_user(update):
        return

    if data == "adashboard":
        if not owner(uid): return
        con = db()
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        projects = con.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        running = con.execute("SELECT COUNT(*) c FROM projects WHERE status='running'").fetchone()["c"]
        failed = con.execute("SELECT COUNT(*) c FROM projects WHERE status='failed'").fetchone()["c"]
        deps = con.execute("SELECT COUNT(*) c FROM deployments").fetchone()["c"]
        con.close()
        await q.edit_message_text(
            f"👑 Dashboard\n\n👥 Users: {users}\n📦 Projects: {projects}\n"
            f"🟢 Running: {running}\n❌ Failed: {failed}\n🚀 Deployments: {deps}",
            reply_markup=owner_menu())
        return
    if data == "ausers":
        if not owner(uid): return
        con = db()
        rows = con.execute("SELECT * FROM users ORDER BY last_active DESC LIMIT 40").fetchall()
        con.close()
        buttons = [[InlineKeyboardButton(
            f"{'🚫' if r['blocked'] else ('✅' if r['access'] else '⏳')} {r['name'][:22]} | {r['user_id']}",
            callback_data=f"user:{r['user_id']}"
        )] for r in rows]
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adashboard")])
        await q.edit_message_text("👥 Users", reply_markup=InlineKeyboardMarkup(buttons))
        return
    if data.startswith("user:"):
        if not owner(uid): return
        x = int(data.split(":")[1])
        con = db()
        u = con.execute("SELECT * FROM users WHERE user_id=?", (x,)).fetchone()
        pc = con.execute("SELECT COUNT(*) c FROM projects WHERE user_id=?", (x,)).fetchone()["c"]
        dc = con.execute("SELECT COUNT(*) c FROM deployments WHERE user_id=?", (x,)).fetchone()["c"]
        con.close()
        if not u: return await q.edit_message_text("User not found.", reply_markup=owner_menu())
        await q.edit_message_text(
            f"👤 {u['name']}\n🔹 @{u['username']}\n🆔 {u['user_id']}\n"
            f"🔐 Access: {'YES' if u['access'] else 'NO'}\n🚫 Blocked: {'YES' if u['blocked'] else 'NO'}\n"
            f"📦 Projects: {pc}\n🚀 Deployments: {dc}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"uapprove:{x}"),
                 InlineKeyboardButton("⛔ Revoke", callback_data=f"urevoke:{x}")],
                [InlineKeyboardButton("🚫 Block", callback_data=f"ublock:{x}"),
                 InlineKeyboardButton("🔓 Unblock", callback_data=f"uunblock:{x}")],
                [InlineKeyboardButton("📦 Projects", callback_data=f"uproj:{x}")],
                [InlineKeyboardButton("🔙 Users", callback_data="ausers")]
            ]))
        return
    if data.startswith(("uapprove:", "urevoke:", "ublock:", "uunblock:")):
        if not owner(uid): return
        action, raw = data.split(":")
        x = int(raw)
        field = {"uapprove":"access=1,blocked=0","urevoke":"access=0","ublock":"blocked=1","uunblock":"blocked=0"}[action]
        con = db(); con.execute(f"UPDATE users SET {field} WHERE user_id=?", (x,)); con.commit(); con.close()
        await q.answer("Updated.", show_alert=True)
        return
    if data.startswith("uproj:"):
        if not owner(uid): return
        x = int(data.split(":")[1])
        await show_projects(q, x, False)
        return
    if data == "aprojects":
        if not owner(uid): return
        await show_projects(q, uid, True)
        return
    if data.startswith("showp:"):
        pid = int(data.split(":")[1])
        con = db(); p = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone(); con.close()
        if not p: return await q.edit_message_text("Project not found.", reply_markup=owner_menu() if owner(uid) else main_menu())
        if p["user_id"] != uid and not owner(uid):
            return await q.answer("Access denied.", show_alert=True)
        await q.edit_message_text(
            f"📦 {p['name']}\n🆔 {p['id']}\n👤 Owner: {p['user_id']}\n"
            f"📌 Status: {p['status']}\n🐍 Startup: {p['startup_file']}\n"
            f"♻️ Auto Restart: {'ON' if p['auto_restart'] else 'OFF'}\n"
            f"🔢 Version: {p['current_version']}",
            reply_markup=project_buttons(pid, owner(uid)))
        return

    if data.startswith("pstart:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        ok,msg=start_project_sync(pid); await q.edit_message_text(("🟢 " if ok else "❌ ")+msg, reply_markup=project_buttons(pid,owner(uid))); return
    if data.startswith("pstop:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        ok,msg=stop_project_sync(pid); await q.edit_message_text(("🔴 " if ok else "❌ ")+msg, reply_markup=project_buttons(pid,owner(uid))); return
    if data.startswith("prestart:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        stop_project_sync(pid); ok,msg=start_project_sync(pid); await q.edit_message_text(("🔄 " if ok else "❌ ")+msg, reply_markup=project_buttons(pid,owner(uid))); return
    if data.startswith("plogs:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        lp=project_log_path(pid)
        txt=lp.read_text(encoding="utf-8",errors="replace")[-3500:] if lp.exists() else "No logs."
        await q.edit_message_text("📜 Logs\n\n"+txt, reply_markup=project_buttons(pid,owner(uid))); return
    if data.startswith("upload:"):
        pid=int(data.split(":")[1]); context.user_data["upload_pid"]=pid
        await q.edit_message_text("📤 Send a .zip or .py document now.", reply_markup=project_buttons(pid,owner(uid))); return
    if data.startswith("files:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        root=project_dir(p)
        items=[]
        for x in sorted(root.rglob("*")):
            if ".versions" in x.parts: continue
            rel=x.relative_to(root)
            if len(rel.parts)<=3:
                items.append(("📁 " if x.is_dir() else "📄 ")+str(rel))
        txt="\n".join(items[:80]) or "Empty."
        await q.edit_message_text("📁 Files\n\n"+txt, reply_markup=project_buttons(pid,owner(uid))); return
    if data.startswith("versions:"):
        pid=int(data.split(":")[1]); con=db(); rows=con.execute("SELECT * FROM versions WHERE project_id=? ORDER BY version_no DESC LIMIT 30",(pid,)).fetchall(); con.close()
        buttons=[[InlineKeyboardButton(f"v{r['version_no']} — rollback",callback_data=f"rollback:{pid}:{r['version_no']}")] for r in rows]
        buttons.append([InlineKeyboardButton("🔙 Back",callback_data=f"showp:{pid}")])
        await q.edit_message_text("🕘 Versions",reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("rollback:"):
        _,raw,rv=data.split(":"); pid=int(raw); ver=int(rv)
        con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); v=con.execute("SELECT * FROM versions WHERE project_id=? AND version_no=?",(pid,ver)).fetchone(); con.close()
        if not p or not v or (p["user_id"]!=uid and not owner(uid)): return
        stop_project_sync(pid)
        src=Path(v["path"]); dest=project_dir(p)
        if not src.exists(): return await q.edit_message_text("Version files missing.",reply_markup=project_buttons(pid,owner(uid)))
        keep=tempfile.mkdtemp(dir=PROJECTS_DIR)
        try:
            # Preserve .versions, replace project content.
            versions = dest / ".versions"
            if versions.exists(): shutil.copytree(versions, Path(keep)/".versions")
            for child in dest.iterdir(): shutil.rmtree(child,ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            for child in src.iterdir():
                if child.name == ".versions": continue
                shutil.copytree(child,dest/child.name) if child.is_dir() else shutil.copy2(child,dest/child.name)
            if (Path(keep)/".versions").exists():
                shutil.copytree(Path(keep)/".versions", dest/".versions", dirs_exist_ok=True)
            startup=startup_candidates(dest)
            con=db(); con.execute("UPDATE projects SET startup_file=?,status='stopped',updated_at=? WHERE id=?",(startup,int(time.time()),pid)); con.commit(); con.close()
            await q.edit_message_text(f"🔙 Rolled back to v{ver}.",reply_markup=project_buttons(pid,owner(uid)))
        finally: shutil.rmtree(keep,ignore_errors=True)
        return
    if data.startswith("pdelete:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        await q.edit_message_text("⚠️ Confirm project deletion?",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("❌ YES DELETE",callback_data=f"confirmdelete:{pid}")],
                                      [InlineKeyboardButton("Cancel",callback_data=f"showp:{pid}")]
                                  ])); return
    if data.startswith("confirmdelete:"):
        pid=int(data.split(":")[1]); con=db(); p=con.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); con.close()
        if not p or (p["user_id"]!=uid and not owner(uid)): return
        stop_project_sync(pid)
        shutil.rmtree(project_dir(p),ignore_errors=True); project_log_path(pid).unlink(missing_ok=True)
        con=db(); con.execute("DELETE FROM projects WHERE id=?",(pid,)); con.commit(); con.close()
        await q.edit_message_text("🗑 Project deleted.",reply_markup=owner_menu() if owner(uid) else main_menu()); return

    if data == "adeployments":
        if not owner(uid): return
        con=db(); rows=con.execute("SELECT * FROM deployments ORDER BY id DESC LIMIT 30").fetchall(); con.close()
        txt="\n".join(f"#{r['id']} P{r['project_id']} v{r['version_no']} {r['status']}" for r in rows) or "No deployments."
        await q.edit_message_text("🚀 Deployments\n\n"+txt,reply_markup=owner_menu()); return
    if data == "aactivity":
        if not owner(uid): return
        con=db(); rows=con.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 40").fetchall(); con.close()
        txt="\n".join(f"{r['created_at']} | U{r['user_id']} | P{r['project_id']} | {r['action']} | {r['result']}" for r in rows) or "No activity."
        await q.edit_message_text("📜 Activity\n\n"+txt[-3800:],reply_markup=owner_menu()); return
    if data == "asettings":
        if not owner(uid): return
        await q.edit_message_text(
            f"⚙️ Settings\n\nMax projects/user: {setting('max_projects_per_user')}\n"
            f"Log retention: {setting('log_retention')}\nMaintenance: {setting('maintenance')}",
            reply_markup=owner_menu()); return
    if data == "amaint":
        if not owner(uid): return
        await q.edit_message_text(
            f"🛠 Maintenance: {'ON' if maintenance_on() else 'OFF'}\nUse /maintenance on or /maintenance off",
            reply_markup=owner_menu()); return
    if data == "abroadcast":
        if not owner(uid): return
        context.user_data["state"]="broadcast"
        await q.edit_message_text("📢 Send the broadcast text:")
        return

async def broadcast_text(update, context):
    if not owner(update.effective_user.id): return
    if context.user_data.get("state") != "broadcast": return
    text=update.message.text
    con=db(); rows=con.execute("SELECT user_id FROM users WHERE blocked=0 AND access=1").fetchall(); con.close()
    sent=0
    for r in rows:
        try:
            await context.bot.send_message(r["user_id"], "📢 Owner Broadcast\n\n"+text)
            sent+=1
        except Exception:
            pass
    context.user_data["state"]=None
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.",reply_markup=owner_menu())

# -------------------- RENDER WEB SERVICE --------------------

@app_web.route("/")
def home():
    return "⚡ KRUTIK CYBER EXPERT — ULTIMATE BOT HOSTING"

@app_web.route("/health")
def health():
    return "OK"

def run_web():
    port=int(os.environ.get("PORT","10000"))
    app_web.run(host="0.0.0.0",port=port,debug=False,use_reloader=False)

def main():
    init_db()
    Thread(target=run_web, daemon=True).start()
    Thread(target=monitor_processes, daemon=True).start()

    application=Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start",start_cmd))
    application.add_handler(CommandHandler("admin",admin_cmd))
    application.add_handler(CommandHandler("approve",approve_cmd))
    application.add_handler(CommandHandler("revoke",revoke_cmd))
    application.add_handler(CommandHandler("block",block_cmd))
    application.add_handler(CommandHandler("unblock",unblock_cmd))
    application.add_handler(CommandHandler("maintenance",maintenance_cmd))
    application.add_handler(CommandHandler("status",status_cmd))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_text, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("⚡ KRUTIK CYBER EXPERT — ULTIMATE BOT HOSTING")
    print("Render Web Service bridge started.")
    print("Telegram polling started.")
    application.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
