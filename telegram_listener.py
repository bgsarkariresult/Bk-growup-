"""
Telegram Listener — har 5 minute me Telegram bot check karta hai.
Agar koi naya link (URL) message aaya ho, to woh GitHub ke 'Run Job Bot'
workflow ko automatically trigger kar deta hai (repository_dispatch se).
Ye script khud GitHub Actions cron se chalti hai — koi alag server nahi chahiye.
"""
import os
import re
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GH_PAT = os.environ["GH_PAT"]
REPO = os.environ["GITHUB_REPOSITORY"]  # "owner/repo" format, GitHub khud deta hai
OFFSET_FILE = "telegram_offset.txt"
URL_PATTERN = re.compile(r"https?://\S+")


def get_offset():
    if os.path.exists(OFFSET_FILE):
        content = open(OFFSET_FILE).read().strip()
        if content:
            return int(content)
    return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"⚠️ Telegram message bhejne me error: {e}")


def trigger_workflow(url):
    api = f"https://api.github.com/repos/{REPO}/dispatches"
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github+json",
    }
    payload = {"event_type": "run_job_bot", "client_payload": {"post_url": url}}
    r = requests.post(api, headers=headers, json=payload, timeout=15)
    return r.status_code == 204


def main():
    offset = get_offset()
    resp = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"offset": offset + 1, "timeout": 5},
        timeout=20,
    ).json()

    updates = resp.get("result", [])
    max_id = offset

    for u in updates:
        max_id = max(max_id, u["update_id"])
        text = u.get("message", {}).get("text", "") or ""
        match = URL_PATTERN.search(text)
        if not match:
            continue

        link = match.group(0)
        print(f"🔗 Naya link mila: {link}")
        if trigger_workflow(link):
            send_message(f"✅ Job shuru kar diya:\n{link}")
        else:
            send_message(f"❌ Workflow trigger nahi ho paya:\n{link}")

    if max_id != offset:
        save_offset(max_id)


if __name__ == "__main__":
    main()
