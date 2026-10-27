# module-template: минимальный модуль для заявки

[English version](README.md)

Здесь лежит минимальный модуль MikoPBX, который проходит все проверки module-applications. Скопируйте его в **свой**
репозиторий и переименуйте `ModuleExample` в идентификатор своего модуля. Исходники остаются у вас, а заявка ссылается
на конкретный коммит. Пример побогаче (веб-страницы, модели, AMI-воркер) — в
[mikopbx/ModuleTemplate](https://github.com/mikopbx/ModuleTemplate).

## Что обязательно для MikoPBX, а что рекомендация

Полный список требований с идентификаторами — [rules/rules.yaml](../rules/rules.yaml) (версия правил `0.3.0`), формат
заявки — [schema/application.schema.json](../schema/application.schema.json) (версия схемы 1). Обязательное для самого
модуля:

| Файл | Правило | Что |
|---|---|---|
| `module.json` в корне репозитория | PKG-001, MJ-001..005 | `moduleUniqueID` = идентификатор модуля (`Module` + буквы и цифры), `version` вида `1.2.3`, `min_pbx_version` вида `2024.1.0` (не ниже 2020.2.468); `lic_product_id`/`lic_feature_id` нет или 0 у бесплатного модуля, целые идентификаторы MIKO у платного |
| `Setup/PbxExtensionSetup.php` | PKG-003, CODE-001 | `namespace Modules\<id>\Setup; class PbxExtensionSetup` (проще всего — наследовать `PbxExtensionSetupBase`) |
| `Lib/<X>Conf.php` (если нужны хуки, воркеры или API) | CODE-003 | `X` — идентификатор без `Module`; `extends ConfigClass`; загружается на текущем Core |
| каждый PHP-файл | CODE-002, LIFE-003 | namespace `Modules\<id>\...` по пути файла; `php -l` проходит; нет fatal error |
| модуль целиком | LIFE-001..005 | ставится, включается, отключается и удаляется через REST API; после этого MikoPBX работает |

Рекомендуется: `support_email` и `developer` в module.json (MJ-006), `uninstallModule()` (CODE-004),
`Messages/en.php` и `ru.php`, `SECURITY.md` с адресом из `support_email`, уникальные имена в `agi-bin` (CODE-005),
никаких `db/*.db` и `.env` в репозитории (PKG-005).

## Собрать ZIP из коммита

Проверки не берут загруженный вами ZIP: они собирают его сами из коммита, указанного в заявке. Посмотреть, что именно
соберётся:

```sh
git clone https://github.com/<mikopbx-org>/module-applications && cd module-applications
pip install -r tools/requirements.txt
tools/package https://github.com/<вы>/ModuleExample <полный SHA коммита, 40 символов> ModuleExample.zip
```

Команда печатает sha256 архива. Один и тот же коммит всегда даёт один и тот же ZIP: записи отсортированы, дата — время
коммита, `%ModuleVersion%` в module.json заменяется на `0.0.0-<коммит>`. Файлы с `export-ignore` в `.gitattributes`
в архив не попадают.

## Те же проверки, что в CI

Заполните `applications/<id>/application.yaml` по [templates/application.yaml](../templates/application.yaml) и запустите:

```sh
tools/validate.py --online <id>      # правила: формат, module.json на коммите, структура (код не исполняется)
tools/check <id>                     # модуль в свежем контейнере MikoPBX (нужен Docker)
```

`tools/check` скачивает образ MikoPBX из [config/mikopbx.yaml](../config/mikopbx.yaml) (по digest) и запускает его в
docker-сети без выхода наружу. Затем ставит ZIP через REST API, включает модуль, ищет его воркеры и API `check`,
отключает и удаляет модуль, просматривает логи и проверяет, что MikoPBX отвечает. Результат печатается в JSON, код
выхода 0 — все шаги пройдены. Для проверки обновления добавьте `--previous-commit <одобренный коммит>`.
На Linux команда добавляет правило iptables, чтобы модуль не достучался до вашей машины; для этого нужен root или sudo.
Запускайте на машине, которую не жалко: код модуля исполняется в контейнере вашего Docker.

В CI те же две команды выполняются на GitHub runners x86_64 и arm64 — для каждой архитектуры, заявленной в заявке.
