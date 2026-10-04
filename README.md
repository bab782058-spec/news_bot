# 毎朝ニュース5選LINEボット(完全無料構成)

世界の政治・経済の重要ニュース5選を、毎朝7時(日本時間)にLINEへ自動配信するボットです。
使っているサービスはすべて無料枠の範囲で完結します。

## 使うもの(すべて無料)

- ニュース取得: NHK・BBC・Bloombergの無料RSS
- 要約AI: Gemini API(無料枠)
- LINE送信: LINE Messaging API(ブロードキャスト配信、月200通まで無料)
- 定期実行: GitHub Actions(無料枠で十分)

## セットアップ手順

### 1. Gemini APIキーを取得する

1. https://aistudio.google.com/ にアクセスし、Googleアカウントでログイン
2. 「Get API key」→「APIキーを作成」をクリック
3. 発行されたキーをメモしておく(後でGitHubのSecretsに登録します)
4. **支払い情報は登録しないこと**(登録すると無料枠の挙動が変わる場合があるため)

### 2. LINE公式アカウント + Messaging APIを準備する

1. https://developers.line.biz/ja/ で LINE Developers アカウントを作成
2. LINE Official Account Manager (https://manager.line.biz/) で、
   自分専用の公式アカウントを新規作成する(プランは「コミュニケーションプラン」= 無料のままでOK)
3. 作成した公式アカウントの管理画面 →「設定」→「Messaging API」から、
   Messaging APIを有効化する
4. 「チャネルアクセストークン(長期)」を発行し、メモしておく
5. 自分のLINEアプリで、その公式アカウントを友だち追加しておく
   (ブロードキャスト配信は「友だち全員」に届くため、自分だけが友だちの状態にしておく)

### 3. GitHubリポジトリを作る

1. 新しいリポジトリを作成(Private でOK)
2. このフォルダの中身(`news_bot.py`、`.github/workflows/daily-news.yml`)をそのままアップロード
   - `.github` フォルダごと、ルート直下に置くこと

### 4. Secretsを登録する

リポジトリの Settings → Secrets and variables → Actions → New repository secret から、以下の2つを登録:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | 手順1で取得したキー |
| `LINE_CHANNEL_ACCESS_TOKEN` | 手順2で取得したトークン |

### 5. 動作確認

1. GitHubリポジトリの「Actions」タブを開く
2. 「Daily News to LINE」ワークフローを選択
3. 右側の「Run workflow」ボタンで手動実行してみる
4. 数十秒後、LINEにメッセージが届けば成功

成功すれば、あとは毎朝7時に自動で届きます。

## メッセージのデザインについて

ただのテキストではなく、LINEの「Flexメッセージ」というカード形式で届きます。
5件すべてがiPhone(無印サイズ)のトーク画面1画面に収まるよう、コンパクトに設計しています。

- ヘッダー: 紺色の帯に「🌍 今日の重要ニュース5選」と日付
- 本文: ニュースごとに
  - 色付きの丸番号アイコン(1〜5で色が変化)
  - タイトル(1行、はみ出す場合は自動で「…」省略)
  - 一言あらすじ(1行、グレーの小さい文字)
  - 右端に「詳細›」リンク(タップすると元記事がブラウザで開く)
  - 記事の間に区切り線
- フッター: 小さく「Powered by Gemini」

「詳細›」をタップすると、その場で展開されるのではなく、**元記事のページがブラウザで開く**仕組みです(LINE公式アカウントはWebhookサーバーを常時立てていないbot構成のため、トーク内でその場展開する機能は組み込んでいません)。しっかり読みたい記事だけ、そこから深掘りできます。

色や文字サイズは `news_bot.py` の `build_flex_message` 関数内の `color`・`size` の値を変えるだけで調整できます(例: `"#2D3E50"` を別の16進カラーコードに変更)。
万が一Geminiの出力がうまくJSONにならずデザイン生成に失敗した場合は、自動的にシンプルなテキストメッセージにフォールバックする仕組みも入れてあります。

## カスタマイズしたくなったら

- ニュースソースを変えたい → `news_bot.py` の `RSS_FEEDS` に好きなRSSのURLを追加・削除
- 配信時刻を変えたい → `daily-news.yml` の `cron` を変更(UTC基準なので、JST希望時刻から9時間引いた値を指定)
- ニュース5選ではなく3選にしたい → `news_bot.py` の `summarize_with_gemini` 内のプロンプト文を書き換える
