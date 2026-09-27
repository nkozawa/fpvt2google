# fpvt2google — FPVTrackside レース状況 中継プログラム 仕様／手順書

`tools/fpvt2google.py` の仕様と運用手順をまとめた独立ドキュメント。
FPVTrackside のレース状況（ラップ・順位表）を外部システム（Google Apps Script など）へ中継する。

- 対象: `tools/fpvt2google.py`（Python 3 標準ライブラリのみ・単一ファイル）
- テスト: `test/fpvt2google_test.py`
- 方式検討の経緯・調査結果: `REPORTS.md`

---

## 1. 概要

### 1.1 目的

FPVTrackside で計測中のレース状況を、外部の表示システムへリアルタイムに渡す。

1. **ヒート中のラップ表示** — 選手毎のラップ回数・ラップタイム・総飛行時間
2. **予選順位の表示** — アプリの Standings と同じ内容（予選が終わっても保持）
3. **勝ち上がり戦の順位の表示** — アプリの Standings と同じ内容（最終順位）

### 1.2 できること

- FPVTrackside の **Gate / LED POST 通知（ExtensionMode）** を HTTP で受信し、
  ラップデータを整形して指定 URL へ POST する
- イベントフォルダの **`Stages.json` を監視**し、順位表が変わったら指定 URL へ POST する
- 同じ内容を **ローカルの Web ページ**（既定 `:5705`）にも表示する。
  `/` を目次に、レース・ステータス・予選順位表・最新順位・ライブ（診断）の4ページを出す（§8）
- 受信した生イベントの **録画／再生**（アプリ無しで開発・検証できる）

### 1.3 できないこと（設計上の割り切り）

- **順位を計算しない**。順位表は Lua スクリプト `ladder_finals.lua` の `standings()` が
  唯一の情報源で、本プログラムはアプリが保存した `Stages.json` をそのまま中継するだけ
- 表示ページの作り込み（Google 側のシート／ダッシュボード）は行わない
- FPVTrackside へ書き戻すことは一切しない（読み取りと受信のみ）

---

## 2. 仕組み

```
                     ┌───────────────────────────────────────────┐
                     │            fpvt2google.py                 │
 FPVTrackside        │                                           │
 ┌──────────────┐    │  ①受信(:8765)   ②整形/間引き   ③送信      │   POST   ┌──────────────┐
 │ ExtensionMode├─PUT─►  即200 OK  ──►  lap-endのみ ──► キュー ──┼────────► │ ラップ用 URL  │
 │ (Gate/LED)   │    │               0.25秒バッチ               │          └──────────────┘
 └──────────────┘    │                                           │
 ┌──────────────┐    │  ④Stages.json 監視（デバウンス）          │   POST   ┌──────────────┐
 │ events/<id>/ ├────►  mtime+size が2回連続一致 → Type 判定 ────┼────────► │ 順位表用 URL  │
 │ Stages.json  │    │                                           │          └──────────────┘
 └──────────────┘    │  ⑤ローカル表示(:5705)  目次/4ページ + /state   │
                     └───────────────────────────────────────────┘
```

### 2.1 なぜ2つの経路が必要か

| 経路 | 理由 |
|---|---|
| ExtensionMode（PUT 受信） | **ヒート中**のラップ情報はここでしか取れない。ゲート通過毎に `DetectionExt` が届く |
| `Stages.json` 監視 | 順位表は Lua スクリプトの計算結果で、**ExtensionMode の `StageRanking` はアプリ独自の別計算**（Lua の行ではない）。Lua にはファイル書込もネットワークも無い（MoonSharp に IO 未登録）ため、アプリが保存する `Stages.json` を読むのが唯一の経路 |

### 2.2 スレッド構成

| スレッド | 役割 |
|---|---|
| 受信サーバ（`:8765`） | PUT/POST を受けて**即 200 OK**、本文はキューへ（リクエスト毎にスレッド） |
| ラップ配信 | 受信キューから `batch_window_sec` の窓で**同じ選手・同じ周回の重複だけ**をまとめ、選手単位の worker キューへ配る。`RaceStart` は即送出 |
| ラップ送信 worker × `lap_senders`（既定 1） | 振り分けられたキューから POST。**同じ選手は常に同じ worker** なので周回順は保たれ、`2` 以上にすれば POST が遅くても他の選手を止めない |
| 順位表送信 | 変化があったときだけ POST。失敗時は指数バックオフでリトライ |
| Stages 監視 | 既定1秒間隔で mtime/size を確認し、落ち着いたところを読んでキューへ |
| ローカル表示（`:5705`） | `/`（目次）・`/stat`・`/qualify`・`/standings`・`/live`（HTML）と `/state`（JSON）、`/shared.css`・`/shared.js` を配信 |

---

## 3. 動作環境

| 項目 | 要件 |
|---|---|
| Python | 3.8 以降（**標準ライブラリのみ**、pip 不要） |
| OS | macOS / Windows（Linux も可） |
| FPVTrackside | ExtensionMode（Gate / LED POST 通知）に対応した版。動作確認は 2.78.x |
| ネットワーク | 受信は同一マシン（127.0.0.1）。**送信はインターネット必須**（Google へ HTTPS） |
| 使用ポート | 既定 8765（受信）/ 5705（ローカル表示）。FPVTrackside 内蔵 Web サーバの 8080 は使わない |

---

## 4. セットアップ（初回）

### 4.1 FPVTrackside 側の設定

1. メニュー → **Profile Settings**（プロファイル設定）を開く
2. 「**Gate / LED POST notifications**」の項目を設定する

   | 設定 | 値 |
   |---|---|
   | `ExtensionMode` | `true` |
   | `NotificationURL` | `http://127.0.0.1:8765/` |

3. **FPVTrackside を再起動する**（設定は起動時にしか読まれない）

> `ExtensionMode = true` にすると、旧来の Gate/LED 通知（RemoteNotifier）は無効になります。
> LED テープ等を併用している場合は注意してください。

### 4.2 中継プログラムの設置

```bash
# リポジトリを任意の場所に置く（tools/fpvt2google.py だけコピーしても動く）
cd FPVTracksideScripts

# 設定ファイルの雛形を作る
python3 tools/fpvt2google.py --init
# → ./fpvt2google.json ができる
```

`fpvt2google.json` を編集する（最低限この2つ）:

```json
{
  "google_lap_url":       "https://script.google.com/macros/s/AAAAAAAA/exec",
  "google_standings_url": "https://script.google.com/macros/s/BBBBBBBB/exec",
  "channel_pos": { "E2": 1, "E1": 2, "F3": 3, "F5": 4 }
}
```

> `fpvt2google.json` は `.gitignore` 済み（URL を含むためコミットされない）。
> Windows では `python` / `py -3` に読み替えてください。

設定ファイルは **実行したディレクトリ → スクリプトの隣 → スクリプトの親**
（`tools/` 配置ならリポジトリ直下）の順に探すので、
`python3 /絶対パス/tools/fpvt2google.py` のように**どのディレクトリからでも起動できます**。
読んだファイルは起動時ログの `設定: …` に出ます。

### 4.3 疎通確認（3ステップ）

```bash
# 1) 送信先を仮の URL にしたまま local-only で起動（POST しない）
python3 tools/fpvt2google.py --local-only

# 2) FPVTrackside を起動してレースを1本流す
#    → ログに "Hello:" "RaceStart" "[local-only lap] {...}" が出れば受信は成功

# 3) ローカル表示をブラウザで開いて確認
open http://localhost:5705/
```

`Hello:` の行が出ない場合は §10 の「PUT が届かない」を参照。

---

## 5. 起動と停止

```bash
python3 tools/fpvt2google.py                       # 通常起動（fpvt2google.json を読む）
python3 tools/fpvt2google.py --config /path/cfg.json
python3 tools/fpvt2google.py --local-only          # Google へ POST せずローカル表示だけ動かす
python3 tools/fpvt2google.py --record cap.jsonl    # 受信した生イベントを録画
python3 tools/fpvt2google.py --replay cap.jsonl    # 録画を再生（アプリ無しで動作確認）
python3 tools/fpvt2google.py --lap-url https://... --standings-url https://...
python3 /絶対パス/tools/fpvt2google.py             # どのディレクトリからでも起動できる
```

- **設定ファイルの探索順**: `--config` 指定 → 実行ディレクトリ → スクリプトの隣 →
  スクリプトの親（`tools/` 配置ならリポジトリ直下）。最初に見つかったものを使い、
  起動時ログに `設定: <パス>` を出す。どこにも無ければ既定値で動き、探索した場所を
  警告として出す。`--config` で明示したファイルが無い場合はエラー終了（exit 1）

- **停止**: コンソールで `Ctrl-C`
- **ログ**: 標準出力に加えて `--log-file relay.log`（または設定 `log_file`）でファイルにも残せる
- **常駐**（任意）:
  - macOS: `nohup python3 tools/fpvt2google.py --log-file relay.log >/dev/null 2>&1 &`
    （または launchd / `screen`）
  - Windows: 別のコンソールウィンドウで起動したままにするか、タスク スケジューラで
    「ログオン時」に `pythonw.exe tools\fpvt2google.py` を実行

起動時のログ例:

```
2026-09-11 14:31:08 設定: /Users/…/FPVTracksideScripts/fpvt2google.json
2026-09-11 14:31:08 channel_pos = {"E2": 1, "E1": 2, "F3": 3, "F5": 4}
2026-09-11 14:31:09 receiver listening on 0.0.0.0:8765
2026-09-11 14:31:09 dashboard listening on 0.0.0.0:5705
2026-09-11 14:31:09 ローカル表示: http://localhost:5705/
```

`google_lap_url` / `google_standings_url` が未設定の場合は起動時に警告を出し、
その種類のデータは送信しません（ローカル表示は動きます）。

---

## 6. 設定リファレンス

`fpvt2google.json`（JSON オブジェクト）。**CLI 引数が設定ファイルより優先**されます。
未指定の項目は既定値が使われます。

### 6.1 送信先

| キー | 既定 | 意味 |
|---|---|---|
| `google_lap_url` | `""` | ラップデータ（`RaceStart` / `DetectionExt`）の POST 先 |
| `google_standings_url` | `""` | 順位表の POST 先 |
| `post_timeout_sec` | `10` | 1回の POST のタイムアウト（秒） |
| `lap_senders` | `1` | ラップ POST の並列数。**既定は直列**。選手単位で振り分けるので増やしても**周回順は保たれる**が、Apps Script 側が並行実行をさばけない（ロック待ち・429・タイムアウト）とかえって失敗が増える。Google 側の実行ログを確認してから上げる |
| `lap_retries` | `1` | ラップの試行回数。既定は再試行なし（**失敗したラップはそのまま欠落**）。ネットが不安定なら `2`〜`3` に |
| `standings_retries` | `5` | 順位表の試行回数（失敗時は 1,2,4,8 秒のバックオフ） |

### 6.2 受信・表示

| キー | 既定 | 意味 |
|---|---|---|
| `listen_host` | `0.0.0.0` | 受信サーバのバインド先（FPVTrackside と同一マシンなら `127.0.0.1` でも可） |
| `listen_port` | `8765` | `NotificationURL` に指定するポート |
| `dashboard_port` | `5705` | ローカル表示ページのポート（`0` で無効） |
| `bar_scale` | `"sheet"` | 棒グラフの縦軸。`"sheet"` = `bar_rows` 段の固定（シートの `RaceStatus` 再現）、`"auto"` = ヒート内の最大周回を100%に合わせる |
| `bar_rows` | `22` | `bar_scale: "sheet"` の段数（シートの1〜22行に対応） |

### 6.3 ラップの整形

| キー | 既定 | 意味 |
|---|---|---|
| `channel_pos` | `{"E2":1,"E1":2,"F3":3,"F5":4}` | **チャンネル → 表示位置 `pos`**。キーは短縮名（`"F3"`）のほか、生の band 名（`"Fatshark3"`）や周波数（`"5732"`）でも書ける。**大文字小文字は区別しない**。該当が無ければ `pos: null` |
| `band_short` | `{}` | band 名 → 短縮名の**追加**読み替え表（既定の `BAND_SHORT` に上書きマージ）。例 `{"MyBand": "M"}` |
| `decimal_places` | `2` | 秒の小数桁。`Hello.decimalPlaces` が届けばそれで上書きされる |
| `batch_window_sec` | `0.25` | **同じ選手・同じ周回**の重複だけを最新1件にまとめる窓（秒）。周回の違うラップは間引かない。`0` で窓なし（届き次第すぐ配信） |

#### チャンネル名の読み替え

FPVTrackside は `channel.band` に**長い名前**（`"Fatshark"` `"Raceband"` など）を入れてくる
（短縮名 `ShortBand` は PUT の JSON に含まれない）。そこで本プログラムは次の表で短縮名に
読み替えてから `channel` と `pos` を決める。

| band | 短縮名 | band | 短縮名 |
|---|---|---|---|
| `Fatshark` | **F** | `HDZero` | **Z** |
| `Raceband` | **R** | `WalkSnail` | **W** |
| `LowBand` | **L** | `Diatone` | **D** |
| `A` / `B` / `E` | **A / B / E** | `DJIFPVHD` / `DJIO3` / `DJIO4` | **D** |

（アプリの `Channels.json` の `ShortBand` に対応。表に無い band は先頭1文字で代用する）

`pos` を引くときは **短縮名 → 生の band 名 → 周波数** の順で `channel_pos` を照合するので、
設定にはどの形で書いてもよい:

```json
{ "channel_pos": { "F3": 3, "FATSHARK5": 4, "5658": 1 } }
```

### 6.4 順位表（Stages.json）

| キー | 既定 | 意味 |
|---|---|---|
| `events_dir` | `""` | FPVTrackside の `events` ディレクトリ。空なら `Hello.paths.eventsDirectory` → `~/Documents/FPVTrackside/events` の順で自動特定 |
| `event_id` | `""` | イベントID。空なら **`Stages.json` の更新時刻が最新**のイベントフォルダを使う |
| `script_format` | `"ladder_finals.lua"` | 複数ステージがあるとき、このスクリプトを使うステージを優先して選ぶ |
| `stages_poll_sec` | `1.0` | 監視間隔。**変化の検出は2回連続で同じ状態になってから**なので、反映まで最大2倍かかる |
| `standings_payload` | `"full"` | `"full"` = `{"Type":..., "stages":[Stages.json 全体]}`／`"stage"` = 該当ステージだけ／`"standings"` = 順位表（Headings/Rows）だけ |
| `send_initial_standings` | `true` | 起動時に読み込んだ順位表も送るか |

### 6.5 開発用

| キー | 既定 | CLI | 意味 |
|---|---|---|---|
| `local_only` | `false` | `--local-only` | Google へ POST せずローカル表示だけ動かす（送信する内容はログに出し、送信成功としてカウントする）。**旧名の `dry_run` も読み替えて受け付ける**ので、既存の設定ファイルはそのまま動く |
| `record` | `""` | `--record FILE` | 受信した**生イベント**を JSONL で追記保存 |
| `replay` | `""` | `--replay FILE` | JSONL を再生して `handle_event` に流す（受信サーバも起動したまま） |
| `replay_speed` | `1.0` | `--speed N` | 再生速度。`0` で待ち時間なし |
| `log_file` | `""` | `--log-file FILE` | ログをファイルにも書く |

### 6.6 CLI 引数一覧

```
--config PATH        設定ファイル（省略時は fpvt2google.json を
                     実行ディレクトリ → スクリプトの隣 → その親 の順に探す）
--init               設定ファイルの雛形を書いて終了（--config 未指定なら実行ディレクトリ）
--lap-url URL        --standings-url URL
--listen-port N      --dashboard-port N
--events-dir PATH    --event-id ID
--channel-pos "E2=1,E1=2,F3=3,F5=4"
--batch-window SEC   --lap-senders N    --local-only
--record FILE        --replay FILE        --speed N
--log-file FILE
```

---

## 7. 送信データの仕様

いずれも `Content-Type: application/json; charset=utf-8` の **HTTP POST**、本文は UTF-8 の JSON。
日本語はそのまま（`\uXXXX` エスケープしない）。
**すべての POST 本文は先頭に `timestamp`（このデータを作成した時刻、
ローカルタイム `yyyy-mm-dd hh:mm:ss`）を含む。**

### 7.1 ラップ用 URL

1回の POST に**1イベント**。`type` は `RaceStart` か `DetectionExt` の2種類だけ。

#### RaceStart（ヒート開始）

```json
{"timestamp":"2026-09-12 10:00:05","type":"RaceStart","round":3,"race":5,"raceType":"Race",
 "actualStart":"2026-09-11T10:00:05.000Z"}
```

| フィールド | 型 | 意味 |
|---|---|---|
| `timestamp` | string | **このデータを作成した時刻**（ローカルタイム、`yyyy-mm-dd hh:mm:ss`）。常に先頭のキー |
| `type` | string | `"RaceStart"` |
| `round` | int | ラウンド番号 |
| `race` | int | ラウンド内のレース番号 |
| `raceType` | string | `Race` / `TimeTrial` など |
| `actualStart` | string\|null | 実際のスタート時刻（ISO-8601 UTC） |

> **ヒート境界の目印**です。受信したらラップ表示をリセットしてください。
> 選手リストは含まれません（`RaceLoaded` は中継しない方針のため）。
> 選手の並びは `channel_pos` で決まる `pos` を使ってください。

#### DetectionExt（ラップ確定）

```json
{"timestamp":"2026-09-12 10:00:33","type":"DetectionExt","pos":1,"channel":"F3","pilot":"Shu_FPV",
 "lap":3,"holeshot":false,"total":28.0,"laptime":8.3,"round":3,"race":5,"position":1,
 "finished":false}
```

| フィールド | 型 | 元の FPVTrackside フィールド | 意味 |
|---|---|---|---|
| `timestamp` | string | — | **このデータを作成した時刻**（ローカルタイム、`yyyy-mm-dd hh:mm:ss`）。常に先頭のキー |
| `type` | string | — | `"DetectionExt"` |
| `pos` | int\|null | `channel` を `channel_pos` で変換 | 表示位置（1〜4）。マップに無ければ `null` |
| `channel` | string | `channel.band` + `channel.number` | **短縮名に読み替えた**チャンネル名（`"Fatshark"`+3 → `"F3"`。§6.3 参照） |
| `pilot` | string | `pilotName` | 選手名 |
| `lap` | int\|null | `lapNumber` | **完了周回数**。アプリの値をそのまま入れる（実データで確認: 最初の lap-end が 1）。holeshot 通過は 0 |
| `holeshot` | bool | `lapNumber == 0` | スタート直後の holeshot 通過なら `true`（周回はまだ完了していない） |
| `total` | number\|null | `raceTime`（秒） | スタートからの総飛行時間。`decimal_places` で丸め |
| `laptime` | number\|null | `lapTimeSoFar`（秒） | このラップのタイム。同上 |
| `round` | int | `round` | ヒート識別用 |
| `race` | int | `race` | ヒート識別用 |
| `position` | int | `position` | FPVTrackside 算出の順位（`pos` とは別物） |
| `finished` | bool | `raceFinishedForPilot` | その選手のこのレース最後の検出なら `true` |

**送信される条件**（すべて満たすものだけ）:

- `isLapEnd == true` … **セクタ通過は送らない**。セクタ構成（例: Aruco 4マーカー＝1プライム＋3スプリット）だと
  1周あたり最大4件来るため、絞らないと POST 量が数倍になる
- `valid != false` … タイミング系に弾かれた検出は送らない
- `detectionId` が未見 … 重複排除（直近4096件を保持）

**間引き（0.25秒バッチ）**: 間引きの単位は `(round, race, pilot, pos, lap)` ＝ **同じ選手の同じ周回だけ**。
窓の中で重複が来たら最新1件を送る。**周回が違うラップは絶対に間引かない**ので、
`lapNumber` が飛び飛びになることはない（旧版は `lap` を含まない単位で間引いていたため、
POST が遅れて溜まると同一選手の別周回が消えて `lapNumber` が欠落した）。
`RaceStart` は窓を待たず即送出し、その前に溜まっていたラップも先に送り出す。

**並列送信（`lap_senders`、既定は `1` ＝ 直列）**: 振り分け先の worker は
`(round, race, pilot, pos)` ＝ 選手単位で決まるので、増やしても**同じ選手のラップは
周回順に届く**。別の選手は並行して送られるため、Apps Script の応答が遅くても
全員のラップが詰まらない。ただし Google 側が並行実行をさばけない構成
（`LockService` で直列化している、シート同期書込が遅い等）だと、
**逆にタイムアウトや 429 が増えて届かなくなる**ので、上げる場合は
Apps Script の実行ログで失敗が無いか確認すること。

### 7.2 順位表用 URL

`Stages.json` が変化したときだけ、1回の POST に1件。

```json
{"timestamp":"2026-09-12 10:05:12","Type":"final","stages":[ …Stages.json の中身そのまま… ]}
```

| フィールド | 型 | 意味 |
|---|---|---|
| `timestamp` | string | **このデータを作成した時刻**（ローカルタイム、`yyyy-mm-dd hh:mm:ss`）。常に先頭のキー |
| `Type` | string | `"qualify"`（予選）/ `"final"`（勝ち上がり戦・決勝） |
| `stages` | array | `Stages.json` の全内容（`standings_payload="full"` の場合） |

`standings_payload` を変えると本体の形が変わる（`timestamp` と `Type` は常に先頭）:

| 値 | 形 |
|---|---|
| `full`（既定） | `{"timestamp":…, "Type":…, "stages":[ … ]}` |
| `stage` | `{"timestamp":…, "Type":…, "stage":{ …該当ステージ… }}` |
| `standings` | `{"timestamp":…, "Type":…, "name":"Ladder Finals 勝ち上がり戦", "standings":{"Headings":[…],"Rows":[…]}}` |

#### Type の判定規則

Lua スクリプト `ladder_finals.lua` の順位表の形から判定する:

1. `Standings.Headings` が **3列以上**（`Laps, Time, Status`）→ `final`
2. 2列（`Laps, Time`）でも、最終列の文字列に `ladder` / `final` / `cut` を含む → `final`
3. それ以外 → `qualify`

> したがって **`ladder_finals.lua` の順位表の列構成を変えると判定が変わります**。
> 列構成を変えた場合は `classify_standings()` も合わせてください。

#### 順位表（Standings）の読み方

`stages[].Standings` の構造（アプリが保存する Lua `standings()` の出力そのまま）:

```json
{
  "Headings": ["Laps", "Time", "Status"],
  "Rows": [
    {"Name": "ゆっきーFPV", "PilotId": "5c8a…", "Values": ["1", "8.644", "final bye"]},
    {"Name": "KPro-FPV",   "PilotId": "88ef…", "Values": ["1", "9.880", "out: ladder 4 (26 pts)"]}
  ]
}
```

- **`Rows` の並び順がそのまま順位**（先頭が1位）。行番号を付けて表示する
- `Values` は `Headings` と同じ列数の文字列配列。最後の列が Status
- Status に出る値（`ladder_finals.lua` の仕様）:

  | Status | 意味 |
  |---|---|
  | `final: 28 pts` | 決勝進出者（決勝ポイント合計） |
  | `final bye` | 予選1・2位（決勝待ち受け） |
  | `finalist` | ラダー最終段の上位2名（決勝未飛行） |
  | `ladder 2: 18 pts` | 段2を飛行中（順位未確定） |
  | `advances: ladder 3` | 段3へ勝ち上がり中（未確定） |
  | `enters: ladder 2` | 段2から出場予定（未確定） |
  | `out: ladder 3 (18 pts)` | 段3で敗退（順位確定） |
  | `out: ladder 2` | 段2に出場せず敗退 |
  | `cut` | 予選で足切り |

- ステージは `script_format`（既定 `ladder_finals.lua`）に一致するものを優先して選ぶ。
  見つからなければ「順位表を持つ最後のステージ」を使う
- 予選中は `Headings` が `["Laps","Time"]` の2列になる（Status 列が無い）

### 7.3 Google 側（Apps Script）実装上の注意

- Web アプリとしてデプロイし、アクセス権は「**全員**」（匿名）。POST 先の URL は
  `/macros/s/<ID>/exec` 形式
- `doPost(e)` では **`e.postData.contents` を `JSON.parse`** する
  （`Content-Type: application/json` で送っている）
- **処理は最小限にして即 return する**。Apps Script は1実行あたりの時間制限があり、
  シート書込は数百msかかる。受信 → `CacheService`/`PropertiesService` に保存 → 別 doGet で表示、
  の構成が安全
- **302 リダイレクト**: Apps Script は `/exec` への POST に 302 を返すことがある。
  本プログラムは**リダイレクト先へ本文を POST し直す**実装なので、Google 側での対応は不要
- **冪等性**: 最新状態だけを持つ表示用なら `round`+`race`+`pos` で上書き、
  周回毎の履歴を残すなら `round`+`race`+`pos`+**`lap`** をキーにする
  （`lap` を含めないとリトライや並列送信で同じ周回が二重に記録される）。
  順位表は「最新で全面置換」にすると、リトライや順序入替に強くなる
- クォータ: 個人アカウントは1日あたりの実行時間に上限がある。
  lap-end 絞り込み済みなので **POST 数 ≒ ラップ数**（1レース数十件）。
  `lap_senders` は同時実行数を増やすだけで総件数は変わらないが、
  実行時間が長いと1日あたりの実行時間上限に早く達する

参考（最小の受け皿）:

```javascript
function doPost(e) {
  const data = JSON.parse(e.postData.contents);
  if (data.type) {                      // ラップ用
    CacheService.getScriptCache().put('lap:' + data.round + ':' + data.race + ':' + data.pos,
                                      JSON.stringify(data), 600);
  } else if (data.Type) {               // 順位表用
    CacheService.getScriptCache().put('standings', JSON.stringify(data), 600);
  }
  return ContentService.createTextOutput('ok');
}
```

---

## 8. ローカル表示（`:5705`）

インターネットが不通でも状況を確認できるように、同じデータをローカルで表示する。
**1つの URL に複数ページ**を持ち、`/` が目次になる。見た目は `/shared.css` と
`/shared.js`（どちらも中継プログラム内の文字列を配信したもの）で共有する。

| URL | 内容 | 更新間隔 |
|---|---|---|
| `http://localhost:5705/` | 目次（4ページへのリンク＋現在の状態の要約） | 1秒 |
| `…/stat` | **レース・ステータス**（棒グラフ）。ヒート中の周回数・最終／ベストラップ・総飛行時間（Google シートの `RaceStatus` に相当） | 1秒 |
| `…/qualify` | **予選順位表**。`Type=qualify` の最後の内容を凍結して保持（勝ち上がり戦に入っても変わらない） | 2秒 |
| `…/standings` | **最新順位**。予選中は予選順位、勝ち上がり戦に入ると最新の結果を1ページで表示 | 2秒 |
| `…/live` | 診断用。受信したラップの一覧・順位表の生の値・送信統計 | 1秒 |
| `…/state` | 状態の JSON（自作の表示ページからも使える） | — |
| `http://localhost:8765/healthz` | 受信サーバの疎通確認（`fpvt2google receiver ok`） | — |
| `http://localhost:8765/state` | 受信サーバ側でも同じ JSON を返す | — |

`listen_host` が `0.0.0.0` なので、**同じ LAN の他端末からも** `http://<Mac の IP>:5705/` で見られる
（プロジェクタや観客用モニタに使える）。

### 8.1 レース・ステータス（`/stat`）

Google シートの `RaceStatus`（`GAS/raceStat.gs`）と同じ見せ方を再現する。

- `channel_pos` の **pos 毎に1列**。列の色はシートと同じ（pos1 赤 / pos2 緑 / pos3 青 /
  pos4 黄、5 以降は紫・水色を繰り返す）。`pos` が引けなかった選手は後ろに追加の列として
  出すので、`channel_pos` の設定ミスが隠れない
- 棒は**下から伸びる**。`bar_scale: "sheet"`（既定）は `bar_rows`（既定 22）段の固定スケールで、
  シートの1〜22行に対応する。`"auto"` にするとヒート内の最大周回を100%に合わせる
- 棒のいちばん上のマスに周回数を書く（シートと同じ）。`holeshot`（lap 0）は周回に数えず
  `HS` と出す
- 棒の下に**最終ラップ・ベスト・総飛行時間**と**選手名＋チャンネル**（`finished` は ★）
- `RaceStart` で全列をクリアする（シートと同じ）

ベストは中継側で畳み込む（`best_lap()`）。`board` は最新1件しか持たないので、
ラップが届くたびにヒート内の最小値を更新している。

### 8.2 予選順位表（`/qualify`）と最新順位（`/standings`）

- `/standings` は `Stages.json` の最新スナップショットをそのまま出す（`Type` バッジ付き）。
  予選中は予選順位、勝ち上がり戦に入ると勝ち上がり・決勝の順位に**自動で切り替わる**
- `/qualify` は `Type=qualify` の**最後の**スナップショットをメモリに凍結して出す
  （シートの「予選順位表」に相当）。勝ち上がり戦の内容では上書きされない。
  メモリだけなので**中継を再起動すると消える**（再起動後は `Stages.json` が既に
  勝ち上がり戦の内容になっているため復元できない）
- Status 列はシート（`GAS/raceResult.gs` の `trans()`）と同じ日本語に読み替えて出す
  （`cut`→順位確定(予選)、`out`→順位確定(勝ち上がり戦)、`advances`→上位へ勝ち上がり、
  `enters`→勝ち上がり戦、`finalist`→決勝戦進出、`final`→決勝戦）。
  読み替えは**表示だけ**で、`/state` の JSON と `/live` は生の値のまま。
  生の値はセルの `title` 属性に残す
- 順位表の更新が120秒以上止まっていると `/standings` に警告を出す

### `/state` の JSON スキーマ

```json
{
  "now": 1789104619.7,
  "hello": {"fpvtVersion": "2.78.0.894", "platform": "macOS", "eventsDirectory": "/…/events"},
  "heat": {"round": 3, "race": 5, "raceType": "Race",
           "startedAt": "2026-09-11T10:00:05.000Z", "startedMono": 1789104611.3, "elapsed": 8.4},
  "laps": [{"timestamp":"2026-09-12 10:00:33","type":"DetectionExt","pos":1,"channel":"F3",
            "pilot":"Shu_FPV","lap":3,"holeshot":false,"total":28.0,"laptime":8.3,
            "bestlap":7.9,"round":3,"race":5,"position":1,"finished":false,
            "updatedAt":1789104619.1}],
  "standings": {"type":"final","name":"Ladder Finals 勝ち上がり戦","timestamp":"2026-09-12 10:01:02",
                "headings":["Laps","Time","Status"],
                "rows":[["ゆっきーFPV","1","8.644","final bye"]],
                "source":"/…/Stages.json","updatedAt":1789104612.0},
  "qualify": {"type":"qualify","name":"Qualifying 予選","timestamp":"2026-09-12 09:40:11",
              "headings":["Laps","Time"],
              "rows":[["ゆっきーFPV","10","101.000"]],
              "source":"/…/Stages.json","updatedAt":1789103211.0},
  "stats": {"recv":27,"lap_sent":5,"lap_failed":0,"standings_sent":1,
            "standings_failed":0,"sector_skipped":12,"invalid_skipped":0,"dup_skipped":0},
  "config": {"channel_pos":{"E2":1,"E1":2,"F3":3,"F5":4},
             "bar_scale":"sheet","bar_rows":22,"decimal_places":2,
             "google_lap_url":true,"google_standings_url":true}
}
```

- `laps` は `RaceStart` でクリアされ、`pos` 順（未設定は最後、同 pos は周回数降順）に並ぶ。
  `bestlap` はヒート内の最小ラップタイム（holeshot は数えない）
- `standings` は `Stages.json` の最新、`qualify` は `Type=qualify` の最後のスナップショット
  （§8.2）。`name` はステージ名、`timestamp` は取得時刻（`yyyy-mm-dd hh:mm:ss`）
- `config.google_*` は **URL が設定されているか**の真偽値（URL 自体は出さない）。
  `bar_scale` / `bar_rows` / `decimal_places` は棒グラフの描画用（§6.2 / §8.1）
- `stats` の意味: `recv`=受信イベント総数、`lap_sent`/`standings_sent`=送信成功、
  `*_failed`=送信失敗、`sector_skipped`=セクタ通過、`invalid_skipped`=無効検出、
  `dup_skipped`=重複

---

## 9. 運用手順

### 9.1 イベント前（チェックリスト）

1. FPVTrackside のプロファイル設定で `ExtensionMode = true`、
   `NotificationURL = http://127.0.0.1:8765/` になっているか（変更したら**再起動**）
2. `fpvt2google.json` の URL 2つと `channel_pos` が今回のチャンネル割当と合っているか
3. `python3 tools/fpvt2google.py --log-file relay.log` を起動
4. ログに `receiver listening on 0.0.0.0:8765` / `dashboard listening` が出たか
5. FPVTrackside を起動 → ログに **`Hello: fpvt … / events=…`** が出るか
   （出なければ ExtensionMode が効いていない）
6. `http://localhost:5705/` が開くか（目次から `/stat` のレース・ステータスと `/standings` が見えること）
7. テストレースを1本 → `RaceStart` と `DetectionExt` のログ、`[lap]` の送信成功が出るか
8. 順位表: `Stages.json changed → Type=qualify` が出るか

### 9.2 イベント中

- 基本は放置でよい。見るのは以下
  - ローカル表示 `:5705` の `/stat`（レース・ステータス）と `/standings`（最新順位）。
    予選の結果は `/qualify` に残る
  - ログの `lap POST failed` / `standings POST failed`（ネット断・クォータ超過の兆候）
- **チャンネル割当が予定と違う**ときは、`--channel-pos "E2=1,E1=2,F3=3,F5=4"` を付けて
  再起動すればよい（`pos` は表示位置なので、分からなければ `null` のまま `channel` と
  `pilot` で表示する運用も可能）
- Google 側が落ちても中継は止まらない（失敗をログに残して次へ進む）

### 9.3 イベント後

1. `Ctrl-C` で停止
2. `relay.log` と（録画していれば）`cap.jsonl` を保存
3. トラブルがあれば §10 と照合

---

## 10. トラブルシュート

| 症状 | 原因 | 対処 |
|---|---|---|
| `Hello:` が出ない | ExtensionMode 未設定／**再起動していない**／URL・ポート違い | プロファイル設定を確認して再起動。`curl http://127.0.0.1:8765/healthz` で受信側の生存確認 |
| `Hello` は出るが `DetectionExt` が出ない | レースが流れていない／`isLapEnd` が無い（セクタのみ） | `--local-only` で `sector_skipped` が増えていないか見る。増えていれば検出は届いている（lap-end が無い＝タイミング設定を確認） |
| `pos` が `null` | `channel_pos` にそのチャンネルが無い | 送信 JSON／ローカル表示の `"channel"` の値（例 `"F3"`）を見て `channel_pos` に追加。band 名の読み替えは §6.3 の表（`Fatshark`→`F` など）を内蔵しているので、**設定には短縮名で書けばよい** |
| `channel` が `Fatshark3` のまま | 古い版を使っている | band 名の短縮は 2026-09-12 以降の版で内蔵。表に無い band は `band_short` で追加できる |
| `lap` が 0 になる | holeshot 通過（スタート直後の1回目） | 正常。`holeshot: true` が付く。ローカル表示では `HS` と出す |
| `lap` が1少ない／多い | 受信側での補正 | 本プログラムは `lapNumber` を**そのまま**送る（実データで 1 始まりを確認済み）。表示側で ±1 しないこと |
| ラップがまったく届かない（Google 側） | URL 誤り／デプロイのアクセス権／クォータ | ログの `lap POST failed (… tries): HTTPError …` を確認。`--local-only` で本文を目視 |
| Google 側で `lapNumber` が飛ぶ（例 1,2,4,6） | 旧版の間引きが `(round,race,pos)` 単位で、POST が遅れて溜まると同一選手の別周回を消していた | 2026-09-15 以降の版へ更新（間引き単位に `lap` を追加）。遅延が大きいなら `lap_senders` を増やす |
| ラップは届くが Google 側の反映が遅い | Apps Script の1実行が遅い（シート書込を同期でしている等） | `lap_senders` を増やす。Google 側は「即 return して別 doGet で表示」にする（§9） |
| `lap_senders` を上げたら POST がほとんど成功しなくなった | Apps Script が並行実行をさばけていない（`LockService` 待ち・429・実行時間超過） | `lap_senders` を `1`（直列）に戻す。ログの `lap POST failed (…) : HTTPError 429` などで原因を特定 |
| 停止時に `警告: 送信できなかったラップが N 件` | 停止時点でもキューにラップが残っていた（POST が遅い／失敗） | `lap_senders` を増やす、`lap_retries` を上げる、Google 側の処理を軽くする |
| 順位表が送られない | `Stages.json` が見つからない／`Standings` が空 | ログに `Stages.json changed` が出るか。出なければ `--events-dir` を明示、または `--event-id` を指定 |
| 別のステージの順位表が送られる | 複数ステージがある | `script_format` を実際のスクリプト名に合わせる |
| `Type` が always `qualify` | 予選中（正常）または Lua の列構成が変わった | §7.2 の判定規則を確認 |
| 同じ順位表が何度も送られる | アプリの書込が複数回に分かれている | デバウンス済み（2回連続一致）。それでも多いなら `stages_poll_sec` を上げる |
| 起動時に `起動失敗: ポートを使えません` | 8765/5705 が使用中 | `--listen-port` / `--dashboard-port` を変える。FPVTrackside 内蔵 Web サーバは 8080 なので通常は衝突しない |
| Windows で他マシンから見えない | ファイアウォール | Python の受信許可、または表示だけなら送信側マシンで開く |
| 日本語が文字化け | 受信側のエンコーディング | 送信は UTF-8（`ensure_ascii=False`）。Google 側で `JSON.parse(e.postData.contents)` を使う |

---

## 11. テストと開発

### 11.1 テスト

```bash
python3 test/fpvt2google_test.py       # 46件（unittest）
```

偽の Google エンドポイント（ローカル HTTP サーバ）と偽のイベントフォルダを使い、
次を検証している: 受信→整形→間引き→送信／`pos` 変換／`Holeshot` の周回数／
セクタ・無効・重複の除外／周回数（1始まり・holeshot=0）／**同じ選手の別周回を間引かないこと**／
**応答が遅い Google でもラップが欠落せず周回順を保つこと**（直列／並列の両方）／`RaceStart` の即時送出／Stages.json 監視と `Type` 判定／
ステージ選択／302 リダイレクトの再 POST／500 時のリトライ／local-only／record・replay／
設定の読込と探索順（実行ディレクトリ優先・`--config` の不在はエラー・`--init` の出力先）／
**ローカル表示の全ページと `/shared.css`・`/shared.js`・`/state` の配信**／
**ベストラップの畳み込み（holeshot は数えない）**／`RaceStart` での board クリア／
**予選順位表が勝ち上がり戦で上書きされないこと**／順位表スナップショットのステージ名と時刻。

Lua 側のテスト（`test/ladder_finals_test.lua` 等）も合わせて実行すること。

### 11.2 録画と再生

```bash
python3 tools/fpvt2google.py --record cap.jsonl --local-only   # 実機イベントを録画
python3 tools/fpvt2google.py --replay cap.jsonl --speed 0      # 再生（待ち時間なし）
python3 tools/fpvt2google.py --replay cap.jsonl --speed 1      # 実時間どおりに再生
```

- 録画されるのは**受信した生イベント**（フィルタ前）。`Hello` も含むので、
  再生すれば `decimalPlaces` や `primaryTimingSystemLocation` も再現できる
- 再生時は受信サーバも起動したままなので、別プロセスから PUT を混ぜることもできる

### 11.3 内部構造（拡張するとき）

| 要素 | 場所 |
|---|---|
| 既定値 | `DEFAULT_CONFIG` |
| イベント整形 | `shape_race_start()` / `shape_detection()` / `channel_key()` |
| 順位表の整形 | `pick_stage()` / `classify_standings()` / `build_standings_payload()` |
| ローカル表示の状態 | `best_lap()`（`_on_detection` で畳み込む）/ `_push_stages`（`standings` と凍結した `qualify`）/ `snapshot()` |
| ローカル表示のページ | `SHARED_CSS` / `SHARED_JS` / `page()` / `INDEX_HTML`・`STAT_HTML`・`QUALIFY_HTML`・`STANDINGS_HTML`・`LIVE_HTML` / `PAGES`・`ASSETS` |
| 中継本体 | `Relay`（`handle_event` / `_on_detection` / `_stages_watcher` / `_lap_sender`（窓＋配信）/ `_lap_worker`（並列 POST）/ `_standings_sender` / `snapshot`） |
| 間引き・振り分けのキー | `_lap_key()`（`round`+`race`+`pos`+**`lap`**）/ `_pilot_key()`（選手単位＝worker の振り分け先） |
| HTTP | `_ReceiverHandler`（PUT 受信）/ `_DashboardHandler`（表示）/ `_RepostRedirect`（302 再 POST）/ `post_json()` |
| 起動 | `parse_args()` / `build_overrides()` / `load_config()` / `main()` |

**新しいイベント種類を追加する場合**: `Relay.handle_event()` に分岐を足し、
`shape_*()` を追加して `self.lap_q.put()` するだけ（間引きは `type == "DetectionExt"` のみ対象）。

---

## 12. 制約と既知の注意点

1. **取りこぼしは回復しない**。FPVTrackside は `Hello` 以外をリトライせず、失敗した通知は
   黙って破棄する。本プログラムも再同期を要求する手段を持たないため、
   取りこぼしたら次の `RaceStart`／順位表の更新で表示が追い付くのを待つ
2. **選手の識別は名前**（`pilotName`）。`DetectionExt` にパイロットIDは含まれない。
   同名選手がいる場合は区別できない
3. **Google 側の反映は秒単位**（受信→Apps Script 実行→表示で概ね2〜6秒）。
   サブ秒のライブ表示には向かない。必要なら `:5705` のローカル表示を使う
4. **インターネット必須**。会場の電波が切れると Google への送信は失敗する
   （ローカル表示は動き続ける）
5. **順位表の鮮度はアプリの書込次第**。`Stages.json` はレースの区切りで更新されるため、
   予選順位・勝ち上がり順位は「レース毎の更新」になる（ラップのような秒単位ではない）
6. **`Stages.json` の位置は推定**。`event_id` を指定しない場合は更新時刻が最新の
   イベントフォルダを選ぶので、別イベントを同時に開いていると誤選択の可能性がある
7. 送信キューは持たない（プロセスを止めると未送信分は失われる）。
   ラップは最新1件に間引く設計なので、溜め込むことは意図していない

---

## 13. 用語

| 用語 | 意味 |
|---|---|
| ExtensionMode | FPVTrackside の「Gate / LED POST notifications」の新方式。イベントを HTTP PUT で外部へ送る |
| `DetectionExt` | ゲート通過1回毎のイベント。セクタ通過とラップ確定の両方が含まれる |
| lap-end | `isLapEnd == true` の検出＝ラップ確定。本プログラムが送るのはこれだけ |
| `pos` | 表示位置。チャンネル（band+number）から `channel_pos` で決める（レース順位ではない） |
| band / ShortBand | 周波数バンド。FPVTrackside は PUT の JSON に長い名前（`Fatshark`）で `band` を入れ、短縮名（`F`）は入れない。本プログラムが §6.3 の表で読み替える |
| `position` | FPVTrackside が計算したレース順位 |
| Standings | Lua スクリプト `standings()` が返す順位表。`Stages.json` に保存される |
| `Type` | 順位表のフェーズ。`qualify`（予選）/ `final`（勝ち上がり戦・決勝） |
| 段（tie） | 勝ち上がり戦の1つの対戦。`LADDER_HEATS` レースで競う（`ladder_finals.lua` の用語） |

---

## 14. 参考

- 方式検討・調査結果: `REPORTS.md`
- 勝ち上がり戦の仕様: `ladder_finals.md` / `AGENTS.md`
- FPVTrackside 公式マニュアル:
  <https://github.com/uewepuep/FPVTracksideCore/blob/master/documentation/FPVTrackside%20Manual.md>
- 拡張インターフェース仕様（ExtensionMode の全イベント）:
  <https://github.com/uewepuep/FPVTracksideCore/blob/master/documentation/POST_Extension_INTERFACE.en.md>
