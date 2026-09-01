import sys
import asyncio
import json
import time
import hmac
import hashlib
import base64
import os
import random
from collections import deque
from urllib.parse import urlencode
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad, unpad
from PySide6.QtGui import QIcon, QFont

import aiohttp
import websockets
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLineEdit, QPushButton, QTextEdit,
    QLabel, QCheckBox, QMessageBox
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtCore import QObject, QStandardPaths, QUrl, Signal
import qasync
from qasync import asyncSlot

API_BASE = "http://api.shangmeng.top/api"
KEY = b"20190301"
KEY_GRAB = b"20241111"
WS_URL = "ws://219.151.188.13:9126/websocket"
APP_NAME = "续赚抢单助手"
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", SOURCE_DIR)
LEGACY_CONFIG_PATH = os.path.join(SOURCE_DIR, "config.json")
MONITOR_ONLY = False
TARGET_CATALOG_NAME = "哈啰出行"
WS_RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)
WS_ERROR_LOG_INTERVAL = 60
GRAB_MODE = "hybrid"
OPEN_API_BASE = "http://open.xuzuan.cn/api"
OPEN_API_BASE_INTERVAL_MIN = 1.2
OPEN_API_BASE_INTERVAL_MAX = 1.8
OPEN_API_BURST_DELAYS = (0, 0.1, 0.25)
OPEN_API_MAX_REQUESTS_PER_MINUTE = 60
OPEN_API_RATE_BACKOFF_INITIAL = 10
OPEN_API_RATE_BACKOFF_MAX = 300
OPEN_API_ERROR_BACKOFF_INITIAL = 2
OPEN_API_ERROR_BACKOFF_MAX = 30

# 日志框最大行数，超出后自动清理旧日志
MAX_LOG_LINES = 2000
# grabbed 集合清理间隔（秒）
GRABBED_CLEANUP_INTERVAL = 3600
# grabbed 集合清理时保留的最大数量
GRABBED_MAX_SIZE = 5000

_LOG_TAG_MAP = {"info": "INFO", "success": "SUCCESS", "warn": "WARN", "error": "ERROR"}


class LogEmitter(QObject):
    appended = Signal(str)


_log_emitter = LogEmitter()


def resource_path(*parts):
    return os.path.join(RESOURCE_DIR, *parts)


def app_data_path(*parts):
    root = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    if not root:
        root = os.path.join(os.path.expanduser("~"), f".{APP_NAME}")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, *parts)


def log(msg, level="info"):
    tag = _LOG_TAG_MAP.get(level, "INFO")
    full = f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}"
    print(full)
    _log_emitter.appended.emit(full)


def load_config():
    config_path = app_data_path("config.json")
    if not os.path.exists(config_path) and os.path.exists(LEGACY_CONFIG_PATH):
        config_path = LEGACY_CONFIG_PATH
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    cfg = dict(cfg)
    for key in ("username", "password", "open_api_secret", "format_id"):
        cfg.pop(key, None)
    with open(app_data_path("config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


async def send_dingtalk(session, cfg, text, markdown):
    """使用外部传入的 session，避免每次通知新建连接"""
    webhook = cfg.get("dingtalk_webhook")
    if not webhook:
        return
    secret = cfg.get("dingtalk_secret", "")
    payload = {"msgtype": "markdown", "markdown": {"title": text, "text": markdown}}
    url = webhook
    if secret:
        timestamp = str(round(time.time() * 1000))
        sign = base64.b64encode(
            hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
        ).decode()
        url = f"{webhook}&timestamp={timestamp}&sign={sign}"
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log(f"钉钉通知失败: HTTP {resp.status}", "warn")
    except Exception as e:
        log(f"钉钉通知异常: {e}", "error")


async def send_wechat(session, cfg, text, markdown):
    """企业微信群机器人 Webhook 推送（markdown 类型）"""
    webhook = cfg.get("wechat_webhook")
    if not webhook:
        return
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    try:
        async with session.post(webhook, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log(f"微信通知失败: HTTP {resp.status}", "warn")
    except Exception as e:
        log(f"微信通知异常: {e}", "error")


def _encrypt(data, key):
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    cipher = DES.new(key, DES.MODE_ECB)
    padded = pad(raw.encode("utf-8"), DES.block_size)
    return cipher.encrypt(padded).hex().upper()


def _decrypt(hex_data, key):
    raw = bytes.fromhex(hex_data.strip())
    cipher = DES.new(key, DES.MODE_ECB)
    decrypted = unpad(cipher.decrypt(raw), DES.block_size)
    return json.loads(decrypted.decode("utf-8"))


async def api_post(session, endpoint, data, devicenumber=None, encrypt_key=KEY):
    url = API_BASE + endpoint
    encrypted = _encrypt(data, encrypt_key)
    headers = {"Content-Type": "application/json", "devicetype": "H5"}
    if devicenumber:
        headers["devicenumber"] = devicenumber
    async with session.post(url, data=encrypted, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
        body = await resp.read()
    return _decrypt(body.decode(), KEY)


def make_open_api_request(secret, format_id):
    payload = {
        "formatIds": [
            {"formatId": format_id, "price": 0, "stock": 999}
        ],
        "count": 1,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sign = hashlib.md5((body + secret).encode("utf-8")).hexdigest()
    return body, sign


async def _wait_for_open_api_rate_slot(state):
    request_times = state.setdefault("open_api_request_times", deque())
    while True:
        now = time.monotonic()
        while request_times and now - request_times[0] >= 60:
            request_times.popleft()
        if len(request_times) < OPEN_API_MAX_REQUESTS_PER_MINUTE:
            request_times.append(now)
            return
        await asyncio.sleep(max(0.05, 60 - (now - request_times[0])))


async def _open_api_grab_once(session, username, body, sign, state):
    await _wait_for_open_api_rate_slot(state)
    started = time.perf_counter()
    query = urlencode({
        "timestamp": int(time.time()),
        "userName": username,
        "sign": sign,
    })
    url = f"{OPEN_API_BASE}/MarketOrder/ReceivingOrder?{query}"
    try:
        async with session.post(
            url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response_text = await response.text()

        request_ms = (time.perf_counter() - started) * 1000
        if response.status in (403, 429):
            return "rate_limited", request_ms
        if not 200 <= response.status < 300:
            log(f"开放接口 HTTP {response.status}", "warn")
            return "error", request_ms

        result = json.loads(response_text)
        message = str(result.get("msg") or "")
        if message == "签名错误":
            log("开放接口签名错误，已停止抢单", "error")
            return "fatal", request_ms
        if any(keyword in message for keyword in ("频繁", "限制", "限流")):
            return "rate_limited", request_ms

        orders = result.get("data") or []
        if not orders:
            return "empty", request_ms

        order = orders[0]
        name = order.get("formatName") or TARGET_CATALOG_NAME
        price = order.get("clinchPrice", "")
        log(f"[抢单成功] {name} | 接口 {request_ms:.1f}ms", "success")
        state["processing"] = max(1, state.get("processing", 0))
        state["pause_until"] = max(
            state.get("pause_until", 0), time.monotonic() + 20
        )
        window = state.get("window")
        if window:
            window.update_processing(state["processing"])
        cfg = state.get("cfg") or {}
        account = order.get("accounts", "")
        notification = (
            "#### 抢单成功\n"
            f"- **商品**: {name}\n"
            f"- **账号**: {account}\n"
            f"- **结算价格**: {price}\n"
            f"- **时间**: {time.strftime('%H:%M:%S')}"
        )
        _create_tracked_task(send_dingtalk(session, cfg, "抢单成功", notification), state)
        _create_tracked_task(send_wechat(session, cfg, "抢单成功", notification), state)
        return "success", request_ms
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log(f"开放接口异常: {type(exc).__name__}", "error")
        return "error", 0


async def open_api_grab_loop(session, username, secret, format_id, state):
    body, sign = make_open_api_request(secret, format_id)
    last_log_time = time.monotonic()
    rate_backoff = OPEN_API_RATE_BACKOFF_INITIAL
    error_backoff = OPEN_API_ERROR_BACKOFF_INITIAL
    burst_event = state.setdefault("burst_event", asyncio.Event())

    while True:
        pause_remaining = state.get("pause_until", 0) - time.monotonic()
        if state.get("processing", 0) > 0 or pause_remaining > 0:
            await asyncio.sleep(min(1, max(0.05, pause_remaining)) if pause_remaining > 0 else 1)
            continue

        is_burst = False
        try:
            await asyncio.wait_for(
                burst_event.wait(),
                timeout=random.uniform(OPEN_API_BASE_INTERVAL_MIN, OPEN_API_BASE_INTERVAL_MAX),
            )
            burst_event.clear()
            is_burst = True
        except asyncio.TimeoutError:
            pass

        delays = OPEN_API_BURST_DELAYS if is_burst else (0,)
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            if state.get("processing", 0) > 0:
                break

            outcome, request_ms = await _open_api_grab_once(
                session, username, body, sign, state
            )
            if outcome == "fatal":
                return
            if outcome == "success":
                log("抢到订单，至少暂停 20 秒", "warn")
                await asyncio.sleep(20)
                break
            if outcome == "rate_limited":
                log(f"触发接口限制，暂停 {rate_backoff} 秒", "warn")
                await asyncio.sleep(rate_backoff)
                rate_backoff = min(rate_backoff * 2, OPEN_API_RATE_BACKOFF_MAX)
                break
            if outcome == "error":
                log(f"网络或接口异常，{error_backoff} 秒后重试", "warn")
                await asyncio.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, OPEN_API_ERROR_BACKOFF_MAX)
                break

            rate_backoff = OPEN_API_RATE_BACKOFF_INITIAL
            error_backoff = OPEN_API_ERROR_BACKOFF_INITIAL
            now = time.monotonic()
            if now - last_log_time >= 5:
                request_times = state.get("open_api_request_times", ())
                log(
                    f"平衡抢单运行中：近 60 秒 {len(request_times)} 次，"
                    f"接口 {request_ms:.1f}ms"
                )
                last_log_time = now


async def login(session, username, password):
    r = await api_post(session, "/MemeberRegistered/GetNewDeviceInfo", {
        "devicetype": "PC",
        "info": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "host": "www.xuzuan.com"
    })
    if r.get("code") != 200:
        raise Exception("获取设备号失败: " + r.get("msg", ""))
    devicenumber = r["data"]["devicenumber"]
    log(f"设备号: {devicenumber}")

    r = await api_post(session, "/MemeberLogIn/XzwAccountPasswordLogIn", {
        "account": username, "pwd": password, "domain": "http://www.xuzuan.com"
    }, devicenumber=devicenumber)
    if r.get("code") != 200:
        raise Exception("登录失败: " + r.get("msg", ""))
    data = r["data"]
    if isinstance(data, str):
        data = json.loads(data)
    member_id = data["Id"]
    log(f"登录成功, 用户ID: {member_id}", "success")
    return member_id, devicenumber


async def get_order_list(session, member_id, devicenumber):
    r = await api_post(session, "/MarketOrder/ReceivingOrderList",
                       {"memberId": member_id}, devicenumber=devicenumber)
    return r.get("data") or []


async def get_processing_count(session, member_id, devicenumber):
    r = await api_post(session, "/MarketOrder/MyOrderList", {
        "status": 3, "content": "", "times": "", "timee": "",
        "page": 1, "size": 1, "memberId": member_id,
        "formatId": 0, "marketUserName": ""
    }, devicenumber=devicenumber)
    if r.get("code") == 200:
        return len(r.get("data") or [])
    return 0


def make_ws_payload(devicenumber, member_id):
    return {
        "Id": int(time.time() * 1000),
        "Parameter": {
            "deviceNo": devicenumber,
            "memberId": member_id
        },
        "RequestOrResponse": 0,
        "MethodName": "QueryOrder"
    }


async def grab_order(session, order, member_id, devicenumber):
    oid = order["Id"]
    data = {
        "id": oid,
        "domainName": "http://www.xuzuan.com/#/receiveOrder",
        "memberId": member_id
    }
    r = await api_post(session, "/MarketOrder/ReceivingOrder", data,
                       devicenumber=devicenumber, encrypt_key=KEY_GRAB)
    return r


async def grab_and_handle(session, order, member_id, devicenumber, grabbed, state):
    oid = order["Id"]
    try:
        if state["processing"] > 0:
            return

        if state.get("monitor_only", True):
            name = order.get("catalogName", "")
            push_type = order.get("pushType")
            log(f"[仅监控] 订单 {oid} - {name} | pushType={push_type}")
            return

        request_started = time.perf_counter()
        queue_ms = (request_started - order.get("_received_at", request_started)) * 1000
        grab_session = state.get("private_grab_session") or state.get("grab_session") or session
        r = await grab_order(grab_session, order, member_id, devicenumber)
        request_ms = (time.perf_counter() - request_started) * 1000
        timing = f"排队 {queue_ms:.1f}ms, 接口 {request_ms:.1f}ms"
        if r.get("code") == 200:
            name = order.get("catalogName", "")
            log(f"[抢单成功] 订单 {oid} - {name} | {timing}", "success")
            state["processing"] = max(1, state.get("processing", 0))
            state["pause_until"] = max(
                state.get("pause_until", 0), time.monotonic() + 20
            )
            window = state.get("window")
            if window:
                window.update_processing(state["processing"])
            if state.get("cfg"):
                acct = order.get("accounts_value") or order.get("accounts", "")
                ts = time.strftime("%H:%M:%S")
                msg = f"####  抢单成功\n- **订单ID**: {oid}\n- **商品**: {name}\n- **账号**: {acct}\n- **时间**: {ts}"
                _create_tracked_task(send_dingtalk(session, state["cfg"], "抢单成功", msg), state)
                _create_tracked_task(send_wechat(session, state["cfg"], "抢单成功", msg), state)
        elif r.get("code") == 506:
            log(f"[被抢走] 订单 {oid}: {r.get('msg','')} | {timing}", "warn")
        else:
            log(f"[抢单失败] 订单 {oid}: code={r.get('code')} msg={r.get('msg','')} | {timing}", "error")
    except Exception as e:
        log(f"[抢单异常] 订单 {oid}: {e}", "error")


def _create_tracked_task(coro, state):
    """创建并追踪异步任务，完成后自动清理，异常会记录日志"""
    bg_tasks = state.setdefault("_bg_tasks", set())
    task = asyncio.create_task(coro)
    bg_tasks.add(task)

    def _done(t):
        bg_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log(f"后台任务异常: {exc}", "error")

    task.add_done_callback(_done)
    return task


async def _grabbed_cleanup_loop(grabbed, state):
    """定期清理 grabbed 集合，防止长时间运行内存增长"""
    while True:
        try:
            await asyncio.sleep(GRABBED_CLEANUP_INTERVAL)
            if len(grabbed) > GRABBED_MAX_SIZE:
                # 保留最近的一半
                to_remove = list(grabbed)[:len(grabbed) - GRABBED_MAX_SIZE // 2]
                for oid in to_remove:
                    grabbed.discard(oid)
                log(f"grabbed 集合已清理，当前大小: {len(grabbed)}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"grabbed 清理异常: {e}", "error")


async def ws_order_listener(session, member_id, devicenumber, grabbed, state):
    last_log_time = 0
    reconnect_index = 0
    last_failure_log = 0
    failure_count = 0
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                reconnect_index = 0
                last_failure_log = 0
                failure_count = 0
                payload = make_ws_payload(devicenumber, member_id)
                await ws.send(_encrypt(payload, KEY))
                log("WebSocket 已连接, 等待订单推送...", "success")
                window = state.get("window")
                if window:
                    window.set_ws_status(True)

                while True:
                    msg = await ws.recv()
                    if isinstance(msg, bytes):
                        msg = msg.decode()
                    try:
                        orders = _decrypt(msg, KEY)
                    except Exception as e:
                        log(f"WS 解密失败: {e}", "error")
                        continue

                    if not isinstance(orders, list):
                        continue

                    if (
                        state["processing"] > 0
                        or state.get("pause_until", 0) > time.monotonic()
                    ):
                        continue

                    now = time.time()
                    if now - last_log_time >= 60:
                        log("正在抢单")
                        last_log_time = now
                    for order in orders:
                        oid = order.get("Id")
                        if oid is None:
                            continue
                        if order.get("pushType") in (2, 3, 5, 6, 7):
                            continue
                        if order.get("catalogName") != TARGET_CATALOG_NAME:
                            continue
                        if oid in grabbed:
                            continue
                        order["_received_at"] = time.perf_counter()
                        grabbed.add(oid)
                        if state.get("hybrid"):
                            state["burst_event"].set()
                            log(f"哈啰订单推送触发短时加速: {oid}")
                        _create_tracked_task(
                            grab_and_handle(session, order, member_id, devicenumber, grabbed, state),
                            state
                        )
        except asyncio.CancelledError:
            log("WebSocket 监听已取消", "warn")
            raise
        except Exception as e:
            reconnect_delay = WS_RECONNECT_DELAYS[reconnect_index]
            failure_count += 1
            now = time.monotonic()
            if failure_count == 1 or now - last_failure_log >= WS_ERROR_LOG_INTERVAL:
                if isinstance(e, websockets.ConnectionClosed):
                    message = "WebSocket 连接断开"
                else:
                    message = f"WebSocket 连接异常: {e}"
                log(
                    f"{message}，{reconnect_delay} 秒后重连"
                    f"（连续失败 {failure_count} 次）",
                    "warn",
                )
                last_failure_log = now
            window = state.get("window")
            if window:
                window.set_ws_status(False)
            await asyncio.sleep(reconnect_delay)
            reconnect_index = min(reconnect_index + 1, len(WS_RECONNECT_DELAYS) - 1)


async def rest_poll_loop(session, member_id, devicenumber, grabbed, state):
    while True:
        try:
            if state["processing"] > 0:
                await asyncio.sleep(2)
                continue

            orders = await get_order_list(session, member_id, devicenumber)
            if orders:
                for order in orders:
                    oid = order["Id"]
                    if order.get("catalogName") != TARGET_CATALOG_NAME:
                        continue
                    if oid in grabbed:
                        continue
                    order["_received_at"] = time.perf_counter()
                    grabbed.add(oid)
                    _create_tracked_task(
                        grab_and_handle(session, order, member_id, devicenumber, grabbed, state),
                        state
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"REST轮询异常: {e}", "error")
        await asyncio.sleep(2)


async def processing_monitor(session, member_id, devicenumber, state):
    while True:
        try:
            count = await get_processing_count(session, member_id, devicenumber)
            if count != state["processing"]:
                state["processing"] = count
                window = state.get("window")
                if window:
                    window.update_processing(count)
                if count > 0:
                    log(f"有 {count} 个订单处理中, 暂停抢新单", "warn")
                else:
                    log("处理中订单已清空, 恢复抢单", "success")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"查询处理中订单数异常: {e}", "error")
        await asyncio.sleep(3)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("续赚抢单工具")
        self.setWindowIcon(QIcon(resource_path("assets", "app_icon.png")))
        self.resize(920, 920)

        self.running = False
        self.tasks = []
        self.session = None
        self.grab_session = None
        self.member_id = None
        self.devicenumber = None
        self.grabbed = set()
        self.state = {}
        self._log_line_count = 0

        self._setup_ui()
        self._load_config_to_ui()
        _log_emitter.appended.connect(self._on_log)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setSpacing(6)

        left = QWidget()
        left_v = QVBoxLayout(left)
        left_v.setSpacing(6)

        cfg_group = QGroupBox("配置")
        cfg_form = QFormLayout(cfg_group)

        row = QHBoxLayout()
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("请输入账号")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("请输入密码")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_pwd_cb = QCheckBox("显示")
        self.show_pwd_cb.toggled.connect(
            lambda checked: self.password_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        row.addWidget(self.username_edit, stretch=3)
        row.addWidget(QLabel("密码:"))
        row.addWidget(self.password_edit, stretch=2)
        row.addWidget(self.show_pwd_cb)
        cfg_form.addRow("账号:", row)

        open_api_row = QHBoxLayout()
        self.open_api_secret_edit = QLineEdit()
        self.open_api_secret_edit.setPlaceholderText("请输入开放接口密钥")
        self.open_api_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_open_api_secret_cb = QCheckBox("显示")
        self.show_open_api_secret_cb.toggled.connect(
            lambda checked: self.open_api_secret_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        self.format_id_edit = QLineEdit()
        self.format_id_edit.setPlaceholderText("请输入数字商品编号")
        open_api_row.addWidget(self.open_api_secret_edit, stretch=3)
        open_api_row.addWidget(self.show_open_api_secret_cb)
        open_api_row.addWidget(QLabel("商品编号:"))
        open_api_row.addWidget(self.format_id_edit, stretch=1)
        cfg_form.addRow("开放接口:", open_api_row)

        password_hint = QLabel("账号、登录密码、开放密钥和商品编号只用于本次运行，不会保存")
        password_hint.setStyleSheet("color: #777;")
        cfg_form.addRow("", password_hint)

        wr = QHBoxLayout()
        self.webhook_edit = QLineEdit()
        self.webhook_edit.setPlaceholderText("钉钉机器人 Webhook 地址(可选)")
        self.save_btn = QPushButton("保存通知")
        self.save_btn.clicked.connect(self._on_save_config)
        wr.addWidget(self.webhook_edit)
        wr.addWidget(self.save_btn)
        cfg_form.addRow("Webhook:", wr)

        self.secret_edit = QLineEdit()
        self.secret_edit.setPlaceholderText("钉钉机器人 Secret(可选)")
        cfg_form.addRow("钉钉密钥:", self.secret_edit)

        self.wechat_edit = QLineEdit()
        self.wechat_edit.setPlaceholderText("企业微信群机器人 Webhook(可选)")
        cfg_form.addRow("微信:", self.wechat_edit)
        left_v.addWidget(cfg_group)

        bar = QHBoxLayout()
        self.status_ws = QLabel("● 未连接")
        self.status_ws.setStyleSheet("color: #999;")
        self.status_device = QLabel("设备号: --")
        self.status_user = QLabel("用户ID: --")
        self.status_processing = QLabel("处理中: 0")
        for lbl in (self.status_ws, self.status_device, self.status_user, self.status_processing):
            bar.addWidget(lbl)
        bar.addStretch()
        left_v.addLayout(bar)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶ 启动")
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("清空日志")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.clear_btn.clicked.connect(self._on_clear_log)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch()
        left_v.addLayout(btn_row)

        safety_label = QLabel("混合模式：开放接口低频预抢，哈啰推送时短暂加速")
        safety_label.setStyleSheet("color: #c62828; font-weight: bold;")
        left_v.addWidget(safety_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))

        # 右侧并排的 WAP 浏览器窗口
        self.wap_view = QWebEngineView()
        # 使用持久化 profile，使登录 cookie 跨启动保留
        profile_dir = app_data_path("web_profile")
        self._wap_profile = QWebEngineProfile("xuzuan", self.wap_view)
        self._wap_profile.setPersistentStoragePath(profile_dir)
        self._wap_profile.setCachePath(profile_dir)
        self._wap_profile.setHttpUserAgent(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
            "Mobile/15E148 Safari/604.1"
        )
        self._wap_page = QWebEnginePage(self._wap_profile, self.wap_view)
        self.wap_view.setPage(self._wap_page)
        # 参照 iPhone 12 Pro：固定 390 x 844
        self.wap_view.setFixedSize(390, 844)
        self.wap_view.load(QUrl("http://www.xuzuan.com"))

        left_v.addWidget(self.log_text, stretch=1)

        layout.addWidget(left, stretch=1)
        layout.addWidget(self.wap_view, stretch=0)

    def _on_log(self, msg):
        colored = (
            msg.replace("[INFO]", '<span style="color:#888">[INFO]</span>')
            .replace("[SUCCESS]", '<span style="color:#4caf50">[SUCCESS]</span>')
            .replace("[WARN]", '<span style="color:#ff9800">[WARN]</span>')
            .replace("[ERROR]", '<span style="color:#f44336">[ERROR]</span>')
        )
        colored = colored.replace("\n", "<br>")
        self.log_text.append(f"<p style='margin:1px 0'>{colored}</p>")

        # 日志行数限流：超过上限时清理前半部分，防止 UI 卡顿
        self._log_line_count += 1
        if self._log_line_count >= MAX_LOG_LINES:
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            remove_count = MAX_LOG_LINES // 2
            for _ in range(remove_count):
                cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            self._log_line_count -= remove_count

        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _set_config_enabled(self, enabled):
        self.username_edit.setEnabled(enabled)
        self.password_edit.setEnabled(enabled)
        self.open_api_secret_edit.setEnabled(enabled)
        self.show_open_api_secret_cb.setEnabled(enabled)
        self.format_id_edit.setEnabled(enabled)
        self.webhook_edit.setEnabled(enabled)
        self.secret_edit.setEnabled(enabled)
        self.show_pwd_cb.setEnabled(enabled)
        self.save_btn.setEnabled(enabled)

    def _on_save_config(self):
        cfg = {
            "dingtalk_webhook": self.webhook_edit.text().strip(),
            "dingtalk_secret": self.secret_edit.text().strip(),
            "wechat_webhook": self.wechat_edit.text().strip(),
        }
        save_config(cfg)
        log("通知配置已保存；账号、密码、开放密钥和商品编号不会保存")

    def _load_config_to_ui(self):
        cfg = load_config()
        if cfg:
            self.webhook_edit.setText(cfg.get("dingtalk_webhook", ""))
            self.secret_edit.setText(cfg.get("dingtalk_secret", ""))
            self.wechat_edit.setText(cfg.get("wechat_webhook", ""))

    def set_ws_status(self, connected):
        if connected:
            self.status_ws.setText("● 已连接")
            self.status_ws.setStyleSheet("color: #4caf50; font-weight: bold;")
        else:
            self.status_ws.setText("● 已断开")
            self.status_ws.setStyleSheet("color: #f44336; font-weight: bold;")

    def update_processing(self, count):
        self.status_processing.setText(f"处理中: {count}")

    @asyncSlot()
    async def _on_start(self):
        username = self.username_edit.text().strip()
        password = self.password_edit.text().strip()
        open_api_secret = self.open_api_secret_edit.text().strip()
        format_id = self.format_id_edit.text().strip()
        if not username or not open_api_secret or not format_id or (
            GRAB_MODE != "open_api" and not password
        ):
            QMessageBox.warning(
                self, "错误", "账号、密码、开放密钥和商品编号不能为空"
            )
            return
        if not format_id.isdigit():
            QMessageBox.warning(self, "错误", "商品编号只能包含数字")
            return

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_config_enabled(False)

        cfg = {
            "dingtalk_webhook": self.webhook_edit.text().strip(),
            "dingtalk_secret": self.secret_edit.text().strip(),
            "wechat_webhook": self.wechat_edit.text().strip(),
        }
        save_config(cfg)
        self.password_edit.clear()
        self.open_api_secret_edit.clear()

        self.grabbed.clear()
        self.state = {
            "processing": 0,
            "cfg": cfg,
            "window": self,
            "monitor_only": MONITOR_ONLY,
            "_bg_tasks": set(),
        }

        if GRAB_MODE == "open_api":
            connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300, keepalive_timeout=300)
            self.grab_session = aiohttp.ClientSession(connector=connector)
            self.state["grab_session"] = self.grab_session
            self.state["burst_event"] = asyncio.Event()
            self.state["open_api_request_times"] = deque()
            self.running = True
            self.status_ws.setText("● 平衡抢单中")
            self.status_ws.setStyleSheet("color: #4caf50; font-weight: bold;")
            self.status_device.setText("模式: 开放接口")
            self.status_user.setText(f"商品编号: {format_id}")
            self.tasks = [
                asyncio.create_task(open_api_grab_loop(
                    self.grab_session, username, open_api_secret, format_id, self.state
                ))
            ]
            log("开放接口平衡抢单已启动", "success")
            return

        if GRAB_MODE == "hybrid":
            log("平衡混合抢单模式已启用", "warn")
        else:
            log("真实抢单模式已启用，将自动提交抢单请求", "warn")
        log("正在登录...")
        try:
            self.session = aiohttp.ClientSession()
            self.member_id, self.devicenumber = await login(self.session, username, password)
        except Exception as e:
            log(f"登录失败: {e}", "error")
            if self.session:
                await self.session.close()
                self.session = None
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self._set_config_enabled(True)
            return

        if GRAB_MODE == "hybrid":
            connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300, keepalive_timeout=300)
            self.grab_session = aiohttp.ClientSession(connector=connector)
            self.state.update({
                "hybrid": True,
                "burst_event": asyncio.Event(),
                "open_api_request_times": deque(),
                "private_grab_session": self.session,
            })
        else:
            connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300, keepalive_timeout=300)
            self.grab_session = aiohttp.ClientSession(connector=connector)
            self.state["grab_session"] = self.grab_session
            try:
                await get_order_list(self.grab_session, self.member_id, self.devicenumber)
                log("抢单专用连接已预热", "success")
            except Exception as e:
                log(f"抢单连接预热失败，将在首单时连接: {e}", "warn")

        self.status_user.setText(f"用户ID: {self.member_id}")
        self.status_device.setText(f"设备号: {self.devicenumber}")
        self.status_ws.setText("● 等待连接")
        self.status_ws.setStyleSheet("color: #ff9800; font-weight: bold;")

        # 将设备号写入 WAP 浏览器 cookie 并重新加载
        cookie = f"deviceNo={self.devicenumber}; path=/; domain=.xuzuan.com"
        parsed = QNetworkCookie.parseCookies(cookie.encode())
        if parsed:
            self._wap_profile.cookieStore().setCookie(parsed[0])
        self.wap_view.load(QUrl("http://www.xuzuan.com"))

        self.running = True
        if GRAB_MODE == "hybrid":
            self.status_device.setText("模式: 混合抢单")
            self.status_user.setText(f"商品编号: {format_id}")
            self.tasks = [
                asyncio.create_task(open_api_grab_loop(
                    self.grab_session, username, open_api_secret, format_id, self.state
                )),
                asyncio.create_task(ws_order_listener(self.session, self.member_id, self.devicenumber, self.grabbed, self.state)),
                asyncio.create_task(processing_monitor(self.session, self.member_id, self.devicenumber, self.state)),
                asyncio.create_task(_grabbed_cleanup_loop(self.grabbed, self.state)),
            ]
            log("混合抢单已启动：低频预抢 + 推送短时加速", "success")
        else:
            self.tasks = [
                asyncio.create_task(ws_order_listener(self.session, self.member_id, self.devicenumber, self.grabbed, self.state)),
                asyncio.create_task(rest_poll_loop(self.session, self.member_id, self.devicenumber, self.grabbed, self.state)),
                asyncio.create_task(processing_monitor(self.session, self.member_id, self.devicenumber, self.state)),
                asyncio.create_task(_grabbed_cleanup_loop(self.grabbed, self.state)),
            ]
            log("抢单已启动", "success")

    @asyncSlot()
    async def _on_stop(self):
        if not self.running:
            return
        self.running = False
        log("正在停止...", "warn")

        # 取消所有主任务
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

        # 取消所有后台追踪任务（钉钉通知等）
        bg_tasks = self.state.get("_bg_tasks", set())
        for task in list(bg_tasks):
            task.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        bg_tasks.clear()

        if self.session:
            await self.session.close()
            self.session = None
        if self.grab_session:
            await self.grab_session.close()
            self.grab_session = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_config_enabled(True)
        self.status_ws.setText("● 未连接")
        self.status_ws.setStyleSheet("color: #999;")
        log("已停止", "warn")

    def _on_clear_log(self):
        self.log_text.clear()
        self._log_line_count = 0

    def closeEvent(self, event):
        if self.running:
            self.running = False
            for task in self.tasks:
                task.cancel()
            bg_tasks = self.state.get("_bg_tasks", set())
            for task in list(bg_tasks):
                task.cancel()
        if self.session and not self.session.closed:
            asyncio.ensure_future(self.session.close())
        if self.grab_session and not self.grab_session.closed:
            asyncio.ensure_future(self.grab_session.close())
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setOrganizationName("xinicn")
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(QIcon(resource_path("assets", "app_icon.png")))
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    w = MainWindow()
    w.show()
    with loop:
        loop.run_forever()
