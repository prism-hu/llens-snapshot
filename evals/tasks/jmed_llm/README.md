# jmed_llm

Multiple-choice tasks from the public JMED-LLM benchmark of Japanese medical language understanding; `run.py` names the upstream repository. Three tasks are run by default: JMMLU-Med (medical MCQ), CRADE (4-level adverse-drug-event likelihood from case reports) and RRTNM (TNM classification from radiology reports). SMDIS and JCSTS are implemented but excluded from `--task all` because of their size; the NER subtasks are not implemented.

- `run.py` presents each CSV row as a question with lettered options and extracts a single uppercase letter from the reply.
- Scoring follows the official leaderboard: Cohen's κ with accuracy in parentheses, `κ(accuracy)`. CRADE (and JCSTS) use linearly weighted κ over the ordinal labels; the others use unweighted κ. A reply that matches no option counts as a distinct miss.
