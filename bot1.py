import os, sqlite3, logging, asyncio
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Telegram numeric Chat ID (Render Environment Variable se liya jayega)
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()
try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW) if OWNER_CHAT_ID_RAW else 0
except ValueError:
    OWNER_CHAT_ID = 0
DB_PATH = os.path.join(os.getcwd(), "support.db")
PORT = int(os.environ.get("PORT", 10000))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("KRUTIK_SUPPORT")

web = Flask(__name__)
@web.get("/")
def home(): return "⚡ KRUTIK CYBER EXPERT — TICKET SUPPORT BOT"
@web.get("/health")
def health(): return "OK"
def run_web(): web.run(host="0.0.0.0", port=PORT)

def conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    c=conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,name TEXT,username TEXT,joined_at TEXT,last_active TEXT,blocked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,service TEXT,details TEXT,status TEXT DEFAULT 'pending',reason TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,category TEXT,priority TEXT DEFAULT 'normal',subject TEXT,status TEXT DEFAULT 'open',assigned_admin INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS ticket_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id INTEGER,sender_id INTEGER,sender_type TEXT,message_type TEXT,text TEXT,telegram_message_id INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY,role TEXT,added_at TEXT);
    CREATE TABLE IF NOT EXISTS activity_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id INTEGER,action TEXT,details TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
    """); c.commit(); c.close()

def upsert(u):
    c=conn(); old=c.execute("SELECT user_id FROM users WHERE user_id=?",(u.id,)).fetchone()
    if old: c.execute("UPDATE users SET name=?,username=?,last_active=? WHERE user_id=?",(u.full_name,u.username,now(),u.id))
    else: c.execute("INSERT INTO users VALUES(?,?,?,?,?,0)",(u.id,u.full_name,u.username,now(),now()))
    c.commit(); c.close()
def blocked(uid):
    c=conn(); r=c.execute("SELECT blocked FROM users WHERE user_id=?",(uid,)).fetchone(); c.close()
    return bool(r and r["blocked"])
def admin(uid):
    if uid==OWNER_CHAT_ID:return True
    c=conn(); r=c.execute("SELECT 1 FROM admins WHERE user_id=?",(uid,)).fetchone(); c.close(); return bool(r)
def activity(uid,a,d=""):
    c=conn(); c.execute("INSERT INTO activity_logs(actor_id,action,details,created_at) VALUES(?,?,?,?)",(uid,a,d,now())); c.commit(); c.close()
def setting(k,d="0"):
    c=conn(); r=c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone(); c.close(); return r["value"] if r else d
def setsetting(k,v):
    c=conn(); c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,str(v))); c.commit(); c.close()

def main_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("📝 Apply / Request"),KeyboardButton("🎫 Create Ticket")],
                                [KeyboardButton("📋 My Requests"),KeyboardButton("🎫 My Tickets")],
                                [KeyboardButton("👤 My Profile")]],resize_keyboard=True)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard",callback_data="adm:dash"),InlineKeyboardButton("👥 Users",callback_data="adm:users")],
        [InlineKeyboardButton("📝 Requests",callback_data="adm:req"),InlineKeyboardButton("🎫 Tickets",callback_data="adm:tickets")],
        [InlineKeyboardButton("📈 Statistics",callback_data="adm:stats"),InlineKeyboardButton("📋 Activity",callback_data="adm:activity")],
        [InlineKeyboardButton("🚀 Bot Hosting",callback_data="adm:service:hosting"),InlineKeyboardButton("🔐 Private Chat Bot",callback_data="adm:service:private")],
        [InlineKeyboardButton("📢 Broadcast",callback_data="adm:broadcast"),InlineKeyboardButton("⚙️ Settings",callback_data="adm:settings")]
    ])

async def start(update,ctx):
    u=update.effective_user; upsert(u); ctx.user_data.clear()
    if blocked(u.id): return await update.message.reply_text("🚫 You are blocked.")
    if setting("maintenance")=="1" and not admin(u.id): return await update.message.reply_text("🛠 Maintenance mode is active.")
    await update.message.reply_text(f"⚡ KRUTIK CYBER EXPERT\n\nWelcome to Support & Approval Center.\n🆔 Your Chat ID: `{u.id}`",parse_mode="Markdown",reply_markup=main_kb())

async def apply(update,ctx):
    ctx.user_data["state"]="service"
    await update.message.reply_text("📝 Select service:",reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Bot Hosting",callback_data="service:hosting")],
        [InlineKeyboardButton("🔐 Private Chat Bot",callback_data="service:private")],
        [InlineKeyboardButton("🎫 Other / Custom",callback_data="service:custom")],
        [InlineKeyboardButton("❌ Cancel",callback_data="cancel")]]))

async def service(update,ctx):
    q=update.callback_query; await q.answer()
    name={"hosting":"🚀 Bot Hosting","private":"🔐 Private Chat Bot","custom":"🎫 Other / Custom"}[q.data.split(":")[1]]
    ctx.user_data.update(state="request",service=name)
    await q.edit_message_text(f"🤖 {name}\n\nRequirement/problem detail bhejo.\n/cancel to cancel.")

async def create_request(update,ctx):
    u=update.effective_user; text=update.message.text or update.message.caption or f"[{update.message.content_type}]"
    c=conn(); cur=c.execute("INSERT INTO requests(user_id,service,details,created_at,updated_at) VALUES(?,?,?,?,?)",(u.id,ctx.user_data["service"],text[:4000],now(),now())); rid=cur.lastrowid; c.commit(); c.close()
    activity(u.id,"create_request",str(rid)); ctx.user_data.clear()
    await update.message.reply_text(f"🆕 Request #{rid} created.\n⏳ Status: Pending\n\n👑 Admin ko notify kar diya.",reply_markup=main_kb())
    await ctx.bot.send_message(OWNER_CHAT_ID,f"🆕 NEW REQUEST #{rid}\n\n👤 {u.full_name}\n🔹 @{u.username or 'N/A'}\n🆔 {u.id}\n🤖 {ctx.user_data.get('service','Service')}\n\n📋 {text[:3000]}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Request",callback_data=f"req:view:{rid}")]]))

async def ticket_start(update,ctx):
    ctx.user_data["state"]="cat"
    await update.message.reply_text("🎫 Select category:",reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 Technical",callback_data="cat:Technical")],[InlineKeyboardButton("💳 Payment",callback_data="cat:Payment")],
        [InlineKeyboardButton("📦 Service",callback_data="cat:Service")],[InlineKeyboardButton("🚨 Complaint",callback_data="cat:Complaint")],
        [InlineKeyboardButton("❓ General",callback_data="cat:General")],[InlineKeyboardButton("❌ Cancel",callback_data="cancel")]]))

async def category(update,ctx):
    q=update.callback_query; await q.answer(); ctx.user_data.update(state="priority",category=q.data.split(":",1)[1])
    await q.edit_message_text("🎯 Select priority:",reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Low",callback_data="pri:low")],[InlineKeyboardButton("🟡 Normal",callback_data="pri:normal")],
        [InlineKeyboardButton("🟠 High",callback_data="pri:high")],[InlineKeyboardButton("🔴 Urgent",callback_data="pri:urgent")]]))

async def priority(update,ctx):
    q=update.callback_query; await q.answer(); ctx.user_data.update(state="subject",priority=q.data.split(":")[1])
    await q.edit_message_text("📝 Send ticket subject:")

async def ticket_subject(update,ctx):
    if not update.message.text:return await update.message.reply_text("Please send subject as text.")
    ctx.user_data.update(state="ticket_message",subject=update.message.text[:300])
    await update.message.reply_text("💬 Send your problem/details:")

async def create_ticket(update,ctx):
    u=update.effective_user; text=update.message.text or update.message.caption or f"[{update.message.content_type}]"
    c=conn(); cur=c.execute("INSERT INTO tickets(user_id,category,priority,subject,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (u.id,ctx.user_data["category"],ctx.user_data["priority"],ctx.user_data["subject"],now(),now())); tid=cur.lastrowid
    c.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_type,message_type,text,telegram_message_id,created_at) VALUES(?,?,?,?,?,?,?)",
        (tid,u.id,"user",update.message.content_type,text[:4000],update.message.message_id,now())); c.commit(); c.close()
    activity(u.id,"create_ticket",str(tid)); cat=ctx.user_data["category"]; pri=ctx.user_data["priority"]; sub=ctx.user_data["subject"]; ctx.user_data.clear()
    await update.message.reply_text(f"🎫 #TK-{tid:04d} created!\n📂 {cat}\n🎯 {pri.title()}\n🟢 Open",reply_markup=main_kb())
    await ctx.bot.send_message(OWNER_CHAT_ID,f"🎫 NEW TICKET #TK-{tid:04d}\n\n👤 {u.full_name}\n🆔 {u.id}\n📂 {cat}\n🎯 {pri.upper()}\n📝 {sub}\n\n💬 {text[:2500]}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Reply",callback_data=f"reply:{tid}"),InlineKeyboardButton("🔒 Close",callback_data=f"ticket:close:{tid}")]]))

async def my_requests(update,ctx):
    c=conn(); rows=c.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 20",(update.effective_user.id,)).fetchall(); c.close()
    await update.message.reply_text("📋 MY REQUESTS\n\n"+("".join(f"#{r['id']} | {r['service']} | {r['status']}\n" for r in rows) or "No requests."))

async def my_tickets(update,ctx):
    c=conn(); rows=c.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 20",(update.effective_user.id,)).fetchall(); c.close()
    if not rows:return await update.message.reply_text("🎫 No tickets.")
    for t in rows:
        await update.message.reply_text(f"🎫 #TK-{t['id']:04d}\n📝 {t['subject']}\n📂 {t['category']}\n🎯 {t['priority'].title()}\n📌 {t['status']}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open",callback_data=f"ticket:view:{t['id']}")],
                                                [InlineKeyboardButton("🔒 Close",callback_data=f"ticket:close:{t['id']}")]]))

async def profile(update,ctx):
    u=update.effective_user; c=conn()
    r=c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=?",(u.id,)).fetchone()["n"]; t=c.execute("SELECT COUNT(*) n FROM tickets WHERE user_id=?",(u.id,)).fetchone()["n"]; c.close()
    await update.message.reply_text(f"👤 MY PROFILE\n\n📛 {u.full_name}\n🔹 @{u.username or 'N/A'}\n🆔 {u.id}\n📝 Requests: {r}\n🎫 Tickets: {t}")

async def admin(update,ctx):
    if not admin(update.effective_user.id):return await update.message.reply_text("🚫 Admin only.")
    await update.message.reply_text("👑 KRUTIK ADMIN PANEL",reply_markup=admin_kb())

async def adm_cb(update,ctx):
    q=update.callback_query
    if not admin(q.from_user.id):return await q.answer("🚫 Admin only.",show_alert=True)
    await q.answer(); d=q.data
    if d=="adm:dash":
        c=conn(); vals=[c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],c.execute("SELECT COUNT(*) n FROM requests WHERE status='pending'").fetchone()["n"],c.execute("SELECT COUNT(*) n FROM tickets WHERE status='open'").fetchone()["n"],c.execute("SELECT COUNT(*) n FROM tickets WHERE status='open' AND priority='urgent'").fetchone()["n"]]; c.close()
        return await q.edit_message_text(f"📊 DASHBOARD\n\n👥 Users: {vals[0]}\n📝 Pending Requests: {vals[1]}\n🎫 Open Tickets: {vals[2]}\n🔴 Urgent: {vals[3]}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:home":return await q.edit_message_text("👑 ADMIN PANEL",reply_markup=admin_kb())
    if d=="adm:users":
        c=conn(); rows=c.execute("SELECT * FROM users ORDER BY last_active DESC LIMIT 25").fetchall(); c.close()
        text="👥 USERS\n\n"+"".join(f"👤 {r['name']} | 🆔 {r['user_id']} | {'🚫' if r['blocked'] else '🟢'}\n" for r in rows)
        return await q.edit_message_text(text[:4000],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:req":
        c=conn(); rows=c.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 25").fetchall(); c.close()
        return await q.edit_message_text("📝 REQUESTS\n\n"+("".join(f"#{r['id']} | {r['service']} | {r['status']} | User {r['user_id']}\n" for r in rows) or "No requests."),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:tickets":
        c=conn(); rows=c.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT 25").fetchall(); c.close()
        return await q.edit_message_text("🎫 TICKETS\n\n"+("".join(f"#TK-{r['id']:04d} | {r['priority']} | {r['status']} | User {r['user_id']}\n" for r in rows) or "No tickets."),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:stats":
        c=conn(); a=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]; b=c.execute("SELECT COUNT(*) n FROM requests").fetchone()["n"]; t=c.execute("SELECT COUNT(*) n FROM tickets").fetchone()["n"]; cl=c.execute("SELECT COUNT(*) n FROM tickets WHERE status='closed'").fetchone()["n"]; c.close()
        return await q.edit_message_text(f"📈 STATISTICS\n\n👥 Users: {a}\n📝 Requests: {b}\n🎫 Tickets: {t}\n✅ Closed: {cl}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:activity":
        c=conn(); rows=c.execute("SELECT * FROM activity_logs ORDER BY id DESC LIMIT 20").fetchall(); c.close()
        return await q.edit_message_text("📋 ACTIVITY\n\n"+"".join(f"#{r['id']} {r['action']} | {r['actor_id']}\n" for r in rows),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:settings":
        return await q.edit_message_text(f"⚙️ Maintenance: {'ON' if setting('maintenance')=='1' else 'OFF'}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Toggle Maintenance",callback_data="adm:maint")],[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))
    if d=="adm:maint":
        setsetting("maintenance","0" if setting("maintenance")=="1" else "1"); return await q.edit_message_text("⚙️ Setting updated.",reply_markup=admin_kb())
    if d=="adm:broadcast":
        ctx.user_data["state"]="broadcast"; return await q.edit_message_text("📢 Send broadcast message. /cancel to cancel.")
    if d.startswith("adm:service:"):
        service="🚀 Bot Hosting" if d.endswith("hosting") else "🔐 Private Chat Bot"; c=conn(); rows=c.execute("SELECT * FROM requests WHERE service=? ORDER BY id DESC LIMIT 20",(service,)).fetchall(); c.close()
        return await q.edit_message_text(f"{service}\n\n"+"".join(f"#{r['id']} | User {r['user_id']} | {r['status']}\n" for r in rows) or "No requests.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data="adm:home")]]))

async def req_cb(update,ctx):
    q=update.callback_query; await q.answer()
    if not admin(q.from_user.id, context):
    return
    _,action,rid=q.data.split(":"); rid=int(rid); c=conn(); r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
    if not r:return await q.edit_message_text("Request not found.")
    if action=="view":
        return await q.edit_message_text(f"📝 REQUEST #{rid}\n\n👤 {r['user_id']}\n🤖 {r['service']}\n📌 {r['status']}\n\n{r['details']}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve",callback_data=f"req:approve:{rid}"),InlineKeyboardButton("❌ Reject",callback_data=f"req:reject:{rid}")],[InlineKeyboardButton("🎫 Create Ticket",callback_data=f"req:ticket:{rid}")]]))
    if action=="approve":
        c.execute("UPDATE requests SET status='approved',updated_at=? WHERE id=?",(now(),rid));c.commit();c.close()
        await ctx.bot.send_message(r["user_id"],f"🎉 Request #{rid} approved!\n🤖 {r['service']}");return await q.edit_message_text(f"✅ Request #{rid} approved.")
    if action=="reject":
        c.close();ctx.user_data.update(state="reject",rid=rid);return await q.message.reply_text(f"❌ Send rejection reason for #{rid}:")
    if action=="ticket":
        c.execute("INSERT INTO tickets(user_id,category,priority,subject,created_at,updated_at) VALUES(?,?,?,?,?,?)",(r["user_id"],r["service"],"normal",f"Request #{rid}",now(),now()));tid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute("UPDATE requests SET status='ticket_created',updated_at=? WHERE id=?",(now(),rid));c.commit();c.close()
        await ctx.bot.send_message(r["user_id"],f"🎫 Ticket #TK-{tid:04d} created from request #{rid}.");return await q.edit_message_text(f"🎫 Ticket #TK-{tid:04d} created.")

async def ticket_cb(update,ctx):
    q=update.callback_query; await q.answer(); p=q.data.split(":"); action=p[1];tid=int(p[2]);c=conn();t=c.execute("SELECT * FROM tickets WHERE id=?",(tid,)).fetchone()
    if not t:return
    if not admin(q.from_user.id) and t["user_id"]!=q.from_user.id:return
    if action in ("view","open"):
        rows=c.execute("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id ASC LIMIT 40",(tid,)).fetchall();c.close()
        text=f"🎫 #TK-{tid:04d}\n📝 {t['subject']}\n📂 {t['category']}\n🎯 {t['priority'].title()}\n📌 {t['status']}\n\n"+"".join(("👑 Admin" if r["sender_type"]=="admin" else "👤 User")+f": {r['text']}\n\n" for r in rows)
        buttons=[[InlineKeyboardButton("💬 Reply",callback_data=f"reply:{tid}")]]
        if t["status"]=="closed":buttons.append([InlineKeyboardButton("🔄 Reopen",callback_data=f"ticket:reopen:{tid}")])
        else:buttons.append([InlineKeyboardButton("🔒 Close",callback_data=f"ticket:close:{tid}")])
        return await q.edit_message_text(text[:4000],reply_markup=InlineKeyboardMarkup(buttons))
    if action in ("close","reopen"):
        new="closed" if action=="close" else "open";c.execute("UPDATE tickets SET status=?,updated_at=? WHERE id=?",(new,now(),tid));c.commit();c.close()
        await ctx.bot.send_message(t["user_id"],f"{'🔒 Closed' if new=='closed' else '🔄 Reopened'}: #TK-{tid:04d}")
        return await q.edit_message_text(f"{'🔒 Closed' if new=='closed' else '🔄 Reopened'} #TK-{tid:04d}")

async def reply_cb(update,ctx):
    q=update.callback_query;await q.answer()
    if not admin(q.from_user.id):return
    tid=int(q.data.split(":")[1]);ctx.user_data.update(state="reply",tid=tid);await q.message.reply_text(f"💬 Send reply for #TK-{tid:04d}.")

async def broadcast(update,ctx):
    text=update.message.text or update.message.caption or f"[{update.message.content_type}]";c=conn();users=c.execute("SELECT user_id FROM users WHERE blocked=0").fetchall();c.close();sent=fail=0
    for r in users:
        try:await update.message.copy(chat_id=r["user_id"]);sent+=1
        except Exception:fail+=1
        await asyncio.sleep(.03)
    ctx.user_data.clear();await update.message.reply_text(f"📢 Done\n✅ {sent}\n❌ {fail}")

async def router(update,ctx):
    if not update.message:return
    u=update.effective_user;upsert(u)
    if blocked(u.id):return await update.message.reply_text("🚫 You are blocked.")
    state=ctx.user_data.get("state")
    if state=="request":return await create_request(update,ctx)
    if state=="ticket_subject":return await ticket_subject(update,ctx)
    if state=="ticket_message":return await create_ticket(update,ctx)
    if state=="reply" and admin(u.id):
        tid=ctx.user_data["tid"];text=update.message.text or update.message.caption or f"[{update.message.content_type}]";c=conn();t=c.execute("SELECT * FROM tickets WHERE id=?",(tid,)).fetchone();c.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_type,message_type,text,telegram_message_id,created_at) VALUES(?,?,?,?,?,?,?)",(tid,u.id,"admin",update.message.content_type,text[:4000],update.message.message_id,now()));c.execute("UPDATE tickets SET status='open',updated_at=? WHERE id=?",(now(),tid));c.commit();c.close();ctx.user_data.clear();await ctx.bot.send_message(t["user_id"],f"💬 Admin replied on #TK-{tid:04d}:\n\n{text[:3500]}");return await update.message.reply_text("✅ Reply sent.")
    if state=="reject" and admin(u.id):
        rid=ctx.user_data["rid"];reason=update.message.text or update.message.caption or "No reason";c=conn();r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone();c.execute("UPDATE requests SET status='rejected',reason=?,updated_at=? WHERE id=?",(reason[:2000],now(),rid));c.commit();c.close();ctx.user_data.clear();await ctx.bot.send_message(r["user_id"],f"❌ Request #{rid} rejected.\n\nReason: {reason[:2000]}");return await update.message.reply_text("❌ Rejected.")
    if state=="broadcast" and admin(u.id):return await broadcast(update,ctx)
    if update.message.text=="📝 Apply / Request":return await apply(update,ctx)
    if update.message.text=="🎫 Create Ticket":return await ticket_start(update,ctx)
    if update.message.text=="📋 My Requests":return await my_requests(update,ctx)
    if update.message.text=="🎫 My Tickets":return await my_tickets(update,ctx)
    if update.message.text=="👤 My Profile":return await profile(update,ctx)
    if is_admin(u.id):await update.message.reply_text("👑 Use /admin for admin panel.")
    else:await update.message.reply_text("Main menu se option select karo.",reply_markup=main_kb())

async def cancel(update,ctx):
    ctx.user_data.clear();await update.message.reply_text("❌ Cancelled.",reply_markup=main_kb())

def main():
    init_db();Thread(target=run_web,daemon=True).start()
    app=Application.builder().token(BOT_TOKEN).build()
    for cmd,fn in [("start",start),("admin",admin),("cancel",cancel)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CommandHandler("approve",lambda u,c: u.message.reply_text("Use Admin Panel to approve requests.")))
    app.add_handler(CallbackQueryHandler(service,pattern=r"^service:"))
    app.add_handler(CallbackQueryHandler(category,pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(priority,pattern=r"^pri:"))
    app.add_handler(CallbackQueryHandler(adm_cb,pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(req_cb,pattern=r"^req:"))
    app.add_handler(CallbackQueryHandler(ticket_cb,pattern=r"^ticket:"))
    app.add_handler(CallbackQueryHandler(reply_cb,pattern=r"^reply:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,router))
    log.info("⚡ KRUTIK CYBER EXPERT Ticket Support Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":main()
