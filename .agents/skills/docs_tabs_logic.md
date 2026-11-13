# Skill: Google Docs API (Tabs) Manipulation

## 概要

このスキルは、Google Docs APIを用いてドキュメント内のタブ構造を解析し、コンテンツを安全に上書きするためのドメイン知識を提供します。

## 重要なドメイン知識と禁忌事項

- **Tabsコンテンツ・一覧の取得（常時 `includeTabsContent=True` 必須）**: `documents.get` エンドポイントを呼び出す際、Phase 1 前の既存タブ一覧取得と Re-fetch の双方で `includeTabsContent=True` を **絶対に指定しなければならない**。`includeTabsContent=False`（または省略）では下位互換のため第 1 タブの本文がルートの `body` に入るのみで、`Document.tabs` フィールド自体が空となり既存タブ一覧を取得できない。
- **Phase 1 作成先行順序と安全ガード**: ドキュメントが 0 タブになるエラーを防ぐため、`addDocumentTab` を必ず配列前半、`deleteTab` を後半に配置する。また予測タブ数が 100 を超える場合（100 タブガード）、または既存 5 件以上で全削除となる場合（全削除防止ガード）は API リクエストを 1 件も送信せず即座に Abort する。
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
