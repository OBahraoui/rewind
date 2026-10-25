# Demo video script (2:50)

One memorable moment: **production breaks, and the agent's entire change set disappears in seconds.**
Record real runs only. Speed up waiting time; never fake a result. Narrate (AI voice is fine).
No third-party trademarks or music.

**Before recording:** `new_pricing` is off in production. Open four tabs: the production storefront
(the demo app's production URL), the Rewind dashboard, the GitLab issue list, and the Cloud Run
revisions page. Keep a terminal ready with `rewind` installed.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:20 | GitLab project, agent-authored MRs | "Agents can now ship to production on GitLab. The question every team asks: what happens when one gets it wrong at 2 a.m.? Today there's no undo for what an agent did." |
| 0:20–0:35 | Title card | "**Rewind: let agents do anything they can undo. Stop them only for what they can't.**" |
| 0:35–1:15 | Label issue "Weekend flash sale: $15 off every order" `rewind:auto`. Hands off the keyboard. Sped-up: checkpoint note, plan comment, MR, gate ✅ (behind the flag), tests + SAST + container scan, Duo code review, merge, staging smoke test passes, production, flag on. | "I label an issue and walk away. Rewind checkpoints the project, Duo plans, builds and reviews it, and Rewind's gate confirms it ships behind a feature flag, so it stays undoable. Finance signed off on the discount, so it goes live." |
| 1:15–1:55 | Storefront: a $4 tea is charged **$0.00** in red. `monitor:production` fails. An incident opens. **Timer on screen.** The storefront flips back to $4.00 and shows "Prices recovered after …"; the run page's track shows Release ✕ and the arc back to the checkpoint; Cloud Run traffic is on the old revision; flag off; `main` restored; issue reopened; the incident closes itself with "zero drift". | "Staging only priced one basket. Production monitoring prices them all, and it enforces the margin guardrail the ticket waved away. Rewind opens an incident and undoes the whole change set: traffic back in seconds, the flag off, the merge reverted. It verifies zero drift and closes its own incident." |
| 1:55–2:10 | The same run, earlier: Duo code review **requested changes** (free orders break the guardrail); the agent couldn't settle a business question; Rewind's note on the MR: everything is undoable, so by policy it ships and production is watched. | "The reviewer was right. It didn't matter: everything this change shipped could be undone, so Rewind let it through, watched production, and took it back in seconds. Anything irreversible would have stopped." |
| 2:10–2:25 | Third issue "Email a receipt": gate ⛔ with the exact line `import httpx` | "Some things can't be undone. An email that's sent stays sent. Rewind doesn't pretend: it blocks the merge and explains why." |
| 2:25–2:40 | Ledger: each change linked to the agent session that made it. On the shipped issue, comment `/rewind`; Rewind replies with the result. | "Every change, the agent session that made it, and its undo, in one ledger. Any teammate can undo a run with one comment, but only an authenticated human can accept what can't be undone." |
| 2:40–2:50 | Nine-stage strip all lit; Cloud Run | "Nine lifecycle stages, deployed on Google Cloud. More autonomy, same risk." |
