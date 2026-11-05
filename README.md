# repo-janitor

A nightly GitHub Action that tidies the code in your repos without changing what it does, and opens pull requests under your name.

- **Small cleanups** (≤ 150 changed lines and ≤ 10 files) are merged automatically.
- **Big cleanups** are opened as PRs labelled `needs-review` and left for you.
- **Every night** the summary is posted as a comment on the *Janitor nightly reports* issue in this repo, so GitHub notifies you.

It runs entirely on GitHub Actions. No AI and no outside services: just formatters, your own tests, and checks that prove nothing changed.

## What it changes

| Step | Tool | Safety check (the file is put back if it fails) |
|---|---|---|
| Remove unused imports | `ruff` (F401) | Only `from x import y` names are removed. A bare `import x` is never touched, because importing a module can have side effects. The rest of the file's syntax tree must be identical. Skipped if another file imports the name from this module or does `import *` from it, or if the file uses `globals()`/`eval`/`sys.modules`. `__init__.py` files are never touched. |
| Format Python | `black` (or `ruff format` if the repo is set up for ruff) | The syntax tree must be identical before and after (docstring indentation aside). |
| Format JS/TS/CSS | `prettier` (follows the repo's own config if it has one) | Every `.js` file that parsed before must still parse after. |

It **never** touches HTML, JSON, Markdown, notebooks, config or CI files, lockfiles, `node_modules/`, `dist/`, `build/`, `venv/`, `vendor/`, `migrations/`, minified or generated files, or anything over 500 KB. It never pushes straight to your default branch and never force-pushes.

## Your tests

If a repo has tests (pytest, or an `npm test` script), the janitor installs the repo's dependencies on the runner and runs the tests **before and after** cleaning:

| Situation | What happens |
|---|---|
| Tests pass before and after | Normal small/big rules apply |
| Tests were **already failing** before cleaning | Repo is skipped and flagged 🔴 *blocked* in the nightly report. It's retried every night until the tests pass, and nothing is lost. |
| Tests fail **after** cleaning | Nothing is pushed; flagged 🔴 *failed* |
| Dependencies won't install, or tests time out (15 min) | Cleaned, but always as a `needs-review` PR (never auto-merged) |
| No tests | Relies on the syntax-tree checks above |

## How a night works

For each repo in `config.yml`:

1. Skips it if an earlier janitor PR is still open, or if nothing changed since the last run.
2. **The first time** it sees a repo, it only records where things stand. From then on, it cleans **only the files you change**. Old, untouched files are left alone.
3. Runs the cleanup steps (one commit per step, authored as you), then your tests.
4. Pushes `janitor/<date-time>` and opens a PR: auto-merged if small, labelled `needs-review` if big.
5. Saves progress in `state.json` and posts the night's summary.

## Setup (about 5 minutes)

1. **Create the repo.** Make a **private** repo called `repo-janitor` and push these files to it.
2. **Create a token.** GitHub → Settings → Developer settings → Fine-grained personal access tokens → *Generate new token*.
   - Resource owner: you. Expiration: up to 1 year (put a reminder in your calendar to renew it).
   - Repository access: *Only select repositories*. Pick the repos listed in `config.yml`.
   - Permissions: **Contents** read & write, **Pull requests** read & write, **Issues** read & write (for labels), **Metadata** read.
3. **Add it as a secret.** In `repo-janitor`: Settings → Secrets and variables → Actions → *New repository secret*. Name it `JANITOR_TOKEN`.
4. **Allow the workflow to write.** In `repo-janitor`: Settings → Actions → General → Workflow permissions → *Read and write permissions*.
5. **Turn on email for the reports (optional).** GitHub → Settings → Notifications → make sure *Watching* has Email ticked. You watch your own repos by default.
6. **Dry run.** Go to the Actions tab → *janitor* → *Run workflow* with **Dry run** ticked. Because nothing is recorded yet, a dry run previews the files changed in each repo's last 10 commits. Download the `janitor-patches` artifact to see exactly what it would change. Nothing is pushed.
7. That's it. From 02:41 IST tonight, the first real run records where every repo stands, and cleaning starts with your next changes.

Private repos on a free account get 2,000 Actions minutes a month. A night where nothing changed takes about 2 minutes. A night with test installs takes longer, but it normally stays well within the limit.

## Everyday use

- **Change repos, limits or tools:** edit `config.yml` and commit.
- **Run now:** Actions → janitor → Run workflow (untick Dry run).
- **Clean a checkout on your machine:** `pip install -r requirements.txt && python -m janitor --local /path/to/repo`. This commits to that checkout's current branch and doesn't touch GitHub.
- **Stop it:** Actions → janitor → ⋯ → *Disable workflow*.
- **Forget a repo's history:** delete its line from `state.json`. The next run starts watching it again from that point.

GitHub pauses scheduled workflows in repos with no commits for 60 days, and emails you first. The `state.json` commits it makes whenever your repos change normally keep it active.

## Tests

```
pip install -r requirements.txt
python -m pytest -q tests
```

The workflow runs these before every night's run, so a broken safety check stops the job before it touches any repo.
