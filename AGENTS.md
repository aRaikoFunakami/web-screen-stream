このリポジトリ（web-screen-stream）で AI（例: GitHub Copilot）と人間が作業するための **契約**。
迷ったらこのファイルを最優先する。
親リポジトリ（smartestiroid-ui）の AGENTS.md と矛盾する場合、**このファイルが優先**。

---

## 0. TL;DR（最重要だけ）

- 作業開始前に `instructions/` 配下と [設計書](../../work/web_screen_stream/design.md) を必ず読む
- 変更は **1ステップずつ** 入れて、直後に必ず検証する
- FFmpeg / Xvfb / Chromium 等のプロセスは **必ず終了する** こと（timeout / kill / スコープ制限）
- このライブラリは **Step 2 で Backend に統合される前提** で設計する
- コミットは **1コミット＝1論点**、メッセージは `type(scope): subject` を厳守する

---

## 1. 「カウントされる変更 / されない変更」

### カウントされる（進捗）

- 新機能：H.264 パイプライン / セッション管理 / WebSocket 配信（回帰テスト必須）
- バグ修正（Fail→Pass テスト必須）
- 安定性：プロセスリーク / クラッシュの排除（回帰テスト必須）
- 終了性：タイムアウト / 無限ループの排除（回帰テスト必須）
- 適合性：意味のあるテストカバレッジ追加

### カウントされない（ドリフト防止）

- ツール / インフラだけ：成果に直結しない変更
- パフォーマンスだけ：終了性や精度に繋がらない最適化
- ドキュメントだけ：混乱を解消しない文書追加

---

## 2. Non-negotiables（絶対禁止）

### 共通

- 特定ケースだけ通すハック禁止（マジック値で帳尻合わせ等）
- 仕様ショートカット禁止（「動くからOK」禁止）
- 例外握りつぶし禁止：ログに記録した上で適切にハンドルする
- 巨大差分禁止：1コミットの上限を意識して分割

### プロセス管理

- **FFmpeg / Xvfb / Chromium のプロセスリーク禁止**: `stop()` / `cleanup()` で必ず kill する
- **無制限バッファ禁止**: キュー / ring buffer には必ずサイズ上限を設ける
- **ブロッキング I/O 禁止**: FFmpeg stdout 読み取りは `asyncio.subprocess` を使用
- **シグナル無視禁止**: `SIGTERM` / `SIGINT` で確実にクリーンアップする

### ライブラリ設計

- **`app/` のコードを `src/web_screen_stream/` に混入しない**: 責務の分離を維持する
- **FastAPI への直接依存を `src/web_screen_stream/` に入れない**: ライブラリは asyncio のみに依存
- **Android 版のコードを直接コピーしない**: 必要な部分を理解して移植する（ライセンス・責務を確認）

### 依存関係

- **`pip install` 禁止**: 必ず `uv add` を使う
- **`uv add` を使わずに pyproject.toml を手動編集しない**

---

## 3. 証拠（Evidence）要件

### 証拠レベル

- 最良：自動テスト（Fail→Pass） + 回帰
- 次点：メトリクス（Before/After） + 再現手順
- 最低：手動確認ログ（スクショ / Docker ログ / hexdump）
- 禁止：「改善した」宣言のみ

### 主張→必要証拠

| 主張 | 必要な証拠 |
|------|----------|
| バグを修正した | Fail→Pass テスト |
| 性能が改善した | Before/After（数値） + 再現スクリプト |
| 終了性を改善した | timeout/プロセスリーク再現→解消テスト |
| パイプラインが動作する | hexdump で H.264 NAL units 確認 or pytest |
| ストリーミングが動作する | ブラウザでの表示スクショ or E2E テスト |

---

## 4. 開発ループ（Single-step）

1. `instructions/` と設計書を確認
2. 差異を1つ特定
3. 変更を1つ入れる
4. 直後に検証（Docker ビルド → 動作確認 or テスト実行）
5. ダメなら 2 に戻る

---

## 5. 実行（許可コマンド）

```bash
# ビルド
docker compose up -d --build

# テスト
uv run pytest tests/ -v
docker compose exec server uv run pytest tests/ -v

# 停止
docker compose down
```

---

## 6. Commit message（必須ルール）

### フォーマット
- `type(web-screen-stream): subject`
- type: `feat | fix | refactor | perf | test | docs | chore | ci | build | revert`

### 例
```
feat(web-screen-stream): add FFmpegSource for x11grab H.264 pipeline
fix(web-screen-stream): fix NAL unit boundary detection in pipe reader
test(web-screen-stream): add Late-join cache replay test
```

### 絶対禁止
- 実行していないテスト/ベンチを「実行した」と記載
- 複数論点を1コミットにまとめる
- APIキー、トークン、個人情報を書く

---

## ⚠️ 作業前の必須確認事項

| ドキュメント | 確認するタイミング |
|-------------|-------------------|
| **[instructions/architecture.md](instructions/architecture.md)** | プロジェクト構成・ディレクトリ役割の把握 |
| **[instructions/protocol.md](instructions/protocol.md)** | WebSocket 仕様・ライブラリ API・FFmpeg コマンドの確認 |
| **[instructions/development.md](instructions/development.md)** | 開発手順・Docker 操作・トラブルシューティング |
| **[設計書](../../work/web_screen_stream/design.md)** | 全体設計・2段階アプローチの理解 |
| **[開発計画](../../work/web_screen_stream/plan.md)** | 次のタスク確認（★毎タスク読む） |
| **[親 AGENTS.md](../../AGENTS.md)** | smartestiroid-ui 全体のルール |
