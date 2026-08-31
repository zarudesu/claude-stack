---
name: vaultwarden-unlock
description: "Vaultwarden auto-unlock через macOS Keychain для проектов на этой машине. Use when: vaultwarden, bitwarden, bw unlock, анлок вольта, нужен кред из vaultwarden, BW_SESSION."
---

## Vaultwarden auto-unlock (для проектов на этой машине)

Проекты с Vaultwarden — анлок через keychain без интерактива: `source <path-to-devops-repo>/scripts/bw-auto-unlock.sh <profile>` (профили — по одному на группу проектов; скрипт читает креды из macOS Keychain `bw-<profile>-*`, экспортит `BW_SESSION`, идемпотентен). Кредов нет в keychain → один раз `bw-keychain-setup.sh <profile>`. Полная инструкция — в runbook своего devops-репо.
