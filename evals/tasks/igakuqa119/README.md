# igakuqa119

The 119th Japanese national medical licensing examination (第119回医師国家試験): 400 questions in blocks 119A-119F, 500-point scale (Essential blocks B/E Q26-50 are worth 3 points, all other questions 1 point). Data is the public IgakuQA119 dataset (OCR text, images and the answer key); `run.py` names the upstream repository.

- `run.py` mirrors the upstream solver's prompt (system prompt plus `answer:` / `confidence:` / `explanation:` lines) and extracts the answer from the `answer:` line, so results are directly comparable with the public leaderboard.
- At start-up it probes whether the model accepts images (a synthetic red square). If it does, image questions are sent multimodally; if not, or with `--no-vision`, all questions are answered from text alone (blind), as the leaderboard does for text-only models.
- Scoring: a question is correct when the set of chosen letters (or the numeric value) equals the key; score = points of correct questions / 500, accuracy = correct / 400. A No-Img bucket (297 questions / 383 points) is reported alongside.
