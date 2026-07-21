@"
# WARP Helius Tunnel — helper scripts
# Config: $HOME/sentinel-secrets/warp-helius-only.conf
# AmneziaVPN: C:\Program Files\AmneziaVPN\AmneziaVPN.exe

function Show-Helper {
    Write-Host @"
WARP Helius Tunnel — Управление

КОНФИГ:       ~/sentinel-secrets/warp-helius-only.conf
              (ВНЕ git-репозитория, приватный ключ в файле)

ЗАПУСК:
  1. Открой AmneziaVPN (GUI).
  2. Нажми "Добавить соединение" → "Импорт конфигурации из файла"
  3. Выбери ~/sentinel-secrets/warp-helius-only.conf
  4. Подключись к импортированному серверу.

ИЛИ через командную строку (требует доработки --import):
  Import-Config

ПРОВЕРКА:
  curl -s -X POST "https://mainnet.helius-rpc.com/?api-key=33a9f314-..." ^
    -H "Content-Type: application/json" ^
    -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'

Если tunnel работает → ответ содержит "result":"ok".
Выходной сигнал curl — привязан к туннелю на уровне ОС (WARP назначает
роутинг для AllowedIPs в конфиге, все запросы к Cloudflare IP идут через
туннель).

ОСТАНОВ:
  В GUI AmneziaVPN — отключи соединение. Или перезагрузи ОС.

AUTOSTART (планировщик задач):
  schtasks /Create /SC ONLOGON /TN "WARP-Helius" ^
    /TR "\"C:\Program Files\AmneziaVPN\AmneziaVPN.exe\" --connect 1" ^
    /DELAY 0000:30 /F

  После импорта один раз проверь индекс сервера:
  "\"C:\Program Files\AmneziaVPN\AmneziaVPN.exe\" --connect 0"

HEALTH CHECK (отдельно, не внутри Sentinel):
  python scripts/check-helius.py
"@
}

Show-Helper
