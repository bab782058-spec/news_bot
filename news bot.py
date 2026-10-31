"""
毎朝、7つのテーマから1件ずつ選んだニュースを LINE Flexメッセージで配信し、
各ニュースの「日本語の詳細ページ」を GitHub Pages に自動生成するスクリプト。

テーマ: 日本の政治 / 日本の経済 / 日本の金融 / 日本のIT / 関東の天候 / 世界の金融 / 世界のIT
(テーマの追加・変更は TOPICS を編集するだけ)

使い方(GitHub Actionsから順番に呼ぶ):
  python news_bot.py build   # 収集→選定→詳細ページ生成(docs/ と out/news.json を作る)
  python news_bot.py send    # out/news.json を読んでLINEにFlexメッセージを送る
  python news_bot.py error   # エラー発生時の通知テキストをLINEに送る

環境変数:
- GEMINI_API_KEY (build で必要)
- LINE_CHANNEL_ACCESS_TOKEN (send / error で必要)
- GEMINI_MODELS (任意。試すモデルの順番。未指定なら自動で選ぶ。gemini_client.py 参照)
- PAGES_BASE_URL (任意。未指定なら GITHUB_REPOSITORY から https://<owner>.github.io/<repo> を組み立てる)
"""

import datetime
import html
import json
import os
import pathlib
import re
import sys
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Callable

import feedparser
import requests

from gemini_client import GeminiClient, GeminiError, parse_json_response
from line_client import LineClient, flex_box, flex_text

JST = datetime.timezone(datetime.timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

DOCS_DIR = pathlib.Path("docs")
OUT_DIR = pathlib.Path("out")
NEWS_JSON = OUT_DIR / "news.json"

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; news-summary-bot)"}
REQUEST_TIMEOUT_SECONDS = 15

MAX_ITEMS_PER_FEED = 10
HEADLINE_SUMMARY_LIMIT = 200  # 選定用に渡す、見出しごとの概要の最大文字数
ARTICLE_TEXT_LIMIT = 12000  # 詳細解説のために読み込む本文の最大文字数(1記事あたり)
DETAIL_INTERVAL_SECONDS = 2  # 無料枠のレート制限に配慮して、詳細解説の間に空ける間隔

CARD_TITLE = "Today's 7 Topics"
EMPTY_TOPIC_NOTICE = "[WARN] 「{name}」は見出しを1件も取得できなかったため、今日は載せません"

# ---- カードの見た目(縦の長さを調整したいときは、ここの値を変える) ----
# Flexの余白トークンは none(0) / xs(2px) / sm(4px) / md(8px) / lg(12px) / xl(16px) / xxl(20px)
CARD_HEADER_PAD = "16px"
CARD_BODY_PAD = "16px"
CARD_FOOTER_PAD = "10px"
ITEM_GAP = "md"  # ニュース同士の間隔(区切り線の上下)
LABEL_WIDTH = "56px"  # 4文字のラベルが収まる幅
TITLE_SIZE = "sm"
OVERVIEW_SIZE = "xs"
HEADER_COLOR = "#2D3E50"


# ---------------------------------------------------------------
# データ型
# ---------------------------------------------------------------
@dataclass(frozen=True)
class Headline:
    """収集した見出し1件(まだGeminiに選ばれる前)"""

    id: int
    topic_key: str
    source: str
    title: str
    summary: str
    link: str
    body: str = ""  # 本文を最初から持っている場合(気象庁の天気概況)。空なら、選ばれた後でリンク先から取得する


@dataclass(frozen=True)
class Topic:
    key: str
    label: str  # カードの色付きラベル(4文字以内)
    prompt_name: str  # Geminiへの指示に出てくるテーマ名
    guideline: str  # このテーマで何を選ぶか(テーマ同士の境目を決める)
    color: str
    collect: Callable[[], list]  # 引数なしで呼ぶと、このテーマの見出し(Headline)一覧を返す


CARD_FIELDS = ("label", "color", "title", "overview", "page_url")  # send コマンドが使う項目


@dataclass
class NewsItem:
    """配信する1件。LINEカードと詳細ページの両方に使う"""

    label: str
    color: str
    title: str  # 日本語の見出し
    overview: str  # あらすじ
    source: str
    orig_title: str
    summary: str
    link: str
    body: str = ""
    page_url: str = ""

    def to_card(self):
        """send コマンドに渡す、カード表示に必要な項目だけ"""
        return {key: getattr(self, key) for key in CARD_FIELDS}


# ---------------------------------------------------------------
# ニュース収集
# ---------------------------------------------------------------
def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=None)
def fetch_feed_entries(source_name, url):
    """RSSを1本取得して、(見出し, 概要, リンク)の組を返す。複数テーマで共有するフィードは1回しか取りに行かない"""
    try:
        feed = feedparser.parse(url)
    except Exception as error:
        print(f"[WARN] {source_name} の取得に失敗: {error}", file=sys.stderr)
        return ()

    entries = []
    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
        title = strip_html(getattr(entry, "title", ""))
        link = getattr(entry, "link", "").strip()
        if title and link:
            summary = strip_html(getattr(entry, "summary", ""))[:HEADLINE_SUMMARY_LIMIT]
            entries.append((title, summary, link))

    print(f"[INFO] {source_name}: {len(entries)}件取得")
    return tuple(entries)


def rss_collector(topic_key, feeds):
    """指定したRSS(出典名, URL)の見出しを集める collect 関数を作る"""

    def collect():
        return [
            Headline(0, topic_key, source_name, title, summary, link)
            for source_name, url in feeds
            for title, summary, link in fetch_feed_entries(source_name, url)
        ]

    return collect


# 気象庁の「府県天気概況」。関東の1都6県(表示名, 気象庁の地域コード)
JMA_OVERVIEW_URL = "https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{office_code}.json"
JMA_FORECAST_PAGE = "https://www.jma.go.jp/bosai/forecast/"
KANTO_WEATHER_OFFICES = [
    ("東京都", "130000"),
    ("神奈川県", "140000"),
    ("埼玉県", "110000"),
    ("千葉県", "120000"),
    ("茨城県", "080000"),
    ("栃木県", "090000"),
    ("群馬県", "100000"),
]
WEATHER_SUMMARY_CHARS_PER_AREA = 90  # 選定用の概要に載せる、1都県あたりの文字数


def fetch_weather_overview(office_code):
    response = requests.get(
        JMA_OVERVIEW_URL.format(office_code=office_code), headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return " ".join(response.json().get("text", "").split())


def collect_kanto_weather():
    """関東1都6県の天気概況を、1件の「関東の天気」にまとめる(取得できた地域だけ)"""
    texts_by_area = []
    for area_name, office_code in KANTO_WEATHER_OFFICES:
        try:
            text = fetch_weather_overview(office_code)
        except Exception as error:
            print(f"[WARN] 気象庁の天気概況({area_name})の取得に失敗: {error}", file=sys.stderr)
            continue
        if text:
            texts_by_area.append((area_name, text))
    print(f"[INFO] 気象庁 天気概況: {len(texts_by_area)}/{len(KANTO_WEATHER_OFFICES)}地域取得")

    if not texts_by_area:
        return []
    summary = " ".join(f"【{name}】{text[:WEATHER_SUMMARY_CHARS_PER_AREA]}" for name, text in texts_by_area)
    body = "\n\n".join(f"【{name}】\n{text}" for name, text in texts_by_area)[:ARTICLE_TEXT_LIMIT]
    return [Headline(0, "kanto_weather", "気象庁", "関東の天気概況", summary, JMA_FORECAST_PAGE, body)]


NHK_POLITICS = ("NHKニュース(政治)", "https://www3.nhk.or.jp/rss/news/cat4.xml")
NHK_ECONOMY = ("NHKニュース(経済)", "https://www3.nhk.or.jp/rss/news/cat5.xml")
NHK_TOP = ("NHKニュース(主要)", "https://www3.nhk.or.jp/rss/news/cat0.xml")
YAHOO_BUSINESS = ("Yahoo!ニュース(経済)", "https://news.yahoo.co.jp/rss/topics/business.xml")
YAHOO_IT = ("Yahoo!ニュース(IT)", "https://news.yahoo.co.jp/rss/topics/it.xml")
FSA_NEWS = ("金融庁 新着情報", "https://www.fsa.go.jp/fsaNewsListAll_rss2.xml")
ITMEDIA_ALL = ("ITmedia", "https://rss.itmedia.co.jp/rss/2.0/itmedia_all.xml")
ITMEDIA_AI = ("ITmedia AI+", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml")
BLOOMBERG_MARKETS = ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss")
BBC_BUSINESS = ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml")
BBC_TECHNOLOGY = ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml")
TECHCRUNCH = ("TechCrunch", "https://techcrunch.com/feed/")

# 配信するテーマ。この並び順でカードに表示される。1テーマ1件。
TOPICS = [
    Topic(
        "jp_politics", "日本政治", "日本の政治",
        "国会・内閣・政党・選挙・外交・安全保障など、日本の政治に関するもの",
        "#4A6FA5", rss_collector("jp_politics", [NHK_POLITICS, NHK_TOP]),
    ),
    Topic(
        "jp_economy", "日本経済", "日本の経済",
        "景気・物価・賃金・雇用・企業業績・貿易・産業など。金利・為替・株価・金融機関の話題は「日本の金融」に回す",
        "#5C8374", rss_collector("jp_economy", [NHK_ECONOMY, YAHOO_BUSINESS]),
    ),
    Topic(
        "jp_finance", "日本金融", "日本の金融",
        "日銀の政策・金利・為替・株価・銀行・保険・証券・金融庁の施策など",
        "#B08968", rss_collector("jp_finance", [FSA_NEWS, YAHOO_BUSINESS, NHK_ECONOMY]),
    ),
    Topic(
        "jp_it", "日本IT", "日本のIT",
        "国内のITサービス・企業・AI・サイバーセキュリティ・デジタル政策など",
        "#8E6C88", rss_collector("jp_it", [ITMEDIA_ALL, ITMEDIA_AI, YAHOO_IT]),
    ),
    Topic(
        "kanto_weather", "関東天気", "関東の天候",
        "見出しは1件だけ。それを選び、今日から明日にかけての天気の要点(雨・気温・風・注意点)をあらすじにまとめる",
        "#3F8E9B", collect_kanto_weather,
    ),
    Topic(
        "world_finance", "世界金融", "世界の金融",
        "海外の株式・債券・為替・商品市場・主要中央銀行の動向など",
        "#9C6644", rss_collector("world_finance", [BLOOMBERG_MARKETS, BBC_BUSINESS]),
    ),
    Topic(
        "world_it", "世界IT", "世界のIT",
        "海外のテック企業・AI・半導体・サービス・規制の動向など",
        "#6B5B95", rss_collector("world_it", [BBC_TECHNOLOGY, TECHCRUNCH]),
    ),
]


def collect_headlines():
    """全テーマの見出しを集めて、通し番号(id)を振る。見出しが取れなかったテーマは、ログに残して飛ばす"""
    headlines = []
    for topic in TOPICS:
        topic_headlines = topic.collect()
        if not topic_headlines:
            print(EMPTY_TOPIC_NOTICE.format(name=topic.prompt_name), file=sys.stderr)
        for headline in topic_headlines:
            headlines.append(replace(headline, id=len(headlines)))

    if not headlines:
        raise RuntimeError("どのテーマからも見出しを取得できませんでした")
    return headlines


def fetch_article_text(url):
    """元記事の本文をできる範囲で取得する(有料記事や取得拒否の場合は空文字)"""
    try:
        import trafilatura

        response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return ""
        return (trafilatura.extract(response.text) or "")[:ARTICLE_TEXT_LIMIT]
    except Exception as error:
        print(f"[WARN] 本文取得に失敗({url}): {error}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------
# Geminiで、テーマごとに1件を選ぶ
# ---------------------------------------------------------------
def build_selection_prompt(headlines):
    sections = []
    for topic in TOPICS:
        topic_headlines = [h for h in headlines if h.topic_key == topic.key]
        if not topic_headlines:
            continue
        lines = [f"## {topic.prompt_name}(選ぶ基準: {topic.guideline})"]
        lines += [f"[{h.id}] ({h.source}) {h.title}\n概要: {h.summary}" for h in topic_headlines]
        sections.append("\n\n".join(lines))

    return f"""以下は、本日時点のニュース見出しを、テーマごとに分けた一覧です。
各見出しの先頭の[ ]内の数字がIDです。

各テーマから、最も重要と考えられるニュースを1つずつ選び、日本語の見出しとあらすじを付けてください。
- 必ず、そのテーマの一覧の中から選ぶ(別のテーマの見出しは選ばない)
- 一覧に見出しがないテーマは、出力に含めない
- 同じ出来事を、複数のテーマで重複して選ばない
- 英語の見出しも、日本語にする

出力は、説明文やMarkdownを一切含めず、次の形式の**JSON配列のみ**にしてください。

[
  {{
    "id": 選んだ見出しのID(整数),
    "title": "日本語の見出し(25字程度、簡潔に)",
    "overview": "あらすじ(60字前後、1〜2文。途中で切れず、これだけで要点が分かる完結した文章)"
  }},
  ...(テーマごとに1件)
]

--- ニュース見出し一覧 ---
{chr(10).join(sections)}
"""


def make_item(topic, headline, pick=None):
    """選ばれた見出しから配信用の1件を作る。Geminiの出力に欠けがあれば、元の見出し・概要で補う"""
    pick = pick or {}
    return NewsItem(
        label=topic.label,
        color=topic.color,
        title=str(pick.get("title", "")).strip() or headline.title,
        overview=str(pick.get("overview", "")).strip() or headline.summary[:100],
        source=headline.source,
        orig_title=headline.title,
        summary=headline.summary,
        link=headline.link,
        body=headline.body,
    )


def resolve_selection(picks, headlines):
    """Geminiの選択結果を、テーマごとに高々1件の NewsItem にする(テーマの並び順)。

    - 存在しないID・別テーマの重複・同じリンクの重複は無視する
    - 見出しがあるのにGeminiが選ばなかったテーマは、先頭の見出しで補う(テーマが欠けるより良い)
    """
    by_id = {h.id: h for h in headlines}
    chosen = {}  # topic_key -> (見出し, Geminiの出力)
    used_links = set()

    for pick in picks:
        try:
            headline = by_id.get(int(pick.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
        if headline is None or headline.topic_key in chosen or headline.link in used_links:
            continue
        chosen[headline.topic_key] = (headline, pick)
        used_links.add(headline.link)

    items = []
    for topic in TOPICS:
        if topic.key in chosen:
            headline, pick = chosen[topic.key]
        else:
            fallback = next((h for h in headlines if h.topic_key == topic.key and h.link not in used_links), None)
            if fallback is None:
                continue
            print(f"[WARN] 「{topic.prompt_name}」はGeminiが選ばなかったため、先頭の見出しで代替します", file=sys.stderr)
            headline, pick = fallback, None
            used_links.add(fallback.link)
        items.append(make_item(topic, headline, pick))
    return items


def select_news(gemini, headlines):
    picks = parse_json_response(gemini.generate(build_selection_prompt(headlines), json_mode=True))
    if not isinstance(picks, list):
        raise RuntimeError(f"Geminiの出力が想定したJSON配列ではありません: {str(picks)[:200]}")
    items = resolve_selection(picks, headlines)
    if not items:
        raise RuntimeError("Geminiが有効なニュースを1件も選べませんでした")
    return items


# ---------------------------------------------------------------
# Geminiで、詳細解説を書く
# ---------------------------------------------------------------
def clean_detail_text(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "■", text)  # 万一Markdown見出しで返ってきても小見出しに直す
    return text.strip()


def write_detail(gemini, item, article_text):
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
見出し: {item.orig_title}
出典: {item.source}
概要: {item.summary}
{material}
"""
    return clean_detail_text(gemini.generate(prompt))


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

DETAIL_NOTES = {
    "summary_only": "※元記事の本文を取得できなかったため、RSSの見出しと概要をもとにした短い解説です。詳しくは原文をご確認ください。",
    "failed": "※詳細解説の生成に失敗したため、あらすじのみを表示しています。",
}


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
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        while lines and lines[0].startswith("■"):
            out.append(f"<h2>{html.escape(lines.pop(0).lstrip('■ ').strip())}</h2>")
        if lines:
            out.append("<p>" + "<br>".join(html.escape(line) for line in lines) + "</p>")
    return "\n".join(out)


def render_article_page(item, body, date_label, detail_status="full"):
    source_html = ""
    if item.link.startswith(("http://", "https://")):
        source_html = (
            f'<p>原文: <a href="{html.escape(item.link)}" rel="noopener noreferrer" target="_blank">'
            f"{html.escape(item.source)}</a>(外部サイト・原文の言語で表示されます)</p>"
        )
    note = DETAIL_NOTES.get(detail_status)
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    body_html = f"""<h1>{html.escape(item.title)}</h1>
<div class="meta"><span class="tag" style="background:{item.color}">{html.escape(item.label)}</span>{html.escape(item.source)} ・ {html.escape(date_label)}</div>
<div class="lead">{html.escape(item.overview)}</div>
{note_html}
{body_to_html(body)}
<div class="src">
{source_html}
<p>この解説はGeminiが原文をもとに日本語で要約・再構成したものです。正確な内容は原文でご確認ください。</p>
<p><a href="index.html">← 今日のニュースに戻る</a></p>
</div>"""
    return page_shell(item.title, body_html)


def render_day_index(items, date_label):
    lis = "\n".join(
        f'<li><a href="{number}.html"><span class="t">{number}. {html.escape(item.title)}</span></a>'
        f'<div class="o">{html.escape(item.overview)}</div></li>'
        for number, item in enumerate(items, 1)
    )
    return page_shell(date_label, f'<h1>{html.escape(date_label)}</h1>\n<ul class="list">\n{lis}\n</ul>')


def write_root_index():
    dates = sorted(
        (p.name for p in DOCS_DIR.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
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
# LINE Flexメッセージ(見た目だけを担当。通信は line_client.py)
# ---------------------------------------------------------------
def build_label_badge(label, color):
    return flex_box(
        [flex_text(label, size="xxs", color="#FFFFFF", weight="bold", align="center")],
        width=LABEL_WIDTH, height="20px", backgroundColor=color, cornerRadius="10px",
        justifyContent="center", alignItems="center", flex=0,
    )


def build_item_row(card):
    # 1行目: テーマのラベル + 見出し + 「詳細›」 / 2行目: あらすじ
    title_row = flex_box(
        [
            build_label_badge(card["label"], card["color"]),
            flex_text(card["title"], size=TITLE_SIZE, weight="bold", wrap=True, flex=1, margin="sm"),
            flex_text("詳細›", size="xxs", weight="bold", color="#4A6FA5", flex=0, margin="sm"),
        ],
        layout="horizontal", alignItems="center",
    )
    overview = flex_text(card["overview"], size=OVERVIEW_SIZE, color="#555555", wrap=True, margin="xs")
    return flex_box(
        [title_row, overview],
        margin=ITEM_GAP,
        action={"type": "uri", "label": "詳細", "uri": card["page_url"]},  # カードのどこをタップしても詳細ページが開く
    )


def build_flex_message(cards):
    body_contents = []
    for index, card in enumerate(cards):
        if index > 0:
            body_contents.append({"type": "separator", "margin": ITEM_GAP})
        body_contents.append(build_item_row(card))

    return {
        "type": "bubble",
        "size": "giga",
        "header": flex_box(
            [flex_text(f"🌍 {CARD_TITLE}", size="lg", color="#FFFFFF", weight="bold", wrap=True)],
            backgroundColor=HEADER_COLOR, paddingAll=CARD_HEADER_PAD,
        ),
        "body": flex_box(body_contents, paddingAll=CARD_BODY_PAD),
        "footer": flex_box(
            [flex_text("Powered by Gemini", size="xxs", color="#AAAAAA", align="center")],
            paddingAll=CARD_FOOTER_PAD,
        ),
    }


# ---------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------
def japanese_date_label(moment):
    return f"{moment.year}年{moment.month}月{moment.day}日({WEEKDAY_JP[moment.weekday()]})"


def build_detail_page(gemini, item, number, total, date_label):
    """詳細ページのHTMLと、「Geminiが使える状態か」を返す。解説の生成に失敗しても、あらすじだけのページで配信は続ける。

    gemini が None のときは、解説を生成せずあらすじだけのページにする(Geminiが全滅した後の時間稼ぎ防止)"""
    article_text = item.body or fetch_article_text(item.link)
    print(f"[INFO] {number}/{total} ({item.source}) 本文: {'あり' if article_text else 'なし(概要のみで解説)'}")

    body, gemini_ok = "", gemini is not None
    if gemini is not None:
        try:
            body = write_detail(gemini, item, article_text)
        except GeminiError as error:
            gemini_ok = False
            print(f"[WARN] {number}番目の詳細解説の作成に失敗、あらすじのみで代替します: {error}", file=sys.stderr)

    if body:
        status = "full" if article_text else "summary_only"
    else:
        status, body = "failed", item.overview
    print(f"[INFO] {number}番目の詳細解説: {len(body)}字 ({status})")
    return render_article_page(item, body, date_label, status), gemini_ok


def cmd_build():
    now = datetime.datetime.now(JST)
    date_iso = now.strftime("%Y-%m-%d")
    date_label = japanese_date_label(now)
    gemini = GeminiClient.from_env()

    print("ニュース収集中...")
    headlines = collect_headlines()

    print("Geminiでテーマごとに選定中...")
    items = select_news(gemini, headlines)

    base = page_base_url()
    day_dir = DOCS_DIR / date_iso
    day_dir.mkdir(parents=True, exist_ok=True)

    print("詳細解説を作成中...")
    detail_writer = gemini  # 全モデルが使えなくなったら None にして、残りはあらすじだけのページにする
    for number, item in enumerate(items, 1):
        page, gemini_ok = build_detail_page(detail_writer, item, number, len(items), date_label)
        if not gemini_ok:
            detail_writer = None
        (day_dir / f"{number}.html").write_text(page, encoding="utf-8")
        item.page_url = f"{base}/{date_iso}/{number}.html"
        time.sleep(DETAIL_INTERVAL_SECONDS)

    (day_dir / "index.html").write_text(render_day_index(items, date_label), encoding="utf-8")
    write_root_index()

    OUT_DIR.mkdir(exist_ok=True)
    cards = [item.to_card() for item in items]
    NEWS_JSON.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完了: {len(items)}件のページを docs/{date_iso}/ に生成しました")


def cmd_send():
    cards = json.loads(NEWS_JSON.read_text(encoding="utf-8"))
    print("LINEにFlexメッセージを送信中...")
    LineClient(os.environ["LINE_CHANNEL_ACCESS_TOKEN"]).send_flex(build_flex_message(cards), alt_text=CARD_TITLE)
    print("完了しました。")


def cmd_error():
    text = "本日のニュース配信でエラーが発生しました。GitHub Actionsのログを確認してください。"
    LineClient(os.environ["LINE_CHANNEL_ACCESS_TOKEN"]).send_text(text)


def main():
    commands = {"build": cmd_build, "send": cmd_send, "error": cmd_error}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print("使い方: python news_bot.py [build|send|error]", file=sys.stderr)
        sys.exit(2)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
