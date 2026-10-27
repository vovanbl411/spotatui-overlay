# spotatui Gentoo overlay

Overlay устанавливает терминальный Spotify-клиент `spotatui` версии 0.47.0.
Сборка включает TUI и воспроизведение Spotify через librespot. Cargo
self-update, telemetry, Discord RPC, scripting, AI DJ/MCP, GUI и дополнительные
источники музыки не включены.

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

`pkgcheck` проходит на amd64 glibc профиле. Portage успешно выполнил фазы
распаковки, настройки, сборки и установки во временный каталог образа
Portage; пакет не объединялся с системой. Собранный бинарник запускается с `--help`.
Интерактивный TUI, авторизация Spotify и воспроизведение не проверялись.
