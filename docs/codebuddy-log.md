# CodeBuddy / WorkBuddy development log

The handbook requires at least 3 screenshots or a screen recording of CodeBuddy
or WorkBuddy chat logs from development (p.37). Each session is logged here;
screenshots are in `docs/codebuddy/`.

| Date | Tool | Task | Outcome | Screenshots |
|---|---|---|---|---|
| 2026-09-29 | WorkBuddy (Coding) | Write the demo UI stylesheet `app.css` from the full `index.html` and a design brief | Ran 20 min without producing output; cancelled | `…-01-workbuddy-ui-css-prompt.jpg`, `…-02-workbuddy-writing-app-css.jpg` |
| 2026-09-29 | WorkBuddy (Coding) + GitHub connector | Clone the private repo, write `app.css`, open a pull request | Blocked: GitHub token rejected (401). WorkBuddy diagnosed the expired authorisation and stopped cleanly | `…-03-workbuddy-repo-task-ui-css.jpg`, `…-04-workbuddy-github-auth-401.jpg`, `…-05-workbuddy-reauth-plan.jpg` |
| 2026-09-29 | WorkBuddy (Coding) | Write `app.css` from a compact DOM map and design brief, print the file | Stalled again at the file-write step after ~15 min of design reasoning; cancelled. The stylesheet was then written by Claude Code | `…-06-workbuddy-standalone-css-task.jpg`, `…-07-workbuddy-css-stalled-again.jpg` |
| 2026-09-29 | WorkBuddy (Coding) | Small task: `parse_zh_amount()` for Mandarin money amounts (两万五 = 25000) with pytest cases, printed in the reply | Reasoned for over an hour without delivering code; cancelled | `…-08-workbuddy-zh-amounts-prompt.jpg` |
| 2026-09-29 | WorkBuddy (Coding) | Write 20 **held-out** benchmark cases from a spec of the bank data and outcome rules only (never saw our prompts or cases) | **Delivered** in 12 min 20 s; saved verbatim as `evals/heldout.yaml` and run once, untuned | `…-09-workbuddy-heldout-cases-prompt.jpg`, `…-10-workbuddy-heldout-cases-delivered.jpg` |

Lesson: WorkBuddy handles small, self-contained tasks well; large single-file generation tasks stalled. Complex, tightly-coupled work is done in Claude Code.

## Components owned by CodeBuddy / WorkBuddy

- [x] Held-out benchmark cases `evals/heldout.yaml` (independent test set)
- [ ] Mandarin amount forms in the number grammar ("两万五"): `parse_zh_amount`
- [ ] More benchmark cases in `evals/cases.yaml`
- [ ] UI polish passes (animations, mobile layout)
