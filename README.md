# fpvt2google

**[日本語](#日本語)** | **[English](#english)**

---

## 日本語

### FPVTrackside のレース状況を Google スプレッドシートへリアルタイム中継

`fpvt2google.py` は、[FPVTrackside](https://github.com/uewepuep/FPVTracksideCore) で計測中の FPV ドローンレースの状況を、Google Apps Script 経由で Google スプレッドシートにリアルタイムで送信する Python プログラムです。

#### 機能

- **ラップデータの中継** — ゲート通過ごとのラップタイム・総飛行時間を POST
- **順位表の中継** — `Stages.json` を監視し、予選・決勝の順位表が変わったら POST
- **ローカル表示** — `:8766` で同一 LAN からも閲覧可能なダッシュボードを提供
- **録画・再生** — 受信イベントを JSONL で保存・再生してオフライン開発が可能

#### 特徴

- **Python 3.8+、標準ライブラリのみ**（pip 不要）
- **単一ファイル** — `fpvt2google.py` を置くだけ
- **macOS / Windows / Linux** 対応
- FPVTrackside への書き戻しは一切なし（読み取り専用）

#### クイックスタート

```bash
# 1. 設定ファイルの雛形を作成
python3 fpvt2google.py --init

# 2. fpvt2google.json を編集（Google Apps Script の URL を設定）
#    google_lap_url, google_standings_url, channel_pos

# 3. FPVTrackside のプロファイル設定で ExtensionMode を有効化
#    ExtensionMode = true
#    NotificationURL = http://127.0.0.1:8765/
#    → FPVTrackside を再起動

# 4. 疎通確認（dry-run: POST せずログに出力）
python3 fpvt2google.py --dry-run

# 5. 本番起動
python3 fpvt2google.py
```

#### ドキュメント

- [日本語 詳細仕様・手順書](fpvt2google.md)
- [English Documentation](fpvt2google_en.md)

#### Google Apps Script（GAS）

`GAS/` ディレクトリに Google スプレッドシート側のスクリプトが含まれています。

| ファイル | 用途 |
|---|---|
| `raceResult.gs` | 順位表（予選・決勝）を受信してスプレッドシートに書き込み |
| `raceStat.gs` | ラップデータを受信してレース状況をリアルタイム表示 |

#### 必要条件

- Python 3.8 以降（標準ライブラリのみ使用）
- FPVTrackside（ExtensionMode 対応版、動作確認: 2.78.x）
- Google アカウント（Apps Script のデプロイ用）

#### ライセンス

MIT License

---

## English

### Real-time FPV drone race data relay from FPVTrackside to Google Sheets

`fpvt2google.py` is a Python program that relays live FPV drone race data from [FPVTrackside](https://github.com/uewepuep/FPVTracksideCore) to Google Sheets via Google Apps Script.

#### Features

- **Lap data relay** — POSTs lap times and total flight time on each gate detection
- **Standings relay** — Monitors `Stages.json` and POSTs qualification/final standings when changed
- **Local dashboard** — Serves a web page on `:8766`, accessible from any device on the same LAN
- **Record & replay** — Captures raw events as JSONL for offline development and testing

#### Highlights

- **Python 3.8+, standard library only** (no pip required)
- **Single file** — just place `fpvt2google.py` anywhere
- **macOS / Windows / Linux** compatible
- Read-only — never writes back to FPVTrackside

#### Quick Start

```bash
# 1. Create a config template
python3 fpvt2google.py --init

# 2. Edit fpvt2google.json (set your Google Apps Script URLs)
#    google_lap_url, google_standings_url, channel_pos

# 3. Enable ExtensionMode in FPVTrackside profile settings
#    ExtensionMode = true
#    NotificationURL = http://127.0.0.1:8765/
#    → Restart FPVTrackside

# 4. Test connectivity (dry-run: logs without POSTing)
python3 fpvt2google.py --dry-run

# 5. Run for real
python3 fpvt2google.py
```

#### Documentation

- [日本語 詳細仕様・手順書](fpvt2google.md)
- [English Documentation](fpvt2google_en.md)

#### Google Apps Script (GAS)

The `GAS/` directory contains Google Sheets-side scripts.

| File | Purpose |
|---|---|
| `raceResult.gs` | Receives standings (qualification/final) and writes to spreadsheet |
| `raceStat.gs` | Receives lap data and displays real-time race status |

#### Requirements

- Python 3.8+ (standard library only)
- FPVTrackside (ExtensionMode-capable version, tested with 2.78.x)
- Google account (for deploying Apps Script)

#### License

MIT License
