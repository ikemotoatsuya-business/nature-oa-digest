#!/usr/bin/env python3
"""
Nature 系列誌 オープンアクセス記事 日次ダイジェスト

処理の流れ:
  1. feeds.txt の各RSSを取得
  2. 既処理(seen.json)と照合して新着のみ抽出
  3. Unpaywall でオープンアクセス(OA)判定 → OAのみ残す
  4. Gemini で日本語の要約/翻訳を生成（プロンプトは prompts/ から読み込み）
  5. output/YYYY-MM-DD.md に書き出し、seen.json を更新

設定はすべて環境変数で行う（README参照）。プロンプトを差し替えても
コードは変更不要。ファクトチェック等の追加処理もここに足せる構造。
"""

import os
import re
import sys
import json
import time
import datetime
import pathlib

import requests
import feedparser

# ----------------------------------------------------------------------
# パス設定
# ----------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.txt"
STATE_FILE = ROOT / "state" / "seen.json"
OUTPUT_DIR = ROOT / "output"
PROMPT_DIR = ROOT / "prompts"

# ----------------------------------------------------------------------
# 環境変数（設定）
# ----------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()  # Unpaywall 必須

# 1回の実行で処理する最大件数（無料枠を超えないための安全弁）
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "40"))
# API呼び出し間の待機秒数（Gemini無料枠 ~10-15 RPM 対策）
REQUEST_DELAY_SEC = float(os.environ.get("REQUEST_DELAY_SEC", "5"))
# OA以外もダイジェストに含めるか（既定: OAのみ）
OA_ONLY = os.environ.get("OA_ONLY", "true").lower() != "false"
# 本文(OA全文)を取得して要約に使うか（既定: 有効。失敗時は要旨にフォールバック）
FETCH_FULLTEXT = os.environ.get("FETCH_FULLTEXT", "true").lower() != "false"
# 本文をLLMに渡す際の最大文字数（無料枠保護のための上限。超過分は切り捨て）
MAX_FULLTEXT_CHARS = int(os.environ.get("MAX_FULLTEXT_CHARS", "20000"))
# 全文取得時のUser-Agent（礼儀として連絡先を入れておく）
USER_AGENT = os.environ.get(
    "USER_AGENT", "nature-oa-digest/1.0 (mailto:{})".format(
        os.environ.get("CONTACT_EMAIL", "anonymous@example.com")
    )
)
# 初回シードモード: 現時点の全記事を「既読」にするだけ（要約せず終了）。
# 初回実行で大量処理して無料枠を使い切らないための仕組み。
SEED_ONLY = os.environ.get("SEED_ONLY", "false").lower() == "true"

UNPAYWALL_ENDPOINT = "https://api.unpaywall.org/v2/{doi}"


# ----------------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_feeds() -> list[str]:
    urls = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            log("seen.json が壊れていたため空で開始します")
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8"
    )


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# RSS 解析
# ----------------------------------------------------------------------
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")


def extract_doi(entry) -> str | None:
    """Nature RSS の各エントリから DOI を頑健に取り出す。"""
    # 1) prism:doi
    doi = entry.get("prism_doi") or entry.get("doi")
    if doi:
        return doi.strip().lower()
    # 2) dc:identifier 形式 "doi:10.1038/..."
    ident = entry.get("dc_identifier") or entry.get("id") or ""
    m = DOI_RE.search(ident)
    if m:
        return m.group(0).lower()
    # 3) 本文/リンクから抽出（Nature の記事URLは /articles/<id> で DOI は 10.1038/<id>）
    link = entry.get("link", "") or ""
    m = re.search(r"/articles/([^/?#]+)", link)
    if m:
        return f"10.1038/{m.group(1)}".lower()
    # 4) summary 中の DOI 文字列
    summary = entry.get("summary", "") or ""
    m = DOI_RE.search(summary)
    if m:
        return m.group(0).lower()
    return None


def clean_abstract(text: str) -> str:
    """RSS の description から HTML と定型の前置き(Published online...; doi...)を除去。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"Published online:.*?doi:\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_feed(url: str) -> list[dict]:
    """1フィードを解析し、記事のリストを返す。"""
    parsed = feedparser.parse(url)
    journal = (parsed.feed.get("title") or url).strip()
    items = []
    for e in parsed.entries:
        doi = extract_doi(e)
        if not doi:
            continue
        items.append(
            {
                "doi": doi,
                "title": (e.get("title") or "").strip(),
                "link": e.get("link", ""),
                "journal": journal,
                "authors": ", ".join(
                    a.get("name", "") for a in e.get("authors", []) if a.get("name")
                ),
                "published": e.get("published", "") or e.get("updated", ""),
                "abstract": clean_abstract(e.get("summary", "")),
            }
        )
    return items


# ----------------------------------------------------------------------
# オープンアクセス判定 (Unpaywall)
# ----------------------------------------------------------------------
def check_open_access(doi: str) -> tuple[bool, str | None]:
    """(is_oa, oa_pdf_or_url) を返す。判定不能時は (False, None)。"""
    if not CONTACT_EMAIL:
        raise RuntimeError("CONTACT_EMAIL が未設定です（Unpaywall に必須）")
    try:
        r = requests.get(
            UNPAYWALL_ENDPOINT.format(doi=doi),
            params={"email": CONTACT_EMAIL},
            timeout=20,
        )
        if r.status_code != 200:
            log(f"  Unpaywall {r.status_code}: {doi}")
            return False, None
        data = r.json()
        if not data.get("is_oa"):
            return False, None
        loc = data.get("best_oa_location") or {}
        return True, (loc.get("url_for_pdf") or loc.get("url"))
    except requests.RequestException as exc:
        log(f"  Unpaywall 通信エラー {doi}: {exc}")
        return False, None


# ----------------------------------------------------------------------
# 本文(OA全文)の取得
# ----------------------------------------------------------------------
def _extract_pdf_text(content: bytes) -> str:
    import io
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(texts)


def _extract_html_text(html: str) -> str:
    import trafilatura

    return trafilatura.extract(html) or ""


def fetch_fulltext(url: str) -> str | None:
    """OAのPDF/HTMLから本文テキストを取得。失敗時は None。"""
    if not url:
        return None
    try:
        r = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=40, allow_redirects=True
        )
        if r.status_code != 200:
            log(f"  本文取得 {r.status_code}: {url}")
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "pdf" in ctype or r.content[:5] == b"%PDF-":
            text = _extract_pdf_text(r.content)
        else:
            text = _extract_html_text(r.text)
        text = re.sub(r"\s+\n", "\n", text or "").strip()
        if len(text) < 200:  # 抽出失敗とみなす閾値
            return None
        if len(text) > MAX_FULLTEXT_CHARS:
            text = text[:MAX_FULLTEXT_CHARS] + "\n…（以下、文字数上限により省略）"
        return text
    except Exception as exc:  # ネットワーク/抽出いずれの失敗も握る
        log(f"  本文取得エラー {url}: {exc}")
        return None


# ----------------------------------------------------------------------
# LLM (Gemini)
# ----------------------------------------------------------------------
def make_gemini_caller():
    """Gemini クライアントを1つ作って返す。SDK未導入/キー無しなら None。"""
    if not GEMINI_API_KEY:
        log("GEMINI_API_KEY 未設定: LLM処理をスキップします")
        return None
    from google import genai  # google-genai

    client = genai.Client(api_key=GEMINI_API_KEY)

    def call(prompt: str) -> str:
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return (resp.text or "").strip()

    return call


def build_section(call, item: dict) -> str:
    """1記事分の Markdown セクションを生成。"""
    body_source = item.get("body_source", "要旨")
    header = (
        f"### {item['title']}\n\n"
        f"- **誌名**: {item['journal']}\n"
        f"- **著者**: {item['authors'] or '—'}\n"
        f"- **公開**: {item['published'] or '—'}\n"
        f"- **DOI**: [{item['doi']}](https://doi.org/{item['doi']})\n"
        f"- **本文(OA)**: {item.get('oa_url') or item['link']}\n"
        f"- **要約の元**: {body_source}\n\n"
    )

    # 要約に渡すテキスト: 本文があれば本文、無ければ要旨
    content = item.get("body") or item["abstract"]

    if call is None or not content:
        body = f"> {content or '(本文・要旨なし)'}\n"
        return header + body + "\n---\n\n"

    try:
        summary = call(
            load_prompt("summarize.txt").format(
                title=item["title"], journal=item["journal"], body=content
            )
        )
    except Exception as exc:  # 1記事の失敗で全体を止めない
        log(f"  要約生成エラー: {exc}")
        summary = "(要約生成に失敗しました)"

    # 翻訳は要旨のみ対象（本文全訳はコストが高いため）
    translation = ""
    if item["abstract"]:
        try:
            translation = call(
                load_prompt("translate.txt").format(abstract=item["abstract"])
            )
        except Exception as exc:
            log(f"  翻訳生成エラー: {exc}")
            translation = "(翻訳生成に失敗しました)"

    parts = [header, f"#### 要約・解釈（{body_source}より）\n\n{summary}\n\n"]
    if translation:
        parts.append(f"#### 要旨（日本語訳）\n\n{translation}\n\n")
    if item["abstract"]:
        parts.append(
            f"<details><summary>原文要旨 (English)</summary>\n\n{item['abstract']}\n\n</details>\n"
        )
    parts.append("\n---\n\n")
    return "".join(parts)


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def main() -> int:
    feeds = load_feeds()
    seen = load_seen()
    log(f"フィード {len(feeds)} 件 / 既読 {len(seen)} 件")

    # 1) 全フィードから新着を収集
    new_items = []
    for url in feeds:
        items = parse_feed(url)
        fresh = [it for it in items if it["doi"] not in seen]
        log(f"  {url} -> {len(items)} 件中 新着 {len(fresh)} 件")
        new_items.extend(fresh)

    # 重複DOIを除去（複数フィードに同一記事が出る場合）
    uniq = {it["doi"]: it for it in new_items}
    new_items = list(uniq.values())
    log(f"新着(重複除去後): {len(new_items)} 件")

    # 2) シードモード: 要約せず既読化して終了（初回の無料枠保護）
    if SEED_ONLY:
        for it in new_items:
            seen.add(it["doi"])
        save_seen(seen)
        log(f"SEED_ONLY: {len(new_items)} 件を既読化して終了")
        return 0

    # 3) OA判定（このタイミングで上限を適用）
    call = make_gemini_caller()
    processed = []
    for it in new_items:
        if len(processed) >= MAX_ITEMS_PER_RUN:
            log(f"MAX_ITEMS_PER_RUN={MAX_ITEMS_PER_RUN} に到達。残りは次回へ")
            break

        is_oa, oa_url = check_open_access(it["doi"])
        time.sleep(1)  # Unpaywall への配慮
        if OA_ONLY and not is_oa:
            seen.add(it["doi"])  # 非OAは既読化して再判定を避ける
            continue

        it["oa_url"] = oa_url
        # 本文(OA全文)を取得。失敗時は要旨にフォールバック。
        if FETCH_FULLTEXT and oa_url:
            full = fetch_fulltext(oa_url)
            time.sleep(2)  # 取得先サーバーへの配慮
            if full:
                it["body"] = full
                it["body_source"] = "本文"
            else:
                it["body"] = it["abstract"]
                it["body_source"] = "要旨（本文取得失敗）"
        else:
            it["body"] = it["abstract"]
            it["body_source"] = "要旨"

        processed.append(it)
        seen.add(it["doi"])
        if call is not None:
            time.sleep(REQUEST_DELAY_SEC)  # Gemini レート制限対策

    # 4) Markdown 出力
    if processed:
        today = datetime.date.today().isoformat()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"{today}.md"
        parts = [f"# Nature系列 OA新着ダイジェスト — {today}\n",
                 f"対象 {len(feeds)} 誌 / 本日のOA新着 {len(processed)} 件\n\n---\n\n"]
        for it in processed:
            parts.append(build_section(call, it))
        out_path.write_text("".join(parts), encoding="utf-8")
        log(f"書き出し: {out_path} ({len(processed)} 件)")
    else:
        log("本日処理対象のOA新着はありませんでした")

    # 5) 状態保存
    save_seen(seen)
    log(f"完了。既読 {len(seen)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
