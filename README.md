# Oracle Space Hunt

Minimal dedicated GitHub Actions repo for safely hunting Oracle Always Free A1 capacity.

[![Oracle Space Hunt](https://github.com/Leebartea/oracle-space-hunt/actions/workflows/oracle-space-hunt.yml/badge.svg)](https://github.com/Leebartea/oracle-space-hunt/actions/workflows/oracle-space-hunt.yml)

This repo only contains:

- the Oracle retry script
- example config files
- a scheduled GitHub Actions workflow

It does **not** commit:

- your OCI private API key
- your live Oracle config
- your SSH private key
- any bot secrets

## Why GitHub Actions

This runs the hunt on GitHub-hosted runners, so it does not depend on your Mac being awake.

That means:

- the schedule runs online, not on your machine
- your Mac can sleep or be offline
- the Oracle VM, once created, persists in Oracle normally

## Security Model

This repo is safe to keep public **only if you keep all live values in GitHub Secrets**.

Store these as repository secrets:

- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_TENANCY_OCID`
- `OCI_REGION`
- `OCI_API_KEY_PEM`
- `ORACLE_HUNT_CONFIG_JSON`

`ORACLE_HUNT_CONFIG_JSON` should be based on `oracle_free_tier_retry_launch.example.json`, but with your real Oracle values and **without** any local-only artifact paths.

Recommended:

- keep the repo public for free GitHub-hosted runs
- never commit `oracle_space_hunt.local.json`
- never echo secrets in workflow steps

## Required Config Notes

Inside `ORACLE_HUNT_CONFIG_JSON`, set:

- `tenancy.profile` to `DEFAULT`
- `launch.ssh_authorized_keys_file` to `/home/runner/.ssh/oracle_bot_key.pub`

The workflow writes your SSH public key file from the JSON field `launch.metadata.github_runner_ssh_public_key`, then removes that helper field before running. That keeps the runtime script unchanged.

## Manual Test

After adding secrets, you can trigger the workflow manually from the Actions tab:

- `Oracle Space Hunt`
- `Run workflow`

## Monitoring Links

- Repo: `https://github.com/Leebartea/oracle-space-hunt`
- Actions UI: `https://github.com/Leebartea/oracle-space-hunt/actions`
- Workflow page: `https://github.com/Leebartea/oracle-space-hunt/actions/workflows/oracle-space-hunt.yml`
- Latest successful validation run: `https://github.com/Leebartea/oracle-space-hunt/actions/runs/24071571530`

If you are using the GitHub-hosted hunter, you do **not** need to keep the local Mac scheduler running. The GitHub workflow is the active Oracle hunter.

## Schedule

The workflow is scheduled at minute `7` and `37` every hour to avoid top-of-hour GitHub schedule congestion.
