# Demo video script (2:50)

One memorable moment: **production breaks, and the agent's entire change set disappears in seconds.**
Record real runs only. Speed up waiting time; never fake a result. Narrate (AI voice is fine).
No third-party trademarks or music.

**Before recording:** `new_pricing` is off in production, the Rewind dashboard is open, the
GitLab issue list is open, and a terminal is ready with `rewind` installed.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:20 | GitLab project, agent-authored MRs | "Agents can now ship to production on GitLab. The question every team asks: what happens when one gets it wrong at 2 a.m.? Today there's no undo for what an agent did." |
| 0:20–0:35 | Title card | "**Rewind: let agents do anything they can undo. Stop them only for what they can't.**" |
| 0:35–1:20 | Label issue "Launch promo: $15 off every order" `rewind:auto`. Hands off the keyboard. Sped-up: checkpoint note, plan comment with reversibility table, MR, gate ✅ (behind the flag), tests + SAST + container scan, merge, staging, production, flag on. | "I label an issue and walk away. Rewind checkpoints the project, Duo plans and builds it, and Rewind's gate confirms it ships behind a feature flag, so it stays undoable. It passes tests, security scans and staging, and goes live." |
| 1:20–2:05 | `monitor:production` fails: "negative total -1100". Ledger updates. **Timer on screen.** Cloud Run revisions page: traffic back on the old revision; flag off; `main` restored; issue reopened. | "Production monitoring catches it: small orders now cost less than nothing. Rewind undoes the agent's whole change set: traffic back to the previous revision in seconds, the flag off, the merge reverted, the issue reopened. Then it verifies zero drift against the checkpoint." |
| 2:05–2:25 | Second issue "Email a receipt": gate ⛔ with the exact line `httpx.post(...)` | "Some things can't be undone. An email that's sent stays sent. Rewind doesn't pretend: it blocks the merge and explains why." |
| 2:25–2:40 | Dashboard: runs, change set grouped by class, rewind report. Terminal: `rewind to <cp> --dry-run`, then `rewind undo-last`. | "Every change, its evidence and its undo, in one ledger. And a rewind can itself be rewound." |
| 2:40–2:50 | Nine-stage strip all lit; Cloud Run | "Nine lifecycle stages, deployed on Google Cloud. More autonomy, same risk." |
