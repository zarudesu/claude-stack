# Установка механизмов после исследования

Установка не создаёт истинные знания и не завершает init. Сначала согласовать
scope; сохранить локальные доработки. Перед записью смотреть facts, dry-run и
существующие settings/CI. Ни commits, ни внешние workflow installer не запускает.

## Обычный repo с STATUS

Создать run.json вне автоматически загружаемых документов проекта — ровно шесть
строковых полей (пример pipeline/run.example.json):

```json
{
  "repo": "/absolute/path/to/repository",
  "research": "/absolute/path/to/research",
  "scratch": "/absolute/path/to/scratch",
  "python": "/absolute/path/to/python3",
  "skill": "/absolute/path/to/ground-truth",
  "date": "YYYY-MM-DD"
}
```

```sh
python3 <skill>/pipeline/gt.py i9_install --run run.json --facts
python3 <skill>/pipeline/gt.py i9_install --run run.json --dry-run
python3 <skill>/pipeline/gt.py i9_install --run run.json --write
```

Применять --write только после оценки конкретного diff/области. Installer копирует
tools (включая gt_context.py), hooks и короткое правило; мержит settings/CI/
pre-commit, может менять gitignore и добавлять bridge tests. Это не просто cp.
Локально изменённый шаблон может быть заменён: сравнить до записи, перенести
нужную адаптацию, затем проверить. Повторный запуск одинаковых assets идемпотентен.

Для Go bridge: --pkg <directory>; JS: --js-test-dir <directory>; Maven:
--java-pkg <dotted.package> при необходимости. --ci-file выбирает существующий
workflow при неоднозначности. Точная механика legacy-стадии — раздел I.9
[pipeline.md](pipeline.md); читать его только при нужде, не весь старый конвейер.

## Что адаптировать обязательно

CI — пример, не готовое окружение. Не оставлять ссылку на несуществующий
requirements-dev.txt: выбрать реально используемую установку dependencies и
toolchains. Полная база PR/изменения нужна session guard. Memory check запускать
отдельно от tests/claim mutations; ошибка/пропуск не дают зелёный gate.
Прочитать [hooks.md](hooks.md) для политики, timeout и изоляции мутаций.

В экосистеме: одна общая модель, relative workspace pointers из repo, точные
версии всех checkout в CI, межрепозиторные проверки. Шаблон не угадывает
repository IDs, credentials или совместимые ревизии. Недостающую authority
запросить, а локальную подготовку честно оставить PARTIAL.

В memory-only repo не устанавливать неработающие STATUS bridge/CI:
адресно установить gt_context.py, rule и hooks из templates; адаптировать
settings через сохранение существующих массивов; отдельный job запускает
gt_context.py check. Нужны Python 3.10+, PyYAML и git. Обычные тесты проекта
сохраняются. Это подготовка memory-only, не эквивалент доказанных STATUS claims.

Если .claude/settings.local.json игнорируется, описание само по себе не
контролирует его drift: проверить фактический dispatch и выбрать безопасные
явные sources/task-local tooling receipt или effective-contract probe по
[project-memory.md](project-memory.md). Никогда не включать секреты.
Для других хостов установить их маленький вход (например AGENTS.md), который
требует start/context/check; наличие .claude/rules не означает чтение всеми.

После установки заполнить/обновить model, отнести новые assets к областям,
провести смысловое ревью и [полную приёмку](init.md). Не писать receipts до
финализации этих входов. Обновляя legacy repo, сначала подготовить переход:
новый CI memory gate закономерно красный без model/receipts.
