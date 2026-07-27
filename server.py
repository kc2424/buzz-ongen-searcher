# -*- coding: utf-8 -*-
"""バズ音源サーチャー — 今バズってる音源を探すローカルツール

外部ライブラリ不要（Python標準ライブラリのみ）。

データ元（どちらもAPIキー不要・公式）:
  - Apple Music 日本 トップソング RSS
  - iTunes Lookup / Search API

順位は毎日SQLiteに記録され、前回記録との差分から「急上昇」を検出する。
"""

import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "buzz.db"
PORT = 8765

CHART_URL = "https://rss.applemarketingtools.com/api/v2/jp/music/most-played/100/songs.json"
LOOKUP_URL = "https://itunes.apple.com/lookup"
SEARCH_URL = "https://itunes.apple.com/search"
UA = "BuzzOngenSearcher/1.0 (personal use)"

# 順位が何位以上跳ねたら「急上昇」扱いにするか
BUZZ_THRESHOLD = 5
# リリースから何日以内なら「新譜」バッジを付けるか
FRESH_DAYS = 30


# --------------------------------------------------------------------------
# HTTP / データ取得
# --------------------------------------------------------------------------

def get_json(url, params=None, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def big_artwork(url):
    """100x100 のサムネURLを600x600に差し替える。"""
    if not url:
        return ""
    return url.replace("100x100bb", "600x600bb")


def fetch_previews(track_ids):
    """トラックIDのリストから試聴URLと再生時間をまとめて引く。50件ずつ。"""
    info = {}
    for i in range(0, len(track_ids), 50):
        chunk = track_ids[i:i + 50]
        try:
            data = get_json(LOOKUP_URL, {
                "id": ",".join(chunk),
                "country": "JP",
                "entity": "song",
            })
        except Exception as e:
            print(f"  ! 試聴URLの取得に失敗（{i + 1}件目以降）: {e}")
            continue
        for r in data.get("results", []):
            tid = str(r.get("trackId", ""))
            if tid:
                info[tid] = {
                    "preview": r.get("previewUrl") or "",
                    "duration": r.get("trackTimeMillis") or 0,
                }
    return info


# --------------------------------------------------------------------------
# データベース
# --------------------------------------------------------------------------

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS song (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                artist       TEXT NOT NULL,
                artwork      TEXT,
                url          TEXT,
                preview      TEXT,
                duration     INTEGER DEFAULT 0,
                genre        TEXT,
                release_date TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshot (
                taken_on TEXT NOT NULL,
                song_id  TEXT NOT NULL,
                rank     INTEGER NOT NULL,
                PRIMARY KEY (taken_on, song_id)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshot_song ON snapshot(song_id);
        """)


def refresh_chart():
    """最新チャートを取得して今日のスナップショットとして保存する。"""
    print("チャートを取得中 ...")
    data = get_json(CHART_URL)
    results = data.get("feed", {}).get("results", [])
    if not results:
        raise RuntimeError("チャートが空で返ってきました")

    print(f"  {len(results)}曲を取得。試聴URLを引いています ...")
    previews = fetch_previews([r["id"] for r in results])

    today = date.today().isoformat()
    with connect() as con:
        for rank, r in enumerate(results, start=1):
            sid = r["id"]
            extra = previews.get(sid, {})
            genres = [g["name"] for g in r.get("genres", [])
                      if g.get("name") and g["name"] != "ミュージック"]
            con.execute("""
                INSERT INTO song (id, name, artist, artwork, url, preview,
                                  duration, genre, release_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    artist = excluded.artist,
                    artwork = excluded.artwork,
                    url = excluded.url,
                    preview = CASE WHEN excluded.preview != ''
                                   THEN excluded.preview ELSE song.preview END,
                    duration = excluded.duration,
                    genre = excluded.genre,
                    release_date = excluded.release_date
            """, (
                sid, r.get("name", ""), r.get("artistName", ""),
                big_artwork(r.get("artworkUrl100", "")), r.get("url", ""),
                extra.get("preview", ""), extra.get("duration", 0),
                " / ".join(genres), r.get("releaseDate", ""),
            ))
            con.execute(
                "INSERT OR REPLACE INTO snapshot (taken_on, song_id, rank) "
                "VALUES (?, ?, ?)", (today, sid, rank))

    print(f"  完了。{today} のランキングとして記録しました。")
    return today


def snapshot_dates():
    with connect() as con:
        rows = con.execute(
            "SELECT DISTINCT taken_on FROM snapshot ORDER BY taken_on DESC"
        ).fetchall()
    return [r["taken_on"] for r in rows]


def days_since(iso_date):
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - d).days


def build_chart():
    """最新チャート＋前回比の差分を組み立てて返す。"""
    dates = snapshot_dates()
    if not dates:
        return {"songs": [], "date": None, "compared_to": None}

    latest = dates[0]
    prev = dates[1] if len(dates) > 1 else None

    with connect() as con:
        rows = con.execute("""
            SELECT s.rank, so.*
            FROM snapshot s JOIN song so ON so.id = s.song_id
            WHERE s.taken_on = ?
            ORDER BY s.rank
        """, (latest,)).fetchall()

        prev_ranks = {}
        if prev:
            for r in con.execute(
                    "SELECT song_id, rank FROM snapshot WHERE taken_on = ?",
                    (prev,)):
                prev_ranks[r["song_id"]] = r["rank"]

        seen_before = set()
        if prev:
            for r in con.execute(
                    "SELECT DISTINCT song_id FROM snapshot WHERE taken_on < ?",
                    (latest,)):
                seen_before.add(r["song_id"])

    songs = []
    for row in rows:
        sid = row["id"]
        prev_rank = prev_ranks.get(sid)
        delta = (prev_rank - row["rank"]) if prev_rank is not None else None
        age = days_since(row["release_date"])
        songs.append({
            "id": sid,
            "rank": row["rank"],
            "name": row["name"],
            "artist": row["artist"],
            "artwork": row["artwork"],
            "url": row["url"],
            "preview": row["preview"],
            "genre": row["genre"],
            "releaseDate": row["release_date"],
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
    }


def build_history(song_id):
    with connect() as con:
        rows = con.execute(
            "SELECT taken_on, rank FROM snapshot WHERE song_id = ? "
            "ORDER BY taken_on", (song_id,)).fetchall()
        song = con.execute(
            "SELECT * FROM song WHERE id = ?", (song_id,)).fetchone()
    return {
        "song": dict(song) if song else None,
        "points": [{"date": r["taken_on"], "rank": r["rank"]} for r in rows],
    }


def search_songs(q):
    """iTunes検索。チャート入りしている曲には現在の順位を添える。"""
    data = get_json(SEARCH_URL, {
        "term": q, "country": "JP", "media": "music",
        "entity": "song", "limit": 30, "lang": "ja_jp",
    })

    dates = snapshot_dates()
    ranks = {}
    if dates:
        with connect() as con:
            for r in con.execute(
                    "SELECT song_id, rank FROM snapshot WHERE taken_on = ?",
                    (dates[0],)):
                ranks[r["song_id"]] = r["rank"]

    out = []
    for r in data.get("results", []):
        sid = str(r.get("trackId", ""))
        out.append({
            "id": sid,
            "name": r.get("trackName", ""),
            "artist": r.get("artistName", ""),
            "artwork": big_artwork(r.get("artworkUrl100", "")),
            "url": r.get("trackViewUrl", ""),
            "preview": r.get("previewUrl") or "",
            "genre": r.get("primaryGenreName", ""),
            "releaseDate": (r.get("releaseDate") or "")[:10],
            "daysSinceRelease": days_since(r.get("releaseDate")),
            "rank": ranks.get(sid),
            "hasHistory": bool(ranks.get(sid)),
        })
    return {"songs": out, "query": q}


# --------------------------------------------------------------------------
# Web サーバー
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # アクセスログは出さない

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                html = (BASE / "index.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")

            elif path == "/api/chart":
                self._json(build_chart())

            elif path == "/api/history":
                sid = query.get("id", [""])[0]
                self._json(build_history(sid))

            elif path == "/api/search":
                q = query.get("q", [""])[0].strip()
                if not q:
                    self._json({"songs": [], "query": ""})
                else:
                    self._json(search_songs(q))

            elif path == "/api/refresh":
                refresh_chart()
                self._json(build_chart())

            else:
                self._json({"error": "not found"}, 404)

        except urllib.error.URLError as e:
            self._json({"error": f"ネットに接続できませんでした: {e}"}, 502)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 52)
    print("  バズ音源サーチャー")
    print("=" * 52)

    init_db()

    today = date.today().isoformat()
    if today not in snapshot_dates():
        try:
            refresh_chart()
        except Exception as e:
            print(f"  ! 取得に失敗しました: {e}")
            print("    （オフラインでも過去の記録は閲覧できます）")
    else:
        print(f"今日（{today}）のランキングは取得済みです。")

    days = len(snapshot_dates())
    print(f"記録日数: {days}日分")
    if days < 2:
        print("※ 急上昇の判定は2日目以降に出ます。明日また起動してください。")

    url = f"http://127.0.0.1:{PORT}/"

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # 二重起動。既に動いている方をブラウザで開くだけにする。
        print("\nすでに起動しているので、そちらをブラウザで開きます。")
        webbrowser.open(url)
        return

    print(f"\nブラウザで開きます → {url}")
    print("終了するには、この黒い画面で Ctrl+C を押してください。\n")

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。")
        server.shutdown()


if __name__ == "__main__":
    main()
