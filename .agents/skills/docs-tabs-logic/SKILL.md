---
name: docs-tabs-logic
description: >
  Google Docs API（Tabs 機能）を用いたドキュメント構造解析・タブ操作・Phase 1/Phase 2 Two-Step 同期パイプライン・
  フラットタブ命名（50文字制限）および安全ガード（100タブ制限・全削除防止・作成先行順序・孤立タブ削除）の
  実装・修正・レビュー時に使用。
---

# Skill: Google Docs API (Tabs) Manipulation

## 概要

このスキルは、Google Docs APIを用いてドキュメント内のタブ構造を解析し、コンテンツを安全に上書きするためのドメイン知識を提供します。

## 重要なドメイン知識と禁忌事項

- **Tabsコンテンツ・一覧の取得（常時 `includeTabsContent=True` 必須）**: `documents.get` エンドポイントを呼び出す際、Phase 1 前の既存タブ一覧取得と Re-fetch の双方で `includeTabsContent=True` を **絶対に指定しなければならない**。`includeTabsContent=False`（または省略）では下位互換のため第 1 タブの本文がルートの `body` に入るのみで、`Document.tabs` フィールド自体が空となり既存タブ一覧を取得できない。
- **Two-Step パイプライン（Phase 1 構造同期 / Phase 2 コンテンツ同期）**:
  - **Phase 1（構造同期）**: `addDocumentTab` および `deleteTab` によりタブの過不足を解消する。
  - **Re-fetch**: `documents.get(includeTabsContent=True)` を再実行し、新規作成されたタブの `tabId` と各タブの最新 `endIndex` を取得する。
  - **Phase 2（コンテンツ同期）**: Re-fetch で確定した `tabId` と `endIndex` に基づき、本文のクリアと挿入を実行する。
- **フラットマッピング & 50 文字ハッシュ短縮 (`generate_flat_tab_title`)**: 子タブ（Nested Tabs）は Gemini Notebook がインジェストできないため作成禁止。すべて第 1 階層のフラットなタブとし、相対パスの `/`・`\`・`.` を `_` に置換する。50 文字を超える場合は `先頭41文字 + '_' + SHA-256先頭8文字` の計 50 文字に決定論的に短縮する。
- **Phase 1 作成先行順序と安全ガード**: ドキュメントが 0 タブになるエラーを防ぐため、`addDocumentTab` を必ず配列前半、`deleteTab` を後半に配置する。また予測タブ数が 100 を超える場合（100 タブガード: `PredictedTabs = CurrentTabs - TabsToDelete + TabsToCreate > 100`）、または既存 5 件以上で全削除となる場合（全削除防止ガード）は API リクエストを 1 件も送信せず即座に Abort する。
- **孤立タブ削除 (`detect_orphan_tabs`)**: 全量同期（`--force`）時は、GitHub 側の有効タブ名一覧にも初期デフォルトタブ名（`Tab 1`, `タブ 1` 等）にも含まれない既存タブを孤立タブとして検出し削除対象とする（差分同期時は今回の差分で `removed` / `renamed` となったタブのみを削除対象とし、他タブには干渉しない）。
- **Write Backwards 戦略（後方からの操作）**: 複数の範囲を編集・削除する場合、ドキュメントの先頭から操作するとインデックスがずれる。必ずインデックスの降順（末尾から前方へ）で操作をソートして処理すること。
- **アトミックな更新処理とチャンク分割**: 既存コンテンツを上書きする場合、①既存範囲の削除 (`deleteContentRange`、`endIndex <= 2` の空タブ時はスキップ) と ②新規テキストの挿入 (`insertText`) を同一チャンク内でペアとしてまとめ、ソケットタイムアウト対策として **最大 10 タブ（20 リクエスト）単位** で分割送信すること。リクエストには必ず `tabId` を指定すること。

## バッチ更新のペイロード要件（厳密なフォーマット）

挿入および削除のリクエストには、必ず `Location` オブジェクト内に `tabId` を指定すること。

```json
{
  "requests": [
    {
      "deleteContentRange": {
        "range": {
          "startIndex": 1,
          "endIndex": 500000,
          "tabId": "TAB_ID_HERE"
        }
      }
    },
    {
      "insertText": {
        "location": {
          "index": 1,
          "tabId": "TAB_ID_HERE"
        },
        "text": "新しい同期コンテンツ"
      }
    }
  ]
}
```
