"""Measuring whether a model driving the operation catalog gets the part right.

`decompiler.corpus_report` measures the *data* side — can a tree be recovered as
an op sequence and replayed. Nothing measured the *agent*: whether a model asked
for a bracket produces a bracket. Every change to the prompt, the schema or the
loop is a guess until it does, and the two most tempting changes — a bigger
prompt, a longer step budget — are exactly the ones that can make things worse
while feeling better.

See `docs/LLM-ACCURACY-ROADMAP.md`.
"""

from evals.agent import Task, TaskResult, run_suite, run_task  # noqa: F401
