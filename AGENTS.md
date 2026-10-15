# Instructions for AI agents working in this repository

This project ships changes to production with no human in the loop. **Rewind** checkpoints the
project before you act, gates your merge request by reversibility, and undoes your whole
change set if production regresses. You are free to do anything Rewind can undo; you are
stopped only for what it can't.

## Ship reversibly
- User-visible behaviour goes behind the GitLab feature flag `new_pricing`.
  - `app/pricing.py`: never change `legacy_total()`, the live default path.
  - Put new pricing logic in `new_pricing_total()`; it runs only when the flag is on.
  - Turn the flag on per environment in `config/flags.yaml`; the pipeline applies it.
- Prices are integer cents. No order may be discounted by more than 50%: production
  monitoring checks this on every response and triggers a rewind if it breaks.
- Add or update tests in `app/tests` for every behaviour change:
  `pip install -r app/requirements-dev.txt && pytest app/tests`.

## Never (the merge request will be blocked)
- Edit `rewind/`, `service/`, `flows/`, `agents/`, `ci/`, `infra/`, `.gitlab/`, `.gitlab-ci.yml`,
  `AGENTS.md` or `CLAUDE.md`. Agents don't change their own guardrails or the pipeline.
- Add code that calls external services or sends messages (email, SMS, webhooks).
- Add a migration in `app/migrations/` without a `downgrade()`.

## Merge requests
- One merge request per issue, targeting the default branch, with `Closes #<iid>`.
- When asked to rework a merge request, push to its existing branch.
