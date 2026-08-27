---
name: spaceship
description: Spaceship.com domain registrar API — manage domains, autorenew, check expiration. Use when user asks about domains, DNS, domain renewal.
---

# Spaceship.com — Управление доменами

## Аутентификация

```bash
source ~/.config/spaceship/credentials.env
curl -s -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET" "$SPACESHIP_API_URL/ENDPOINT"
```

## Список доменов
```bash
curl -s "$SPACESHIP_API_URL/domains?take=20&skip=0" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET"
```

## Информация о домене
```bash
curl -s "$SPACESHIP_API_URL/domains/example.com" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET"
```

## Включить/выключить автопродление
```bash
curl -s -X PUT "$SPACESHIP_API_URL/domains/DOMAIN/autorenew" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"isEnabled": true}'
```

## DNS записи

API DNS **РАБОТАЕТ** даже с "basic" NS provider. Метод: `PUT /dns/records/DOMAIN`.

### Список записей
```bash
curl -s "$SPACESHIP_API_URL/dns/records/DOMAIN?take=50&skip=0" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET"
```

### Добавить запись (PUT, не POST!)
Каждый тип имеет свои обязательные поля:

```bash
# A record
curl -s -X PUT "$SPACESHIP_API_URL/dns/records/DOMAIN" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"type":"A","name":"@","address":"1.2.3.4","ttl":3600}]}'

# MX record — поле "exchange", не "address"!
curl -s -X PUT ... \
  -d '{"items":[{"type":"MX","name":"@","exchange":"mx1.example.com","priority":10,"ttl":3600}]}'

# TXT record — поле "value"
curl -s -X PUT ... \
  -d '{"items":[{"type":"TXT","name":"@","value":"v=spf1 ...","ttl":3600}]}'

# CNAME record — поле "cname", не "address"!
curl -s -X PUT ... \
  -d '{"items":[{"type":"CNAME","name":"sub","cname":"target.example.com","ttl":3600}]}'
```

**Важно:** PUT возвращает `204 No Content` при успехе (пустое тело).

⚠️ **PUT ДОБАВЛЯЕТ запись, не заменяет.** Чтобы СМЕНИТЬ значение существующей записи (напр. A с одного IP на другой) — сначала `DELETE` старой, потом `PUT` новой. Иначе получишь ДВЕ записи (round-robin между старым и новым адресом). Проверено 14 June 2026 на смене example.com A.

### Удалить запись
```bash
# ⚠️ DELETE body = ГОЛЫЙ МАССИВ, НЕ {"items":[...]} (иначе 422 "should be array")
curl -s -X DELETE "$SPACESHIP_API_URL/dns/records/DOMAIN" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '[{"type":"A","name":"@","address":"1.2.3.4"}]'
```

### Список записей (GET — требует `take` И `skip`)
```bash
# ⚠️ без skip → 422 "The skip field is required"
curl -s "$SPACESHIP_API_URL/dns/records/DOMAIN?take=100&skip=0" \
  -H "X-Api-Key: $SPACESHIP_API_KEY" -H "X-Api-Secret: $SPACESHIP_API_SECRET" | jq '.items[]'
```

### Поля по типу записи
| Тип | Обязательные поля |
|-----|-------------------|
| A | name, address, ttl |
| AAAA | name, address, ttl |
| CNAME | name, **cname**, ttl |
| MX | name, **exchange**, priority, ttl |
| TXT | name, **value**, ttl |

