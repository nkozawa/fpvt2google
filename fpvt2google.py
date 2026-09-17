#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FPVTrackside のレース状況を Google 側（Apps Script 等）へ中継するプログラム。

機能
  1. FPVTrackside の「Gate / LED POST 通知」(ExtensionMode, HTTP PUT) を受信し、
     RaceStart と DetectionExt（ラップ確定のみ）を整形して lap 用 URL へ POST する
  2. イベントフォルダの Stages.json を監視し、変化したら順位表を standings 用 URL へ POST する
     （Stages.json の Standings は Lua スクリプト standings() の出力そのもの）
  3. 同じ内容をローカルの Web ページにも表示する（インターネット不通時のフォールバック）

Python 3.8 以降・標準ライブラリのみ。macOS / Windows 共通。

使い方
  python3 fpvt2google.py --init                # 設定ファイルの雛形を作る
  python3 fpvt2google.py                       # 起動（fpvt2google.json を読む）
  python3 fpvt2google.py --dry-run             # 送信せずに内容をログへ
  python3 fpvt2google.py --record cap.jsonl    # 受信した生イベントを録画
  python3 fpvt2google.py --replay cap.jsonl    # 録画を再生（Google 無しで開発）

FPVTrackside 側の準備
  プロファイル設定「Gate / LED POST notifications」で
    ExtensionMode    = true
    NotificationURL  = http://127.0.0.1:8765/
  を設定してアプリを再起動する。
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

CONFIG_FILENAME = "fpvt2google.json"

DEFAULT_CONFIG = {
    # --- 受信（FPVTrackside の NotificationURL 先に立てる） ---
    "listen_host": "0.0.0.0",
    "listen_port": 8765,
    # --- 送信先（Google Apps Script の Web アプリ URL） ---
    "google_lap_url": "",
    "google_standings_url": "",
    # --- 表示用のローカルページ（0 で無効） ---
    "dashboard_port": 8766,
    # --- チャンネル → 表示位置（pos）。キーは短縮名(F3)/生のband名(Fatshark3)/周波数 ---
    "channel_pos": {"E2": 1, "E1": 2, "F3": 3, "F5": 4},
    "band_short": {},              # band 名の読み替えを追加する場合（既定は BAND_SHORT）
    # --- Stages.json の場所（空なら Hello の paths / 既定パスから自動特定） ---
    "events_dir": "",
    "event_id": "",
    "script_format": "ladder_finals.lua",
    "stages_poll_sec": 1.0,
    "send_initial_standings": True,
    # standings の POST 本体: full=Stages.json 全体 / stage=該当ステージ / standings=順位表だけ
    "standings_payload": "full",
    # --- 送信の節度 ---
    "batch_window_sec": 0.25,      # 同じ選手・同じ周回の重複だけを最新1件にまとめる窓
    # ラップ POST の並列数（選手毎の順序は維持）。既定は直列。
    # Apps Script 側が並行実行をさばけない（ロック待ち・429・タイムアウト）と
    # かえって失敗が増えるので、上げる場合は Google 側の実行ログを確認してから。
    "lap_senders": 1,
    "post_timeout_sec": 10.0,
    "lap_retries": 1,
    "standings_retries": 5,
    # --- 表示 ---
    "decimal_places": 2,           # Hello.decimalPlaces が届けばそれで上書き
    # --- 開発用 ---
    "record": "",
    "replay": "",
    "replay_speed": 1.0,
    "dry_run": False,
    "log_file": "",
}


# --------------------------------------------------------------------------- #
# ログ
# --------------------------------------------------------------------------- #
class Logger:
    def __init__(self, path=None):
        self.lock = threading.Lock()
        self.fh = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with self.lock:
            print(line, flush=True)
            if self.fh:
                self.fh.write(line + "\n")
                self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


# --------------------------------------------------------------------------- #
# イベントの整形
# --------------------------------------------------------------------------- #
# FPVTrackside の band 名 → 短縮名（アプリの Channels.json の ShortBand に対応）
BAND_SHORT = {
    "a": "A", "b": "B", "e": "E",
    "fatshark": "F", "raceband": "R", "lowband": "L",
    "hdzero": "Z", "walksnail": "W",
    "diatone": "D", "djifpvhd": "D", "djio3": "D", "djio4": "D",
}


def channel_key(channel, band_short=None):
    """ChannelInfo → 'F3' / 'R1' のような短縮チャンネル名

    FPVTrackside は band に長い名前（"Fatshark" など）を入れてくるので、
    Channels.json の ShortBand に対応する短縮名へ読み替える。
    """
    if not isinstance(channel, dict):
        return "" if channel is None else str(channel)

    table = band_short or BAND_SHORT
    short = channel.get("shortBand") or channel.get("ShortBand")
    number = channel.get("number")
    if number is None:
        number = channel.get("Number")
    name = channel.get("displayName") or channel.get("DisplayName")

    if short:
        return "%s%s" % (short, "" if number is None else number)
    if name:
        return str(name)

    band = channel.get("band")
    if band is None:
        band = channel.get("Band")
    if band is not None:
        text = str(band).strip()
        abbr = table.get(text.lower())
        if abbr is None:
            # 未知のバンドは先頭1文字で代用（例: 'RaceBand' → 'R'）
            abbr = text[:1].upper()
        return "%s%s" % (abbr, "" if number is None else number)

    if channel.get("frequency"):
        return str(channel["frequency"])
    return ""


def pos_candidates(channel, key):
    """channel_pos を引くときの候補キー（短縮名 → 生の band 名 → 周波数）"""
    candidates = [key]
    if isinstance(channel, dict):
        band = channel.get("band") or channel.get("Band")
        number = channel.get("number")
        if number is None:
            number = channel.get("Number")
        if band is not None and number is not None:
            candidates.append("%s%s" % (band, number))
        frequency = channel.get("frequency")
        if frequency:
            candidates.append(str(frequency))
    return candidates


def lookup_pos(table, candidates):
    """channel_pos から pos を引く。大文字小文字は区別しない。"""
    for candidate in candidates:
        if candidate in table:
            return table[candidate]
    lowered = {str(k).lower(): v for k, v in table.items()}
    for candidate in candidates:
        if str(candidate).lower() in lowered:
            return lowered[str(candidate).lower()]
    return None


def timestamp(when=None):
    """POST するデータを作成した時刻（ローカルタイム、yyyy-mm-dd hh:mm:ss）"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def _round(value, decimals):
    if value is None:
        return None
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return None


def shape_race_start(ev):
    """RaceStart。選手リストは含まれないイベントなので round/race を必ず添える。"""
    return {
        "timestamp": timestamp(),
        "type": "RaceStart",
        "round": ev.get("round"),
        "race": ev.get("race"),
        "raceType": ev.get("raceType"),
        "actualStart": ev.get("actualStart"),
    }


def shape_detection(ev, cfg, decimals):
    """DetectionExt（ラップ確定）→ Google 向け JSON

    lapNumber はアプリの Detection.LapNumber そのままで、
    **完了周回数**（holeshot 通過だけが 0）。実データで確認済みなので補正しない。
    """
    channel = ev.get("channel")
    key = channel_key(channel, cfg.get("band_short"))
    return {
        "timestamp": timestamp(),
        "type": "DetectionExt",
        "pos": lookup_pos(cfg["channel_pos"], pos_candidates(channel, key)),
        "channel": key,
        "pilot": ev.get("pilotName"),
        "lap": ev.get("lapNumber"),
        "holeshot": ev.get("lapNumber") == 0,
        "total": _round(ev.get("raceTime"), decimals),
        "laptime": _round(ev.get("lapTimeSoFar"), decimals),
        "round": ev.get("round"),
        "race": ev.get("race"),
        "position": ev.get("position"),
        "finished": bool(ev.get("raceFinishedForPilot")),
    }


def pick_stage(stages, script_format):
    """Stages.json（配列）から順位表を持つステージを選ぶ"""
    if not isinstance(stages, list):
        return None
    fallback = None
    for stage in stages:
        card = (stage or {}).get("Standings")
        if not card or not card.get("Rows"):
            continue
        if script_format and stage.get("ScriptFormatFilename") == script_format:
            return stage
        fallback = stage
    return fallback


def classify_standings(card):
    """Lua の順位表が予選フェーズか勝ち上がり/決勝フェーズかを判定する

    ladder_finals.lua は予選中 Headings=[Laps,Time] の2列、
    勝ち上がり開始後は [Laps,Time,Status] の3列を返す。
    """
    headings = card.get("Headings") or []
    if len(headings) >= 3:
        return "final"
    for row in card.get("Rows") or []:
        values = row.get("Values") or []
        if values:
            text = str(values[-1])
            if "ladder" in text or "final" in text or text == "cut":
                return "final"
    return "qualify"


def build_standings_payload(stages, cfg):
    """Stages.json の中身 → POST する JSON（変化が無ければ None）"""
    stage = pick_stage(stages, cfg["script_format"])
    if stage is None:
        return None
    card = stage.get("Standings") or {}
    kind = classify_standings(card)
    mode = cfg["standings_payload"]
    payload = {"timestamp": timestamp(), "Type": kind}
    if mode == "standings":
        payload["name"] = stage.get("Name")
        payload["standings"] = card
    elif mode == "stage":
        payload["stage"] = stage
    else:
        payload["stages"] = stages
    return payload


# --------------------------------------------------------------------------- #
# HTTP 送信（Apps Script は 302 でリダイレクトするため POST を張り直す）
# --------------------------------------------------------------------------- #
class _RepostRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.data is not None and code in (301, 302, 303, 307, 308):
            new = urlrequest.Request(newurl, data=req.data, method="POST")
            new.add_header("Content-Type", req.get_header("Content-type") or "application/json")
            return new
        return urlrequest.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def post_json(url, payload, timeout, opener):
    """POST して (status, body) を返す。失敗は例外。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read(512)


# --------------------------------------------------------------------------- #
# 中継本体
# --------------------------------------------------------------------------- #
class Relay:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.stop = threading.Event()
        self.opener = urlrequest.build_opener(_RepostRedirect)

        self.lap_q = queue.Queue()
        self.standing_q = queue.Queue(maxsize=8)

        # ラップは選手毎のキューに分けて並列送信する（同じ選手は常に同じ worker
        # なので周回順は保たれる）。POST が遅くても他の選手の送信を止めない。
        self.lap_senders = max(1, int(cfg.get("lap_senders") or 1))
        self.lap_out = [queue.Queue() for _ in range(self.lap_senders)]
        self.lap_openers = [urlrequest.build_opener(_RepostRedirect)
                            for _ in range(self.lap_senders)]
        self._lap_busy = [False] * self.lap_senders
        self._lap_done = threading.Event()
        self._threads = []

        self.lock = threading.Lock()
        self.hello = {}
        self.heat = {}
        self.board = OrderedDict()      # key -> 最新のラップ状態
        self.standings = {}             # ローカル表示用の最新順位表
        self.stats = {"recv": 0, "lap_sent": 0, "lap_failed": 0,
                      "standings_sent": 0, "standings_failed": 0,
                      "sector_skipped": 0, "invalid_skipped": 0, "dup_skipped": 0}
        self._seen = OrderedDict()      # detectionId の重複排除
        self._record_fh = None
        if cfg["record"]:
            self._record_fh = open(cfg["record"], "a", encoding="utf-8")
        self._stages_sig = None

    # ---------- 起動・停止 ---------- #
    def start(self):
        threads = []
        self.receiver = self._serve(self.cfg["listen_host"], self.cfg["listen_port"],
                                    _ReceiverHandler, "receiver")
        threads.append(threading.Thread(target=self.receiver.serve_forever,
                                        kwargs={"poll_interval": 0.2}, daemon=True))
        if self.cfg["dashboard_port"]:
            self.dashboard = self._serve(self.cfg["listen_host"], self.cfg["dashboard_port"],
                                         _DashboardHandler, "dashboard")
            threads.append(threading.Thread(target=self.dashboard.serve_forever,
                                            kwargs={"poll_interval": 0.2}, daemon=True))
        for i in range(self.lap_senders):
            threads.append(threading.Thread(target=self._lap_worker, args=(i,), daemon=True))
        for target in (self._lap_sender, self._standings_sender, self._stages_watcher):
            threads.append(threading.Thread(target=target, daemon=True))
        for t in threads:
            t.start()
        self._threads = threads
        return threads

    def _serve(self, host, port, handler_cls, label):
        handler_cls.relay = self
        server = ThreadingHTTPServer((host, port), handler_cls)
        server.daemon_threads = True
        self.log("%s listening on %s:%d" % (label, host, port))
        return server

    def shutdown(self):
        self.stop.set()
        for name in ("receiver", "dashboard"):
            server = getattr(self, name, None)
            if server:
                server.shutdown()
                server.server_close()
        # 送信キューに残ったラップは送り切ってから抜ける
        deadline = time.time() + 10.0
        for t in self._threads:
            t.join(timeout=max(0.0, deadline - time.time()))
        left = self.lap_q.qsize() + sum(q.qsize() for q in self.lap_out)
        if left:
            self.log("警告: 送信できなかったラップが %d 件あります。POST が遅いか失敗しています。"
                     "lap_senders を増やす／lap_retries を見直す／Google 側の実行時間を確認してください"
                     % left)
        if self._record_fh:
            self._record_fh.close()
        self.log("stopped")

    # ---------- 受信 ---------- #
    def handle_event(self, ev):
        if not isinstance(ev, dict):
            return
        if self._record_fh:
            self._record_fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._record_fh.flush()

        kind = ev.get("type")
        with self.lock:
            self.stats["recv"] += 1

        if kind == "Hello":
            self._on_hello(ev)
        elif kind == "RaceStart":
            self._on_race_start(ev)
        elif kind == "DetectionExt":
            self._on_detection(ev)
        else:
            pass    # RaceLoaded / RaceEnd / RaceResult / StageRanking などは中継しない

    def _on_hello(self, ev):
        with self.lock:
            self.hello = ev
        paths = ev.get("paths") or {}
        self.log("Hello: fpvt %s / %s / events=%s / decimalPlaces=%s"
                 % (ev.get("fpvtVersion"), ev.get("platform"),
                    paths.get("eventsDirectory"), ev.get("decimalPlaces")))

    def _on_race_start(self, ev):
        payload = shape_race_start(ev)
        with self.lock:
            self.heat = {"round": ev.get("round"), "race": ev.get("race"),
                         "raceType": ev.get("raceType"),
                         "startedAt": ev.get("actualStart") or ev.get("ts"),
                         "startedMono": time.time()}
            self.board = OrderedDict()      # 新しいヒートなのでラップ表示をクリア
        self.log("RaceStart round=%s race=%s" % (ev.get("round"), ev.get("race")))
        self.lap_q.put(payload)

    def _on_detection(self, ev):
        if not ev.get("isLapEnd"):
            with self.lock:
                self.stats["sector_skipped"] += 1
            return
        if ev.get("valid") is False:
            with self.lock:
                self.stats["invalid_skipped"] += 1
            return
        did = ev.get("detectionId")
        if did:
            with self.lock:
                if did in self._seen:
                    self.stats["dup_skipped"] += 1
                    return
                self._seen[did] = True
                while len(self._seen) > 4096:
                    self._seen.popitem(last=False)

        payload = shape_detection(ev, self.cfg, self.decimals())
        key = (payload["round"], payload["race"],
               payload["pos"] if payload["pos"] is not None else payload["pilot"])
        with self.lock:
            self.board[key] = dict(payload, updatedAt=time.time())
        self.lap_q.put(payload)

    def decimals(self):
        with self.lock:
            value = self.hello.get("decimalPlaces")
        return value if isinstance(value, int) else self.cfg["decimal_places"]

    # ---------- Stages.json 監視 ---------- #
    def stages_path(self):
        if self.cfg["event_id"]:
            base = self._events_dir()
            return Path(base) / self.cfg["event_id"] / "Stages.json" if base else None

        base = self._events_dir()
        if not base or not os.path.isdir(base):
            return None
        newest, newest_mtime = None, -1.0
        try:
            entries = os.listdir(base)
        except OSError:
            return None
        for name in entries:
            candidate = Path(base) / name / "Stages.json"
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = candidate, mtime
        return newest

    def _events_dir(self):
        if self.cfg["events_dir"]:
            return self.cfg["events_dir"]
        with self.lock:
            paths = self.hello.get("paths") or {}
        if paths.get("eventsDirectory"):
            return paths["eventsDirectory"]
        return str(Path.home() / "Documents" / "FPVTrackside" / "events")

    def _stages_watcher(self):
        pending = None
        while not self.stop.is_set():
            try:
                path = self.stages_path()
                if path is not None:
                    info = path.stat()
                    sig = (str(path), info.st_mtime_ns, info.st_size)
                    # 2回連続で同じ状態になってから読む（アプリの書込途中を避ける）
                    if sig == pending and sig != self._stages_sig:
                        first = self._stages_sig is None
                        if not first or self.cfg["send_initial_standings"]:
                            if self._push_stages(path):
                                self._stages_sig = sig
                        else:
                            self._stages_sig = sig
                    pending = sig
            except FileNotFoundError:
                pending = None
            except Exception as exc:                      # 監視は止めない
                self.log("stages watcher error: %r" % (exc,))
            self.stop.wait(self.cfg["stages_poll_sec"])

    def _push_stages(self, path):
        """Stages.json を読んで送信キューに積む。True = ファイルを読めた"""
        try:
            with open(path, encoding="utf-8") as fh:
                stages = json.load(fh)
        except (OSError, ValueError) as exc:
            self.log("cannot read %s: %r" % (path, exc))
            return False
        payload = build_standings_payload(stages, self.cfg)
        if payload is None:
            return True                                   # 順位表はまだ無い
        with self.lock:
            card = (pick_stage(stages, self.cfg["script_format"]) or {}).get("Standings") or {}
            self.standings = {
                "type": payload.get("Type"),
                "headings": card.get("Headings") or [],
                "rows": [[r.get("Name")] + list(r.get("Values") or [])
                         for r in (card.get("Rows") or [])],
                "source": str(path),
                "updatedAt": time.time(),
            }
        while True:                                        # 最新だけ残す
            try:
                self.standing_q.get_nowait()
            except queue.Empty:
                break
        self.standing_q.put(payload)
        self.log("Stages.json changed → Type=%s (%s)" % (payload.get("Type"), path.name))
        return True

    # ---------- 送信 ---------- #
    def _send(self, url, payload, retries, label, opener=None):
        if not url:
            return False
        opener = opener or self.opener
        if self.cfg["dry_run"]:
            self.log("[dry-run %s] %s" % (label, json.dumps(payload, ensure_ascii=False)))
            with self.lock:
                self.stats[label + "_sent"] += 1
            return True
        attempt = 0
        while attempt <= max(0, retries - 1):
            attempt += 1
            try:
                status, _body = post_json(url, payload, self.cfg["post_timeout_sec"], opener)
                if 200 <= status < 300:
                    with self.lock:
                        self.stats[label + "_sent"] += 1
                    return True
                raise RuntimeError("HTTP %d" % status)
            except (urlerror.URLError, OSError, RuntimeError) as exc:
                if attempt < retries:
                    time.sleep(min(2 ** attempt * 0.5, 8.0))
                else:
                    with self.lock:
                        self.stats[label + "_failed"] += 1
                    self.log("%s POST failed (%d tries): %r payload=%s"
                             % (label, attempt, exc,
                                json.dumps(payload, ensure_ascii=False)[:200]))
        return False

    def _pilot_key(self, payload):
        """選手を表すキー（ラップの振り分け先を決める。同じ選手は同じ worker へ）

        `pos` だけに頼ると `channel_pos` の設定ミスで別選手が混ざるため名前も入れる。
        """
        return (payload.get("round"), payload.get("race"),
                payload.get("pilot"), payload.get("pos"))

    def _lap_key(self, payload):
        """間引きの単位。**lap を必ず含める**こと。

        含めないと同一選手が窓内で2周したときに後だけが届き、Google 側の
        lapNumber が飛び飛びになる（POST が遅れてキューが溜まると多発する）。
        """
        return self._pilot_key(payload) + (payload.get("lap"),)

    def _lap_sender(self):
        """lap_q を窓でまとめ、選手毎の送信キューへ振り分ける（POST は _lap_worker）"""
        pending = OrderedDict()
        deadline = None
        window = max(0.0, self.cfg["batch_window_sec"])
        while not self.stop.is_set():
            try:
                item = self.lap_q.get(timeout=0.05)
            except queue.Empty:
                item = None
            if item is not None:
                if item.get("type") == "DetectionExt":
                    pending[self._lap_key(item)] = item    # 同じ周回の重複だけ最新で上書き
                    if deadline is None and window > 0:
                        deadline = time.time() + window
                else:
                    self._dispatch_laps(pending)           # レース開始は即送出
                    deadline = None
                    self._await_lap_workers()              # 前のレースのラップより後に送る
                    self._send(self.cfg["google_lap_url"], item, self.cfg["lap_retries"], "lap")
            if pending and (window <= 0 or (deadline is not None and time.time() >= deadline)):
                self._dispatch_laps(pending)
                deadline = None
        self._dispatch_laps(pending)
        self._lap_done.set()                               # worker はこれを見て送り切って終了

    def _dispatch_laps(self, pending):
        """溜まったラップを worker へ配る（選手単位で振り分けるので周回順は保たれる）"""
        n = len(self.lap_out)
        while pending:
            _key, payload = pending.popitem(last=False)
            self.lap_out[hash(self._pilot_key(payload)) % n].put(payload)

    def _lap_worker(self, index):
        out = self.lap_out[index]
        opener = self.lap_openers[index]
        while not (self._lap_done.is_set() and out.empty()):
            try:
                payload = out.get(timeout=0.05)
            except queue.Empty:
                continue
            with self.lock:
                self._lap_busy[index] = True
            try:
                self._send(self.cfg["google_lap_url"], payload,
                           self.cfg["lap_retries"], "lap", opener)
            finally:
                with self.lock:
                    self._lap_busy[index] = False

    def _await_lap_workers(self, timeout=30.0):
        """ラップの POST が全部はけるまで待つ"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                busy = any(self._lap_busy)
            if not busy and all(q.empty() for q in self.lap_out):
                return
            time.sleep(0.01)

    def _standings_sender(self):
        while not self.stop.is_set():
            try:
                payload = self.standing_q.get(timeout=0.2)
            except queue.Empty:
                continue
            while True:                                    # 溜まっていれば最新だけ
                try:
                    payload = self.standing_q.get_nowait()
                except queue.Empty:
                    break
            self._send(self.cfg["google_standings_url"], payload,
                       self.cfg["standings_retries"], "standings")

    # ---------- ローカル表示用スナップショット ---------- #
    def snapshot(self):
        with self.lock:
            heat = dict(self.heat)
            board = [dict(v) for v in self.board.values()]
            standings = dict(self.standings)
            stats = dict(self.stats)
            hello = {"fpvtVersion": self.hello.get("fpvtVersion"),
                     "platform": self.hello.get("platform"),
                     "eventsDirectory": (self.hello.get("paths") or {}).get("eventsDirectory")}
        board.sort(key=lambda row: (row.get("pos") is None, row.get("pos") or 0,
                                    -(row.get("lap") or 0)))
        if heat.get("startedMono"):
            heat["elapsed"] = round(time.time() - heat["startedMono"], 1)
        return {"now": time.time(), "hello": hello, "heat": heat, "laps": board,
                "standings": standings, "stats": stats,
                "config": {"channel_pos": self.cfg["channel_pos"],
                           "google_lap_url": bool(self.cfg["google_lap_url"]),
                           "google_standings_url": bool(self.cfg["google_standings_url"])}}

    # ---------- 録画の再生 ---------- #
    def replay(self, path, speed):
        events = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    self.log("skip bad line in replay file")
        self.log("replay %d events from %s (speed x%s)" % (len(events), path, speed))
        previous = None
        for ev in events:
            if self.stop.is_set():
                break
            stamp = ev.get("ts")
            if previous and stamp and speed > 0:
                gap = _iso_delta(previous, stamp)
                if gap:
                    self.stop.wait(gap / speed)
            previous = stamp
            self.handle_event(ev)


def _iso_delta(first, second):
    try:
        a = time.strptime(first.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
        b = time.strptime(second.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except (ValueError, AttributeError):
        return 0.0
    return max(0.0, time.mktime(b) - time.mktime(a))


# --------------------------------------------------------------------------- #
# HTTP ハンドラ
# --------------------------------------------------------------------------- #
class _Base(BaseHTTPRequestHandler):
    relay = None
    protocol_version = "HTTP/1.1"
    server_version = "fpvt2google/1.0"

    def log_message(self, *_args):        # アクセスログは抑止
        pass

    def _respond(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class _ReceiverHandler(_Base):
    """FPVTrackside の PUT を受けて即 200 を返し、処理はキューに渡す"""

    def do_PUT(self):
        self._ingest()

    def do_POST(self):
        self._ingest()

    def do_GET(self):
        if self.path in ("/", "/healthz"):
            self._respond(200, b"fpvt2google receiver ok\n")
        elif self.path == "/state":
            self._respond(200, json.dumps(self.relay.snapshot(), ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")
        else:
            self._respond(404, b"not found\n")

    def _ingest(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        self._respond(200)                       # 仕様に従い、処理前に即 200 OK
        if not body:
            return
        try:
            event = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            self.relay.log("received non-JSON body (%d bytes)" % len(body))
            return
        self.relay.handle_event(event)


class _DashboardHandler(_Base):
    """ネットが不通でも状況を確かめられるローカル表示"""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._respond(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/state":
            self._respond(200, json.dumps(self.relay.snapshot(), ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")
        else:
            self._respond(404, b"not found\n")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FPVTrackside Live</title>
<style>
 body{background:#111;color:#eee;font:16px/1.5 system-ui,-apple-system,sans-serif;margin:0;padding:12px}
 h1{font-size:18px;margin:0 0 4px} h2{font-size:15px;margin:18px 0 6px;color:#9ad}
 table{border-collapse:collapse;width:100%;max-width:900px}
 th,td{border-bottom:1px solid #333;padding:6px 8px;text-align:left;white-space:nowrap}
 th{color:#889;font-weight:600;font-size:13px}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
 .big{font-size:26px;font-weight:700}
 .meta{color:#889;font-size:13px}
 .badge{display:inline-block;padding:1px 8px;border-radius:10px;background:#245;font-size:12px}
 .stale{color:#f80}
</style></head><body>
<h1>FPVTrackside Live <span class="badge" id="type">-</span></h1>
<div class="meta" id="heat">-</div>
<h2>ラップ</h2>
<table><thead><tr><th class="num">Pos</th><th>Pilot</th><th>Ch</th>
<th class="num">Lap</th><th class="num">Lap Time</th><th class="num">Total</th></tr></thead>
<tbody id="laps"></tbody></table>
<h2>順位表</h2>
<table><thead id="shead"></thead><tbody id="sbody"></tbody></table>
<div class="meta" id="stats">-</div>
<script>
const $=id=>document.getElementById(id);
const fmt=v=>v==null?'-':(typeof v==='number'?v.toFixed(2):v);
async function tick(){
  let s; try{ s=await (await fetch('state',{cache:'no-store'})).json(); }
  catch(e){ $('stats').textContent='state 取得失敗'; return; }
  const h=s.heat||{};
  $('heat').textContent=h.round?('Round '+h.round+' / Race '+h.race+
      (h.elapsed!=null?'  経過 '+h.elapsed.toFixed(0)+'s':'')):'レース待機';
  $('laps').innerHTML=(s.laps||[]).map(r=>'<tr><td class="num big">'+(r.pos??'-')+
      '</td><td>'+(r.pilot||'')+(r.finished?' ★':'')+'</td><td>'+(r.channel||'')+
      '</td><td class="num big">'+(r.holeshot?'HS':(r.lap??'-'))+'</td><td class="num">'+fmt(r.laptime)+
      '</td><td class="num">'+fmt(r.total)+'</td></tr>').join('')||
      '<tr><td colspan="6" class="meta">データなし</td></tr>';
  const st=s.standings||{};
  $('type').textContent=st.type||'-';
  const heads=['#'].concat(st.headings&&st.headings.length?st.headings:['-']);
  $('shead').innerHTML='<tr>'+heads.map((h,i)=>'<th'+(i===0?' class="num"':'')+'>'+h+'</th>').join('')+'</tr>';
  $('sbody').innerHTML=(st.rows||[]).map((r,i)=>'<tr><td class="num">'+(i+1)+'</td>'+
      r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')||
      '<tr><td class="meta">データなし</td></tr>';
  const age=st.updatedAt?Math.round(Date.now()/1000-st.updatedAt):null;
  $('stats').textContent='recv '+s.stats.recv+' / lap送信 '+s.stats.lap_sent+
    ' / standings送信 '+s.stats.standings_sent+' / 失敗 lap '+s.stats.lap_failed+
    ' standings '+s.stats.standings_failed+(age!=null?' / 順位表 '+age+'秒前':'');
}
tick(); setInterval(tick,1000);
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# 起動
# --------------------------------------------------------------------------- #
def config_candidates(explicit=None):
    """設定ファイルの探索順を返す。

    --config 指定 → 実行ディレクトリ → スクリプトの隣 → その親（tools/ 配置）。
    どのディレクトリから実行してもスクリプトの隣（またはリポジトリ直下）の
    設定が読めるようにする。
    """
    if explicit:
        return [explicit]
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(os.getcwd(), CONFIG_FILENAME),
            os.path.join(here, CONFIG_FILENAME),
            os.path.join(os.path.dirname(here), CONFIG_FILENAME)]


def find_config(explicit=None):
    """最初に見つかった設定ファイルのパス（無ければ None）"""
    for path in config_candidates(explicit):
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def load_config(path, overrides):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            raise ValueError("設定ファイルは JSON オブジェクトにしてください: %s" % path)
        cfg.update(loaded)
    for key, value in (overrides or {}).items():
        if value is not None:
            cfg[key] = value
    cfg["channel_pos"] = {str(k).strip().upper(): v
                          for k, v in (cfg.get("channel_pos") or {}).items()}
    band_short = dict(BAND_SHORT)
    band_short.update({str(k).strip().lower(): v
                       for k, v in (cfg.get("band_short") or {}).items()})
    cfg["band_short"] = band_short
    return cfg


def write_sample_config(path):
    sample = dict(DEFAULT_CONFIG)
    sample.update({
        "google_lap_url": "https://script.google.com/macros/s/XXXXXXXX/exec",
        "google_standings_url": "https://script.google.com/macros/s/YYYYYYYY/exec",
        "record": "", "replay": "", "log_file": "",
    })
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sample, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("設定の雛形を書きました: %s" % path)
    print("google_lap_url / google_standings_url / channel_pos を編集してください。")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="FPVTrackside のレース状況を Google 側へ中継する",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None,
                   help="設定ファイル（省略時は %s を 実行ディレクトリ → スクリプトの隣 → "
                        "その親 の順に探す）" % CONFIG_FILENAME)
    p.add_argument("--init", action="store_true",
                   help="設定ファイルの雛形を書いて終了（--config 未指定なら実行ディレクトリ）")
    p.add_argument("--lap-url", dest="google_lap_url", help="ラップデータの POST 先")
    p.add_argument("--standings-url", dest="google_standings_url", help="順位表の POST 先")
    p.add_argument("--listen-port", type=int, dest="listen_port", help="ExtensionMode の受信ポート")
    p.add_argument("--dashboard-port", type=int, dest="dashboard_port", help="ローカル表示のポート（0=無効）")
    p.add_argument("--events-dir", dest="events_dir", help="FPVTrackside の events ディレクトリ")
    p.add_argument("--event-id", dest="event_id", help="イベントID（省略時は最新の Stages.json）")
    p.add_argument("--channel-pos", dest="channel_pos",
                   help='チャンネル→pos の対応（例: "E2=1,E1=2,F3=3,F5=4"）')
    p.add_argument("--batch-window", type=float, dest="batch_window_sec",
                   help="同じ選手・同じ周回をまとめる窓（秒。既定 0.25）")
    p.add_argument("--lap-senders", type=int, dest="lap_senders",
                   help="ラップ POST の並列数（既定 1＝直列。Google 側が並行実行に耐えるなら増やす）")
    p.add_argument("--dry-run", action="store_true", help="送信せず内容をログに出す")
    p.add_argument("--record", help="受信した生イベントを JSONL で保存")
    p.add_argument("--replay", help="JSONL を再生して送信（--record のファイルを使う）")
    p.add_argument("--speed", type=float, dest="replay_speed", help="再生速度（既定 1.0）")
    p.add_argument("--log-file", dest="log_file", help="ログをファイルにも書く")
    return p.parse_args(argv)


def parse_channel_pos(text):
    mapping = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("channel_pos は 'E2=1' の形で指定してください: %s" % item)
        key, _, value = item.partition("=")
        mapping[key.strip().upper()] = int(value)
    return mapping


def build_overrides(args):
    """CLI 引数 → 設定の上書き分（未指定のものは含めない）"""
    overrides = {}
    for key, value in vars(args).items():
        if key not in DEFAULT_CONFIG or value is None:
            continue
        if isinstance(value, bool) and not value:
            continue                      # 指定されなかったフラグで設定を潰さない
        overrides[key] = value
    if args.channel_pos:
        overrides["channel_pos"] = parse_channel_pos(args.channel_pos)
    return overrides


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.init:
        write_sample_config(args.config or os.path.join(os.getcwd(), CONFIG_FILENAME))
        return 0

    cfg_path = find_config(args.config)
    if args.config and not cfg_path:
        sys.stderr.write("設定ファイルがありません: %s\n" % args.config)
        return 1

    cfg = load_config(cfg_path, build_overrides(args))

    log = Logger(cfg["log_file"] or None)
    relay = Relay(cfg, log)

    if cfg_path:
        log("設定: %s" % cfg_path)
    else:
        log("警告: 設定ファイルが見つからないので既定値で動作します（探索: %s）。"
            "--init で雛形を作れます" % " / ".join(config_candidates()))
    if not cfg["google_lap_url"]:
        log("警告: google_lap_url が未設定（ラップデータは送信しません）")
    if not cfg["google_standings_url"]:
        log("警告: google_standings_url が未設定（順位表は送信しません）")
    log("channel_pos = %s" % json.dumps(cfg["channel_pos"], ensure_ascii=False))
    if cfg["dry_run"]:
        log("dry-run モード（POST しません）")

    try:
        relay.start()
    except OSError as exc:
        relay.shutdown()
        log("起動失敗: ポートを使えません（listen=%s dashboard=%s）: %r"
            % (cfg["listen_port"], cfg["dashboard_port"], exc))
        log("他のプロセスが同じポートを使っていないか確認し、"
            "listen_port / dashboard_port を変えてください。")
        log.close()
        return 1
    if cfg["dashboard_port"]:
        host = "localhost" if cfg["listen_host"] == "0.0.0.0" else cfg["listen_host"]
        log("ローカル表示: http://%s:%d/" % (host, cfg["dashboard_port"]))

    try:
        if cfg["replay"]:
            relay.replay(cfg["replay"], cfg["replay_speed"])
            log("replay 完了（Ctrl-C で終了）")
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        relay.shutdown()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
