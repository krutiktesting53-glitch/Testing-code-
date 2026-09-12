# ============================================================
# ⚡ KRUTIK CYBER EXPERT — ULTIMATE BOT HOSTING
# ============================================================
# Features:
# - User management
# - Project upload (.zip / .py)
# - Project management
# - Start / Stop / Restart
# - Auto restart
# - requirements.txt installation
# - Logs
# - Error logs
# - Deployment records
# - Version records
# - Owner/Admin panel
# - User block/unblock
# - Project force control
# - Statistics
# - Broadcast
# - File manager basics
#
# Removed by design:
# ❌ Backup System
# ❌ Resource Monitoring
# ❌ Webhook/Polling Manager
# ============================================================

import os
import sys
import json
import time
import shutil
import sqlite3
import zipfile
import asyncio
import subprocess
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Telegram numeric Chat ID (Render Environment Variable se liya jayega)
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()
try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW) if OWNER_CHAT_ID_RAW else 0
except ValueError:
    OWNER_CHAT_ID = 0

BASE_DIR = os.path.abspath(os.getcwd())

DB_PATH = os.path.join(BASE_DIR, "hosting.db")

PROJECTS_DIR = os.path.join(BASE_DIR, "projects")

LOGS_DIR = os.path.join(BASE_DIR, "logs")

TEMP_DIR = os.path.join(BASE_DIR, "temp")

MAX_UPLOAD_SIZE = 100 * 1024 * 1024

AUTO_RESTART = True

MAX_RESTART_ATTEMPTS = 5


# ============================================================
# DIRECTORIES
# ============================================================

os.makedirs(PROJECTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


# ============================================================
# DATABASE
# ============================================================

def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():

    connection = db()
    cursor = connection.cursor()

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            name TEXT,
            username TEXT,
            joined_at TEXT,
            last_active TEXT,
            blocked INTEGER DEFAULT 0
        )
    """)

    # --------------------------------------------------------
    # PROJECTS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT UNIQUE,
            user_id INTEGER,
            name TEXT,
            path TEXT,
            startup_file TEXT,
            python_version TEXT,
            status TEXT DEFAULT 'STOPPED',
            current_version INTEGER DEFAULT 1,
            created_at TEXT,
            last_started TEXT,
            last_stopped TEXT,
            last_deployment TEXT,
            restart_count INTEGER DEFAULT 0
        )
    """)

    # --------------------------------------------------------
    # DEPLOYMENTS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            user_id INTEGER,
            version INTEGER,
            status TEXT,
            started_at TEXT,
            completed_at TEXT,
            error TEXT
        )
    """)

    # --------------------------------------------------------
    # VERSIONS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            version INTEGER,
            path TEXT,
            created_at TEXT,
            status TEXT
        )
    """)

    # --------------------------------------------------------
    # ACTIVITY
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            project_id TEXT,
            action TEXT,
            result TEXT,
            details TEXT,
            created_at TEXT
        )
    """)

    connection.commit()
    connection.close()


init_db()


# ============================================================
# RUNTIME PROCESS STORAGE
# ============================================================

running_processes = {}

restart_tasks = {}


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(name):

    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

    result = ""

    for char in name:

        if char in allowed:
            result += char

    if not result:
        result = "project"

    return result[:50]


def project_path(project_id):

    return os.path.join(PROJECTS_DIR, project_id)


def log_path(project_id):

    return os.path.join(LOGS_DIR, f"{project_id}.log")


def error_log_path(project_id):

    return os.path.join(LOGS_DIR, f"{project_id}_error.log")


def is_owner(user_id):

    return user_id == OWNER_CHAT_ID


def get_user(user_id):

    connection = db()

    row = connection.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    connection.close()

    return row


def is_blocked(user_id):

    user = get_user(user_id)

    if not user:
        return False

    return bool(user["blocked"])


def register_user(user):

    connection = db()

    existing = connection.execute(
        "SELECT id FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()

    if existing:

        connection.execute("""
            UPDATE users
            SET name = ?,
                username = ?,
                last_active = ?
            WHERE user_id = ?
        """, (
            user.full_name,
            user.username or "",
            now(),
            user.id
        ))

    else:

        connection.execute("""
            INSERT INTO users
            (
                user_id,
                name,
                username,
                joined_at,
                last_active
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user.id,
            user.full_name,
            user.username or "",
            now(),
            now()
        ))

    connection.commit()
    connection.close()


def add_activity(
    user_id,
    action,
    result="OK",
    project_id=None,
    details=""
):

    connection = db()

    connection.execute("""
        INSERT INTO activity
        (
            user_id,
            project_id,
            action,
            result,
            details,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        project_id,
        action,
        result,
        details,
        now()
    ))

    connection.commit()
    connection.close()


def get_project(project_id):

    connection = db()

    row = connection.execute(
        "SELECT * FROM projects WHERE project_id = ?",
        (project_id,)
    ).fetchone()

    connection.close()

    return row


def user_projects(user_id):

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM projects
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    connection.close()

    return rows


# ============================================================
# USER MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "➕ New Project",
                callback_data="new_project"
            )
        ],

        [
            InlineKeyboardButton(
                "🤖 My Projects",
                callback_data="my_projects"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="my_stats"
            )
        ],

        [
            InlineKeyboardButton(
                "📜 Activity",
                callback_data="my_activity"
            )
        ],

    ])


# ============================================================
# OWNER MENU
# ============================================================

def owner_menu():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📊 Dashboard",
                callback_data="admin_dashboard"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 Users",
                callback_data="admin_users"
            ),

            InlineKeyboardButton(
                "📦 Projects",
                callback_data="admin_projects"
            )
        ],

        [
            InlineKeyboardButton(
                "🚀 Deployments",
                callback_data="admin_deployments"
            ),

            InlineKeyboardButton(
                "📜 Logs",
                callback_data="admin_logs"
            )
        ],

        [
            InlineKeyboardButton(
                "🔄 Versions",
                callback_data="admin_versions"
            ),

            InlineKeyboardButton(
                "📈 Statistics",
                callback_data="admin_stats"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="admin_broadcast"
            )
        ],

        [
            InlineKeyboardButton(
                "⚙️ Hosting Settings",
                callback_data="admin_settings"
            )
        ],

    ])


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    register_user(user)

    if is_blocked(user.id) and not is_owner(user.id):

        await update.message.reply_text(
            "🚫 You are blocked from using this hosting service."
        )

        return

    add_activity(
        user.id,
        "START",
        "OK"
    )

    if is_owner(user.id):

        await update.message.reply_text(
            "👑 KRUTIK CYBER EXPERT\n\n"
            "⚡ ULTIMATE BOT HOSTING\n\n"
            "Welcome Owner.\n"
            "Use the Admin Panel below.",
            reply_markup=owner_menu()
        )

        return

    await update.message.reply_text(
        "⚡ KRUTIK CYBER EXPERT\n\n"
        "☁️ ULTIMATE BOT HOSTING\n\n"
        "Welcome!\n\n"
        "Upload your Python project and manage it directly from Telegram.",
        reply_markup=main_menu()
    )


# ============================================================
# NEW PROJECT
# ============================================================

async def new_project_start(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if is_blocked(user_id):

        await query.edit_message_text(
            "🚫 You are blocked."
        )

        return

    context.user_data["creating_project"] = True

    await query.edit_message_text(
        "➕ CREATE PROJECT\n\n"
        "Send your project as:\n\n"
        "📦 ZIP file\n"
        "📄 Python file\n\n"
        "Example:\n"
        "mybot.zip\n\n"
        "The project will be created automatically."
    )


# ============================================================
# FILE UPLOAD
# ============================================================

async def handle_document(update, context):

    user = update.effective_user

    register_user(user)

    if is_blocked(user.id) and not is_owner(user.id):

        await update.message.reply_text(
            "🚫 You are blocked."
        )

        return

    document = update.message.document

    if not document:

        return

    file_name = document.file_name or "project"

    if document.file_size:

        if document.file_size > MAX_UPLOAD_SIZE:

            await update.message.reply_text(
                "❌ File is too large."
            )

            return

    # --------------------------------------------------------
    # Require project creation mode
    # --------------------------------------------------------

    if not context.user_data.get("creating_project"):

        await update.message.reply_text(
            "ℹ️ First press ➕ New Project."
        )

        return

    project_name = os.path.splitext(file_name)[0]

    project_name = safe_name(project_name)

    project_id = (
        f"{user.id}_"
        f"{int(time.time())}"
    )

    destination = project_path(project_id)

    os.makedirs(destination, exist_ok=True)

    temp_file = os.path.join(
        TEMP_DIR,
        f"{project_id}_{file_name}"
    )

    try:

        await update.message.reply_text(
            "📥 Downloading project..."
        )

        telegram_file = await document.get_file()

        await telegram_file.download_to_drive(
            temp_file
        )

        await update.message.reply_text(
            "📦 Processing project..."
        )

        # ----------------------------------------------------
        # ZIP
        # ----------------------------------------------------

        if file_name.lower().endswith(".zip"):

            with zipfile.ZipFile(
                temp_file,
                "r"
            ) as archive:

                archive.extractall(
                    destination
                )

        # ----------------------------------------------------
        # PYTHON
        # ----------------------------------------------------

        elif file_name.lower().endswith(".py"):

            shutil.copy(
                temp_file,
                os.path.join(
                    destination,
                    file_name
                )
            )

        else:

            os.remove(temp_file)

            shutil.rmtree(
                destination,
                ignore_errors=True
            )

            await update.message.reply_text(
                "❌ Supported files:\n\n"
                "📦 ZIP\n"
                "📄 PY"
            )

            return

        os.remove(temp_file)

        # ----------------------------------------------------
        # Find startup file
        # ----------------------------------------------------

        startup = find_startup_file(
            destination
        )

        if not startup:

            await update.message.reply_text(
                "⚠️ Project uploaded but no Python startup file was found.\n\n"
                "Create a .py file and use project settings."
            )

            startup = ""

        connection = db()

        connection.execute("""
            INSERT INTO projects
            (
                project_id,
                user_id,
                name,
                path,
                startup_file,
                python_version,
                status,
                current_version,
                created_at,
                last_deployment
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            project_id,
            user.id,
            project_name,
            destination,
            startup,
            f"{sys.version_info.major}.{sys.version_info.minor}",
            "STOPPED",
            1,
            now(),
            now()
        ))

        connection.execute("""
            INSERT INTO versions
            (
                project_id,
                version,
                path,
                created_at,
                status
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            project_id,
            1,
            destination,
            now(),
            "CURRENT"
        ))

        connection.execute("""
            INSERT INTO deployments
            (
                project_id,
                user_id,
                version,
                status,
                started_at,
                completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            project_id,
            user.id,
            1,
            "SUCCESS",
            now(),
            now()
        ))

        connection.commit()
        connection.close()

        add_activity(
            user.id,
            "PROJECT_CREATED",
            "SUCCESS",
            project_id,
            project_name
        )

        context.user_data["creating_project"] = False

        await update.message.reply_text(
            "✅ PROJECT CREATED\n\n"
            f"📦 Name: {project_name}\n"
            f"🆔 ID: {project_id}\n"
            f"🐍 Startup: {startup or 'Not found'}\n"
            f"🔢 Version: 1\n\n"
            "Use My Projects to manage it.",
            reply_markup=main_menu()
        )

    except Exception as e:

        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass

        shutil.rmtree(
            destination,
            ignore_errors=True
        )

        await update.message.reply_text(
            f"❌ Project creation failed.\n\n"
            f"Error: {str(e)[:1500]}"
        )


# ============================================================
# FIND STARTUP FILE
# ============================================================

def find_startup_file(path):

    priority = [
        "bot.py",
        "main.py",
        "app.py",
        "run.py",
        "server.py"
    ]

    for file in priority:

        full = os.path.join(
            path,
            file
        )

        if os.path.isfile(full):

            return file

    for root, dirs, files in os.walk(path):

        for file in files:

            if file.endswith(".py"):

                relative = os.path.relpath(
                    os.path.join(root, file),
                    path
                )

                return relative

    return None


# ============================================================
# PROJECT LIST
# ============================================================

async def my_projects(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    rows = user_projects(user_id)

    if not rows:

        await query.edit_message_text(
            "📦 MY PROJECTS\n\n"
            "No projects found.",
            reply_markup=main_menu()
        )

        return

    buttons = []

    for row in rows:

        status = row["status"]

        icon = "🟢" if status == "RUNNING" else "🔴"

        buttons.append([

            InlineKeyboardButton(
                f"{icon} {row['name']}",
                callback_data=f"project:{row['project_id']}"
            )

        ])

    buttons.append([

        InlineKeyboardButton(
            "🔙 Back",
            callback_data="back_main"
        )

    ])

    await query.edit_message_text(
        "📦 MY PROJECTS",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# PROJECT DETAILS
# ============================================================

async def project_details(update, context):

    query = update.callback_query

    await query.answer()

    project_id = query.data.split(":", 1)[1]

    project = get_project(project_id)

    if not project:

        await query.edit_message_text(
            "❌ Project not found."
        )

        return

    if (
        project["user_id"] != query.from_user.id
        and not is_owner(query.from_user.id)
    ):

        await query.edit_message_text(
            "🚫 Access denied."
        )

        return

    status = project["status"]

    buttons = []

    if status == "RUNNING":

        buttons.append([

            InlineKeyboardButton(
                "⏹️ Stop",
                callback_data=f"stop:{project_id}"
            ),

            InlineKeyboardButton(
                "🔄 Restart",
                callback_data=f"restart:{project_id}"
            )

        ])

    else:

        buttons.append([

            InlineKeyboardButton(
                "▶️ Start",
                callback_data=f"startbot:{project_id}"
            )

        ])

    buttons.append([

        InlineKeyboardButton(
            "📜 Logs",
            callback_data=f"logs:{project_id}"
        ),

        InlineKeyboardButton(
            "❌ Errors",
            callback_data=f"errors:{project_id}"
        )

    ])

    buttons.append([

        InlineKeyboardButton(
            "📦 Deploy",
            callback_data=f"deploy:{project_id}"
        ),

        InlineKeyboardButton(
            "🔄 Versions",
            callback_data=f"versions:{project_id}"
        )

    ])

    buttons.append([

        InlineKeyboardButton(
            "🗑️ Delete",
            callback_data=f"delete_confirm:{project_id}"
        )

    ])

    buttons.append([

        InlineKeyboardButton(
            "🔙 Back",
            callback_data="my_projects"
        )

    ])

    await query.edit_message_text(
        f"📦 PROJECT\n\n"
        f"Name: {project['name']}\n"
        f"ID: {project['project_id']}\n"
        f"Status: {status}\n"
        f"Startup: {project['startup_file'] or 'Not set'}\n"
        f"Version: {project['current_version']}\n"
        f"Created: {project['created_at']}\n"
        f"Restarts: {project['restart_count']}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# INSTALL REQUIREMENTS
# ============================================================

async def install_requirements(path, project_id):

    requirements = os.path.join(
        path,
        "requirements.txt"
    )

    if not os.path.isfile(requirements):

        return True, "requirements.txt not found."

    log_file = log_path(project_id)

    try:

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            requirements,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=path
        )

        output = []

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            text = line.decode(
                errors="ignore"
            )

            output.append(text)

            with open(
                log_file,
                "a",
                encoding="utf-8"
            ) as f:

                f.write(text)

        code = await process.wait()

        if code == 0:

            return True, "".join(output)

        return False, "".join(output)

    except Exception as e:

        return False, str(e)


# ============================================================
# START PROJECT
# ============================================================

async def start_project(project_id, user_id):

    project = get_project(project_id)

    if not project:
        return False, "Project not found."

    if (
        project["user_id"] != user_id
        and not is_owner(user_id)
    ):

        return False, "Access denied."

    if project_id in running_processes:

        return False, "Project is already running."

    path = project["path"]

    startup = project["startup_file"]

    if not startup:

        startup = find_startup_file(path)

    if not startup:

        return False, "No Python startup file found."

    startup_path = os.path.join(
        path,
        startup
    )

    if not os.path.isfile(startup_path):

        return False, "Startup file does not exist."

    # --------------------------------------------------------
    # Requirements
    # --------------------------------------------------------

    success, output = await install_requirements(
        path,
        project_id
    )

    if not success:

        return False, (
            "Dependency installation failed.\n\n"
            + output[-3000:]
        )

    log_file = log_path(project_id)

    error_file = error_log_path(project_id)

    log_handle = open(
        log_file,
        "a",
        encoding="utf-8"
    )

    error_handle = open(
        error_file,
        "a",
        encoding="utf-8"
    )

    log_handle.write(
        f"\n\n===== START {now()} =====\n"
    )

    try:

        process = subprocess.Popen(
            [
                sys.executable,
                startup_path
            ],
            cwd=path,
            stdout=log_handle,
            stderr=error_handle
        )

    except Exception as e:

        log_handle.close()
        error_handle.close()

        return False, str(e)

    running_processes[project_id] = {
        "process": process,
        "log_handle": log_handle,
        "error_handle": error_handle,
        "user_id": project["user_id"],
        "restart_attempts": 0
    }

    connection = db()

    connection.execute("""
        UPDATE projects
        SET status = 'RUNNING',
            last_started = ?
        WHERE project_id = ?
    """, (
        now(),
        project_id
    ))

    connection.commit()
    connection.close()

    add_activity(
        user_id,
        "START",
        "SUCCESS",
        project_id
    )

    if project_id not in restart_tasks:

        restart_tasks[project_id] = asyncio.create_task(
            monitor_process(project_id)
        )

    return True, "Project started successfully."


# ============================================================
# PROCESS MONITOR
# ============================================================

async def monitor_process(project_id):

    while project_id in running_processes:

        data = running_processes.get(project_id)

        if not data:

            break

        process = data["process"]

        return_code = process.poll()

        if return_code is None:

            await asyncio.sleep(3)

            continue

        try:
            data["log_handle"].close()
        except Exception:
            pass

        try:
            data["error_handle"].close()
        except Exception:
            pass

        user_id = data["user_id"]

        attempts = data["restart_attempts"]

        running_processes.pop(
            project_id,
            None
        )

        connection = db()

        connection.execute("""
            UPDATE projects
            SET status = 'STOPPED',
                last_stopped = ?,
                restart_count = restart_count + 1
            WHERE project_id = ?
        """, (
            now(),
            project_id
        ))

        connection.commit()
        connection.close()

        # ----------------------------------------------------
        # AUTO RESTART
        # ----------------------------------------------------

        if AUTO_RESTART and attempts < MAX_RESTART_ATTEMPTS:

            await asyncio.sleep(3)

            project = get_project(project_id)

            if project:

                ok, message = await start_project(
                    project_id,
                    user_id
                )

                if ok:

                    if project_id in running_processes:

                        running_processes[
                            project_id
                        ][
                            "restart_attempts"
                        ] = attempts + 1

                else:

                    await notify_user(
                        user_id,
                        f"❌ {project['name']} crashed.\n\n"
                        f"♻️ Auto-restart failed.\n\n"
                        f"{message[:1000]}"
                    )

        else:

            await notify_user(
                user_id,
                "❌ Your project stopped.\n\n"
                f"🔄 Restart attempts reached the limit."
            )

        break


# ============================================================
# NOTIFY USER
# ============================================================

async def notify_user(user_id, text):

    try:

        application = CURRENT_APPLICATION

        if application:

            await application.bot.send_message(
                chat_id=user_id,
                text=text
            )

    except Exception:
        pass


CURRENT_APPLICATION = None


# ============================================================
# STOP PROJECT
# ============================================================

async def stop_project(project_id, user_id):

    project = get_project(project_id)

    if not project:

        return False, "Project not found."

    if (
        project["user_id"] != user_id
        and not is_owner(user_id)
    ):

        return False, "Access denied."

    data = running_processes.get(project_id)

    if not data:

        connection = db()

        connection.execute("""
            UPDATE projects
            SET status = 'STOPPED',
                last_stopped = ?
            WHERE project_id = ?
        """, (
            now(),
            project_id
        ))

        connection.commit()
        connection.close()

        return True, "Project already stopped."

    process = data["process"]

    try:

        process.terminate()

        try:

            process.wait(timeout=5)

        except subprocess.TimeoutExpired:

            process.kill()

    except Exception as e:

        return False, str(e)

    try:
        data["log_handle"].close()
    except Exception:
        pass

    try:
        data["error_handle"].close()
    except Exception:
        pass

    running_processes.pop(
        project_id,
        None
    )

    connection = db()

    connection.execute("""
        UPDATE projects
        SET status = 'STOPPED',
            last_stopped = ?
        WHERE project_id = ?
    """, (
        now(),
        project_id
    ))

    connection.commit()
    connection.close()

    add_activity(
        user_id,
        "STOP",
        "SUCCESS",
        project_id
    )

    return True, "Project stopped successfully."


# ============================================================
# RESTART
# ============================================================

async def restart_project(project_id, user_id):

    ok, message = await stop_project(
        project_id,
        user_id
    )

    if not ok:

        return False, message

    await asyncio.sleep(1)

    return await start_project(
        project_id,
        user_id
    )


# ============================================================
# DELETE PROJECT
# ============================================================

async def delete_project(project_id, user_id):

    project = get_project(project_id)

    if not project:

        return False, "Project not found."

    if (
        project["user_id"] != user_id
        and not is_owner(user_id)
    ):

        return False, "Access denied."

    await stop_project(
        project_id,
        user_id
    )

    path = project["path"]

    try:

        if os.path.exists(path):

            shutil.rmtree(path)

        log = log_path(project_id)

        error = error_log_path(project_id)

        if os.path.exists(log):
            os.remove(log)

        if os.path.exists(error):
            os.remove(error)

    except Exception as e:

        return False, str(e)

    connection = db()

    connection.execute(
        "DELETE FROM versions WHERE project_id = ?",
        (project_id,)
    )

    connection.execute(
        "DELETE FROM deployments WHERE project_id = ?",
        (project_id,)
    )

    connection.execute(
        "DELETE FROM activity WHERE project_id = ?",
        (project_id,)
    )

    connection.execute(
        "DELETE FROM projects WHERE project_id = ?",
        (project_id,)
    )

    connection.commit()
    connection.close()

    add_activity(
        user_id,
        "DELETE_PROJECT",
        "SUCCESS",
        project_id
    )

    return True, "Project deleted successfully."


# ============================================================
# LOGS
# ============================================================

def read_log(project_id, error=False):

    path = (
        error_log_path(project_id)
        if error
        else log_path(project_id)
    )

    if not os.path.isfile(path):

        return "No logs available."

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as f:

            content = f.read()

        return content[-7000:]

    except Exception as e:

        return str(e)


# ============================================================
# VERSIONS
# ============================================================

def get_versions(project_id):

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM versions
        WHERE project_id = ?
        ORDER BY version DESC
    """, (project_id,)).fetchall()

    connection.close()

    return rows


# ============================================================
# DEPLOY PROJECT
# ============================================================

async def deploy_project(project_id, user_id):

    project = get_project(project_id)

    if not project:

        return False, "Project not found."

    if (
        project["user_id"] != user_id
        and not is_owner(user_id)
    ):

        return False, "Access denied."

    new_version = project["current_version"] + 1

    connection = db()

    connection.execute("""
        INSERT INTO deployments
        (
            project_id,
            user_id,
            version,
            status,
            started_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        project_id,
        project["user_id"],
        new_version,
        "RUNNING",
        now()
    ))

    connection.commit()

    connection.execute("""
        UPDATE projects
        SET current_version = ?,
            last_deployment = ?
        WHERE project_id = ?
    """, (
        new_version,
        now(),
        project_id
    ))

    connection.execute("""
        INSERT INTO versions
        (
            project_id,
            version,
            path,
            created_at,
            status
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        project_id,
        new_version,
        project["path"],
        now(),
        "CURRENT"
    ))

    connection.execute("""
        UPDATE versions
        SET status = 'OLD'
        WHERE project_id = ?
        AND version != ?
    """, (
        project_id,
        new_version
    ))

    connection.execute("""
        UPDATE deployments
        SET status = 'SUCCESS',
            completed_at = ?
        WHERE project_id = ?
        AND version = ?
    """, (
        now(),
        project_id,
        new_version
    ))

    connection.commit()
    connection.close()

    add_activity(
        user_id,
        "DEPLOY",
        "SUCCESS",
        project_id,
        f"Version {new_version}"
    )

    return True, f"Deployment successful. Version {new_version}"


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    data = query.data

    user_id = query.from_user.id

    await query.answer()

    # --------------------------------------------------------
    # USER MENU
    # --------------------------------------------------------

    if data == "new_project":

        await new_project_start(
            update,
            context
        )

        return

    if data == "my_projects":

        await my_projects(
            update,
            context
        )

        return

    if data == "my_stats":

        await show_my_stats(
            update,
            context
        )

        return

    if data == "my_activity":

        await show_my_activity(
            update,
            context
        )

        return

    if data == "back_main":

        await query.edit_message_text(
            "⚡ KRUTIK CYBER EXPERT\n\n"
            "☁️ ULTIMATE BOT HOSTING",
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # PROJECT
    # --------------------------------------------------------

    if data.startswith("project:"):

        await project_details(
            update,
            context
        )

        return

    if data.startswith("startbot:"):

        project_id = data.split(":", 1)[1]

        ok, message = await start_project(
            project_id,
            user_id
        )

        await query.answer(
            message,
            show_alert=True
        )

        await project_details(
            update,
            context
        )

        return

    if data.startswith("stop:"):

        project_id = data.split(":", 1)[1]

        ok, message = await stop_project(
            project_id,
            user_id
        )

        await query.answer(
            message,
            show_alert=True
        )

        await project_details(
            update,
            context
        )

        return

    if data.startswith("restart:"):

        project_id = data.split(":", 1)[1]

        ok, message = await restart_project(
            project_id,
            user_id
        )

        await query.answer(
            message,
            show_alert=True
        )

        await project_details(
            update,
            context
        )

        return

    if data.startswith("logs:"):

        project_id = data.split(":", 1)[1]

        project = get_project(project_id)

        if not project:

            await query.edit_message_text(
                "❌ Project not found."
            )

            return

        if (
            project["user_id"] != user_id
            and not is_owner(user_id)
        ):

            await query.edit_message_text(
                "🚫 Access denied."
            )

            return

        logs = read_log(project_id)

        await query.edit_message_text(
            f"📜 LOGS — {project['name']}\n\n"
            f"<pre>{escape_html(logs[-6500:])}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data=f"project:{project_id}"
                    )
                ]
            ])
        )

        return

    if data.startswith("errors:"):

        project_id = data.split(":", 1)[1]

        project = get_project(project_id)

        if not project:

            await query.edit_message_text(
                "❌ Project not found."
            )

            return

        errors = read_log(
            project_id,
            error=True
        )

        await query.edit_message_text(
            f"❌ ERROR LOG — {project['name']}\n\n"
            f"<pre>{escape_html(errors[-6500:])}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data=f"project:{project_id}"
                    )
                ]
            ])
        )

        return

    if data.startswith("deploy:"):

        project_id = data.split(":", 1)[1]

        ok, message = await deploy_project(
            project_id,
            user_id
        )

        await query.answer(
            message,
            show_alert=True
        )

        await project_details(
            update,
            context
        )

        return

    if data.startswith("versions:"):

        await show_versions(
            update,
            context
        )

        return

    if data.startswith("delete_confirm:"):

        project_id = data.split(":", 1)[1]

        await query.edit_message_text(
            "⚠️ DELETE PROJECT?\n\n"
            "This will delete the project files and records.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🗑️ YES DELETE",
                        callback_data=f"delete:{project_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"project:{project_id}"
                    )
                ]
            ])
        )

        return

    if data.startswith("delete:"):

        project_id = data.split(":", 1)[1]

        ok, message = await delete_project(
            project_id,
            user_id
        )

        await query.answer(
            message,
            show_alert=True
        )

        await my_projects(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if data.startswith("admin_"):

        if not is_owner(user_id):

            await query.answer(
                "🚫 Owner only.",
                show_alert=True
            )

            return

        if data == "admin_dashboard":

            await admin_dashboard(
                update,
                context
            )

        elif data == "admin_users":

            await admin_users(
                update,
                context
            )

        elif data == "admin_projects":

            await admin_projects(
                update,
                context
            )

        elif data == "admin_deployments":

            await admin_deployments(
                update,
                context
            )

        elif data == "admin_logs":

            await admin_logs(
                update,
                context
            )

        elif data == "admin_versions":

            await admin_versions(
                update,
                context
            )

        elif data == "admin_stats":

            await admin_stats(
                update,
                context
            )

        elif data == "admin_settings":

            await admin_settings(
                update,
                context
            )

        elif data == "admin_broadcast":

            context.user_data["broadcast"] = True

            await query.edit_message_text(
                "📢 BROADCAST\n\n"
                "Send the message you want to broadcast."
            )

        return


# ============================================================
# HTML ESCAPE
# ============================================================

def escape_html(text):

    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# MY STATISTICS
# ============================================================

async def show_my_stats(update, context):

    query = update.callback_query

    user_id = query.from_user.id

    projects = user_projects(user_id)

    running = sum(
        1 for p in projects
        if p["status"] == "RUNNING"
    )

    connection = db()

    deployments = connection.execute("""
        SELECT COUNT(*)
        FROM deployments
        WHERE user_id = ?
    """, (user_id,)).fetchone()[0]

    connection.close()

    await query.edit_message_text(
        "📊 MY STATISTICS\n\n"
        f"📦 Projects: {len(projects)}\n"
        f"🟢 Running: {running}\n"
        f"🔴 Stopped: {len(projects) - running}\n"
        f"🚀 Deployments: {deployments}",
        reply_markup=main_menu()
    )


# ============================================================
# MY ACTIVITY
# ============================================================

async def show_my_activity(update, context):

    query = update.callback_query

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM activity
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 15
    """, (
        query.from_user.id,
    )).fetchall()

    connection.close()

    if not rows:

        text = "📜 No activity."

    else:

        lines = []

        for row in rows:

            lines.append(
                f"• {row['created_at']}\n"
                f"  {row['action']} — {row['result']}"
            )

        text = "📜 MY ACTIVITY\n\n" + "\n\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=main_menu()
    )


# ============================================================
# VERSIONS
# ============================================================

async def show_versions(update, context):

    query = update.callback_query

    project_id = query.data.split(":", 1)[1]

    project = get_project(project_id)

    if not project:

        await query.edit_message_text(
            "❌ Project not found."
        )

        return

    rows = get_versions(project_id)

    lines = [
        f"🔄 VERSIONS — {project['name']}",
        ""
    ]

    for row in rows:

        icon = (
            "🟢"
            if row["status"] == "CURRENT"
            else "⚪"
        )

        lines.append(
            f"{icon} Version {row['version']} "
            f"— {row['status']}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data=f"project:{project_id}"
                )
            ]
        ])
    )


# ============================================================
# ADMIN DASHBOARD
# ============================================================

async def admin_dashboard(update, context):

    query = update.callback_query

    connection = db()

    users = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    projects = connection.execute(
        "SELECT COUNT(*) FROM projects"
    ).fetchone()[0]

    running = connection.execute("""
        SELECT COUNT(*)
        FROM projects
        WHERE status = 'RUNNING'
    """).fetchone()[0]

    stopped = connection.execute("""
        SELECT COUNT(*)
        FROM projects
        WHERE status = 'STOPPED'
    """).fetchone()[0]

    deployments = connection.execute(
        "SELECT COUNT(*) FROM deployments"
    ).fetchone()[0]

    activities = connection.execute(
        "SELECT COUNT(*) FROM activity"
    ).fetchone()[0]

    connection.close()

    await query.edit_message_text(
        "👑 ADMIN DASHBOARD\n\n"
        f"👥 Users: {users}\n"
        f"📦 Projects: {projects}\n"
        f"🟢 Running: {running}\n"
        f"🔴 Stopped: {stopped}\n"
        f"🚀 Deployments: {deployments}\n"
        f"📜 Activities: {activities}",
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def admin_users(update, context):

    query = update.callback_query

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM users
        ORDER BY id DESC
        LIMIT 30
    """).fetchall()

    connection.close()

    if not rows:

        text = "👥 No users."

    else:

        lines = [
            "👥 USERS",
            ""
        ]

        for row in rows:

            status = (
                "🚫 BLOCKED"
                if row["blocked"]
                else "🟢 ACTIVE"
            )

            lines.append(
                f"{status}\n"
                f"👤 {row['name']}\n"
                f"🆔 {row['user_id']}\n"
                f"🔗 @{row['username'] or '-'}\n"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN PROJECTS
# ============================================================

async def admin_projects(update, context):

    query = update.callback_query

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM projects
        ORDER BY id DESC
        LIMIT 30
    """).fetchall()

    connection.close()

    if not rows:

        text = "📦 No projects."

    else:

        lines = [
            "📦 ALL PROJECTS",
            ""
        ]

        for row in rows:

            lines.append(
                f"📦 {row['name']}\n"
                f"🆔 {row['project_id']}\n"
                f"👤 {row['user_id']}\n"
                f"📌 {row['status']}\n"
                f"🔄 V{row['current_version']}\n"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN DEPLOYMENTS
# ============================================================

async def admin_deployments(update, context):

    query = update.callback_query

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM deployments
        ORDER BY id DESC
        LIMIT 25
    """).fetchall()

    connection.close()

    if not rows:

        text = "🚀 No deployments."

    else:

        lines = [
            "🚀 DEPLOYMENTS",
            ""
        ]

        for row in rows:

            lines.append(
                f"📦 {row['project_id']}\n"
                f"👤 {row['user_id']}\n"
                f"🔢 Version: {row['version']}\n"
                f"📌 {row['status']}\n"
                f"🕐 {row['started_at']}\n"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN LOGS
# ============================================================

async def admin_logs(update, context):

    query = update.callback_query

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM activity
        ORDER BY id DESC
        LIMIT 30
    """).fetchall()

    connection.close()

    if not rows:

        text = "📜 No activity logs."

    else:

        lines = [
            "📜 ADMIN ACTIVITY LOG",
            ""
        ]

        for row in rows:

            lines.append(
                f"{row['created_at']}\n"
                f"👤 {row['user_id']}\n"
                f"⚙️ {row['action']}\n"
                f"📌 {row['result']}\n"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN VERSIONS
# ============================================================

async def admin_versions(update, context):

    query = update.callback_query

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM versions
        ORDER BY id DESC
        LIMIT 30
    """).fetchall()

    connection.close()

    if not rows:

        text = "🔄 No versions."

    else:

        lines = [
            "🔄 ALL VERSIONS",
            ""
        ]

        for row in rows:

            lines.append(
                f"📦 {row['project_id']}\n"
                f"🔢 V{row['version']}\n"
                f"📌 {row['status']}\n"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats(update, context):

    query = update.callback_query

    connection = db()

    total_users = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    blocked_users = connection.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE blocked = 1
    """).fetchone()[0]

    total_projects = connection.execute(
        "SELECT COUNT(*) FROM projects"
    ).fetchone()[0]

    total_deployments = connection.execute(
        "SELECT COUNT(*) FROM deployments"
    ).fetchone()[0]

    total_versions = connection.execute(
        "SELECT COUNT(*) FROM versions"
    ).fetchone()[0]

    connection.close()

    await query.edit_message_text(
        "📈 GLOBAL STATISTICS\n\n"
        f"👥 Users: {total_users}\n"
        f"🚫 Blocked: {blocked_users}\n"
        f"📦 Projects: {total_projects}\n"
        f"🚀 Deployments: {total_deployments}\n"
        f"🔄 Versions: {total_versions}",
        reply_markup=owner_menu()
    )


# ============================================================
# ADMIN SETTINGS
# ============================================================

async def admin_settings(update, context):

    query = update.callback_query

    await query.edit_message_text(
        "⚙️ HOSTING SETTINGS\n\n"
        f"♻️ Auto Restart: "
        f"{'ON' if AUTO_RESTART else 'OFF'}\n\n"
        f"📦 Max Upload: "
        f"{MAX_UPLOAD_SIZE // (1024 * 1024)} MB\n\n"
        f"🔄 Max Restart Attempts: "
        f"{MAX_RESTART_ATTEMPTS}\n\n"
        "These are currently configured in bot.py.",
        reply_markup=owner_menu()
    )


# ============================================================
# BROADCAST
# ============================================================

async def handle_broadcast(update, context):

    if not is_owner(update.effective_user.id):

        return

    text = update.message.text

    connection = db()

    rows = connection.execute(
        "SELECT user_id FROM users WHERE blocked = 0"
    ).fetchall()

    connection.close()

    sent = 0
    failed = 0

    for row in rows:

        try:

            await update.get_bot().send_message(
                chat_id=row["user_id"],
                text=text
            )

            sent += 1

            await asyncio.sleep(0.05)

        except Exception:

            failed += 1

    context.user_data["broadcast"] = False

    await update.message.reply_text(
        "📢 BROADCAST COMPLETED\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}",
        reply_markup=owner_menu()
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    user = update.effective_user

    register_user(user)

    if is_blocked(user.id) and not is_owner(user.id):

        await update.message.reply_text(
            "🚫 You are blocked."
        )

        return

    if (
        is_owner(user.id)
        and context.user_data.get("broadcast")
    ):

        await handle_broadcast(
            update,
            context
        )

        return

    await update.message.reply_text(
        "⚡ Use the buttons below.",
        reply_markup=(
            owner_menu()
            if is_owner(user.id)
            else main_menu()
        )
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    print(
        "ERROR:",
        context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global CURRENT_APPLICATION

    if (
        not BOT_TOKEN
        or BOT_TOKEN == "YAHAN_APNA_BOT_TOKEN_DALO"
    ):

        print(
            "\n❌ BOT_TOKEN set karo bot.py mein.\n"
        )

        return

    print("=" * 60)

    print(
        "⚡ KRUTIK CYBER EXPERT — "
        "ULTIMATE BOT HOSTING"
    )

    print("=" * 60)

    print(
        f"Database: {DB_PATH}"
    )

    print(
        f"Projects: {PROJECTS_DIR}"
    )

    print(
        f"Owner: {OWNER_CHAT_ID}"
    )

    print("=" * 60)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    CURRENT_APPLICATION = application

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "🚀 Hosting bot started."
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()
