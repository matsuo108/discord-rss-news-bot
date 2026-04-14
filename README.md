# Discord RSS News Bot

Discord に RSS の新着情報を自動投稿するシンプルな Bot です。  
GitHub Actions で定期実行し、投稿済みURLを保存して重複投稿を防ぎます。

OpenAI API Key を設定すると、記事の短い要約も一緒に投稿できます。  
API Key を設定しない場合は、タイトルとリンクのみ投稿します。

---

## 機能

- RSS フィードの新着記事取得
- Discord Webhook への自動投稿
- 投稿済みURLの保存による重複防止
- OpenAI を使った短文要約（任意）
- GitHub Actions による定期実行

---

## ディレクトリ構成

```text
.
├─ .github/workflows/rss-news.yml
├─ config/feeds.json
├─ data/posted_urls.json
├─ src/main.py
├─ requirements.txt
├─ .gitignore
├─ README.md
└─ LICENSE
