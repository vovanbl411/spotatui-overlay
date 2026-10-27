# spotatui Gentoo overlay

Overlay устанавливает терминальный Spotify-клиент `spotatui` версии 0.47.0.
Сборка включает TUI и воспроизведение Spotify через librespot. Дополнительные
возможности включаются независимо USE-флагами и по умолчанию выключены. Cargo
self-update, telemetry, Discord RPC, scripting, AI DJ/MCP, GUI и Soloist не
включены.

## USE-флаги

| USE-флаг | Cargo feature | Возможность | Внешнее требование |
| --- | --- | --- | --- |
| `cover` | `cover-art` | Обложки в TUI | Нет |
| `internet-radio` | `internet-radio` | Интернет-радио | Сетевой доступ к радиостанции |
| `local-files` | `local-files` | Локальные аудиофайлы | Поддерживаемые аудиофайлы |
| `mpris` | `mpris` | Управление через MPRIS | Сеансовая шина D-Bus |
| `qobuz` | `qobuz` | Источник Qobuz | Доступ к Qobuz и учётная запись |
| `subsonic` | `subsonic` | Subsonic-совместимые серверы | Доступный сервер и учётная запись |
| `visualizer` | `audio-viz` | Аудиовизуализатор | PipeWire и запущенный сервис PipeWire |
| `youtube` | `youtube` | Поиск и воспроизведение YouTube | `yt-dlp` (устанавливается зависимостью пакета) |

Пример `/etc/portage/package.use/spotatui` для обложек и MPRIS:

```text
media-sound/spotatui cover mpris
```

Spotify и локальные аудиоисточники используют разные части программы.
Воспроизведение Spotify по-прежнему собрано с ALSA. Флаг `visualizer`
подключает PipeWire только для визуализатора и не меняет аудиобэкенд Spotify.
Для YouTube пакет устанавливает `yt-dlp`; `ffmpeg` может быть полезен для
обработки аудио, но не является обязательной зависимостью.

## Подготовка обновлений

Workflow `Prepare spotatui release` ежедневно проверяет stable-релизы
`LargeModGames/spotatui`. Для ручного запуска откройте Actions, выберите этот
workflow и включите `dry_run`. В сводке появится план без скачивания исходников
и создания ветки, commit или PR. Workflow запускается в 05:23 UTC.

Для нового релиза workflow проверяет Cargo features, лицензии crate и системные
интеграции, обновляет `CRATES` по `Cargo.lock`, создаёт Manifest и запускает
`pkgcheck`.
Для совместимого обновления workflow создаёт отдельную ветку и Draft PR. USE
mapping, зависимости Gentoo, backend и patches автоматически не меняются.
Изменения, требующие решения сопровождающего, останавливают подготовку.

Workflow проверяет Python-логику и метаданные пакета, но не подтверждает сборку
клиента или его работу. Перед принятием PR проверьте diff, запустите
`emerge -pv media-sound/spotatui`, соберите пакет с нужными USE-флагами и
проверьте его работу. Push и создание PR через встроенный `GITHUB_TOKEN` не
запускают следующий workflow, поэтому подготовительный job сам выполняет
необходимые проверки.

Для создания Draft PR в настройках репозитория GitHub Actions должен быть
разрешён доступ к созданию pull request. Ветка автоматически не сливается.

## Подключение

Создайте `/etc/portage/repos.conf/spotatui-overlay.conf`:

```ini
[spotatui-overlay]
location = /var/db/repos/spotatui-overlay
sync-type = git
sync-uri = https://github.com/vovanbl411/spotatui-overlay.git
auto-sync = yes
```

## Установка

Затем установите пакет:

```sh
emerge --ask media-sound/spotatui
```

Для воспроизведения Spotify требуется Spotify Premium. На Linux используется
штатный ALSA backend; `media-libs/alsa-lib` нужен для сборки и запуска.
Профили musl не поддерживаются: upstream выбирает для них `rodio`.

## Проверка

Для исходного минимального пакета пользователь подтвердил успешную сборку и
воспроизведение Spotify на Gentoo. Это подтверждение относится к сборке без
новых USE-флагов и не распространяется на дополнительные возможности.

Для `0.47.0-r1` прошли `bash -n`, проверка XML и `pkgcheck` на профиле
amd64 glibc. Release-сборки Cargo с новыми флагами выключенными и со всеми
включёнными прошли с `--offline`; все 815 crate-записей из `CRATES` найдены
в Manifest. Прямой compile-фазе Portage помешал `sccache` с ошибкой
`Operation not permitted`; Cargo-сборки запускались из подготовленного
Portage vendor-каталога без сети. Новые возможности в runtime не проверялись.
