# VERY IMPORTANT: Gevent monkey patching must happen before other imports 
# to ensure things like database drivers and socket handlers don't block the event loop.
import gevent.monkey
gevent.monkey.patch_all()

import os
import psycopg2
import psycopg2.extras
import uuid
import json
import io
from flask import Flask, request, render_template_string, redirect, url_for, jsonify, session, abort, send_file
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

# Load environment variables from .env
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-poll-key')

# Configure SocketIO to use gevent as the async mode
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Please add it to your .env file or Render dashboard.")

ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "supersecret")

def get_db_connection():
    # Connect using PostgreSQL
    conn = psycopg2.connect(DB_URL)
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Note: SQLite 'INTEGER PRIMARY KEY AUTOINCREMENT' -> Postgres 'SERIAL PRIMARY KEY'
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY, 
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            poll_id TEXT NOT NULL,
            text TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            FOREIGN KEY (poll_id) REFERENCES polls (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS options (
            id SERIAL PRIMARY KEY,
            question_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            votes INTEGER DEFAULT 0,
            FOREIGN KEY (question_id) REFERENCES questions (id) ON DELETE CASCADE
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

# Initialize DB on startup
init_db()

# ----------------- HTML TEMPLATES -----------------
# (Templates remain identical)

HTML_LOGIN = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login - LivePoll</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #4c1d95 100%);
        }
        .login-card {
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(20px);
            padding: 48px 40px;
            border-radius: 24px;
            box-shadow: 0 25px 60px rgba(0,0,0,0.3);
            width: 100%;
            max-width: 400px;
            text-align: center;
        }
        .brand { font-weight: 800; font-size: 28px; color: #f97316; margin-bottom: 8px; }
        .subtitle { font-size: 14px; color: #9ca3af; margin-bottom: 32px; }
        .error-msg { background: #fef2f2; color: #dc2626; padding: 10px 16px; border-radius: 10px; font-size: 13px; font-weight: 500; margin-bottom: 20px; }
        .form-group { margin-bottom: 16px; text-align: left; }
        .form-label { font-size: 12px; font-weight: 600; color: #6b7280; margin-bottom: 6px; display: block; text-transform: uppercase; letter-spacing: 0.5px; }
        .form-input {
            width: 100%; padding: 12px 16px;
            border: 2px solid #e5e7eb; border-radius: 12px;
            font-size: 15px; font-family: 'Inter', sans-serif;
            transition: border-color 0.2s; outline: none;
        }
        .form-input:focus { border-color: #f97316; }
        .btn-login {
            width: 100%; padding: 14px;
            background: linear-gradient(135deg, #f97316, #ea580c);
            color: #fff; border: none; border-radius: 12px;
            font-size: 16px; font-weight: 700; cursor: pointer;
            font-family: 'Inter', sans-serif;
            transition: transform 0.2s, box-shadow 0.2s;
            margin-top: 8px;
        }
        .btn-login:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(249,115,22,0.4); }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="brand">LivePoll</div>
        <div class="subtitle">Presenter Login</div>
        {% if error %}<div class="error-msg">{{ error }}</div>{% endif %}
        <form method="post">
            <div class="form-group">
                <label class="form-label">Username</label>
                <input type="text" name="username" placeholder="Enter username" required class="form-input">
            </div>
            <div class="form-group">
                <label class="form-label">Password</label>
                <input type="password" name="password" placeholder="Enter password" required class="form-input">
            </div>
            <button type="submit" class="btn-login">Sign In</button>
        </form>
    </div>
</body>
</html>
"""

HTML_ADMIN_DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - LivePoll</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background: #f5f5f7; color: #1d1d1f; min-height: 100vh; }
        .top-bar {
            background: #fff; border-bottom: 1px solid #e5e7eb;
            padding: 0 32px; height: 56px;
            display: flex; align-items: center; justify-content: space-between;
        }
        .brand { font-weight: 800; font-size: 20px; color: #f97316; }
        .logout-link { color: #6b7280; text-decoration: none; font-size: 13px; font-weight: 500; }
        .logout-link:hover { color: #ef4444; }
        .container { max-width: 800px; margin: 0 auto; padding: 40px 24px; }
        h1 { font-size: 28px; font-weight: 800; margin-bottom: 32px; }
        .create-card {
            background: #fff; border-radius: 16px; padding: 28px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 32px;
        }
        .create-card h2 { font-size: 18px; font-weight: 700; margin-bottom: 16px; }
        .create-form { display: flex; gap: 12px; }
        .create-input {
            flex: 1; padding: 12px 16px; border: 2px solid #e5e7eb; border-radius: 12px;
            font-size: 15px; font-family: 'Inter', sans-serif; outline: none;
            transition: border-color 0.2s;
        }
        .create-input:focus { border-color: #f97316; }
        .btn-create {
            background: linear-gradient(135deg, #f97316, #ea580c);
            color: #fff; border: none; padding: 12px 28px; border-radius: 12px;
            font-weight: 700; font-size: 15px; cursor: pointer; font-family: 'Inter', sans-serif;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn-create:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(249,115,22,0.3); }
        .poll-list { display: flex; flex-direction: column; gap: 12px; }
        .poll-card {
            background: #fff; border-radius: 14px; padding: 20px 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            display: flex; justify-content: space-between; align-items: center;
            transition: transform 0.15s, box-shadow 0.15s;
        }
        .poll-card:hover { transform: translateY(-2px); box-shadow: 0 4px 16px rgba(0,0,0,0.08); }
        .poll-title { font-weight: 700; font-size: 17px; }
        .btn-manage {
            background: #fff7ed; color: #f97316; padding: 10px 20px; border-radius: 10px;
            font-weight: 600; font-size: 14px; text-decoration: none;
            transition: background 0.2s;
        }
        .btn-manage:hover { background: #ffedd5; }
        .empty-state { text-align: center; padding: 60px 20px; color: #9ca3af; }
        .empty-icon { font-size: 48px; margin-bottom: 12px; opacity: 0.5; }
        .empty-text { font-size: 16px; font-weight: 500; }
    </style>
</head>
<body>
    <nav class="top-bar">
        <span class="brand">LivePoll</span>
        <a href="/logout" class="logout-link">Logout</a>
    </nav>
    <div class="container">
        <h1>Your Presentations</h1>
        <div class="create-card">
            <h2>Create New Presentation</h2>
            <form action="/create_poll" method="post" class="create-form">
                <input type="text" name="title" placeholder="Presentation Title (e.g. Q3 All Hands)" required class="create-input">
                <button type="submit" class="btn-create">Create</button>
            </form>
        </div>
        {% if polls %}
        <div class="poll-list">
            {% for poll in polls %}
            <div class="poll-card">
                <span class="poll-title">{{ poll.title }}</span>
                <a href="/admin/poll/{{ poll.id }}" class="btn-manage">Manage Questions &rarr;</a>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty-state">
            <div class="empty-icon">📊</div>
            <div class="empty-text">No presentations yet. Create one above!</div>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

HTML_POLL_ADMIN = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ poll.title }} - Presenter</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background: #f5f5f7; color: #1d1d1f; height: 100vh; overflow: hidden; }

        /* ---- Layout ---- */
        .app-layout { display: flex; flex-direction: column; height: 100vh; }

        /* ---- Top Nav ---- */
        .top-nav {
            display: flex; align-items: center; justify-content: space-between;
            padding: 0 24px; height: 56px; background: #fff;
            border-bottom: 1px solid #e5e7eb; flex-shrink: 0; z-index: 10;
        }
        .nav-left { display: flex; align-items: center; gap: 16px; }
        .nav-brand { font-weight: 800; font-size: 20px; color: #f97316; }
        .nav-sep { color: #d1d5db; }
        .nav-link { color: #6b7280; text-decoration: none; font-size: 14px; font-weight: 500; transition: color 0.2s; }
        .nav-link:hover { color: #1d1d1f; }
        .nav-title { font-weight: 700; font-size: 15px; color: #374151; }
        .nav-right { display: flex; align-items: center; gap: 12px; }
        .nav-live-dot { display: inline-block; width: 8px; height: 8px; background: #ef4444; border-radius: 50%; animation: blink 1.5s infinite; }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .nav-live-text { font-size: 13px; color: #6b7280; font-weight: 500; }
        .btn-present {
            display: inline-flex; align-items: center; gap: 6px;
            background: #22c55e; color: #fff; border: none; padding: 8px 20px;
            border-radius: 8px; font-weight: 600; font-size: 14px; cursor: pointer;
            text-decoration: none; transition: background 0.2s;
        }
        .btn-present:hover { background: #16a34a; }
        .btn-nav-end {
            color: #6b7280; text-decoration: none; font-size: 13px; font-weight: 500;
            padding: 8px 14px; border-radius: 8px; transition: all 0.2s;
        }
        .btn-nav-end:hover { background: #fee2e2; color: #dc2626; }

        /* ---- Main Content ---- */
        .main-content { display: flex; flex: 1; overflow: hidden; }

        /* ---- Sidebar ---- */
        .sidebar {
            width: 300px; background: #fff; border-right: 1px solid #e5e7eb;
            display: flex; flex-direction: column; flex-shrink: 0;
        }
        .sidebar-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 20px 20px 16px; border-bottom: 1px solid #f3f4f6;
        }
        .sidebar-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #9ca3af; }
        .btn-add {
            background: #f97316; color: #fff; border: none; padding: 6px 14px;
            border-radius: 6px; font-weight: 600; font-size: 13px; cursor: pointer;
            transition: all 0.2s; font-family: 'Inter', sans-serif;
        }
        .btn-add:hover { background: #ea580c; transform: translateY(-1px); }

        .question-list { flex: 1; overflow-y: auto; padding: 4px 0; }
        .question-list::-webkit-scrollbar { width: 4px; }
        .question-list::-webkit-scrollbar-track { background: transparent; }
        .question-list::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 4px; }

        .question-item {
            padding: 14px 20px; cursor: pointer;
            border-left: 3px solid transparent; transition: all 0.15s; position: relative;
        }
        .question-item:hover { background: #f9fafb; }
        .question-item.selected { background: #fff7ed; border-left-color: #f97316; }
        .question-item.active-q { border-left-color: #22c55e; }
        .question-item.selected.active-q { border-left-color: #f97316; }
        .q-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
        .q-info { flex: 1; min-width: 0; }
        .q-number { font-size: 12px; font-weight: 600; color: #9ca3af; margin-bottom: 2px; }
        .q-text {
            font-size: 14px; font-weight: 500; color: #374151; line-height: 1.4;
            overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
            -webkit-line-clamp: 2; -webkit-box-orient: vertical;
        }
        .q-actions { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
        .btn-golive {
            background: #22c55e; color: #fff; border: none; padding: 4px 12px;
            border-radius: 4px; font-weight: 600; font-size: 11px; cursor: pointer;
            transition: all 0.2s; font-family: 'Inter', sans-serif;
        }
        .btn-golive:hover { background: #16a34a; }
        .badge-live {
            background: #f97316; color: #fff; padding: 3px 10px; border-radius: 4px;
            font-weight: 700; font-size: 11px; letter-spacing: 0.5px;
            animation: pulse-live 2s infinite;
        }
        @keyframes pulse-live { 0%,100%{opacity:1} 50%{opacity:0.7} }

        /* ---- Right Panel ---- */
        .right-panel { flex: 1; overflow-y: auto; padding: 32px 40px; position: relative; }

        .panel-placeholder {
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; height: 100%; color: #9ca3af;
        }
        .panel-placeholder-icon { font-size: 56px; margin-bottom: 16px; opacity: 0.4; }
        .panel-placeholder-text { font-size: 16px; font-weight: 500; }
        .panel-placeholder-sub { font-size: 13px; margin-top: 6px; color: #c4c7cc; }

        /* ---- Results View ---- */
        .results-label {
            font-size: 11px; font-weight: 700; text-transform: uppercase;
            letter-spacing: 1px; color: #f97316; margin-bottom: 16px;
        }
        .results-question { font-size: 26px; font-weight: 800; color: #1d1d1f; line-height: 1.3; margin-bottom: 12px; }
        .results-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 32px; }
        .results-dot { width: 8px; height: 8px; border-radius: 50%; background: #f97316; }
        .results-count { font-size: 13px; color: #6b7280; font-weight: 500; }

        .option-item { margin-bottom: 20px; }
        .option-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
        .option-text { font-size: 15px; font-weight: 500; color: #374151; }
        .option-votes { font-size: 14px; font-weight: 600; color: #9ca3af; min-width: 28px; text-align: right; }
        .option-bar-bg {
            height: 28px; background: #f3f4f6; border-radius: 8px;
            overflow: hidden; position: relative;
        }
        .option-bar {
            height: 100%;
            background: linear-gradient(90deg, #fdba74, #f97316);
            border-radius: 8px;
            transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1);
            min-width: 0;
        }
        .results-actions { display: flex; gap: 12px; margin-top: 32px; }
        .btn-edit {
            background: #e5e7eb; color: #374151; border: none;
            padding: 10px 24px; border-radius: 8px;
            font-weight: 600; font-size: 14px; cursor: pointer;
            transition: all 0.2s; font-family: 'Inter', sans-serif;
        }
        .btn-edit:hover { background: #d1d5db; }

        /* ---- QR Section ---- */
        .qr-float {
            position: fixed; bottom: 20px; right: 20px;
            background: #fff; padding: 12px; border-radius: 14px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.12); text-align: center;
            z-index: 20; transition: transform 0.2s;
        }
        .qr-float:hover { transform: scale(1.05); }
        .qr-float img { width: 110px; height: 110px; border-radius: 6px; display: block; }
        .qr-label { font-size: 10px; font-weight: 600; color: #9ca3af; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
        .qr-download {
            display: inline-block; margin-top: 6px; font-size: 11px; color: #f97316;
            text-decoration: none; font-weight: 600; transition: color 0.2s;
        }
        .qr-download:hover { color: #ea580c; }

        /* ---- Form Styles ---- */
        .form-section { max-width: 600px; }
        .form-title { font-size: 24px; font-weight: 800; color: #1d1d1f; margin-bottom: 24px; }
        .form-group { margin-bottom: 20px; }
        .form-label {
            font-size: 12px; font-weight: 600; color: #6b7280; margin-bottom: 8px;
            display: block; text-transform: uppercase; letter-spacing: 0.5px;
        }
        .form-input {
            width: 100%; padding: 12px 16px; border: 2px solid #e5e7eb; border-radius: 10px;
            font-size: 15px; font-family: 'Inter', sans-serif;
            transition: border-color 0.2s, box-shadow 0.2s; outline: none;
        }
        .form-input:focus { border-color: #f97316; box-shadow: 0 0 0 3px rgba(249,115,22,0.1); }
        .options-list { display: flex; flex-direction: column; gap: 10px; }
        .option-input-row { display: flex; gap: 8px; align-items: center; }
        .option-input-row .form-input { flex: 1; }
        .btn-remove-opt {
            background: none; border: none; color: #d1d5db; font-size: 22px;
            cursor: pointer; padding: 8px; border-radius: 6px; transition: all 0.2s;
            line-height: 1;
        }
        .btn-remove-opt:hover { background: #fee2e2; color: #ef4444; }
        .btn-add-opt {
            background: none; border: 2px dashed #d1d5db; color: #6b7280;
            padding: 10px; border-radius: 10px; font-size: 14px; font-weight: 500;
            cursor: pointer; transition: all 0.2s; width: 100%;
            font-family: 'Inter', sans-serif; margin-top: 10px;
        }
        .btn-add-opt:hover { border-color: #f97316; color: #f97316; }
        .form-actions { display: flex; gap: 12px; margin-top: 28px; align-items: center; }
        .btn-save {
            background: linear-gradient(135deg, #f97316, #ea580c); color: #fff; border: none;
            padding: 12px 28px; border-radius: 10px; font-weight: 600; font-size: 15px;
            cursor: pointer; transition: all 0.2s; font-family: 'Inter', sans-serif;
        }
        .btn-save:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(249,115,22,0.3); }
        .btn-cancel {
            background: #f3f4f6; color: #6b7280; border: none;
            padding: 12px 28px; border-radius: 10px; font-weight: 600; font-size: 15px;
            cursor: pointer; transition: all 0.2s; font-family: 'Inter', sans-serif;
        }
        .btn-cancel:hover { background: #e5e7eb; color: #374151; }
        .btn-delete {
            background: none; color: #ef4444; border: 2px solid #fecaca;
            padding: 10px 24px; border-radius: 10px; font-weight: 600; font-size: 14px;
            cursor: pointer; transition: all 0.2s; margin-left: auto;
            font-family: 'Inter', sans-serif;
        }
        .btn-delete:hover { background: #fee2e2; border-color: #ef4444; }

        /* ---- Animations ---- */
        .fade-in { animation: fadeIn 0.25s ease-out; }
        @keyframes fadeIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:none} }

        /* ---- Empty sidebar ---- */
        .sidebar-empty { padding: 40px 20px; text-align: center; color: #c4c7cc; font-size: 14px; line-height: 1.6; }
    </style>
</head>
<body>
    <div class="app-layout">
        <!-- Top Navigation -->
        <nav class="top-nav">
            <div class="nav-left">
                <span class="nav-brand">LivePoll</span>
                <span class="nav-sep">|</span>
                <span class="nav-title">{{ poll.title }}</span>
                <span class="nav-sep">&middot;</span>
                <a href="/" class="nav-link">&larr; Dashboard</a>
            </div>
            <div class="nav-right">
                <span id="live-indicator" style="display:none">
                    <span class="nav-live-dot"></span>
                    <span class="nav-live-text">1 live</span>
                </span>
                <a href="/present/{{ poll.id }}" target="_blank" class="btn-present">&#x1F4CA; Present</a>
                <a href="/" class="btn-nav-end">End session</a>
            </div>
        </nav>

        <!-- Main Content -->
        <div class="main-content">
            <!-- Left Sidebar -->
            <aside class="sidebar">
                <div class="sidebar-header">
                    <span class="sidebar-title">Questions</span>
                    <button class="btn-add" onclick="showAddForm()">+ Add</button>
                </div>
                <div class="question-list" id="question-list"></div>
            </aside>

            <!-- Right Panel -->
            <main class="right-panel" id="right-panel"></main>
        </div>
    </div>

    <!-- QR Code Float -->
    <div class="qr-float" id="qr-float">
        <img src="/qr/{{ poll.id }}" alt="QR Code to vote">
        <div class="qr-label">Scan to vote</div>
        <a href="/qr/{{ poll.id }}" download="poll-qr-{{ poll.id }}.png" class="qr-download">&#x2B07; Download PNG</a>
    </div>

    <script>
        const pollId = "{{ poll.id }}";
        let allQuestions = {{ questions_json | safe }};
        let selectedQuestionId = null;
        let mode = 'none'; // 'none' | 'view' | 'edit' | 'add'

        // ---- Escape HTML ----
        function esc(text) {
            const d = document.createElement('div');
            d.textContent = text;
            return d.innerHTML;
        }

        // ---- Render Sidebar ----
        function renderSidebar() {
            const list = document.getElementById('question-list');
            if (allQuestions.length === 0) {
                list.innerHTML = '<div class="sidebar-empty">No questions yet.<br>Click <strong>+ Add</strong> to create one.</div>';
                updateLiveIndicator();
                return;
            }
            let html = '';
            allQuestions.forEach((q, i) => {
                const isSel = q.id === selectedQuestionId;
                const isActive = q.is_active === 1;
                let cls = 'question-item';
                if (isSel) cls += ' selected';
                if (isActive) cls += ' active-q';

                html += '<div class="' + cls + '" onclick="selectQuestion(' + q.id + ')">';
                html += '  <div class="q-info">';
                html += '    <div class="q-number">Q' + (i + 1) + '</div>';
                html += '    <div class="q-text">' + esc(q.text) + '</div>';
                html += '  </div>';
                html += '  <div class="q-actions">';
                if (isActive) {
                    html += '<span class="badge-live">LIVE</span>';
                } else {
                    html += '<button class="btn-golive" onclick="event.stopPropagation(); goLive(' + q.id + ')">&#x25B6; Go live</button>';
                }
                html += '  </div>';
                html += '</div>';
            });
            list.innerHTML = html;
            updateLiveIndicator();
        }

        function updateLiveIndicator() {
            const liveQ = allQuestions.find(q => q.is_active === 1);
            const el = document.getElementById('live-indicator');
            if (liveQ) {
                el.style.display = 'inline-flex';
                el.style.alignItems = 'center';
                el.style.gap = '6px';
            } else {
                el.style.display = 'none';
            }
        }

        // ---- Render Right Panel ----
        function renderRightPanel() {
            const panel = document.getElementById('right-panel');

            if (mode === 'none') {
                panel.innerHTML = '<div class="panel-placeholder fade-in">' +
                    '<div class="panel-placeholder-icon">&#x1F4CB;</div>' +
                    '<div class="panel-placeholder-text">Select a question to view results</div>' +
                    '<div class="panel-placeholder-sub">Or click + Add to create a new one</div>' +
                    '</div>';
                return;
            }

            if (mode === 'add') { renderAddForm(panel); return; }
            if (mode === 'edit') { renderEditForm(panel); return; }

            // View mode
            const q = allQuestions.find(x => x.id === selectedQuestionId);
            if (!q) { mode = 'none'; renderRightPanel(); return; }

            const totalVotes = q.options.reduce((s, o) => s + o.votes, 0);
            const maxVotes = Math.max(...q.options.map(o => o.votes), 1);

            let html = '<div class="fade-in">';
            html += '<div class="results-label">' + (q.is_active ? 'Live Results' : 'Results') + '</div>';
            html += '<h1 class="results-question">' + esc(q.text) + '</h1>';
            html += '<div class="results-meta">';
            html += '  <span class="results-dot" style="background:' + (q.is_active ? '#22c55e' : '#f97316') + '"></span>';
            html += '  <span class="results-count">' + totalVotes + ' response' + (totalVotes !== 1 ? 's' : '') + '</span>';
            html += '</div>';

            q.options.forEach(opt => {
                const pct = totalVotes > 0 ? (opt.votes / maxVotes * 100) : 0;
                html += '<div class="option-item">';
                html += '  <div class="option-header">';
                html += '    <span class="option-text">' + esc(opt.text) + '</span>';
                html += '    <span class="option-votes">' + opt.votes + '</span>';
                html += '  </div>';
                html += '  <div class="option-bar-bg">';
                html += '    <div class="option-bar" style="width:' + pct + '%"></div>';
                html += '  </div>';
                html += '</div>';
            });

            html += '<div class="results-actions">';
            html += '<button class="btn-edit" onclick="startEdit()">&#x270F;&#xFE0F; Edit Question</button>';
            if (!q.is_active) {
                html += '<button class="btn-golive" style="padding:10px 20px;font-size:13px;border-radius:8px" onclick="goLive(' + q.id + ')">&#x25B6; Go Live</button>';
            }
            html += '</div>';
            html += '</div>';

            panel.innerHTML = html;
        }

        // ---- Add Form ----
        function renderAddForm(panel) {
            let html = '<div class="form-section fade-in">';
            html += '<h2 class="form-title">Add a Question</h2>';
            html += '<div class="form-group">';
            html += '  <label class="form-label">Question Text</label>';
            html += '  <input type="text" id="new-q-text" class="form-input" placeholder="Type your question here...">';
            html += '</div>';
            html += '<div class="form-group">';
            html += '  <label class="form-label">Options</label>';
            html += '  <div class="options-list" id="new-opts">';
            html += '    <div class="option-input-row"><input type="text" class="form-input opt-input" placeholder="Option 1"></div>';
            html += '    <div class="option-input-row"><input type="text" class="form-input opt-input" placeholder="Option 2"></div>';
            html += '  </div>';
            html += '  <button class="btn-add-opt" data-target="new-opts">+ Add Option</button>';
            html += '</div>';
            html += '<div class="form-actions">';
            html += '  <button class="btn-save" onclick="saveNewQuestion()">Save Question</button>';
            html += '  <button class="btn-cancel" onclick="cancelForm()">Cancel</button>';
            html += '</div>';
            html += '</div>';
            panel.innerHTML = html;
            panel.querySelector('.btn-add-opt').addEventListener('click', function() { addOptInput(this.dataset.target); });
            setTimeout(() => document.getElementById('new-q-text')?.focus(), 100);
        }

        // ---- Edit Form ----
        function renderEditForm(panel) {
            const q = allQuestions.find(x => x.id === selectedQuestionId);
            if (!q) return;

            let optsHtml = '';
            q.options.forEach((opt, i) => {
                optsHtml += '<div class="option-input-row" data-opt-id="' + opt.id + '">';
                optsHtml += '  <input type="text" class="form-input opt-input" value="' + esc(opt.text) + '" placeholder="Option ' + (i+1) + '">';
                if (q.options.length > 2) {
                    optsHtml += '  <button class="btn-remove-opt" onclick="this.parentElement.remove()">&times;</button>';
                }
                optsHtml += '</div>';
            });

            let html = '<div class="form-section fade-in">';
            html += '<h2 class="form-title">Edit Question</h2>';
            html += '<div class="form-group">';
            html += '  <label class="form-label">Question Text</label>';
            html += '  <input type="text" id="edit-q-text" class="form-input" value="' + esc(q.text) + '">';
            html += '</div>';
            html += '<div class="form-group">';
            html += '  <label class="form-label">Options</label>';
            html += '  <div class="options-list" id="edit-opts">' + optsHtml + '</div>';
            html += '  <button class="btn-add-opt" data-target="edit-opts">+ Add Option</button>';
            html += '</div>';
            html += '<div class="form-actions">';
            html += '  <button class="btn-save" onclick="saveEditQuestion()">Save Changes</button>';
            html += '  <button class="btn-cancel" onclick="cancelEdit()">Cancel</button>';
            html += '  <button class="btn-delete" onclick="deleteQuestion()">&#x1F5D1; Delete</button>';
            html += '</div>';
            html += '</div>';
            panel.innerHTML = html;
            panel.querySelector('.btn-add-opt').addEventListener('click', function() { addOptInput(this.dataset.target); });
        }

        // ---- Actions ----
        function selectQuestion(id) {
            selectedQuestionId = id;
            mode = 'view';
            renderSidebar();
            renderRightPanel();
        }

        function showAddForm() {
            selectedQuestionId = null;
            mode = 'add';
            renderSidebar();
            renderRightPanel();
        }

        function startEdit() {
            mode = 'edit';
            renderRightPanel();
        }

        function cancelEdit() {
            mode = 'view';
            renderRightPanel();
        }

        function cancelForm() {
            selectedQuestionId = null;
            mode = 'none';
            renderSidebar();
            renderRightPanel();
        }

        async function goLive(questionId) {
            try {
                const res = await fetch('/activate_question/' + pollId + '/' + questionId, { method: 'POST' });
                if (res.ok) {
                    allQuestions.forEach(q => q.is_active = (q.id === questionId) ? 1 : 0);
                    renderSidebar();
                    if (selectedQuestionId === questionId || mode === 'view') renderRightPanel();
                }
            } catch (e) { console.error('Failed to activate:', e); }
        }

        async function saveNewQuestion() {
            const text = document.getElementById('new-q-text').value.trim();
            const inputs = document.querySelectorAll('#new-opts .opt-input');
            const options = Array.from(inputs).map(i => i.value.trim()).filter(v => v);

            if (!text) { alert('Please enter a question.'); return; }
            if (options.length < 2) { alert('Please add at least 2 options.'); return; }

            try {
                const res = await fetch('/api/add_question/' + pollId, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question: text, options: options })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    allQuestions.push(data.question);
                    selectedQuestionId = data.question.id;
                    mode = 'view';
                    renderSidebar();
                    renderRightPanel();
                }
            } catch (e) { console.error('Failed to save:', e); }
        }

        async function saveEditQuestion() {
            const text = document.getElementById('edit-q-text').value.trim();
            const rows = document.querySelectorAll('#edit-opts .option-input-row');
            const options = [];
            rows.forEach(row => {
                const input = row.querySelector('.opt-input');
                const val = input.value.trim();
                const optId = row.dataset.optId;
                if (val) {
                    options.push(optId ? { id: parseInt(optId), text: val } : { text: val });
                }
            });

            if (!text) { alert('Please enter a question.'); return; }
            if (options.length < 2) { alert('Please add at least 2 options.'); return; }

            try {
                const res = await fetch('/api/question/' + pollId + '/' + selectedQuestionId, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question: text, options: options })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    const idx = allQuestions.findIndex(q => q.id === selectedQuestionId);
                    if (idx !== -1) allQuestions[idx] = data.question;
                    mode = 'view';
                    renderSidebar();
                    renderRightPanel();
                }
            } catch (e) { console.error('Failed to update:', e); }
        }

        async function deleteQuestion() {
            if (!confirm('Delete this question and all its votes?')) return;
            try {
                const res = await fetch('/api/question/' + pollId + '/' + selectedQuestionId, { method: 'DELETE' });
                const data = await res.json();
                if (data.status === 'success') {
                    allQuestions = allQuestions.filter(q => q.id !== selectedQuestionId);
                    selectedQuestionId = null;
                    mode = 'none';
                    renderSidebar();
                    renderRightPanel();
                }
            } catch (e) { console.error('Failed to delete:', e); }
        }

        function addOptInput(listId) {
            const list = document.getElementById(listId);
            const count = list.querySelectorAll('.opt-input').length + 1;
            const row = document.createElement('div');
            row.className = 'option-input-row';
            row.innerHTML = '<input type="text" class="form-input opt-input" placeholder="Option ' + count + '">' +
                '<button class="btn-remove-opt" onclick="this.parentElement.remove()">&times;</button>';
            list.appendChild(row);
            row.querySelector('input').focus();
        }

        // ---- Init ----
        renderSidebar();
        renderRightPanel();
    </script>
</body>
</html>
"""

HTML_PRESENT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Results</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            min-height: 100vh; display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            background: #0f0f13; color: #fff; padding: 40px;
            position: relative;
        }
        #content { width: 100%; max-w: 900px; text-align: center; }
        .waiting { font-size: 2.5rem; font-weight: 700; color: #4b5563; animation: pulse 2s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

        .q-title { font-size: 2.8rem; font-weight: 800; color: #fff; letter-spacing: -0.5px; margin-bottom: 40px; line-height: 1.2; }

        .results-table {
            background: #1a1a24; border-radius: 20px; overflow: hidden;
            box-shadow: 0 8px 40px rgba(0,0,0,0.4); border: 1px solid #2a2a3a;
            width: 100%; max-width: 900px;
        }
        .results-table table { width: 100%; border-collapse: collapse; }
        .results-table thead tr { background: #22223a; }
        .results-table th {
            padding: 18px 28px; text-align: left; color: #9ca3af;
            text-transform: uppercase; font-size: 14px; font-weight: 600; letter-spacing: 1px;
        }
        .results-table th:last-child { text-align: right; }
        .results-table td { padding: 20px 28px; border-bottom: 1px solid #2a2a3a; }
        .results-table tbody tr { transition: background 0.2s; }
        .results-table tbody tr:hover { background: #22223a; }
        .results-table .opt-name { font-size: 1.2rem; font-weight: 500; color: #e5e7eb; }
        .results-table .opt-votes { font-size: 1.8rem; font-weight: 800; color: #f97316; text-align: right; }
        .results-table tfoot td {
            padding: 18px 28px; background: linear-gradient(135deg, #f97316, #ea580c);
            color: #fff; font-weight: 700; font-size: 1rem; text-transform: uppercase;
        }
        .results-table tfoot td:last-child { text-align: right; font-size: 1.3rem; }

        .join-bar {
            margin-top: 48px; display: inline-flex; align-items: center; gap: 20px;
            background: #1a1a24; padding: 12px 28px; border-radius: 60px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3); border: 1px solid #2a2a3a;
        }
        .join-text { color: #6b7280; font-size: 14px; }
        .join-url { color: #fff; font-weight: 700; font-size: 18px; }

        /* QR in corner */
        .qr-corner {
            position: fixed; bottom: 20px; right: 20px;
            background: #fff; padding: 10px; border-radius: 12px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.4);
        }
        .qr-corner img { width: 100px; height: 100px; display: block; border-radius: 4px; }

        /* Presenter controls */
        .presenter-controls {
            position: fixed; bottom: 0; left: 0; width: 100%;
            padding: 16px 24px; display: flex; justify-content: space-between; align-items: center;
            background: rgba(26,26,36,0.95); border-top: 1px solid #2a2a3a;
            opacity: 0; transition: opacity 0.3s;
        }
        .presenter-controls:hover { opacity: 1; }
        .ctrl-btn {
            background: #2a2a3a; color: #e5e7eb; border: none;
            padding: 10px 24px; border-radius: 10px;
            font-weight: 600; font-size: 14px; cursor: pointer;
            transition: background 0.2s; font-family: 'Inter', sans-serif;
        }
        .ctrl-btn:hover { background: #3a3a4a; }
        .ctrl-hint { color: #4b5563; font-size: 13px; }
    </style>
</head>
<body>
    <div id="content" style="max-width:900px;width:100%">
        <h2 class="waiting">Waiting for the presenter to start...</h2>
    </div>

    <div class="join-bar">
        <span class="join-text">Join at:</span>
        <span class="join-url">{{ base_url }}vote/{{ poll.id }}</span>
    </div>

    <!-- QR Code Corner -->
    <div class="qr-corner">
        <img src="/qr/{{ poll.id }}" alt="QR Code">
    </div>

    <!-- Hover Controls -->
    <div class="presenter-controls">
        <button onclick="prevQuestion()" class="ctrl-btn">&larr; Prev</button>
        <span class="ctrl-hint">Use Left/Right Arrow Keys to Navigate</span>
        <button onclick="nextQuestion()" class="ctrl-btn">Next &rarr;</button>
    </div>

    <script>
        const pollId = "{{ poll.id }}";
        const socket = io();
        const allQuestions = {{ questions_json | safe }};
        let currentOptions = [];
        let currentIndex = -1;

        socket.on('connect', () => {
            socket.emit('join', {poll_id: pollId});
        });

        socket.on('question_activated', (data) => {
            currentOptions = data.options;
            renderTable(data.question);
            currentIndex = allQuestions.findIndex(q => q.id === data.question_id);
        });

        socket.on('vote_update', (data) => {
            const option = currentOptions.find(o => o.id === data.option_id);
            if (option) {
                option.votes = data.votes;
                updateTableUI();
            }
        });

        function renderTable(question) {
            let html = '<h1 class="q-title">' + question + '</h1>';
            html += '<div class="results-table"><table>';
            html += '<thead><tr><th>Option</th><th style="text-align:right">Votes</th></tr></thead>';
            html += '<tbody id="table-body"></tbody>';
            html += '<tfoot><tr><td>Total Responses</td><td id="total-votes" style="text-align:right">0</td></tr></tfoot>';
            html += '</table></div>';
            document.getElementById('content').innerHTML = html;
            updateTableUI();
        }

        function updateTableUI() {
            const tbody = document.getElementById('table-body');
            if (!tbody) return;
            let html = '';
            let total = 0;
            currentOptions.forEach(opt => {
                html += '<tr><td class="opt-name">' + opt.text + '</td><td class="opt-votes">' + opt.votes + '</td></tr>';
                total += opt.votes;
            });
            tbody.innerHTML = html;
            document.getElementById('total-votes').innerText = total;
        }

        async function activateQuestionId(qId) {
            if (!qId) return;
            await fetch('/activate_question/' + pollId + '/' + qId, { method: 'POST' });
        }

        function nextQuestion() {
            if (allQuestions.length === 0) return;
            let nextIdx = currentIndex + 1;
            if (nextIdx >= allQuestions.length) nextIdx = allQuestions.length - 1;
            activateQuestionId(allQuestions[nextIdx].id);
        }

        function prevQuestion() {
            if (allQuestions.length === 0) return;
            let prevIdx = currentIndex - 1;
            if (prevIdx < 0) prevIdx = 0;
            activateQuestionId(allQuestions[prevIdx].id);
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowRight' || e.key === ' ') nextQuestion();
            else if (e.key === 'ArrowLeft') prevQuestion();
        });
    </script>
</body>
</html>
"""

HTML_VOTE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vote - LivePoll</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            min-height: 100vh; display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            background: #f5f5f7; padding: 20px;
        }
        #content {
            background: #fff; padding: 32px; border-radius: 20px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            width: 100%; max-width: 480px; text-align: center;
        }
        .waiting { font-size: 1.3rem; font-weight: 600; color: #9ca3af; padding: 40px 0; animation: pulse 2s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
        .vote-q { font-size: 1.4rem; font-weight: 800; color: #1d1d1f; margin-bottom: 24px; line-height: 1.3; }
        .vote-btn {
            width: 100%; background: #fff; border: 2px solid #e5e7eb;
            color: #374151; font-weight: 600; font-size: 1rem;
            padding: 16px 20px; border-radius: 14px; cursor: pointer;
            text-align: left; margin-bottom: 10px;
            transition: all 0.2s; font-family: 'Inter', sans-serif;
        }
        .vote-btn:hover { border-color: #f97316; background: #fff7ed; color: #f97316; }
        .result-card { text-align: left; }
        .result-card h1 { font-size: 1.2rem; font-weight: 700; color: #1d1d1f; margin-bottom: 16px; }
        .result-table {
            width: 100%; border-collapse: collapse;
            background: #f9fafb; border-radius: 12px; overflow: hidden;
            border: 1px solid #e5e7eb;
        }
        .result-table th {
            padding: 10px 16px; background: #f3f4f6;
            text-align: left; font-size: 11px; text-transform: uppercase;
            color: #6b7280; font-weight: 600; letter-spacing: 0.5px;
        }
        .result-table th:last-child { text-align: right; }
        .result-table td { padding: 12px 16px; border-bottom: 1px solid #e5e7eb; }
        .result-table td:last-child { text-align: right; font-weight: 700; color: #f97316; }
        .result-table tfoot td {
            background: #f3f4f6; font-weight: 700; color: #374151; font-size: 13px;
        }
        .voted-msg {
            margin-top: 12px; background: #f0fdf4; color: #16a34a;
            padding: 10px 16px; border-radius: 10px; font-size: 13px; font-weight: 600;
        }
    </style>
</head>
<body>
    <div id="content">
        <h2 class="waiting">Waiting for the next question...</h2>
    </div>

    <script>
        const pollId = "{{ poll_id }}";
        const socket = io();
        let currentQuestionId = null;
        let currentOptions = [];

        socket.on('connect', () => {
            socket.emit('join', {poll_id: pollId});
        });

        socket.on('question_activated', (data) => {
            currentQuestionId = data.question_id;
            currentOptions = data.options;
            
            // Namespace the localStorage key using the unique pollId to avoid clashes 
            // from old testing sessions (especially if DB resets IDs to 1)
            const storageKey = 'voted_' + pollId + '_' + currentQuestionId;
            
            if (localStorage.getItem(storageKey)) {
                showResultsTable(data.question);
            } else {
                renderVotingForm(data.question);
            }
        });

        socket.on('vote_update', (data) => {
            const option = currentOptions.find(o => o.id === data.option_id);
            if (option) option.votes = data.votes;
            if (document.getElementById('voter-results-table')) updateTableUI();
        });

        function renderVotingForm(question) {
            let html = '<h1 class="vote-q">' + question + '</h1><div>';
            currentOptions.forEach(opt => {
                html += '<button class="vote-btn" onclick="submitVote(' + opt.id + ')">' + opt.text + '</button>';
            });
            html += '</div>';
            document.getElementById('content').innerHTML = html;
        }

        async function submitVote(optionId) {
            try {
                const response = await fetch('/vote/' + pollId, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ option_id: optionId })
                });
                if (response.ok) {
                    const storageKey = 'voted_' + pollId + '_' + currentQuestionId;
                    localStorage.setItem(storageKey, 'true');
                    
                    const option = currentOptions.find(o => o.id === optionId);
                    if (option) option.votes += 1;
                    const questionText = document.querySelector('.vote-q')?.innerText || 'Question';
                    showResultsTable(questionText);
                }
            } catch (error) {
                console.error('Error submitting vote:', error);
            }
        }

        function showResultsTable(question) {
            let html = '<div class="result-card">';
            html += '<h1>' + question + '</h1>';
            html += '<table id="voter-results-table" class="result-table">';
            html += '<thead><tr><th>Option</th><th>Votes</th></tr></thead>';
            html += '<tbody id="table-body"></tbody>';
            html += '<tfoot><tr><td>Total</td><td id="total-votes">0</td></tr></tfoot>';
            html += '</table>';
            html += '<div class="voted-msg">&#x2705; Vote recorded! Live results above.</div>';
            html += '</div>';
            document.getElementById('content').innerHTML = html;
            updateTableUI();
        }

        function updateTableUI() {
            const tbody = document.getElementById('table-body');
            if (!tbody) return;
            let html = '';
            let total = 0;
            currentOptions.forEach(opt => {
                html += '<tr><td>' + opt.text + '</td><td>' + opt.votes + '</td></tr>';
                total += opt.votes;
            });
            tbody.innerHTML = html;
            document.getElementById('total-votes').innerText = total;
        }
    </script>
</body>
</html>
"""

# ----------------- ADMIN ROUTES -----------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USER and request.form.get('password') == ADMIN_PASS:
            session['admin'] = True
            return redirect(url_for('dashboard'))
        return render_template_string(HTML_LOGIN, error="Invalid credentials")
    return render_template_string(HTML_LOGIN)

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))

@app.route('/')
def dashboard():
    if not session.get('admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM polls ORDER BY created_at DESC")
    polls = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return render_template_string(HTML_ADMIN_DASHBOARD, polls=[dict(p) for p in polls])

@app.route('/create_poll', methods=['POST'])
def create_poll():
    if not session.get('admin'): return abort(403)
    title = request.form.get("title")
    poll_id = str(uuid.uuid4())[:8]
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO polls (id, title) VALUES (%s, %s)", (poll_id, title))
    conn.commit()
    cursor.close()
    conn.close()
    
    return redirect(url_for('manage_poll', poll_id=poll_id))

@app.route('/admin/poll/<poll_id>')
def manage_poll(poll_id):
    if not session.get('admin'): return redirect(url_for('login'))
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        cursor.close()
        conn.close()
        return "Poll not found", 404
        
    cursor.execute("SELECT * FROM questions WHERE poll_id = %s ORDER BY id ASC", (poll_id,))
    questions = cursor.fetchall()
    
    q_data = []
    for q in questions:
        q_dict = dict(q)
        cursor.execute("SELECT * FROM options WHERE question_id = %s ORDER BY id ASC", (q['id'],))
        opts = cursor.fetchall()
        q_dict['options'] = [dict(o) for o in opts]
        q_data.append(q_dict)
        
    cursor.close()
    conn.close()
    
    return render_template_string(HTML_POLL_ADMIN, poll=dict(poll), questions=q_data, questions_json=json.dumps(q_data))

@app.route('/add_question/<poll_id>', methods=['POST'])
def add_question(poll_id):
    if not session.get('admin'): return abort(403)
    text = request.form.get("question")
    options = request.form.getlist("options")
    options = [opt for opt in options if opt.strip()]
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Use RETURNING id for PostgreSQL instead of lastrowid
    cursor.execute("INSERT INTO questions (poll_id, text) VALUES (%s, %s) RETURNING id", (poll_id, text))
    question_id = cursor.fetchone()[0]
    
    for opt in options:
        cursor.execute("INSERT INTO options (question_id, text) VALUES (%s, %s)", (question_id, opt))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return redirect(url_for('manage_poll', poll_id=poll_id))

@app.route('/activate_question/<poll_id>/<question_id>', methods=['POST'])
def activate_question(poll_id, question_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("UPDATE questions SET is_active = 0 WHERE poll_id = %s", (poll_id,))
    cursor.execute("UPDATE questions SET is_active = 1 WHERE id = %s", (question_id,))
    conn.commit()
    
    cursor.execute("SELECT * FROM questions WHERE id = %s", (question_id,))
    question = cursor.fetchone()
    
    cursor.execute("SELECT * FROM options WHERE question_id = %s ORDER BY id ASC", (question_id,))
    options = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    socketio.emit('question_activated', {
        'question_id': int(question_id),
        'question': question['text'],
        'options': [dict(o) for o in options]
    }, to=poll_id)
    
    return jsonify({"status": "success"})

# ----------------- JSON API ROUTES -----------------

@app.route('/api/add_question/<poll_id>', methods=['POST'])
def api_add_question(poll_id):
    if not session.get('admin'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    text = data.get("question", "").strip()
    options = [o.strip() for o in data.get("options", []) if o.strip()]
    
    if not text or len(options) < 2:
        return jsonify({"status": "error", "message": "Question and at least 2 options required"}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("INSERT INTO questions (poll_id, text) VALUES (%s, %s) RETURNING id", (poll_id, text))
    question_id = cursor.fetchone()[0]
    
    option_data = []
    for opt_text in options:
        cursor.execute("INSERT INTO options (question_id, text) VALUES (%s, %s) RETURNING id", (question_id, opt_text))
        opt_id = cursor.fetchone()[0]
        option_data.append({
            "id": opt_id,
            "question_id": question_id,
            "text": opt_text,
            "votes": 0
        })
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({
        "status": "success",
        "question": {
            "id": question_id,
            "poll_id": poll_id,
            "text": text,
            "is_active": 0,
            "options": option_data
        }
    })

@app.route('/api/question/<poll_id>/<int:question_id>', methods=['PUT'])
def api_edit_question(poll_id, question_id):
    if not session.get('admin'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    text = data.get("question", "").strip()
    options = data.get("options", [])
    
    if not text:
        return jsonify({"status": "error", "message": "Question text required"}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("UPDATE questions SET text = %s WHERE id = %s AND poll_id = %s", (text, question_id, poll_id))
    
    cursor.execute("SELECT id FROM options WHERE question_id = %s", (question_id,))
    existing_opts = cursor.fetchall()
    existing_ids = set(o['id'] for o in existing_opts)
    
    kept_ids = set()
    option_data = []
    
    for opt in options:
        opt_text = opt.get("text", "").strip() if isinstance(opt, dict) else str(opt).strip()
        if not opt_text: continue
        
        opt_id = opt.get("id") if isinstance(opt, dict) else None
        
        if opt_id and opt_id in existing_ids:
            cursor.execute("UPDATE options SET text = %s WHERE id = %s", (opt_text, opt_id))
            kept_ids.add(opt_id)
            cursor.execute("SELECT * FROM options WHERE id = %s", (opt_id,))
            o = cursor.fetchone()
            option_data.append(dict(o))
        else:
            cursor.execute("INSERT INTO options (question_id, text) VALUES (%s, %s) RETURNING id", (question_id, opt_text))
            new_opt_id = cursor.fetchone()[0]
            option_data.append({
                "id": new_opt_id,
                "question_id": question_id,
                "text": opt_text,
                "votes": 0
            })
            
    removed_ids = existing_ids - kept_ids
    for oid in removed_ids:
        cursor.execute("DELETE FROM options WHERE id = %s", (oid,))
    
    cursor.execute("SELECT is_active FROM questions WHERE id = %s", (question_id,))
    q = cursor.fetchone()
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({
        "status": "success",
        "question": {
            "id": question_id,
            "poll_id": poll_id,
            "text": text,
            "is_active": q["is_active"] if q else 0,
            "options": option_data
        }
    })

@app.route('/api/question/<poll_id>/<int:question_id>', methods=['DELETE'])
def api_delete_question(poll_id, question_id):
    if not session.get('admin'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM options WHERE question_id = %s", (question_id,))
    cursor.execute("DELETE FROM questions WHERE id = %s AND poll_id = %s", (question_id, poll_id))
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({"status": "success"})

# ----------------- QR CODE ROUTE -----------------

@app.route('/qr/<poll_id>')
def get_qr(poll_id):
    if not HAS_QRCODE:
        svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
            <rect width="200" height="200" fill="#f3f4f6"/>
            <text x="100" y="95" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#9ca3af">QR Code</text>
            <text x="100" y="115" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#d1d5db">pip install qrcode[pil]</text>
        </svg>'''
        return svg, 200, {'Content-Type': 'image/svg+xml'}
    
    base_url = request.url_root
    vote_url = f"{base_url}vote/{poll_id}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(vote_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    return send_file(buf, mimetype='image/png', download_name=f'poll-qr-{poll_id}.png')

# ----------------- PUBLIC ROUTES -----------------

@app.route('/vote/<poll_id>')
def view_vote(poll_id):
    return render_template_string(HTML_VOTE, poll_id=poll_id)

@app.route('/vote/<poll_id>', methods=['POST'])
def submit_vote(poll_id):
    data = request.get_json()
    option_id = data.get("option_id")
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("UPDATE options SET votes = votes + 1 WHERE id = %s", (option_id,))
    conn.commit()
    
    cursor.execute("SELECT votes FROM options WHERE id = %s", (option_id,))
    option = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if option:
        socketio.emit('vote_update', {
            "option_id": option_id,
            "votes": option["votes"]
        }, to=poll_id)
        return jsonify({"status": "success"})
        
    return jsonify({"status": "error"}), 404

@app.route('/present/<poll_id>')
def view_present(poll_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll: 
        cursor.close()
        conn.close()
        return "Poll not found", 404
        
    cursor.execute("SELECT * FROM questions WHERE poll_id = %s ORDER BY id ASC", (poll_id,))
    questions = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    q_list = [dict(q) for q in questions]
    
    return render_template_string(
        HTML_PRESENT, 
        poll=dict(poll), 
        base_url=request.url_root,
        questions_json=json.dumps(q_list)
    )

# ----------------- SOCKET.IO -----------------

@socketio.on('join')
def on_join(data):
    poll_id = data['poll_id']
    join_room(poll_id)
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT * FROM questions WHERE poll_id = %s AND is_active = 1", (poll_id,))
    active_q = cursor.fetchone()
    
    if active_q:
        cursor.execute("SELECT * FROM options WHERE question_id = %s ORDER BY id ASC", (active_q['id'],))
        opts = cursor.fetchall()
        emit('question_activated', {
            'question_id': active_q['id'],
            'question': active_q['text'],
            'options': [dict(o) for o in opts]
        })
        
    cursor.close()
    conn.close()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    # When running locally, this will spin up the gevent WSGI server natively.
    socketio.run(app, host="0.0.0.0", port=port, debug=True)
