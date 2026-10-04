"""
毎朝、世界の政治・経済・金融・IT/AIの重要ニュース5選を
LINE Flexメッセージで配信し、各ニュースの「日本語の詳細ページ」を
GitHub Pagesに自動生成するスクリプト。

使い方(GitHub Actionsから順番に呼ぶ):
  python news_bot.py build   # 収集→選定→詳細ページ生成(docs/ と out/news.json を作る)
  python news_bot.py send    # out/news.json を読んでLINEにFlexメッセージを送る
  python news_bot.py error   # エラー発生時の通知テキストをLINEに送る

環境変数:
- GEMINI_API_KEY (build で必要)
- LINE_CHANNEL_ACCESS_TOKEN (send / error で必要)
- GEMINI_MODEL (任意。未指定なら DEFAULT_GEMINI_MODEL)
- PAGES_BASE_URL (任意。未指定なら GITHUB_REPOSITORY から https://<owner>.github.io/<repo> を組み立てる)
"""

import os
import re
import sys
import time
import json
import html
import pathlib
import datetime
import feedparser
import requests

# gemini-2.0-flash は提供終了済み、gemini-2.5-* も2026年10月16日終了予定のため
# 無料枠で使えるFlash-Lite系を既定にしている。動かなければ AI Studio で現行名を確認し、
# GitHubの Variables に GEMINI_MODEL を登録して差し替える。
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"

RSS_FEEDS = [
    ("NHKニュース(主要)", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
    ("BBC World News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("ITmedia AI+", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml"),
]

MAX_ITEMS_PER_FEED = 10
ARTICLE_TEXT_LIMIT = 5000  # 要約のために読み込む本文の最大文字数(1記事あたり)

JST = datetime.timezone(datetime.timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

DOCS_DIR = pathlib.Path("docs")
OUT_DIR = pathlib.Path("out")
NEWS_JSON = OUT_DIR / "news.json"

# カードの左側に出す丸数字の背景色(1〜5番目で色を変える)
ACCENT_COLORS = ["#4A6FA5", "#5C8374", "#B08968", "#8E6C88", "#4A6FA5"]

CARD_TITLE = "Today's Top 5 Stories"


# ---------------------------------------------------------------
# ニュース収集
# ---------------------------------------------------------------
def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def collect_headlines():
    items = []
    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] {source_name} の取得に失敗: {e}", file=sys.stderr)
            continue

        entries = feed.entries[:MAX_ITEMS_PER_FEED]
        print(f"[INFO] {source_name}: {len(entries)}件取得")

        for entry in entries:
            title = strip_html(getattr(entry, "title", ""))
            summary = strip_html(getattr(entry, "summary", ""))
            link = getattr(entry, "link", "").strip()
            if not title or not link:
                continue
            items.append(
                {
                    "id": len(items),
                    "source": source_name,
                    "title": title,
                    "summary": summary,
                    "link": link,
                }
            )

    if not items:
        raise RuntimeError("どのRSSフィードからもニュースを取得できませんでした")
    return items


def fetch_article_text(url):
    """元記事の本文をできる範囲で取得する(有料記事や取得拒否の場合は空文字)"""
    try:
        import trafilatura

        res = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; news-summary-bot)"},
            timeout=15,
        )
        if res.status_code != 200:
            return ""
        text = trafilatura.extract(res.text) or ""
        return text[:ARTICLE_TEXT_LIMIT]
    except Exception as e:
        print(f"[WARN] 本文取得に失敗({url}): {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------
def parse_json_list(raw_text):
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        cleaned = cleaned.strip()
    data = json.loads(cleaned)
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Geminiの出力が想定したJSON配列ではありません: {raw_text[:200]}")
    return data


def gemini_json(prompt):
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": os.environ["GEMINI_API_KEY"],
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    # 503(混雑)・429(無料枠の一時的な上限)・5xx・通信エラーは一時的なことが多いので、待ってやり直す
    retry_statuses = {429, 500, 502, 503, 504}
    waits = [10, 20, 40, 60]  # 最大5回試行、合計で最長およそ2分半待つ
    res = None
    for attempt in range(len(waits) + 1):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=120)
            if res.status_code == 200 or res.status_code not in retry_statuses:
                break
            reason = f"HTTP {res.status_code}"
        except (requests.ConnectionError, requests.Timeout) as e:
            res = None
            reason = f"通信エラー: {e}"
        if attempt < len(waits):
            print(f"[WARN] Geminiが一時的に使えません({reason})。{waits[attempt]}秒待って再試行します({attempt + 1}/{len(waits)})", file=sys.stderr)
            time.sleep(waits[attempt])

    if res is None:
        raise RuntimeError(f"Gemini APIに接続できませんでした(model={model})")
    if res.status_code != 200:
        # 404ならモデル名の提供終了、429が続くなら無料枠の上限超過の可能性が高い
        raise RuntimeError(f"Gemini APIエラー(model={model}): {res.status_code} {res.text[:500]}")
    data = res.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Geminiの応答解析に失敗しました: {data}") from e
    return parse_json_list(raw_text)


def select_news(headlines):
    """見出し一覧から5件を選び、日本語タイトルとあらすじを付ける"""
    lines = []
    for h in headlines:
        lines.append(f"[{h['id']}] ({h['source']}) {h['title']}\n概要: {h['summary'][:200]}")
    headlines_text = "\n\n".join(lines)

    prompt = f"""以下は、本日時点での複数のニュースソースの見出し一覧です。
各見出しの先頭の[ ]内の数字がIDです。

この中から、政治・経済・金融・IT・AIの分野で特に重要と考えられるニュースを5つ選んでください。
選ぶ際は、政治、経済・金融、IT・AIの3分野からバランスよく選んでください。目安は、政治1〜2件、経済・金融1〜2件、IT・AI1〜2件で、合計5件です。どれか1分野だけに偏らないようにしてください。ただし、入力に該当分野の見出しが少ない場合は、他の分野で補って構いません。
同じ出来事を扱う記事は1つにまとめ、重複して選ばないでください。

出力は、説明文やMarkdownを一切含めず、次の形式の**JSON配列のみ**にしてください。

[
  {{
    "id": 選んだ見出しのID(整数),
    "title": "日本語の見出し(25字程度、簡潔に)",
    "overview": "あらすじ(80字前後、1〜2文。途中で切れず、これだけで要点が分かる完結した文章)"
  }},
  ...(5件)
]

--- ニュース見出し一覧 ---
{headlines_text}
"""
    picked = gemini_json(prompt)

    by_id = {h["id"]: h for h in headlines}
    items = []
    seen = set()
    for p in picked:
        try:
            hid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if hid not in by_id or hid in seen:
            continue
        seen.add(hid)
        h = by_id[hid]
        items.append(
            {
                "title": str(p.get("title", "")).strip() or h["title"],
                "overview": str(p.get("overview", "")).strip() or h["summary"][:100],
                "source": h["source"],
                "orig_title": h["title"],
                "summary": h["summary"],
                "link": h["link"],
            }
        )
        if len(items) == 5:
            break

    if not items:
        raise RuntimeError("Geminiが有効なニュースを1件も選べませんでした")
    return items


def write_details(items):
    """各ニュースの日本語の詳細解説(本文)を作る。{番号: 本文} を返す"""
    blocks = []
    for n, it in enumerate(items, 1):
        text = fetch_article_text(it["link"])
        print(f"[INFO] 本文取得 {n}: {'成功' if text else '失敗(概要のみで要約)'} ({it['source']})")
        material = text if text else "(本文は取得できませんでした。概要のみです)"
        blocks.append(
            f"### {n}\n見出し: {it['orig_title']}\n出典: {it['source']}\n概要: {it['summary']}\n本文抜粋:\n{material}"
        )
    materials = "\n\n".join(blocks)

    prompt = f"""以下の{len(items)}件のニュースそれぞれについて、日本語の詳細解説を書いてください。

ルール:
- 原文の直訳や長い引用はせず、要点を自分の言葉で日本語にまとめる(著作権配慮)
- 入力に書かれていない事実・数字・固有名詞を付け足さない。推測や一般論で水増ししない
- 読みやすい文章で、段落は2〜4つ。段落の間は空行(\\n\\n)で区切る
- 目安は300〜500字。ただし入力の情報が少ない記事は、無理に長くせず短くてよい
- 英語の固有名詞は、日本語で一般的な表記があればそれを使う

出力は、説明文やMarkdownを一切含めず、次の形式の**JSON配列のみ**にしてください。

[
  {{"n": 1, "body": "詳細解説の本文"}},
  ...
]

--- ニュース素材 ---
{materials}
"""
    result = gemini_json(prompt)
    bodies = {}
    for r in result:
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError):
            continue
        body = str(r.get("body", "")).strip()
        if body:
            bodies[n] = body
    return bodies


# ---------------------------------------------------------------
# 詳細ページ(GitHub Pages)の生成
# ---------------------------------------------------------------
PAGE_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--sub:#666;--card:#f1f4f8;--accent:#2d3e50;--link:#3d5f94}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--fg:#e8eaed;--sub:#9aa3ad;--card:#1f262e;--accent:#2d3e50;--link:#8fb0e0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;line-height:1.85;-webkit-text-size-adjust:100%}
header{background:var(--accent);color:#fff;padding:14px 20px;font-weight:700}
main{max-width:720px;margin:0 auto;padding:24px 20px 48px}
h1{font-size:1.35rem;line-height:1.5;margin:0 0 8px}
.meta{color:var(--sub);font-size:.85rem;margin-bottom:20px}
.lead{background:var(--card);border-radius:10px;padding:14px 16px;margin:0 0 24px;font-weight:600}
p{margin:0 0 1.1em;font-size:1.02rem}
.src{margin-top:32px;padding-top:16px;border-top:1px solid var(--card);font-size:.85rem;color:var(--sub)}
.src p{font-size:.85rem;margin:0 0 .6em}
a{color:var(--link)}
ul.list{list-style:none;padding:0;margin:0}
ul.list li{padding:14px 0;border-bottom:1px solid var(--card)}
ul.list .t{font-weight:700}
ul.list .o{color:var(--sub);font-size:.9rem}
"""


def page_shell(title, body_html):
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header>🌍 {html.escape(CARD_TITLE)}</header>
<main>
{body_html}
</main>
</body>
</html>
"""


def render_article_page(item, body, date_label):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    paras = "\n".join(
        "<p>" + html.escape(p).replace(chr(10), "<br>") + "</p>" for p in paragraphs
    )
    link = item["link"]
    source_html = ""
    if link.startswith(("http://", "https://")):
        source_html = (
            f'<p>原文: <a href="{html.escape(link)}" rel="noopener noreferrer" target="_blank">'
            f'{html.escape(item["source"])}</a>(外部サイト・原文の言語で表示されます)</p>'
        )
    body_html = f"""<h1>{html.escape(item["title"])}</h1>
<div class="meta">{html.escape(item["source"])} ・ {html.escape(date_label)}</div>
<div class="lead">{html.escape(item["overview"])}</div>
{paras}
<div class="src">
{source_html}
<p>この解説はGeminiが原文をもとに日本語で要約したものです。正確な内容は原文でご確認ください。</p>
<p><a href="index.html">← 今日の5本に戻る</a></p>
</div>"""
    return page_shell(item["title"], body_html)


def render_day_index(items, date_label):
    lis = "\n".join(
        f'<li><a href="{n}.html"><span class="t">{n}. {html.escape(it["title"])}</span></a>'
        f'<div class="o">{html.escape(it["overview"])}</div></li>'
        for n, it in enumerate(items, 1)
    )
    body_html = f'<h1>{html.escape(date_label)}</h1>\n<ul class="list">\n{lis}\n</ul>'
    return page_shell(date_label, body_html)


def write_root_index():
    dates = sorted(
        [p.name for p in DOCS_DIR.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)],
        reverse=True,
    )
    lis = "\n".join(f'<li><a href="{d}/index.html"><span class="t">{d}</span></a></li>' for d in dates)
    body_html = f'<h1>Archive</h1>\n<ul class="list">\n{lis}\n</ul>'
    (DOCS_DIR / "index.html").write_text(page_shell("Archive", body_html), encoding="utf-8")


def page_base_url():
    base = os.environ.get("PAGES_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    raise RuntimeError("PAGES_BASE_URL も GITHUB_REPOSITORY も設定されていません")


# ---------------------------------------------------------------
# LINE Flexメッセージ
# ---------------------------------------------------------------
def build_flex_message(items):
    body_contents = []
    for i, item in enumerate(items):
        color = ACCENT_COLORS[i % len(ACCENT_COLORS)]

        number_badge = {
            "type": "box",
            "layout": "vertical",
            "width": "26px",
            "height": "26px",
            "backgroundColor": color,
            "cornerRadius": "13px",
            "justifyContent": "center",
            "alignItems": "center",
            "flex": 0,
            "contents": [
                {
                    "type": "text",
                    "text": str(i + 1),
                    "color": "#FFFFFF",
                    "size": "sm",
                    "weight": "bold",
                    "align": "center",
                }
            ],
        }

        text_column = {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "margin": "md",
            "spacing": "xs",
            "contents": [
                {
                    "type": "text",
                    "text": item["title"],
                    "weight": "bold",
                    "size": "md",
                    "wrap": True,
                    "color": "#1A1A1A",
                },
                {
                    "type": "text",
                    "text": item["overview"],
                    "size": "sm",
                    "wrap": True,
                    "color": "#555555",
                },
                {
                    "type": "text",
                    "text": "詳細を読む ›",
                    "size": "xs",
                    "weight": "bold",
                    "color": "#4A6FA5",
                    "align": "end",
                    "margin": "sm",
                },
            ],
        }

        row = {
            "type": "box",
            "layout": "horizontal",
            "margin": "lg",
            "alignItems": "flex-start",
            # カードのどこをタップしても、日本語の詳細ページが開く
            "action": {"type": "uri", "label": "詳細", "uri": item["page_url"]},
            "contents": [number_badge, text_column],
        }
        body_contents.append(row)

        if i < len(items) - 1:
            body_contents.append({"type": "separator", "margin": "lg"})

    return {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#2D3E50",
            "paddingAll": "20px",
            "contents": [
                {
                    "type": "text",
                    "text": f"🌍 {CARD_TITLE}",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "xl",
                    "wrap": True,
                }
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "text",
                    "text": "Powered by Gemini",
                    "size": "xxs",
                    "color": "#AAAAAA",
                    "align": "center",
                }
            ],
        },
    }


def line_broadcast(messages):
    res = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={
            "Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN']}",
            "Content-Type": "application/json",
        },
        json={"messages": messages},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"LINE送信に失敗しました: {res.status_code} {res.text}")


# ---------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------
def cmd_build():
    now = datetime.datetime.now(JST)
    date_iso = now.strftime("%Y-%m-%d")
    date_label = f"{now.year}年{now.month}月{now.day}日({WEEKDAY_JP[now.weekday()]})"

    print("ニュース収集中...")
    headlines = collect_headlines()

    print("Geminiで5件を選定中...")
    items = select_news(headlines)

    print("詳細解説を作成中...")
    try:
        bodies = write_details(items)
    except Exception as e:
        # 詳細解説が作れなくても、あらすじだけのページで配信は続ける
        print(f"[WARN] 詳細解説の作成に失敗、あらすじのみで代替します: {e}", file=sys.stderr)
        bodies = {}

    base = page_base_url()
    day_dir = DOCS_DIR / date_iso
    day_dir.mkdir(parents=True, exist_ok=True)

    for n, item in enumerate(items, 1):
        body = bodies.get(n) or item["overview"]
        (day_dir / f"{n}.html").write_text(render_article_page(item, body, date_label), encoding="utf-8")
        item["page_url"] = f"{base}/{date_iso}/{n}.html"

    (day_dir / "index.html").write_text(render_day_index(items, date_label), encoding="utf-8")
    write_root_index()

    OUT_DIR.mkdir(exist_ok=True)
    NEWS_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完了: {len(items)}件のページを docs/{date_iso}/ に生成しました")


def cmd_send():
    items = json.loads(NEWS_JSON.read_text(encoding="utf-8"))
    bubble = build_flex_message(items)
    print("LINEにFlexメッセージを送信中...")
    line_broadcast([{"type": "flex", "altText": CARD_TITLE, "contents": bubble}])
    print("完了しました。")


def cmd_error():
    text = "本日のニュース配信でエラーが発生しました。GitHub Actionsのログを確認してください。"
    line_broadcast([{"type": "text", "text": text}])


def main():
    commands = {"build": cmd_build, "send": cmd_send, "error": cmd_error}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print("使い方: python news_bot.py [build|send|error]", file=sys.stderr)
        sys.exit(2)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
