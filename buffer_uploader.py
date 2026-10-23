#!/usr/bin/env python3
"""
Buffer Reel Uploader (BK GrowUp)
--------------------------------
Bot jo Shorts/reel YouTube par daalta hai, wahi reel Buffer par bhi jati hai.

Do hisse:
1. queue_reel(video_path, caption)  -> bot (run_shorts_pipeline) isse reel ko reels/ folder me rakhta hai.
2. python buffer_uploader.py        -> workflow ka step: reels/ ki nayi reels Buffer API se post karta hai.

Buffer file upload nahi leta, sirf public URL leta hai. Repo public hai, isliye
reels/reel_X.mp4 -> https://raw.githubusercontent.com/<owner>/<repo>/<branch>/reels/reel_X.mp4

Purani reels (7 din se zyada) apne aap delete ho jati hain, taaki repo bhari na ho.
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests

API_URL = "https://api.buffer.com"
REELS_DIR = Path("reels")
STATE_FILE = Path("uploaded.json")

KEEP_DAYS = 7
MAX_FILE_MB = 95  # GitHub 100MB se badi file push nahi hone deta
MAX_CAPTION = 2000  # Instagram caption limit 2200

MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id }
    }
    ... on MutationError {
      message
    }
  }
}
"""


def _cfg():
    return {
        "api_key": os.environ.get("BUFFER_API_KEY", "").strip(),
        "ig": os.environ.get("BUFFER_INSTAGRAM_CHANNEL_ID", "").strip(),
        "others": [c.strip() for c in os.environ.get("BUFFER_OTHER_CHANNEL_IDS", "").split(",") if c.strip()],
        "mode": os.environ.get("BUFFER_MODE", "").strip() or "addToQueue",
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "branch": os.environ.get("GITHUB_REF_NAME", "main") or "main",
    }


def send_telegram(message):
    print(f"📨 {message}")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": message[:4000]}, timeout=15)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")


# ------------------------------------------------------------------
# Part 1: bot se call hota hai
# ------------------------------------------------------------------
def queue_reel(video_path, caption):
    """Reel ko reels/ me rakhta hai (workflow baad me commit + Buffer par post karega).
    BUFFER_API_KEY set na ho to kuch nahi karta."""
    if not os.environ.get("BUFFER_API_KEY", "").strip():
        send_telegram("⚠️ BUFFER_API_KEY secret nahi mila (ya workflow ke 'Run Job Bot' step me pass nahi hua), isliye reel Buffer ke liye queue nahi hui.")
        return None
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        send_telegram(f"⚠️ Reel {size_mb:.0f}MB ki hai (limit {MAX_FILE_MB}MB), Buffer par nahi bheji ja sakti.")
        return None
    REELS_DIR.mkdir(exist_ok=True)
    name = f"reel_{int(time.time())}.mp4"
    shutil.copyfile(video_path, REELS_DIR / name)
    (REELS_DIR / name).with_suffix(".txt").write_text(caption[:MAX_CAPTION].strip(), encoding="utf-8")
    print(f"📥 Reel Buffer ke liye queue me: reels/{name}")
    send_telegram("📥 Reel Buffer ke liye queue me rakh di, ab workflow ke agle steps Buffer par bhejenge.")
    return name


# ------------------------------------------------------------------
# Part 2: workflow step
# ------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def wait_until_public(url, tries=12, delay=10):
    for i in range(tries):
        try:
            r = requests.get(url, headers={"Range": "bytes=0-1"}, timeout=30, stream=True)
            r.close()
            if r.status_code in (200, 206):
                return True
            print(f"  URL abhi ready nahi (HTTP {r.status_code}), retry {i + 1}/{tries}")
        except requests.RequestException as e:
            print(f"  URL check error: {e}")
        time.sleep(delay)
    return False


def _gql(api_key, query):
    """Seedhi GraphQL query chalata hai; data dict lautata hai, warna exception."""
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"query": query},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], ensure_ascii=False)[:400])
    return body.get("data") or {}


def discover_channels(api_key):
    """API key se hi sab channels nikalta hai: [(channel_id, service, naam), ...]"""
    data = _gql(api_key, "query { account { organizations { id name } } }")
    orgs = (data.get("account") or {}).get("organizations") or []
    found = []
    for org in orgs:
        q = ("query { channels(input: {organizationId: %s}) "
             "{ id name displayName service } }") % json.dumps(org["id"])
        for ch in (_gql(api_key, q).get("channels") or []):
            label = ch.get("displayName") or ch.get("name") or ch["id"]
            found.append((ch["id"], str(ch.get("service") or "").lower(), label))
    return found


def pick_channels(cfg):
    """Kaun-kaun se channels par post karni hai: [(id, is_instagram, naam)]. Sirf API key kaafi hai."""
    chosen = []
    if cfg["ig"]:  # purana secret ho to wahi use hoga
        chosen.append((cfg["ig"], True, "Instagram"))
    else:
        for cid, service, label in discover_channels(cfg["api_key"]):
            if service == "instagram":
                chosen.append((cid, True, f"Instagram ({label})"))
    for cid in cfg["others"]:
        chosen.append((cid, False, "channel"))
    return chosen


def create_post(api_key, mode, channel_id, video_url, caption, is_instagram):
    post_input = {
        "text": caption,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": mode,
        "assets": [{"video": {"url": video_url}}],
    }
    if is_instagram:
        post_input["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": MUTATION, "variables": {"input": post_input}},
            timeout=60,
        )
    except requests.RequestException as e:
        return False, f"network error: {e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    if data.get("errors"):
        return False, json.dumps(data["errors"], ensure_ascii=False)[:400]
    result = (data.get("data") or {}).get("createPost") or {}
    if result.get("post"):
        return True, result["post"].get("id", "")
    return False, result.get("message", "unknown error")


def cleanup_old(state):
    """7 din se purani reels (naam me unix time hai) delete + state se hatao."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in list(REELS_DIR.glob("reel_*.mp4")):
        m = re.match(r"reel_(\d+)\.mp4$", f.name)
        if m and int(m.group(1)) < cutoff:
            f.unlink(missing_ok=True)
            f.with_suffix(".txt").unlink(missing_ok=True)
            state.pop(f.name, None)
            print(f"🧹 Purani reel hata di: {f.name}")


def main():
    cfg = _cfg()
    REELS_DIR.mkdir(exist_ok=True)
    state = load_state()
    original_state = json.dumps(state, sort_keys=True)

    # "ALL" = us reel ke saare channels par post ho chuki
    pending = [f for f in sorted(REELS_DIR.glob("reel_*.mp4")) if "ALL" not in state.get(f.name, [])]

    channels = []
    if not pending:
        print("Buffer ke liye koi nayi reel nahi.")
    elif not cfg["api_key"]:
        send_telegram("⚠️ BUFFER_API_KEY secret set nahi hai, reel Buffer par nahi gayi.")
        pending = []
    else:
        try:
            channels = pick_channels(cfg)
        except Exception as e:
            send_telegram(f"❌ Buffer se channels nahi mil paye (API key sahi hai?):\n{e}")
            pending = []
        else:
            if not channels:
                send_telegram("⚠️ Buffer account me koi Instagram channel nahi mila. Buffer me Instagram connect hai?")
                pending = []
            else:
                print("Channels: " + ", ".join(name for _i, _g, name in channels))

    for f in pending:
        caption_file = f.with_suffix(".txt")
        caption = caption_file.read_text(encoding="utf-8").strip() if caption_file.exists() else "New reel"
        url = f"https://raw.githubusercontent.com/{cfg['repo']}/{cfg['branch']}/reels/{f.name}"
        print(f"Buffer upload: {f.name}")
        if not wait_until_public(url):
            send_telegram(f"❌ Buffer: reel ka public URL ready nahi hua:\n{url}")
            continue
        done = [c for c in state.get(f.name, []) if c != "ALL"]
        for channel_id, is_ig, name in channels:
            if channel_id in done:
                continue
            ok, info = create_post(cfg["api_key"], cfg["mode"], channel_id, url, caption, is_ig)
            if ok:
                done.append(channel_id)
                send_telegram(f"✅ Reel Buffer me {'queue' if cfg['mode'] == 'addToQueue' else 'post'} ho gayi: {name}")
            else:
                send_telegram(f"❌ Buffer post fail ({name}):\n{info}")
        if all(cid in done for cid, _g, _n in channels):
            done.append("ALL")
        state[f.name] = done
        save_state(state)

    cleanup_old(state)
    if json.dumps(state, sort_keys=True) != original_state:
        save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        send_telegram(f"❌ Buffer uploader crash: {e}")
        sys.exit(0)  # Buffer ki galti se poora job fail na ho
