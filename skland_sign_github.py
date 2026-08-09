"""
森空岛自动签到脚本（GitHub Actions 版）
支持从环境变量 SKLAND_TOKEN 读取 Token
"""
import json
import hashlib
import hmac
import time
import os
import sys
import requests
from urllib.parse import urlparse

APP_VERSION = "1.5.1"

HEADER_FOR_SIGN = {
    "platform": "1",
    "timestamp": "",
    "dId": "",
    "vName": APP_VERSION,
}

HEADER_LOGIN = {
    "User-Agent": f"Skland/{APP_VERSION} (com.hypergryph.skland; build:100001014; Android 31; ) Okhttp/4.11.0",
    "Accept-Encoding": "gzip",
    "Connection": "close",
}


def generate_signature(token, path, body_or_query):
    t = str(int(time.time()) - 2)
    hc = json.loads(json.dumps(HEADER_FOR_SIGN))
    hc["timestamp"] = t
    hc_str = json.dumps(hc, separators=(",", ":"))

    if body_or_query is None:
        body_or_query = ""
    elif not isinstance(body_or_query, str):
        body_or_query = json.dumps(body_or_query, separators=(",", ":"))

    s = path + body_or_query + t + hc_str
    hex_s = hmac.new(token.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).hexdigest()
    sign = hashlib.md5(hex_s.encode("utf-8")).hexdigest()
    return sign, hc


def get_sign_header(url, method, body, base_headers, sign_token):
    h = json.loads(json.dumps(base_headers))
    p = urlparse(url)
    if method.lower() == "get":
        h["sign"], hc = generate_signature(sign_token, p.path, p.query or "")
    else:
        h["sign"], hc = generate_signature(sign_token, p.path, json.dumps(body))
    for k, v in hc.items():
        h[k] = v
    return h


def get_grant_code(hg_token):
    resp = requests.post(
        "https://as.hypergryph.com/user/oauth2/v2/grant",
        json={"appCode": "4ca99fa6b56cc2ba", "token": hg_token, "type": 0},
        headers=HEADER_LOGIN,
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != 0:
        raise RuntimeError(f"获取授权码失败: {result.get('msg')}")
    return result["data"]["code"]


def get_cred(grant_code):
    resp = requests.post(
        "https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code",
        json={"code": grant_code, "kind": 1},
        headers=HEADER_LOGIN,
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"换取cred失败: {result.get('message')}")
    return result["data"]["cred"], result["data"]["token"]


def get_bindings(cred, sign_token):
    url = "https://zonai.skland.com/api/v1/game/player/binding"
    base_headers = {"cred": cred, **HEADER_LOGIN}
    headers = get_sign_header(url, "get", None, base_headers, sign_token)
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"获取角色绑定失败: {result.get('message')}")

    bindings = []
    for item in result["data"]["list"]:
        app_name = item.get("appName", "")
        for b in item.get("bindingList", []):
            bindings.append({
                "appName": app_name,
                "uid": b["uid"],
                "gameId": b.get("gameId", b["channelMasterId"]),
                "nickName": b.get("nickName", "未知"),
                "channelName": b.get("channelName", ""),
            })
    return bindings


def do_sign(cred, sign_token, uid, game_id):
    url = "https://zonai.skland.com/api/v1/game/attendance"
    body = {"uid": str(uid), "gameId": game_id}
    base_headers = {"cred": cred, **HEADER_LOGIN}
    headers = get_sign_header(url, "post", body, base_headers, sign_token)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def run_sign():
    hg_token = os.environ.get("SKLAND_TOKEN", "")

    if not hg_token:
        print("错误: 未设置 SKLAND_TOKEN 环境变量")
        sys.exit(1)

    # Step 1
    try:
        grant_code = get_grant_code(hg_token)
        cred, sign_token = get_cred(grant_code)
        print(f"[OK] Token 验证成功")
    except Exception as e:
        print(f"[ERROR] Token 无效或已过期: {e}")
        sys.exit(1)

    # Step 2
    try:
        bindings = get_bindings(cred, sign_token)
        print(f"[OK] 获取到 {len(bindings)} 个绑定角色")
    except Exception as e:
        print(f"[ERROR] 获取角色绑定失败: {e}")
        sys.exit(1)

    if not bindings:
        print("[INFO] 没有绑定任何游戏角色")
        return

    # Step 3
    success_count = 0
    for b in bindings:
        app_name = b["appName"]
        uid = b["uid"]
        game_id = b["gameId"]
        nickname = b["nickName"]
        channel = b["channelName"]

        try:
            result = do_sign(cred, sign_token, uid, game_id)
            code = result.get("code", -1)
            msg = result.get("message", "")

            if code == 0:
                awards = result.get("data", {}).get("awards", [])
                award_str = " / ".join(
                    f"{a['resource']['name']}x{a.get('count', 1)}" for a in awards
                )
                print(f"[OK] {app_name} - {nickname}({channel}) 签到成功 | {award_str}")
                success_count += 1
            elif code in (10001, 10002):
                print(f"[SKIP] {app_name} - {nickname}({channel}) 今日已签到")
                success_count += 1
            else:
                print(f"[FAIL] {app_name} - {nickname}({channel}) 签到失败: {msg}")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if hasattr(e, 'response') else 0
            if status == 403:
                print(f"[SKIP] {app_name} - {nickname}({channel}) 请求被拒绝(可能今日已签到或风控)")
                success_count += 1
            else:
                print(f"[ERROR] {app_name} - {nickname}({channel}) HTTP {status}: {e}")
        except Exception as e:
            print(f"[ERROR] {app_name} - {nickname}({channel}) 签到异常: {e}")

        time.sleep(1)

    print(f"\n签到完成: {success_count}/{len(bindings)} 个角色处理成功")


if __name__ == "__main__":
    run_sign()
