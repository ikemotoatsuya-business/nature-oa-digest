#!/usr/bin/env python3
"""
output/<today>.md を Slack に転送する。

前提:
  - daily.yml の main.py 実行ステップの「後」に呼ぶ
  - SLACK_WEBHOOK_URL を環境変数(GitHub Secret)で渡す
使い方:
  python src/notify_slack.py                # 本日分 output/YYYY-MM-DD.md を送る
  python src/notify_slack.py output/2026-06-22.md   # ファイル指定も可
"""
import os
import sys
import datetime
import pathlib

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

# Slack は1メッセージが長すぎると切れる/弾かれるため、この文字数で分割する
CHUNK = 3500


def post(text: str) -> None:
    res = requests.post(WEBHOOK, json={"text": text}, timeout=15)
    res.raise_for_status()


def split_by_lines(body: str, limit: int) -> list[str]:
    """行の途中で切らないよう、行単位で limit 文字以下のかたまりに分割する。"""
    chunks, cur = [], ""
    for line in body.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks


def main() -> int:
    if not WEBHOOK:
        print("SLACK_WEBHOOK_URL が未設定です。送信を中止します。")
        return 1

    # 引数があればそのファイル、無ければ本日分を対象にする
    if len(sys.argv) > 1:
        md_path = pathlib.Path(sys.argv[1])
    else:
        today = datetime.date.today().isoformat()
        md_path = OUTPUT_DIR / f"{today}.md"

    # 新着OAが無い日はファイルが作られない。その場合は静かに正常終了。
    if not md_path.exists():
        print(f"本日のダイジェストはありません（{md_path}）。送信しません。")
        return 0

    content = md_path.read_text(encoding="utf-8").strip()
    if not content:
        print("中身が空のため送信しません。")
        return 0

    chunks = split_by_lines(content, CHUNK)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        label = f"（{i}/{total}）" if total > 1 else ""
        # コードブロックで囲む → Slack上でホバーすると「コピー」ボタンが出る
        post(f"📄 *{md_path.name}* {label}\n```{chunk}```")
    print(f"Slack送信完了: {md_path.name}（{total}通）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
