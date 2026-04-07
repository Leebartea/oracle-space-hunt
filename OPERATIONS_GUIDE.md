# Oracle Space Hunt Operations Guide

This guide covers the small set of commands and UI actions needed to manage the hosted Oracle hunter later without re-learning the setup.

## Main Links

- Repo: `https://github.com/Leebartea/oracle-space-hunt`
- Workflow page: `https://github.com/Leebartea/oracle-space-hunt/actions/workflows/oracle-space-hunt.yml`
- Actions list: `https://github.com/Leebartea/oracle-space-hunt/actions`
- Hosted dashboard: `https://leebartea.github.io/oracle-space-hunt/`

## What Runs Automatically

- The hosted hunter runs on GitHub Actions, not on your Mac.
- Current schedule targets minute `:07`, `:22`, `:37`, and `:52` of every hour.
- GitHub scheduled workflows are best-effort, so some runs may drift by a few minutes or occasionally be skipped.

## Normal Monitoring

Use these pages:

- Hosted dashboard for the latest visible status
- Workflow page for manual runs or workflow disable/enable
- Actions list for exact run history

What the main dashboard fields mean:

- `Attempts`: total hosted workflow runs reflected in the dashboard
- `Next Run`: next expected GitHub schedule slot
- `Fallback`: `1 OCPU / 6 GB (manual only)` means smaller fallback is available, but only when manually triggered
- `Guarded after success`: future scheduled runs wake up but exit early without doing fresh Oracle checks

## Manual Fallback Run

Fallback is a one-off smaller hunt profile.

Use GitHub UI:

1. Open the workflow page.
2. Click `Run workflow`.
3. Choose `fallback`.
4. Start the run.

Use terminal:

```bash
gh workflow run oracle-space-hunt.yml -R Leebartea/oracle-space-hunt -f profile=fallback
```

## Manual Primary Run

Use GitHub UI:

1. Open the workflow page.
2. Click `Run workflow`.
3. Choose `primary`.
4. Start the run.

Use terminal:

```bash
gh workflow run oracle-space-hunt.yml -R Leebartea/oracle-space-hunt -f profile=primary
```

## Check Recent Run History

```bash
gh run list -R Leebartea/oracle-space-hunt --workflow oracle-space-hunt.yml --limit 10
```

## Watch One Run Live

```bash
gh run watch RUN_ID -R Leebartea/oracle-space-hunt --exit-status
```

Replace `RUN_ID` with the run ID from the Actions page or `gh run list`.

## Full Shutdown

If Oracle capacity is secured and you want GitHub to stop waking the workflow at all:

Use GitHub UI:

1. Open the workflow page.
2. Open the workflow controls menu.
3. Click `Disable workflow`.

Use terminal:

```bash
gh workflow disable oracle-space-hunt.yml -R Leebartea/oracle-space-hunt
```

## Re-enable Later

```bash
gh workflow enable oracle-space-hunt.yml -R Leebartea/oracle-space-hunt
```

## Soft Stop vs Full Stop

- `Soft stop`: already implemented. After a real success, future scheduled runs should exit early before doing fresh Oracle checks.
- `Full stop`: disables the workflow itself, so GitHub stops waking it entirely.

## Recommended Default

- Keep the hosted hunter active while Oracle capacity is still unavailable.
- Use manual fallback only when you explicitly want to test `1 / 6`.
- Once a VM is secured and you have confirmed it, use `Disable workflow` for a clean full stop.
