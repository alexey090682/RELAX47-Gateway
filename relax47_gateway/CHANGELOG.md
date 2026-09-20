# 7.14.9

- Публикация Gateway как отдельного Home Assistant repository.
- Существующие инструменты LAN, роутера, HA, Tuya, OAuth/MCP и SSH сохранены.
- SQL status/inspect/install для единственной RELAX47 БД; SHA-256 и SQLite checks.
- SQL install требует согласованного обслуживания и остановки внешних writers;
  управляет остановкой/запуском Core через Supervisor после проверенного HA backup.
- SQLite backup transaction вместо замены inode; backup/rollback, межпроцессная блокировка.
- Generic config upload не применяет базы в обход SQL-проверок.
- SQL и backup cleanup доступны только административным MCP-токенам.
- Очистка backup: dry-run по умолчанию, минимум три последних и protected сохранены.
- Dockerfile включает sql_migration.py. Кэши Python исключены.
- Установка из репозитория не использует старое самообновление local_relax47_gateway.

Совместимость схемы SQL: 1–9. Полная миграция бизнес-данных и боевой запуск в этот
релиз публикации не входят. Инструкции перехода и отката — в корневом README.
