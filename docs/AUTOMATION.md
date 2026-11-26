# От релиза Spotatui до принятия обновления

Автоматизация готовит ротацию ebuild для стабильного релиза. После успешного прогона появляется
Draft PR с проверенными файлами и ссылкой на отчёт.
Владелец решает, принимать ли обновление. [Установка](../README.md) · [Диагностика и приёмка](CI_AND_SECURITY.md)

```text
Prepare job (чтение репозитория)             Publish job        Владелец
Discover → Prepare → Validate → Handoff  →  Ветка → Draft PR  → Accept
                                ZIP
```

[Релизный workflow](../.github/workflows/prepare-release.yml) задаёт
расписание, окружение, права заданий и передачу ZIP.
[`release_automation.py`](../scripts/release_automation.py) ищет релиз,
проверяет источники, готовит ebuild и Manifest, формирует и проверяет
handoff. [`publish_release.py`](../scripts/publish_release.py#L325)
применяет проверенный кандидат, контролирует Git diff и записывает ветку
и Draft PR. [Тесты подготовки](../scripts/test_release_automation.py)
используют фикстуры релизов, архивов и Cargo.
[Тесты публикации](../scripts/test_publish_release.py) используют
временные Git-репозитории и ответы API под контролем теста.

## Discover — выбор релиза

Workflow проверяет релизы ежедневно в 05:23 UTC. Ручной запуск доступен
через `workflow_dispatch` с опцией `dry_run`. Оба задания допускаются
в `vovanbl411/spotatui-overlay` с workflow из `main` на той же ревизии,
что и запуск. Перед поиском Prepare сверяет checkout и запускает unit-тесты.

[`discover()`](../scripts/release_automation.py#L937) читает
`releases/latest` проекта `LargeModGames/spotatui`. Он проверяет явные
признаки стабильного релиза, тег вида `vX.Y.Z` и канонический URL.
Текущий ebuild определяется по наибольшей версии и ревизии в checkout.
Для нового релиза скрипт проверяет историю PR по точной ветке
`automation/spotatui-vX.Y.Z`, включая закрытые PR. Если ветка свободна
от этой истории, Discover сохраняет ID релиза и разрешает тег в полный SHA.
У lightweight tag ref указывает прямо на коммит. У annotated tag ref
указывает на объект тега. Скрипт идёт по объектам тегов до коммита.
Неизвестный тип, цикл или неверный SHA останавливают поиск.
[Разрешение тега](../scripts/release_automation.py#L425)

Например, условный оверлей хранит `1.6.0-r1` и `1.5.0`. Для нового
тега `v1.7.0` план добавит `1.7.0`, сохранит точный `1.6.0-r1` для
отката и уберёт `1.5.0`. Если ebuild был один, он остаётся fallback,
а удалять нечего. Больше двух подходящих ebuild требует ручной проверки.
[Правила ротации](../scripts/release_automation.py#L630)

| Исход | Что означает |
| --- | --- |
| `release-available` | Есть новый релиз, известен SHA, построен план. |
| `up-to-date` | Версия релиза не новее текущей в checkout. |
| `already-processed` | В истории есть PR с точным automation head. |
| `ignored-release` | Полученный релиз помечен как draft или prerelease. |

Discover записывает JSON и сводку. Workflow читает выход `outcome`.
Ошибки API или некорректные данные дают неуспешный статус команды.
Dry-run оставляет исход поиска и ставит `dry_run: true`. Он делает GET
для релиза, PR и тега по необходимости. Скачивание архивов, подготовка в
Gentoo, загрузка handoff и публикация пропускаются.

## Prepare — проверка исходников

Prepare получает план и SHA checkout. Для текущего релиза и кандидата
он скачивает канонический архив
`https://github.com/LargeModGames/spotatui/archive/refs/tags/vX.Y.Z.tar.gz`.
Разрешены HTTPS и известные адреса перенаправления GitHub codeload.
Перед распаковкой базового архива сверяются размер, BLAKE2B и SHA512
с принятым Manifest. Поэтому сравнение Cargo опирается на байты уже
упакованного релиза. Повторная выдача архива того же тега с другими
байтами остановит подготовку.

Скрипт ограничивает размер и читает gzip до конца. В tar он проверяет
завершающие нулевые блоки, отсутствие ненулевого хвоста и совпадение SHA
с Git-комментарием из заголовков PAX. Файлы должны лежать внутри
`spotatui-X.Y.Z/`. Допустимы только обычные файлы и каталоги,
без дубликатов, переходов по `..` и ссылок. `Cargo.toml` и `Cargo.lock`
должны быть обычными файлами. Код проверяет связь ответа API и метаданных
канонического архива, полагаясь на GitHub и HTTPS. Он не восстанавливает
Git tree из tar и не проверяет подписи тегов.
[Проверка архива и скачивание](../scripts/release_automation.py#L503)

## Cargo — от функций приложения до distfiles

`Cargo.toml` объявляет версию приложения, требования к Rust, зависимости
и Cargo features. Feature может включить опциональный crate, соседнюю
feature или feature зависимости. Gentoo USE — выбор пользователя на
уровне пакета. Ebuild переводит USE в Cargo features и при необходимости
добавляет системные зависимости. Для `visualizer` это `audio-viz`
и PipeWire. Во фрагменте `src_configure()` показаны два из восьми
преобразований USE в Cargo features.

```bash
local -a myfeatures=( tui streaming )
use cover && myfeatures+=( cover-art )
use visualizer && myfeatures+=( audio-viz )
cargo_src_configure --no-default-features
```

`--no-default-features` отключает upstream-набор по умолчанию, чтобы
сборку определяли базовые `tui`, `streaming` и выбранные USE.
`cargo.eclass` передаёт массив `myfeatures` в Cargo и формирует список
URI crate из `CRATES`. [Ebuild и метаданные](../media-sound/spotatui/)

`Cargo.lock` фиксирует точные версии зависимостей. У crate из crates.io
в нём есть источник и SHA-256 архива. Пример записи без списка зависимостей:

```toml
[[package]]
name = "alsa-sys"
version = "0.3.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "db8fee663d06c4e303404ef5f40488a53e062f89ba8bfed81f42325aafad1527"
```

В `CRATES` этому соответствует `alsa-sys@0.3.1`. Знак `@` разделяет имя и
версию. В имени скачанного файла этот разделитель заменяется дефисом,
получается `alsa-sys-0.3.1.crate`. `DISTDIR` хранит скачанные архивы,
а тонкий Manifest перечисляет нужные distfiles, их размеры, BLAKE2B и
SHA512. Контрольная сумма в Cargo.lock связывает байты с upstream lock. Manifest фиксирует контрольные суммы тех же байтов для Gentoo.

### Когда требуется ручное решение

[`check_source_contract()`](../scripts/release_automation.py#L257) сравнивает
метаданные обоих архивов. `package.version` должен соответствовать ebuild
базы и тегу кандидата. Изменение `rust-version` приложения, лицензии
или объявленного файла лицензии требует ревью. Проверка сравнивает
отображаемые ebuild features и features, достижимые через их зависимости
в старом и новом `Cargo.toml`. Она также сравнивает набор известных
нативных зависимостей с учётом типа зависимости и целевой платформы.
Это проверка изменений в известных местах, а не полный анализ сборки.

Git-зависимости блокируются. При чтении `Cargo.lock` для `CRATES`
разрешается только стандартный реестр crates.io для записей с внешним
`source`. Локальные записи без `source` не создают distfiles.
Изменение лицензий crate, которое не покрывает принятый `LICENSE`,
также требует ручного решения. [Чтение lock и проверка лицензий crate](../scripts/release_automation.py#L373)

**Пример от 27 ноября 2026 года.** При проверке архивов `0.48.0` →
`0.49.0` выяснилось, что из Cargo feature `local-files` исчезло прямое
`dep:symphonia`. Имя feature осталось прежним, но изменился её состав.
Скрипт остановился на `Upstream Cargo features changed` с указанием
`local-files`. Сопровождающему нужно изучить upstream diff, пересмотреть
упаковку и отдельно проверить локальное воспроизведение. Сам факт
появления релиза в Discover этого решения не заменяет.

### Генерация и проверка CRATES

Prepare проверяет версию импортируемого модуля `pycargoebuild` и
принимает ровно `0.16.0`. Для этой версии проверено, что вызов ниже
скачивает registry crate и сверяет их SHA-256 с `Cargo.lock`.
Другая версия требует отдельной проверки поведения.

```bash
pycargoebuild --no-config --no-manifest --distdir "$distdir" \
  --license-mapping "$license_mapping" --input "$input_ebuild" \
  --output "$generated" "$source_dir"
```

`--no-config` исключает пользовательскую конфигурацию утилиты,
`--no-manifest` оставляет генерацию Manifest Portage. `--distdir`
задаёт каталог загрузок. `--license-mapping` указывает на
`metadata/license-mapping.conf` дерева Gentoo. Временный
`--input` основан на текущем ebuild, `--output` принимает созданный
ebuild, а последний аргумент указывает на распакованный кандидат.
Скрипт использует этот ebuild для сверки списка crate и лицензий.
Для конечного ebuild он берёт принятый шаблон и заменяет только `CRATES`.
Он также сверяет исходные хеши `Cargo.toml` и `Cargo.lock` и проверяет,
что `CRATES` упакованной версии совпадает с её lock.
[Вызов генератора](../scripts/release_automation.py#L592) · [Подготовка кандидата](../scripts/release_automation.py#L687)

## Validate — итоговый ebuild и Manifest

Workflow берёт дерево Gentoo из томов закреплённого `gentoo/portage`
и запускает одноразовый `gentoo/stage3`. Checkout смонтирован в
`/overlay` только для чтения, `RUNNER_TEMP` доступен для записи
как `/runner-temp`. Команда
`emerge --oneshot app-portage/pycargoebuild dev-util/pkgcheck`
устанавливает инструменты в контейнер без добавления их в world.
Кандидат, архивы, отчёт и ZIP остаются во временной области runner.
[Шаг подготовки в workflow](../.github/workflows/prepare-release.yml#L76)

Из checkout в кандидат копируются конфигурация оверлея, метаданные
пакета, текущий ebuild как fallback и принятый Manifest. Новый ebuild
создаётся рядом. Старый fallback в эту копию не попадает.
Две команды подставляют пути к временной копии и выполняются из неё.

```bash
DISTDIR="$distdir" ebuild "$candidate_path" manifest
pkgcheck scan --repo "$output" --profile default/linux/amd64/23.0 --exit
```

Первая команда обновляет Manifest для пакета. `DISTDIR` указывает
на архив кандидата и проверенные crate. Перед вызовом Prepare фиксирует
их размеры и хеши. После вызова он сверяет записи Manifest и повторно
считает хеши файлов. Список distfiles должен быть точным объединением
кандидата и оставленного fallback. Строки fallback сохраняют принятые
размеры и хеши. Общий crate записывается один раз и должен совпасть
с обеими проверками. Записи файлов, нужных только удалённой версии,
исчезают из Manifest. [Сверка distfiles](../scripts/release_automation.py#L668)

`pkgcheck scan` проверяет метаданные временного оверлея для указанного
amd64 glibc профиля. `--exit` делает ошибки блокирующими. После сканирования
скрипт сверяет, что байты ebuild и Manifest для передачи не изменились.
`metadata/md5-cache` — производный кэш Portage, который исключён из Git.
Его можно воссоздать из ebuild и eclass. Источником изменений остаются
метаданные пакета, а не копия кэша. Проверки этой стадии не запускают
resolver для Spotatui, компиляцию, установку или runtime.
[Генерация и проверка Manifest](../scripts/release_automation.py#L747) · [Правило исключения кэша](../.gitignore)

## Handoff — три файла и доказательства

Prepare с правами чтения подготавливает данные, а Publish получает
права записи в GitHub. Publish запускает код из доверенного checkout.
ZIP от предыдущего задания служит входом для проверки, его файлы
не исполняются как код публикации.

В ZIP ровно три обычных файла в режиме `0644`.

- `release-handoff.json`.
- `media-sound/spotatui/spotatui-<версия>.ebuild`.
- `media-sound/spotatui/Manifest`.

JSON связывает SHA базы оверлея и upstream-коммита, ID и тег релиза,
размер и хеши архива, SHA-256 обоих Cargo-файлов, результаты
обязательных проверок, план ротации, SHA-256 обоих файлов кандидата,
run ID и attempt. README исключён, потому что пользовательская инструкция
не дублирует текущую версию и не участвует в ротации.

Workflow загружает готовый ZIP как один файл со сроком хранения один день.
Шаг readiness сравнивает SHA-256 локального ZIP с digest, полученным
при upload, проверяет ID и только затем выставляет `publish_ready=true`.
Publish скачивает ZIP по этому ID без распаковки и независимо сверяет
хеш всего файла. Хешей внутри ZIP недостаточно, потому что их можно
заменить вместе с содержимым. Привязку к ожидаемому артефакту даёт
доверенный результат upload из того же Prepare. Проверка также сверяет
run ID, attempt, базу, состав, режимы и хеши файлов, пересчитывает
ротацию и сопоставляет ebuild и Manifest с checkout.
[Создание и проверка handoff](../scripts/release_automation.py#L813) · [Передача в workflow](../.github/workflows/prepare-release.yml#L99)

## Publish — от чистого checkout до Draft PR

1. [`publish()`](../scripts/publish_release.py#L325) допускает только
   `schedule` или `workflow_dispatch` для разрешённого workflow на
   `refs/heads/main`. Требуются выключенный dry-run, успешный Prepare,
   readiness, корректные artifact ID и digest, run ID и attempt.
2. `HEAD` checkout должен равняться `GITHUB_WORKFLOW_SHA` и `GITHUB_SHA`.
   Рабочее дерево, то есть файлы checkout, и индекс должны быть чистыми.
   Индекс хранит содержимое, подготовленное для следующего коммита.
   URL fetch и push у `origin` сверяются с точным HTTPS-адресом оверлея.
   Удалённый ref `refs/heads/main` должен указывать на SHA базы.
3. После проверки ZIP скрипт пишет ebuild и Manifest в рабочее дерево
   и удаляет только старый fallback, если он указан в плане.
   В индекс добавляются только эти пути. Дерево Git описывает
   зафиксированный снимок путей, режимов и содержимого файлов. Publish
   сверяет дерево из индекса с базой и байтами ZIP, затем проверяет,
   что локальный коммит содержит это дерево и имеет ровно одного
   родителя с SHA базы. Пустой или неожиданный diff блокирует публикацию.
4. Скрипт ищет PR по точному head во всех состояниях. В открытых PR в
   `main` проверяет также добавление того же релиза под другой веткой,
   в том числе ebuild с `-rN`. Существующая ветка кандидата блокирует
   создание. Для точного `git ls-remote --exit-code` код `0` означает
   найденный ref, `2` — отсутствие, другие ошибки останавливают работу.
   Перед push `main` проверяется снова.
5. **Первая удалённая запись в репозиторий — push ветки кандидата.**
   Ниже форма команды из кода. `ref` — полное имя ветки кандидата
   `refs/heads/automation/spotatui-vX.Y.Z`, `commit` — его проверенный SHA.

   ```bash
   git push --porcelain --no-follow-tags --recurse-submodules=no \
     --force-with-lease="$ref:" origin "$commit:$ref"
   ```

   Двоеточие с пустым ожидаемым значением требует отсутствия ref в момент
   создания. `--porcelain` даёт машиночитаемую строку результата. Нужны
   успешный exit и `[new branch]` для точной пары SHA и ref. Ответ
   `up-to-date` лишь говорит, что ветка уже указывает на этот коммит.
   Он не подтверждает создание или принадлежность ветки этому запуску.
6. После подтверждённого push скрипт повторяет поиск конфликтующих PR,
   проверяет удалённую `main` и точный SHA новой ветки. Только затем
   отправляет POST с Draft PR в `main` и проверяет ответ GitHub.
   В PR указаны релиз, SHA, ротация, изменения crate, результаты
   проверок, артефакт и ссылка на run. Атомарно защищено только создание
   ветки. Удалённая база и PR проверяются отдельными операциями.
   [Проверки и восстановление](CI_AND_SECURITY.md#диагностика-и-восстановление)

## Accept — ручное решение

Владелец сверяет точный head Draft PR и `main`, diff и доказательства
из указанной попытки Prepare. Затем проводит проверки пакета из
[инструкции по приёмке](CI_AND_SECURITY.md#приёмка-владельцем).
При изменении head проверки должны относиться к новому SHA. Принятое
состояние меняет только ручное слияние.

## Практика на локальной копии

Из корня оверлея на Linux с Git, Bash и Python 3.11 или новее запустите
проверки ниже. Первая команда ищет оба тестовых модуля в `scripts/`.
Тесты используют временные файлы и локальные Git-репозитории, без сети.
Вторая сверяет имена crate из ebuild с записями Manifest и выводит их
количество. Для неё не нужны Gentoo, скачанные архивы или запись в дерево.
Она не проверяет содержимое архивов.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 scripts/release_automation.py check-manifest --root .
```

Чтобы проследить решения по ротации и публикации, запустите четыре
сценария отдельно и посмотрите их assertions. Сначала проверяются ротация и
переход по связанным Cargo features. Затем тесты создают конкурирующий
ref во временном bare-репозитории и имитируют потерю ответа на создание
Draft PR. Ожидается `OK`, при этом конфликтующий ref остаётся на месте,
а потерянный POST не повторяется.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  scripts.test_release_automation.ReleaseAutomationTests.test_rotation_discards_old_fallback_and_keeps_current_revision \
  scripts.test_release_automation.ReleaseAutomationTests.test_transitive_feature_change_stops_preparation \
  scripts.test_publish_release.PublishReleaseTests.test_expected_absent_lease_rejects_branch_race_without_overwriting \
  scripts.test_publish_release.PublishReleaseTests.test_ambiguous_post_reads_back_expected_draft_without_retry
```

Чтобы увидеть план на текущем состоянии upstream, выполните из того же
каталога следующий dry-run. Он использует публичные GET-запросы и зависит
от сети и лимитов API. `env -u GH_TOKEN` убирает токен из окружения
команды. `--root` выбирает дерево для сравнения версий,
`--repository` — репозиторий для проверки истории PR, а
`--result` и `--summary` сохраняют план и сводку только в новой
временной папке. При успехе проверьте `Outcome` и отметку `Dry run`.

```sh
work=$(mktemp -d) &&
env -u GH_TOKEN python3 scripts/release_automation.py discover --root "$PWD" \
  --repository vovanbl411/spotatui-overlay --dry-run \
  --result "$work/release.json" --summary "$work/summary.md" &&
  cat "$work/summary.md"
```

План отражает ebuild выбранного checkout и состояние API на момент
запуска. Для изменений в отборе или ротации начните с тестов Prepare. Для изменений
в допуске, конфликтах или восстановлении начните с тестов Publish.
Перепроверьте команды и диагностические сообщения этого руководства,
если меняете соответствующее поведение.
