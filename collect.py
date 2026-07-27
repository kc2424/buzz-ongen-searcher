# -*- coding: utf-8 -*-
"""毎日の収集スクリプト（GitHub Actions から実行される）

server.py がローカルのSQLiteに貯めるのに対し、こちらは data/*.json に貯める。
JSONにする理由は2つ:
  1. Gitで差分が見える（いつ何位だったかが履歴に残る）
  2. raw.githubusercontent.com がCORS付きで配信してくれる
     → Base44などのフロントから直接 fetch できる

Python標準ライブラリのみ。pip install 不要。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
HISTORY_PATH = DATA / "history.json"
CHART_PATH = DATA / "chart.json"

CHART_URL = "https://rss.applemarketingtools.com/api/v2/jp/music/most-played/100/songs.json"
LOOKUP_URL = "https://itunes.apple.com/lookup"
# GitHub ActionsのIPはデータセンター扱いされ、独自UAだと塞き止められる/固まる
# ことがあるため、実ブラウザに近いUAを使う
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# GitHub Actions はUTCで動くので、日付は必ずJSTで決める
JST = timezone(timedelta(hours=9))

BUZZ_THRESHOLD = 5   # 何位以上上がったら「急上昇」とみなすか
FRESH_DAYS = 30      # リリース何日以内なら「新譜」バッジを付けるか
KEEP_DAYS = 90       # 履歴を何日分保持するか


def get_json(url, params=None, timeout=20, retries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                print(f"  ! 通信に失敗（{attempt}/{retries}回目）: {e} → 再試行します")
                time.sleep(5 * attempt)
    raise last_err


def fetch_previews(track_ids):
    """試聴URLを50件ずつまとめて引く。失敗しても収集は続行する。"""
    info = {}
    for i in range(0, len(track_ids), 50):
        chunk = track_ids[i:i + 50]
        try:
            data = get_json(LOOKUP_URL, {
                "id": ",".join(chunk), "country": "JP", "entity": "song",
            })
        except Exception as e:
            print(f"  ! 試聴URL取得に失敗（{i + 1}件目以降）: {e}")
            continue
        for r in data.get("results", []):
            tid = str(r.get("trackId", ""))
            if tid and r.get("previewUrl"):
                info[tid] = r["previewUrl"]
    return info


def load_history():
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return {"songs": {}, "snapshots": {}}


def days_since(iso_date, today):
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (today - d).days


def build_chart(history, today):
    """履歴から、フロントがそのまま表示できる形のチャートを組み立てる。"""
    dates = sorted(history["snapshots"].keys(), reverse=True)
    if not dates:
        return {"songs": [], "date": None, "compared_to": None,
                "history_days": 0, "buzz_threshold": BUZZ_THRESHOLD}

    latest = dates[0]
    prev = dates[1] if len(dates) > 1 else None
    cur_ranks = history["snapshots"][latest]
    prev_ranks = history["snapshots"].get(prev, {}) if prev else {}

    # 「初登場」判定用に、今日より前に一度でも出た曲を集める
    seen_before = set()
    for d in dates[1:]:
        seen_before.update(history["snapshots"][d].keys())

    songs = []
    for sid, rank in sorted(cur_ranks.items(), key=lambda kv: kv[1]):
        meta = history["songs"].get(sid, {})
        prev_rank = prev_ranks.get(sid)
        delta = (prev_rank - rank) if prev_rank is not None else None
        age = days_since(meta.get("releaseDate", ""), today)
        songs.append({
            "id": sid,
            "rank": rank,
            "name": meta.get("name", ""),
            "artist": meta.get("artist", ""),
            "artwork": meta.get("artwork", ""),
            "url": meta.get("url", ""),
            "preview": meta.get("preview", ""),
            "genre": meta.get("genre", ""),
            "releaseDate": meta.get("releaseDate", ""),
            "daysSinceRelease": age,
            "prevRank": prev_rank,
            "delta": delta,
            "isNew": bool(prev) and sid not in seen_before,
            "isFresh": age is not None and age <= FRESH_DAYS,
            "isBuzzing": delta is not None and delta >= BUZZ_THRESHOLD,
        })

    return {
        "songs": songs,
        "date": latest,
        "compared_to": prev,
        "history_days": len(dates),
        "buzz_threshold": BUZZ_THRESHOLD,
        "generatedAt": datetime.now(JST).isoformat(timespec="seconds"),
    }


def main():
    today_date = datetime.now(JST).date()
    today = today_date.isoformat()
    print(f"収集日（JST）: {today}")

    results = get_json(CHART_URL).get("feed", {}).get("results", [])
    if not results:
        raise SystemExit("チャートが空で返ってきました。中止します。")
    print(f"  {len(results)}曲を取得")

    previews = fetch_previews([r["id"] for r in results])
    print(f"  試聴URL: {len(previews)}件")

    history = load_history()
    snapshot = {}
    for rank, r in enumerate(results, start=1):
        sid = r["id"]
        snapshot[sid] = rank
        genres = [g["name"] for g in r.get("genres", [])
                  if g.get("name") and g["name"] != "ミュージック"]
        old = history["songs"].get(sid, {})
        history["songs"][sid] = {
            "name": r.get("name", ""),
            "artist": r.get("artistName", ""),
            "artwork": r.get("artworkUrl100", "").replace("100x100bb", "600x600bb"),
            "url": r.get("url", ""),
            # 今回引けなかった曲は、前回引けた試聴URLを引き継ぐ
            "preview": previews.get(sid) or old.get("preview", ""),
            "genre": " / ".join(genres),
            "releaseDate": r.get("releaseDate", ""),
        }
    history["snapshots"][today] = snapshot

    # 古い記録を捨てる。捨てた日にしか出ない曲のメタ情報も一緒に片付ける
    cutoff = (today_date - timedelta(days=KEEP_DAYS)).isoformat()
    history["snapshots"] = {d: s for d, s in history["snapshots"].items()
                            if d >= cutoff}
    alive = set()
    for s in history["snapshots"].values():
        alive.update(s.keys())
    history["songs"] = {k: v for k, v in history["songs"].items() if k in alive}

    DATA.mkdir(exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")
    chart = build_chart(history, today_date)
    CHART_PATH.write_text(
        json.dumps(chart, ensure_ascii=False, indent=1),
        encoding="utf-8")

    buzzing = sum(1 for s in chart["songs"] if s["isBuzzing"])
    print(f"  記録日数: {chart['history_days']}日分 / 急上昇: {buzzing}曲")
    print(f"  書き出し: {HISTORY_PATH.name}, {CHART_PATH.name}")


if __name__ == "__main__":
    main()
