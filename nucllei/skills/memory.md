---
title: Memory
description: "会話を跨いで残す記憶 (ユーザーの所属・好み・応答スタイルの希望・進行中の仕事・自分用の参照情報) の読み書き。『覚えておいて』『記憶して』『メモして』『次からは〜して』と残す指示、『覚えてる?』『前に言った私の〜』『私の〜は?』『いつもの』と記憶を当てにした時、『忘れて』『何を覚えてる?』の時にロードする。手順・やり方を残す・使う話 (『この手順を覚えて』『レシピにして』『〇〇のレシピで』) は skill recipes。『前に話した〜』『この前の相談』のように過去の会話で扱った中身 (相談の結論・作った文面・資料) を指すときは記憶でなく会話履歴で、grep_history / read_history を使う (この skill は要らない)。"
---

# 記憶 (会話を跨いで残す)

保存先は sandbox の `/workspace/memory/` (ユーザーごとに 1 つ、会話を跨いで共有。workspace を 1 年使わないと workspace ごと消える。`/workspace/imports/` `/workspace/exports/` `/workspace/trash/` は別で、入れたものは約 1 か月で消える)。
読むのは `read_file`、書くのは `write_file` (テキスト上書き)、CSV の追記だけ `run_python_once`、`ls`・`rm` は `run_shell`。
今日の日付は {{CURRENT_DATE}}。
記憶にあるのは残すよう頼まれたものだけ。過去の会話の中身は記憶ではなく `grep_history` で探して `read_history` で読む (MEMORY.md に無いからといって「話していない」とは言わない)。

## 何を記憶に入れるか

会話が終わっても残す価値がある、**ユーザー自身についての持続的な事実**だけ。

| 種類 | 例 | 置き場 |
|---|---|---|
| user — 誰か | 所属科・専門・立場・よく扱う疾患 | `profile.md` |
| feedback — 応答の希望 | 「英語で」「箇条書きは嫌」「単位は mg/dL」 | `preferences.md` |
| project — 進行中の仕事 | 研究テーマ・締切・使っている解析手法・次にやること | `projects.md` (増えたら 1 件 1 ファイルに分割) |
| reference — 自分用の参照 | 略語の自分流の意味・よく使う計算条件・個人的な連絡先 | 用件ごとのファイル |
| 同じ列を持つ記録の並び | 学会・締切一覧、当直パターン、読んだ論文リスト | `*.csv` |

**入れないもの**: 患者個人を特定できる情報 (氏名・ID・個別患者の経過。記憶は「あなたについて」であって
症例記録ではない) / パスワード・秘密 / その場限りの作業データ (それは作業場所の普通のファイル) /
会話中に既に分かっていること。頼まれても患者情報とパスワードは断り、代替 (匿名化・workspace の
通常ファイル) を提案する。

**保存するのは明示の依頼 (『覚えておいて』等) と、明確な『次からは〜して』型の指示だけ。**
それ以外の会話内容を自発的に保存しない (患者情報が混ざる会話が多い)。保存したら本文で 1 行、
何をどこに残したか言う (全文は貼らない)。

## 形式の選び方

- **Markdown** (既定): 文で書ける事実。1 ファイル 1 トピック、先頭は `# タイトル` と `更新: YYYY-MM-DD`、
  本文は `- (YYYY-MM-DD) 事実` の箇条書き。日付は「新しい方が勝つ」の判定と古い記憶の見直しに使う。
  1 ファイルの目安 3KB — 超えたらトピックを分ける。
- **CSV**: 「同じ列を持つ行が増えていく」もの。1 行目はヘッダー、UTF-8、追記は必ず `csv` モジュール
  (手で文字列連結するとクォートが壊れる)。1〜2 行しか無いなら Markdown で足りる。
- ファイル名は ASCII の kebab-case (`profile.md`, `conference-deadlines.csv`)。中身は日本語でよい。

## MEMORY.md (索引)

`/workspace/memory/MEMORY.md` は全記憶の目次。**読むときも書くときも、中身のファイルより先にこれを読む**。

```
- [プロフィール](profile.md) — 呼吸器内科、肺癌の臨床研究
- [学会・締切](conference-deadlines.csv) — 列: date,name,kind,note
```

1 ファイル 1 行、ファイルを作る/消す/趣旨が変わるたびに書き直す。目安 40 行。
索引に無いファイルは存在しないのと同じなので、`ls` と食い違っていたら `ls /workspace/memory/` の結果で作り直す。

## 手順・やり方は記憶でなくレシピ

「この手順を覚えて」「このやり方を次からも」「これをレシピにして」「私のスキルにして」のように**やり方** (手順・テンプレート・
チェックリスト・SQL) を残す・使う話は、記憶に書かず `load_skill` で skill `recipes` をロードしてそれに従う
(置き場は `/workspace/recipes/`。`/workspace/memory/` の外)。1 行で言える応答の好み (「英語で」「単位は SI で」) は記憶の `preferences.md`、
手順を踏むもの・テンプレートはレシピ。どちらか分からなければ聞く。「いつもの手順で」「前に決めた形式で」のように『レシピ』と言わずに
自分の手順を指されたら、記憶を見たうえで、無ければ「レシピとして残してあれば探します」と聞く (聞かずに探さない)。
`MEMORY.md` に `skill:` の行がある・`/workspace/memory/skills/` が残っているなら旧形式なので、skill `recipes` の「旧置き場からの移行」に従う。

## 手順

どの手順も最初に `run_shell("ls -A /workspace/memory 2>/dev/null || true")` で中を見る。
出力が空ならまだ何も無い (初回。`/workspace/memory/` は `write_file` が作る)。`MEMORY.md` が出ていれば、それを `read_file` で読むところから始める
(`read_file` は在るのを見てから呼ぶ。無いものを `read_file` しない)。`MEMORY.md` が無いのに他のファイルがあれば初回ではない —
先に `ls` の結果から索引を作り直してから進む (上の「MEMORY.md (索引)」)。

### 残す (『覚えておいて』『次からは〜』)

1. 上の `ls` で中を見て、`MEMORY.md` があれば `read_file("/workspace/memory/MEMORY.md")` を最後まで (`next_offset` が出たら続きも。出力が空なら初回 — 索引は手順 4 で作る)
2. 索引から入れる先を決める。既存トピックなら `read_file("/workspace/memory/<file>")` で**全文を手元に置く** (`next_offset` が出たら最後まで)
   (`write_file` は上書きなので、読まずに書くと他の記憶が消える)
3. 既存ファイルは全文を書き直す形で `write_file("/workspace/memory/<file>", …)` — 同じ事実は 1 行に統合、矛盾する古い行は消して
   新しい方だけ残す (「以前は X だった」は必要な時だけ残す)、`更新:` を今日にする
4. 新ファイルなら `write_file("/workspace/memory/<file>", …)` で作り、続けて `write_file("/workspace/memory/MEMORY.md", …)` で索引も全文書き直す
5. CSV 追記は `run_python_once`:
   ```python
   import csv, pathlib
   p = pathlib.Path("/workspace/memory/conference-deadlines.csv")
   new = not p.exists()
   with p.open("a", newline="", encoding="utf-8") as f:
       w = csv.writer(f)
       if new:
           w.writerow(["date", "name", "kind", "note"])
       w.writerow(["2026-10-15", "日本肺癌学会", "演題締切", ""])
   ```
6. 本文で 1 行報告 (例: 「preferences.md に『検査値は SI 単位で』を追加しました」)

### 思い出す (『覚えてる?』『いつもの』『私の〜』)

1. 上の `ls` で中を見て、`MEMORY.md` があれば `read_file("/workspace/memory/MEMORY.md")`
2. 用件に関係する行のファイルだけ `read_file` (全部は読まない。大きな CSV は `run_shell` の `rg` / `head` で絞ってよい)
3. 見つかったら、それを根拠に答える。**無ければ「記憶にありません」と言い、推測で補わない** (過去の会話で扱った話なら `grep_history` で探す)。
   同じ主題で日付が違う記述が残っていたら新しい方を採る

`ls` の出力が空 (まだ何も無い) のときは、そう伝えて終わる。ファイルを作ってはいけない。

### 忘れる (『忘れて』『消して』)

レシピを消す話 (『〇〇のレシピを消して』) は記憶ではない — skill `recipes` の「消す」に従う。

- 1 件: 該当ファイルを `read_file` で最後まで (`next_offset` が出たら続きも) → その行を除いて `write_file` で書き直す。ファイルが空になるなら
  `run_shell rm /workspace/memory/<file>` して索引からも消す
- ファイル/トピック丸ごと: `rm` + 索引更新
- **全消去 (『全部忘れて』) は実行前に確認を取る** — 頼まれたことは了承ではない。聞いたらそのターンを終え、次のターンで了承されてから `rm -r /workspace/memory`。レシピ (`/workspace/recipes/`) は記憶ではないので消えない — 確認のときにそう添え、レシピも消したいと言われたら skill `recipes` の「消す」に従う
- 消したら 1 行報告

### 一覧 (『何を覚えてる?』)

上の `ls` で中を見て、`MEMORY.md` があれば `read_file` で読み (`ls` の出力が空なら「まだ何も覚えていません」)、文にして見せる (ファイル本文の羅列はしない)。レシピは記憶に含まない (一覧は skill `recipes`)。
「中身も見たい」と言われたファイルだけ `read_file` して見せる。
ファイルとして欲しいと言われたら `run_shell`: `cd /workspace/memory && zip -r /tmp/memory.zip . && cp /tmp/memory.zip /workspace/exports/` で渡す (`exports/` に写したものが添付される)。

## 注意

- 記憶は**この skill をロードした時だけ**読む。会話の冒頭で自動的には読み込まれないので、
  ユーザーが記憶を当てにしている気配 (『いつもの』『前に頼んだ通り』) があれば自分からロードする
- workspace 全体が失われる操作 (`rm -r /workspace/*` 等) は記憶も一緒に消える。記憶に触る時は
  `/workspace/memory/` 配下だけを相手にする
