# Батч-разбор очереди отложенного дрейфа

Очередь `.ground-truth/pending.yaml` (schema.md §15) — не свалка: у неё лимиты,
срок жизни записи и метрики качества. Каждая запись закрывается вердиктом с
evidence; запись, перешедшая в следующий прогон без вердикта, получает
`batches_survived += 1` и после `max_batches` эскалирует.

## Когда

Что наступит раньше:

- расписание — 2 раза в неделю (CI schedule ниже);
- очередь заполнена на 70% `total` и больше;
- перед merge в main.

SessionStart дописывает к строке очереди `; batch review due: <причина>` при
заполнении >= 70%, последнем прогоне в `pending-log.yaml` старше 3.5 дней
(лога нет — по самой старой записи) при непустой очереди или эскалированной записи.

## Процедура разборщика

1. Прочитать `.ground-truth/pending.yaml` и
   `python3 tools/ground_truth/gt_pending.py --root . report`.
2. Для каждой записи проверить код и контракт (`evidence`, `first_seen.rev`,
   STATUS.yaml, model.yaml) и выбрать вердикт:

| Вердикт | Когда | Что с записью |
|---|---|---|
| Keep | долг осознанно оставлен | остаётся с `kept: {date, evidence}`, `batches_survived` обнуляется; возраст для эскалации — от `kept.date`; в `total`/`per_area` по-прежнему считается |
| Update | контракт или код поправлены в этом же MR | снимается |
| Consolidate | дубль другой записи | снимается; `into: <key>` — другая запись очереди, в которую он влит |
| Replace | claim заменён новым | снимается; evidence содержит `STATUS.yaml#<id>` существующего claim'а |
| Delete | находка неактуальна; `noise: true` — ложная, шум проверки | снимается |

   Evidence обязателен для каждого вердикта: что проверено и где (файл:строка,
   коммит, claim id). Пустой или `todo`/`tbd`/`n/a`/`-` не принимается.
3. Записать вердикты в файл (YAML-список) и применить:

```yaml
- key: uncovered:src/a.py
  verdict: Update
  evidence: STATUS.yaml claim price now covers src/a.py
- key: stale-area:bot
  verdict: Keep
  evidence: bot refactor in progress, receipt after the release branch lands
- key: missing-path:price:src/old.py
  verdict: Replace
  evidence: replaced by STATUS.yaml#price_v2
- key: cited-line:price:tests/test_core.py:64b5daad
  verdict: Consolidate
  into: missing-path:price:src/old.py
  evidence: same stale claim
- key: no-canary:src/
  verdict: Delete
  noise: true
  evidence: src/ has a canary in tests/test_canary.py:12, the check misreads it
```

```bash
python3 tools/ground_truth/gt_pending.py --root . batch --verdicts verdicts.yaml
```

   Применение атомарно: неизвестный key, дубль key, вердикт вне списка, пустой
   evidence, Consolidate без `into`, Replace без claim id, `noise` не у Delete —
   rc 1, очередь и лог не тронуты. Записи без вердикта получают
   `batches_survived += 1`.
4. Закоммитить очередь, `pending-log.yaml` и правки контракта одним MR. Файл
   вердиктов не коммитится.

## Метрики и тревоги

`batch` дописывает в `.ground-truth/pending-log.yaml` один блок за прогон
(только append, коммитится):

```yaml
- date: '2026-10-13'
  rev: abc1234
  keys: [stale-area:bot]               # очередь после прогона
  flow: {in: 3, out: 4}                # новые ключи / ушедшие с прошлого прогона
  verdicts: {Keep: 1, Update: 1, Consolidate: 1, Replace: 1, Delete: 1}
  closed: {uncovered: 1, missing-path: 1, cited-line: 1, no-canary: 1}
  noise: {no-canary: 1}
  escalated: {stale-area: 1}           # пришли на прогон эскалированными
  carried: 0                           # без вердикта
  age: {median: 3, max: 3}             # дни с first_seen, очередь после прогона
  auto_repaired: 2                     # ссылок, починенных Stop-хуком с прошлого прогона
  reopened: 0                          # снятых вердиктом и вернувшихся с прошлого прогона
```

Приток — ключи очереди, которых не было в снапшоте прошлого блока; отток —
ключи прошлого снапшота и текущей очереди, которых нет после прогона (вердикт
или исчезнувшая находка). Доля авто-ремонта — `auto_repaired / (auto_repaired +
closed)`: ссылки, которые Stop-хук починил сам, против записей, снятых
вердиктом. `report` показывает очередь, метрики последнего
прогона (приток/отток, возраст, доля авто-ремонта, перенесённых без вердикта,
Delete как шум) и:

- тревогу владельцу, если отток меньше притока 2 прогона подряд;
- `demote <class>`: за последние 4 прогона больше 50% закрытых записей класса —
  шум;
- `promote <class>`: класс с наибольшим числом эскалаций за последние 4 прогона.

Рекомендацию применяет владелец или агент правкой шапки очереди:
`classes: {<class>: ignore|block}`. `ignore` — record не ставит находки класса
в очередь (WARN печатается как раньше); `block` — любая запись класса в
тронутой области блокирует Stop (только при blocking). Автоматически шапка не
меняется.

## CI

Мягкий режим. `verify.py --mode=full` понижает до WARN находку, которая стоит
в очереди и не эскалирована; находка вне очереди — FAIL, как раньше. Из самой
очереди красное — переполнение `total` или эскалированная запись
(`gt_pending.ci_problems`: только записи, которые прогон ещё находит, без
классов с `ignore`; STATUS-классы считает verify, классы памяти —
`gt_context.py check`). Отложенный дрейф CI не валит; его показывает один
issue, обновляемый на месте (опционально). Перед merge в main: строка
`python3 tools/ground_truth/gt_pending.py --root . report` в job MR (rc 0, только
печать) показывает очередь ревьюеру; due или эскалация — повод провести разбор
в той же ветке.

GitHub Actions, отдельный workflow:

```yaml
name: ground-truth pending review
on:
  schedule:
    - cron: "0 6 * * 1,4"   # Mon and Thu
  workflow_dispatch:
permissions:
  contents: read
  issues: write
jobs:
  pending-report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install pyyaml
      - name: upsert the queue issue
        env:
          GH_TOKEN: ${{ github.token }}
          TITLE: "ground-truth: pending queue review"
        run: |
          python3 tools/ground_truth/gt_pending.py --root . report --format md > body.md
          num=$(gh issue list --state open --search "in:title \"$TITLE\"" --json number,title \
                --jq ".[] | select(.title == \"$TITLE\") | .number" | head -1)
          if [ -n "$num" ]; then gh issue edit "$num" --body-file body.md
          else gh issue create --title "$TITLE" --body-file body.md; fi
```

GitLab: расписание в CI/CD → Schedules (cron `0 6 * * 1,4`), job в
`.gitlab-ci.yml`; `GITLAB_TOKEN` — masked-переменная с правом `api`
(`CI_JOB_TOKEN` issue не создаёт):

```yaml
pending-review:
  stage: test
  needs: []
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
  script:
    - pip install pyyaml
    - python3 tools/ground_truth/gt_pending.py --root . report --format md > body.md
    - TITLE="ground-truth: pending queue review"
    - iid=$(glab issue list --search "$TITLE" --output json | python3 -c 'import json,sys; print(next((str(i["iid"]) for i in json.load(sys.stdin) if i["title"] == sys.argv[1]), ""))' "$TITLE")
    - if [ -n "$iid" ]; then glab issue update "$iid" --description "$(cat body.md)"; else glab issue create --title "$TITLE" --description "$(cat body.md)"; fi
```

Сам разбор (шаги 1–4) выполняет агент или владелец по issue или триггеру
SessionStart; CI его не запускает.
