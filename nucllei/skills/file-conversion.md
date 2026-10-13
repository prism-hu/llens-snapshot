---
title: File Conversion
description: "sandbox でのファイル変換・展開の手順。旧形式の Word / Excel / PowerPoint (.doc / .xls / .ppt)・RTF・Outlook の .msg・Access (.mdb / .accdb)・.dbf・SPSS (.sav) を開く・docx / xlsx / pdf に変換する、zip / LZH / 7z を解凍する (日本語ファイル名の文字化け・パスワード付きも)、スキャン PDF や画像の束を OCR で文字にする (『PDF を文字起こし』もここ)、PDF を軽くする・壊れた PDF を直す、新旧 2 つの版の差分 (新旧対照) を出す、院外に出す前に作成者名などのメタデータを消す、のいずれかをするときにロードする。添付の注記が『抽出の対象外の形式』と告知した添付もここ (『大きすぎる』の告知は large-file-handling、音声の文字起こしは audio)。"
---

# ファイル変換 (sandbox)

対象の添付は先に `send_to_workspace(file_id=…)` で `/workspace/imports/` に取り込む
(戻り値の絶対パスをそのまま使う。日本語・空白を含むのでシェルでは必ず `"…"` でクォート)。
下のコマンドは `run_shell` で打つ。出力先は作業場所 (`/workspace/<YYYYMMDD>-<作業名>/`。`mkdir -p` してから)。
**`/workspace/exports/` に作業中のファイルを置かない** — 置いたものは全部チャットに添付される。
`/tmp` は 256MB で、`run_shell` では実行ごとに消える。プロセス置換 `<(…)` は使えない (一時ファイルに書く)。

変換したあと:
- **中身を答えに使う** → `/workspace/<YYYYMMDD>-<作業名>/` にテキストを書き出し (`pandoc x.docx -t plain -o /workspace/<YYYYMMDD>-<作業名>/x.txt` / `pdftotext x.pdf /workspace/<YYYYMMDD>-<作業名>/x.txt` / `xls2csv -d utf-8 x.xls > /workspace/<YYYYMMDD>-<作業名>/x.csv`)、`read_file` で読む。
  全文は要らない・長くて `next_offset` が続くなら、skill `large-file-handling` の手順で絞ってから
- **見た目・図・表の配置を確かめる** → pdf にして `feed_image(path="/workspace/<YYYYMMDD>-<作業名>/x.pdf", pages=…)`
- 展開した中身 (zip の中の pdf / docx 等) は添付ではないので file_id は無い。path で扱う
- **ユーザーが変換後のファイル自体を求めた** (docx にして / 軽くして / メタを消して) → 仕上がりだけを `/workspace/exports/` にコピーする。
  途中の docx 化・展開した中身・OCR のテキスト・患者を一覧にした抽出物は置かない

## 変換の手順

- **旧 Office (.doc / .xls / .ppt) と相互変換**: `soffice --headless --convert-to docx --outdir /workspace/<YYYYMMDD>-<作業名> "<file>"` (.xls は `xlsx`、.ppt は `pptx` か `pdf`。LibreOffice。`--convert-to png` は先頭ページだけなので全ページは pdf → `pdftoppm -png`)。テキストだけなら `antiword -m UTF-8.txt x.doc` (文字が落ちるなら soffice で docx 化) / `xls2csv -d utf-8 x.xls` (catdoc)。`.ppt` は pptx か pdf に変換してから読む。出力名は入力の拡張子を替えただけなので、同名の .doc と .xls を同じ outdir で pdf にすると上書きし合う
- **RTF**: `soffice --convert-to docx` か `unrtf --html x.rtf | pandoc -f html -t plain` (`unrtf --text` は日本語を落とす)
- **圧縮**: zip / LZH / 7z は `unar -o /workspace/<YYYYMMDD>-<作業名> "<file>"` で展開、`lsar` で一覧 (ファイル名の文字コードを自動判定。zip / LZH を `unzip` / `lha` / `7z` で開くと Shift_JIS 名が化ける)。パスワード付きは `unar -p <pw>` (.7z は `7z x -p<pw>` も可。`-p` と値の間は空けない)、`.cab` は `cabextract`
- **Access (.mdb / .accdb)**: `mdb-tables -1 x.mdb` → `mdb-export -T '%Y-%m-%d %H:%M:%S' x.mdb <table>` (既定の日付は MM/DD/YY なので書式を指定する)
- **そのまま読める形式 (変換不要、`run_python`)**: Outlook `.msg` は `extract_msg` (本文と添付を取り出す。添付が旧 Office なら上の変換へ)、`.eml` は標準の `email`、`.dbf` は `dbfread`、SPSS `.sav` / SAS / Stata は `pyreadstat`。`.log` `.yaml` `.py` `.sql` などのテキストは `read_file` で読む (UTF-8 / cp932 は自動で判別。「テキストとして読めない」と返ったら `nkf -g` で文字コードを確かめ、`nkf -w` で UTF-8 にして `/workspace/<YYYYMMDD>-<作業名>/` に書いてから)
- **経路が無い形式** (一太郎 `.jtd`、`.mht` 等): 推測で読まない。PDF か docx で保存し直して添付してもらうよう伝える
- **OCR (束を一括で文字にする。1 枚を見るなら `feed_image`)**: `ocrmypdf -l jpn --force-ocr --jobs 4 in.pdf out.pdf` → `pdftotext out.pdf /workspace/<YYYYMMDD>-<作業名>/out.txt` → `read_file`。テキスト層があるなら `--skip-text`、縦書きは `-l jpn_vert`、英語は `eng`。**`--jobs 4` を必ず付ける** (既定は全コアを使い推論サーバと競合する)。大きな束は `qpdf --split-pages=20 in.pdf part-%d.pdf` で分けてから。画像 1 枚なら `tesseract img.png - -l jpn`、画像の束は `img2pdf *.png -o in.pdf` で PDF にしてから ocrmypdf
- **PDF の圧縮・修復**: `gs -q -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook -dNOPAUSE -dBATCH -sOutputFile=out.pdf in.pdf` (スキャン PDF を数分の 1 に)、壊れた・保護された PDF は `qpdf` (`--decrypt` / `--linearize`)
- **版比較**: 両方を pandoc で md 化 → `git diff --no-index --word-diff a.md b.md` か `wdiff a.md b.md`。差分の要約を答えに書き、差分そのものは長ければファイルで渡す
- **メタデータ**: `exiftool file` で作成者等を見る。剥がすのは作業場所で `exiftool -all= -overwrite_original file` (原本 `_original` を残さない)。PDF はその後 `qpdf --linearize in.pdf out.pdf` で書き直さないと復元できる形で残る。docx / xlsx / pptx は exiftool では書けないので `python-docx` / `openpyxl` / `python-pptx` の core_properties を空にして保存し直す。仕上がりだけを exports へ
- **フォント**: 既定は Noto CJK。古い Word の「ＭＳ 明朝 / ＭＳ ゴシック」は soffice で PDF 化するとき IPA 明朝 / ゴシックに自動で置き換わる (字幅が近い)
