# Vultr Fallback Guide

This is a later-option guide in case Oracle remains blocked and Vultr becomes affordable enough for you to use as a paid fallback VPS.

## Why Vultr Was Considered

Vultr is one of the more straightforward mainstream VPS providers that:

- supports credit and debit cards
- has simpler self-serve VPS provisioning than some stricter hosts
- is cheaper than DigitalOcean and Linode at similar common sizes

## Honest Budget Note

Vultr is not in the same low-cost range as Hetzner's cheaper plans.

That means:

- it is a practical fallback
- but not the cheapest paid fallback
- if budget is tight, it may still be too expensive right now

## Recommended Sizes For This Bot

Minimum workable starting point:

- `2 vCPU`
- `4 GB RAM`
- `80 GB NVMe`

Calmer option with more room:

- `4 vCPU`
- `8 GB RAM`
- `160 GB NVMe`

## Suggested Approach If You Use Vultr Later

1. Create the Vultr account and complete card verification.
2. Create one clean Ubuntu server.
3. Use the same deployment posture as the other hosts:
   - Docker
   - Postgres
   - private app access first
   - same Git-based update path
4. Treat it as a first stable hosted runtime if Oracle is still unavailable.

## Why This Would Still Help Oracle Later

Running the bot successfully on Vultr would still make a later Oracle move easier because:

- the bot stack is already portable
- the Git-based deployment flow stays the same
- Docker/compose behavior gets proven first on a real VPS

## What To Revisit Later

Before using Vultr later, re-check:

- current Vultr pricing
- current payment acceptance for your account/region
- whether Oracle capacity has improved enough to avoid paying at all

## Keep Oracle Hunter Separate

Even if Vultr becomes the bot host later, the GitHub-hosted Oracle hunter can keep running until Oracle capacity is finally secured.
