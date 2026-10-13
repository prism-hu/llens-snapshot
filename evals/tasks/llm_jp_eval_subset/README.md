# llm_jp_eval_subset

Four general-purpose Japanese tasks from the public llm-jp-eval suite: JCommonsenseQA (5-choice commonsense MCQ), JEMHopQA (multi-hop QA, free text), JSQuAD (extractive QA) and MGSM-ja (math word problems, numeric answer). The data is the test split produced by llm-jp-eval's own preprocessing script.

- `run.py` reads the preprocessed task JSON, appends a short answer-format hint to each instruction (option number only / `<answer></answer>` tags / `\boxed{}`), queries the OpenAI-compatible endpoint and extracts the answer with the matching regex.
- Scoring uses the metric named in each dataset file: `exact_match` (JCommonsenseQA), `char_f1` (JEMHopQA, JSQuAD) and `mathematical_equivalence` (MGSM-ja), averaged over the samples. Timing and reasoning-token statistics are recorded per sample.
