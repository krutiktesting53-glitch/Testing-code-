import os
import sqlite3
import secrets
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID", "").strip()

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# Owner Telegram username / branding
OWNER_USERNAME = "@Cyber_expert_KRUTIK"

DB_PATH = "code_share.db"


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_blocked INTEGER DEFAULT 0,
            joined_at TEXT,
            last_active TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            language TEXT DEFAULT '',
            file_name TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            storage_chat_id TEXT NOT NULL,
            storage_message_id INTEGER NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            is_deleted INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS share_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_id TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT,
            expires_at TEXT,
            max_uses INTEGER DEFAULT 0,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS retrievals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_id TEXT NOT NULL,
            share_token TEXT,
            user_id INTEGER NOT NULL,
            retrieved_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            code_id TEXT,
            details TEXT,
            created_at TEXT
        )
    """)

    con.commit()
    con.close()


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def user_blocked(user_id):
    con = db()
    row = con.execute(
        "SELECT is_blocked FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    con.close()

    return bool(row and row[0])


def register_user(user):
    con = db()

    existing = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if existing:
        con.execute("""
            UPDATE users
            SET username=?,
                first_name=?,
                last_name=?,
                last_active=?
            WHERE user_id=?
        """, (
            user.username,
            user.first_name,
            user.last_name,
            now(),
            user.id,
        ))
    else:
        con.execute("""
            INSERT INTO users
            (
                user_id,
                username,
                first_name,
                last_name,
                joined_at,
                last_active
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            now(),
            now(),
        ))

    con.commit()
    con.close()


def log_activity(user_id, action, code_id=None, details=""):
    con = db()

    con.execute("""
        INSERT INTO activity_logs
        (user_id, action, code_id, details, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        user_id,
        action,
        code_id,
        details,
        now(),
    ))

    con.commit()
    con.close()


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload Code", callback_data="upload"),
            InlineKeyboardButton("📋 My Codes", callback_data="my_codes"),
        ],
        [
            InlineKeyboardButton("🔎 Get Code", callback_data="get_code"),
            InlineKeyboardButton("🔗 My Shares", callback_data="my_shares"),
        ],
        [
            InlineKeyboardButton("👤 My Profile", callback_data="profile"),
            InlineKeyboardButton("📊 Statistics", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    register_user(user)

    if user_blocked(user.id):
        await update.message.reply_text(
            "🚫 You are blocked from using this bot."
        )
        return

    # Shared code link
    if context.args:
        arg = context.args[0]

        if arg.startswith("code_"):
            token = arg.replace("code_", "", 1)
            await retrieve_by_token(update, context, token)
            return

        if arg.startswith("CODE-"):
            code_id = arg.upper()
            await retrieve_by_code_id(update, context, code_id)
            return

    text = (
        "💻 *Code Save and Share*\n\n"
        "Save your code files and share them with anyone "
        "through Telegram.\n\n"
        f"👑 Owner {OWNER_USERNAME}"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user = query.from_user
    register_user(user)

    if user_blocked(user.id):
        await query.edit_message_text(
            "🚫 You are blocked from using this bot."
        )
        return

    data = query.data

    if data == "upload":

        context.user_data["state"] = "waiting_file"

        await query.edit_message_text(
            "📤 *Upload Code*\n\n"
            "Send your code file now.\n\n"
            "Supported examples:\n"
            "`.py` `.js` `.html` `.css` `.json` `.java` `.cpp` `.php` `.txt`\n\n"
            "❌ /cancel to cancel",
            parse_mode="Markdown",
        )

    elif data == "my_codes":
        await show_my_codes(query, user.id)

    elif data == "get_code":

        context.user_data["state"] = "waiting_code_id"

        await query.edit_message_text(
            "🔎 Send Code ID.\n\n"
            "Example:\n"
            "`CODE-1001`",
            parse_mode="Markdown",
        )

    elif data == "my_shares":
        await show_my_shares(query, user.id)

    elif data == "profile":
        await show_profile(query, user.id)

    elif data == "stats":
        await show_stats(query, user.id)

    elif data == "help":
        await query.edit_message_text(
            "ℹ️ *How it works*\n\n"
            "1️⃣ Upload your code file.\n"
            "2️⃣ Bot stores it in the private Telegram storage channel.\n"
            "3️⃣ Bot creates a Code ID and share link.\n"
            "4️⃣ Share the Telegram link with anyone.\n"
            "5️⃣ They open it and receive the file.\n\n"
            f"👑 Owner {OWNER_USERNAME}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="home")]
            ]),
        )

    elif data == "home":
        await query.edit_message_text(
            "💻 *Code Save and Share*\n\n"
            f"👑 Owner {OWNER_USERNAME}",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )


# ============================================================
# TEXT / FILE ROUTER
# ============================================================

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    register_user(user)

    if user_blocked(user.id):
        await update.message.reply_text(
            "🚫 You are blocked from using this bot."
        )
        return

    state = context.user_data.get("state")

    if state == "waiting_file":

        if not update.message.document:
            await update.message.reply_text(
                "❌ Please send a code file/document."
            )
            return

        await process_upload(update, context)
        return

    if state == "waiting_title":

        title = update.message.text.strip()

        if not title:
            await update.message.reply_text(
                "❌ Title cannot be empty."
            )
            return

        context.user_data["title"] = title
        context.user_data["state"] = "waiting_description"

        await update.message.reply_text(
            "📝 Send a description.\n\n"
            "Or send `skip`."
        )
        return

    if state == "waiting_description":

        description = update.message.text.strip()

        if description.lower() == "skip":
            description = ""

        context.user_data["description"] = description
        context.user_data["state"] = "waiting_language"

        await update.message.reply_text(
            "🏷️ Enter programming language.\n\n"
            "Example: `Python`\n\n"
            "Or send `auto`."
        )
        return

    if state == "waiting_language":

        language = update.message.text.strip()

        if language.lower() == "auto":
            language = detect_language(
                context.user_data.get("file_name", "")
            )

        context.user_data["language"] = language

        await finish_upload(update, context)
        return

    if state == "waiting_code_id":

        code_id = update.message.text.strip().upper()

        await retrieve_by_code_id(update, context, code_id)

        context.user_data.clear()
        return

    await update.message.reply_text(
        "👇 Please use the menu.",
        reply_markup=main_menu(),
    )


# ============================================================
# UPLOAD
# ============================================================

async def process_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):

    document = update.message.document

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            "❌ File is too large.\n"
            "Maximum allowed size is 50 MB."
        )
        return

    if not STORAGE_CHANNEL_ID:
        await update.message.reply_text(
            "❌ Storage channel is not configured.\n\n"
            "Owner needs to set STORAGE_CHANNEL_ID."
        )
        return

    try:

        storage_chat_id = int(STORAGE_CHANNEL_ID)

        sent = await context.bot.send_document(
            chat_id=storage_chat_id,
            document=document.file_id,
            caption=(
                "💻 Code Save and Share\n\n"
                f"File: {document.file_name}\n"
                f"Uploader ID: {update.effective_user.id}\n"
                f"Time: {now()}\n"
            ),
        )

        context.user_data["storage_message_id"] = sent.message_id
        context.user_data["storage_chat_id"] = str(storage_chat_id)
        context.user_data["file_name"] = document.file_name or "code"

        context.user_data["state"] = "waiting_title"

        await update.message.reply_text(
            "✅ File received and securely stored.\n\n"
            "📝 Now send a title for your code."
        )

    except Exception as e:

        logger.exception("Storage upload failed")

        await update.message.reply_text(
            "❌ Upload failed.\n\n"
            "Please check that the bot is an administrator "
            "of the private storage channel."
        )


# ============================================================
# FINISH UPLOAD
# ============================================================

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    title = context.user_data.get("title", "Untitled")
    description = context.user_data.get("description", "")
    language = context.user_data.get("language", "")
    file_name = context.user_data.get("file_name", "code")
    storage_chat_id = context.user_data.get("storage_chat_id")
    storage_message_id = context.user_data.get("storage_message_id")

    code_id = create_code_id()

    con = db()

    con.execute("""
        INSERT INTO codes
        (
            code_id,
            user_id,
            title,
            description,
            language,
            file_name,
            file_size,
            storage_chat_id,
            storage_message_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        code_id,
        user.id,
        title,
        description,
        language,
        file_name,
        0,
        storage_chat_id,
        storage_message_id,
        now(),
        now(),
    ))

    token = secrets.token_urlsafe(16)

    con.execute("""
        INSERT INTO share_links
        (
            code_id,
            token,
            created_by,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        code_id,
        token,
        user.id,
        now(),
    ))

    con.commit()
    con.close()

    log_activity(
        user.id,
        "UPLOAD_CODE",
        code_id,
        file_name,
    )

    bot_username = context.bot.username

    share_link = (
        f"https://t.me/{bot_username}?start=code_{token}"
    )

    context.user_data.clear()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 Share Code",
                url=share_link
            )
        ],
        [
            InlineKeyboardButton(
                "📋 My Codes",
                callback_data="my_codes"
            )
        ],
    ])

    await update.message.reply_text(
        "✅ *Code Saved Successfully!*\n\n"
        f"🆔 Code ID: `{code_id}`\n"
        f"📄 File: `{file_name}`\n"
        f"🏷️ Language: `{language}`\n\n"
        "🔗 Share this code using the button below.\n\n"
        f"👑 Owner {OWNER_USERNAME}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


def create_code_id():

    con = db()

    row = con.execute(
        "SELECT COUNT(*) FROM codes"
    ).fetchone()

    con.close()

    number = (row[0] if row else 0) + 1001

    return f"CODE-{number}"


def detect_language(filename):

    ext = os.path.splitext(filename.lower())[1]

    languages = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".html": "HTML",
        ".css": "CSS",
        ".json": "JSON",
        ".java": "Java",
        ".cpp": "C++",
        ".c": "C",
        ".php": "PHP",
        ".go": "Go",
        ".rs": "Rust",
        ".rb": "Ruby",
        ".kt": "Kotlin",
        ".swift": "Swift",
        ".txt": "Text",
    }

    return languages.get(ext, "Unknown")


# ============================================================
# RETRIEVE
# ============================================================

async def retrieve_by_token(update, context, token):

    con = db()

    row = con.execute("""
        SELECT
            c.code_id,
            c.user_id,
            c.title,
            c.description,
            c.language,
            c.file_name,
            c.storage_chat_id,
            c.storage_message_id,
            s.used_count,
            s.max_uses,
            s.is_active
        FROM share_links s
        JOIN codes c ON c.code_id=s.code_id
        WHERE s.token=?
          AND c.is_deleted=0
    """, (token,)).fetchone()

    if not row:
        con.close()

        await update.message.reply_text(
            "❌ This share link is invalid or expired."
        )
        return

    (
        code_id,
        owner_id,
        title,
        description,
        language,
        file_name,
        storage_chat_id,
        storage_message_id,
        used_count,
        max_uses,
        is_active,
    ) = row

    if not is_active:
        con.close()

        await update.message.reply_text(
            "🚫 This share link has been disabled."
        )
        return

    if max_uses and used_count >= max_uses:
        con.close()

        await update.message.reply_text(
            "🚫 This share link has reached its usage limit."
        )
        return

    con.execute("""
        UPDATE share_links
        SET used_count=used_count+1
        WHERE token=?
    """, (token,))

    con.execute("""
        INSERT INTO retrievals
        (code_id, share_token, user_id, retrieved_at)
        VALUES (?, ?, ?, ?)
    """, (
        code_id,
        token,
        update.effective_user.id,
        now(),
    ))

    con.commit()
    con.close()

    log_activity(
        update.effective_user.id,
        "RETRIEVE_CODE",
        code_id,
        file_name,
    )

    await send_storage_file(
        update,
        context,
        code_id,
        title,
        description,
        language,
        file_name,
        storage_chat_id,
        storage_message_id,
    )


async def retrieve_by_code_id(update, context, code_id):

    con = db()

    row = con.execute("""
        SELECT
            code_id,
            user_id,
            title,
            description,
            language,
            file_name,
            storage_chat_id,
            storage_message_id
        FROM codes
        WHERE code_id=?
          AND is_deleted=0
    """, (code_id,)).fetchone()

    con.close()

    if not row:
        await update.message.reply_text(
            "❌ Code not found."
        )
        return

    (
        code_id,
        owner_id,
        title,
        description,
        language,
        file_name,
        storage_chat_id,
        storage_message_id,
    ) = row

    log_activity(
        update.effective_user.id,
        "RETRIEVE_CODE_ID",
        code_id,
        file_name,
    )

    await send_storage_file(
        update,
        context,
        code_id,
        title,
        description,
        language,
        file_name,
        storage_chat_id,
        storage_message_id,
    )


async def send_storage_file(
    update,
    context,
    code_id,
    title,
    description,
    language,
    file_name,
    storage_chat_id,
    storage_message_id,
):

    try:

        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=int(storage_chat_id),
            message_id=int(storage_message_id),
        )

        await update.message.reply_text(
            "💻 *Code Information*\n\n"
            f"🆔 `{code_id}`\n"
            f"📝 {title}\n"
            f"📄 `{file_name}`\n"
            f"🏷️ {language}\n"
            + (
                f"\n📖 {description}\n"
                if description
                else ""
            )
            + f"\n👑 Owner {OWNER_USERNAME}",
            parse_mode="Markdown",
        )

    except Exception:

        logger.exception("Failed to copy storage message")

        await update.message.reply_text(
            "❌ Unable to retrieve this file right now."
        )


# ============================================================
# MY CODES
# ============================================================

async def show_my_codes(query, user_id):

    con = db()

    rows = con.execute("""
        SELECT code_id, title, file_name, language
        FROM codes
        WHERE user_id=?
          AND is_deleted=0
        ORDER BY id DESC
        LIMIT 50
    """, (user_id,)).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "📋 *My Codes*\n\n"
            "You have not uploaded any codes yet.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="home"
                )]
            ]),
        )
        return

    text = "📋 *MY CODES*\n\n"

    buttons = []

    for code_id, title, file_name, language in rows:

        text += (
            f"🆔 `{code_id}`\n"
            f"📝 {title}\n"
            f"📄 {file_name}\n"
            f"🏷️ {language}\n\n"
        )

        buttons.append([
            InlineKeyboardButton(
                f"📥 {code_id}",
                callback_data=f"view_{code_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="home"
        )
    ])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# CODE VIEW
# ============================================================

async def view_code(update, context):

    query = update.callback_query
    await query.answer()

    code_id = query.data.replace("view_", "", 1)

    con = db()

    row = con.execute("""
        SELECT
            code_id,
            user_id,
            title,
            description,
            language,
            file_name,
            storage_chat_id,
            storage_message_id
        FROM codes
        WHERE code_id=?
          AND is_deleted=0
    """, (code_id,)).fetchone()

    con.close()

    if not row:
        await query.edit_message_text(
            "❌ Code not found."
        )
        return

    (
        code_id,
        owner_id,
        title,
        description,
        language,
        file_name,
        storage_chat_id,
        storage_message_id,
    ) = row

    if owner_id != query.from_user.id:
        await query.edit_message_text(
            "🚫 This code does not belong to you."
        )
        return

    bot_username = context.bot.username

    con = db()

    share = con.execute("""
        SELECT token
        FROM share_links
        WHERE code_id=?
          AND is_active=1
        ORDER BY id DESC
        LIMIT 1
    """, (code_id,)).fetchone()

    con.close()

    if share:
        link = f"https://t.me/{bot_username}?start=code_{share[0]}"
    else:
        link = None

    buttons = []

    if link:
        buttons.append([
            InlineKeyboardButton(
                "🔗 Share Code",
                url=link
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "📋 My Codes",
            callback_data="my_codes"
        )
    ])

    await query.edit_message_text(
        "💻 *CODE DETAILS*\n\n"
        f"🆔 `{code_id}`\n"
        f"📝 {title}\n"
        f"📄 `{file_name}`\n"
        f"🏷️ {language}\n"
        + (
            f"📖 {description}\n"
            if description
            else ""
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# MY SHARES
# ============================================================

async def show_my_shares(query, user_id):

    con = db()

    rows = con.execute("""
        SELECT
            s.code_id,
            s.used_count,
            s.max_uses,
            s.is_active
        FROM share_links s
        WHERE s.created_by=?
        ORDER BY s.id DESC
        LIMIT 50
    """, (user_id,)).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "🔗 *My Share Links*\n\n"
            "No share links yet.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="home"
                )]
            ]),
        )
        return

    text = "🔗 *MY SHARE LINKS*\n\n"

    for code_id, used, max_uses, active in rows:

        status = "🟢 Active" if active else "🔴 Disabled"

        limit = (
            "♾️ Unlimited"
            if not max_uses
            else f"{max_uses} uses"
        )

        text += (
            f"🆔 `{code_id}`\n"
            f"{status}\n"
            f"📥 Retrieves: {used}\n"
            f"🔢 Limit: {limit}\n\n"
        )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 Back",
                callback_data="home"
            )]
        ]),
    )


# ============================================================
# PROFILE
# ============================================================

async def show_profile(query, user_id):

    con = db()

    user = con.execute("""
        SELECT username, first_name, last_name, joined_at
        FROM users
        WHERE user_id=?
    """, (user_id,)).fetchone()

    codes = con.execute("""
        SELECT COUNT(*)
        FROM codes
        WHERE user_id=?
          AND is_deleted=0
    """, (user_id,)).fetchone()[0]

    shares = con.execute("""
        SELECT COUNT(*)
        FROM share_links
        WHERE created_by=?
    """, (user_id,)).fetchone()[0]

    retrieves = con.execute("""
        SELECT COUNT(*)
        FROM retrievals
        WHERE user_id=?
    """, (user_id,)).fetchone()[0]

    con.close()

    username = (
        f"@{user[0]}"
        if user and user[0]
        else "Not set"
    )

    first_name = user[1] if user else "Unknown"

    await query.edit_message_text(
        "👤 *MY PROFILE*\n\n"
        f"Name: {first_name}\n"
        f"Username: {username}\n"
        f"ID: `{user_id}`\n\n"
        f"💻 Codes: {codes}\n"
        f"🔗 Shares: {shares}\n"
        f"📥 Retrieves: {retrieves}\n\n"
        f"👑 Owner {OWNER_USERNAME}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 Back",
                callback_data="home"
            )]
        ]),
    )


# ============================================================
# STATISTICS
# ============================================================

async def show_stats(query, user_id):

    con = db()

    codes = con.execute("""
        SELECT COUNT(*)
        FROM codes
        WHERE user_id=?
          AND is_deleted=0
    """, (user_id,)).fetchone()[0]

    shares = con.execute("""
        SELECT COUNT(*)
        FROM share_links
        WHERE created_by=?
    """, (user_id,)).fetchone()[0]

    retrieves = con.execute("""
        SELECT COUNT(*)
        FROM retrievals r
        JOIN codes c ON c.code_id=r.code_id
        WHERE c.user_id=?
    """, (user_id,)).fetchone()[0]

    con.close()

    await query.edit_message_text(
        "📊 *MY STATISTICS*\n\n"
        f"💻 Total Codes: {codes}\n"
        f"🔗 Total Share Links: {shares}\n"
        f"📥 Total Retrieves: {retrieves}\n\n"
        f"👑 Owner {OWNER_USERNAME}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 Back",
                callback_data="home"
            )]
        ]),
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=main_menu(),
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    logger.exception(
        "Unhandled error: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    if not STORAGE_CHANNEL_ID:
        logger.warning(
            "STORAGE_CHANNEL_ID is not configured."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    application.add_handler(
        CallbackQueryHandler(
            buttons,
            pattern="^(upload|my_codes|get_code|my_shares|profile|stats|help|home)$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            view_code,
            pattern=r"^view_CODE-\d+$"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            message_router
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_router
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Code Save and Share bot started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
