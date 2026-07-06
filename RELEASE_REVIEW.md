# Sector Gap Analyzer — Pre-Release Go/No-Go Review

**Reviewer role:** Independent release engineering / QA / security review
**Date:** 2026-07-05
**Reviewed revision:** `b255363` ("feat: make the app installable/portable for other users"), cloned fresh from GitHub
**Verdict: NO-GO — conditionally.** Fix the four code blockers below (all small), disable UPX, and run one clean-VM install test; after that this is a GO. Nothing found is architecturally wrong — the pipeline core is solid and behaved correctly in every functional test that could be run here.

---

## Review environment and its limits (read first)

This review ran in a Linux sandbox with full source access, Python 3.10, and a local mock EDSM server. That allowed real execution of the entire CLI pipeline and the GUI's worker/pipeline layer. It did **not** allow:

- Building the Windows PyInstaller exe or running Inno Setup — spec/installer findings are from code review plus a Linux PyInstaller smoke build of the CLI (which passed, including ijson collection).
- Launching the Tkinter GUI (no display / no tkinter in sandbox) — `gui/app.py` findings are from code review; `worker.py` + `pipeline.py` were executed headless.
- Reaching the live EDSM API (sandbox egress blocked) — validation logic was exercised against a local mock HTTP server instead, including success, in-EDSM, HTTP-500, retry, and cache paths.
- **Scanning any built artifact with VirusTotal or a local AV.** No release artifact exists yet (no tags/releases on the repo), and this environment has no AV/VT access. Part D below is research-based; the actual detection profile must be measured by uploading the first built installer to VirusTotal before release. I am stating this explicitly rather than guessing vendor results.

**The single most important open item is the one this review cannot do for you:** install and first-run on a clean Windows machine/VM with no Python installed. Nothing in the code *suggests* an undeclared dependency (the frozen app needs only stdlib + tkinter + ijson, all bundled; TLS on Windows uses the OS cert store), but that test has never been run anywhere, and it is the canonical way PyInstaller releases fail. Treat it as a release gate.

---

## Go/No-Go blockers

### BLOCKER 1 — One bad sector name aborts the entire multi-sector run
`scripts/gap_full_export.py:441` and `:453`, `scripts/gap_extrapolate_export.py:668` call `sys.exit(1)` inside `run()` — library functions the GUI calls directly.

**Reproduced:** ran `gui.pipeline.run_pipeline` through `gui.worker.Worker` with sectors `["Nomatch Sector", "Testa Sector", "Testb Sector"]`. Extraction succeeded, then `gap_full_export.run()` hit "0 systems loaded" for the first sector and `sys.exit(1)` unwound the whole job: the two valid sectors were never analyzed, no aggregation ran, and the GUI would show only `exit=1`. A first-time user with one typo in a sector list loses an entire (multi-hour, on real data) run's analysis phase.

**Fix:** replace `sys.exit(1)` with an exception or return code in `run()` functions (keep `sys.exit` in `main()` only), and have `gui/pipeline.py` catch/skip per sector the same way it already does for a missing DB file (`pipeline.py:255-257`).

### BLOCKER 2 — Uncompressed `.json` galaxy dump crashes extraction
`scripts/extract_sector_systems_to_sqlite.py:277` — `iter_systems_json_doc()` hardcodes `gzip.open(path, "rb")`, but the README and `detect_format()` both promise support for plain `.json` array documents, and the GUI's file browser explicitly offers `*.json`.

**Reproduced:** a plain `.json` JSON-array dump → `gzip.BadGzipFile: Not a gzipped file (b'[\n')`, uncaught traceback, empty sqlite file left behind. (The `.json.gz` array path and both JSONL paths work correctly.) Anyone who decompresses their dump — common, for speed — hits this on first run.

**Fix:** open binary conditionally: `handle = gzip.open(path, "rb") if path.suffix.lower() == ".gz" else path.open("rb")`.

### BLOCKER 3 — Closing the window during a run can corrupt sector databases
`gui/app.py:285-292` + `gui/worker.py:68`. `_on_close()` sets the cancel event and then calls `root.destroy()` immediately. The worker is a `daemon=True` thread, so the interpreter kills it mid-flight — possibly mid-`executemany`/mid-commit into a sector sqlite or the EDSM cache. Cooperative cancellation (which works — see test results) never gets a chance to flush. A user who cancels-and-closes at hour 3 of a 5-hour extraction can corrupt the DB they were building.

**Fix:** after `self.worker.cancel()`, disable the window and poll `worker.is_running()` via `root.after(...)`, destroying only once the thread exits (with a timeout fallback + warning).

### BLOCKER 4 — Release gate: clean-machine install/run test (process, not code)
Not performable from this environment (see limits above). Before tagging v1.0.0: build, install, and run extraction + a small validated run on a Windows VM with no Python/dev tooling. Verify in the packaged app's log that ijson reports backend `yajl2_c` (see C-2).

---

## A. Functional QA — what was tested and found

Synthetic Spansh-format dumps (72 systems, 2 real sectors + noise + a token-boundary trap name, JSON-array gz/plain and JSONL variants) were used to exercise the pipeline end-to-end.

**Passed:**
- Single-sector extraction (`.json.gz` array + `.jsonl`): correct counts (18 systems / 22 bodies / 11 rings), token-boundary prefix matching correctly excluded `"Testa Sectorius XY-Z a1-1"` from prefix `"Testa Sector"`... and unit checks of `matches_prefix` confirmed the "Oochost"/"Oochostia" case from the docstring.
- Multi-sector single-pass extraction: identical per-sector results to single-sector runs.
- Bracketed gap generation: exactly the hand-computed set (`d1-3`, `d1-7`, `c2-5`); wide gap (width 40 > max 25) correctly skipped; known/duplicate removal correct.
- Backward extrapolation: `c2-2, c2-1, c2-0` with correct `steps_from_edge`, floor at 0 respected.
- Forward chaining: step-1 per family; chain extended to step 2 only for the family whose step-1 hit in mock EDSM; chain summary CSV correct (`spansh_max=4, edsm_confirmed_max=5, terminated at step 2`).
- **EDSM failure-handling regression check (the previously fixed bug):** in both shipping scripts, a failed check (HTTP 500 after 2 retries) is excluded from results, warned about, and *not* written to the cache — confirmed by test and by cache-table inspection. Retry/backoff and the 7-day cache TTL logic behave as documented; re-runs made zero API calls for cached names and re-attempted only the failed one. **However, the original bug still exists verbatim in `planner/edsm.py:101-104`** (`exists = False` on exception, *and* it caches the false negative). See B-1.
- Rate limiter: 1 req/s pacing logic verified by inspection and (scaled) under mock; both retries and fresh requests pass through `_throttle()`.
- Cross-sector aggregation: 12/12 expected rows, correct type ordering, dry-run/`skipped` and `check_failed`/`pending` rows correctly excluded, `master_gap_candidates.csv` correctly ignored on re-scan.
- Mid-run cancellation: cancel during extraction flushes and returns 130; cancel during validation stops promptly, remaining stages skipped, GUI sentinel `__job_done__:130` delivered; a second `Worker.start()` while running is refused.
- Settings persistence (`gui/config.py`): round-trip save/load, corrupt-JSON fallback to defaults, and partial-config stage merging all pass.
- First-run guard (code review): empty project dir and missing dump are both caught before starting, with redirect to Settings (`app.py:222-239`).

**Not testable here (must be covered by the clean-VM pass):** GUI widget behavior, installer install/upgrade/uninstall lifecycle, user-data survival across uninstall. On the last point, code review says data *will* survive: config lives in `%APPDATA%\SectorGapAnalyzer`, workspace in `%LOCALAPPDATA%\SectorGapAnalyzer\workspace`, the app installs to `%LOCALAPPDATA%\Programs\SectorGapAnalyzer`, and `installer.iss` has no `[UninstallDelete]` — uninstall removes only the app directory. Verify on the VM anyway.

### A punch list (beyond the blockers)

| # | Severity | Finding |
|---|----------|---------|
| A-1 | Nice-to-have | Cancelling mid-validation still writes `*_validated.csv/md` outputs that look complete (reproduced: cancelled at 3/3, got a 2-row "validated" CSV with no incompleteness marker). If the user later runs with only "Aggregate" checked, the partial file merges silently. Suggest a `_partial` suffix or a cancelled-run marker line, or delete phase outputs on cancel. |
| A-2 | Nice-to-have | The Run guard requires the galaxy dump to exist even when the Extract stage is unchecked (`gui/app.py:229-239`) — users re-analyzing existing sector DBs after deleting the 100+ GB dump are blocked for no reason. Only require the dump when `stages["extract"]` is true. |
| A-3 | Nice-to-have | GUI accepts non-positive/nonsense numeric params silently (`_collect_config`, `app.py:197-212` falls back to defaults on non-int but accepts e.g. negative depths); CLI validates `max_forward_step <= extend_depth` but the GUI path never does. |
| A-4 | Nice-to-have | `check_failed` forward-chain candidates terminate the chain and are recorded as `terminated` in the chain summary (`gap_extrapolate_export.py:752-760`) — an EDSM outage mid-chain is indistinguishable from a genuine "not in EDSM" termination in the summary. Consider a `check_failed` chain status. |
| A-5 | Trivial | Off-by-one inconsistency: `--limit` stops at `>= limit` in the single-sector extractor (line 631) but `> limit` in multi-sector (line 205). |

---

## B. Code quality & security

**SQL injection: clean.** Every query in the shipping code (`scripts/*.py`, `gui/*`) uses `?` parameterization — checked all `execute`/`executemany` call sites. No string-formatted SQL anywhere, including the user-supplied sector name paths.

**Path injection: clean.** Sector names are sanitized to `[A-Za-z0-9_-]` before becoming DB filenames (`sanitize_prefix`, verified: `"../../evil"` → `"evil"`, `".."` → `"sector"`), and to `[a-z0-9_]` for output slugs. No user input reaches a path unsanitized.

| # | Severity | Finding |
|---|----------|---------|
| B-1 | **High** | `planner/edsm.py:101-104` still contains the original EDSM bug — worse than the original, in fact: on any fetch failure it sets `exists = False` **and caches the false negative**, so a transient network error becomes a poisoned 7-day cache entry claiming "confirmed not in EDSM." The `planner/` package is currently dead code (it imports `planner_strategic.*`, which doesn't exist in this repo, so it can't even be imported) and the README says to treat it as reference material — but shipping a known-buggy copy of the exact bug this release cycle fixed is a landmine for the first person who "reconnects" it. Delete `planner/`, `tests/`, and `scripts/offline_gap_lists.py` from the release, or fix the bug in place with a loud comment. |
| B-2 | High | The test suite cannot run at all: `tests/test_strategic_planner.py:10` imports the nonexistent `app.config` → pytest collection error, 0 tests execute. The project ships with effectively zero automated tests of the code it actually runs. Acknowledged in the README, but for a public release the shipping pipeline (candidate generation, EDSM cache/error handling, `matches_prefix`) deserves a small real test file — most of the harness logic used for this review would translate directly. |
| B-3 | Medium | LIKE wildcards in sector names are not escaped: `"SELECT name FROM systems WHERE LOWER(name) LIKE LOWER(?)"` with `sector + " %"` (`gap_full_export.py:430-433`, `gap_extrapolate_export.py:658-661`). A sector entry containing `%` or `_` over-matches. Not injection (parameterized), and impact is bounded because each sector DB only contains one sector's systems — but a sector name like `A_a` silently matches wrong rows. Escape `%`/`_` with `ESCAPE '\'`. |
| B-4 | Medium | README's SSL troubleshooting ("`pip install pip-system-certs`") is unusable for end users of the packaged exe — there is no pip in a frozen app. Since the TLS-interception problem (Norton et al.) was already observed on the dev machine, expect it in the field. Either bundle the equivalent behavior (e.g. `truststore`-style init at startup on Windows) into the build, or rewrite that section with an end-user-appropriate remedy. |
| B-5 | Low | `extract_sector_systems_to_sqlite.py:551-552`: the sqlite connection leaks if `init_db` raises (created before the `try`). Same pattern in `gap_full_export.run` Phase 1 (`conn` at 428, no try/finally around the query). Cosmetic in practice. |
| B-6 | Low | `gap_full_export.run()` never creates `out_dir` (its CLI `main()` does at line 599; `gap_extrapolate_export.run()` does at 671). The GUI pre-creates it, so this only bites direct API callers — which is exactly how it bit this review's first harness run. Add `out_dir.mkdir(parents=True, exist_ok=True)` for symmetry. |
| B-7 | Low | Dead vestigial imports `from app.config import ...` wrapped in `try/except Exception` in both extractors (`extract_sector_systems_to_sqlite.py:21-25`) — always fails here, silently. Remove to avoid masking real import errors. |
| B-8 | Low | Overlapping sector prefixes in multi-extract are first-match-wins with a misleading comment (`extract_multi_sector_to_sqlite.py:223`, "a system name can only belong to one sector prefix" — false for nested prefixes like `"Oochost"` + `"Oochost A"`). Document or detect overlap. |
| B-9 | Info | `write_chain_summary`'s markdown row builder (`gap_extrapolate_export.py:601-616`) relies on implicit f-string concatenation binding tighter than the ternary — it *is* correct (verified by output), but it's one edit away from a very confusing bug. Parenthesize the two branches. |

Thread/resource lifecycle otherwise looks correct: one job at a time under a lock, fresh `Event` per run, unbounded queue drained on a 100 ms Tk timer, `contextlib.redirect_stdout` scoped to the worker thread's callable, connections closed in `finally` in all validation paths, and multi-sector sinks flushed+closed in `finally` even on KeyboardInterrupt (verified by cancellation test). One caveat: `redirect_stdout` is process-global, not thread-local — anything the Tk main thread printed during a run would be swallowed into the log queue. Harmless today; worth a comment.

---

## C. Packaging & build integrity

Could not build the Windows artifact here (see limits). Findings from spec/installer review + Linux smoke build:

| # | Severity | Finding |
|---|----------|---------|
| C-1 | **Blocking (do before release)** | `SectorGapAnalyzer.spec:35,49` — `upx=True`. Disable it. Full rationale in Part D; short version: meaningful AV false-positive reduction for a trivial cost, and onedir barely benefits from UPX anyway. |
| C-2 | High | ijson backend: the spec's `collect_all('ijson')` + explicit `yajl2_c` hiddenimport is the right approach and resolved correctly in a Linux smoke build (a PyInstaller onedir build of `gap_full_export.py` ran the full dry-run pipeline successfully). Remaining risk is environmental: if the build venv's ijson was installed without the compiled backend, the frozen app silently falls back to pure Python (~several× slower on a 113 GB dump — hours of difference). Add a build-gate check, e.g. a startup log line and a pre-build `python -c "import ijson; assert ijson.backend=='yajl2_c'"`. |
| C-3 | High | Upgrade lifecycle, `installer.iss`: (a) no `CloseApplications=yes`, so installing over a running app fails with file-in-use errors; (b) no stale-file cleanup — `[Files]` overlays the new onedir tree onto the old one, so files removed between releases (renamed DLLs after a Python/PyInstaller bump, dropped modules) linger forever in `{app}`, a classic source of broken upgrades and AV heuristic noise. Add `CloseApplications=yes` and an `[InstallDelete]` entry (or wipe `{app}\_internal` before install). |
| C-4 | Medium | The exe has no Windows version resource (no `version=` in the spec) — shows blank publisher/product/version metadata in Task Manager and properties. Zero-metadata binaries score worse with SmartScreen/AV heuristics and look unprofessional. Generate a version-info file and reference it in `EXE()`. |
| C-5 | Medium | Reproducibility: not achievable today. `requirements*.txt` pin nothing (`ijson>=3.2`, `pyinstaller>=6.0`), and PyInstaller output embeds timestamps unless `SOURCE_DATE_EPOCH` is set. For release integrity (and for the checksum story in Part D), pin exact versions for release builds (a `requirements-build.lock`), record the Python version in the release notes, and set `SOURCE_DATE_EPOCH`. Perfect bit-reproducibility isn't required for v1.0.0, but a documented, pinned build environment is. |
| C-6 | Medium | No published checksums: the README section drafted in Part D assumes a SHA-256 per release artifact. Add checksum generation to the release procedure (`certutil -hashfile ... SHA256` or `Get-FileHash`) and paste it into each GitHub Release's notes. |
| C-7 | Low | `installer.iss` `[Files]` lacks `ignoreversion` — versionless PyInstaller DLLs can be skipped on upgrade if Windows thinks the existing file is "newer." Standard Inno guidance: `Flags: ignoreversion recursesubdirs`. |
| C-8 | Low | Repo hygiene for a clean checkout build: fine — `.gitignore` covers `build/`, `dist/`, `dist-installer/`, `*.sqlite`; no data or artifacts tracked; icon files present; fresh clone + `pip install -r requirements-dev.txt` is sufficient on the Python side (verified everything imports and compiles under 3.10, matching the README's stated floor). |

---

## D. Antivirus / SmartScreen analysis (priority area)

### D-1. Why this app *will* trigger warnings

Expect warnings for every user on first release. Four compounding causes:

1. **Zero file reputation.** SmartScreen is reputation-based: a brand-new unsigned exe with no download history gets "Windows protected your PC" until enough users run it. Every new release resets this (new hash), so it recurs at each version bump.
2. **Unknown publisher, twice.** Both the Inno Setup installer *and* the inner `SectorGapAnalyzer.exe` are unsigned, so both UAC-adjacent surfaces show "Unknown publisher."
3. **PyInstaller bootloader guilt-by-association.** Real malware is built with PyInstaller, so some AV engines' heuristics (and occasionally their signature databases) flag the generic bootloader stub itself; clean apps inherit the flag. This is a well-documented, years-old problem ([PyInstaller's own issue template](https://github.com/pyinstaller/pyinstaller/blob/develop/.github/ISSUE_TEMPLATE/antivirus.md), [pythonguis overview](https://www.pythonguis.com/faq/problems-with-antivirus-software-and-pyinstaller/), [pyinstaller#6754](https://github.com/pyinstaller/pyinstaller/issues/6754)). The onedir layout you already use is the *less* suspicious option (no self-extract-to-temp "dropper" pattern that onefile has) — good call, keep it.
4. **UPX packing** (currently enabled) — packed executables are a malware hallmark and a known false-positive multiplier ([upx#711](https://github.com/upx/upx/issues/711)).

### D-2. UPX: disable it

Recommendation: **set `upx=False` in both `EXE()` and `COLLECT()`.** The tradeoff is lopsided:

- Benefit of UPX here is small: it's an onedir build, so there's no single-file size pressure; the installer already LZMA2-compresses everything, and UPX-then-LZMA typically *worsens* final installer size versus LZMA on uncompressed binaries. Startup is marginally *slower* with UPX (decompress on load) and packed DLLs defeat page sharing.
- Cost of UPX is real: measurably higher heuristic detection rates on exactly the engines most likely to quarantine (consumer AVs). For a v1.0.0 with zero reputation, this is the one free knob you control.
- Caveat for honesty: UPX only ran at all if `upx.exe` was on the build machine's PATH — if it wasn't, your binaries are already unpacked and this change is a no-op formality. Check the built DLLs (`upx -t`) or just set the flag and stop thinking about it.

### D-3. Code signing: pursue SignPath Foundation; don't buy a cert yet

- **[SignPath Foundation](https://signpath.org/)** offers free OV-level code signing for qualifying OSS projects ([terms](https://signpath.org/terms.html)). Sector Gap Analyzer plausibly qualifies: OSI license required (⚠ the repo currently has **no LICENSE file** — that's a prerequisite, add one), public repo, actively maintained, released artifacts, MFA on GitHub, a published code-signing policy page, and CI-integrated signing (they sign via their platform, key held in their HSM; cert names SignPath Foundation, not you). Cost: application effort + setting up CI, roughly a weekend. This is the recommended path: OV signatures don't instantly clear SmartScreen (reputation still accrues per-cert, but the cert's reputation persists across releases — that's the win over unsigned).
- **Microsoft Azure Trusted Signing / Artifact Signing** ($9.99/mo) would be ideal (its certs clear SmartScreen quickly), but as of mid-2026 individual-developer onboarding is restricted/paused (US/CA orgs with 3-year history; [status](https://techcommunity.microsoft.com/blog/microsoft-security-blog/trusted-signing-is-now-open-for-individual-developers-to-sign-up-in-public-previ/4273554), [FAQ](https://learn.microsoft.com/en-us/azure/artifact-signing/faq)) — check current eligibility, but don't block the release on it.
- **Paid OV certs** (~$200–400/yr + hardware key requirements) are not worth it for a free hobby tool at this stage.
- **Interim plan (v1.0.0): ship unsigned + document the warnings** (README section below) **+ publish SHA-256 checksums** in each release, **+ submit the installer to Microsoft's [false-positive submission portal](https://www.microsoft.com/en-us/wdsi/filesubmission) and to any flagging vendors after checking VirusTotal.** Uploading each release to VirusTotal yourself, pre-announcement, also gives you the real per-vendor detection list this review could not produce.

### D-4. What I could not verify

No built installer/exe exists yet (no GitHub releases, and this environment can't produce a Windows build or reach VirusTotal), so **no actual scan was performed and no per-vendor detections can be reported.** Before the release announcement: build, scan on VirusTotal, record results, and file false-positive reports with any engine that flags it.

### D-5. Drafted README section (ready to paste)

Suggested placement: immediately after step 1 of "Installation (end users)", replacing the current single SmartScreen sentence.

---

```markdown
## Antivirus and SmartScreen warnings — what to expect and what to do

Sector Gap Analyzer is a small open-source tool. Its releases are currently
**not code-signed** (code-signing certificates are expensive and reputation
takes time to build), so Windows and some antivirus products will treat the
installer with suspicion the first time they see it. **This is expected and
does not mean anything is wrong with the file** — it means Windows has never
seen this exact file before and no publisher identity is attached to it.
You can (and should) verify the download yourself; instructions below.

### First: verify your download

Every release on the [Releases](../../releases) page lists a SHA-256
checksum for the installer. After downloading, open PowerShell and run:

    Get-FileHash .\SectorGapAnalyzer-Setup-X.Y.Z.exe -Algorithm SHA256

If the hash printed matches the one in the release notes, your file is
byte-for-byte the one the maintainer published, and the warnings below are
safe to click through. If it does **not** match, delete the file and
re-download from the Releases page only — never from a third-party mirror.

### "Windows protected your PC" (SmartScreen) — blue dialog

This appears because the installer is new and unsigned, not because
anything harmful was detected. If your checksum matched:

1. Click **More info**.
2. Click **Run anyway**.

That's it — SmartScreen only gates the first run.

### Your antivirus flags, quarantines, or deletes the file

Some antivirus products go further than a warning and quarantine the
installer or the installed `SectorGapAnalyzer.exe`. This is a **false
positive** with a known cause: the app is packaged with PyInstaller (a
standard tool that bundles a Python program and the Python runtime into an
exe), and because some actual malware is also built with PyInstaller, a few
antivirus engines flag *everything* built with it. See PyInstaller's own
[note on antivirus false positives](https://github.com/pyinstaller/pyinstaller/blob/develop/.github/ISSUE_TEMPLATE/antivirus.md).

If this happens:

1. **Verify the checksum first** (above). Only proceed if it matches.
2. **Restore the file from quarantine** using your AV's quarantine/history
   screen (the wording varies: "Restore", "Allow", "Not a threat").
3. **Add an exclusion** so it doesn't recur — either for the installer
   file, or (after installing) for the app folder:
   `%LOCALAPPDATA%\Programs\SectorGapAnalyzer`
   In Windows Security this is under: Virus & threat protection →
   Manage settings → Exclusions → Add or remove exclusions.
4. Optionally, **report the false positive** to your AV vendor — this
   genuinely helps: enough reports get the file whitelisted for everyone.

**Never add exclusions for files whose checksum you haven't verified.**
The verify-then-restore order matters: the checksum is what tells you the
flagged file really is the one published here.

### Why not just sign the releases?

We're pursuing free code signing for open-source projects (e.g.
[SignPath Foundation](https://signpath.org/)). Until that's in place,
checksums + this documentation are the interim answer. If a release is
ever signed, this section will be updated.
```

---

## Consolidated punch list (priority order)

**Blocking for v1.0.0:**
1. Stop `sys.exit()` aborting multi-sector GUI runs (`gap_full_export.py:441,453`; `gap_extrapolate_export.py:668`; handle per-sector in `gui/pipeline.py`).
2. Fix plain-`.json` array-doc crash (`extract_sector_systems_to_sqlite.py:277`).
3. Safe shutdown while a job is running (`gui/app.py:285-292`).
4. `upx=False` in `SectorGapAnalyzer.spec:35,49`.
5. Clean Windows VM: install → first run → small end-to-end run → upgrade-over-install → uninstall (confirm workspace/config survive). Verify `ijson.backend == yajl2_c` in the frozen app.
6. Installer upgrade hygiene: `CloseApplications=yes` + `[InstallDelete]`/`ignoreversion` (`installer.iss`).
7. Add a LICENSE file (also a SignPath prerequisite).
8. VirusTotal-scan the built installer; publish SHA-256 in the release; add the README AV section (Part D-5).

**High / should do soon:** delete-or-fix `planner/edsm.py` false-negative caching (B-1); a minimal real test suite for the shipping pipeline (B-2); pin build deps + version-info resource (C-4, C-5); apply to SignPath Foundation (D-3).

**Nice-to-have:** partial-output marker on cancel (A-1); don't require the dump when Extract is unchecked (A-2); GUI param validation (A-3); `check_failed` chain status (A-4); LIKE-wildcard escaping (B-3); frozen-app SSL/TLS-interception story (B-4); remaining Low items in B/C.

---

*Report generated as part of an independent pre-release review. No commits were pushed and no release artifacts were modified.*
