"""
Gemini モデル監視スクリプト
GitHub Actions で毎日実行し、モデルの有効性と新バージョンをチェックする。
問題があれば Telegram に通知する。
"""

import os
import re
import json
import urllib.request
import urllib.error

# --- 監視対象モデル定義 ---
MONITORED_MODELS = {
    "flash": {
        "main": "gemini-2.5-flash",
        "fallbacks": ["gemini-2.0-flash", "gemini-2.5-pro"],
    }
}

# --- 環境変数 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("GEMINI_MONITOR_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("GEMINI_MONITOR_CHAT_ID", "")


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

    # モデル名から "models/" プレフィックスを除去して返す
    models = []
    for m in data.get("models", []):
        name = m.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        models.append(name)
    return models


def parse_version(model_name):
    """
    モデル名からバージョン番号を抽出する。
    例: gemini-2.5-flash -> (2, 5), gemini-3.0-pro -> (3, 0)
    パースできない場合は None を返す。
    """
    match = re.match(r"gemini-(\d+)\.(\d+)-", model_name)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return None


def detect_newer_versions(current_model, available_models):
    """
    現在のメインモデルより新しいバージョンがあるか検出する。
    同じサフィックス（flash, pro等）のモデルのみ比較対象。
    """
    current_ver = parse_version(current_model)
    if current_ver is None:
        return []

    # サフィックスを取得（例: "flash", "pro"）
    suffix_match = re.match(r"gemini-\d+\.\d+-(.+)", current_model)
    if not suffix_match:
        return []
    suffix = suffix_match.group(1)

    newer = []
    for model in available_models:
        # 同じサフィックスのモデルだけ比較
        if not model.endswith(f"-{suffix}"):
            continue
        ver = parse_version(model)
        if ver and ver > current_ver:
            newer.append(model)

    # バージョン順にソート（新しい方が先）
    newer.sort(key=lambda m: parse_version(m), reverse=True)
    return newer


def get_telegram_chat_id():
    """固定のchat_idを返す（環境変数から取得）"""
    if TELEGRAM_CHAT_ID:
        return int(TELEGRAM_CHAT_ID)
    print("警告: GEMINI_MONITOR_CHAT_ID が設定されていません")
    return None


def send_telegram_message(chat_id, text):
    """Telegram にメッセージを送信する"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"Telegram 通知送信成功")
            else:
                print(f"Telegram 通知送信失敗: {result}")
    except urllib.error.HTTPError as e:
        print(f"Telegram 送信エラー: {e.code} {e.reason}")


def main():
    """メイン処理"""
    if not GEMINI_API_KEY:
        print("エラー: GEMINI_API_KEY が設定されていません")
        exit(1)
    if not TELEGRAM_BOT_TOKEN:
        print("エラー: GEMINI_MONITOR_BOT_TOKEN が設定されていません")
        exit(1)
    if not TELEGRAM_CHAT_ID:
        print("エラー: GEMINI_MONITOR_CHAT_ID が設定されていません")
        exit(1)

    print("Gemini モデル一覧を取得中...")
    available = get_available_models()
    print(f"取得したモデル数: {len(available)}")

    # 通知メッセージを溜めるリスト
    alerts = []

    for category, config in MONITORED_MODELS.items():
        main_model = config["main"]
        fallbacks = config["fallbacks"]

        print(f"\n--- カテゴリ: {category} ---")
        print(f"メインモデル: {main_model}")

        # 1. メインモデルの有効性チェック
        if main_model not in available:
            # メインモデルが廃止された場合、フォールバックから次の候補を探す
            next_candidate = None
            for fb in fallbacks:
                if fb in available:
                    next_candidate = fb
                    break

            # 新しいバージョンがあればそちらを推奨
            suffix_match = re.match(r"gemini-\d+\.\d+-(.+)", main_model)
            suffix = suffix_match.group(1) if suffix_match else ""
            newer_available = [m for m in available if re.match(rf"gemini-\d+\.\d+-{re.escape(suffix)}$", m)]
            if newer_available:
                newer_available.sort(key=lambda m: parse_version(m) or (0, 0), reverse=True)
                next_candidate = newer_available[0]

            recommendation = f" shared-env を以下に更新してください:\nexport GEMINI_FLASH_MODEL={next_candidate}" if next_candidate else " 代替モデルが見つかりません。手動で確認してください。"
            alerts.append(f"🚨 {main_model} が廃止されました！{recommendation}")
            print(f"⚠️ メインモデル {main_model} が見つかりません！")
        else:
            print(f"✅ メインモデル {main_model} は有効です")

        # 2. 新バージョンの検出
        newer = detect_newer_versions(main_model, available)
        for new_model in newer:
            alerts.append(f"🆕 新バージョン検出: {new_model}（現在: {main_model}）")
            print(f"🆕 新バージョン発見: {new_model}")

        # 3. フォールバックモデルの有効性チェック
        for fb in fallbacks:
            if fb not in available:
                alerts.append(
                    f"🗑️ フォールバック {fb} が廃止。"
                    f"shared-env の GEMINI_FLASH_FALLBACKS から除去してください"
                )
                print(f"⚠️ フォールバック {fb} が見つかりません！")
            else:
                print(f"✅ フォールバック {fb} は有効です")

    # 通知の送信
    if alerts:
        print(f"\n{len(alerts)} 件の問題を検出しました。Telegram に通知します...")
        chat_id = get_telegram_chat_id()
        if chat_id:
            header = "<b>⚡ Gemini モデル監視レポート</b>\n\n"
            message = header + "\n\n".join(alerts)
            send_telegram_message(chat_id, message)
        else:
            print("エラー: Telegram のチャットIDを取得できませんでした")
            # チャットIDが取れなくても問題の内容はログに出力済みなので終了コードは0
    else:
        # 正常時もテスト通知を送る（--test フラグ or TEST_NOTIFY 環境変数）
        if os.environ.get("TEST_NOTIFY") or "--test" in __import__("sys").argv:
            chat_id = get_telegram_chat_id()
            if chat_id:
                send_telegram_message(chat_id, "✅ Geminiモデル監視テスト: 全モデル正常です。GitHub Actionsからの通知テスト成功。")
        print("\n✅ 全てのモデルが正常です。通知はスキップします。")


if __name__ == "__main__":
    main()
