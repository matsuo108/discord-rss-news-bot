import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "feeds.json"
POSTED_URLS_PATH = ROOT_DIR / "data" / "posted_urls.json"

MAX_STORED_URLS_PER_CHANNEL = 200
MAX_POSTS_PER_RUN_PER_CHANNEL = 3
REQUEST_TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def summarize_text(client, title: str, link: str, summary_hint: str = "") -> str:
    if client is None:
        return ""

    prompt = f"""
次の記事を、日本語で2〜3文の短い要約にしてください。
- 誇張しない
- 未確認情報っぽいことは断定しない
- 40〜100文字程度を目安
- 1行で読みやすくまとめる
- 記事タイトルや補足から推測しすぎない

タイトル: {title}
URL: {link}
補足情報: {summary_hint}
""".strip()

    try:
        response = client.responses.create(
            model="gpt-5-mini",
            input=prompt,
        )
        text = getattr(response, "output_text", "") or ""
        return text.strip()
    except Exception as e:
        print(f"[WARN] Failed to summarize: {title} / {e}")
        return ""


def fetch_feed_entries(feed_url: str) -> List[Dict[str, Any]]:
    parsed = feedparser.parse(feed_url)

    if getattr(parsed, "bozo", 0):
        print(f"[WARN] Feed parse warning: {feed_url}")

    entries: List[Dict[str, Any]] = []
    for entry in parsed.entries:
        link = entry.get("link", "").strip()
        title = entry.get("title", "").strip()
        summary = entry.get("summary", "").strip()

        if not link or not title:
            continue

        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published_ts = 0
        if published_parsed:
            try:
                import calendar
                published_ts = calendar.timegm(published_parsed)
            except Exception:
                published_ts = 0

        entries.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "published_ts": published_ts,
            }
        )

    entries.sort(key=lambda x: x["published_ts"])
    return entries


def fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def is_noise_link(title: str, full_url: str) -> bool:
    """
    ニュース記事ではないリンク（カテゴリ、一覧ページなど）を除外する
    """
    noise_keywords = [
        "ニュース",
        "一覧",
        "カテゴリー",
        "カテゴリ",
        "すべて",
        "全て",
        "TOP",
        "トップ",
        "戻る",
    ]

    # タイトルがノイズっぽい
    if any(keyword in title for keyword in noise_keywords):
        return True

    # 一覧ページそのもの
    noise_urls = {
        "https://www.pokemon.co.jp/info",
        "https://www.pokemon.co.jp/info/",
        "https://idolmaster-official.jp/news",
        "https://idolmaster-official.jp/news/",
    }

    if full_url.rstrip("/") in {url.rstrip("/") for url in noise_urls}:
        return True

    # ポケモンのカテゴリページを除外
    if "pokemon.co.jp/info/cat_" in full_url:
        return True

    return False


def try_extract_entries_with_selectors(
    html: str,
    base_url: str,
    selectors: List[str],
) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    entries: List[Dict[str, Any]] = []

    for selector in selectors:
        nodes = soup.select(selector)
        print(f"[DEBUG] selector='{selector}' -> {len(nodes)} nodes")

        for node in nodes:
            href = node.get("href", "").strip()
            title = node.get_text(" ", strip=True)
        
            if not href or not title:
                continue
        
            full_url = urljoin(base_url, href)
        
            # ページ内リンク・一覧ページそのもの除外
            if full_url == base_url or "#" in href:
                continue
        
            # カテゴリやノイズっぽいリンクを除外
            if is_noise_link(title, full_url):
                continue
        
            entries.append(
                {
                    "title": title,
                    "link": full_url,
                    "summary": "",
                    "published_ts": 0,
                }
            )
        
        # 1つのセレクタで十分取れたらそれを採用
        if len(entries) >= 3:
            break

    return dedupe_entries(entries)


def dedupe_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen_links = set()

    for entry in entries:
        link = entry.get("link", "")
        title = entry.get("title", "")
        if not link or not title or link in seen_links:
            continue
        seen_links.add(link)
        deduped.append(entry)

    return deduped


def fetch_scrape_entries(channel_key: str, page_url: str) -> List[Dict[str, Any]]:
    html = fetch_html(page_url)
    print(f"[DEBUG] fetched html: {page_url} ({len(html)} chars)")

    # サイトごとの候補セレクタ
    selector_map = {
        "pokemon": [
            "main article a",
            "main li a",
            "main .news a",
            "main .archive a",
            "main a[href*='/info/']",
        ],
        "imas_million": [
            "main a[href*='/news/']",
            "article a[href*='/news/']",
            "section a[href*='/news/']",
            "a[href*='/news/']",
        ],
    }

    selectors = selector_map.get(
        channel_key,
        [
            "main a",
            "article a",
            "section a",
            "a",
        ],
    )

    entries = try_extract_entries_with_selectors(
        html=html,
        base_url=page_url,
        selectors=selectors,
    )

    print(f"[DEBUG] scrape result for {channel_key}: {len(entries)} entries")

    # デバッグしやすいように先頭数件だけログ
    for i, entry in enumerate(entries[:20], start=1):
        print(f"[DEBUG] {i:02d} | title={entry['title'][:100]} | link={entry['link']}")

    return entries


def fetch_pokemon_api_entries(api_url: str) -> List[Dict[str, Any]]:
    response = requests.get(
        api_url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    results = data.get("results", [])
    entries: List[Dict[str, Any]] = []

    for item in results:
        title = str(item.get("title", "")).strip()
        link = str(item.get("full_uniq") or item.get("uniq") or "").strip()
        summary_parts = []

        start_date = str(item.get("start_date", "")).strip()
        sub_text = str(item.get("txt_1", "")).strip()
        item_type = str(item.get("type", "")).strip()
        is_new = int(item.get("new", 0) or 0)

        if not title or not link:
            continue

        if link.startswith("/"):
            link = urljoin("https://www.pokemon.co.jp", link)

        # カテゴリなど不要URLを除外
        if "/info/cat_" in link:
            continue

        if start_date:
            summary_parts.append(start_date)
        if sub_text:
            summary_parts.append(sub_text)
        if item_type:
            summary_parts.append(f"[{item_type}]")
        if is_new:
            summary_parts.append("NEW")

        entries.append(
            {
                "title": title,
                "link": link,
                "summary": " / ".join(summary_parts),
                "published_ts": 0,
            }
        )

    print(f"[DEBUG] pokemon_api result: {len(entries)} entries")
    for i, entry in enumerate(entries[:20], start=1):
        print(
            f"[DEBUG] {i:02d} | "
            f"title={entry['title'][:100]} | "
            f"link={entry['link']}"
        )

    return dedupe_entries(entries)


def build_discord_message(channel_name: str, title: str, link: str, summary: str) -> Dict[str, Any]:
    description_lines = []
    if summary:
        description_lines.append(summary)
        description_lines.append("")
    description_lines.append(f"記事はこちら: {link}")

    return {
        "embeds": [
            {
                "title": f"📰 {title}",
                "description": "\n".join(description_lines),
                "footer": {"text": channel_name},
            }
        ]
    }


def post_to_discord(webhook_url: str, payload: Dict[str, Any]) -> None:
    response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()


def normalize_posted_urls(raw_data: Dict[str, Any], channel_keys: List[str]) -> Dict[str, List[str]]:
    normalized: Dict[str, List[str]] = {}
    for key in channel_keys:
        value = raw_data.get(key, [])
        if isinstance(value, list):
            normalized[key] = [str(v) for v in value]
        else:
            normalized[key] = []
    return normalized


def main() -> int:
    try:
        config = load_json(CONFIG_PATH)
        posted_urls_raw = load_json(POSTED_URLS_PATH)
    except Exception as e:
        print(f"[ERROR] Failed to load config files: {e}")
        return 1

    channel_keys = list(config.keys())
    posted_urls = normalize_posted_urls(posted_urls_raw, channel_keys)
    client = get_openai_client()

    for channel_key, channel_config in config.items():
        channel_name = channel_config.get("name", channel_key)
        webhook_env = channel_config.get("webhook_env", "")
        source_type = channel_config.get("type", "rss")

        webhook_url = os.getenv(webhook_env)
        if not webhook_url:
            print(f"[WARN] Skip {channel_key}: env var '{webhook_env}' is not set")
            continue

        seen_urls: Set[str] = set(posted_urls.get(channel_key, []))
        fetched_entries: List[Dict[str, Any]] = []

        try:
            if source_type == "rss":
                feed_urls = channel_config.get("feeds", [])
                for feed_url in feed_urls:
                    entries = fetch_feed_entries(feed_url)
                    print(f"[DEBUG] rss {feed_url}: {len(entries)} entries")
                    fetched_entries.extend(entries)

            elif source_type == "scrape":
                page_url = channel_config.get("url", "").strip()
                if not page_url:
                    print(f"[WARN] Skip {channel_key}: scrape url is empty")
                    continue

                fetched_entries = fetch_scrape_entries(
                    channel_key=channel_key,
                    page_url=page_url,
                )

            elif source_type == "pokemon_api":
                api_url = channel_config.get("url", "").strip()
                if not api_url:
                    print(f"[WARN] Skip {channel_key}: pokemon_api url is empty")
                    continue
            
                fetched_entries = fetch_pokemon_api_entries(api_url)
                
            else:
                print(f"[WARN] Skip {channel_key}: unknown type '{source_type}'")
                continue

        except Exception as e:
            print(f"[ERROR] Failed to fetch entries for {channel_key}: {e}")
            continue

        deduped = dedupe_entries(fetched_entries)

        new_entries = [entry for entry in deduped if entry["link"] not in seen_urls]
        posts_to_send = new_entries[:MAX_POSTS_PER_RUN_PER_CHANNEL]

        print(f"[INFO] {channel_key}: fetched={len(deduped)} new={len(posts_to_send)}")

        for entry in posts_to_send:
            summary = summarize_text(
                client=client,
                title=entry["title"],
                link=entry["link"],
                summary_hint=entry.get("summary", ""),
            )

            payload = build_discord_message(
                channel_name=channel_name,
                title=entry["title"],
                link=entry["link"],
                summary=summary,
            )

            try:
                post_to_discord(webhook_url, payload)
                print(f"[INFO] Posted: {entry['title']}")
                posted_urls[channel_key].append(entry["link"])
            except Exception as e:
                print(f"[ERROR] Failed to post to Discord: {entry['title']} / {e}")

        posted_urls[channel_key] = posted_urls[channel_key][-MAX_STORED_URLS_PER_CHANNEL:]

    try:
        save_json(POSTED_URLS_PATH, posted_urls)
    except Exception as e:
        print(f"[ERROR] Failed to save posted URLs: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
