import json
import os
import re
import selectors
import subprocess
import threading
import time

import telebot
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN no está configurado en las variables de entorno")

WORKSPACE = os.getenv("WORKSPACE", "")

allowed_ids_raw = os.getenv("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = {
    int(x)
    for x in allowed_ids_raw.split(",")
    if x.strip().isdigit()
}

bot = telebot.TeleBot(TOKEN)

_session_lock = threading.Lock()
def _default_timeout() -> int | None:
    raw_env = os.getenv("CODEX_TIMEOUT")
    if raw_env is None:
        return None
    raw = raw_env.strip()
    if raw.lower() in {"0", "off", "none"}:
        return None
    try:
        return int(raw)
    except ValueError:
        return 900


_session_state = {
    "active": True,
    "has_session": False,
    "session_id": None,
    "timeout_sec": _default_timeout(),
    "explain": False,
    "progress_enabled": True,
    "progress_interval_sec": 10,
}

_ansi_re = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_session_id_re = re.compile(r"session id:\s*([0-9a-f-]+)", re.IGNORECASE)
_progress_prefix = "💭 pensando..."


def _strip_ansi(text: str) -> str:
    return _ansi_re.sub("", text)

def _extract_session_id(text: str) -> str | None:
    match = _session_id_re.search(text)
    if not match:
        return None
    return match.group(1)


def _is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def _run_shell(command: str) -> str:
    try:
        result = subprocess.run(
            ["/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if not output.strip():
            output = f"✓ Comando ejecutado (exit code: {result.returncode})"
        elif result.returncode != 0:
            output = f"⚠️ Exit code {result.returncode}\n\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return "❌ Timeout: El comando tardó demasiado"
    except Exception as exc:
        return f"❌ Error: {exc}"


def _run_codex_exec(
    prompt: str,
    session_id: str | None,
    timeout_sec: int | None,
    chat_id: int,
    progress_enabled: bool,
    progress_interval_sec: int,
) -> tuple[str, str | None]:
    if session_id:
        cmd = ["codex", "exec", "--json", "resume", session_id, prompt]
    else:
        cmd = ["codex", "exec", "--json", prompt]

    stop_event = threading.Event()
    last_progress_time = 0.0
    last_summary = None

    def _maybe_send_progress(message: str) -> None:
        nonlocal last_progress_time
        if not progress_enabled or progress_interval_sec <= 0:
            return
        now = time.time()
        if now - last_progress_time < progress_interval_sec:
            return
        bot.send_message(chat_id, message)
        last_progress_time = now

    def _progress_pinger() -> None:
        if stop_event.wait(progress_interval_sec):
            return
        _maybe_send_progress(_progress_prefix)
        while not stop_event.wait(progress_interval_sec):
            _maybe_send_progress(_progress_prefix)

    def _summarize_event(evt: dict) -> str | None:
        evt_type = (evt.get("type") or "").lower()
        if evt_type in {"turn.started"}:
            return "procesando solicitud"
        if evt_type in {"turn.completed"}:
            return "finalizando respuesta"

        if evt_type in {"item.started", "item.completed"}:
            item = evt.get("item") or {}
            item_type = (item.get("type") or "").lower()
            if item_type in {"reasoning", "agent_message"}:
                return None
            if item_type == "command_execution":
                cmd_text = item.get("command") or ""
                cmd_text = cmd_text.strip()
                if cmd_text:
                    return f"ejecutando comando: {cmd_text[:160]}"
                return "ejecutando comando"
            if item_type in {"file_change", "file_edit", "file_write"}:
                path = item.get("path") or item.get("file") or ""
                path = path.strip()
                if path:
                    return f"modificando archivo: {path}"
                return "modificando archivos"
            if item_type in {"mcp_tool_call"}:
                return "llamando herramienta MCP"
            if item_type in {"web_search"}:
                return "buscando en la web"
            if item_type in {"plan"}:
                return "actualizando plan"
        return None

    try:
        pinger_thread = None
        if progress_enabled and progress_interval_sec > 0:
            pinger_thread = threading.Thread(target=_progress_pinger, daemon=True)
            pinger_thread.start()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORKSPACE,
            env={**os.environ, "TERM": "dumb"},
        )

        selector = selectors.DefaultSelector()
        if proc.stdout:
            selector.register(proc.stdout, selectors.EVENT_READ)
        if proc.stderr:
            selector.register(proc.stderr, selectors.EVENT_READ)

        stderr_lines: list[str] = []
        final_message: str | None = None
        new_session_id = session_id
        start_time = time.time()
        timeout = None if not timeout_sec or timeout_sec <= 0 else timeout_sec

        while True:
            if timeout is not None and (time.time() - start_time) > timeout:
                proc.kill()
                stop_event.set()
                return "❌ Timeout: El comando tardó demasiado", new_session_id

            if proc.poll() is not None and not selector.get_map():
                break

            events = selector.select(timeout=0.5)
            if not events:
                continue

            for key, _ in events:
                line = key.fileobj.readline()
                if line == "":
                    selector.unregister(key.fileobj)
                    continue

                if key.fileobj is proc.stderr:
                    stderr_lines.append(line)
                    if len(stderr_lines) > 200:
                        stderr_lines = stderr_lines[-200:]
                    continue

                raw_line = line.strip()
                if not raw_line:
                    continue
                try:
                    evt = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if evt.get("type") == "thread.started":
                    thread_id = evt.get("thread_id")
                    if thread_id:
                        new_session_id = thread_id

                if evt.get("type") == "item.completed":
                    item = evt.get("item") or {}
                    if (item.get("type") or "").lower() == "agent_message":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            final_message = text.strip()

                summary = _summarize_event(evt)
                if summary and summary != last_summary:
                    last_summary = summary
                    _maybe_send_progress(f"{_progress_prefix} {summary}")

        stop_event.set()
        if pinger_thread:
            pinger_thread.join(timeout=1.0)

        returncode = proc.returncode or 0
        if returncode != 0:
            stderr_text = _strip_ansi("".join(stderr_lines)).strip()
            if not stderr_text:
                stderr_text = "Error sin detalle en stderr."
            return f"⚠️ Exit code {returncode}\n\n{stderr_text}", new_session_id

        if final_message:
            return final_message, new_session_id

        stderr_text = _strip_ansi("".join(stderr_lines)).strip()
        if stderr_text:
            return stderr_text, new_session_id
        return "✓ Comando ejecutado", new_session_id
    except Exception as exc:
        stop_event.set()
        return f"❌ Error: {exc}", session_id


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:3900] + "\n\n... (truncado)"

def _start_text(chat_id: int) -> str:
    return (
        "✓ Inicio\n\n"
        f"Tu chat_id es: {chat_id}\n"
        "Agrega este chat_id en ALLOWED_CHAT_IDS.\n"
        "Luego reinicia el servicio para que tome los valores.\n\n"
        "Cuando estés habilitado, ejecuta /open para iniciar sesión.\n"
    )

def _help_text() -> str:
    return (
        "✓ Sesión abierta\n\n"
        "Comandos disponibles:\n"
        "- /start o /open: abre sesión y muestra esta ayuda.\n"
        "- /help: muestra esta ayuda.\n"
        "- /stop, /close, /reset, /new: cierra sesión.\n"
        "- /status: estado de la sesión.\n"
        "- /timeout <segundos|off>: configura timeout.\n"
        "- /explain <on|off>: agrega resumen de razonamiento (alto nivel).\n"
        "- /progress <on|off|segundos>: mensajes de progreso.\n"
        "- !<comando>: ejecuta un comando en el shell (usa WORKSPACE).\n\n"
        "Importante:\n"
        "- Si ALLOWED_CHAT_IDS está vacío, se permiten todos los chats.\n"
        "- No compartas tu token ni tu .env.\n"
    )


@bot.message_handler(func=lambda message: True)
def run_command(message):
    if not message.text:
        return

    text = message.text.strip()
    if not text:
        return

    if text in {"/start"}:
        with _session_lock:
            output = _start_text(message.chat.id)
        bot.reply_to(message, _truncate(output))
        return

    if text in {"/open", "/help"} and not _is_allowed(message.chat.id):
        bot.reply_to(message, "No autorizado. Solicita acceso al administrador.")
        return

    if not _is_allowed(message.chat.id):
        bot.reply_to(message, "No autorizado. Solicita acceso al administrador.")
        return

    with _session_lock:
        if text in {"/open", "/help"}:
            _session_state["active"] = True
            output = _help_text()
        elif text in {"/stop", "/close", "/reset", "/new"}:
            _session_state["active"] = False
            _session_state["has_session"] = False
            _session_state["session_id"] = None
            output = "✓ Sesión cerrada"
        elif text in {"/status"}:
            if _session_state["active"] and _session_state["has_session"]:
                output = "✓ Sesión activa"
            elif _session_state["active"]:
                output = "• Sesión activa (sin contexto previo)"
            else:
                output = "• Sesión inactiva"
        elif text.startswith("/timeout"):
            parts = text.split()
            if len(parts) == 1:
                current = _session_state["timeout_sec"]
                if current is None:
                    output = "• Timeout desactivado"
                else:
                    output = f"• Timeout actual: {current}s"
            else:
                value = parts[1].strip().lower()
                if value in {"0", "off", "none"}:
                    _session_state["timeout_sec"] = None
                    output = "✓ Timeout desactivado"
                else:
                    try:
                        _session_state["timeout_sec"] = int(value)
                        output = f"✓ Timeout actualizado: {_session_state['timeout_sec']}s"
                    except ValueError:
                        output = "⚠️ Uso: /timeout <segundos|off>"
        elif text.startswith("/explain"):
            parts = text.split()
            if len(parts) == 1:
                output = "• explain está " + ("ON" if _session_state["explain"] else "OFF")
            else:
                value = parts[1].strip().lower()
                if value in {"on", "1", "true", "si", "sí"}:
                    _session_state["explain"] = True
                    output = "✓ explain ON"
                elif value in {"off", "0", "false", "no"}:
                    _session_state["explain"] = False
                    output = "✓ explain OFF"
                else:
                    output = "⚠️ Uso: /explain <on|off>"
        elif text.startswith("/progress"):
            parts = text.split()
            if len(parts) == 1:
                status = "ON" if _session_state["progress_enabled"] else "OFF"
                output = (
                    f"• progress {status} (cada {_session_state['progress_interval_sec']}s)"
                )
            else:
                value = parts[1].strip().lower()
                if value in {"on", "1", "true", "si", "sí"}:
                    _session_state["progress_enabled"] = True
                    output = "✓ progress ON"
                elif value in {"off", "0", "false", "no"}:
                    _session_state["progress_enabled"] = False
                    output = "✓ progress OFF"
                else:
                    try:
                        _session_state["progress_interval_sec"] = int(value)
                        output = (
                            "✓ progress intervalo "
                            f"{_session_state['progress_interval_sec']}s"
                        )
                    except ValueError:
                        output = "⚠️ Uso: /progress <on|off|segundos>"
        elif text.startswith("!"):
            command = text[1:].strip()
            output = _run_shell(command)
        else:
            session_id = _session_state["session_id"] if _session_state["active"] else None
            prompt = text
            if _session_state["explain"]:
                prompt = (
                    f"{prompt}\n\n"
                    "Incluye un resumen breve de razonamiento en 3 bullets (alto nivel, sin pasos detallados)."
                )
            output, session_id = _run_codex_exec(
                prompt,
                session_id=session_id,
                timeout_sec=_session_state["timeout_sec"],
                chat_id=message.chat.id,
                progress_enabled=_session_state["progress_enabled"],
                progress_interval_sec=_session_state["progress_interval_sec"],
            )
            _session_state["active"] = True
            _session_state["has_session"] = session_id is not None
            _session_state["session_id"] = session_id

    bot.reply_to(message, _truncate(output))


bot.infinity_polling()
