---
name: maintain-project-docs
description: >
  src/、config.yaml、または .github/workflows/ のコード・設定を変更したタスクの完了時に使用。
  仕様変更の有無を判定し、docs/、.agents/、README.md の更新が必要か判断して、
  必要な場合のみ最小限の差分で同期更新する。
---

# Project Documentation & Agent Skill Maintenance

コードや設定ファイルの変更完了時に、`docs/`・`.agents/`・`README.md` の更新が必要かどうかを判定し、**必要な場合のみ**最小限の差分で同期更新する。

## 1. 更新要否の判定マトリクス

変更内容を以下の基準と照合し、1つでも「要更新」に該当する場合のみ対象ファイルを更新する。すべて「更新不要」に該当する場合は、ドキュメント類には一切手を加えない。

| 変更の種類                                                                                                           | 判定         | 更新対象ファイル                                                                             |
| :------------------------------------------------------------------------------------------------------------------- | :----------- | :------------------------------------------------------------------------------------------- |
| **データモデル・アルゴリズム・正規表現の変更**<br>（`models.py`, `liquid_resolver.py`, `path_utils.py` 等）          | **要更新**   | `docs/Detailed_Logic.md`<br>`.agents/skills/github_sync_logic.md`                            |
| **API 制限・フェイルセーフ・トランザクション規約の変更**<br>（`docs_client.py`, `github_client.py` 等）              | **要更新**   | `docs/Detailed_Logic.md`<br>`docs/System_Overview.md`<br>`.agents/skills/docs_tabs_logic.md` |
| **CLI オプション・実行方法・環境変数・依存関係の変更**<br>（`main.py`, `requirements.txt`, `.github/workflows/` 等） | **要更新**   | `README.md`<br>`docs/System_Overview.md`                                                     |
| **ディレクトリ構成の追加・削除・変更**                                                                               | **要更新**   | `.agents/AGENTS.md`<br>`README.md`                                                           |
| **分冊（Doc 2〜8）の有効化・監視パス変更**<br>（`config.yaml` の更新）                                               | **要更新**   | `docs/System_Overview.md`<br>`README.md`                                                     |
| **既存仕様どおりのバグ修正・内部リファクタリング**                                                                   | **更新不要** | なし                                                                                         |
| **テストコード（`tests/`）のみの追加・修正**                                                                         | **更新不要** | なし                                                                                         |

## 2. 更新時の厳守ルール

1. **実装との完全一致（推測禁止）**: 関数名、正規表現、定数は必ず実際の `src/` コードと一字一句一致させる。
2. **冗長な変更履歴の禁止**: 日記風の修正履歴は書かず、常に「現在の最新仕様」として記述を書き換える。
3. **コミット分離**: 原則として機能実装とドキュメント同期のコミットは分ける。
