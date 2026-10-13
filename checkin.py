#!/usr/bin/env python3
"""SYNC PROJECT (同调计划) 每日自动签到/打招呼

机制参考 skland-daily-attendance:
  - 凭证通过环境变量注入 (GitHub Secrets), 多账号逗号分隔
  - 每个账号: 拉取全部好友 -> 逐个打招呼 (ping, +10 经验/次)
  - 会话失效时发送飞书通知提醒更新 cookie
  - 失败自动重试

环境变量:
  SYNC_COOKIES         必填, 会话 cookie (Zwin-ArkRadar 的 UUID 值), 多账号用逗号分隔
  SYNC_FEISHU_WEBHOOK  可选, 飞书自定义机器人 webhook (open.feishu.cn/open-apis/bot/v2/hook/xxx)
  SYNC_MAX_RETRIES     可选, 单账号最大重试次数, 默认 3
  SYNC_DELAY           可选, 每次打招呼间隔秒数, 默认 1.5
  SYNC_MEET_ENABLE     可选, 启用每日寻访加好友, 默认 true (设 false 关闭)
  SYNC_MEET_STATE_FILE 可选, 寻访状态文件路径, 默认 meet_state.json
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime

import requests

BASE = "https://www.tongdiaojihua.com/zwinport"
SALT = "sWsx5468kklgd542"  # 客户端硬编码签名盐 (AppApi::gentoken)


def auth_pair():
    t = str(time.time_ns() // 1000)
    return f"{hashlib.md5((t + SALT).encode()).hexdigest()},{t}", t


class SessionDead(Exception):
    pass


class Client:
    def __init__(self, cookie: str):
        self.cookie = cookie.strip()
        self.s = requests.Session()

    def call(self, path, method="GET", data=None):
        auth, tn = auth_pair()
        h = {"user-agent": "zwintech-arkradar-app", "content-type": "application/json",
             "authorization": auth, "timenum": tn,
             "cookie": f"Zwin-ArkRadar={self.cookie}"}
        fn = self.s.get if method == "GET" else self.s.post
        return fn(f"{BASE}/{path}", headers=h, data=data, timeout=20)

    @staticmethod
    def _json(r):
        t = r.text.strip()
        return json.loads(t) if t.startswith("{") else None

    def me(self):
        r = self.call("member/list", "POST", json.dumps({}))
        d = self._json(r)
        if not d or not d.get("userInfo"):
            raise SessionDead("会话失效")
        return d["userInfo"]

    def level_info(self):
        r = self.call("api/experience/level-info")
        d = self._json(r) or {}
        return d.get("data") or {}

    def all_friends(self):
        friends, page = [], 1
        while True:
            r = self.call(f"api/friends/friendList?pageIndex={page}&pageSize=20"
                          f"&queryValue=&isOrder=true&serverName=&isSameCity=0")
            d = self._json(r)
            if not d:
                raise SessionDead("会话失效")
            data = d.get("data") or {}
            records = data.get("records") or []
            if not records:
                break
            friends.extend(records)
            if page >= (data.get("pages") or 1):
                break
            page += 1
            time.sleep(0.8)
        return friends

    def ping(self, friend_user_id: int):
        return self.call("api/friends/ping", "POST",
                         json.dumps({"friendUserId": friend_user_id}))

    def pick_bottle(self):
        """寻访捞瓶 (消耗 2 理智, 理智不足时返回业务 fail)"""
        return self.call("api/drift-bottle/pick", "POST", json.dumps({}))

    def pending_list(self):
        """全部捞瓶记录 (含瓶主 userId, 可发起加好友)"""
        r = self.call("app/pollingTask/getPendingList?lastTime=0")
        d = self._json(r) or {}
        return d.get("list") or []

    def add_friend(self, friend_user_id: int):
        return self.call("api/friends/addFriend", "POST",
                         json.dumps({"friendUserId": friend_user_id}))


def run_account(cookie: str, idx: int, delay: float, retries: int) -> dict:
    """执行单账号签到, 返回结构化结果"""
    res = {
        "ok": False, "error": None, "nickname": f"账号#{idx + 1}",
        "uid": None, "friend_count": 0, "ping_ok": 0, "ping_fail": 0,
        "level": None, "level_name": None, "exp_gained": 0,
        "total_exp_before": None, "total_exp_after": None,
        "level_progress": None,
    }
    c = Client(cookie)
    for attempt in range(1, retries + 1):
        try:
            me = c.me()
            res["nickname"] = me.get("nickname") or f"账号#{idx + 1}"
            res["uid"] = me.get("id")

            li = c.level_info()
            res["level"] = li.get("level")
            res["level_name"] = li.get("levelName")
            res["total_exp_before"] = li.get("totalExperience")
            res["level_progress"] = li.get("levelProgress")

            friends = c.all_friends()
            res["friend_count"] = len(friends)

            for f in friends:
                fid = f.get("id")
                if not fid:
                    continue
                try:
                    d = c._json(c.ping(int(fid)))
                    if d and d.get("succ"):
                        res["ping_ok"] += 1
                    else:
                        res["ping_fail"] += 1
                except Exception:
                    res["ping_fail"] += 1
                time.sleep(delay)

            li2 = c.level_info()
            res["total_exp_after"] = li2.get("totalExperience")
            res["exp_gained"] = (res["total_exp_after"] or 0) - (res["total_exp_before"] or 0)
            res["level"] = li2.get("level")
            res["level_name"] = li2.get("levelName")
            res["level_progress"] = li2.get("levelProgress")
            res["ok"] = True
            return res
        except SessionDead as e:
            res["error"] = f"会话失效 ({e})"
            if attempt < retries:
                time.sleep(5)
        except Exception as e:
            res["error"] = f"执行异常: {e}"
            if attempt < retries:
                time.sleep(5)
    return res


# ---------------- 每日寻访加好友 ----------------

def load_meet_state(path):
    if os.path.exists(path):
        return json.load(open(path))
    return {"added": [], "seen": []}


def save_meet_state(path, st):
    json.dump(st, open(path, "w"))


def run_meet(c: Client, state_path: str, max_add: int = 0) -> dict:
    """每日寻访加好友:
    1. pick 尝试捞瓶 (消耗 2 理智; 理智不足/无可捞则跳过, 不算失败)
    2. 拉取全部捞瓶记录, 提取瓶主 userId
    3. 对未处理过的 userId 逐个 addFriend
    首次运行将历史记录记为基线 (不发送请求), 之后只加新捞到的
    """
    st = load_meet_state(state_path)
    added, seen = set(st.get("added", [])), set(st.get("seen", []))
    res = {"pick_ok": False, "pick_msg": "", "new_records": 0, "added": [], "skipped_base": 0}

    # 1) 尝试捞瓶
    d = c._json(c.pick_bottle())
    res["pick_msg"] = (d or {}).get("msg", "")[:40]

    # 2) 全量捞瓶记录
    records = c.pending_list()
    user_ids = []
    for rec in records:
        uid = rec.get("userId")
        if uid and str(uid) not in seen and str(uid) not in added:
            user_ids.append(int(uid))

    # 3) 首次基线: 只记录不加
    if not seen and not added:
        seen.update(str(u) for u in user_ids)
        seen.update(added)
        save_meet_state(state_path, {"added": sorted(added), "seen": sorted(seen)})
        res["skipped_base"] = len(user_ids)
        return res

    # 4) 逐个加好友
    for uid in user_ids:
        if max_add and len(res["added"]) >= max_add:
            break
        try:
            d = c._json(c.add_friend(uid))
            if d and d.get("succ"):
                added.add(str(uid))
                res["added"].append(uid)
            else:
                seen.add(str(uid))  # 失败也标记, 避免反复重试
        except Exception:
            seen.add(str(uid))
        time.sleep(1)
    res["new_records"] = len(user_ids)
    save_meet_state(state_path, {"added": sorted(added), "seen": sorted(seen)})
    return res


# ---------------- 飞书推送: 图片模式 / 卡片模式 ----------------

def _find_cjk_font():
    """跨平台找一个可用中文字体"""
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc",          # macOS
        "/System/Library/Fonts/Hiragino Sans GB.ttc",        # macOS
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # debian
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",           # wqy
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def render_report_image(results: list) -> bytes:
    """渲染深色签到报表 PNG"""
    from PIL import Image, ImageDraw, ImageFont
    W, PAD, ROW = 760, 36, 26
    font_cjk_path = _find_cjk_font()
    f_title = ImageFont.truetype(font_cjk_path, 26) if font_cjk_path else ImageFont.load_default()
    f_head = ImageFont.truetype(font_cjk_path, 17) if font_cjk_path else ImageFont.load_default()
    f_text = ImageFont.truetype(font_cjk_path, 16) if font_cjk_path else ImageFont.load_default()

    H = PAD * 2 + 56 + 24 + len(results) * (ROW * 3 + 14) + 20
    img = Image.new("RGB", (W, H), (16, 18, 24))
    dr = ImageDraw.Draw(img)
    GREEN, RED, WHITE, GREY, CYAN = (140, 220, 160), (235, 110, 110), (225, 228, 235), (120, 126, 138), (70, 200, 200)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_ok = all(r["ok"] for r in results)
    title = "SYNC PROJECT 每日签到" + (" · 成功" if all_ok else " · 异常")
    dr.rectangle([0, 0, W, 6], fill=CYAN if all_ok else RED)
    dr.text((PAD, 22), title, font=f_title, fill=WHITE)
    dr.text((W - PAD - f_head.getlength(now), 32), now, font=f_head, fill=GREY)

    y = PAD + 56
    for r in results:
        color = GREEN if r["ok"] else RED
        mark = "OK" if r["ok"] else "FAIL"
        dr.text((PAD, y), f"[{mark}]  {r['nickname']}", font=f_head, fill=color)
        y += ROW
        if r["ok"]:
            lines = [
                f"好友 {r['friend_count']}   打招呼 {r['ping_ok']}/{r['ping_ok'] + r['ping_fail']}",
                f"经验 +{r['exp_gained']}   总计 {r['total_exp_after']}",
                f"Lv.{r['level']} {r['level_name'] or ''}   进度 {(r.get('level_progress') or 0) * 100:.1f}%",
            ]
            for ln in lines:
                dr.text((PAD + 24, y), ln, font=f_text, fill=GREY)
                y += ROW - 4
        else:
            dr.text((PAD + 24, y), r["error"] or "未知错误", font=f_text, fill=RED)
            y += ROW - 4
            dr.text((PAD + 24, y), "请更新 SYNC_COOKIES", font=f_text, fill=GREY)
            y += ROW - 4
        y += 14

    buf = __import__("io").BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def feishu_tenant_token(app_id, app_secret):
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"token 获取失败: {d}")
    return d["tenant_access_token"]


def feishu_upload_image(token: str, png: bytes) -> str:
    r = requests.post("https://open.feishu.cn/open-apis/im/v1/images",
                      headers={"Authorization": f"Bearer {token}"},
                      data={"image_type": "message"},
                      files={"image": ("report.png", png, "image/png")}, timeout=20)
    d = r.json()
    key = (d.get("data") or {}).get("image_key")
    if not key:
        raise RuntimeError(f"图片上传失败: {d}")
    return key


def build_interactive_card(results: list) -> dict:
    now = datetime.now().strftime("%m-%d %H:%M")
    all_ok = all(r["ok"] for r in results)
    lines = []
    for r in results:
        if r["ok"]:
            gain = f"+{r['exp_gained']}" if r["exp_gained"] else "0 (配额用完)"
            prog = r.get("level_progress")
            prog_s = f"{prog * 100:.1f}%" if isinstance(prog, (int, float)) else "-"
            lines.append(
                f"**{r['nickname']}**  ·  Lv.{r['level']} {r['level_name'] or ''} ({prog_s})\n"
                f"好友 {r['friend_count']}  ·  打招呼 {r['ping_ok']}/{r['ping_ok'] + r['ping_fail']}"
                f"  ·  经验 {gain}  ·  总计 {r['total_exp_after']}")
        else:
            lines.append(f"**{r['nickname']}**  ·  失败\n{r['error'] or '未知错误'}\n"
                         f"请更新 SYNC_COOKIES")
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": ("SYNC PROJECT 每日签到 · 完成 " if all_ok else "SYNC PROJECT 每日签到 · 异常 ") + now},
                "template": "green" if all_ok else "red",
            },
            "elements": [
                {"tag": "markdown", "content": "\n\n".join(lines)},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": f"SYNC.PROJECT · {len(results)} account(s) · next run 09:00/21:00"}]},
            ],
        },
    }


def notify(results: list, meet: dict = None):
    webhook = os.environ.get("SYNC_FEISHU_WEBHOOK", "").strip()
    app_id = os.environ.get("SYNC_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("SYNC_FEISHU_APP_SECRET", "").strip()
    try:
        if app_id and app_secret:
            # 图片模式: 渲染报表 PNG -> 上传 -> image 消息
            token = feishu_tenant_token(app_id, app_secret)
            key = feishu_upload_image(token, render_report_image(results))
            payload = {"msg_type": "image", "content": {"image_key": key}}
        else:
            # 卡片模式: markdown 卡片 (零配置)
            payload = build_interactive_card(results)
        r = requests.post(webhook, json=payload, timeout=15)
        resp = r.json()
        if resp.get("code") == 0 or resp.get("StatusCode") == 0:
            print("[notify sent]")
        else:
            print(f"[notify failed] {resp}")
    except Exception as e:
        print(f"[notify failed] {e}")


def main():
    cookies = [c.strip() for c in os.environ.get("SYNC_COOKIES", "").split(",") if c.strip()]
    if not cookies:
        print("SYNC_COOKIES 未配置")
        sys.exit(1)
    delay = float(os.environ.get("SYNC_DELAY", "1.5"))
    retries = int(os.environ.get("SYNC_MAX_RETRIES", "3"))

    meet_enable = os.environ.get("SYNC_MEET_ENABLE", "true").lower() in ("1", "true", "yes")
    meet_state = os.environ.get("SYNC_MEET_STATE_FILE", "meet_state.json")
    meet_max = int(os.environ.get("SYNC_MEET_MAX_ADD", "0"))

    results = [run_account(ck, i, delay, retries) for i, ck in enumerate(cookies)]

    meet_res = None
    if meet_enable:
        try:
            mc = Client(cookies[0])
            meet_res = run_meet(mc, meet_state)
            print(f"[meet] 捞瓶: {meet_res['pick_msg'] or '跳过'} | "
                  f"新记录 {meet_res['new_records']} | 已加好友 {len(meet_res['added'])} | "
                  f"基线跳过 {meet_res['skipped_base']}")
        except SessionDead:
            print("[meet] 会话失效, 跳过寻访")
        except Exception as e:
            print(f"[meet] 异常: {e}")

    for r in results:
        if r["ok"]:
            print(f"[{r['nickname']}] 好友 {r['friend_count']} · "
                  f"打招呼 {r['ping_ok']}/{r['ping_ok'] + r['ping_fail']} · "
                  f"经验 +{r['exp_gained']} · Lv.{r['level']}")
        else:
            print(f"[{r['nickname']}] 失败: {r['error']}")

    notify(results, meet_res)
    if not all(r["ok"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
