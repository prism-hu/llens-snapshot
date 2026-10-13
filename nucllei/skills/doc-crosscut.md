---
title: Doc Crosscut
description: "doc の複数文書・複数カテゴリにまたがる集計と横断の進め方。『全ガイドラインで◯◯を比較』『◯◯について書いてある文書を全部』『◯◯は全部で何件か (複数ディレクトリの合算)』『点数が N 以上の区分』『入院日Ⅱが 30 日以上の分類』『front matter の値で絞る』のように、1 つの skill のカタログや grep_doc 1 回に収まらない数え上げ・絞り込み・横断を求められたら、作業を始める前にロードすること。sandbox に読み取り専用で置いてある /ext/doc (doc 本文のファイル。doc の path と同じパス) と /ext/doc-static (図・原本 PDF・原本 CSV/xlsx) の使い方を持つ。内容の比較・まとめは sandbox で本文を集めず read_doc で読む (その進め方もここ)。"
---

# doc の集計と横断

doc と同じ文書がサンドボックスに読み取り専用で置いてある (`/ext/doc/` `/ext/doc-static/`)。
**用途は doc ツールに書けない条件でノードを絞る・数えること** — 数値の大小 (「点数が N 以上の区分」
「入院日Ⅱが 30 日以上の分類」)、複数条件の組合せ、front matter の値、原本 CSV/xlsx の列。

- 1 語の出現ノード数は `grep_doc` の `match_count` (`mode` が exact のとき) で足りる。1 ディレクトリ内の件数は `list_doc` の `child_count`
- **多数ノードの内容を集める・まとめる (比較・一覧の要約) のは横断ではなく読み** — `read_doc` で行い、sandbox で本文を集めない
- 比較・まとめは、まず対象カテゴリごとの skill (`doc-*`) をロードし、そのカタログから起点の path を取る。`grep_doc(path="/ext/doc/")` から始めない
- 範囲が広すぎる問いは、中間ノードの範囲説明を示して絞ってもらう

## `/ext/doc/` — 本文

1 ノード = 1 ファイルで、**sandbox のパスがそのまま doc の path**。リーフは `/ext/doc/…/<名前>.md`。ディレクトリのノードは `/ext/doc/…/<dir>/` で、その本文が `<dir>/_index.md` (本文の無いディレクトリは `_index.yaml`)。

- `rg -l` / `rg -c` / `find` (条件が複雑なら python で読んで絞ってもよい) で**ファイル名と件数だけ**を stdout に出す (本文行を出力しない)
- 当たったファイルは、そのパスを `read_doc(path=)` に渡して読む (`<dir>/_index.md`・`<dir>/_index.yaml` は `<dir>/` を渡す)。パスを書き換えない
- ファイルは front matter 付き。**引用と図の URL は `read_doc` の戻り値から取る**。出典は `read_doc` で読んだノードだけに付ける。本文は `read_doc`

## `/ext/doc-static` — 図・原本 PDF・原本 CSV/xlsx

- 原本 CSV/xlsx を pandas で引く経路はここ (`send_to_workspace` での取り込みは不要)。**パスと列の対応表は各 skill が持つ** — 診療行為マスター CSV は `doc-shinryo-hoshu`「コード単位で網羅的に調べたいとき」、DPC の xlsx は `doc-dpc`「分類をまたいで調べたいとき」をロードして literal を得る。sandbox で中身を読み込んでよい原本 (pandas 等で開くもの) は skill が名乗る `/ext/doc-static/…` の literal だけ (見つけたファイルを `feed_image(path=)` で見るのとは別の話)
- 図・原本 PDF を自分で見るのは `feed_image(url=)` に `read_doc` の戻り値の URL を渡すのが基本。sandbox の作業で見つけた `/ext/doc-static/…` のファイル (`rg` / `find` の結果など) は、そのパスのまま `feed_image(path=)` で見てよい
- 原本 PDF をテキスト化し直しても doc 本文以上の文字は出ない (画像しか無いページは `feed_image` で見る)
- **ユーザーに見せる URL は tool の戻り値 (`read_doc` / `fetch_from_workspace`) か、sandbox で見つけた `/ext/doc-static/…` の literal だけ。組み立てない・書き換えない** (`/ext/doc-static/…` は sandbox のパスと URL が同じ文字列なので、`rg` / `find` で見つけたものをそのままリンク先にしてよい。doc の `/ext/doc/…` は `read_doc` で読んでから出典として示す)
