# evalkit — a personal eval for comparing LLMs

A CLI that runs **your own tasks** against models from several vendors, grades the answers, and produces a decision-ready report.
Works without API keys: through official CLIs on your subscriptions, or by copy/paste.

## Quick start

Requirements: Python 3.10+, and Go 1.22+ for the Go tasks (without Go they're skipped, not scored 0).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest                    # harness tests + every task's reference passes its own graders

# 1. Zero-cost dry run — validates the whole pipeline (expected: good > sloppy > broken)
python -m evalkit run --config config.mock.yaml --run mock
open results/mock/report.html

# 2. Check config + tasks without calling any model
python -m evalkit validate

# 3. Preflight: one trivial call per model AND judge, retries off, real error on failure
python -m evalkit doctor

# 4. Small run first, then everything
python -m evalkit run --run smoke --tasks gen_capacity_estimate
python -m evalkit run --run v1
```

### Models × reasoning effort
One config entry can expand into several competitors:
```yaml
- name: gpt-sol
  command: [codex, exec, ..., -m, gpt-6.2-sol, -c, 'model_reasoning_effort="{effort}"', "{prompt}"]
  variants: {effort: [low, medium, high]}     # -> gpt-sol-low, gpt-sol-medium, gpt-sol-high
  timeout_s: 900                              # high effort thinks longer than run.timeout_s
```
- **Use a funnel, not a grid.** Every model × every effort is ~75 competitors × 66 calls. Stage 1: each
  current-generation model at one effort (the default `config.yaml`). Stage 2: sweep effort on the top 2-3.
- **Always pin effort.** In an isolated home, unpinned codex runs with reasoning effort `none`.
- **Effort names aren't comparable across vendors.** Compare models at similar latency/cost, and use
  effort sweeps to ask "does `high` buy enough score for its extra latency *on this model*?"
- **Validated at load time,** because the CLIs don't: `claude --effort bogus` silently uses the default, and
  codex accepts efforts a model doesn't support (checked against `~/.codex/models_cache.json`).

### Will my subscription quota last? (Plan run)
Subscriptions limit **messages per time window**, not tokens, and no CLI reports what's left. So the
planner (`serve` → **Plan run**, or `run --dry-run`) shows the exact **calls per subscription** for your
selection, with tokens estimated from earlier runs. Pick models × efforts with the dropdowns ("All" per
model, or "All efforts, all models"), categories and repetitions; it gives you the `run` command.
- **Judging is usually the biggest cost.** Every rubric-graded answer goes to every judge and is charged
  to the *judge's* subscription. A cheap first pass: `--repetitions 1`, or comment out one judge.
- **Running out waits, by itself.** When a subscription hits its usage window (e.g. 5 hours), its calls
  pause until the reset - the time is read from the error ("try again at 9:15 PM"), else re-checked
  every 15 min - and continue automatically; other subscriptions keep running meanwhile. One probe call
  tests the reset, not all of them. Answers are never graded by half the judge panel. After
  `quota_max_wait_h` (8 h) it stops waiting; runs resume, so running again fills in the rest.

### Web UI (local, any OS)
```bash
cd web && npm ci && npm run build && cd ..   # once, and after UI changes (needs Node 20+)
python -m evalkit serve                     # open http://localhost:8765
```
Screens:
- **Models**: every competitor and judge, the model id and effort that actually run, one-click health checks.
- **Plan run**: pick an effort per model (models left on "—" don't run), see calls and cost per subscription,
  then **Run**. The run belongs to the server: you can close the tab and come back to its progress. One run
  at a time (they'd compete for the same quotas). Cancel keeps the answers so far; starting the same run
  again resumes it. If the server stops, the run stops too - start it again to resume.
- **Results**: every model with *fit for you*, *general quality* and *reliability* (%), sortable, with
  margins; head-to-head underneath. "View results" after a run opens it here. UI development: run `serve`, then `npm run dev` in `web/`
(hot reload, proxies `/api` to the server).

**Why a local server, not a hosted site:** models run through CLIs logged in on *your* machine;
a remote server has neither your logins nor your machine. The server binds `127.0.0.1`, rejects
foreign `Host` headers (DNS rebinding) and non-JSON writes (CSRF), because a request to it spends
your subscription quota. `--host 0.0.0.0` exposes it to your network: there is no authentication.

### Gemini (Antigravity CLI, on a Google AI Pro subscription)
Gemini CLI stopped serving personal AI Pro/Ultra accounts on 2026-06-18; its replacement is `agy`:
```bash
# install: https://antigravity.google/docs/install   (read the install script before piping it to bash)
agy                      # once, interactively: sign in with "Google OAuth"; login is cached in the OS keyring
agy models               # the slugs on your plan; effort is part of the slug (gemini-4-neon-high)
python -m evalkit doctor --isolation --models gemini-neon-medium
```
`agy` has no documented flag to skip user rules or disable tools, so `doctor --isolation` asks each model
whether it sees personal instruction files. It caught the CLAUDE.md and AGENTS.md leaks in claude and codex.

### Manual models (e.g. Mistral Le Chat, GLM)
```bash
python -m evalkit export            # writes manual/<model>/<task>.prompt.md
# paste each prompt into the chat UI, save the reply to <task>.answer.md in the same folder
python -m evalkit run --run v1      # saved answers are picked up and graded; missing ones are skipped
```
Tip: open a fresh conversation per prompt so earlier context doesn't leak into the answer.

### Useful commands
| Command | What it does |
|---|---|
| `run --dry-run` | calls per subscription (answers + judging), calls nothing |
| `run --models gpt-sol,claude-opus-high` | a family name = all its efforts; `all` = everything; default = `defaults` only |
| `run --repetitions 1` | cheap first pass (manual is always 1) |
| `run --category backend_code` | only one category |
| `grade --run v1 --regrade` | re-grade without regenerating answers (after changing a rubric) |
| `report --run v1` | rebuild the report |
| `doctor --models claude-opus` | preflight a subset; exit code 1 if anything fails |
| `doctor --isolation` | also ask each model if it sees personal instruction files (1 extra call each) |

## Architecture

```
config.yaml          who competes, how they're called, and how much each category matters to you
tasks/*.yaml         what we test: prompt, reference, graders (twins_he.yaml = Hebrew twins of English tasks)
tests/               harness unit tests + golden test over all tasks
evalkit/
  doctor.py          preflight: one ping per model/judge, flags single-vendor judge panels
  api/               FastAPI for the web UI: routes (HTTP) -> services (logic) -> core; schemas = API contract
web/                 React + TS + Vite UI: services/api.ts -> hooks/ (state) -> components/ (rendering)
  providers.py       one adapter per vendor behind a single interface: complete(Request) -> Completion
                     anthropic | openai_compat | cli | manual | mock
  graders.py         python_tests, go_tests, node_tests (execute code) | answer_tests (pytest over the raw answer:
                     structured output, format rules) | match (regex) | length, language (constraints) | rubric (LLM-as-judge)
  limits.py          concurrency caps: global, per subscription (what vendors rate-limit), per model
  procs.py           subprocesses with timeouts that kill the whole process tree (agentic CLIs spawn children)
  runner.py          generate -> generations.jsonl, grade -> grades.jsonl  (two stages, resumable)
  report.py          weighted score, bootstrap CI, latency, cost, per-answer drill-down
```

**Design decisions and why**
- **Narrow Provider interface.** Switching from CLI to API is a one-line config change, not a refactor. A new vendor is one class.
- **Generate and Grade are separate stages.** Once you see real answers you'll want to change rubrics; raw outputs are stored, so re-grading is free.
- **Idempotent and resumable.** Every result is keyed by `model|task|fingerprint|rep`. A crashed run resumes where it stopped. Editing a task changes its fingerprint, so stale results are never silently counted.
- **Constraints multiply, they aren't averaged.** An empty answer "passes" a length limit; averaging would hand it free points (caught during the mock run).
- **Prompt language = answer language.** Tasks are written in the language you'd actually use (Hebrew tasks have Hebrew prompts). The `language` constraint counts *words*, not letters — Hebrew has no written vowels, so letter counts under-weight it.
- **Overall = mean of category means, not of tasks.** Otherwise the category you wrote the most tasks for silently dominates.
- **EN/HE twins inherit graders (`twin_of`).** One source of truth: fix a test once and both languages get it, so the only variable in the comparison is the language.
- **Golden test over every task.** A broken test or regex punishes all models equally and never shows on the leaderboard. CI fails if any reference answer doesn't pass its own deterministic graders.
- **Wrong answers are tested too (`wrong_answers`).** The golden test proves a reference passes; it can't prove the
  tests can *fail*. Each `wrong_answers` entry is a plausible mistake that the deterministic graders must reject.
- **Model-written code never sees your environment.** Graders run it with a minimal env (PATH, locale, HOME = a temp
  dir): API keys and tokens in your shell are invisible to it, so it can't print them into an answer.
- **Go concurrency tasks run with `-race`.** A detected data race scores 0 even if the assertions happened to pass. Failures are labelled (build failed / deadlock / panic / race), because each points to a different fix.

## Reading the report
- **CI 90%** — if two models' intervals overlap heavily, the eval can't separate them yet. Add tasks; don't crown a winner.
- **HE vs EN** — mean score change on the *same task* in Hebrew vs English. Negative = the model gets worse when you work in Hebrew. With only a few pairs this is noisy: treat it as a signal to investigate, and add pairs before trusting it.
- **Inconsistency** — std-dev across repetitions. A slightly lower but more predictable model may be the better choice.
- **Task signal** — tasks every model aces (all ≥ 0.95, *too easy*) add nothing to the decision; replace them.
  Tasks nobody passes (all ≤ 0.05) are more likely broken than hard: check the reference. Also printed after `run`.
- **Judge spread** (in the drill-down) — large disagreement between judges usually means a vague rubric, not a borderline answer.
- **Always read the answers yourself.** The score is a filter, not the truth.

## Limitations to be aware of
1. **A CLI is not a clean model.** Claude Code, Codex and Gemini CLI add their own system prompts. By default they also load *your* setup — Claude Code reads `~/.claude/CLAUDE.md` (a mentoring-style CLAUDE.md visibly changes answers, e.g. appending "something to think about…" questions), and Codex always loads `~/.codex/AGENTS.md` plus `config.toml` (model, sandbox) — no flag skips AGENTS.md. `config.yaml` strips this: `--safe-mode --tools ""` for Claude; for Codex an `isolated_home` (a clean `CODEX_HOME` containing only a symlink to your login) plus `-s read-only` and pins model ids so a CLI update doesn't silently swap the model. A residual vendor system prompt remains; that is "the tool as you use it".
2. **Self-preference bias.** A judge tends to prefer answers from its own model family, hence a two-vendor judge panel. `validate` warns when the same model both competes and judges.
3. **Model-written code runs on your machine** — inside a subprocess with a timeout. That protects against accidents, not malice. For unfamiliar models, run inside Docker.
4. **Subscription rate limits.** At most `run.subscription_concurrency` (default 2) calls per subscription at a time,
   `run.concurrency` overall. If you hit a limit, wait and re-run; resume continues where it stopped.
5. **Contamination.** Tasks that exist publicly may have been seen in training. Tasks from your own work avoid this.

## The most important step: replace my tasks with yours
The 26 tasks here (7 backend code incl. 2 Go and 1 SQL, 5 debugging incl. a security fix, 4 system design,
6 general incl. strict JSON output, 1 TypeScript, 3 Hebrew writing; 4 have Hebrew twins) demonstrate each grader
type. `tasks/edge_cases.yaml` shows the pattern to copy: a task built around the edge cases weak models get wrong. A trustworthy eval needs **15–30 tasks from your real work**:
- a real bug you fixed this month, plus the test that catches it;
- a design question you agonized over, plus the trade-offs that decided it;
- an email or document you actually sent, in the language you actually write in.

**Rule:** a good task is one a weak model fails. If every model scores 1.0, the task adds nothing to the decision.

Workflow for a new task: write it with 1-2 `wrong_answers`, run `python -m pytest` (the golden test proves the
reference passes AND the wrong answers fail), run it against models, then check the report's **Task signal**.

### New task template
```yaml
- id: my_task
  category: backend_code          # or a new category (then give it a weight in config)
  prompt: |
    ...
  reference: |                    # shows the judge what "good" looks like; must pass the deterministic graders
    ...
  wrong_answers:                  # plausible mistakes; each must FAIL the deterministic graders
    - |
      ...
  graders:
    - type: python_tests          # or go_tests / node_tests / answer_tests / match / length / language / rubric
      weight: 2
      tests: |
        from solution import ...
    - type: rubric
      criteria:
        - id: correctness
          weight: 2
          description: an observable behaviour ("states what it would measure first"), not a vibe ("insightful")

# Hebrew twin of an existing task: inherits category + graders
- id: my_task_he
  twin_of: my_task
  prompt: |
    ...
  reference: |
    ...
  extra_graders:
    - type: language
      expected: he
```
