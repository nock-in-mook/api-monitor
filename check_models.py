"""
API モデル監視スクリプト
GitHub Actions で毎日実行し、モデルの有効性と新バージョンをチェックする。
異常があれば詳細を、正常なら「正常」の一言を Telegram に通知する。
最後に Healthchecks.io に生存報告を送る。
"""

import os
import re
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# --- 設定ファイル読み込み ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(SCRIPT_DIR, "config.json"), encoding="utf-8") as f:
    CONFIG = json.load(f)

# --- 環境変数 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("GEMINI_MONITOR_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("GEMINI_MONITOR_CHAT_ID", "")
HEALTHCHECKS_PING_URL = os.environ.get("HEALTHCHECKS_PING_URL", "")

# --- 日本時間 ---
JST = timezone(timedelta(hours=9))


def get_available_models():
    """Gemini API の models.list を叩いて有効なモデル名一覧を取得する"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"API エラー: {e.code} {e.reason}")
        raise

    models = []
    for m in data.get("models", []):
        name = m.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        models.append(name)
    return models


def test_model_call(model_id):
    """モデルに実際にリクエストを送って動くか確認"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
    payload = json.dumps({"contents": [{"parts": [{"text": "OK"}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return True  # 503等は一時的、モデル自体は存在
    except Exception:
        return True  # ネットワークエラーは判断保留


def parse_version(model_name):
    """モデル名からバージョン番号を抽出する"""
    match = re.match(r"gemini-(\d+)\.(\d+)-", model_name)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return None


def detect_newer_versions(current_model, available_models):
    """現在のメインモデルより新しいバージョンがあるか検出する"""
    current_ver = parse_version(current_model)
    if current_ver is None:
        return []

    suffix_match = re.match(r"gemini-\d+\.\d+-(.+)", current_model)
    if not suffix_match:
        return []
    suffix = suffix_match.group(1)

    newer = []
    for model in available_models:
        if not model.endswith(f"-{suffix}"):
            continue
        ver = parse_version(model)
        if ver and ver > current_ver:
            newer.append(model)

    newer.sort(key=lambda m: parse_version(m), reverse=True)
    return newer


def send_telegram_message(text):
    """Telegram にメッセージを送信する"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print("Telegram 通知送信成功")
            else:
                print(f"Telegram 通知送信失敗: {result}")
    except urllib.error.HTTPError as e:
        print(f"Telegram 送信エラー: {e.code} {e.reason}")


def ping_healthchecks(status="success"):
    """Healthchecks.io に生存報告を送る"""
    if not HEALTHCHECKS_PING_URL:
        print("Healthchecks.io URL未設定、スキップ")
        return

    url = HEALTHCHECKS_PING_URL
    if status == "fail":
        url += "/fail"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Healthchecks.io ping 送信成功 ({status})")
    except Exception as e:
        print(f"Healthchecks.io ping 失敗: {e}")


def check_gemini_models():
    """Gemini モデルをチェックし、アラートのリストを返す"""
    print("Gemini モデル一覧を取得中...")
    available = get_available_models()
    print(f"取得したモデル数: {len(available)}")

    alerts = []
    # 監視対象モデルを config.json から収集（重複除去）
    monitored = {}
    for app_name, app_config in CONFIG["apps"].items():
        if app_config["api"] != "gemini":
            continue
        models = app_config["models"]
        main_model = models["main"]
        fallback = models.get("fallback")

        if main_model not in monitored:
            monitored[main_model] = {"apps": [], "fallbacks": {}}
        monitored[main_model]["apps"].append(app_name)
        if fallback:
            monitored[main_model]["fallbacks"][fallback] = \
                monitored[main_model]["fallbacks"].get(fallback, [])
            monitored[main_model]["fallbacks"][fallback].append(app_name)

    for model, info in monitored.items():
        apps = info["apps"]
        apps_str = ", ".join(apps)

        print(f"\n--- {model} (使用: {apps_str}) ---")

        # メインモデルの有効性チェック
        works = model in available and test_model_call(model)
        if not works:
            alerts.append(
                f"🚨 <b>{model}</b> が廃止されました！\n"
                f"   影響アプリ: {apps_str}"
            )
            print(f"⚠️ {model} が利用不可！")
        else:
            print(f"✅ {model} は有効")

        # 新バージョンの検出
        newer = detect_newer_versions(model, available)
        for new_model in newer:
            alerts.append(
                f"🆕 新バージョン検出: <b>{new_model}</b>\n"
                f"   現在: {model} → 影響アプリ: {apps_str}"
            )
            print(f"🆕 新バージョン発見: {new_model}")

        # フォールバックのチェック
        for fallback, fb_apps in info["fallbacks"].items():
            fb_apps_str = ", ".join(fb_apps)
            if fallback not in available or not test_model_call(fallback):
                alerts.append(
                    f"🗑️ フォールバック <b>{fallback}</b> が廃止\n"
                    f"   影響アプリ: {fb_apps_str}"
                )
                print(f"⚠️ フォールバック {fallback} は利用不可！")
            else:
                print(f"✅ フォールバック {fallback} は有効")

    return alerts


def main():
    """メイン処理"""
    # 環境変数チェック
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("GEMINI_MONITOR_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("GEMINI_MONITOR_CHAT_ID")
    if missing:
        print(f"エラー: 環境変数未設定: {', '.join(missing)}")
        exit(1)

    now = datetime.now(JST).strftime("%m/%d %H:%M")

    try:
        alerts = check_gemini_models()
    except Exception as e:
        # API自体にアクセスできない場合
        error_msg = f"❌ <b>API監視エラー</b> ({now})\n\n{e}"
        send_telegram_message(error_msg)
        ping_healthchecks("fail")
        raise

    if alerts:
        # 異常あり → 詳細通知
        header = f"⚡ <b>API監視レポート</b> ({now})\n\n"
        message = header + "\n\n".join(alerts)
        send_telegram_message(message)
        print(f"\n{len(alerts)} 件の問題を検出")
    else:
        # 正常 → シンプルな一言通知
        send_telegram_message(f"✅ API監視 正常 ({now})")
        print("\n✅ 全てのモデルが正常")

    # 生存報告
    ping_healthchecks("success" if not alerts else "fail")


if __name__ == "__main__":
    main()
