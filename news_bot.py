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
- GEMINI_FALLBACK_MODELS (任意。カンマ区切り。主モデルが使えないとき順に試す予備モデル)
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

# 常に最新のFlashモデルを使うため、エイリアス gemini-flash-latest を既定にしている。
# Googleが新しいFlashを出すと、このエイリアスの指す先が自動で入れ替わる(約2週間前にメール告知あり)。
# 挙動を固定したいときは、GitHubの Variables に GEMINI_MODEL を登録して、
# gemini-3.6-flash のようにバージョンを明示する。
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
# 主モデルが混雑(503)・無料枠の上限(429)・提供終了(404)のときに順に試す予備モデル。
# 存在しないモデル名だった場合は404としてスキップされるだけで、実行は止まらない。
# GitHubの Variables に GEMINI_FALLBACK_MODELS(カンマ区切り)を登録すると差し替えられる。
DEFAULT_FALLBACK_MODELS = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

RSS_FEEDS = [
    ("NHKニュース(主要)", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
    ("BBC World News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("ITmedia AI+", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml"),
]

MAX_ITEMS_PER_FEED = 10
ARTICLE_TEXT_LIMIT = 12000  # 詳細解説のために読み込む本文の最大文字数(1記事あたり)

JST = datetime.timezone(datetime.timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

DOCS_DIR = pathlib.Path("docs")
OUT_DIR = pathlib.Path("out")
NEWS_JSON = OUT_DIR / "news.json"

CARD_TITLE = "Today's Top 5 Stories"

# 分野ごとのラベル色(カードの見出し横に表示)。この5分野以外は「その他」(グレー)になる
CATEGORY_COLORS = {
    "政治": "#4A6FA5",
    "経済": "#5C8374",
    "金融": "#B08968",
    "IT": "#8E6C88",
    "AI": "#3F8E9B",
}
DEFAULT_CATEGORY = "その他"
DEFAULT_CATEGORY_COLOR = "#777777"

# ---- カードの余白(縦の長さを調整したいときは、ここの値を変える) ----
# Flexの余白トークンは none(0) / xs(2px) / sm(4px) / md(8px) / lg(12px) / xl(16px) / xxl(20px)
CARD_HEADER_PAD = "16px"
CARD_BODY_PAD = "16px"
CARD_FOOTER_PAD = "10px"
ITEM_GAP = "md"  # ニュース同士の間隔(区切り線の上下)


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


def _post_with_retry(model, headers, payload, waits):
    """1つのモデルに対し、一時的なエラー(混雑・上限・通信エラー)なら待って再試行する。
    最後の応答(通信自体に失敗した場合は None)を返す"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    retry_statuses = {429, 500, 502, 503, 504}
    res = None
    for attempt in range(len(waits) + 1):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=180)
            if res.status_code not in retry_statuses:
                return res
            reason = f"HTTP {res.status_code}"
        except (requests.ConnectionError, requests.Timeout) as e:
            res = None
            reason = f"通信エラー: {e}"
        if attempt < len(waits):
            print(
                f"[WARN] {model} が一時的に使えません({reason})。{waits[attempt]}秒待って再試行します({attempt + 1}/{len(waits)})",
                file=sys.stderr,
            )
            time.sleep(waits[attempt])
    return res


def gemini_generate(prompt, json_mode=True):
    """Geminiに依頼して、出力テキストを返す。
    主モデルが混雑(503など)や提供終了(404)で使えないときは、予備モデルに自動で切り替える"""
    primary = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    fallbacks_env = os.environ.get("GEMINI_FALLBACK_MODELS")
    fallbacks = (
        [m.strip() for m in fallbacks_env.split(",") if m.strip()]
        if fallbacks_env is not None
        else DEFAULT_FALLBACK_MODELS
    )
    models = []
    for m in [primary] + fallbacks:
        if m not in models:
            models.append(m)

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": os.environ["GEMINI_API_KEY"],
    }
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}

    failures = []
    res = None
    for i, model in enumerate(models):
        # 主モデルは少し粘り、予備モデルは短めに待つ(全体が長引かないように)
        waits = [10, 20, 40] if i == 0 else [5, 10]
        res = _post_with_retry(model, headers, payload, waits)
        if res is not None and res.status_code == 200:
            if i > 0:
                print(f"[INFO] 予備モデル {model} で成功しました")
            break
        if res is None:
            failures.append(f"{model}: 接続できませんでした")
        elif res.status_code in (429, 500, 502, 503, 504, 404):
            # 混雑・上限・モデル提供終了は、次のモデルで試す価値がある
            failures.append(f"{model}: HTTP {res.status_code} {res.text[:200]}")
        else:
            # 400/401/403(キーが無効など)は、モデルを変えても直らないので即エラー
            raise RuntimeError(f"Gemini APIエラー(model={model}): {res.status_code} {res.text[:500]}")
        print(f"[WARN] {failures[-1][:150]} → 次のモデルを試します", file=sys.stderr)
    else:
        raise RuntimeError("すべてのGeminiモデルで失敗しました: " + " | ".join(failures))

    data = res.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Geminiの応答解析に失敗しました: {data}") from e


def gemini_json(prompt):
    return parse_json_list(gemini_generate(prompt, json_mode=True))


def normalize_category(value):
    cat = str(value or "").strip()
    if cat.lower() in ("it", "ai"):
        cat = cat.upper()
    return cat if cat in CATEGORY_COLORS else DEFAULT_CATEGORY


def select_news(headlines):
    """見出し一覧から5件を選び、日本語タイトル・分野・あらすじを付ける"""
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
    "category": "分野。次のいずれか1つだけ: 政治 / 経済 / 金融 / IT / AI",
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
                "category": normalize_category(p.get("category")),
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


def clean_detail_text(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "■", text)  # 万一Markdown見出しで返ってきても小見出しに直す
    return text.strip()


def write_detail(item, article_text):
    """1つのニュースについて、日本語の詳細解説(本文)を作る"""
    if article_text:
        material = f"本文抜粋:\n{article_text}"
        length_rule = "文字数の目安は1500〜3000字。本文抜粋に書かれている内容は、できるだけ漏らさず盛り込む。"
    else:
        material = "(元記事の本文は取得できませんでした。見出しと概要だけが手がかりです)"
        length_rule = "入力が見出しと概要だけなので、そこから言えることだけを200〜400字で短くまとめる。水増しは絶対にしない。"

    prompt = f"""以下のニュース記事について、日本語で、できる限り詳しい解説記事を書いてください。

ルール:
- 本文抜粋に書かれている事実・数字・日付・人物名・発言の内容・背景・経緯・各者の立場・今後の見通しを、できるだけ詳細に盛り込む
- 原文の直訳や長い引用はせず、自分の言葉で書き直す(著作権への配慮。引用が必要でも短い一節にとどめる)
- 入力に書かれていない事実・数字・固有名詞は付け足さない。推測や一般論で水増ししない
- 構成は、「■」で始まる小見出しの行を3〜5個置き、各小見出しの下に段落を書く。段落の間は空行を入れる
- {length_rule}
- 出力は解説の本文のみ。前置きや結びの挨拶、Markdown記法(#や**)は使わない
- 英語の固有名詞は、日本語で一般的な表記があればそれを使う

--- 記事 ---
見出し: {item['orig_title']}
出典: {item['source']}
概要: {item['summary']}
{material}
"""
    return clean_detail_text(gemini_generate(prompt, json_mode=False))


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
h2{font-size:1.1rem;line-height:1.5;margin:1.8em 0 .6em;padding-left:.6em;border-left:4px solid var(--link)}
.meta{color:var(--sub);font-size:.85rem;margin-bottom:20px}
.tag{display:inline-block;color:#fff;font-size:.75rem;font-weight:700;border-radius:999px;padding:0 .8em;margin-right:.6em;line-height:1.7}
.lead{background:var(--card);border-radius:10px;padding:14px 16px;margin:0 0 24px;font-weight:600}
p{margin:0 0 1.1em;font-size:1.02rem}
.note{color:var(--sub);font-size:.85rem;border:1px dashed var(--sub);border-radius:8px;padding:8px 12px;margin:0 0 20px}
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


def body_to_html(body):
    """本文を、「■」小見出し(h2)と段落(p)のHTMLに変換する"""
    out = []
    for block in re.split(r"\n\s*\n", body):
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        while lines and lines[0].startswith("■"):
            out.append(f"<h2>{html.escape(lines.pop(0).lstrip('■ ').strip())}</h2>")
        if lines:
            out.append("<p>" + "<br>".join(html.escape(l) for l in lines) + "</p>")
    return "\n".join(out)


DETAIL_NOTES = {
    "summary_only": "※元記事の本文を取得できなかったため、RSSの見出しと概要をもとにした短い解説です。詳しくは原文をご確認ください。",
    "failed": "※詳細解説の生成に失敗したため、あらすじのみを表示しています。",
}


def render_article_page(item, body, date_label, detail_status="full"):
    link = item["link"]
    source_html = ""
    if link.startswith(("http://", "https://")):
        source_html = (
            f'<p>原文: <a href="{html.escape(link)}" rel="noopener noreferrer" target="_blank">'
            f'{html.escape(item["source"])}</a>(外部サイト・原文の言語で表示されます)</p>'
        )
    cat = item.get("category", DEFAULT_CATEGORY)
    color = CATEGORY_COLORS.get(cat, DEFAULT_CATEGORY_COLOR)
    note = DETAIL_NOTES.get(detail_status)
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    body_html = f"""<h1>{html.escape(item["title"])}</h1>
<div class="meta"><span class="tag" style="background:{color}">{html.escape(cat)}</span>{html.escape(item["source"])} ・ {html.escape(date_label)}</div>
<div class="lead">{html.escape(item["overview"])}</div>
{note_html}
{body_to_html(body)}
<div class="src">
{source_html}
<p>この解説はGeminiが原文をもとに日本語で要約・再構成したものです。正確な内容は原文でご確認ください。</p>
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
        cat = item.get("category", DEFAULT_CATEGORY)
        color = CATEGORY_COLORS.get(cat, DEFAULT_CATEGORY_COLOR)

        # 見出しの横に出す、分野ラベル(政治・経済・金融・IT・AI)
        category_label = {
            "type": "box",
            "layout": "vertical",
            "width": "40px",
            "height": "20px",
            "backgroundColor": color,
            "cornerRadius": "10px",
            "justifyContent": "center",
            "alignItems": "center",
            "flex": 0,
            "contents": [
                {
                    "type": "text",
                    "text": cat,
                    "color": "#FFFFFF",
                    "size": "xxs",
                    "weight": "bold",
                    "align": "center",
                }
            ],
        }

        # 1行目: 分野ラベル + 見出し + 「詳細›」
        title_row = {
            "type": "box",
            "layout": "horizontal",
            "alignItems": "center",
            "contents": [
                category_label,
                {
                    "type": "text",
                    "text": item["title"],
                    "weight": "bold",
                    "size": "md",
                    "wrap": True,
                    "color": "#1A1A1A",
                    "flex": 1,
                    "margin": "sm",
                },
                {
                    "type": "text",
                    "text": "詳細›",
                    "size": "xxs",
                    "weight": "bold",
                    "color": "#4A6FA5",
                    "flex": 0,
                    "margin": "sm",
                },
            ],
        }

        # 2行目: あらすじ(横幅いっぱいに使って、行数を減らす)
        overview = {
            "type": "text",
            "text": item["overview"],
            "size": "sm",
            "wrap": True,
            "color": "#555555",
            "margin": "xs",
        }

        item_box = {
            "type": "box",
            "layout": "vertical",
            "margin": ITEM_GAP,
            # カードのどこをタップしても、日本語の詳細ページが開く
            "action": {"type": "uri", "label": "詳細", "uri": item["page_url"]},
            "contents": [title_row, overview],
        }
        body_contents.append(item_box)

        if i < len(items) - 1:
            body_contents.append({"type": "separator", "margin": ITEM_GAP})

    return {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#2D3E50",
            "paddingAll": CARD_HEADER_PAD,
            "contents": [
                {
                    "type": "text",
                    "text": f"🌍 {CARD_TITLE}",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "lg",
                    "wrap": True,
                }
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": CARD_BODY_PAD,
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": CARD_FOOTER_PAD,
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

    base = page_base_url()
    day_dir = DOCS_DIR / date_iso
    day_dir.mkdir(parents=True, exist_ok=True)

    print("詳細解説を作成中...")
    for n, item in enumerate(items, 1):
        article_text = fetch_article_text(item["link"])
        print(f"[INFO] {n}/{len(items)} ({item['source']}) 本文取得: {'成功' if article_text else '失敗(概要のみで解説)'}")

        body = ""
        try:
            body = write_detail(item, article_text)
        except Exception as e:
            # 詳細解説が作れなくても、あらすじだけのページで配信は続ける
            print(f"[WARN] {n}番目の詳細解説の作成に失敗、あらすじのみで代替します: {e}", file=sys.stderr)

        if body:
            status = "full" if article_text else "summary_only"
        else:
            status = "failed"
            body = item["overview"]
        print(f"[INFO] {n}番目の詳細解説: {len(body)}字 ({status})")

        (day_dir / f"{n}.html").write_text(
            render_article_page(item, body, date_label, status), encoding="utf-8"
        )
        item["page_url"] = f"{base}/{date_iso}/{n}.html"
        time.sleep(2)  # 無料枠のレート制限に配慮して、少し間隔を空ける

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
