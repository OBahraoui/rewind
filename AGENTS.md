# Instructions for AI agents working in this repository

This project ships changes to production with no human in the loop. **Rewind** checkpoints the
project before you act, checks your merge request for anything it couldn't undo, and undoes your
whole change set if production regresses. You are free to do anything Rewind can undo.

Rewind's gate and production monitoring check more than this file lists. If your merge request is
rerouted or blocked, the Rewind ledger on the issue explains why and what to change.

## How to ship here
- Ship user-visible behaviour behind a GitLab feature flag, so a release can be switched off.
  Pricing changes go in `new_pricing_total()` in `app/pricing.py`, which runs only when the flag
  `new_pricing` is on; turn the flag on per environment in `config/flags.yaml` (the pipeline applies it).
- Prices are integer cents.
- Add or update tests in `app/tests` for every behaviour change. Run them if Python is available
  (`python3 -m pytest app/tests`); the merge request pipeline always runs them.

## Out of bounds
- Don't edit `rewind/`, `service/`, `flows/`, `agents/`, `ci/`, `infra/`, `.gitlab/`, `.gitlab-ci.yml`,
  `AGENTS.md` or `CLAUDE.md`: agents don't change their own guardrails or the delivery pipeline.
- Don't push to the default branch directly. Changes reach `main` only through a merge request.

## Merge requests
- One merge request per issue, targeting the default branch, with `Closes #<iid>`.
- When asked to rework a merge request, push to its existing branch.
