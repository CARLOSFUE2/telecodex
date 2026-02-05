# Telecodex Bot

Bot de Telegram para interactuar con Codex desde un chat. Permite ejecutar prompts en Codex, recibir respuestas, y (opcionalmente) ejecutar comandos de shell prefijados con `!`.

## Requisitos
- Python 3.11+ (recomendado)
- Token de bot de Telegram
- CLI `codex` disponible en el PATH (si vas a usar la funcionalidad de Codex)

## Crear el bot en Telegram (BotFather)
Documentación oficial: [Telegram Bots – How do I create a bot?](https://core.telegram.org/bots#how-do-i-create-a-bot)

Resumen de pasos:
1. Abre Telegram y busca `@BotFather`.
2. Inicia el chat y envía `/newbot`.
3. Define el nombre del bot.
4. Define el username del bot (debe terminar en `bot`).
5. BotFather te entregará el token del bot. **Guárdalo y no lo publiques**.

## Instalación
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuración
Crea un archivo `.env` (o exporta variables en tu entorno). Ejemplo en `.env.example`.

Variables soportadas:
- `TELEGRAM_TOKEN` (obligatoria): token del bot de Telegram.
- `ALLOWED_CHAT_IDS` (opcional): lista separada por comas de IDs de chat autorizados. Si se deja vacío, se permiten todos.
- `WORKSPACE` (opcional): ruta donde se ejecutan comandos y `codex exec`.
- `CODEX_TIMEOUT` (opcional): timeout en segundos para `codex exec`. Usa `off`, `0` o `none` para desactivar.

Flujo recomendado de autorización:
1. Ejecuta `/start` para ver tu `chat_id` (este comando siempre responde).
2. Agrega ese `chat_id` a `ALLOWED_CHAT_IDS`.
3. Reinicia el servicio para que tome los cambios.
4. Ejecuta `/open` para iniciar sesión y ver la ayuda completa.

## Ejecución
```bash
python local_bot.py
```

## Comandos del bot
- `/start`: muestra tu `chat_id` e instrucciones para habilitar acceso (no requiere autorización).
- `/open`: inicia sesión y muestra ayuda (requiere estar en `ALLOWED_CHAT_IDS`).
- `/help`: muestra la ayuda (requiere estar en `ALLOWED_CHAT_IDS`).
- `/stop`, `/close`, `/reset`, `/new`: cierra sesión.
- `/status`: estado de la sesión.
- `/timeout <segundos|off>`: configura timeout.
- `/explain <on|off>`: agrega resumen de razonamiento (alto nivel) a las respuestas de Codex.
- `/progress <on|off|segundos>`: habilita/ajusta mensajes de progreso.
- `!<comando>`: ejecuta un comando en el shell (usa `WORKSPACE` como directorio).

## Seguridad
- **Restringe el acceso** usando `ALLOWED_CHAT_IDS` para evitar que terceros ejecuten comandos.
- **No subas** tu `.env` ni claves al repositorio.
- Considera desactivar el acceso a shell (`!`) si no lo necesitas.
- Los chats no autorizados verán un mensaje de “No autorizado” al intentar usar `/open` o cualquier otro comando.

## Notas
- Si `TELEGRAM_TOKEN` no está configurado, el bot falla al iniciar.
- `WORKSPACE` vacío usa el directorio actual.
