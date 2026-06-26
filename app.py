#!/usr/bin/env python3
"""AutoBaomiGuan Web 包装 - Flask 后端 + 前端页面"""

import io
import json
import logging
import os
import sys
import time
import threading
from io import BytesIO
from collections import deque

import qrcode as qrcode_lib
from flask import Flask, jsonify, render_template_string, request, send_file

import login
from course import CourseManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

CREDENTIALS_FILE = os.environ.get('CREDENTIALS_FILE', 'credentials.json')
CURRENT_COURSE_PACKET_ID = '312bc914-8e11-421b-b9bc-e900fe1a4e50'

session = login.session
_course_manager = None
_task_status = {'running': False, 'message': '', 'type': ''}

# ── 日志收集器 ─────────────────────────────────────────────

_log_lines = deque(maxlen=500)
_log_lock = threading.Lock()
_log_index = 0

import re
_noise_patterns = [
    re.compile(r'\d+\.\d+\.\d+\.\d+ - - \['),                        # HTTP access log (all variants)
    re.compile(r'Serving Flask app|Debug mode:'),
    re.compile(r'development server'),
    re.compile(r'Running on '),
    re.compile(r'Press CTRL\+C'),
]


def _is_noise(line):
    return any(p.search(line) for p in _noise_patterns)


class LogCollector(io.StringIO):
    """拦截所有 print/stdout 输出并存入日志缓冲区"""

    def write(self, s):
        if s and s.strip():
            stripped = s.rstrip()
            if not _is_noise(stripped):
                _append_log(stripped)
        return super().write(s)

    def flush(self):
        pass


class LogHandler(logging.Handler):
    """拦截 logging 输出并存入日志缓冲区"""

    def emit(self, record):
        msg = self.format(record)
        for code in ['\x1b[31m', '\x1b[32m', '\x1b[33m', '\x1b[34m', '\x1b[35m',
                      '\x1b[36m', '\x1b[0m', '\x1b[1m']:
            msg = msg.replace(code, '')
        if not _is_noise(msg):
            _append_log(msg)


def _append_log(line):
    global _log_index
    with _log_lock:
        _log_lines.append({'i': _log_index, 't': line})
        _log_index += 1


# 安装日志收集
_log_collector = LogCollector()
_log_handler = LogHandler()
_log_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
logging.getLogger().addHandler(_log_handler)
sys.stdout = _log_collector


def check_token(token):
    """验证 token 是否有效"""
    if not token:
        return None
    headers = login.build_headers(token)
    url = 'https://www.baomi.org.cn/portal/main-api/checkToken.do'
    try:
        response = session.get(url, headers=headers).json()
        if response.get('result'):
            nickname = response['data'].get('nickName')
            return nickname or '用户'
    except Exception as e:
        logging.error(f'检查token失败: {e}')
    return None


def get_course_manager():
    global _course_manager
    token = load_token()
    if token and check_token(token):
        _course_manager = CourseManager(session, token)
        return _course_manager
    return None


def load_token():
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            data = json.load(f)
            return data.get('token')
    except Exception:
        return None


def save_credentials(login_name, password, token):
    data = {
        'loginName': login_name,
        'passWord': password,
        'token': token,
        'timestamp': int(time.time())
    }
    with open(CREDENTIALS_FILE, 'w') as f:
        json.dump(data, f)


# ── API 路由 ──────────────────────────────────────────────

@app.route('/api/check')
def api_check():
    token = load_token()
    if token:
        nickname = check_token(token)
        if nickname:
            return jsonify({'logged_in': True, 'nickname': nickname})
    return jsonify({'logged_in': False})


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    login_name = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not login_name or not password:
        return jsonify({'success': False, 'error': '请输入用户名和密码'}), 400
    try:
        token = login.login(login_name, password)
        save_credentials(login_name, password, token)
        nickname = check_token(token) or login_name
        return jsonify({'success': True, 'nickname': nickname})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/qr')
def api_qr():
    try:
        qr_content, qr_token = get_qr_inline()
        img = qrcode_lib.make(qr_content)
        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/qr/check', methods=['POST'])
def api_qr_check():
    data = request.get_json()
    qr_token = data.get('qr_token', '')
    if not qr_token:
        return jsonify({'status': 'error', 'error': '缺少 qr_token'})
    try:
        status = login.check_qr_login(qr_token)
        if status == 1:
            save_credentials('扫码登录用户', '', qr_token)
            return jsonify({'status': 'confirmed', 'nickname': '扫码登录用户'})
        elif status == -1:
            return jsonify({'status': 'expired'})
        else:
            return jsonify({'status': 'waiting'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


_qr_cache = {}


def get_qr_inline():
    global _qr_cache
    qr_content, qr_token_parsed = login.get_qr_code()
    _qr_cache['token'] = qr_token_parsed
    _qr_cache['content'] = qr_content
    _qr_cache['time'] = time.time()
    return qr_content, qr_token_parsed


@app.route('/api/qr/token')
def api_qr_token():
    if not _qr_cache.get('token'):
        return jsonify({'error': '请先获取二维码'}), 400
    return jsonify({'qr_token': _qr_cache['token']})


@app.route('/api/status')
def api_status():
    global _task_status
    return jsonify(_task_status)


@app.route('/api/logs')
def api_logs():
    since = request.args.get('since', 0, type=int)
    with _log_lock:
        lines = [x for x in _log_lines if x['i'] >= since]
    return jsonify({'logs': lines, 'latest_index': _log_index})


@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    global _log_index
    with _log_lock:
        _log_lines.clear()
        _log_index = 0
    return jsonify({'success': True})


def _run_task(task_type, fn, *args):
    global _task_status
    _task_status = {'running': True, 'message': f'正在执行{task_type}...', 'type': task_type}
    try:
        result = fn(*args)
        if result:
            _task_status = {'running': False, 'message': f'{task_type}完成!', 'type': task_type, 'success': True}
        else:
            _task_status = {'running': False, 'message': f'{task_type}失败', 'type': task_type, 'success': False}
    except Exception as e:
        _task_status = {'running': False, 'message': str(e), 'type': task_type, 'success': False}


@app.route('/api/study', methods=['POST'])
def api_study():
    global _task_status
    if _task_status.get('running'):
        return jsonify({'success': False, 'error': '已有任务在运行'}), 409
    mgr = get_course_manager()
    if not mgr:
        return jsonify({'success': False, 'error': '未登录'}), 401
    threading.Thread(target=_run_task, args=('课程学习', mgr.study_course, CURRENT_COURSE_PACKET_ID), daemon=True).start()
    return jsonify({'success': True, 'message': '开始学习课程'})


@app.route('/api/exam', methods=['POST'])
def api_exam():
    global _task_status
    if _task_status.get('running'):
        return jsonify({'success': False, 'error': '已有任务在运行'}), 409
    mgr = get_course_manager()
    if not mgr:
        return jsonify({'success': False, 'error': '未登录'}), 401
    threading.Thread(target=_run_task, args=('考试', mgr.complete_exam, CURRENT_COURSE_PACKET_ID), daemon=True).start()
    return jsonify({'success': True, 'message': '开始考试'})


@app.route('/api/all', methods=['POST'])
def api_all():
    global _task_status
    if _task_status.get('running'):
        return jsonify({'success': False, 'error': '已有任务在运行'}), 409
    mgr = get_course_manager()
    if not mgr:
        return jsonify({'success': False, 'error': '未登录'}), 401

    def do_all():
        global _task_status
        _task_status = {'running': True, 'message': '正在学习课程...', 'type': '一键完成'}
        try:
            study_ok = mgr.study_course(CURRENT_COURSE_PACKET_ID)
            if study_ok:
                _task_status = {'running': True, 'message': '课程学习完成，正在考试...', 'type': '一键完成'}
                exam_ok = mgr.complete_exam(CURRENT_COURSE_PACKET_ID)
                if exam_ok:
                    _task_status = {'running': False, 'message': '学习与考试全部完成!', 'type': '一键完成', 'success': True}
                else:
                    _task_status = {'running': False, 'message': '学习完成但考试失败', 'type': '一键完成', 'success': False}
            else:
                _task_status = {'running': False, 'message': '课程学习失败', 'type': '一键完成', 'success': False}
        except Exception as e:
            _task_status = {'running': False, 'message': str(e), 'type': '一键完成', 'success': False}

    threading.Thread(target=do_all, daemon=True).start()
    return jsonify({'success': True, 'message': '开始一键完成'})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    global _course_manager
    _course_manager = None
    if os.path.exists(CREDENTIALS_FILE):
        os.remove(CREDENTIALS_FILE)
    return jsonify({'success': True})


@app.route('/api/diag')
def api_diag():
    """网络诊断：测试与保密观 API 的连通性"""
    import requests as req
    results = {}
    s = req.Session()

    tests = [
        ('getPublishKey', 'GET', 'https://www.baomi.org.cn/portal/main-api/getPublishKey.do', {}),
        ('getQrToken', 'POST', 'https://www.baomi.org.cn/portal/main-api/v2/spc/getQrToken.do', {}),
        ('loginInNew', 'POST', 'https://www.baomi.org.cn/portal/main-api/loginInNew.do',
         {'loginName': 'x', 'passWord': 'x', 'deviceId': 1711, 'deviceOs': 'pc', 'lon': 40, 'lat': 30, 'siteId': '95', 'sinopec': 'false'}),
    ]

    for name, method, url, body in tests:
        try:
            h = login.build_headers()
            if method == 'GET':
                r = s.get(url, headers=h, timeout=15, allow_redirects=False)
            else:
                r = s.post(url, json=body, headers=h, timeout=15, allow_redirects=False)
            results[name] = {
                'status': r.status_code,
                'redirect': r.headers.get('Location', '') if r.status_code in (301, 302, 307, 308) else '',
                'body_preview': r.text[:150],
            }
        except Exception as e:
            results[name] = {'status': 'ERROR', 'error': str(e)}

    return jsonify(results)


# ── 前端页面 ──────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>保密观 自动刷课</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex;
       justify-content: center; align-items: center; padding: 20px; }
.container { max-width: 520px; width: 100%; }
.card { background: #1e293b; border-radius: 16px; padding: 32px; border: 1px solid #334155; }
h1 { font-size: 24px; text-align: center; margin-bottom: 8px; color: #f8fafc; }
.subtitle { text-align: center; color: #94a3b8; font-size: 14px; margin-bottom: 24px; }
h2 { font-size: 18px; margin-bottom: 16px; color: #f1f5f9; }
.tabs { display: flex; gap: 8px; margin-bottom: 20px; }
.tab { flex: 1; padding: 10px; text-align: center; border-radius: 8px; cursor: pointer;
       background: #334155; color: #94a3b8; border: none; font-size: 14px; transition: .2s; }
.tab.active { background: #3b82f6; color: #fff; }
.form { display: flex; flex-direction: column; gap: 12px; }
input { padding: 12px; border-radius: 8px; border: 1px solid #475569; background: #0f172a;
        color: #e2e8f0; font-size: 14px; outline: none; }
input:focus { border-color: #3b82f6; }
.btn { padding: 12px; border-radius: 8px; border: none; cursor: pointer; font-size: 15px;
       font-weight: 600; transition: .2s; }
.btn-primary { background: #3b82f6; color: #fff; }
.btn-primary:hover { background: #2563eb; }
.btn-danger { background: #ef4444; color: #fff; }
.btn-danger:hover { background: #dc2626; }
.btn-success { background: #22c55e; color: #fff; }
.btn-success:hover { background: #16a34a; }
.btn-warning { background: #f59e0b; color: #000; }
.btn-warning:hover { background: #d97706; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.qr-box { text-align: center; padding: 20px; }
.qr-box img { max-width: 240px; border-radius: 8px; }
.qr-hint { margin-top: 12px; color: #94a3b8; font-size: 13px; }
.error { color: #f87171; font-size: 13px; text-align: center; }
.success { color: #4ade80; font-size: 13px; text-align: center; }
.hidden { display: none; }
.status-bar { margin-top: 16px; padding: 12px; border-radius: 8px; background: #334155;
              font-size: 13px; text-align: center; }
.status-bar.running { background: #1e3a5f; color: #93c5fd; }
.status-bar.success { background: #14532d; color: #86efac; }
.status-bar.fail { background: #7f1d1d; color: #fca5a5; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
.user-info { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.user-info span { font-weight: 600; }
.refresh { font-size: 12px; color: #94a3b8; text-align: center; margin-top: 8px; }
.log-window { margin-top: 16px; background: #0f172a; border: 1px solid #334155;
              border-radius: 8px; padding: 12px; max-height: 260px; overflow-y: auto; }
.log-window .log-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.log-window .log-header span { font-size: 13px; color: #94a3b8; }
.log-window .log-header button { background: none; border: 1px solid #475569; color: #94a3b8;
                                  padding: 2px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
.log-line { font-size: 12px; font-family: "SF Mono", "Fira Code", monospace; padding: 2px 0;
            border-bottom: 1px solid #1e293b; line-height: 1.5; word-break: break-all; }
.log-line .time { color: #64748b; margin-right: 6px; }
.log-line .text { color: #cbd5e1; }
.log-line.warn { color: #fbbf24; }
.log-line.error { color: #f87171; }
.log-line.success { color: #4ade80; }
.log-window::-webkit-scrollbar { width: 6px; }
.log-window::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
</style>
</head>
<body>
<div class="container">

<div id="login-panel" class="card">
  <h1>保密观 自动刷课</h1>
  <p class="subtitle">2026 年度全国保密教育线上培训</p>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('qr')">扫码登录</button>
    <button class="tab" onclick="switchTab('password')">密码登录</button>
  </div>

  <div id="qr-login" class="form">
    <div class="qr-box">
      <img id="qr-img" src="/api/qr" alt="QR Code" onerror="this.style.display='none'; document.getElementById('qr-error').classList.remove('hidden')">
    </div>
    <div id="qr-error" class="error hidden">二维码加载失败，请刷新</div>
    <p class="qr-hint">请使用保密观 APP 扫描二维码</p>
    <p id="qr-status" class="success hidden"></p>
    <button class="btn btn-primary" onclick="refreshQR()">刷新二维码</button>
  </div>

  <div id="pw-login" class="form hidden">
    <input type="text" id="username" placeholder="用户名">
    <input type="password" id="password" placeholder="密码">
    <p id="login-error" class="error hidden"></p>
    <button class="btn btn-primary" onclick="passwordLogin()">登录</button>
  </div>
</div>

<div id="dashboard-panel" class="card hidden">
  <h1>保密观 自动刷课</h1>
  <div class="user-info">
    <span id="nickname-display">已登录</span>
    <div>
      <button class="btn" style="padding:6px 14px;font-size:13px;background:#475569;color:#e2e8f0;margin-right:6px" onclick="runDiag()">诊断</button>
      <button class="btn btn-danger" style="padding:6px 14px;font-size:13px" onclick="logout()">退出</button>
    </div>
  </div>

  <div class="grid">
    <button class="btn btn-warning" onclick="doAll()" id="btn-all">一键完成</button>
    <button class="btn btn-primary" onclick="doStudy()" id="btn-study">学习课程</button>
    <button class="btn btn-success" onclick="doExam()" id="btn-exam">开始考试</button>
    <button class="btn btn-danger" onclick="logout()" style="visibility:hidden"></button>
  </div>

  <div id="task-status" class="status-bar hidden"></div>
  <div id="log-window" class="log-window hidden">
    <div class="log-header">
      <span>任务日志</span>
      <button onclick="clearLogs()">清空</button>
    </div>
    <div id="log-lines"></div>
  </div>
</div>

</div>
<script>
let qrToken = '';
let qrPollTimer = null;
let taskPollTimer = null;
let logPollTimer = null;
let logSince = 0;

async function fetchQRToken() {
  try {
    const resp = await fetch('/api/qr/token');
    const data = await resp.json();
    if (data.qr_token) qrToken = data.qr_token;
  } catch(e) {}
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('qr-login').classList.toggle('hidden', tab !== 'qr');
  document.getElementById('pw-login').classList.toggle('hidden', tab !== 'password');
  if (tab === 'qr') { refreshQR(); } else { stopQRPoll(); }
}

function refreshQR() {
  document.getElementById('qr-img').src = '/api/qr?_=' + Date.now();
  document.getElementById('qr-status').classList.add('hidden');
  qrToken = '';
  stopQRPoll();
  fetchQRToken();
  setTimeout(() => { if (qrToken) startQRPoll(); }, 1500);
}

function startQRPoll() {
  stopQRPoll();
  if (!qrToken) return;
  qrPollTimer = setInterval(async () => {
    try {
      const resp = await fetch('/api/qr/check', { method:'POST',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({qr_token:qrToken}) });
      const data = await resp.json();
      if (data.status === 'confirmed') {
        stopQRPoll();
        document.getElementById('qr-status').textContent = '扫码成功!';
        document.getElementById('qr-status').classList.remove('hidden');
        setTimeout(() => showDashboard(data.nickname), 800);
      } else if (data.status === 'expired') {
        stopQRPoll();
        document.getElementById('qr-status').textContent = '二维码已过期，请刷新';
        document.getElementById('qr-status').classList.remove('hidden');
      }
    } catch(e) {}
  }, 2000);
}

function stopQRPoll() { if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; } }

async function passwordLogin() {
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value.trim();
  const errEl = document.getElementById('login-error');
  if (!username || !password) { errEl.textContent = '请输入用户名和密码'; errEl.classList.remove('hidden'); return; }
  try {
    const resp = await fetch('/api/login', { method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({username, password}) });
    const data = await resp.json();
    if (data.success) { showDashboard(data.nickname); }
    else { errEl.textContent = data.error || '登录失败'; errEl.classList.remove('hidden'); }
  } catch(e) { errEl.textContent = '网络错误'; errEl.classList.remove('hidden'); }
}

async function showDashboard(nickname) {
  document.getElementById('login-panel').classList.add('hidden');
  document.getElementById('dashboard-panel').classList.remove('hidden');
  document.getElementById('nickname-display').textContent = nickname || '已登录';
}

async function checkLogin() {
  try {
    const resp = await fetch('/api/check');
    const data = await resp.json();
    if (data.logged_in) showDashboard(data.nickname);
  } catch(e) {}
}

function setButtons(disabled) {
  ['btn-all','btn-study','btn-exam'].forEach(id => {
    document.getElementById(id).disabled = disabled;
  });
}

function showStatus(msg, cls) {
  const el = document.getElementById('task-status');
  el.textContent = msg;
  el.className = 'status-bar ' + cls;
  el.classList.remove('hidden');
}

function startLogPoll() {
  stopLogPoll();
  document.getElementById('log-window').classList.remove('hidden');
  logSince = 0;
  logPollTimer = setInterval(pollLogs, 1500);
}

function stopLogPoll() { if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; } }

async function pollLogs() {
  try {
    const resp = await fetch('/api/logs?since=' + logSince);
    const data = await resp.json();
    if (data.logs && data.logs.length > 0) {
      appendLogLines(data.logs);
      logSince = data.latest_index;
    }
  } catch(e) {}
}

function appendLogLines(lines) {
  const container = document.getElementById('log-lines');
  lines.forEach(l => {
    const div = document.createElement('div');
    div.className = 'log-line';
    let text = l.t;
    let cls = '';
    if (/失败|错误|ERROR|FAIL/i.test(text)) cls = 'error';
    else if (/完成|成功|通过|SUCCESS/i.test(text)) cls = 'success';
    else if (/WARNING|警告|跳过/i.test(text)) cls = 'warn';
    div.innerHTML = '<span class="time">' + new Date().toLocaleTimeString() + '</span>' +
                    '<span class="text ' + cls + '">' + text + '</span>';
    container.appendChild(div);
  });
  const win = document.getElementById('log-window');
  win.scrollTop = win.scrollHeight;
}

async function clearLogs() {
  await fetch('/api/logs/clear', { method:'POST' });
  document.getElementById('log-lines').innerHTML = '';
  logSince = 0;
}

function pollTaskStatus() {
  stopTaskPoll();
  taskPollTimer = setInterval(async () => {
    try {
      const resp = await fetch('/api/status');
      const data = await resp.json();
      showStatus(data.message, data.running ? 'running' : (data.success ? 'success' : 'fail'));
      if (!data.running) { setButtons(false); stopTaskPoll(); }
    } catch(e) {}
  }, 2000);
}

function stopTaskPoll() { if (taskPollTimer) { clearInterval(taskPollTimer); taskPollTimer = null; } }

async function doStudy() {
  setButtons(true);
  showStatus('正在学习课程...', 'running');
  startLogPoll();
  const resp = await fetch('/api/study', { method:'POST' });
  const data = await resp.json();
  if (!data.success) { showStatus(data.error, 'fail'); setButtons(false); }
  else pollTaskStatus();
}

async function doExam() {
  setButtons(true);
  showStatus('正在考试...', 'running');
  startLogPoll();
  const resp = await fetch('/api/exam', { method:'POST' });
  const data = await resp.json();
  if (!data.success) { showStatus(data.error, 'fail'); setButtons(false); }
  else pollTaskStatus();
}

async function doAll() {
  setButtons(true);
  showStatus('正在一键完成...', 'running');
  startLogPoll();
  const resp = await fetch('/api/all', { method:'POST' });
  const data = await resp.json();
  if (!data.success) { showStatus(data.error, 'fail'); setButtons(false); }
  else pollTaskStatus();
}

async function logout() {
  stopQRPoll();
  stopTaskPoll();
  stopLogPoll();
  await fetch('/api/logout', { method:'POST' });
  document.getElementById('login-panel').classList.remove('hidden');
  document.getElementById('dashboard-panel').classList.add('hidden');
  document.getElementById('password').value = '';
  document.getElementById('log-window').classList.add('hidden');
}

async function runDiag() {
  document.getElementById('log-window').classList.remove('hidden');
  document.getElementById('log-lines').innerHTML = '';
  logSince = 0;
  appendLogLines([{t: '正在诊断网络连通性...'}]);
  try {
    const resp = await fetch('/api/diag');
    const data = await resp.json();
    for (const [name, r] of Object.entries(data)) {
      if (r.status === 'ERROR') {
        appendLogLines([{t: name + ': 连接失败 - ' + r.error}]);
      } else if (r.redirect) {
        appendLogLines([{t: name + ': ' + r.status + ' 重定向 -> ' + r.redirect, cls: 'error'}]);
      } else {
        appendLogLines([{t: name + ': ' + r.status + ' ' + (r.body_preview || '').substring(0, 60)}]);
      }
    }
  } catch(e) {
    appendLogLines([{t: '诊断失败: ' + e.message}]);
  }
}

checkLogin();
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(INDEX_HTML)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8765, debug=False, threaded=True)
