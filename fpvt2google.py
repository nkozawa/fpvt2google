#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FPVTrackside のレース状況を Google 側（Apps Script 等）へ中継するプログラム。

機能
  1. FPVTrackside の「Gate / LED POST 通知」(ExtensionMode, HTTP PUT) を受信し、
     RaceStart と DetectionExt（ラップ確定のみ）を整形して lap 用 URL へ POST する
  2. イベントフォルダの Stages.json を監視し、変化したら順位表を standings 用 URL へ POST する
     （Stages.json の Standings は Lua スクリプト standings() の出力そのもの）
  3. 同じ内容をローカルの Web ページにも表示する（インターネット不通時のフォールバック）。
     既定 :5705 で `/`（目次）`/stat`（レース・ステータス）`/qualify`（予選順位表）
     `/standings`（最新順位）`/live`（診断）を出す。仕様は fpvt2google.md §8

Python 3.8 以降・標準ライブラリのみ。macOS / Windows 共通。

使い方
  python3 fpvt2google.py --init                # 設定ファイルの雛形を作る
  python3 fpvt2google.py                       # 起動（fpvt2google.json を読む）
  python3 fpvt2google.py --local-only          # Google へ POST せずローカル表示だけ動かす
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
    "dashboard_port": 5705,
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
    # レース・ステータス（/stat）の棒グラフの縦軸:
    #   sheet = シートの RaceStatus 再現（bar_rows 段の固定）/ auto = 最大周回に合わせる
    "bar_scale": "sheet",
    "bar_rows": 22,                # sheet の段数（シートの 1〜22 行に対応）
    # --- 開発用 ---
    "record": "",
    "replay": "",
    "replay_speed": 1.0,
    "local_only": False,           # Google へ POST せずローカル表示だけ動かす（内容はログへ）
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


def best_lap(previous, laptime, lap):
    """ヒート内のベスト lap を更新する。lap 0（holeshot 通過）は周回では無いので数えない"""
    if not lap or laptime is None:
        return previous
    if previous is None:
        return laptime
    return min(previous, laptime)


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
        self.qualify = {}               # 予選順位表（Type=qualify の最後の内容を凍結して保持）
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
            previous = self.board.get(key)
            entry = dict(payload, updatedAt=time.time())
            # レース・ステータス用。board は最新1件しか持たないのでベストはここで畳み込む
            entry["bestlap"] = best_lap(previous.get("bestlap") if previous else None,
                                        entry["laptime"], entry["lap"])
            self.board[key] = entry
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
            stage = pick_stage(stages, self.cfg["script_format"]) or {}
            card = stage.get("Standings") or {}
            snapshot = {
                "type": payload.get("Type"),
                "name": stage.get("Name"),
                "timestamp": payload.get("timestamp"),
                "headings": card.get("Headings") or [],
                "rows": [[r.get("Name")] + list(r.get("Values") or [])
                         for r in (card.get("Rows") or [])],
                "source": str(path),
                "updatedAt": time.time(),
            }
            self.standings = snapshot
            # 予選順位表はここで凍結する。勝ち上がり戦に入ると Stages.json は
            # 最新の結果しか持たないので、予選の内容はここからしか復元できない
            if snapshot["type"] == "qualify":
                self.qualify = dict(snapshot)
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
        if self.cfg["local_only"]:
            self.log("[local-only %s] %s" % (label, json.dumps(payload, ensure_ascii=False)))
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
            qualify = dict(self.qualify)
            stats = dict(self.stats)
            hello = {"fpvtVersion": self.hello.get("fpvtVersion"),
                     "platform": self.hello.get("platform"),
                     "eventsDirectory": (self.hello.get("paths") or {}).get("eventsDirectory")}
        board.sort(key=lambda row: (row.get("pos") is None, row.get("pos") or 0,
                                    -(row.get("lap") or 0)))
        if heat.get("startedMono"):
            heat["elapsed"] = round(time.time() - heat["startedMono"], 1)
        return {"now": time.time(), "hello": hello, "heat": heat, "laps": board,
                "standings": standings, "qualify": qualify, "stats": stats,
                "config": {"channel_pos": self.cfg["channel_pos"],
                           "bar_scale": self.cfg["bar_scale"],
                           "bar_rows": self.cfg["bar_rows"],
                           "decimal_places": self.decimals(),
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
        self.send_header("Cache-Control", "no-store")   # ページの差し替え・状態を確実に反映
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
    """ネットが不通でも状況を確かめられるローカル表示（1つの URL に複数ページ）

    `/` が目次で、配下に レース・ステータス `/stat`、予選順位表 `/qualify`、
    最新順位 `/standings`、診断用 `/live` を出す。見た目は `/shared.css` と
    `/shared.js`（どちらもこのファイル内の文字列）で共有する。
    """

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in PAGES:
            self._respond(200, PAGES[path].encode("utf-8"), "text/html; charset=utf-8")
        elif path in ASSETS:
            ctype, body = ASSETS[path]
            self._respond(200, body.encode("utf-8"), ctype)
        elif path == "/state":
            self._respond(200, json.dumps(self.relay.snapshot(), ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")
        else:
            self._respond(404, b"not found\n")


# --------------------------------------------------------------------------- #
# ローカル表示のページ（Google シートの RaceStatus / 予選順位表 / 順位表 に相当）
# --------------------------------------------------------------------------- #
SHARED_CSS = """
:root{--bg:#0e1116;--fg:#eef2f7;--muted:#94a1b5;--line:#2a3240;--accent:#4aa3ff;--panel:#151a22}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);padding:12px 16px 28px;
     font:16px/1.5 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:20px 0 6px;color:var(--accent)}
a{color:var(--accent)}
table{border-collapse:collapse;width:100%;max-width:1000px}
th,td{border-bottom:1px solid var(--line);padding:6px 10px;text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:13px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.meta{color:var(--muted);font-size:13px}
.big{font-size:26px;font-weight:700}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;background:#24344d;
       font-size:13px;vertical-align:middle}
.badge.qualify{background:#1d4030}
.badge.final{background:#4a2136}
.stale{color:#f80}
.empty{color:var(--muted);padding:24px 0}
nav.top{font-size:14px;margin:0 0 12px}
nav.top a{margin-right:14px;text-decoration:none}
nav.top a.here{color:var(--fg);font-weight:700;border-bottom:2px solid var(--accent)}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.cards a{flex:1 1 240px;max-width:360px;padding:18px;border:1px solid var(--line);
         border-radius:12px;background:var(--panel);text-decoration:none;color:var(--fg)}
.cards a:hover{border-color:var(--accent)}
.cards b{display:block;font-size:20px;margin-bottom:6px}
.cards span{color:var(--muted);font-size:13px;line-height:1.5}
.cols{display:flex;gap:10px;align-items:flex-start;margin-top:12px;overflow-x:auto}
.col{flex:1 1 0;min-width:130px}
.col h3{margin:0 0 6px;font-size:14px;text-align:center;color:var(--muted);font-weight:600}
.bar{position:relative;height:56vh;min-height:220px;background:#1b2230;
     border:1px solid var(--line);border-radius:4px;overflow:hidden}
.bar.fixed{display:grid;grid-template-rows:repeat(var(--rows,22),1fr);gap:1px}
.bar .cell{background:#202836}
.bar .cell.on{background:var(--c,#4aa3ff);color:var(--cf,#fff)}
.bar .cell.on span{display:flex;align-items:center;justify-content:center;height:100%;
                   font-weight:700;font-size:13px}
.bar.auto .fill{position:absolute;left:0;right:0;bottom:0;background:var(--c,#4aa3ff);
                transition:height .3s ease-out}
.facts{margin-top:8px;display:grid;gap:2px}
.facts div{display:flex;align-items:baseline;justify-content:space-between;gap:6px}
.facts .v{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums}
.facts .k{color:var(--muted);font-size:11px}
.pilot{margin-top:8px;text-align:center;font-size:17px;font-weight:700;
       overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pilot small{display:block;color:var(--muted);font-weight:400;font-size:12px}
.pilot.done{color:#ffd54f}
"""

SHARED_JS = """
const $ = id => document.getElementById(id);
let DEC = 2;
// 色はシートの RaceStatus に合わせる（pos1 赤 / pos2 緑 / pos3 青 / pos4 黄）
const PALETTE = [['#e53935','#fff'],['#2e7d32','#fff'],['#1565c0','#fff'],
                 ['#fdd835','#111'],['#6a1b9a','#fff'],['#00838f','#fff']];
const NAV = [['/','目次'],['/stat','レース・ステータス'],['/qualify','予選順位表'],
             ['/standings','最新順位'],['/live','ライブ']];

function buildNav(){
  const nav = document.getElementById('nav');
  if(!nav) return;
  let p = location.pathname;
  if(p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
  if(p === '/index.html') p = '/';
  nav.innerHTML = NAV.map(function(x){
    return '<a href="'+x[0]+'"'+(x[0]===p?' class="here"':'')+'>'+x[1]+'</a>';
  }).join('');
}
buildNav();

function esc(v){
  return String(v==null?'':v).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function fmt(v){
  if(v==null || v==='') return '-';
  return typeof v==='number' ? v.toFixed(DEC) : v;
}
function ago(unix){ return unix ? Math.max(0, Math.round(Date.now()/1000 - unix)) : null; }

async function getState(){
  const r = await fetch('state', {cache:'no-store'});
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}
function poll(render, ms){
  ms = ms || 1000;
  (async function tick(){
    try{
      const s = await getState();
      DEC = (s.config && s.config.decimal_places) || 2;
      render(s);
      const e = $('err'); if(e){ e.className='meta'; e.textContent=''; }
    }catch(err){
      const e = $('err');
      if(e){ e.className='meta stale'; e.textContent='state 取得失敗: '+err; }
    }
    setTimeout(tick, ms);
  })();
}
function heatLine(h){
  if(!h || h.round==null) return 'レース待機';
  return 'Round '+h.round+' / Race '+h.race+
         (h.elapsed!=null?'　経過 '+Math.floor(h.elapsed)+'s':'');
}
// シートの trans() と同じ読み替え。表示だけで、/state の JSON は生の値のまま
function transStatus(text){
  if(text==null || text==='') return '';
  return String(text)
    .replace('cut','順位確定(予選)')
    .replace('out','順位確定(勝ち上がり戦)')
    .replace('advances','上位へ勝ち上がり')
    .replace('enters','勝ち上がり戦')
    .replace('finalist','決勝戦進出')
    .replace('final','決勝戦');
}
function standingsTable(st, translate){
  if(!st || !st.rows || !st.rows.length) return '<div class="empty">データなし</div>';
  const heads = (st.headings && st.headings.length) ? st.headings : [];
  let statusIdx = -1;
  heads.forEach(function(h,i){ if(/^status$/i.test(String(h))) statusIdx = i; });
  let html = '<table><thead><tr><th class="num">#</th><th>選手</th>'+
    heads.map(function(h){ return '<th>'+esc(h)+'</th>'; }).join('')+'</tr></thead><tbody>';
  st.rows.forEach(function(row,i){
    html += '<tr><td class="num">'+(i+1)+'</td><td>'+esc(row[0])+'</td>'+
      row.slice(1).map(function(cell,j){
        const isStatus = translate && j === statusIdx;
        const text = isStatus ? (transStatus(cell) || ' ') : cell;
        return '<td'+(isStatus?' title="'+esc(cell)+'"':'')+'>'+esc(text)+'</td>';
      }).join('')+'</tr>';
  });
  return html+'</tbody></table>';
}
"""

_PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/shared.css">
</head><body>
<nav class="top" id="nav"></nav>
@@BODY@@
<div class="meta" id="err"></div>
<script src="/shared.js"></script>
<script>
@@SCRIPT@@
</script>
</body></html>
"""


def page(title, body, script):
    """共通の骨組み（nav・shared.css/js・エラー欄）に本文とスクリプトを埋め込む"""
    return (_PAGE.replace("@@TITLE@@", title)
                 .replace("@@BODY@@", body)
                 .replace("@@SCRIPT@@", script))


INDEX_HTML = page("FPVTrackside ローカル表示", """
<h1>FPVTrackside ローカル表示</h1>
<div class="meta" id="now">-</div>
<div class="cards">
 <a href="/stat"><b>レース・ステータス</b><span>ヒート中の周回数・最終／ベストラップ・総飛行時間。Google シートの RaceStatus に相当</span></a>
 <a href="/qualify"><b>予選順位表</b><span>予選終了時の順位をそのまま保持。勝ち上がり戦に入っても内容は変わらない</span></a>
 <a href="/standings"><b>最新順位</b><span>予選中は予選順位、勝ち上がり戦に入ると最新の結果を1ページで表示</span></a>
 <a href="/live"><b>ライブ（診断）</b><span>受信したラップの一覧・順位表の生の値・送信統計。トラブル確認用</span></a>
</div>
<div class="meta" id="foot">-</div>
""", """
poll(function(s){
  const st = s.standings||{}, q = s.qualify||{}, stats = s.stats||{};
  $('now').textContent = heatLine(s.heat);
  $('foot').textContent = '最新順位: '+(st.type||'なし')+(st.name?'（'+st.name+'）':'')+
    '　/　予選順位表: '+(q.rows?'保持中':'なし')+
    '　/　受信 '+stats.recv+' 件・ラップ送信 '+stats.lap_sent+' 件'+
    ((stats.lap_failed||stats.standings_failed)
      ? '　/　失敗 ラップ '+stats.lap_failed+'・順位表 '+stats.standings_failed : '');
});
""")

STAT_HTML = page("レース・ステータス — FPVTrackside", """
<h1>レース・ステータス <span class="badge" id="mode">-</span></h1>
<div class="meta" id="heat">-</div>
<div class="cols" id="cols"></div>
<div class="meta" id="foot">-</div>
""", """
function posColor(pos){
  if(pos==null) return '--c:#6b7684;--cf:#fff';
  const p = PALETTE[(Math.abs(pos)-1) % PALETTE.length];
  return '--c:'+p[0]+';--cf:'+p[1];
}
function posChannels(channelPos){
  const map = {};
  Object.keys(channelPos||{}).forEach(function(ch){
    const p = channelPos[ch];
    if(p==null) return;
    map[p] = map[p] ? map[p]+'/'+ch : ch;
  });
  return map;
}
// channel_pos の pos 毎に1列（データにしか無い pos も列にする）。
// pos が引けなかった選手は後ろに並べるので、設定ミスが隠れない
function columns(channelPos, laps){
  const byPos = {};
  Object.keys(channelPos||{}).forEach(function(ch){
    const p = channelPos[ch];
    if(p!=null && byPos[p]===undefined) byPos[p] = null;
  });
  const extra = [];
  laps.forEach(function(r){
    if(r.pos!=null) byPos[r.pos] = r; else extra.push(r);
  });
  const cols = Object.keys(byPos).map(Number).sort(function(a,b){ return a-b; })
                 .map(function(p){ return {pos:p, row:byPos[p]}; });
  extra.forEach(function(r){ cols.push({pos:null, row:r}); });
  return cols;
}
function columnHtml(c, rows, auto, maxLap, chans){
  const r = c.row, pos = c.pos, style = posColor(pos);
  const laps = r ? (r.lap||0) : 0;
  let bar;
  if(auto){
    const pct = (laps>0 && maxLap>0) ? Math.max(3, Math.round(laps/maxLap*100)) : 0;
    bar = '<div class="bar auto" style="'+style+'">'+
          '<div class="fill" style="height:'+pct+'%"></div></div>';
  }else{
    const filled = Math.min(laps, rows);
    let cells = '';
    for(let i=rows; i>=1; i--){
      cells += '<div class="cell'+(i<=filled?' on':'')+'">'+
               (i===filled && filled>0 ? '<span>'+laps+'</span>' : '')+'</div>';
    }
    bar = '<div class="bar fixed" style="--rows:'+rows+';'+style+'">'+cells+'</div>';
  }
  const hasLap = r && r.lap>0;
  const facts = [['周回', r ? (hasLap ? laps : (r.holeshot ? 'HS' : 0)) : '-'],
                 ['最終', hasLap ? fmt(r.laptime) : '-'],
                 ['ベスト', hasLap ? fmt(r.bestlap) : '-'],
                 ['総合(s)', r ? fmt(r.total) : '-']];
  return '<div class="col"><h3>'+(pos==null?'?':'pos '+pos)+'</h3>'+bar+
    '<div class="facts">'+facts.map(function(f){
      return '<div><span class="v">'+f[1]+'</span><span class="k">'+f[0]+'</span></div>';
    }).join('')+'</div>'+
    '<div class="pilot'+(r&&r.finished?' done':'')+'">'+(r?esc(r.pilot):'—')+
      '<small>'+(r?esc(r.channel||'')+(r.finished?' ★':''):esc(chans[pos]||''))+'</small>'+
    '</div></div>';
}
poll(function(s){
  const cfg = s.config||{};
  const rows = Math.max(1, cfg.bar_rows||22);
  const auto = cfg.bar_scale === 'auto';
  $('mode').textContent = auto ? 'auto（最大周回に合わせる）' : 'シート '+rows+'段';
  $('heat').textContent = heatLine(s.heat);
  const laps = s.laps||[];
  const maxLap = laps.reduce(function(m,r){ return Math.max(m, r.lap||0); }, 0);
  const cols = columns(cfg.channel_pos, laps);
  $('cols').innerHTML = cols.length
    ? cols.map(function(c){ return columnHtml(c, rows, auto, maxLap, posChannels(cfg.channel_pos)); }).join('')
    : '<div class="empty">channel_pos が未設定です（fpvt2google.json）</div>';
  const st = s.stats||{};
  $('foot').textContent = '受信 '+st.recv+' 件 / ラップ送信 '+st.lap_sent+' 件'+
    (st.invalid_skipped ? ' / 無効検出 '+st.invalid_skipped : '')+
    (st.dup_skipped ? ' / 重複 '+st.dup_skipped : '')+
    (laps.length ? '' : '　— このヒートのラップはまだありません');
});
""")

QUALIFY_HTML = page("予選順位表 — FPVTrackside", """
<h1>予選順位表 <span class="badge" id="state">-</span></h1>
<div class="meta" id="info">-</div>
<div id="table"></div>
""", """
poll(function(s){
  const q = s.qualify||{};
  $('state').textContent = q.rows ? '保持中' : 'なし';
  $('state').className = 'badge'+(q.rows ? ' qualify' : '');
  $('info').textContent = q.rows
    ? (q.name||'')+'　/　Type=qualify　/　取得 '+(q.timestamp||'-')+
      (ago(q.updatedAt)!=null ? '　/　'+ago(q.updatedAt)+'秒前に更新' : '')+
      '　/　予選終了時の内容で固定（勝ち上がり戦に入っても変わらない）'
    : 'Type=qualify の順位表がまだ届いていません（Stages.json を監視中）';
  $('table').innerHTML = standingsTable(q, true);
}, 2000);
""")

STANDINGS_HTML = page("最新順位 — FPVTrackside", """
<h1>最新順位 <span class="badge" id="state">-</span></h1>
<div class="meta" id="info">-</div>
<div id="table"></div>
""", """
poll(function(s){
  const st = s.standings||{}, type = st.type||'';
  $('state').textContent = type==='final' ? '勝ち上がり・決勝' : (type==='qualify' ? '予選' : '-');
  $('state').className = 'badge'+(type ? ' '+type : '');
  if(st.rows){
    const a = ago(st.updatedAt);
    const ageText = a==null ? '' : (a>120
      ? '　/　<span class="stale">'+a+'秒前に更新（止まっています）</span>'
      : '　/　'+a+'秒前に更新');
    $('info').innerHTML = esc((st.name||'')+'　/　Type='+type+'　/　'+(st.timestamp||''))+
      ageText+'　/　出典: Stages.json（Lua standings）';
  }else{
    $('info').textContent = '順位表はまだありません（Stages.json を監視中）';
  }
  $('table').innerHTML = standingsTable(st, true);
}, 2000);
""")

LIVE_HTML = page("ライブ（診断） — FPVTrackside", """
<h1>ライブ（診断） <span class="badge" id="type">-</span></h1>
<div class="meta" id="heat">-</div>
<h2>ラップ</h2>
<div id="laps"></div>
<h2>順位表（生の値）</h2>
<div id="table"></div>
<div class="meta" id="stats">-</div>
""", """
poll(function(s){
  $('heat').textContent = heatLine(s.heat);
  $('type').textContent = (s.standings||{}).type || '-';
  const laps = s.laps||[];
  $('laps').innerHTML = laps.length
    ? '<table><thead><tr><th class="num">Pos</th><th>Pilot</th><th>Ch</th>'+
      '<th class="num">Lap</th><th class="num">Lap Time</th><th class="num">Best</th>'+
      '<th class="num">Total</th></tr></thead><tbody>'+
      laps.map(function(r){
        return '<tr><td class="num big">'+(r.pos==null?'-':r.pos)+'</td><td>'+esc(r.pilot)+
          (r.finished?' ★':'')+'</td><td>'+esc(r.channel)+'</td><td class="num big">'+
          (r.holeshot?'HS':(r.lap==null?'-':r.lap))+'</td><td class="num">'+fmt(r.laptime)+
          '</td><td class="num">'+fmt(r.bestlap)+'</td><td class="num">'+fmt(r.total)+'</td></tr>';
      }).join('')+'</tbody></table>'
    : '<div class="empty">データなし</div>';
  $('table').innerHTML = standingsTable(s.standings, false);
  const st = s.stats||{}, a = ago((s.standings||{}).updatedAt);
  $('stats').textContent = 'recv '+st.recv+' / lap送信 '+st.lap_sent+
    ' / standings送信 '+st.standings_sent+' / 失敗 lap '+st.lap_failed+
    ' standings '+st.standings_failed+(a!=null ? ' / 順位表 '+a+'秒前' : '')+
    ' / '+(s.hello&&s.hello.fpvtVersion ? 'fpvt '+s.hello.fpvtVersion : 'Hello 未受信');
});
""")

PAGES = {
    "/": INDEX_HTML, "/index.html": INDEX_HTML,
    "/stat": STAT_HTML, "/qualify": QUALIFY_HTML,
    "/standings": STANDINGS_HTML, "/live": LIVE_HTML,
}
ASSETS = {
    "/shared.css": ("text/css; charset=utf-8", SHARED_CSS),
    "/shared.js": ("application/javascript; charset=utf-8", SHARED_JS),
}


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
        # 旧名 dry_run の設定ファイルも受け付ける。黙って無視すると POST が復活するので読み替える
        if "local_only" not in loaded and "dry_run" in loaded:
            cfg["local_only"] = bool(loaded["dry_run"])
        cfg.pop("dry_run", None)
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
    p.add_argument("--local-only", action="store_true",
                   help="Google へ POST せずローカル表示だけ動かす（送信する内容はログに出す）")
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
    if cfg["local_only"]:
        log("ローカル表示のみ（Google へ POST しません）")
    else:
        if not cfg["google_lap_url"]:
            log("警告: google_lap_url が未設定（ラップデータは送信しません）")
        if not cfg["google_standings_url"]:
            log("警告: google_standings_url が未設定（順位表は送信しません）")
    log("channel_pos = %s" % json.dumps(cfg["channel_pos"], ensure_ascii=False))

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
