#!/usr/bin/env python3
"""Muse <-> WeChat bridge, text-only v1.

Speaks the iLink bot protocol documented by Tencent/openclaw-weixin
(docs/protocol_zh_CN.md) and implemented in its MIT-licensed sources:
  - QR login:  POST /ilink/bot/get_bot_qrcode?bot_type=3
                GET  /ilink/bot/get_qrcode_status?qrcode=...
  - Receive:   POST /ilink/bot/getupdates (long-poll, cursor get_updates_buf)
  - Send:      POST /ilink/bot/sendmessage (text item, context_token echoed)
  - Lifecycle: POST /ilink/bot/msg/notifystart | notifystop

Modes:
  login   fetch QR, poll status, save credentials on confirmed
  run     long-poll inbound -> inbox.jsonl, outbox.jsonl -> sendmessage

The bot token is a credential: it is stored only in .secrets/credentials.json
(mode 0600) and never printed to stdout or logs.
"""

import base64
import json
import os
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote

FIXED_BASE = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
CHANNEL_VERSION = "2.4.10"
BOT_AGENT = "MuseBridge/1.0"
CLIENT_VERSION = str((2 << 16) | (4 << 8) | 10)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.join(BASE_DIR, ".secrets")
CREDS_PATH = os.path.join(SECRETS_DIR, "credentials.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
INBOX_PATH = os.path.join(BASE_DIR, "inbox.jsonl")
INBOX_RAW_PATH = os.path.join(BASE_DIR, "inbox_raw.jsonl")
OUTBOX_PATH = os.path.join(BASE_DIR, "outbox.jsonl")
LOGIN_STATUS_PATH = os.path.join(BASE_DIR, "login_status.json")
VERIFY_CODE_PATH = os.path.join(BASE_DIR, "verify_code.txt")
QR_PNG_PATH = os.path.join(BASE_DIR, "login_qr.png")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def random_uin():
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def common_headers():
    return {"iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION}


def post_headers(token=None):
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_uin(),
    }
    headers.update(common_headers())
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_post(base, endpoint, body, token=None, timeout=15):
    url = base.rstrip("/") + "/" + endpoint
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=post_headers(token), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_get(base, endpoint, timeout=35):
    url = base.rstrip("/") + "/" + endpoint
    req = urllib.request.Request(url, headers=common_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def save_json_atomic(path, obj, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"cursor": "", "outbox_offset": 0, "seen_ids": []}


def save_state(state):
    save_json_atomic(STATE_PATH, state)


def write_login_status(obj):
    save_json_atomic(LOGIN_STATUS_PATH, obj)


def render_qr(payload):
    """Return the path of a QR image for `payload`.

    If the payload is an image URL, download it. Otherwise render the payload
    itself as a QR code image (same as the upstream terminal display).
    """
    # Keep the raw payload locally (not in chat) so the login link can be
    # offered as a tap-to-open fallback if QR scanning keeps expiring.
    with open(os.path.join(BASE_DIR, "login_qr_payload.txt"), "w") as f:
        f.write(payload)
    if payload.startswith("http"):
        try:
            req = urllib.request.Request(payload, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                ctype = resp.headers.get_content_type()
                data = resp.read()
            if ctype.startswith("image/"):
                ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}.get(
                    ctype, "png"
                )
                path = os.path.join(BASE_DIR, f"login_qr.{ext}")
                with open(path, "wb") as f:
                    f.write(data)
                log(f"QR image downloaded ({ctype}, {len(data)} bytes)")
                return path
        except Exception as exc:  # fall through to rendering the payload itself
            log(f"QR payload URL fetch failed, rendering as QR instead: {exc}")
    try:
        import qrcode  # type: ignore
    except ImportError:
        log("python qrcode lib missing; writing payload to login_qr.txt instead")
        txt = os.path.join(BASE_DIR, "login_qr.txt")
        with open(txt, "w") as f:
            f.write(payload)
        return txt
    img = qrcode.make(payload)
    img.save(QR_PNG_PATH)
    log(f"QR rendered to {QR_PNG_PATH}")
    return QR_PNG_PATH


def fetch_qr():
    last_exc = None
    for attempt in range(3):
        try:
            data = http_post(
                FIXED_BASE,
                f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
                {"local_token_list": []},
                token=None,
                timeout=30,
            )
            return data["qrcode"], data.get("qrcode_img_content", "")
        except Exception as exc:  # transient egress stalls: retry with backoff
            last_exc = exc
            log(f"fetch_qr attempt {attempt + 1} failed: {exc}")
            time.sleep(2 * (attempt + 1))
    raise last_exc


def cmd_login():
    qrcode, img_content = fetch_qr()
    qr_path = render_qr(img_content)
    write_login_status({"stage": "wait_scan", "qr_path": qr_path})
    log(f"QR ready: {qr_path} -- waiting for scan (up to 8 min)")

    base = FIXED_BASE
    deadline = time.time() + 480
    refreshes = 0
    verify_code = None
    while time.time() < deadline:
        if os.path.exists(VERIFY_CODE_PATH):
            with open(VERIFY_CODE_PATH) as f:
                verify_code = f.read().strip()
            os.remove(VERIFY_CODE_PATH)
            log("verify code supplied, polling with it")
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code)}"
        try:
            st = http_get(base, endpoint, timeout=35)
        except Exception as exc:
            log(f"poll error (treated as wait): {exc}")
            st = {"status": "wait"}
        status = st.get("status")
        log(f"qr status: {status}")

        if status == "scaned":
            if verify_code:
                log("verify code accepted")
                verify_code = None
            write_login_status({"stage": "scanned"})
        elif status == "need_verifycode":
            write_login_status({"stage": "need_verifycode"})
            log("NEED_VERIFYCODE: put the digits from WeChat into verify_code.txt")
            for _ in range(45):  # wait up to 90s for the code, then poll again
                if os.path.exists(VERIFY_CODE_PATH):
                    break
                time.sleep(2)
            continue
        elif status == "scaned_but_redirect":
            host = st.get("redirect_host")
            if host:
                base = f"https://{host}"
                log(f"IDC redirect, polling host now {host}")
        elif status == "binded_redirect":
            write_login_status({"stage": "binded_redirect"})
            log("bot already bound to another instance, aborting")
            return 2
        elif status in ("expired", "verify_code_blocked"):
            if status == "verify_code_blocked":
                verify_code = None
            refreshes += 1
            if refreshes > 3:
                write_login_status({"stage": "failed", "reason": status})
                log("QR refreshed too many times, giving up")
                return 2
            log(f"QR {status}, refreshing ({refreshes}/3)")
            qrcode, img_content = fetch_qr()
            qr_path = render_qr(img_content)
            write_login_status({"stage": "wait_scan", "qr_path": qr_path})
            log(f"new QR ready: {qr_path}")
        elif status == "confirmed":
            bot_id = st.get("ilink_bot_id")
            if not bot_id:
                write_login_status({"stage": "failed", "reason": "no_bot_id"})
                log("confirmed but server returned no ilink_bot_id")
                return 2
            creds = {
                "bot_token": st.get("bot_token"),
                "ilink_bot_id": bot_id,
                "baseurl": st.get("baseurl") or base,
                "ilink_user_id": st.get("ilink_user_id"),
            }
            os.makedirs(SECRETS_DIR, exist_ok=True)
            save_json_atomic(CREDS_PATH, creds, mode=0o600)
            write_login_status({"stage": "confirmed", "ilink_bot_id": bot_id})
            log("LOGIN CONFIRMED, credentials saved (token not printed)")
            return 0
        # "wait" and anything else: keep polling
        time.sleep(1)

    write_login_status({"stage": "timeout"})
    log("login timed out")
    return 2


def extract_text(msg):
    """First text item, voice transcription, or a media label (v1: text only)."""
    for item in msg.get("item_list") or []:
        t = item.get("type")
        if t == 1 and (item.get("text_item") or {}).get("text") is not None:
            return str(item["text_item"]["text"])
        if t == 3 and (item.get("voice_item") or {}).get("text"):
            return str(item["voice_item"]["text"])
        if t in (2, 4, 5):
            return {2: "[图片]", 4: "[文件]", 5: "[视频]"}.get(t, "[媒体]")
    return ""


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_run():
    with open(CREDS_PATH) as f:
        creds = json.load(f)
    token = creds["bot_token"]
    base = creds.get("baseurl") or FIXED_BASE
    account = creds["ilink_bot_id"]
    lock = threading.Lock()
    state = load_state()
    seen = set(state.get("seen_ids") or [])

    try:
        http_post(base, "ilink/bot/msg/notifystart", {"base_info": base_info()}, token, 10)
        log("notifyStart ok")
    except Exception as exc:
        log(f"notifyStart failed (ignored): {exc}")

    stop = {"flag": False}

    def on_term(*_args):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    def sender_loop():
        """Independent sender: polls outbox.jsonl every 2s so a reply does
        not wait for the next getUpdates long-poll cycle (up to ~35s)."""
        while not stop["flag"]:
            try:
                with open(OUTBOX_PATH) as f:
                    lines = f.readlines()
            except FileNotFoundError:
                lines = []
            with lock:
                offset = state.get("outbox_offset", 0)
            if offset >= len(lines):
                time.sleep(2)
                continue
            line = lines[offset].strip()
            if not line:
                with lock:
                    state["outbox_offset"] = offset + 1
                    save_state(state)
                continue
            try:
                item = json.loads(line)
            except ValueError:
                # Never swallow a broken line silently: a merged-line bug once
                # ate a whole summary. Log it loudly, then skip.
                log(f"outbox line {offset} unparseable, skipping (len={len(line)})")
                with lock:
                    state["outbox_offset"] = offset + 1
                    save_state(state)
                continue
            body = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": item["to_user_id"],
                    "client_id": str(uuid.uuid4()),
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": item["text"]}}],
                    "context_token": item.get("context_token"),
                },
                "base_info": base_info(),
            }
            try:
                http_post(base, "ilink/bot/sendmessage", body, token, timeout=15)
            except Exception as exc:
                log(f"sendmessage failed, will retry: {exc}")
                time.sleep(5)
                continue
            with lock:
                state["outbox_offset"] = offset + 1
                save_state(state)
            log(f"sent reply to={item['to_user_id']} len={len(item['text'])}")

    sender = threading.Thread(target=sender_loop, daemon=True)
    sender.start()

    failures = 0
    log(f"bridge running (account={account})")
    while not stop["flag"]:
        try:
            resp = http_post(
                base,
                "ilink/bot/getupdates",
                {"get_updates_buf": state.get("cursor", ""), "base_info": base_info()},
                token,
                timeout=45,
            )
        except Exception as exc:
            failures += 1
            log(f"getUpdates error ({failures}): {exc}")
            time.sleep(30 if failures >= 3 else 2)
            if failures >= 3:
                failures = 0
            continue
        ret = resp.get("ret")
        err = resp.get("errcode")
        if (ret is not None and ret != 0) or (err is not None and err != 0):
            if ret == -14 or err == -14:
                log("STALE TOKEN (-14): pausing all requests for 1h")
                failures = 0
                for _ in range(360):
                    if stop["flag"]:
                        break
                    time.sleep(10)
                continue
            failures += 1
            log(f"getUpdates api error ret={ret} err={err} msg={resp.get('errmsg')}")
            time.sleep(30 if failures >= 3 else 2)
            if failures >= 3:
                failures = 0
            continue
        failures = 0
        if resp.get("get_updates_buf"):
            with lock:
                state["cursor"] = resp["get_updates_buf"]
                save_state(state)
        for msg in resp.get("msgs") or []:
            append_jsonl(INBOX_RAW_PATH, msg)
            if msg.get("message_type") != 1:  # only user messages, not bot echoes
                continue
            mid = str(msg.get("message_id") or "")
            if mid and mid in seen:
                continue
            text = extract_text(msg)
            if not text:
                continue
            if mid:
                seen.add(mid)
                with lock:
                    state["seen_ids"] = list(seen)[-500:]
                    save_state(state)
            append_jsonl(
                INBOX_PATH,
                {
                    "from_user_id": msg.get("from_user_id"),
                    "text": text,
                    "context_token": msg.get("context_token"),
                    "message_id": mid,
                    "create_time_ms": msg.get("create_time_ms"),
                },
            )
            log(f"inbound from={msg.get('from_user_id')} text={text[:80]!r}")
        # Replies are sent by the independent sender thread.

    try:
        http_post(base, "ilink/bot/msg/notifystop", {"base_info": base_info()}, token, 10)
        log("notifyStop ok")
    except Exception as exc:
        log(f"notifyStop failed (ignored): {exc}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("login", "run"):
        print("usage: bridge.py <login|run>", file=sys.stderr)
        sys.exit(2)
    sys.exit(cmd_login() if sys.argv[1] == "login" else cmd_run())
