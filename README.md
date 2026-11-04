# repo-janitor

A nightly GitHub Action that tidies the code in your repos without changing what it does, and opens pull requests under your name.

- **Small cleanups** (≤ 150 changed lines and ≤ 10 files by default) are merged automatically.
- **Big cleanups** are opened as PRs labelled `needs-review` and left for you. You also get an issue in this repo so GitHub notifies you.

It runs fully on GitHub Actions. No AI, no external services; just formatters plus checks that prove nothing changed.

## What it changes

| Step | Tool | Safety check (file is restored if it fails) |
|---|---|---|
| Remove unused imports | `ruff` (F401) | Only `from x import y` names are removed; bare `import x` is never touched (side effects). The rest of the file's syntax tree must be identical. Skipped if another file imports the name from this module, does `import *` from it, or the file uses `globals()`/`eval`/`sys.modules`. `__init__.py` is never touched. |
| Format Python | `black` (or `ruff format` if the repo is configured for ruff) | The syntax tree before and after must be identical (docstring indentation excepted). |
| Format JS/TS/CSS | `prettier` (uses the repo's own config if it has one) | Every `.js` file that parsed before must still parse after. |

It **never** touches: HTML, JSON, Markdown, notebooks, config/CI files, lockfiles, `node_modules/`, `dist/`, `build/`, `venv/`, `vendor/`, `migrations/`, minified or generated files, or anything over 500 KB. It never pushes to your default branch directly and never force-pushes.

## How a night works

For each repo in `config.yml`:

1. Skips it if an earlier janitor PR is still open, or if nothing changed since the last run.
2. On the first run it looks at the whole repo. After that, only files changed since the last run.
3. Runs the steps above, one commit per step, authored as you.
4. Pushes `janitor/<date>` and opens a PR. Small PRs are squash-merged right away; big ones get `needs-review`.
5. Saves progress in `state.json` and writes a summary table to the run page.

Expect your first run to produce several `needs-review` PRs, because it's cleaning whole repos for the first time. After that, PRs should be small and merge on their own.

## Setup (about 5 minutes)

1. **Create a token.** Go to GitHub → Settings → Developer settings → Fine-grained personal access tokens → *Generate new token*.
   - Resource owner: you. Expiration: up to 1 year.
   - Repository access: *Only select repositories*, and pick the repos listed in `config.yml`.
   - Permissions: **Contents** read & write, **Pull requests** read & write, **Issues** read & write (for labels), **Metadata** read.
2. **Add it to this repo.** Settings → Secrets and variables → Actions → *New repository secret*. Name it `JANITOR_TOKEN` and paste the token.
3. **Try a dry run.** Actions tab → *janitor* → *Run workflow*, with **Dry run** ticked (optionally fill *Only* with one repo name). Download the `janitor-patches` artifact from the run to see exactly what it would change.
4. Once you're happy with the patches, do nothing else. The schedule takes over at 02:41 IST every night.

## Everyday use

- **Change repos, limits or tools:** edit `config.yml` and commit.
- **Run now:** Actions → janitor → Run workflow (untick Dry run).
- **Clean one checkout on your machine:** `pip install -r requirements.txt && python -m janitor --local /path/to/repo` (commits to that checkout's current branch, no GitHub).
- **Stop it:** Actions → janitor → ⋯ → *Disable workflow*.
- **Redo a repo from scratch:** delete its line from `state.json`.

Note: GitHub pauses scheduled workflows in repos with no activity for 60 days, and emails you first. The nightly `state.json` commits normally keep it active.

## Tests

```
pip install -r requirements.txt
python -m pytest -q tests
```

The workflow runs these tests before every run, so a broken safety check stops the job before it touches any repo.
