"""
毎朝7時に、世界の政治・経済に関する重要ニュース5選を
デザインされたカード形式(LINE Flexメッセージ)でLINEに送るスクリプト。

必要な環境変数(GitHub Actionsのsecretsで設定する):
- GEMINI_API_KEY: Google AI StudioでGemini APIキーを発行して取得
- LINE_CHANNEL_ACCESS_TOKEN: LINE Developersコンソールで発行する「チャネルアクセストークン」

任意の環境変数:
- GEMINI_MODEL: 使うGeminiモデル名。未指定なら DEFAULT_GEMINI_MODEL を使う
  (モデルの提供終了に備え、コードを書き換えずに差し替えられるようにしてある)

使っている無料枠:
- ニュース取得: 無料RSS(BBC World, NHKニュース, Bloomberg Markets)
- 要約AI: Gemini API(無料枠)
- LINE送信: LINE Messaging API のブロードキャスト配信(コミュニケーションプラン / 月200通まで無料)
"""

import os
import sys
import json
import datetime
import feedparser
import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

# gemini-2.0-flash は2026年6月に提供終了、gemini-2.5-* も2026年10月16日に終了予定のため、
# 無料枠で使える後継の Flash-Lite 系を既定にしている。
# 動かない場合は Google AI Studio で現行のモデル名を確認し、
# GitHub Actionsの環境変数 GEMINI_MODEL(またはこの値)を差し替えること。
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL

RSS_FEEDS = [
    ("NHKニュース(主要)", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
    ("BBC World News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
]

MAX_ITEMS_PER_FEED = 10

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

# カードの左側に出す丸数字の背景色(1〜5番目で色を変える)
ACCENT_COLORS = ["#4A6FA5", "#5C8374", "#B08968", "#8E6C88", "#4A6FA5"]


def collect_headlines():
    collected = []
    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] {source_name} の取得に失敗: {e}", file=sys.stderr)
            continue

        entries = feed.entries[:MAX_ITEMS_PER_FEED]
        print(f"[INFO] {source_name}: {len(entries)}件取得")

        for entry in entries:
            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title:
                continue
            collected.append(
                f"[{source_name}] {title}\n概要: {summary}\nリンク: {link}"
            )

    if not collected:
        raise RuntimeError("どのRSSフィードからもニュースを取得できませんでした")

    return "\n\n".join(collected)


def parse_news_json(raw_text):
    """Geminiの出力テキストからニュースのJSON配列を取り出す"""
    cleaned = raw_text.strip()

    # 念のため、```json ... ``` のコードブロックで返ってきた場合は取り除く
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        cleaned = cleaned.strip()

    news_items = json.loads(cleaned)

    if not isinstance(news_items, list) or not news_items:
        raise RuntimeError(f"Geminiの出力が想定したJSON配列ではありません: {raw_text[:200]}")

    return news_items


def summarize_with_gemini(headlines_text):
    """集めた見出しをGeminiに渡し、構造化JSON(タイトル+要約)で5件を返させる"""
    prompt = f"""以下は、本日時点での複数のニュースソースの見出し一覧です。
各見出しには、末尾に「リンク: 」として元記事のURLが付いています。

この中から、世界の政治・経済において特に重要と考えられるニュースを5つ選んでください。
出力は、説明文やMarkdownを一切含めず、次の形式の**JSON配列のみ**にしてください。

[
  {{
    "title": "ニュースの見出し(18字程度、簡潔に、体言止めでもOK)",
    "overview": "一言あらすじ(25字程度の一文。詳細はリンク先で読む前提の、ごく短い概要)",
    "link": "そのニュースの元記事URL(入力の「リンク: 」の値をそのままコピーすること。改変しない)"
  }},
  ...(5件)
]

--- ニュース見出し一覧 ---
{headlines_text}
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    # APIキーはURLに埋め込まず、ヘッダーで渡す(ログやエラー文に残りにくくなる)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 出力をJSONに固定して、前置きや```付きの返答でパースが失敗するのを防ぐ
        "generationConfig": {"responseMimeType": "application/json"},
    }

    res = requests.post(url, headers=headers, json=payload, timeout=60)
    if res.status_code != 200:
        # 404ならモデル名の提供終了、429なら無料枠の上限超過の可能性が高い
        raise RuntimeError(
            f"Gemini APIエラー(model={GEMINI_MODEL}): {res.status_code} {res.text[:500]}"
        )
    data = res.json()

    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Geminiの応答解析に失敗しました: {data}") from e

    return parse_news_json(raw_text)[:5]


def build_date_header():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    weekday = WEEKDAY_JP[now.weekday()]
    return f"{now.year}年{now.month}月{now.day}日({weekday})"


def build_flex_message(news_items):
    """ニュース一覧から、カード型のFlexメッセージを組み立てる"""
    date_str = build_date_header()

    body_contents = []
    for i, item in enumerate(news_items):
        color = ACCENT_COLORS[i % len(ACCENT_COLORS)]
        number = i + 1
        link = item.get("link", "").strip()

        # タイトル行(折り返さず1行に収め、はみ出す場合は自動で...省略される)
        title_text = {
            "type": "text",
            "text": item.get("title", ""),
            "weight": "bold",
            "size": "sm",
            "wrap": False,
            "color": "#1A1A1A",
        }

        # 一言概要(1行、折り返さない)
        overview_text = {
            "type": "text",
            "text": item.get("overview", ""),
            "size": "xxs",
            "color": "#777777",
            "wrap": False,
            "margin": "xs",
        }

        text_column = {
            "type": "box",
            "layout": "vertical",
            "margin": "sm",
            "flex": 1,
            "contents": [title_text, overview_text],
        }

        number_badge = {
            "type": "box",
            "layout": "vertical",
            "width": "22px",
            "height": "22px",
            "backgroundColor": color,
            "cornerRadius": "11px",
            "justifyContent": "center",
            "alignItems": "center",
            "contents": [
                {
                    "type": "text",
                    "text": str(number),
                    "color": "#FFFFFF",
                    "size": "xxs",
                    "weight": "bold",
                    "align": "center",
                }
            ],
        }

        row_contents = [number_badge, text_column]

        # 詳細(元記事)へのリンクがあれば、右端に小さな「詳細›」を置き、タップで開けるようにする
        if link:
            row_contents.append(
                {
                    "type": "text",
                    "text": "詳細›",
                    "size": "xxs",
                    "color": "#4A6FA5",
                    "weight": "bold",
                    "align": "end",
                    "gravity": "center",
                    "action": {"type": "uri", "uri": link},
                }
            )

        item_box = {
            "type": "box",
            "layout": "horizontal",
            "margin": "md",
            "alignItems": "center",
            "contents": row_contents,
        }
        body_contents.append(item_box)

        if i < len(news_items) - 1:
            body_contents.append({"type": "separator", "margin": "md"})

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#2D3E50",
            "paddingAll": "20px",
            "contents": [
                {
                    "type": "text",
                    "text": "🌍 今日の重要ニュース5選",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "lg",
                },
                {
                    "type": "text",
                    "text": date_str,
                    "color": "#CCD6E0",
                    "size": "xs",
                    "margin": "sm",
                },
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

    return bubble


def send_line_flex(bubble, alt_text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messages": [
            {
                "type": "flex",
                "altText": alt_text,
                "contents": bubble,
            }
        ]
    }

    res = requests.post(url, headers=headers, json=payload, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"LINE送信に失敗しました: {res.status_code} {res.text}")


def send_line_text_fallback(text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": text[:4900]}]}
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"LINE送信に失敗しました: {res.status_code} {res.text}")


def main():
    print("ニュース収集中...")
    headlines_text = collect_headlines()

    print(f"Geminiで要約中...(model={GEMINI_MODEL})")
    try:
        news_items = summarize_with_gemini(headlines_text)
        bubble = build_flex_message(news_items)
        print("LINEにFlexメッセージを送信中...")
        send_line_flex(bubble, alt_text="本日の重要ニュース5選")
    except Exception as e:
        # Flex生成・送信で何か失敗したら、最低限テキストだけでも送る
        print(f"[WARN] Flexメッセージの生成/送信に失敗、テキストで代替送信します: {e}", file=sys.stderr)
        send_line_text_fallback("本日のニュース要約生成でエラーが発生しました。GitHub Actionsのログを確認してください。")
        raise

    print("完了しました。")


if __name__ == "__main__":
    main()
