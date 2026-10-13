# jmle2026

The 120th Japanese national medical licensing examination (第120回医師国家試験): 400 questions in blocks A-F, 500-point scale (Essential blocks B/E Q26-50 are worth 3 points, all other questions 1 point). Data is the public JMLE2026-Bench dataset (question text and images); `run.py` names the upstream repository.

- `run.py` uses the upstream system prompts verbatim and extracts the answer from the `【回答】` line, so results are directly comparable with the public leaderboard. Serial questions share a common context that is prepended to the prompt.
- At start-up it probes whether the model accepts images (a synthetic red square). If it does, image questions are sent multimodally; if not, or with `--no-vision`, all questions are answered from text alone (blind), which is the leaderboard's default `image_mode`.
- Scoring: a question is correct when the set of chosen letters (or the numeric value) equals the key; score = points of correct questions / 500, accuracy = correct / 400. The result JSON also carries a Text-only bucket and a `submission` object in the leaderboard's format.
