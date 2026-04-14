import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import feedparser
import requests

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
- 記事タイトルから推測しすぎない

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


def fetch_feed_entries(feed_url: str) -> List[Dict[str, str]]:
    parsed = feedparser.parse(feed_url)

    if getattr(parsed, "bozo", 0):
        print(f"[WARN] Feed parse warning: {feed_url}")

    entries: List[Dict[str, str]] = []
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

    entries.sort(key=lambda x: x["published_ts"], reverse=False)
    return entries


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
        feed_urls = channel_config.get("feeds", [])

        webhook_url = os.getenv(webhook_env)
        if not webhook_url:
            print(f"[WARN] Skip {channel_key}: env var '{webhook_env}' is not set")
            continue

        seen_urls: Set[str] = set(posted_urls.get(channel_key, []))
        new_entries: List[Dict[str, str]] = []

        for feed_url in feed_urls:
            try:
                entries = fetch_feed_entries(feed_url)
            except Exception as e:
                print(f"[WARN] Failed to fetch feed {feed_url}: {e}")
                continue

            for entry in entries:
                if entry["link"] not in seen_urls:
                    new_entries.append(entry)

        deduped = []
        added_links = set()
        for entry in new_entries:
            if entry["link"] in added_links:
                continue
            added_links.add(entry["link"])
            deduped.append(entry)

        deduped.sort(key=lambda x: x["published_ts"])
        posts_to_send = deduped[:MAX_POSTS_PER_RUN_PER_CHANNEL]

        print(f"[INFO] {channel_key}: {len(posts_to_send)} new posts")

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
