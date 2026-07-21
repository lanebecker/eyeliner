# Vinyl Now Playing

<!-- SHARED-FACTS:START -->
<!-- AUTO-GENERATED shared facts. Do NOT edit between the SHARED-FACTS markers, and do NOT delete the markers themselves. This block is synced into every Projects/*/CLAUDE.md by scripts/sync-shared-facts.py from the master ~/Documents/Claude/shared/SHARED-MEMORY.md, and are re-run by a scheduled task. To change these facts: edit shared/SHARED-MEMORY.md and rerun the script. Manual edits or deletions here are overwritten/restored on the next sync. -->
## Gmail accounts (two Gmail MCPs connected)
- **Personal** — `lanebecker@gmail.com` → the `mcp__gmail__*` tools (self-hosted ArtyMcLabin Gmail-MCP-Server fork; installed Jun 13 2026; scopes: gmail.modify + settings.basic).
- **Work** — `lbecker@wikimedia.org` → the pre-existing Gmail connector.
- Default: "my Gmail" / "personal Gmail" → the fork (`mcp__gmail__*`). Say "work Gmail" for the WMF account.

## GitHub workflow
- **Default: local-first** (as of 2026-06-16; it's faster than the API). Claude edits files in a local clone that lives **inside the connected project folder**, runs the test suites in the sandbox, then hands Lane a `git add -A && git commit -m "…" && git push` to run on his Mac.
- **The sandbox never runs `git`.** git's lock/hardlink operations fail on the FUSE-mounted folder ("could not lock config file … Operation not permitted") and the sandbox can't authenticate a push anyway. So cloning, committing, and pushing are all done by Lane on his Mac (native filesystem, his credentials).
- **GitHub API / MCP (`push_files`, `create_or_update_file`) is the backup** — use it when Lane is away from his Mac or no local clone is available. (The PAT-carrying MCP tools still work; they're just no longer the default.)
- **Commit email must be the GitHub `noreply` alias.** Lane's account has "Block command line pushes that expose my email" enabled, so any commit stamped with `lanebecker@gmail.com` is rejected on push with `GH007: Your push would publish a private email address`. Each clone's `user.email` must be his noreply address (`<id>+lanebecker@users.noreply.github.com`, from github.com/settings/emails). Set it globally — `git config --global user.email "<id>+lanebecker@users.noreply.github.com"` — so every clone inherits it. If a commit was already made with the wrong email: `git commit --amend --reset-author --no-edit` then push.
- **Milestones + issues via `gh` (Mac-side scripts).** The GitHub MCP creates/closes/labels/assigns issues but has **no milestone-creation tool**, so Claude hands Lane `gh` commands to run on his Mac. Create a milestone (there's no native `gh milestone` subcommand) via the API: `gh api repos/<owner>/<repo>/milestones -f title="…" -f description="…" -f state=open` (prints the new milestone's `number`). Assign issues by **title**: `gh issue edit <n1> <n2> … --repo <owner>/<repo> --milestone "<title>"` (takes multiple issue numbers in one call). ⚠️ Interactive **zsh doesn't treat `#` as a comment** by default → strip `#` comment lines from pasted blocks or each errors `command not found: #` (harmless — the real commands still run).

## Memory conventions
- **Keep a `log.md`** — an append-only change history, newest at the bottom, each line prefixed with an ISO-8601 UTC timestamp. `scripts/sync-shared-facts.py` creates one per project and records each sync; add your own notable changes too. In a Cowork space, keep it at `memory/log.md`.
- **Timestamp every memory entry.** Any knowledge/memory file with YAML frontmatter carries an ISO-8601 `timestamp` field; the sync script warns about `memory/*.md` entries that don't.

## Engineering workflow (code/tech projects only — skip for non-tech)
- **Review + test after every completed step, unprompted.** On finishing any step that changes code: (a) review the change for correctness/regressions, (b) add or update tests covering it, (c) run the suite — *before* moving on. Don't wait to be asked; this is how the app stays maintainable.
- **End-of-phase adversarial cold audit (agent-based).** After wrapping a significant chunk (e.g., the end of a phase), spawn an **independent subagent** to cold-audit the codebase with no inherited context — hunting for efficiency wins, architecture improvements, significant refactors, and bugs. Agent-based on purpose so Claude can **argue with it**: pressure-test and debate the findings rather than accepting them wholesale, then act on the ones that survive scrutiny.
- **Record code-audit findings as GitHub issues/milestones BEFORE acting on them.** After the cold audit + argue-down triage, file the surviving findings as issues in the relevant repo (group into a milestone if it's a coherent batch) before starting fixes — so nothing's lost mid-remediation. Code audits only; documentation-audit findings are fixed in-flow (no issues needed).
- **Then a documentation cold audit.** After the code audit, cold-audit ALL docs (CLAUDE.md, design docs, READMEs, CHANGELOGs, memory, log.md) to confirm every change is fully mapped and nothing slipped through the cracks.
- **Reviews carry TWO verdicts: spec compliance, then code quality** *(adopted from obra/superpowers, 2026-07-17)*. Every review or audit of a change reports (a) **SPEC** — the change does exactly what the plan/finding/issue text said, nothing missing, nothing extra — and (b) **QUALITY** — it is well-built. A review missing either verdict is incomplete. Apply the SPEC verdict to **remediations** especially: check the fix against the original finding's text before judging the code (every audit round's largest source of findings has been the previous round's remediation). Two sub-rules: never tell an auditor what NOT to flag or pre-rate a finding's severity in the dispatch — the argue-down happens AFTER the report, never inside the prompt; and verify an implementer subagent's "done" against the actual diff, never its self-report.
- **Write plans to the junior-engineer bar** *(adopted from obra/superpowers, 2026-07-17)*. Implementation plans are written for "an enthusiastic junior engineer with poor taste, no judgement, and no project context" — who is also exactly the no-context cold auditor that will later read them. Exact file paths; complete code in every step that changes code; exact commands with expected output. Banned placeholders: "TBD", "add appropriate error handling", "write tests for the above" (without the tests), "similar to Task N" (repeat the content — tasks get read out of order). Self-review the finished plan: (1) spec coverage — every requirement points at a task; (2) placeholder scan; (3) name/type consistency across tasks.
- **Three failed fixes = stop and question the architecture** *(adopted from obra/superpowers, 2026-07-17)*. Count fix attempts on any bug. At the third failure the problem is structural, not hypothetical — do not attempt fix #4; stop and question the pattern (with Lane, if it implies real refactoring). Sibling rule for multi-component pipelines (Gmail→Worker→inbound→parse→commit): instrument the component boundaries — log what enters and exits each layer — to locate the failing component BEFORE forming any hypothesis.
- **Two explanations = stop and MEASURE** *(2026-07-20, #454)*. The sibling of the rule above: it counts fixes, this counts EXPLANATIONS. When one surprising observation gets a second competing explanation, stop mid-sentence and name the measurement that would discriminate them; if none exists, say so rather than picking the nicer story. **Premise before mechanism** — when a fix "doesn't work", first PROVE the fix is present in the thing under test (pushed? deployed? the right URL/row/binary?) before explaining why it failed. **Know what the instrument reports vs interprets**: prefer raw signal (`curl -sI` for status + `cf-cache-status` + `age`, the live pg catalog, executing the function) over anything that renders or summarises, and ask whether the probe could even return *different* output for the two hypotheses — if not, it is not evidence. **And a tool's success message reports ITS criterion, not yours** — name your goal in an observable number BEFORE running anything, then check that number, not the green tick (`memory-index.py --fix` printed "clean" while making the file it was run to shrink 105 bytes BIGGER; the served-surface test passed by asserting the RULES EXISTED while the site was wide open). Where you can, make the tool print the number instead of remembering this. **A verification fetch can cache the broken state and then confirm it**, so cache-bust every check. Earned the hard way: four explanations for one 200 (Pages precedence → nothing deployed → edge cache → apex pinned to an old deployment), three of them wrong, ~2h, plus a whole-repo restructure justified by the first one and written into three files as established fact. One `curl -I` was available from the first 200. ⚠️ Note the three memories that already said "measure, don't reason" — read-the-live-object, verify-the-finding-before-fixing-it, instrument-the-boundaries — were all loaded and none fired, because each triggers on a different ACT (editing, receiving a finding, attempting a fix) and none on EXPLAINING an observation. That is why this is stated as a counter and not as a principle.
<!-- SHARED-FACTS:END -->

Design prototype and production renderer for a vinyl now-playing display (1024×600 Waveshare, Raspberry Pi).

## Repository Structure

| Path | Purpose |
|------|---------|
| `design/` | React/Babel design prototype — browser-runnable, design tool only |
| `src/` | Production Python/Pillow/pygame renderer |
| `src/display/assets/fonts/` | Bundled OFL fonts (Inter Tight, Newsreader, JetBrains Mono) used by the production renderer |
| `PRODUCT.md` | Product spec |
| `DESIGN.md` | Design system and production handoff spec |
| `design/.impeccable/design.json` | Design system tokens for impeccable tooling |

## Prototype vs. Production

The `design/` directory is a **design tool, not production code.** It runs in a browser via React/Babel CDN imports and is used for design iteration and review. All geometry and specs in `DESIGN.md` are derived from the prototype.

The `src/` directory contains the **production renderer** — Python/Pillow/pygame targeting the Raspberry Pi. When a design decision from the prototype is ready to ship, it gets translated here.

## Running the Prototype

Open `design/index.html` in a browser (serve from the `design/` directory — local `file://` works for most features). No build step.

## Key Design Constraints

- Display: 1024×600px target device. The renderer is actually **resolution-independent** — it reads width/height from config and scales every constant proportionally (`s = min(width/1024, height/600)`); there are no hard-coded breakpoints, but there is also no fixed artboard. (A-10)
- Font hierarchy: Inter Tight 600 72px (song) / Inter Tight 500 48px (artist) / Newsreader italic 32px (album) / JetBrains Mono 13px (catalog) / 12px (chip) / 11px (label) — all at the 1024×600 reference, scaled by `s`
- DisplayPalette: 5 values — bg, surface, accent, text, muted — 1s lerp on track change
- Full-Opacity Rule: `p.muted` never gets additional `opacity` stacked on top; it is contrast-clamped to ≥4.5:1 vs `bg` at palette-extraction time (and re-clamped during the lerp)
- Hue Diversity Rule (**NOT a production feature — deliberately deferred 2026-06-19, #73**): the prototype's `data.js` is hand-tuned for ≥60° OKLCH hue separation between album accents, but the production renderer does **not** implement it and is not planned to. `accent` is simply the most-saturated quantized color of the current cover, in isolation — kept authentic to the artwork on purpose. Enforcing cross-album separation would make the accent synthetic (a color not in the cover) and the palette order-dependent (breaking the pure `url → palette` cache); revisit only if same-hue runs prove distracting on the physical display. (was A-1)
- WCAG AA: 4.5:1 minimum contrast on all text
