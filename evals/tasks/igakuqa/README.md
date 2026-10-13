# igakuqa

Five years (2018-2022) of the Japanese national medical licensing examination (医師国家試験): 400 questions per year, 2,000 questions and 2,485 points in total. Data is the public IgakuQA dataset (question text and per-question `points`; images are not distributed); `run.py` names the upstream repository.

- `run.py` asks the model to return the chosen letters inside `<answer></answer>` tags and extracts them from there. Image questions are always answered from text alone (blind), which is the same scope as the published comparisons on this dataset.
- Scoring: a question is correct when the set of chosen letters equals the key; score = sum of each correct question's `points` / 2,485, accuracy = correct / 2,000. Results are reported for the 5-year total and per year, plus a No-Img bucket.
