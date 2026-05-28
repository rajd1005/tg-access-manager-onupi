import os
import sqlite3
import datetime
import secrets
import requests
import re
import base64
import smtplib
import random
from email.message import EmailMessage
import hashlib
import uuid
from fastapi import FastAPI, Depends, HTTPException, status, Request, Response, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from dotenv import load_dotenv

load_dotenv()
DB_PATH = "/app/data/access_control.db"

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
def get_ist_now(): return datetime.datetime.now(IST).replace(tzinfo=None)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def get_db_connection(): 
    conn = sqlite3.connect(DB_PATH, timeout=20)
    # Enable WAL mode for high-concurrency read/writes
    conn.execute("PRAGMA journal_mode=WAL;") 
    return conn

def hash_password(password: str):
    return hashlib.sha256(password.encode()).hexdigest()

def init_auth_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS web_users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'AGENT', expiry_date TEXT, invite_durations TEXT DEFAULT '1, 7, 30, lifetime', extend_durations TEXT DEFAULT '7, 30, lifetime', notify_join TEXT DEFAULT 'true', notify_leave TEXT DEFAULT 'true', reminder_sent TEXT DEFAULT 'false')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, email TEXT, expiry TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS otps (email TEXT PRIMARY KEY, otp TEXT, expiry TEXT)''')
    
    try: cursor.execute("ALTER TABLE channels ADD COLUMN agent_id INTEGER")
    except: pass
    try: cursor.execute("ALTER TABLE web_users ADD COLUMN agent_upi_id TEXT DEFAULT ''")
    except: pass
    
    # Internal System Plans & Payments
    cursor.execute('''CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, duration_str TEXT, details TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (id TEXT PRIMARY KEY, agent_email TEXT, plan_id INTEGER, amount REAL, status TEXT, utr TEXT, created_at TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS received_sms (id INTEGER PRIMARY KEY AUTOINCREMENT, utr TEXT UNIQUE, amount REAL, timestamp TEXT, matched TEXT DEFAULT 'false')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS upi_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, upi_id TEXT UNIQUE, is_primary INTEGER DEFAULT 0)''')
    
    try: cursor.execute("ALTER TABLE plans ADD COLUMN details TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE plans ADD COLUMN duration_str TEXT")
    except: pass
    
    # B2C SaaS Tables
    cursor.execute('''CREATE TABLE IF NOT EXISTS customer_plans (id TEXT PRIMARY KEY, agent_id INTEGER, channel_id INTEGER, plan_name TEXT, price REAL, duration_days INTEGER, description TEXT, is_active INTEGER DEFAULT 1)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS coupons (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, code TEXT, discount_pct INTEGER, max_uses INTEGER, used_count INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, plan_id TEXT DEFAULT '')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS customer_transactions (txn_id TEXT PRIMARY KEY, agent_id INTEGER, customer_email TEXT, plan_id TEXT, coupon_code TEXT, final_amount REAL, status TEXT, utr TEXT, tg_invite_link TEXT, created_at TEXT)''')
    
    # Payment Links Table & Updates
    cursor.execute('''CREATE TABLE IF NOT EXISTS payment_links (id TEXT PRIMARY KEY, agent_id INTEGER, title TEXT, type TEXT, amount REAL, discount_code TEXT, redirect_url TEXT, is_active INTEGER DEFAULT 1, created_at TEXT)''')
    try: cursor.execute("ALTER TABLE payment_links ADD COLUMN description TEXT DEFAULT ''")
    except: pass
    
    try: cursor.execute("ALTER TABLE customer_transactions ADD COLUMN customer_name TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE customer_transactions ADD COLUMN customer_mobile TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE customer_transactions ADD COLUMN payment_link_id TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE coupons ADD COLUMN plan_id TEXT DEFAULT ''")
    except: pass

    # --- Webhook Integrations ---
    try: cursor.execute("ALTER TABLE web_users ADD COLUMN webhook_key TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE customer_plans ADD COLUMN allow_free_webhook INTEGER DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE payment_links ADD COLUMN allow_free_webhook INTEGER DEFAULT 0")
    except: pass

    # --- Multi-Email Notifications ---
    try: cursor.execute("ALTER TABLE customer_plans ADD COLUMN extra_emails TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE payment_links ADD COLUMN extra_emails TEXT DEFAULT ''")
    except: pass

    # Generate webhook keys for any existing users that don't have one
    cursor.execute("SELECT id FROM web_users WHERE webhook_key = '' OR webhook_key IS NULL")
    for row in cursor.fetchall():
        new_key = "wh_" + secrets.token_hex(16)
        cursor.execute("UPDATE web_users SET webhook_key=? WHERE id=?", (new_key, row[0]))

    # --- PERFORMANCE OPTIMIZATIONS (Database Indexes) ---
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_joined_users_expiry ON joined_users(expiry_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_joined_users_status ON joined_users(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_transactions_status ON customer_transactions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status)")

    # Global Settings
    cursor.execute('''CREATE TABLE IF NOT EXISTS global_settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('menu_message', '🤖 **Welcome {user_name}!**\\n\\nChoose an option from the menu below:')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('menu_buttons', '🌐 Website - https://example.com | 💬 Support - https://t.me/support')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('registration_enabled', 'true')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('trial_days', '3')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('upi_randomize', 'false')")
    
    # Defaults for branding, smtp, and bot token
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('site_title', 'TG Manager')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('site_tagline', 'Admin & Agent Portal')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (key, value) VALUES ('site_icon', '')")
    cursor.execute(f"INSERT OR IGNORE INTO global_settings (key, value) VALUES ('bot_token', '{os.getenv('TELEGRAM_BOT_TOKEN', '')}')")
    cursor.execute(f"INSERT OR IGNORE INTO global_settings (key, value) VALUES ('smtp_host', '{os.getenv('SMTP_HOST', 'smtp.hostinger.com')}')")
    cursor.execute(f"INSERT OR IGNORE INTO global_settings (key, value) VALUES ('smtp_port', '{os.getenv('SMTP_PORT', '465')}')")
    cursor.execute(f"INSERT OR IGNORE INTO global_settings (key, value) VALUES ('smtp_secure', '{os.getenv('SMTP_SECURE', 'true')}')")
    cursor.execute(f"INSERT OR IGNORE INTO global_settings (key, value) VALUES ('smtp_user', '{os.getenv('SMTP_USER', '')}')")
    cursor.execute(f"INSERT OR IGNORE INTO global_settings (key, value) VALUES ('smtp_pass', '{os.getenv('SMTP_PASS', '')}')")
    
    cursor.execute("SELECT COUNT(*) FROM web_users")
    if cursor.fetchone()[0] == 0:
        default_pass = hash_password(os.getenv("BOT_PASSWORD", "admin123"))
        cursor.execute("INSERT INTO web_users (email, password_hash, role) VALUES (?, ?, 'ADMIN')", ('admin@admin.com', default_pass))
    conn.commit()
    conn.close()

init_auth_db()

def get_bot_token():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM global_settings WHERE key='bot_token'")
    row = c.fetchone()
    conn.close()
    if row and row[0]: return row[0]
    return os.getenv("TELEGRAM_BOT_TOKEN", "")

def get_seo_data():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT key, value FROM global_settings WHERE key IN ('site_title', 'site_tagline', 'site_icon')")
    seo = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return seo

def send_email_sync(to_email, subject, body, extra_bcc=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT key, value FROM global_settings WHERE key LIKE 'smtp_%' OR key='site_title'")
    cfg = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    
    s_host = cfg.get('smtp_host', '')
    s_port = int(cfg.get('smtp_port', 465))
    s_secure = str(cfg.get('smtp_secure', 'true')).lower() == 'true'
    s_user = cfg.get('smtp_user', '')
    s_pass = cfg.get('smtp_pass', '')
    site_title = cfg.get('site_title', 'TG Manager')
    
    if not s_host or not s_user: return

    # Convert newlines to HTML breaks
    formatted_body = body.replace('\n', '<br>')
    
    # Professional HTML Email Template
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
        msg['From'] = f"{site_title} <{s_user}>"
        msg['To'] = to_email
        if extra_bcc:
            msg['Bcc'] = f"{s_user}, {extra_bcc}"
        else:
            msg['Bcc'] = s_user
        if s_port == 465 or s_secure:
            with smtplib.SMTP_SSL(s_host, s_port, timeout=12) as server:
                server.login(s_user, s_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(s_host, s_port, timeout=12) as server:
                server.starttls()
                server.login(s_user, s_pass)
                server.send_message(msg)
    except Exception as e: print(f"SMTP Error: {e}")

def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token: raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db_connection()
    cursor = conn.cursor()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT email FROM sessions WHERE token=? AND expiry > ?", (token, now))
    session = cursor.fetchone()
    if not session:
        conn.close(); raise HTTPException(status_code=401, detail="Session expired")
    
    cursor.execute("SELECT id, email, role, invite_durations, extend_durations, notify_join, notify_leave, expiry_date, agent_upi_id, webhook_key FROM web_users WHERE email=?", (session[0],))
    user = cursor.fetchone()
    conn.close()
    if not user: raise HTTPException(status_code=401, detail="User not found")
    
    return {
        "id": user[0], "email": user[1], "role": user[2], "invite_durations": user[3], 
        "extend_durations": user[4], "notify_join": user[5], "notify_leave": user[6], 
        "expiry_date": user[7], "agent_upi_id": user[8], "webhook_key": user[9]
    }

def verify_channel_access(cursor, channel_id, user):
    if user['role'] == 'ADMIN': return True
    cursor.execute("SELECT agent_id FROM channels WHERE channel_id=?", (channel_id,))
    row = cursor.fetchone()
    return row and row[0] == user['id']


# --- IMAGE ROUTE ---
@app.get("/api/image/{img_name}")
async def get_image(img_name: str, user: dict = Depends(get_current_user)):
    # Validate img_name to prevent path traversal security risks
    if not re.match(r'^[a-zA-Z0-9_]+\.jpg$', img_name):
        raise HTTPException(status_code=400, detail="Invalid image name")
    
    file_path = f"/app/data/{img_name}"
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="Image not found")


# --- UI ROUTES ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"seo": get_seo_data()})

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    token = request.cookies.get("session_token")
    if not token: return RedirectResponse(url="/login")
    conn = get_db_connection()
    cursor = conn.cursor()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT email FROM sessions WHERE token=? AND expiry > ?", (token, now))
    if not cursor.fetchone():
        conn.close(); return RedirectResponse(url="/login")
    conn.close()
    return templates.TemplateResponse(request=request, name="index.html", context={"seo": get_seo_data()})

@app.get("/checkout/{plan_id}", response_class=HTMLResponse)
async def checkout_page(request: Request, plan_id: str):
    return templates.TemplateResponse(request=request, name="checkout.html", context={"plan_id": plan_id, "seo": get_seo_data()})


# --- CONFIG & AUTH APIs ---
@app.get("/api/public_config")
async def api_public_config():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM global_settings WHERE key IN ('registration_enabled', 'trial_days', 'site_title', 'site_tagline', 'site_icon')")
    config = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.execute("SELECT id, name, price, duration_str, details FROM plans ORDER BY price ASC")
    plans = [{"id": r[0], "name": r[1], "price": r[2], "duration_str": str(r[3]), "details": r[4] or ""} for r in cursor.fetchall()]
    conn.close()
    return {"status": "success", "config": config, "plans": plans}

@app.post("/api/auth/login")
async def api_login(req: Request, response: Response):
    data = await req.json()
    email, pwd = data.get('email'), data.get('password')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, role, password_hash, expiry_date FROM web_users WHERE email=?", (email,))
    user = cursor.fetchone()
    
    if user and user[2] == hash_password(pwd):
        token = secrets.token_hex(32)
        expiry = (get_ist_now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO sessions (token, email, expiry) VALUES (?, ?, ?)", (token, email, expiry))
        conn.commit(); conn.close()
        response.set_cookie(key="session_token", value=token, httponly=True, max_age=7*24*3600)
        return {"status": "success"}
    conn.close()
    return {"status": "error", "message": "Invalid email or password"}

@app.post("/api/auth/logout")
async def api_logout(response: Response):
    response.delete_cookie("session_token")
    return {"status": "success"}

@app.get("/api/me")
async def api_me(user: dict = Depends(get_current_user)):
    return {"status": "success", "user": user}

@app.post("/api/admin/update_credentials")
async def api_admin_update_credentials(req: Request, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    new_email = data.get('email')
    new_pwd = data.get('password')
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        if new_pwd:
            cursor.execute("UPDATE web_users SET email=?, password_hash=? WHERE id=?", (new_email, hash_password(new_pwd), user['id']))
        else:
            cursor.execute("UPDATE web_users SET email=? WHERE id=?", (new_email, user['id']))
        
        cursor.execute("UPDATE sessions SET email=? WHERE email=?", (new_email, user['email']))
        conn.commit()
        return {"status": "success"}
    except sqlite3.IntegrityError:
        return {"status": "error", "message": "Email already exists"}
    finally:
        conn.close()

@app.post("/api/auth/forgot")
async def api_forgot(req: Request, bg_tasks: BackgroundTasks):
    data = await req.json()
    email = data.get('email')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM web_users WHERE email=?", (email,))
    if not cursor.fetchone(): conn.close(); return {"status": "error", "message": "Email not found"}
    otp = str(secrets.randbelow(900000) + 100000)
    expiry = (get_ist_now() + datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO otps (email, otp, expiry) VALUES (?, ?, ?) ON CONFLICT(email) DO UPDATE SET otp=excluded.otp, expiry=excluded.expiry", (email, otp, expiry))
    conn.commit(); conn.close()
    bg_tasks.add_task(send_email_sync, email, "Password Reset OTP", f"Your Password Reset OTP is: {otp}\n\nValid for 15 minutes.")
    return {"status": "success"}

@app.post("/api/auth/reset")
async def api_reset(req: Request):
    data = await req.json()
    email, otp, new_pwd = data.get('email'), data.get('otp'), data.get('password')
    conn = get_db_connection()
    cursor = conn.cursor()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT otp FROM otps WHERE email=? AND expiry > ?", (email, now))
    record = cursor.fetchone()
    if record and record[0] == str(otp):
        cursor.execute("UPDATE web_users SET password_hash=? WHERE email=?", (hash_password(new_pwd), email))
        cursor.execute("DELETE FROM otps WHERE email=?", (email,))
        cursor.execute("DELETE FROM sessions WHERE email=?", (email,))
        conn.commit(); conn.close()
        return {"status": "success"}
    conn.close()
    return {"status": "error", "message": "Invalid OTP"}

@app.post("/api/auth/register_otp")
async def api_register_otp(req: Request, bg_tasks: BackgroundTasks):
    data = await req.json()
    email = data.get('email')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_settings WHERE key='registration_enabled'")
    reg_enabled = cursor.fetchone()
    if not reg_enabled or reg_enabled[0] != 'true': conn.close(); return {"status": "error", "message": "Registration disabled."}
    cursor.execute("SELECT id FROM web_users WHERE email=?", (email,))
    if cursor.fetchone(): conn.close(); return {"status": "error", "message": "Email is already registered."}
    otp = str(secrets.randbelow(900000) + 100000)
    expiry = (get_ist_now() + datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO otps (email, otp, expiry) VALUES (?, ?, ?) ON CONFLICT(email) DO UPDATE SET otp=excluded.otp, expiry=excluded.expiry", (email, otp, expiry))
    conn.commit(); conn.close()
    bg_tasks.add_task(send_email_sync, email, "Registration OTP", f"Welcome!\n\nYour Registration OTP is: {otp}")
    return {"status": "success"}

@app.post("/api/auth/register")
async def api_register(req: Request, response: Response, bg_tasks: BackgroundTasks):
    data = await req.json()
    email, otp, pwd, plan_id = data.get('email'), data.get('otp'), data.get('password'), data.get('plan_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT otp FROM otps WHERE email=? AND expiry > ?", (email, now))
    record = cursor.fetchone()
    
    if record and record[0] == str(otp):
        if plan_id == 'trial':
            cursor.execute("SELECT value FROM global_settings WHERE key='trial_days'")
            trial_days = int(cursor.fetchone()[0] or 3)
            expiry_date = (get_ist_now() + datetime.timedelta(days=trial_days)).strftime("%Y-%m-%d")
        else:
            expiry_date = (get_ist_now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            
        new_webhook_key = "wh_" + secrets.token_hex(16)
        cursor.execute("INSERT INTO web_users (email, password_hash, role, expiry_date, webhook_key) VALUES (?, ?, 'AGENT', ?, ?)", (email, hash_password(pwd), expiry_date, new_webhook_key))
        cursor.execute("DELETE FROM otps WHERE email=?", (email,))
        
        token = secrets.token_hex(32)
        sess_expiry = (get_ist_now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO sessions (token, email, expiry) VALUES (?, ?, ?)", (token, email, sess_expiry))
        conn.commit(); conn.close()
        
        if plan_id == 'trial':
            bg_tasks.add_task(send_email_sync, email, "Trial Activated", f"Your trial is active until {expiry_date}.")
        
        response.set_cookie(key="session_token", value=token, httponly=True, max_age=7*24*3600)
        return {"status": "success"}
    conn.close()
    return {"status": "error", "message": "Invalid OTP"}

# --- PAYMENT HELPERS ---
def apply_agent_payment(cursor, bg_tasks, txn_id, utr, amount, agent_email, plan_id):
    cursor.execute("SELECT duration_str, name FROM plans WHERE id=?", (plan_id,))
    plan = cursor.fetchone()
    duration_val = str(plan[0]).lower()
    
    cursor.execute("UPDATE transactions SET status='SUCCESS', utr=? WHERE id=?", (utr, txn_id))
    cursor.execute("SELECT expiry_date FROM web_users WHERE email=?", (agent_email,))
    user_data = cursor.fetchone()
    
    if duration_val == 'lifetime':
        new_expiry = 'lifetime'
    else:
        days = int(duration_val)
        today = get_ist_now().strftime("%Y-%m-%d")
        current_expiry = user_data[0] if (user_data and user_data[0]) else today
        if current_expiry != 'lifetime' and current_expiry < today: current_expiry = today
        
        if current_expiry == 'lifetime': new_expiry = 'lifetime'
        else: new_expiry = (datetime.datetime.strptime(current_expiry, "%Y-%m-%d") + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        
    cursor.execute("UPDATE web_users SET expiry_date=?, reminder_sent='false' WHERE email=?", (new_expiry, agent_email))
    body = f"Payment Successful!\n\nYou have purchased the <b>{plan[1]}</b> plan.<br>Your new expiry date is: <b>{new_expiry}</b><br><br>Transaction ID: {txn_id}<br>UTR: {utr}"
    bg_tasks.add_task(send_email_sync, agent_email, "Subscription Confirmed", body)

def apply_customer_payment(cursor, bg_tasks, txn_id, utr, customer_email, plan_id):
    cursor.execute("SELECT plan_name, duration_days, channel_id, agent_id, extra_emails FROM customer_plans WHERE id=?", (plan_id,))
    plan = cursor.fetchone()
    
    agent_id = None
    item_name = "Payment"
    invite_link = ""
    redirect_url = ""
    extra_emails = ""

    if plan:
        item_name, duration_days, channel_ids_str, agent_id, extra_emails = plan[0], plan[1], plan[2], plan[3], plan[4]
        duration_str = str(duration_days) if duration_days > 0 else 'lifetime'
        
        channel_ids = [c.strip() for c in str(channel_ids_str).split(',') if c.strip()]
        invite_links_list = []
        invite_links_email = []
        
        for cid in channel_ids:
            res = requests.post(f"https://api.telegram.org/bot{get_bot_token()}/createChatInviteLink", json={"chat_id": cid, "creates_join_request": True}).json()
            if res.get("ok"):
                link = res["result"]["invite_link"]
                cursor.execute("INSERT INTO invite_links (invite_link, channel_id, duration, max_uses) VALUES (?, ?, ?, 1)", (link, cid, duration_str))
                invite_links_list.append(link)
                
                # Fetch Channel Name for Email
                cursor.execute("SELECT channel_name FROM channels WHERE channel_id=?", (cid,))
                cn = cursor.fetchone()
                cname = cn[0] if cn else "Telegram Channel"
                invite_links_email.append(f"• <b>{cname}</b>: {link}")
        
        # Join links with line breaks
        invite_link = "\n".join(invite_links_list)
        invite_link_email_str = "<br>".join(invite_links_email)
        
        cursor.execute("UPDATE customer_transactions SET tg_invite_link=?, status='SUCCESS', utr=? WHERE txn_id=?", (invite_link, utr, txn_id))
        cus_body = f"Payment Successful for <b>{item_name}</b>!<br><br>Here are your exclusive, 1-time use links to join the Telegram channel(s):<br>{invite_link_email_str}"
    else:
        cursor.execute("SELECT title, redirect_url, agent_id, extra_emails FROM payment_links WHERE id=?", (plan_id,))
        link_data = cursor.fetchone()
        if link_data:
            item_name, redirect_url, agent_id, extra_emails = link_data[0], link_data[1], link_data[2], link_data[3]
            cursor.execute("UPDATE customer_transactions SET status='SUCCESS', utr=? WHERE txn_id=?", (utr, txn_id))
            cus_body = f"Payment Successful for <b>{item_name}</b>!<br><br>Transaction ID: {txn_id}<br>UTR: {utr}"
            if redirect_url:
                cus_body += f"<br><br>Access your content here: <a href='{redirect_url}'>{redirect_url}</a>"
        else:
            return {"invite_link": "", "redirect_url": ""}

    cursor.execute("SELECT customer_name, customer_mobile, final_amount FROM customer_transactions WHERE txn_id=?", (txn_id,))
    txn_data = cursor.fetchone()
    cus_name = txn_data[0] if txn_data else "Customer"
    cus_mobile = txn_data[1] if txn_data else "N/A"
    amt = txn_data[2] if txn_data else 0

    # Fetch agent email to use as BCC
    agent_email = None
    if agent_id:
        cursor.execute("SELECT email FROM web_users WHERE id=?", (agent_id,))
        agent_data = cursor.fetchone()
        if agent_data:
            agent_email = agent_data[0]

    # Combine existing extra_emails with the agent's primary email
    bcc_list = []
    if extra_emails:
        bcc_list.extend([e.strip() for e in extra_emails.split(',') if e.strip()])
    if agent_email:
        bcc_list.append(agent_email)
        
    combined_bcc = ", ".join(set(bcc_list))

    # Send the customer confirmation email with the agent BCC'd
    bg_tasks.add_task(send_email_sync, customer_email, f"Payment Confirmation: {item_name}", cus_body, extra_bcc=combined_bcc)

    # (Optional) Retain the separate internal notification for the agent
    if agent_email:
        agent_body = f"🎉 New Payment Received!<br><br>Item: <b>{item_name}</b><br>Amount: ₹{amt}<br>Customer: {cus_name}<br>Email: {customer_email}<br>Phone: {cus_mobile}<br>UTR: {utr}"
        for email_addr in set(bcc_list):
            if email_addr:
                bg_tasks.add_task(send_email_sync, email_addr, f"Payment Received: ₹{amt}", agent_body)

    return {"invite_link": invite_link, "redirect_url": redirect_url}

# --- WEBHOOK APIs ---
@app.post("/api/webhook/sms/{account_id}")
async def api_webhook_sms(account_id: str, req: Request, bg_tasks: BackgroundTasks):
    try:
        # Require Webhook Security Key
        key = req.query_params.get("key")
        if not key:
            return {"status": "error", "message": "Missing security key."}

        data = await req.json()
        payload_str = str(data).replace(',', ' ').replace('"', ' ').replace("'", ' ')
        
        utr_match = re.search(r'(?<!\d)(\d{12})(?!\d)', payload_str)
        amt_match = re.search(r'(?:Rs\.?|INR)\s*(\d+(?:\.\d{1,2})?)', payload_str, re.IGNORECASE)
        if not utr_match or not amt_match: return {"status": "ignored"}
            
        utr = utr_match.group(1)
        amount = float(amt_match.group(1))
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            if account_id == 'admin':
                cursor.execute("SELECT id FROM web_users WHERE email='admin@admin.com' AND webhook_key=?", (key,))
                if not cursor.fetchone(): 
                    conn.close(); return {"status": "error", "message": "Invalid security key."}

                cursor.execute("SELECT id, agent_email, plan_id FROM transactions WHERE status='PENDING' AND ABS(amount - ?) < 0.01", (amount,))
                agent_txns = cursor.fetchall()
                if len(agent_txns) == 1:
                    txn = agent_txns[0]
                    # ATOMIC LOCK: Prevent Double Delivery
                    cursor.execute("UPDATE transactions SET status='PROCESSING' WHERE id=? AND status='PENDING'", (txn[0],))
                    if cursor.rowcount == 1:
                        apply_agent_payment(cursor, bg_tasks, txn[0], utr, amount, txn[1], txn[2])
                        cursor.execute("INSERT INTO received_sms (utr, amount, timestamp, matched) VALUES (?, ?, ?, 'true')", (utr, amount, now_str))
                else:
                    cursor.execute("INSERT INTO received_sms (utr, amount, timestamp, matched) VALUES (?, ?, ?, 'false')", (utr, amount, now_str))
            
            else:
                cursor.execute("SELECT id FROM web_users WHERE id=? AND webhook_key=?", (account_id, key))
                if not cursor.fetchone(): 
                    conn.close(); return {"status": "error", "message": "Invalid security key."}

                cursor.execute("SELECT txn_id, customer_email, plan_id FROM customer_transactions WHERE status='PENDING' AND agent_id=? AND ABS(final_amount - ?) < 0.01", (account_id, amount))
                customer_txns = cursor.fetchall()
                if len(customer_txns) == 1:
                    txn = customer_txns[0]
                    # ATOMIC LOCK: Prevent Double Delivery
                    cursor.execute("UPDATE customer_transactions SET status='PROCESSING' WHERE txn_id=? AND status='PENDING'", (txn[0],))
                    if cursor.rowcount == 1:
                        apply_customer_payment(cursor, bg_tasks, txn[0], utr, txn[1], txn[2])
                        cursor.execute("INSERT INTO received_sms (utr, amount, timestamp, matched) VALUES (?, ?, ?, 'true')", (utr, amount, now_str))
                else:
                    cursor.execute("INSERT INTO received_sms (utr, amount, timestamp, matched) VALUES (?, ?, ?, 'false')", (utr, amount, now_str))
            
            conn.commit()
        except sqlite3.IntegrityError:
            pass # Catch duplicates
            
        conn.close()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/api/webhook/external_free")
async def api_webhook_external_free(req: Request, bg_tasks: BackgroundTasks):
    data = await req.json()
    api_key = req.headers.get("X-API-Key")
    plan_id = data.get("plan_id")
    email, name, mobile = data.get("email"), data.get("name", ""), data.get("mobile", "")

    if not api_key: return {"status": "error", "message": "Missing X-API-Key header."}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM web_users WHERE webhook_key=?", (api_key,))
    agent = cursor.fetchone()
    if not agent: conn.close(); return {"status": "error", "message": "Invalid API Key."}
    
    agent_id = agent[0]

    # Verify plan/link exists, is active, AND allows free webhook
    cursor.execute("SELECT allow_free_webhook FROM customer_plans WHERE id=? AND agent_id=? AND is_active=1", (plan_id, agent_id))
    plan = cursor.fetchone()
    if not plan:
        cursor.execute("SELECT allow_free_webhook FROM payment_links WHERE id=? AND agent_id=? AND is_active=1", (plan_id, agent_id))
        plan = cursor.fetchone()
    
    if not plan: conn.close(); return {"status": "error", "message": "Item unavailable."}
    if int(plan[0]) != 1: conn.close(); return {"status": "error", "message": "Security Error: This item does not allow free webhook fulfillment."}

    txn_id = "CUS" + str(uuid.uuid4().hex)[:8].upper()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO customer_transactions (txn_id, agent_id, customer_email, customer_name, customer_mobile, plan_id, final_amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'PENDING', ?)", (txn_id, agent_id, email, name, mobile, plan_id, now))
    conn.commit()
    
    # Process the free order immediately
    apply_customer_payment(cursor, bg_tasks, txn_id, 'WEBHOOK_FREE', email, plan_id)
    conn.commit(); conn.close()
    
    return {"status": "success", "message": "Free order processed successfully."}


# --- ADMIN SYSTEM APIs ---
@app.post("/api/admin/restart_bot")
async def api_restart_bot(user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    try:
        # Write the restart flag for main.py to detect and exit
        with open("/app/data/restart.flag", "w") as f:
            f.write("restart")
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/payment/initiate")
async def api_payment_init(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    plan_id = data.get('plan_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name, price FROM plans WHERE id=?", (plan_id,))
    plan = cursor.fetchone()
    if not plan: conn.close(); return {"status": "error", "message": "Plan not found"}
        
    cursor.execute("SELECT value FROM global_settings WHERE key='upi_randomize'")
    rand_val = cursor.fetchone()
    randomize = rand_val and rand_val[0] == 'true'
    
    if randomize:
        cursor.execute("SELECT upi_id FROM upi_ids")
        upis = cursor.fetchall()
        upi_id = random.choice(upis)[0] if upis else None
    else:
        cursor.execute("SELECT upi_id FROM upi_ids WHERE is_primary=1")
        p = cursor.fetchone()
        if p: upi_id = p[0]
        else:
            cursor.execute("SELECT upi_id FROM upi_ids LIMIT 1")
            f = cursor.fetchone()
            upi_id = f[0] if f else None
            
    if not upi_id: conn.close(); return {"status": "error", "message": "Admin has not configured UPI IDs."}
        
    txn_id = "TXN" + str(uuid.uuid4().hex)[:8].upper()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    
    fee = random.randint(11, 99) / 100.0
    unique_amount = float(plan[1]) + fee
    
    cursor.execute("INSERT INTO transactions (id, agent_email, plan_id, amount, status, created_at) VALUES (?, ?, ?, ?, 'PENDING', ?)", (txn_id, user['email'], plan_id, unique_amount, now))
    conn.commit(); conn.close()
    
    upi_string = f"upi://pay?pa={upi_id}&pn=TG%20Manager&am={unique_amount:.2f}&tr={txn_id}&cu=INR"
    return {"status": "success", "txn_id": txn_id, "upi_string": upi_string, "amount": unique_amount, "base_price": float(plan[1]), "fee": fee}

@app.get("/api/payment/status/{txn_id}")
async def api_payment_status(txn_id: str, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM transactions WHERE id=? AND agent_email=?", (txn_id, user['email']))
    txn = cursor.fetchone()
    conn.close()
    if txn and txn[0] == 'SUCCESS': return {"status": "SUCCESS"}
    return {"status": "PENDING"}

@app.post("/api/payment/verify_manual")
async def api_payment_verify_manual(req: Request, bg_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    data = await req.json()
    txn_id, utr = data.get('txn_id'), data.get('utr')
    if not utr or len(str(utr)) < 12: return {"status": "error", "message": "Invalid UTR."}
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT plan_id, status, amount FROM transactions WHERE id=? AND agent_email=?", (txn_id, user['email']))
    txn = cursor.fetchone()
    if not txn or txn[1] != 'PENDING': conn.close(); return {"status": "error", "message": "Invalid transaction."}
    
    cursor.execute("SELECT id, amount FROM received_sms WHERE utr=? AND matched='false'", (utr,))
    sms = cursor.fetchone()
    if sms and abs(sms[1] - txn[2]) < 1.0:
        cursor.execute("UPDATE transactions SET status='PROCESSING' WHERE id=? AND status='PENDING'", (txn_id,))
        if cursor.rowcount == 1:
            apply_agent_payment(cursor, bg_tasks, txn_id, utr, txn[2], user['email'], txn[0])
            cursor.execute("UPDATE received_sms SET matched='true' WHERE utr=?", (utr,))
            conn.commit(); conn.close()
            return {"status": "success"}
    
    conn.close()
    return {"status": "error", "message": "Payment not received yet. Wait 1 minute and retry."}


# --- PUBLIC B2C CHECKOUT APIs ---
@app.get("/api/public/checkout/plan/{plan_id}")
async def api_public_checkout_plan(plan_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT p.plan_name, p.price, p.duration_days, p.description, p.is_active, u.agent_upi_id, p.channel_id, p.agent_id FROM customer_plans p JOIN web_users u ON p.agent_id = u.id WHERE p.id=?", (plan_id,))
    row = cursor.fetchone()
    if row and row[4] == 1:
        # Fetch all channel names
        channel_ids = [c.strip() for c in str(row[6]).split(',') if c.strip()]
        if channel_ids:
            placeholders = ','.join('?' for _ in channel_ids)
            cursor.execute(f"SELECT channel_name FROM channels WHERE channel_id IN ({placeholders})", channel_ids)
            names = [r[0] for r in cursor.fetchall() if r[0]]
            channel_name_str = " & ".join(names) if names else "Multiple Channels"
        else:
            channel_name_str = "Telegram Channel"
            
        conn.close()
        return {"status": "success", "plan_name": row[0], "price": row[1], "duration_days": row[2], "description": row[3], "agent_upi_id": row[5], "channel_name": channel_name_str, "agent_id": row[7], "type": "TG_PLAN"}
    
    cursor.execute("SELECT p.title, p.amount, p.type, p.redirect_url, p.is_active, u.agent_upi_id, p.agent_id, p.description FROM payment_links p JOIN web_users u ON p.agent_id = u.id WHERE p.id=?", (plan_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[4] == 1:
        return {"status": "success", "plan_name": row[0], "price": row[1], "link_type": row[2], "redirect_url": row[3], "agent_upi_id": row[5], "agent_id": row[6], "description": row[7], "type": "PAYMENT_LINK", "channel_name": "Payment Link"}

    return {"status": "error", "message": "Item not found or inactive."}

@app.post("/api/public/checkout/coupon/validate")
async def api_public_coupon_validate(req: Request):
    data = await req.json()
    code, agent_id, plan_id = data.get('code'), data.get('agent_id'), data.get('plan_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, discount_pct, max_uses, used_count, plan_id FROM coupons WHERE code=? AND agent_id=? AND is_active=1", (code, agent_id))
    coupon = cursor.fetchone()
    conn.close()
    if not coupon: return {"status": "error", "message": "Invalid code"}
    if coupon[4] and coupon[4] != "" and coupon[4] != plan_id:
        return {"status": "error", "message": "Coupon not valid for this item."}
    if coupon[2] > 0 and coupon[3] >= coupon[2]: return {"status": "error", "message": "Coupon usage limit reached."}
    return {"status": "success", "discount_pct": coupon[1]}

@app.post("/api/public/checkout/initiate")
async def api_public_checkout_initiate(req: Request, bg_tasks: BackgroundTasks):
    data = await req.json()
    plan_id = data.get('plan_id')
    email = data.get('email')
    name = data.get('name', '')
    mobile = data.get('mobile', '')
    coupon_code = data.get('coupon_code', '')
    flexible_amount = float(data.get('flexible_amount', 0))
    
    if not email or not name or not mobile: 
        return {"status": "error", "message": "Name, Email, and Mobile are required."}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    is_payment_link = False
    cursor.execute("SELECT plan_name, price, agent_id, is_active FROM customer_plans WHERE id=?", (plan_id,))
    plan = cursor.fetchone()
    
    if not plan:
        cursor.execute("SELECT title, amount, agent_id, is_active, type, discount_code FROM payment_links WHERE id=?", (plan_id,))
        plan = cursor.fetchone()
        is_payment_link = True

    if not plan or plan[3] == 0: 
        conn.close(); return {"status": "error", "message": "Item unavailable."}
    
    agent_id = plan[2]
    
    if is_payment_link and plan[4] == 'FLEXIBLE':
        if flexible_amount <= 0:
            conn.close(); return {"status": "error", "message": "Enter a valid amount."}
        base_price = flexible_amount
    else:
        base_price = float(plan[1])
        
    final_price = base_price
    
    if coupon_code:
        cursor.execute("SELECT id, discount_pct, max_uses, used_count, plan_id FROM coupons WHERE code=? AND agent_id=? AND is_active=1", (coupon_code, agent_id))
        coupon = cursor.fetchone()
        if coupon and (not coupon[4] or coupon[4] == "" or coupon[4] == plan_id) and (coupon[2] == 0 or coupon[3] < coupon[2]):
            final_price = base_price * (1 - (coupon[1]/100.0))
            cursor.execute("UPDATE coupons SET used_count=used_count+1 WHERE id=?", (coupon[0],))
        else:
            coupon_code = ''
            
    txn_id = "CUS" + str(uuid.uuid4().hex)[:8].upper()
    now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    
    if final_price <= 0:
        cursor.execute("INSERT INTO customer_transactions (txn_id, agent_id, customer_email, customer_name, customer_mobile, plan_id, coupon_code, final_amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'PENDING', ?)", (txn_id, agent_id, email, name, mobile, plan_id, coupon_code, now))
        conn.commit()
        payment_data = apply_customer_payment(cursor, bg_tasks, txn_id, '100_PCT_DISCOUNT', email, plan_id)
        conn.commit(); conn.close()
        return {"status": "success_bypass", "txn_id": txn_id, "invite_link": payment_data.get("invite_link"), "redirect_url": payment_data.get("redirect_url")}
        
    cursor.execute("SELECT agent_upi_id FROM web_users WHERE id=?", (agent_id,))
    upi_row = cursor.fetchone()
    upi_id = upi_row[0] if upi_row else ""
    if not upi_id: conn.close(); return {"status": "error", "message": "Seller cannot accept payments right now."}
    
    fee = random.randint(11, 99) / 100.0
    final_amount = final_price + fee
    
    cursor.execute("INSERT INTO customer_transactions (txn_id, agent_id, customer_email, customer_name, customer_mobile, plan_id, coupon_code, final_amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)", (txn_id, agent_id, email, name, mobile, plan_id, coupon_code, final_amount, now))
    conn.commit(); conn.close()
    
    upi_string = f"upi://pay?pa={upi_id}&pn=Payment&am={final_amount:.2f}&tr={txn_id}&cu=INR"
    return {"status": "success", "txn_id": txn_id, "upi_string": upi_string, "amount": final_amount, "base_price": final_price, "fee": fee}

@app.get("/api/public/checkout/status/{txn_id}")
async def api_public_checkout_status(txn_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status, tg_invite_link, plan_id FROM customer_transactions WHERE txn_id=?", (txn_id,))
    txn = cursor.fetchone()
    if txn:
        if txn[0] == 'SUCCESS': 
            redirect_url = ""
            cursor.execute("SELECT redirect_url FROM payment_links WHERE id=?", (txn[2],))
            pl = cursor.fetchone()
            if pl: redirect_url = pl[0]
            conn.close()
            return {"status": "SUCCESS", "invite_link": txn[1], "redirect_url": redirect_url}
    conn.close()
    return {"status": "PENDING"}

@app.post("/api/public/checkout/verify_manual")
async def api_public_checkout_verify_manual(req: Request, bg_tasks: BackgroundTasks):
    data = await req.json()
    txn_id, utr = data.get('txn_id'), data.get('utr')
    if not utr or len(str(utr)) < 12: return {"status": "error", "message": "Invalid UTR."}
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT plan_id, status, final_amount, customer_email FROM customer_transactions WHERE txn_id=?", (txn_id,))
    txn = cursor.fetchone()
    if not txn or txn[1] != 'PENDING': conn.close(); return {"status": "error", "message": "Invalid transaction."}
    
    cursor.execute("SELECT id, amount FROM received_sms WHERE utr=? AND matched='false'", (utr,))
    sms = cursor.fetchone()
    if sms and abs(sms[1] - txn[2]) < 1.0:
        cursor.execute("UPDATE customer_transactions SET status='PROCESSING' WHERE txn_id=? AND status='PENDING'", (txn_id,))
        if cursor.rowcount == 1:
            payment_data = apply_customer_payment(cursor, bg_tasks, txn_id, utr, txn[3], txn[0])
            cursor.execute("UPDATE received_sms SET matched='true' WHERE utr=?", (utr,))
            conn.commit(); conn.close()
            return {"status": "success", "invite_link": payment_data.get("invite_link"), "redirect_url": payment_data.get("redirect_url")}
    
    conn.close()
    return {"status": "error", "message": "Payment not received yet. Wait 1 minute and retry."}


# --- B2C AGENT MGMT APIs ---
@app.post("/api/user/agent_upi")
async def api_user_agent_upi(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    conn = get_db_connection()
    conn.execute("UPDATE web_users SET agent_upi_id=? WHERE id=?", (data.get('upi_id', ''), user['id']))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/b2c/plans/save")
async def api_b2c_plans_save(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Parse and verify access for ALL selected channels
        channel_ids = [c.strip() for c in str(data['channel_id']).split(',') if c.strip()]
        for cid in channel_ids:
            if not verify_channel_access(cursor, cid, user):
                return {"status": "error", "message": f"Unauthorized access to channel {cid}."}
            
        dur = int(data['duration_days']) if data['duration_days'] else 0
        allow_free = int(data.get('allow_free_webhook', 0))
        extra_emails = data.get('extra_emails', '')
        
        if data.get('plan_id'):
            conn.execute("UPDATE customer_plans SET plan_name=?, channel_id=?, price=?, duration_days=?, description=?, allow_free_webhook=?, extra_emails=? WHERE id=? AND agent_id=?", 
                         (data['plan_name'], data['channel_id'], float(data['price']), dur, data['description'], allow_free, extra_emails, data['plan_id'], user['id']))
        else:
            plan_id = "PLN" + str(uuid.uuid4().hex)[:8].upper()
            conn.execute("INSERT INTO customer_plans (id, agent_id, channel_id, plan_name, price, duration_days, description, allow_free_webhook, extra_emails) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                         (plan_id, user['id'], data['channel_id'], data['plan_name'], float(data['price']), dur, data['description'], allow_free, extra_emails))
        conn.commit()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/b2c/plans/delete/{plan_id}")
async def api_b2c_plans_delete(plan_id: str, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    conn.execute("DELETE FROM customer_plans WHERE id=? AND agent_id=?", (plan_id, user['id']))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/b2c/payment_links/save")
async def api_b2c_payment_links_save(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    conn = get_db_connection()
    try:
        allow_free = int(data.get('allow_free_webhook', 0))
        extra_emails = data.get('extra_emails', '')
        
        if data.get('link_id'):
            conn.execute("UPDATE payment_links SET title=?, type=?, amount=?, discount_code=?, redirect_url=?, description=?, allow_free_webhook=?, extra_emails=? WHERE id=? AND agent_id=?", 
                         (data['title'], data['type'], float(data['amount'] or 0), data.get('discount_code', ''), data.get('redirect_url', ''), data.get('description', ''), allow_free, extra_emails, data['link_id'], user['id']))
        else:
            link_id = "PAY" + str(uuid.uuid4().hex)[:8].upper()
            now = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO payment_links (id, agent_id, title, type, amount, discount_code, redirect_url, description, allow_free_webhook, created_at, extra_emails) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                         (link_id, user['id'], data['title'], data['type'], float(data['amount'] or 0), data.get('discount_code', ''), data.get('redirect_url', ''), data.get('description', ''), allow_free, now, extra_emails))
        conn.commit()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/b2c/payment_links/delete/{link_id}")
async def api_b2c_payment_links_delete(link_id: str, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    conn.execute("DELETE FROM payment_links WHERE id=? AND agent_id=?", (link_id, user['id']))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/b2c/txns/force_success/{txn_id}")
async def api_b2c_txns_force_success(txn_id: str, bg_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT plan_id, status, customer_email, final_amount FROM customer_transactions WHERE txn_id=? AND agent_id=?", (txn_id, user['id']))
        txn = cursor.fetchone()
        
        if not txn or txn[1] != 'PENDING':
            return {"status": "error", "message": "Transaction invalid or not PENDING."}
        
        cursor.execute("UPDATE customer_transactions SET status='PROCESSING' WHERE txn_id=? AND status='PENDING'", (txn_id,))
        if cursor.rowcount == 1:
            unique_utr = f"MANUAL_{txn_id}"
            apply_customer_payment(cursor, bg_tasks, txn_id, unique_utr, txn[2], txn[0])
            now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO received_sms (utr, amount, timestamp, matched) VALUES (?, ?, ?, 'true')", (unique_utr, txn[3], now_str))
            conn.commit()
            return {"status": "success"}
        else:
            return {"status": "error", "message": "Transaction already processed."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()

@app.post("/api/b2c/coupons/save")
async def api_b2c_coupons_save(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    conn = get_db_connection()
    try:
        plan_id = data.get('plan_id', '')
        if data.get('coupon_id'):
            conn.execute("UPDATE coupons SET code=?, discount_pct=?, max_uses=?, plan_id=? WHERE id=? AND agent_id=?", 
                         (data['code'], int(data['discount_pct']), int(data['max_uses']), plan_id, data['coupon_id'], user['id']))
        else:
            conn.execute("INSERT INTO coupons (agent_id, code, discount_pct, max_uses, plan_id) VALUES (?, ?, ?, ?, ?)", 
                         (user['id'], data['code'], int(data['discount_pct']), int(data['max_uses']), plan_id))
        conn.commit()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/b2c/coupons/delete/{coupon_id}")
async def api_b2c_coupons_delete(coupon_id: int, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    conn.execute("DELETE FROM coupons WHERE id=? AND agent_id=?", (coupon_id, user['id']))
    conn.commit(); conn.close()
    return {"status": "success"}


# --- ADMIN SYSTEM APIs ---
@app.post("/api/admin/txns/force_success/{txn_id}")
async def api_admin_txns_force_success(txn_id: str, bg_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT plan_id, status, agent_email, amount FROM transactions WHERE id=?", (txn_id,))
        txn = cursor.fetchone()
        
        if not txn or txn[1] != 'PENDING':
            return {"status": "error", "message": "Transaction invalid or not PENDING."}
        
        cursor.execute("UPDATE transactions SET status='PROCESSING' WHERE id=? AND status='PENDING'", (txn_id,))
        if cursor.rowcount == 1:
            unique_utr = f"MANUAL_{txn_id}"
            apply_agent_payment(cursor, bg_tasks, txn_id, unique_utr, txn[3], txn[2], txn[0])
            now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO received_sms (utr, amount, timestamp, matched) VALUES (?, ?, ?, 'true')", (unique_utr, txn[3], now_str))
            conn.commit()
            return {"status": "success"}
        else:
            return {"status": "error", "message": "Transaction already processed."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()

@app.post("/api/plans/save")
async def api_save_plan(req: Request, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    conn = get_db_connection()
    if data.get('id'): conn.execute("UPDATE plans SET name=?, price=?, duration_str=?, details=? WHERE id=?", (data['name'], data['price'], data['duration_str'], data['details'], data['id']))
    else: conn.execute("INSERT INTO plans (name, price, duration_str, details) VALUES (?, ?, ?, ?)", (data['name'], data['price'], data['duration_str'], data['details']))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/plans/delete/{plan_id}")
async def api_delete_plan(plan_id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.get("/api/upi")
async def api_get_upi(user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, upi_id, is_primary FROM upi_ids")
    upis = [{"id": r[0], "upi_id": r[1], "is_primary": bool(r[2])} for r in cursor.fetchall()]
    cursor.execute("SELECT value FROM global_settings WHERE key='upi_randomize'")
    rand_val = cursor.fetchone()
    randomize = rand_val and rand_val[0] == 'true'
    conn.close()
    return {"status": "success", "upis": upis, "randomize": randomize}

@app.post("/api/upi/add")
async def api_add_upi(req: Request, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO upi_ids (upi_id) VALUES (?)", (data['upi_id'],))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM upi_ids")
        if cursor.fetchone()[0] == 1: conn.execute("UPDATE upi_ids SET is_primary=1")
        conn.commit(); return {"status": "success"}
    except: return {"status": "error", "message": "UPI ID exists."}
    finally: conn.close()

@app.post("/api/upi/delete/{id}")
async def api_delete_upi(id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    conn.execute("DELETE FROM upi_ids WHERE id=?", (id,))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/upi/primary/{id}")
async def api_primary_upi(id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    conn.execute("UPDATE upi_ids SET is_primary=0")
    conn.execute("UPDATE upi_ids SET is_primary=1 WHERE id=?", (id,))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/update_system_config")
async def api_update_system_config(req: Request, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    conn = get_db_connection()
    valid_keys = ['registration_enabled', 'trial_days', 'upi_randomize', 'site_title', 'site_tagline', 'site_icon', 'smtp_host', 'smtp_port', 'smtp_secure', 'smtp_user', 'smtp_pass', 'bot_token']
    for key, val in data.items():
        if key in valid_keys:
            conn.execute("UPDATE global_settings SET value=? WHERE key=?", (str(val), key))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/update_menu")
async def api_update_menu(req: Request, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    conn = get_db_connection()
    conn.execute("UPDATE global_settings SET value=? WHERE key='menu_message'", (data.get('message', ''),))
    conn.execute("UPDATE global_settings SET value=? WHERE key='menu_buttons'", (data.get('buttons', ''),))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/user/durations")
async def api_user_durations(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    conn = get_db_connection()
    if 'invite_durations' in data: conn.execute("UPDATE web_users SET invite_durations=? WHERE id=?", (data['invite_durations'], user['id']))
    if 'extend_durations' in data: conn.execute("UPDATE web_users SET extend_durations=? WHERE id=?", (data['extend_durations'], user['id']))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/user/notifications")
async def api_user_notifications(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    conn = get_db_connection()
    if 'notify_join' in data: conn.execute("UPDATE web_users SET notify_join=? WHERE id=?", (data['notify_join'], user['id']))
    if 'notify_leave' in data: conn.execute("UPDATE web_users SET notify_leave=? WHERE id=?", (data['notify_leave'], user['id']))
    conn.commit(); conn.close()
    return {"status": "success"}

# --- AGENT & CHANNEL MANAGEMENT ---
@app.get("/api/agents")
async def api_get_agents(user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, role, expiry_date FROM web_users ORDER BY id DESC")
    agents = [{"id": r[0], "email": r[1], "role": r[2], "expiry_date": r[3] or ""} for r in cursor.fetchall()]
    conn.close()
    return {"status": "success", "agents": agents}

@app.post("/api/agents/add")
async def api_add_agent(req: Request, bg_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    email, pwd, role, expiry = data.get('email'), data.get('password'), data.get('role', 'AGENT'), data.get('expiry_date')
    conn = get_db_connection()
    try:
        new_webhook_key = "wh_" + secrets.token_hex(16)
        conn.execute("INSERT INTO web_users (email, password_hash, role, expiry_date, webhook_key) VALUES (?, ?, ?, ?, ?)", (email, hash_password(pwd), role, expiry, new_webhook_key))
        conn.commit()
        bg_tasks.add_task(send_email_sync, email, "Account Created", f"Your account is ready.\n\nEmail: {email}\nPassword: {pwd}")
        return {"status": "success"}
    except sqlite3.IntegrityError: return {"status": "error", "message": "Email already exists"}
    finally: conn.close()

@app.post("/api/agents/edit_expiry/{agent_id}")
async def api_edit_agent_expiry(agent_id: int, req: Request, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    data = await req.json()
    conn = get_db_connection()
    conn.execute("UPDATE web_users SET expiry_date=?, reminder_sent='false' WHERE id=?", (data.get('expiry_date'), agent_id))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/agents/delete/{agent_id}")
async def api_delete_agent(agent_id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    if user['id'] == agent_id: return {"status": "error", "message": "Cannot delete yourself"}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id FROM channels WHERE agent_id=?", (agent_id,))
    agent_channels = [r[0] for r in cursor.fetchall()]
    for cid in agent_channels:
        try: requests.post(f"https://api.telegram.org/bot{get_bot_token()}/leaveChat", json={"chat_id": cid})
        except: pass
        cursor.execute("DELETE FROM joined_users WHERE channel_id=?", (cid,))
        cursor.execute("DELETE FROM invite_links WHERE channel_id=?", (cid,))
        cursor.execute("DELETE FROM channels WHERE channel_id=?", (cid,))
    cursor.execute("DELETE FROM web_users WHERE id=?", (agent_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/channels/block/{channel_id}")
async def api_block_channel(channel_id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM joined_users WHERE channel_id=?", (channel_id,))
        cursor.execute("DELETE FROM invite_links WHERE channel_id=?", (channel_id,))
        cursor.execute("UPDATE channels SET bot_status='BLOCKED' WHERE channel_id=?", (channel_id,))
        conn.commit()
        requests.post(f"https://api.telegram.org/bot{get_bot_token()}/leaveChat", json={"chat_id": channel_id})
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/channels/clear/{channel_id}")
async def api_clear_channel(channel_id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM joined_users WHERE channel_id=?", (channel_id,))
        cursor.execute("DELETE FROM invite_links WHERE channel_id=?", (channel_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/channels/unblock/{channel_id}")
async def api_unblock_channel(channel_id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE channels SET bot_status='INACTIVE' WHERE channel_id=?", (channel_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/channels/delete/{channel_id}")
async def api_delete_channel(channel_id: int, user: dict = Depends(get_current_user)):
    if user['role'] != 'ADMIN': raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM joined_users WHERE channel_id=?", (channel_id,))
        cursor.execute("DELETE FROM invite_links WHERE channel_id=?", (channel_id,))
        cursor.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        conn.commit()
        requests.post(f"https://api.telegram.org/bot{get_bot_token()}/leaveChat", json={"chat_id": channel_id})
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.get("/api/data")
async def api_get_data(user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    now = get_ist_now()
    
    # Adding LIMIT 2000 to users to prevent memory bloat until server-side pagination is fully implemented on UI
    if user['role'] == 'ADMIN': cursor.execute("SELECT u.id, u.user_id, u.user_name, u.status, u.join_date, u.expiry_date, c.channel_name, u.invite_link_used, c.bot_status FROM joined_users u LEFT JOIN channels c ON u.channel_id = c.channel_id ORDER BY u.id DESC LIMIT 2000")
    else: cursor.execute("SELECT u.id, u.user_id, u.user_name, u.status, u.join_date, u.expiry_date, c.channel_name, u.invite_link_used, c.bot_status FROM joined_users u INNER JOIN channels c ON u.channel_id = c.channel_id WHERE c.agent_id=? ORDER BY u.id DESC LIMIT 2000", (user['id'],))
    users = []
    for r in cursor.fetchall():
        user_status, expiry_val = r[3], r[5]
        if user_status == 'LEFT' and expiry_val != 'lifetime':
            try:
                if datetime.datetime.strptime(expiry_val, "%Y-%m-%d %H:%M:%S") > now: user_status = 'LEFT EARLY'
            except: pass
        users.append({"id": r[0], "user_id": r[1], "name": r[2], "status": user_status, "join_date": r[4], "expiry": expiry_val, "channel": (r[6] or "Unknown") + (" ⛔ Blocked" if r[8] == 'BLOCKED' else (" ⚠️" if r[8] == 'INACTIVE' else "")), "link": r[7] or ""})
    
    if user['role'] == 'ADMIN': cursor.execute("SELECT l.id, l.invite_link, l.duration, c.channel_name, l.max_uses, l.used_count, c.bot_status FROM invite_links l LEFT JOIN channels c ON l.channel_id = c.channel_id WHERE l.status = 'PENDING' ORDER BY l.id DESC")
    else: cursor.execute("SELECT l.id, l.invite_link, l.duration, c.channel_name, l.max_uses, l.used_count, c.bot_status FROM invite_links l INNER JOIN channels c ON l.channel_id = c.channel_id WHERE l.status = 'PENDING' AND c.agent_id=? ORDER BY l.id DESC", (user['id'],))
    links = [{"id": r[0], "link": r[1], "duration": r[2], "channel": (r[3] or "Unknown") + (" ⚠️" if r[6] == 'INACTIVE' else ""), "max_uses": r[4], "used_count": r[5]} for r in cursor.fetchall()]
    
    if user['role'] == 'ADMIN': cursor.execute("SELECT channel_id, channel_name, bot_status, welcome_message, farewell_message, welcome_buttons, farewell_buttons, welcome_image, farewell_image FROM channels")
    else: cursor.execute("SELECT channel_id, channel_name, bot_status, welcome_message, farewell_message, welcome_buttons, farewell_buttons, welcome_image, farewell_image FROM channels WHERE agent_id=?", (user['id'],))
    channels = [{"id": r[0], "name": r[1], "bot_status": r[2], "welcome": r[3] or "", "farewell": r[4] or "", "w_btns": r[5] or "", "f_btns": r[6] or "", "w_img": bool(r[7]), "f_img": bool(r[8])} for r in cursor.fetchall()]
    
    # B2C Data
    cursor.execute("SELECT id, plan_name, price, duration_days, description, is_active, channel_id, allow_free_webhook, extra_emails FROM customer_plans WHERE agent_id=?", (user['id'],))
    b2c_plans = [{"id": r[0], "plan_name": r[1], "price": r[2], "duration_days": r[3], "description": r[4], "is_active": r[5], "channel_id": r[6], "allow_free_webhook": r[7] or 0, "extra_emails": r[8] or ""} for r in cursor.fetchall()]
    
    cursor.execute("SELECT id, title, type, amount, discount_code, redirect_url, is_active, description, allow_free_webhook, extra_emails FROM payment_links WHERE agent_id=?", (user['id'],))
    payment_links = [{"id": r[0], "title": r[1], "type": r[2], "amount": r[3], "discount_code": r[4] or "", "redirect_url": r[5] or "", "is_active": r[6], "description": r[7] or "", "allow_free_webhook": r[8] or 0, "extra_emails": r[9] or ""} for r in cursor.fetchall()]
    
    cursor.execute("SELECT id, code, discount_pct, max_uses, used_count, is_active, plan_id FROM coupons WHERE agent_id=?", (user['id'],))
    coupons = [{"id": r[0], "code": r[1], "discount_pct": r[2], "max_uses": r[3], "used_count": r[4], "is_active": r[5], "plan_id": r[6] or ""} for r in cursor.fetchall()]
    
    cursor.execute("SELECT txn_id, customer_email, plan_id, coupon_code, final_amount, status, utr, created_at, customer_name, customer_mobile FROM customer_transactions WHERE agent_id=? ORDER BY created_at DESC LIMIT 1000", (user['id'],))
    b2c_txns = [{"txn_id": r[0], "customer_email": r[1], "plan_id": r[2], "coupon_code": r[3], "final_amount": r[4], "status": r[5], "utr": r[6], "created_at": r[7], "customer_name": r[8], "customer_mobile": r[9]} for r in cursor.fetchall()]

    admin_txns = []
    if user['role'] == 'ADMIN':
        cursor.execute("SELECT id, agent_email, plan_id, amount, status, utr, created_at FROM transactions ORDER BY created_at DESC LIMIT 1000")
        admin_txns = [{"txn_id": r[0], "agent_email": r[1], "plan_id": r[2], "amount": r[3], "status": r[4], "utr": r[5], "created_at": r[6]} for r in cursor.fetchall()]

    cursor.execute("SELECT key, value FROM global_settings")
    settings = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.execute("SELECT id, name, price, duration_str, details FROM plans ORDER BY price ASC")
    plans = [{"id": r[0], "name": r[1], "price": r[2], "duration_str": str(r[3]), "details": r[4] or ""} for r in cursor.fetchall()]
    
    today = get_ist_now().strftime("%Y-%m-%d")
    is_expired = user['role'] == 'AGENT' and user['expiry_date'] != 'lifetime' and user['expiry_date'] <= today
    
    conn.close()
    return {
        "users": users, "links": links, "channels": channels, "settings": settings, "plans": plans,
        "b2c_plans": b2c_plans, "payment_links": payment_links, "coupons": coupons, "b2c_txns": b2c_txns,
        "admin_txns": admin_txns,
        "user_settings": {
            "invite_durations": user['invite_durations'], "extend_durations": user['extend_durations'],
            "notify_join": user['notify_join'], "notify_leave": user['notify_leave'],
            "expiry_date": user['expiry_date'], "agent_upi_id": user['agent_upi_id'], "webhook_key": user.get('webhook_key', ''), "is_expired": is_expired, "id": user['id']
        }
    }

@app.get("/api/bot_info")
async def api_get_bot_info(user: dict = Depends(get_current_user)):
    try:
        res = requests.get(f"https://api.telegram.org/bot{get_bot_token()}/getMe").json()
        if res.get("ok"): return {"name": res["result"]["first_name"], "username": res["result"]["username"]}
    except: pass
    return {"name": "Bot", "username": "unknown"}

@app.post("/api/update_messages")
async def api_update_messages(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    cid = data['channel_id']
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if not verify_channel_access(cursor, cid, user): return {"status": "error", "message": "Unauthorized"}
        cursor.execute("UPDATE channels SET welcome_message=?, farewell_message=?, welcome_buttons=?, farewell_buttons=? WHERE channel_id=?", (data['welcome'], data['farewell'], data['w_btns'], data['f_btns'], cid))
        for img_type in ['welcome_image', 'farewell_image']:
            if data.get(img_type):
                path = f"/app/data/{img_type}_{cid}.jpg"
                if data[img_type] == "DELETE":
                    try:
                        if os.path.exists(path): os.remove(path)
                    except OSError:
                        pass # Ignore if file is already missing or locked
                    cursor.execute(f"UPDATE channels SET {img_type}=NULL WHERE channel_id=?", (cid,))
                elif data[img_type].startswith("data:image"):
                    with open(path, "wb") as f: f.write(base64.b64decode(data[img_type].split(",", 1)[1]))
                    cursor.execute(f"UPDATE channels SET {img_type}=? WHERE channel_id=?", (path, cid))
        conn.commit(); return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/add_channel")
async def api_add_channel(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    channel_id = data['channel_id']
    channel_name = "Pending (Bot not added)"
    try:
        res = requests.get(f"https://api.telegram.org/bot{get_bot_token()}/getChat?chat_id={channel_id}").json()
        if res.get("ok"): channel_name = res["result"].get("title", channel_name)
    except: pass
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT bot_status, agent_id FROM channels WHERE channel_id=?", (channel_id,))
        row = cursor.fetchone()
        if row:
            if row[0] == 'BLOCKED': return {"status": "error", "message": "Channel is BLOCKED."}
            if row[1] is not None and row[1] != user['id'] and user['role'] != 'ADMIN': return {"status": "error", "message": "Belongs to another agent."}
        cursor.execute("INSERT INTO channels (channel_id, channel_name, agent_id) VALUES (?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET channel_name=excluded.channel_name, agent_id=excluded.agent_id", (channel_id, channel_name, user['id']))
        conn.commit(); return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/generate_link")
async def api_generate_link(req: Request, user: dict = Depends(get_current_user)):
    data = await req.json()
    channel_id, duration = data['channel_id'], data['duration']
    quantity, max_uses = int(data.get('quantity', 1)), int(data.get('max_uses', 1))
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if not verify_channel_access(cursor, channel_id, user): return {"status": "error", "message": "Unauthorized"}
        generated_links = []
        for _ in range(quantity):
            res = requests.post(f"https://api.telegram.org/bot{get_bot_token()}/createChatInviteLink", json={"chat_id": channel_id, "creates_join_request": True}).json()
            if res.get("ok"):
                link = res["result"]["invite_link"]
                cursor.execute("INSERT INTO invite_links (invite_link, channel_id, duration, max_uses) VALUES (?, ?, ?, ?)", (link, channel_id, duration, max_uses))
                generated_links.append(link)
        conn.commit(); 
        if not generated_links: return {"status": "error", "message": "Failed to generate links."}
        return {"status": "success", "links": generated_links}
    finally: conn.close()

@app.post("/api/action/user/{action}/{user_db_id}")
async def api_action_user(action: str, user_db_id: int, req: Request, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, channel_id, user_name, expiry_date FROM joined_users WHERE id=?", (user_db_id,))
    row = cursor.fetchone()
    if not row: return {"status": "error", "message": "User not found."}
    uid, cid, uname, expiry = row[0], row[1], row[2], row[3]
    if not verify_channel_access(cursor, cid, user): return {"status": "error", "message": "Unauthorized"}
    if action == "revoke":
        cursor.execute("UPDATE joined_users SET status = 'REVOKED' WHERE id=?", (user_db_id,))
        conn.commit()
        requests.post(f"https://api.telegram.org/bot{get_bot_token()}/banChatMember", json={"chat_id": cid, "user_id": uid})
        requests.post(f"https://api.telegram.org/bot{get_bot_token()}/unbanChatMember", json={"chat_id": cid, "user_id": uid})
    elif action == "extend":
        days = (await req.json()).get("days")
        if expiry != 'lifetime':
            if str(days).lower() == 'lifetime': cursor.execute("UPDATE joined_users SET expiry_date='lifetime', status='ACTIVE' WHERE id=?", (user_db_id,))
            else:
                new_expiry = (datetime.datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S") + datetime.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("UPDATE joined_users SET expiry_date=?, status='ACTIVE' WHERE id=?", (new_expiry, user_db_id))
        conn.commit()
    conn.close(); return {"status": "success"}

@app.post("/api/action/link/delete/{link_id}")
async def api_action_link_delete(link_id: int, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT invite_link, channel_id FROM invite_links WHERE id=?", (link_id,))
    row = cursor.fetchone()
    if row:
        if not verify_channel_access(cursor, row[1], user): return {"status": "error", "message": "Unauthorized"}
        requests.post(f"https://api.telegram.org/bot{get_bot_token()}/revokeChatInviteLink", json={"chat_id": row[1], "invite_link": row[0]})
        cursor.execute("DELETE FROM invite_links WHERE id=?", (link_id,))
        conn.commit()
    conn.close(); return {"status": "success"}