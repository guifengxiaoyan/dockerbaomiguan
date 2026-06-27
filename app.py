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
    re.compile(r'\d+\.\d+\.\d+\.\d+ - - \['),
    re.compile(r'Serving Flask app|Debug mode:'),
    re.compile(r'development server'),
    re.compile(r'Running on '),
    re.compile(r'Press CTRL\+C'),
    re.compile(r'^>>> (GET|POST|PUT|DELETE) '),
    re.compile(r'^>>> Headers:'),
    re.compile(r'^>>> Body keys:'),
    re.compile(r'^<<< Status:'),
    re.compile(r'^<<< Body preview:'),
    re.compile(r'^\[' + '\x1b' + r'\[\d+m(启动|预热)'),
    re.compile(r'^' + '\x1b' + r'\[\d+m'),
]


def _is_noise(line):
    return any(p.search(line) for p in _noise_patterns)


class LogCollector(io.StringIO):
    """拦截所有 print/stdout 输出并存入日志缓冲区"""

    def write(self, s):
        if s and s.strip():
            stripped = s.rstrip()
            for code in ['\x1b[31m', '\x1b[32m', '\x1b[33m', '\x1b[34m', '\x1b[35m',
                          '\x1b[36m', '\x1b[0m', '\x1b[1m', '\x1b[30m']:
                stripped = stripped.replace(code, '')
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
    import socket
    results = {}

    # DNS 解析测试
    try:
        ip = socket.gethostbyname('www.baomi.org.cn')
        results['dns'] = {'status': 'OK', 'ip': ip}
    except Exception as e:
        results['dns'] = {'status': 'ERROR', 'error': str(e)}

    # SSL 测试
    try:
        r = req.get('https://www.baomi.org.cn', timeout=10)
        results['ssl'] = {'status': r.status_code}
    except req.exceptions.SSLError as e:
        results['ssl'] = {'status': 'SSL_ERROR', 'error': str(e)[:100]}
    except Exception as e:
        results['ssl'] = {'status': 'ERROR', 'error': str(e)[:100]}

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
            results[name] = {'status': 'ERROR', 'error': str(e)[:150]}

    return jsonify(results)


# ── 前端页面 ──────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>保密观 自动刷课</title>
<style>
:root {
  --bg: #060b18; --surface: #0c1428; --surface2: #111d3a;
  --border: #1a2a4a; --border-glow: #1e3a6e;
  --cyan: #00e5ff; --cyan-dim: #008899; --cyan-glow: rgba(0,229,255,.18);
  --green: #00ff88; --green-dim: #008844; --green-glow: rgba(0,255,136,.18);
  --purple: #a855f7; --purple-dim: #6b21a8; --purple-glow: rgba(168,85,247,.18);
  --red: #ff4472; --amber: #ffb300;
  --text: #c0ccdd; --text2: #6b7d99; --text-dim: #4a5568;
  --radius: 12px; --radius-sm: 8px;
  --font: "Inter", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", "SF Mono", "Fira Code", "Cascadia Code", monospace;
}

* { margin:0; padding:0; box-sizing:border-box; }

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex; justify-content: center; align-items: flex-start;
  padding: 48px 16px 80px;
  overflow-x: hidden;
}

/* ── Animated grid background ── */
body::before {
  content: '';
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background-image:
    linear-gradient(rgba(0,229,255,.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,229,255,.03) 1px, transparent 1px);
  background-size: 60px 60px;
  animation: gridMove 20s linear infinite;
}
@keyframes gridMove {
  0% { background-position: 0 0, 0 0; }
  100% { background-position: 0 60px, 60px 0; }
}

/* ── Radial vignette ── */
body::after {
  content: '';
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background: radial-gradient(ellipse at 50% 0%, rgba(0,229,255,.04) 0%, transparent 55%),
              radial-gradient(ellipse at 85% 20%, rgba(168,85,247,.03) 0%, transparent 40%),
              radial-gradient(ellipse at 15% 80%, rgba(0,255,136,.02) 0%, transparent 35%);
}

.container {
  position: relative; z-index: 1;
  width: 100%; max-width: 500px;
  animation: fadeUp .5s cubic-bezier(.16,1,.3,1);
}
@keyframes fadeUp {
  from { opacity:0; transform: translateY(24px); }
  to { opacity:1; transform: translateY(0); }
}

/* ── Card with glass effect ── */
.card {
  background: linear-gradient(135deg, rgba(12,20,40,.92), rgba(8,14,30,.96));
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 40px 36px 36px;
  position: relative; overflow: hidden;
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  box-shadow: 0 0 0 1px rgba(0,229,255,.06),
              0 8px 32px rgba(0,0,0,.5),
              0 0 80px rgba(0,229,255,.04);
}

/* ── Card top accent line ── */
.card::before {
  content: '';
  position: absolute; top: 0; left: 20px; right: 20px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--cyan), var(--purple), transparent);
  opacity: .5;
}

/* ── Header ── */
.header-icon {
  width: 56px; height: 56px; margin: 0 auto 18px; position: relative;
  display: flex; align-items: center; justify-content: center;
}
.header-icon::before {
  content: '';
  position: absolute; inset: 0; border-radius: 16px;
  background: linear-gradient(135deg, var(--cyan), var(--purple));
  opacity: .15;
}
.header-icon::after {
  content: '';
  position: absolute; inset: 0; border-radius: 16px;
  border: 1px solid transparent;
  background: linear-gradient(135deg, var(--cyan), var(--purple)) border-box;
  -webkit-mask: linear-gradient(#fff 0 0) padding-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  animation: iconGlow 3s ease-in-out infinite;
}
@keyframes iconGlow {
  0%,100% { opacity: .4; }
  50% { opacity: .9; }
}
.header-icon .ico-text {
  font-size: 26px; z-index: 1;
  background: linear-gradient(135deg, var(--cyan), var(--purple));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  font-weight: 700;
}

h1 { font-size: 22px; text-align: center; color: #e8edf5; font-weight: 700; letter-spacing: 1px; }
.subtitle { text-align: center; color: var(--text2); font-size: 13px; margin: 6px 0 30px; letter-spacing: .5px; }

/* ── Tabs ── */
.tabs {
  display: flex; gap: 4px; margin-bottom: 28px;
  background: var(--surface); border-radius: 10px; padding: 4px;
  border: 1px solid var(--border);
}
.tab {
  flex: 1; padding: 10px 0; text-align: center; border-radius: 8px; cursor: pointer;
  background: transparent; color: var(--text2); border: none; font-size: 14px;
  font-weight: 500; transition: all .25s; position: relative; font-family: var(--font);
}
.tab.active {
  background: linear-gradient(135deg, rgba(0,229,255,.15), rgba(168,85,247,.1));
  color: #fff;
  box-shadow: 0 0 12px var(--cyan-glow), inset 0 1px 0 rgba(255,255,255,.06);
}
.tab:hover:not(.active) { color: #d0d8e5; }

/* ── Form inputs ── */
.form { display: flex; flex-direction: column; gap: 16px; }
.input-group { position: relative; }
.input-group .icon {
  position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
  color: var(--text2); font-size: 15px; transition: color .2s;
  pointer-events: none; z-index: 1;
}
input {
  width: 100%; padding: 13px 14px 13px 42px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); background: rgba(6,11,24,.6); color: #dde4f0;
  font-size: 14px; outline: none; transition: all .25s; font-family: var(--font);
}
input:focus {
  border-color: var(--cyan);
  box-shadow: 0 0 0 3px var(--cyan-glow), 0 0 20px rgba(0,229,255,.06);
}
input:focus + .icon, .input-group:focus-within .icon { color: var(--cyan); }
input::placeholder { color: var(--text-dim); }

/* ── Buttons ── */
.btn {
  padding: 12px 20px; border-radius: var(--radius-sm); border: none; cursor: pointer;
  font-size: 14px; font-weight: 600; transition: all .25s; display: inline-flex;
  align-items: center; justify-content: center; gap: 8px;
  font-family: var(--font); position: relative; overflow: hidden;
  letter-spacing: .3px;
}
.btn::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(255,255,255,.08) 0%, transparent 50%);
  pointer-events: none;
}
.btn:active { transform: scale(.96); }
.btn:disabled { opacity: .4; cursor: not-allowed; transform: none; filter: grayscale(.3); }

.btn-primary {
  background: linear-gradient(135deg, #0077cc, #0055aa);
  color: #fff; box-shadow: 0 4px 14px rgba(0,119,204,.3);
  border: 1px solid rgba(0,229,255,.2);
}
.btn-primary:hover:not(:disabled) {
  background: linear-gradient(135deg, #0088dd, #0066bb);
  box-shadow: 0 4px 20px rgba(0,119,204,.45), 0 0 30px rgba(0,229,255,.08);
}

.btn-danger {
  background: linear-gradient(135deg, #cc2255, #991144);
  color: #fff; border: 1px solid rgba(255,68,114,.25);
  box-shadow: 0 4px 14px rgba(255,68,114,.2);
}
.btn-danger:hover:not(:disabled) {
  box-shadow: 0 4px 20px rgba(255,68,114,.35), 0 0 30px rgba(255,68,114,.08);
}

.btn-success {
  background: linear-gradient(135deg, #008844, #006633);
  color: #fff; border: 1px solid rgba(0,255,136,.25);
  box-shadow: 0 4px 14px rgba(0,255,136,.15);
}
.btn-success:hover:not(:disabled) {
  box-shadow: 0 4px 20px rgba(0,255,136,.25), 0 0 30px rgba(0,255,136,.06);
}

.btn-amber {
  background: linear-gradient(135deg, #ffa000, #cc7700);
  color: #111; box-shadow: 0 4px 14px rgba(255,179,0,.25);
  border: 1px solid rgba(255,179,0,.3);
}
.btn-amber:hover:not(:disabled) {
  box-shadow: 0 4px 20px rgba(255,179,0,.4), 0 0 30px rgba(255,179,0,.1);
}

.btn-ghost {
  background: rgba(255,255,255,.03); border: 1px solid var(--border);
  color: var(--text2); backdrop-filter: blur(10px);
}
.btn-ghost:hover { background: rgba(255,255,255,.06); border-color: var(--border-glow); color: #d0d8e5; }

.btn-sm { padding: 6px 14px; font-size: 12px; border-radius: 7px; letter-spacing: 0; }

/* ── QR Code ── */
.qr-box { text-align: center; padding: 8px 0 4px; }
.qr-box img {
  width: 200px; height: 200px; border-radius: 12px;
  border: 1px solid var(--border-glow);
  background: #fff; padding: 10px;
  box-shadow: 0 0 30px rgba(0,229,255,.08);
}
.qr-hint {
  margin-top: 12px; color: var(--text2); font-size: 13px;
  display: flex; align-items: center; justify-content: center; gap: 8px;
}
.qr-hint .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--green); box-shadow: 0 0 8px rgba(0,255,136,.5);
  animation: pulse 1.8s ease-in-out infinite;
}
@keyframes pulse { 0%,100% { opacity:1; transform:scale(1); } 50% { opacity:.3; transform:scale(.8); } }

.msg { font-size: 13px; text-align: center; padding: 6px 0; }
.msg.error { color: var(--red); }
.msg.success { color: #66ffaa; }
.hidden { display: none !important; }

/* ── Dashboard ── */
.user-bar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 24px; padding: 10px 14px;
  background: var(--surface); border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.user-bar .uname { font-weight: 600; color: #d0d8e5; font-size: 14px; }
.user-bar .btns { display: flex; gap: 6px; }

.action-grid {
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px;
  margin-bottom: 16px;
}
.action-grid .btn { font-size: 14px; padding: 14px 8px; }

/* ── Status bar ── */
.status-bar {
  margin-top: 16px; padding: 12px 16px; border-radius: var(--radius-sm);
  font-size: 13px; font-weight: 500; text-align: center;
  display: flex; align-items: center; justify-content: center; gap: 10px;
  backdrop-filter: blur(8px);
}
.status-bar.running {
  background: rgba(0,229,255,.08); color: #80eaff;
  border: 1px solid rgba(0,229,255,.15);
  box-shadow: 0 0 20px rgba(0,229,255,.04);
}
.status-bar.success {
  background: rgba(0,255,136,.08); color: #66ffaa;
  border: 1px solid rgba(0,255,136,.15);
}
.status-bar.fail {
  background: rgba(255,68,114,.08); color: #ff88a0;
  border: 1px solid rgba(255,68,114,.15);
}

.spinner {
  width: 16px; height: 16px;
  border: 2px solid currentColor; border-top-color: transparent;
  border-radius: 50%; animation: spin .7s linear infinite; display: none;
  box-shadow: 0 0 8px currentColor;
}
.status-bar.running .spinner { display: inline-block; }
@keyframes spin { to { transform: rotate(360deg) } }

/* ── Log window (terminal style) ── */
.log-window {
  margin-top: 16px; background: rgba(3,7,16,.9); border: 1px solid var(--border);
  border-radius: var(--radius-sm); overflow: hidden;
  box-shadow: 0 0 20px rgba(0,0,0,.4);
  position: relative;
}
.log-window .log-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 16px; background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.log-window .log-header span {
  font-size: 11px; color: var(--text2); font-weight: 600;
  text-transform: uppercase; letter-spacing: 1.5px;
  display: flex; align-items: center; gap: 8px;
}
.log-window .log-header span::before {
  content: ''; width: 8px; height: 8px; border-radius: 50%;
  background: var(--green); box-shadow: 0 0 6px rgba(0,255,136,.5);
  animation: pulse 2s ease-in-out infinite;
}
.log-window .log-body {
  max-height: 260px; overflow-y: auto; padding: 10px 0;
  font-family: var(--font-mono); font-size: 12px;
}
.log-line {
  padding: 4px 16px; line-height: 1.7;
  border-bottom: 1px solid rgba(255,255,255,.015);
  position: relative;
}
.log-line::before {
  content: '\25B8'; margin-right: 8px; font-size: 10px; opacity: .4;
}
.log-line.error { color: var(--red); }
.log-line.error::before { color: var(--red); opacity: .7; }
.log-line.success { color: #66ffaa; }
.log-line.success::before { color: var(--green); opacity: .7; }
.log-line.warn { color: var(--amber); }
.log-line.warn::before { color: var(--amber); opacity: .7; }
.log-line.info { color: var(--text2); }

/* Scan line effect on log body */
.log-window::after {
  content: ''; position: absolute; top: 42px; left: 0; right: 0; bottom: 0;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,229,255,.008) 2px, rgba(0,229,255,.008) 4px);
  pointer-events: none; z-index: 2;
}

.log-window .log-body::-webkit-scrollbar { width: 4px; }
.log-window .log-body::-webkit-scrollbar-track { background: transparent; }
.log-window .log-body::-webkit-scrollbar-thumb {
  background: var(--border-glow); border-radius: 2px;
}
.log-window .log-body::-webkit-scrollbar-thumb:hover { background: var(--cyan-dim); }

/* ── Footer ── */
.footer {
  text-align: center; margin-top: 24px; font-size: 11px;
  color: var(--text-dim); letter-spacing: .5px;
}
</style>
</head>
<body>
<div class="container">

<!-- Login Panel -->
<div id="login-panel" class="card">
  <div class="header-icon">
    <span class="ico-text">&#x25C9;</span>
  </div>
  <h1>保密观 自动刷课</h1>
  <p class="subtitle">2026 年度全国保密教育线上培训</p>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('qr')">扫码登录</button>
    <button class="tab" onclick="switchTab('password')">密码登录</button>
  </div>

  <div id="qr-login" class="form">
    <div class="qr-box">
      <img id="qr-img" src="/api/qr" alt="QR Code"
           onerror="this.style.display='none';var e=document.getElementById('qr-error');e.classList.remove('hidden')">
    </div>
    <div id="qr-error" class="msg error hidden">二维码加载失败</div>
    <p class="qr-hint"><span class="dot"></span>请使用保密观 APP 扫描二维码</p>
    <p id="qr-status" class="msg success hidden"></p>
    <button class="btn btn-ghost" onclick="refreshQR()" style="width:100%">&#x21BB; 刷新二维码</button>
  </div>

  <div id="pw-login" class="form hidden">
    <div class="input-group">
      <span class="icon">&#x263A;</span>
      <input type="text" id="username" placeholder="用户名" autocomplete="username">
    </div>
    <div class="input-group">
      <span class="icon">&#x26BF;</span>
      <input type="password" id="password" placeholder="密码" autocomplete="current-password">
    </div>
    <p id="login-error" class="msg error hidden"></p>
    <button class="btn btn-primary" onclick="passwordLogin()" style="width:100%">登 录</button>
  </div>
</div>

<!-- Dashboard Panel -->
<div id="dashboard-panel" class="card hidden">
  <div class="header-icon">
    <span class="ico-text">&#x2713;</span>
  </div>
  <h1>保密观 自动刷课</h1>
  <p class="subtitle" id="nickname-display">已登录</p>

  <div class="action-grid">
    <button class="btn btn-amber" onclick="doAll()" id="btn-all">&#x25B6; 一键完成</button>
    <button class="btn btn-primary" onclick="doStudy()" id="btn-study">&#x25B6; 学习课程</button>
    <button class="btn btn-success" onclick="doExam()" id="btn-exam">&#x25B6; 开始考试</button>
  </div>

  <div class="user-bar">
    <span class="uname" id="nickname-label" style="font-size:13px;color:var(--text2)"></span>
    <div class="btns">
      <button class="btn btn-ghost btn-sm" onclick="runDiag()">诊断</button>
      <button class="btn btn-ghost btn-sm" onclick="logout()">退出</button>
    </div>
  </div>

  <div id="task-status" class="status-bar hidden">
    <span class="spinner"></span><span class="status-text"></span>
  </div>
  <div id="log-window" class="log-window hidden">
    <div class="log-header">
      <span>任务日志</span>
      <button class="btn btn-ghost btn-sm" onclick="clearLogs()">清空</button>
    </div>
    <div class="log-body" id="log-lines"></div>
  </div>
</div>

<p class="footer">github.com/guifengxiaoyan/dockerbaomiguan</p>
</div>
<script>
let qrToken = '', qrPollTimer = null, taskPollTimer = null, logPollTimer = null, logSince = 0;

async function fetchQRToken() {
  try { const r = await fetch('/api/qr/token'); const d = await r.json(); if (d.qr_token) qrToken = d.qr_token; } catch(e) {}
}
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('qr-login').classList.toggle('hidden', tab !== 'qr');
  document.getElementById('pw-login').classList.toggle('hidden', tab !== 'password');
  tab === 'qr' ? refreshQR() : stopQRPoll();
}
function refreshQR() {
  document.getElementById('qr-img').src = '/api/qr?_=' + Date.now();
  document.getElementById('qr-status').classList.add('hidden');
  qrToken = ''; stopQRPoll(); fetchQRToken();
  setTimeout(() => { if (qrToken) startQRPoll(); }, 1500);
}
function startQRPoll() {
  stopQRPoll(); if (!qrToken) return;
  qrPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/qr/check', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({qr_token:qrToken}) });
      const d = await r.json();
      if (d.status === 'confirmed') { stopQRPoll(); showMsg('qr-status', '扫码成功!', 'success'); setTimeout(() => showDashboard(d.nickname), 600); }
      else if (d.status === 'expired') { stopQRPoll(); showMsg('qr-status', '二维码已过期', 'error'); }
    } catch(e) {}
  }, 2000);
}
function stopQRPoll() { if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; } }

function showMsg(id, text, cls) {
  const el = document.getElementById(id); el.textContent = text;
  el.className = 'msg ' + cls; el.classList.remove('hidden');
}

async function passwordLogin() {
  const u = document.getElementById('username').value.trim(), p = document.getElementById('password').value.trim();
  if (!u || !p) return showMsg('login-error', '请输入用户名和密码', 'error');
  try {
    const r = await fetch('/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u, password:p}) });
    const d = await r.json();
    d.success ? showDashboard(d.nickname) : showMsg('login-error', d.error || '登录失败', 'error');
  } catch(e) { showMsg('login-error', '网络错误', 'error'); }
}

function showDashboard(nickname) {
  document.getElementById('login-panel').classList.add('hidden');
  document.getElementById('dashboard-panel').classList.remove('hidden');
  document.getElementById('nickname-display').textContent = '欢迎, ' + (nickname || '用户');
  document.getElementById('nickname-label').textContent = nickname || '';
}

async function checkLogin() {
  try { const r = await fetch('/api/check'); const d = await r.json(); if (d.logged_in) showDashboard(d.nickname); } catch(e) {}
}

function setButtons(d) { ['btn-all','btn-study','btn-exam'].forEach(id => document.getElementById(id).disabled = d); }

function setStatus(msg, cls) {
  const el = document.getElementById('task-status');
  el.querySelector('.status-text').textContent = msg;
  el.className = 'status-bar ' + cls; el.classList.remove('hidden');
}

function startLogPoll() { stopLogPoll(); document.getElementById('log-window').classList.remove('hidden'); logSince = 0; logPollTimer = setInterval(pollLogs, 1500); }
function stopLogPoll() { if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; } }

async function pollLogs() {
  try {
    const r = await fetch('/api/logs?since=' + logSince); const d = await r.json();
    if (d.logs && d.logs.length > 0) { appendLogLines(d.logs); logSince = d.latest_index; }
  } catch(e) {}
}
function appendLogLines(lines) {
  const c = document.getElementById('log-lines');
  lines.forEach(l => {
    const div = document.createElement('div');
    let cls = 'info';
    if (/失败|错误|ERROR|FAIL/i.test(l.t)) cls = 'error';
    else if (/完成|成功|通过|SUCCESS/i.test(l.t)) cls = 'success';
    else if (/WARNING|警告|跳过/i.test(l.t)) cls = 'warn';
    div.className = 'log-line ' + cls;
    div.textContent = l.t;
    c.appendChild(div);
  });
  c.parentElement.scrollTop = c.parentElement.scrollHeight;
}
async function clearLogs() { await fetch('/api/logs/clear', { method:'POST' }); document.getElementById('log-lines').innerHTML = ''; logSince = 0; }

function pollTaskStatus() {
  stopTaskPoll();
  taskPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/status'); const d = await r.json();
      setStatus(d.message, d.running ? 'running' : (d.success ? 'success' : 'fail'));
      if (!d.running) { setButtons(false); stopTaskPoll(); }
    } catch(e) {}
  }, 2000);
}
function stopTaskPoll() { if (taskPollTimer) { clearInterval(taskPollTimer); taskPollTimer = null; } }

async function doStudy() {
  setButtons(true); setStatus('正在学习课程...', 'running'); startLogPoll();
  const r = await fetch('/api/study', { method:'POST' }); const d = await r.json();
  d.success ? pollTaskStatus() : (setStatus(d.error, 'fail'), setButtons(false));
}
async function doExam() {
  setButtons(true); setStatus('正在考试...', 'running'); startLogPoll();
  const r = await fetch('/api/exam', { method:'POST' }); const d = await r.json();
  d.success ? pollTaskStatus() : (setStatus(d.error, 'fail'), setButtons(false));
}
async function doAll() {
  setButtons(true); setStatus('正在一键完成...', 'running'); startLogPoll();
  const r = await fetch('/api/all', { method:'POST' }); const d = await r.json();
  d.success ? pollTaskStatus() : (setStatus(d.error, 'fail'), setButtons(false));
}
async function logout() {
  stopQRPoll(); stopTaskPoll(); stopLogPoll();
  await fetch('/api/logout', { method:'POST' });
  document.getElementById('login-panel').classList.remove('hidden');
  document.getElementById('dashboard-panel').classList.add('hidden');
  document.getElementById('password').value = '';
  document.getElementById('log-window').classList.add('hidden');
  document.getElementById('task-status').classList.add('hidden');
}
async function runDiag() {
  document.getElementById('log-window').classList.remove('hidden');
  document.getElementById('log-lines').innerHTML = ''; logSince = 0;
  appendLogLines([{t: '正在诊断网络连通性...'}]);
  try {
    const r = await fetch('/api/diag'); const d = await r.json();
    for (const [k, v] of Object.entries(d)) {
      const cls = v.status === 200 || v.status === 'OK' ? 'success' : 'error';
      appendLogLines([{t: k + ': ' + (v.status === 'ERROR' ? v.error : v.status + (v.body_preview ? ' ' + v.body_preview.substring(0, 40) : ''))}]);
    }
  } catch(e) { appendLogLines([{t: '诊断失败: ' + e.message}]); }
}
checkLogin();
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(INDEX_HTML)


if __name__ == '__main__':
    print('[启动] 正在检查网络连通性...')
    try:
        import requests as _r, socket
        ip = socket.gethostbyname('www.baomi.org.cn')
        print(f'[启动] DNS 解析 www.baomi.org.cn -> {ip}')
        _resp = _r.get('https://www.baomi.org.cn/portal/main-api/getPublishKey.do',
                       headers={'User-Agent': 'Mozilla/5.0', 'siteId': '95'}, timeout=10)
        print(f'[启动] API 连通性: {_resp.status_code}')
    except Exception as e:
        print(f'[启动] 网络检查失败: {e}')
        print('[启动] 继续运行，但 API 调用可能失败')

    print('[启动] 服务运行在 http://0.0.0.0:8765')
    app.run(host='0.0.0.0', port=8765, debug=False, threaded=True)
