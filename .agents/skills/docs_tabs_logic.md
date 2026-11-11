# Skill: Google Docs API (Tabs) Manipulation

## 概要

このスキルは、Google Docs APIを用いてドキュメント内のタブ構造を解析し、コンテンツを安全に上書きするためのドメイン知識を提供します。

## 重要なドメイン知識と禁忌事項

- **Tabsコンテンツの取得**: `documents.get` エンドポイントを呼び出す際、タブの内容を取得するためには URL パラメータとして `includeTabsContent=true` を **絶対に指定しなければならない**。これを怠ると、下位互換性のために最初のタブしか読み込まれず、他のタブ情報がサイレントに無視されてしまう。
- **Write Backwards 戦略（後方からの操作）**: 複数の範囲を編集・削除する場合、ドキュメントの先頭から操作するとインデックスがずれる。必ずインデックスの降順（末尾から前方へ）で操作をソートして処理すること。
- **アトミックな更新処理**: 既存コンテンツを上書きする場合、①既存範囲の削除 (`deleteContentRange`) と ②新規テキストの挿入 (`insertText`) を必ず `documents.batchUpdate` の1つのリクエスト配列にまとめてアトミックに送信・実行すること。リクエストには必ず `tabId` を指定すること。

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
