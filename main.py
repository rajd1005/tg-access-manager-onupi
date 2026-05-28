import os
import sqlite3
import datetime
import re
import smtplib
import threading
import asyncio
from email.message import EmailMessage
from logging import getLogger, INFO, basicConfig
from dotenv import load_dotenv
from telegram import Update, BotCommand, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatJoinRequestHandler,
    ChatMemberHandler, ContextTypes, ConversationHandler, filters
)

load_dotenv()
basicConfig(level=INFO)
logger = getLogger(__name__)

DB_PATH = "/app/data/access_control.db"

def get_bot_token():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    c = conn.cursor()
    try:
        c.execute("SELECT value FROM global_settings WHERE key='bot_token'")
        row = c.fetchone()
        if row and row[0]: return row[0]
    except Exception as e:
        logger.error(f"Error fetching bot token: {e}")
    finally: conn.close()
    return os.getenv("TELEGRAM_BOT_TOKEN")

TELEGRAM_BOT_TOKEN = get_bot_token()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
SMTP_SECURE = os.getenv("SMTP_SECURE", "true").lower() == "true"
SMTP_USER = os.getenv("SMTP_USER", "admin@rdalgo.com")
SMTP_PASS = os.getenv("SMTP_PASS", "")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
def get_ist_now(): return datetime.datetime.now(IST).replace(tzinfo=None)

def _send_email_sync(to_email, subject, body):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    c = conn.cursor()
    try:
        c.execute("SELECT value FROM global_settings WHERE key='site_title'")
        row = c.fetchone()
        site_title = row[0] if row else "TG Manager"
    except Exception as e:
        logger.error(f"Error fetching site title: {e}")
        site_title = "TG Manager"
    finally:
        conn.close()

    formatted_body = body.replace('\n', '<br>')
    
    html_template = f"""
    <html>
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f7f6; margin: 0; padding: 40px 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
            <div style="background-color: #2563eb; color: #ffffff; padding: 25px 30px; text-align: center;">
                <h1 style="margin: 0; font-size: 24px; font-weight: 700;">{site_title}</h1>
            </div>
            <div style="padding: 30px; color: #333333; line-height: 1.6; font-size: 16px;">
                <h2 style="color: #1e293b; margin-top: 0; font-size: 20px;">{subject}</h2>
                <p style="margin: 0;">{formatted_body}</p>
            </div>
            <div style="background-color: #f8fafc; padding: 20px 30px; text-align: center; color: #64748b; font-size: 13px; border-top: 1px solid #e2e8f0;">
                &copy; {datetime.datetime.now().year} {site_title}. All rights reserved.<br>
                This is an automated message, please do not reply directly.
            </div>
        </div>
    </body>
    </html>
    """

    try:
        msg = EmailMessage()
        msg.set_content("Please enable HTML to view this email.")
        msg.add_alternative(html_template, subtype='html')
        msg['Subject'] = subject
        msg['From'] = f"{site_title} <{SMTP_USER}>"
        msg['To'] = to_email
        if SMTP_PORT == 465 or SMTP_SECURE:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
    except Exception as e:
        logger.error(f"Bot SMTP Error: {e}")

def send_notification(to_email, subject, body):
    if not to_email: return
    threading.Thread(target=_send_email_sync, args=(to_email, subject, body)).start()

def format_tg_message(text):
    if not text: return ""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<u>\1</u>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!_)_(?!_)(.*?)(?<!_)_(?!_)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
    return text.strip()

def build_keyboard(btn_str):
    if not btn_str or not btn_str.strip(): return None
    keyboard = []
    for line in btn_str.split('\n'):
        if not line.strip(): continue
        row = []
        for btn_part in line.split('|'):
            if '-' in btn_part:
                parts = btn_part.split('-', 1)
                text = parts[0].strip()
                url = parts[1].strip()
                if not url.startswith('http'): url = 'https://' + url
                row.append(InlineKeyboardButton(text, url=url))
        if row:
            keyboard.append(row)
    return InlineKeyboardMarkup(keyboard) if keyboard else None

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS invite_links (id INTEGER PRIMARY KEY AUTOINCREMENT, invite_link TEXT UNIQUE, channel_id INTEGER, duration TEXT, status TEXT DEFAULT 'PENDING', max_uses INTEGER DEFAULT 1, used_count INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS joined_users (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, user_name TEXT, channel_id INTEGER, join_date TEXT, expiry_date TEXT, status TEXT DEFAULT 'ACTIVE', invite_link_used TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER UNIQUE, channel_name TEXT, bot_status TEXT DEFAULT 'ACTIVE', welcome_message TEXT DEFAULT '', farewell_message TEXT DEFAULT '', welcome_buttons TEXT DEFAULT '', farewell_buttons TEXT DEFAULT '', welcome_image TEXT DEFAULT '', farewell_image TEXT DEFAULT '')''')
    
    try: cursor.execute("ALTER TABLE channels ADD COLUMN agent_id INTEGER")
    except Exception: pass
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS web_users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'AGENT', expiry_date TEXT, invite_durations TEXT DEFAULT '1, 7, 30, lifetime', extend_durations TEXT DEFAULT '7, 30, lifetime', notify_join TEXT DEFAULT 'true', notify_leave TEXT DEFAULT 'true', reminder_sent TEXT DEFAULT 'false')''')
    try: cursor.execute("ALTER TABLE web_users ADD COLUMN agent_upi_id TEXT DEFAULT ''")
    except Exception: pass

    try: cursor.execute("ALTER TABLE customer_plans ADD COLUMN extra_emails TEXT DEFAULT ''")
    except Exception: pass
    try: cursor.execute("ALTER TABLE payment_links ADD COLUMN extra_emails TEXT DEFAULT ''")
    except Exception: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, duration_str TEXT, details TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (id TEXT PRIMARY KEY, agent_email TEXT, plan_id INTEGER, amount REAL, status TEXT, utr TEXT, created_at TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS received_sms (id INTEGER PRIMARY KEY AUTOINCREMENT, utr TEXT UNIQUE, amount REAL, timestamp TEXT, matched TEXT DEFAULT 'false')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS upi_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, upi_id TEXT UNIQUE, is_primary INTEGER DEFAULT 0)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS customer_plans (id TEXT PRIMARY KEY, agent_id INTEGER, channel_id INTEGER, plan_name TEXT, price REAL, duration_days INTEGER, description TEXT, is_active INTEGER DEFAULT 1)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS coupons (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, code TEXT, discount_pct INTEGER, max_uses INTEGER, used_count INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS customer_transactions (txn_id TEXT PRIMARY KEY, agent_id INTEGER, customer_email TEXT, plan_id TEXT, coupon_code TEXT, final_amount REAL, status TEXT, utr TEXT, tg_invite_link TEXT, created_at TEXT)''')

    # --- PERFORMANCE OPTIMIZATIONS (Database Indexes) ---
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_joined_users_expiry ON joined_users(expiry_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_joined_users_status ON joined_users(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_transactions_status ON customer_transactions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status)")

    cursor.execute('''CREATE TABLE IF NOT EXISTS global_settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('menu_message', '🤖 **Welcome {user_name}!**\\n\\nChoose an option from the menu below:')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('menu_buttons', '🌐 Website - https://example.com | 💬 Support - https://t.me/support')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('registration_enabled', 'true')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('trial_days', '3')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('upi_randomize', 'false')")
    
    conn.commit()
    conn.close()

async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "Open Main Menu")])

def _get_start_cmd_data():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_settings WHERE key='menu_message'")
    row_msg = cursor.fetchone()
    msg = row_msg[0] if row_msg else "Welcome!"
    
    cursor.execute("SELECT value FROM global_settings WHERE key='menu_buttons'")
    row_btns = cursor.fetchone()
    btns = row_btns[0] if row_btns else ""
    conn.close()
    return msg, btns

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.full_name if update.effective_user else "User"
    msg, btns = await asyncio.to_thread(_get_start_cmd_data)

    msg = msg.replace("{user_name}", user_name)
    btns = btns.replace("{user_name}", user_name)
    clean_msg = format_tg_message(msg)
    keyboard = build_keyboard(btns)
    
    try: 
        await update.message.reply_text(clean_msg, reply_markup=keyboard, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e: 
        logger.error(f"Error sending start menu to {user_name}: {e}")

def _update_bot_chat_member(channel_id, channel_name, bot_status):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT bot_status FROM channels WHERE channel_id=?", (channel_id,))
        row = cursor.fetchone()
        if row and row[0] == 'BLOCKED':
            return 'BLOCKED'

        cursor.execute("INSERT INTO channels (channel_id, channel_name, bot_status) VALUES (?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET channel_name=excluded.channel_name, bot_status=excluded.bot_status", (channel_id, channel_name, bot_status))
        conn.commit()
        return bot_status
    except Exception as e:
        logger.error(f"Error updating channel status in DB: {e}")
        return None
    finally: conn.close()

async def handle_bot_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm or cm.chat.type not in ["channel", "supergroup", "group"]: return
    
    bot_status = 'ACTIVE' if cm.new_chat_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER] else 'INACTIVE'
    status = await asyncio.to_thread(_update_bot_chat_member, cm.chat.id, cm.chat.title, bot_status)
    
    if status == 'BLOCKED':
        try:
            await context.bot.leave_chat(cm.chat.id)
        except Exception as e:
            logger.error(f"Error leaving blocked chat: {e}")

def _process_user_left_db(user_id, channel_id):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, user_name, expiry_date, status FROM joined_users WHERE user_id=? AND channel_id=? ORDER BY id DESC LIMIT 1", (user_id, channel_id))
        row = cursor.fetchone()
        if not row: return None
        db_id, user_name, expiry, current_db_status = row
        
        if current_db_status == 'ACTIVE':
            cursor.execute("UPDATE joined_users SET status = 'LEFT EARLY' WHERE id=?", (db_id,))
            
            cursor.execute("SELECT u.email, c.channel_name, u.notify_leave FROM web_users u JOIN channels c ON u.id = c.agent_id WHERE c.channel_id = ?", (channel_id,))
            agent_data = cursor.fetchone()
            
            cursor.execute("SELECT farewell_message, farewell_buttons, farewell_image, channel_name FROM channels WHERE channel_id=?", (channel_id,))
            chan_row = cursor.fetchone()
            
            conn.commit()
            return {'user_name': user_name, 'expiry': expiry, 'agent_data': agent_data, 'chan_row': chan_row}
        return None
    except Exception as e:
        logger.error(f"Error processing user left in DB: {e}")
        return None
    finally: conn.close()

def _save_new_invite_link(new_link, channel_id, duration):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO invite_links (invite_link, channel_id, duration, max_uses) VALUES (?, ?, ?, 1)", (new_link, channel_id, duration))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving generated invite link: {e}")
    finally: conn.close()

async def handle_user_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm or cm.chat.type not in ["channel", "supergroup", "group"]: return
    
    if cm.new_chat_member.status == ChatMember.LEFT:
        user_id, channel_id = cm.new_chat_member.user.id, cm.chat.id
        data = await asyncio.to_thread(_process_user_left_db, user_id, channel_id)
        
        if data:
            user_name, expiry, agent_data, chan_row = data['user_name'], data['expiry'], data['agent_data'], data['chan_row']
            
            if agent_data and agent_data[2] == 'true':
                send_notification(agent_data[0], f"User Left: {agent_data[1]}", f"{user_name} (ID: {user_id}) has left {agent_data[1]} early.")
            
            if chan_row and chan_row[0]:
                f_msg = chan_row[0].replace("{user_name}", user_name).replace("{channel_name}", chan_row[3])
                f_btns = (chan_row[1] or "").replace("{user_name}", user_name).replace("{channel_name}", chan_row[3])
                f_img = chan_row[2]
                
                if "{rejoin_link}" in f_msg or "{rejoin_link}" in f_btns:
                    duration = '1'
                    if expiry == 'lifetime': duration = 'lifetime'
                    else:
                        try:
                            rem = (datetime.datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S") - get_ist_now()).days
                            if rem > 0: duration = str(rem)
                        except Exception: pass
                    try:
                        res = await context.bot.create_chat_invite_link(chat_id=channel_id, creates_join_request=True)
                        new_link = res.invite_link
                        await asyncio.to_thread(_save_new_invite_link, new_link, channel_id, duration)
                        f_msg = f_msg.replace("{rejoin_link}", new_link)
                        f_btns = f_btns.replace("{rejoin_link}", new_link)
                    except Exception as e: 
                        logger.error(f"Error generating rejoin link: {e}")
                
                f_msg_clean = format_tg_message(f_msg)
                keyboard = build_keyboard(f_btns)
                
                try:
                    if f_img and os.path.exists(f_img):
                        with open(f_img, 'rb') as f:
                            await context.bot.send_photo(chat_id=user_id, photo=f, caption=f_msg_clean, reply_markup=keyboard, parse_mode='HTML')
                    else:
                        await context.bot.send_message(chat_id=user_id, text=f_msg_clean, reply_markup=keyboard, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Error sending farewell message to {user_id}: {e}")

def _get_expired_users_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    try:
        now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("SELECT id, user_id, channel_id, user_name FROM joined_users WHERE status = 'ACTIVE' AND expiry_date != 'lifetime' AND expiry_date <= ?", (now,))
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error fetching expired users: {e}")
        return []
    finally: conn.close()

def _process_expired_user_db(row_id, channel_id):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE joined_users SET status = 'EXPIRED' WHERE id = ?", (row_id,))
        cursor.execute("SELECT u.email, c.channel_name, u.notify_leave FROM web_users u JOIN channels c ON u.id = c.agent_id WHERE c.channel_id = ?", (channel_id,))
        agent_data = cursor.fetchone()
        cursor.execute("SELECT farewell_message, farewell_buttons, farewell_image, channel_name FROM channels WHERE channel_id=?", (channel_id,))
        chan_row = cursor.fetchone()
        conn.commit()
        return agent_data, chan_row
    except Exception as e:
        logger.error(f"Error processing expired user in DB: {e}")
        return None, None
    finally: conn.close()

async def auto_revoke_job(context: ContextTypes.DEFAULT_TYPE):
    expired_users = await asyncio.to_thread(_get_expired_users_db)
    
    for row_id, user_id, channel_id, user_name in expired_users:
        try:
            agent_data, chan_row = await asyncio.to_thread(_process_expired_user_db, row_id, channel_id)
            
            if agent_data and agent_data[2] == 'true':
                send_notification(agent_data[0], f"User Expired: {agent_data[1]}", f"{user_name} (ID: {user_id})'s access to {agent_data[1]} has expired. They have been removed.")
            
            if chan_row and chan_row[0]:
                f_msg = chan_row[0].replace("{user_name}", user_name).replace("{channel_name}", chan_row[3])
                f_btns = (chan_row[1] or "").replace("{user_name}", user_name).replace("{channel_name}", chan_row[3])
                f_img = chan_row[2]
                
                if "{rejoin_link}" in f_msg or "{rejoin_link}" in f_btns:
                    try:
                        res = await context.bot.create_chat_invite_link(chat_id=channel_id, creates_join_request=True)
                        new_link = res.invite_link
                        await asyncio.to_thread(_save_new_invite_link, new_link, channel_id, '30')
                        f_msg = f_msg.replace("{rejoin_link}", new_link)
                        f_btns = f_btns.replace("{rejoin_link}", new_link)
                    except Exception as e:
                        logger.error(f"Error generating automated rejoin link: {e}")
                
                f_msg_clean = format_tg_message(f_msg)
                keyboard = build_keyboard(f_btns)
                
                try:
                    if f_img and os.path.exists(f_img):
                        with open(f_img, 'rb') as f:
                            await context.bot.send_photo(chat_id=user_id, photo=f, caption=f_msg_clean, reply_markup=keyboard, parse_mode='HTML')
                    else:
                        await context.bot.send_message(chat_id=user_id, text=f_msg_clean, reply_markup=keyboard, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Error sending expiration farewell message: {e}")

            await context.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
        except Exception as e:
            logger.error(f"Error processing revocation for user {user_id}: {e}")

def _get_expiring_agents_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    try:
        target_7_days = (get_ist_now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        cursor.execute("SELECT id, email FROM web_users WHERE role='AGENT' AND expiry_date=? AND reminder_sent='false'", (target_7_days,))
        agents_to_remind = cursor.fetchall()
        for agent_id, _ in agents_to_remind:
            cursor.execute("UPDATE web_users SET reminder_sent='true' WHERE id=?", (agent_id,))
        conn.commit()
        return agents_to_remind
    except Exception as e: 
        logger.error(f"Agent Expiry Job DB Error: {e}")
        return []
    finally: conn.close()

async def agent_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    agents_to_remind = await asyncio.to_thread(_get_expiring_agents_db)
    for agent_id, email in agents_to_remind:
        send_notification(email, "Warning: Account Expiring Soon", "Your TG Manager agent account will expire in exactly 7 days.\n\nPlease log in and renew your plan to avoid interruption. If your account expires, all your channels and user data will be permanently deleted.")

def _process_join_request_db(invite_link, channel_id, user_id, user_name):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, duration, status, max_uses, used_count FROM invite_links WHERE invite_link = ?", (invite_link,))
        row = cursor.fetchone()
        
        if row and row[2] == 'PENDING':
            link_id, duration, max_uses, used_count = row[0], row[1], row[3], row[4]
            
            cursor.execute("SELECT welcome_message, welcome_buttons, welcome_image, channel_name FROM channels WHERE channel_id = ?", (channel_id,))
            chan_row = cursor.fetchone()
            
            cursor.execute("SELECT u.email, c.channel_name, u.notify_join FROM web_users u JOIN channels c ON u.id = c.agent_id WHERE c.channel_id = ?", (channel_id,))
            agent_data = cursor.fetchone()

            now_obj = get_ist_now()
            join_date = now_obj.strftime("%Y-%m-%d %H:%M:%S")
            expiry_date = 'lifetime' if duration == 'lifetime' else (now_obj + datetime.timedelta(days=int(duration))).strftime("%Y-%m-%d %H:%M:%S")
            
            cursor.execute("INSERT INTO joined_users (user_id, user_name, channel_id, join_date, expiry_date, invite_link_used) VALUES (?, ?, ?, ?, ?, ?)", (user_id, user_name, channel_id, join_date, expiry_date, invite_link))
            
            used_count += 1
            should_revoke_link = False
            if max_uses > 0 and used_count >= max_uses:
                cursor.execute("UPDATE invite_links SET status = 'USED', used_count = ? WHERE id = ?", (used_count, link_id))
                should_revoke_link = True
            else: 
                cursor.execute("UPDATE invite_links SET used_count = ? WHERE id = ?", (used_count, link_id))
            
            conn.commit()
            return True, chan_row, agent_data, should_revoke_link
        return False, None, None, False
    except Exception as e:
        logger.error(f"Error processing join request in DB: {e}")
        return False, None, None, False
    finally: conn.close()

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_request = update.chat_join_request
    if not join_request.invite_link: return
    
    invite_link = join_request.invite_link.invite_link
    user_id, user_name, channel_id = join_request.from_user.id, join_request.from_user.full_name, join_request.chat.id
    
    is_valid, chan_row, agent_data, should_revoke_link = await asyncio.to_thread(_process_join_request_db, invite_link, channel_id, user_id, user_name)
    
    if is_valid:
        try:
            if chan_row and chan_row[0]:
                w_msg = chan_row[0].replace("{user_name}", user_name).replace("{channel_name}", chan_row[3])
                w_btns = (chan_row[1] or "").replace("{user_name}", user_name).replace("{channel_name}", chan_row[3])
                w_img = chan_row[2]
                w_msg_clean = format_tg_message(w_msg)
                keyboard = build_keyboard(w_btns)
                
                try:
                    if w_img and os.path.exists(w_img):
                        with open(w_img, 'rb') as f:
                            await context.bot.send_photo(chat_id=user_id, photo=f, caption=w_msg_clean, reply_markup=keyboard, parse_mode='HTML')
                    else:
                        await context.bot.send_message(chat_id=user_id, text=w_msg_clean, reply_markup=keyboard, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Error sending welcome message: {e}")

            await join_request.approve()
            
            if agent_data and agent_data[2] == 'true':
                send_notification(agent_data[0], f"New User Joined: {agent_data[1]}", f"Great news! {user_name} (ID: {user_id}) has joined {agent_data[1]} via an invite link.")
            
            if should_revoke_link:
                try: 
                    await context.bot.revoke_chat_invite_link(chat_id=channel_id, invite_link=invite_link)
                except Exception as e:
                    logger.error(f"Error revoking invite link from Telegram API: {e}")
                    
        except Exception as e:
            logger.error(f"Error during join request approval process: {e}")
    else: 
        try:
            await join_request.decline()
        except Exception as e:
            logger.error(f"Error declining join request: {e}")

def _db_cleanup_sync():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    try:
        now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("DELETE FROM sessions WHERE expiry < ?", (now_str,))
        cursor.execute("DELETE FROM otps WHERE expiry < ?", (now_str,))
        conn.commit()
    except Exception as e:
        logger.error(f"DB Cleanup Error: {e}")
    finally:
        conn.close()

async def db_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """Safely cleans up expired user sessions and OTPs to prevent database bloat over time."""
    await asyncio.to_thread(_db_cleanup_sync)

async def check_restart_flag(context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists("/app/data/restart.flag"):
        try: 
            os.remove("/app/data/restart.flag")
        except Exception as e: 
            logger.error(f"Error removing restart flag: {e}")
        logger.info("Restart flag detected via Admin Panel. Exiting process...")
        os._exit(0)

def main():
    init_db()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(ChatJoinRequestHandler(handle_join_request))
    application.add_handler(ChatMemberHandler(handle_bot_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(handle_user_left, ChatMemberHandler.CHAT_MEMBER))
    
    application.job_queue.run_repeating(check_restart_flag, interval=3)
    application.job_queue.run_repeating(auto_revoke_job, interval=60, first=10)
    application.job_queue.run_repeating(agent_expiry_job, interval=3600, first=30) 
    application.job_queue.run_repeating(db_cleanup_job, interval=86400, first=10)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__': main()