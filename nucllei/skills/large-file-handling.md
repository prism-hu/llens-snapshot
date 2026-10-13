---
title: Large File Handling
description: "巨大な添付を、中身を一度に context へ展開せずに自分で処理する手順。添付の注記が『大きすぎる』ため本文を読み込んでいないと告知した時、または巨大な CSV / Excel / PDF / ログ等を全文を会話に出さずに集計・要約・抽出したい時は、作業を始める前に本スキルをロードしてその手順に従うこと (run_python での集計・抽出・絞り込みと、read_attachment の offset で要る範囲だけ読む手順を持つ)。『抽出の対象外の形式』の告知は file-conversion。"
---

# 大容量ファイル処理 (中身を一度に context に出さずに扱う)

## いつ使うか

- **添付の注記** (システムからの注記) が次を告知した時。そのファイルの中身は**プロンプトに無い**ので、file_id から取りに行く。**告知の種類で経路が違う**:
  - 「大きすぎるため、本文をこの会話に読み込んでいない」(file_id が並ぶ) → 下の手順で処理する
  - 「抽出結果が空だった (file_id=…)」→ PDF ならテキスト層が無い。`feed_image(file_id=…)` で見る (`pypdf` も空を返す)。PDF 以外は `send_to_workspace` で取り込んで原本を読む
  - 「抽出の対象外の形式」の告知は本 skill の対象外 (`send_to_workspace` → skill `file-conversion` で変換)
- 巨大な CSV / Excel / PDF / ログ等を、**全文を会話に展開せずに**集計・要約・抽出したい時。

## 大原則

1. **全文を出さない。** stdout も `read_attachment` の戻り値も会話 context に入る。出してよいのは「サイズ・shape・行数・列名・ページ数などのメタ情報」と「file_id」、集計値、そして**答えに要ると絞り込んだ範囲**だけ。個票・本文を丸ごと出さない (院内 = 個人情報前提)。
2. **`file_id` を直接受け取る tool があるなら、それに渡して終わり。** 取り込みも run_python も要らない (下表)。
3. **前処理 (削る・割る・集計・変換) は `run_python` でやる。** サーバー実行なので速く、pandas / pypdf / openpyxl / python-docx 等がそのまま使える。会話内で変数・import は永続 (notebook のセル)。
4. **中身の理解が要るときも、読むのは絞った後。** 先に目次や計算で範囲を決め (パターン B)、要る範囲だけを読む。頭から順に読み切らない。
5. **並行させたい呼び出しは 1 回の応答に並べる** (同時に走る。sandbox の tool は並べても直列)。`background: true` は時間のかかる呼び出しを待たずに別の作業を進めたいときだけ。

## 渡し先を先に決める

`file_id` をそのまま受け取る tool は、添付を**取り込まずに**処理できる (手元の tool にあるものだけ使う):

| やりたいこと | 渡し先 |
|---|---|
| スキャン PDF・生成物の画像を見る (直接添付の画像は既に見えている) | `feed_image(file_id=…)` |
| 抽出済みの本文を範囲を区切って読む | `read_attachment(file_id=…, offset=…)` — パターン B |
| 音声を文字起こしする | `transcribe_speech(file_id=…)` — 長い録音は `start_sec=…` で続きを取る (長さは `inspect_audio`) |

## 取り込み (run_python で自分で開く時だけ)

`run_python` の jail からは `/api/v1/...` を直接叩けない。**jail 内でファイルを開く必要がある時に限り**、`send_to_workspace` で `/workspace/imports/` に取り込んでから読む。

- **注記で外された添付** → 注記の `file_id` で取る: `send_to_workspace(file_id="…")`
- **URL のリソース** (`fetch_url` の戻り値に出た院内 Web の `http(s)://…`) → `send_to_workspace(url="…")` (URL は literal のまま、ホストを補わない。jail 内で curl はできない)。doc の図・原本 `/ext/doc-static/…` は取り込まれず sandbox のパスが返るだけ (次の行。doc 本文 `/ext/doc/…` は `read_doc` で読む)
- **doc の原本 CSV/xlsx** → 取り込まない。jail に `/ext/doc-static/...` として最初からあるので、そのパスを直接読む (列の対応表は各 doc skill)。図・原本 PDF を見るだけなら `feed_image(url=…)` (read_doc の戻り値の URL。sandbox で見つけた `/ext/…` のファイルならそのパスで `feed_image(path=…)`)。原本そのものをユーザーに渡すなら、そのパス `/ext/doc-static/…` は同じ文字列が URL なので、その literal をそのままリンク先にして `[ラベル](…)` で本文に書く (裸のパスはリンクにならず、ラベル無しの `[/ext/…]` は出典の番号になる)
- **通常の添付** → 名前で取る: `send_to_workspace(filename="report.csv")` (部分一致)

取り込んだら run_python で `/workspace/imports/<name>` を読む。

## run_python での処理

サーバー実行なので pandas / pypdf / openpyxl 等をそのまま普通に使ってよい。まず**メタだけ**確認して戦略を決める:

```python
import pandas as pd
df = pd.read_csv("/workspace/imports/report.csv", encoding="cp932")  # 電子カルテ系は cp932 が多い
print(df.shape, list(df.columns))   # 形と列名だけ (値は出さない)
```

- **エンコーディング**は決め打ちしない: `utf-8-sig` → 駄目なら `cp932` → それでも駄目なら chardet。
- **テキスト層の無いスキャン PDF は `run_python` では読めない** (pypdf が空を返す) — 見るなら `feed_image`、束を文字にするなら skill `file-conversion` の OCR。
- 状態は会話内で保持され、前セルの `df` を次セルでそのまま使える (最長 12 時間でリセット。戻り値に注記が出たら定義し直す)。

## パターン

### A. 計算だけで済む (集計 / 絞り込み / 変換 / ルール抽出) — 最も多い

本文を読む必要は無い。run_python で処理し、**小さい結果だけ** print する。

```python
n = (df["科"] == "呼吸器内科").sum()
print("呼吸器内科の行数:", int(n))      # 出すのは集計値だけ
# ユーザーが絞り込んだファイル自体を求めた時だけ、作業場所で作ってから exports/ へ写す (自動でチャットに添付される。個票を勝手に添付しない)
import shutil
out = "/workspace/<YYYYMMDD>-<作業名>/subset.csv"
df[df["科"] == "呼吸器内科"].to_csv(out, index=False)
shutil.copy(out, "/workspace/exports/")
```

### B. 中身の理解が要る (要約・記述の確認・文脈の読み取り)

読む前に範囲を決める。要らない部分を読まないことが、この skill の目的そのもの。

- **添付の抽出本文を区切って読む** — `read_attachment(file_id=…)` は抽出済みの本文を 1 回に一部 (`max_chars` 字、既定 40000) だけ返し、戻り値に全体の字数 `total_chars`、続きがあれば `next_offset` が出る。先頭 (表紙・目次・見出し) を読んで当たりを付け、要る範囲だけ `offset=…` で取る。続きは要るところまで (`next_offset` が出ていても、答えが揃ったら読み切らない)
- **sandbox のテキストを区切って読む** — `read_file(path=…)` も 1 回に一部だけ返す。続きは戻り値の `next_offset` をそのまま `offset` に渡す (`read_attachment` とは数え方が違う。自分で計算した値を渡さない)。読むのは上と同じく要るところまで
- **位置が分からない・条件で抜きたい** → run_python で該当部分だけ抜いてファイルに書き、件数・字数だけ print する:
  ```python
  # text は取り込んだ原本から抜いた全文 (pypdf / pandas 等で。中身は print しない)
  hits = [ln for ln in text.splitlines() if "呼吸器" in ln]
  open("/workspace/<YYYYMMDD>-<作業名>/extract.txt", "w").write("\n".join(hits))
  print("抽出行数:", len(hits), "字数:", sum(map(len, hits)))
  ```
  抜いた結果は `read_file(path="/workspace/<YYYYMMDD>-<作業名>/extract.txt")` で読む (`next_offset` が続くほど長ければ抜き方を絞り直す)。自分で読むだけなら、抜いたファイルを `fetch_from_workspace` で添付にしない (チャットに付き、ユーザーにも見える)
- **全体の要約・網羅が要る** → 1 回で読み切ろうとしない。run_python で章・ページ・期間などの単位に割り、単位ごとに要る範囲を読んで要点を短く書き留め (作業場所の `notes.md` などに書いてよい)、最後に統合する。本当に全網羅が要るか先に検討する (多くは A か「位置が分からない」の絞り込みで足りる)

## fetch_from_workspace の注意

`fetch_from_workspace(path)` は sandbox のファイル (`/workspace/…` `/ext/…`) を利用者に渡す。`/ext/…` は添付せず同じファイルを指す URL が返る (percent-encode で表記が変わることがある。渡さずにパスをそのままリンク先にしてもよい)。doc の `/ext/doc/…` は渡さない (本文は `read_doc` で読み、出典の形で示す)。それ以外は Files に登録し `file_id` を返す (チャットにも添付されユーザーに見えるが、tool が付けた添付は次ターンに再注入はされない)。ユーザーに渡す最終成果物は作業場所で作り、できたものを `run_python` / `run_shell` で `/workspace/exports/` へ写せば自動添付される。作業途中のファイルは、file_id にして渡す先があるとき以外は添付にしない (自分で読むなら `read_file` で足りる)。

## やってはいけないこと

- 中身 (本文・個票・全抽出テキスト・DataFrame 全体) を丸ごと print / 応答本文に貼る。
- `file_id` を直接受け取る tool で足りるのに `send_to_workspace` で取り込む。jail の disk を無駄に食う。
- 巨大ファイルを頭から `next_offset` で読み切ってから考える → context が尽きる。数十万字級は先に run_python で削る / 割るか、目次から当たりを付けて範囲を決める。
- 取り込みや file_id 化を挟まず、jail から直接ダウンロードしようとする (ネットワーク不可)。
