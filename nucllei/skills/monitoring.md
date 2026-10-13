---
title: Monitoring
description: "推論基盤の監視 (管理者用)。『重い』『遅い』『GPU の空きは』『負荷はどう』『今の状況は』『さっきから詰まっている』『監視』『メトリクス』『温度』『VRAM』『待ち行列』のように、GPU / 推論エンジン / サンドボックスの現在値や推移を問われたら、記憶や体感で答えず本スキルをロードして monitoring_now / monitoring_export で実測値を引くこと。"
tool_ids: monitoring
# access_control: sync 直後は public。管理画面で tool `monitoring` と本 skill の両方を admin グループ (read) に落とす
---

# 監視 (GPU / 推論エンジン / サンドボックス)

数値は監視データベースの実測。答えは **数値 → 一行の読み → ダッシュボードのリンク (戻り値にあれば)** の順。
データに無いことは推測しない。接続先アドレスや内部ホスト名は答えに出さない。

## 使い分け

- **「今どう?」** → `monitoring_now(group="sglang")` と `monitoring_now(group="gpu")` の 2 本。
  サンドボックスを問われたら `group="sandbox"`。個別なら `names=["gpu_temp", "sglang_queue"]`。
- **「さっきから」「最近」「一時的か続いているか」「ピークは」「相関は」** → `monitoring_export`
  (`save_to` は必須で、作業場所の CSV のパス `/workspace/<YYYYMMDD>-<作業名>/<名前>.csv`。`group` か `names`、
  `since` は "15m" / "1h" / "6h" / "24h"、最大 30d)。戻り値の `path:` の CSV を `run_python` の pandas で読み、集計した数値だけを答える。行数が上限を超えると ERROR が返るので、ERROR の「次の手:」どおり
  `since` を短くするか `step` を粗くする。
- CSV は縦持ち `ts,time,name,labels,value` (`labels` は `gpu=0` の形)。
  `df.pivot_table(index="time", columns=["name","labels"], values="value")` で横持ちにしてから
  `describe()` / `resample` / `idxmax()`。生の行や CSV の中身は答えに貼らない。

## 指標名 (name) — group と単位

| group | name | 単位 |
|---|---|---|
| gpu (GPU ごとに 1 系列、ラベル `gpu`) | `gpu_util` / `gpu_temp` / `gpu_vram_used` / `gpu_power` | % / °C / GiB / W |
| sglang | `sglang_running` / `sglang_queue` | 件 |
| sglang | `sglang_gen_throughput` / `sglang_prompt_tok_rate` / `sglang_gen_tok_rate` | tok/s |
| sglang | `sglang_ttft_p50` / `sglang_ttft_p95` / `sglang_ttft_p99` / `sglang_e2e_p50` / `sglang_e2e_p95` | s |
| sglang | `sglang_kv_used_tokens` / `sglang_cache_hit_ratio` | tokens / % |
| sandbox | `sandbox_loop_used` / `sandbox_loop_nodes_total` / `sandbox_workspaces` / `sandbox_sessions_active` | 件 |
| sandbox | `sandbox_disk_used` / `sandbox_disk_total` | GiB |

## 判断基準

| 指標 | 良好 | 要注意 | 読み |
|---|---|---|---|
| `sglang_ttft_p95` | < 1 s | > 3 s | 待ち時間の体感。キューと一緒に見る |
| `sglang_gen_throughput` | > 30 tok/s | < 15 tok/s | 生成が遅い体感 |
| `sglang_queue` | 0〜数件 | 増え続ける | 同時利用の飽和。`sglang_running` と並べる |
| `gpu_temp` | < 75 °C | > 83 °C | スロットリング域。冷却 / 負荷の問題 |
| `gpu_vram_used` | 余裕あり | 141 GiB の 90 % 超 | 1 基 141 GiB。KV キャッシュの逼迫 |
| `sandbox_disk_used` | — | `sandbox_disk_total` の 90 % 超 | ワークスペースの掃除が要る |

「重い」と言われたら、まず `sglang_queue` と `sglang_ttft_p95` (待ち) と `sglang_running` (同時数) を
見て、次に `gpu_util` / `gpu_temp` (ハード) を見る。全 GPU が同じ挙動なら 1 モデルの負荷、
特定 GPU だけ違えばハード側を疑う。値が `—` の指標はその時点の観測が無い。

## 答え方

1. 取得時刻と主要な数値 (表をそのまま、または集計値 2〜4 個)
2. 判断基準に照らした一行の読み (「p95 2.1 s で要注意域の手前、キューは 0」)
3. `monitoring_now` の戻り値に `ダッシュボード:` の行があれば、そのリンクをラベルごとそのまま写して添える (期間を見たい人向け。無ければ書かない・組み立てない)
