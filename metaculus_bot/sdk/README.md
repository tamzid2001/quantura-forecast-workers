# Retained Metaculus models

This directory retains the MIT-licensed question parser, question models and CDF
implementation from `forecasting-tools` 0.3.4. `LICENSE` contains the upstream
license; `provenance.json` records the original source hashes and transformations.

The bot uses these models but does not use that package's LLM, search or network
clients. Installing the complete package also installed AskNews, whose dependency
range prevented the cryptography security update. Keeping only the existing pure
model implementation removes that dependency conflict without overriding package
constraints. Local CDF corrections are recorded in `provenance.json`.

The October 28 reliability patch handles repeated scaled quantile locations as
right-continuous CDF jumps instead of dividing by a zero-width interval. It also
rejects non-finite bounds and zero inbound mass with validation errors. The
upstream CDF grid, log/date transforms, probability-mass caps and closed-bound
normalization remain in use. Regression tests cover large numeric/date scales,
discrete grids, log scales, concentrated distributions and invalid scales.

Do not replace these files with simplified probability math. Compare question
parsing, dates, scaling, conditional/group questions and every CDF ordinate with
the recorded upstream release when updating the retained implementation. The
owning regression suite is `python -m pytest metaculus_bot/tests`.

This change does not enable submissions or comments. Existing bot pause and
permission controls continue to apply.
