import asyncio
import os
import sys
import subprocess
import time
import psutil
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= Configuration Management =================
BASE_DIR = r"C:\Users\Administrator\Desktop\algo-trading"
ENV_FILE = os.path.join(BASE_DIR, "env.txt")
TARGET_SCRIPT = os.path.join(BASE_DIR, "main.py")
STOCK_SCREENER_SCRIPT = os.path.join(BASE_DIR, "stock-screener.py")
BASE_DIR2 = r"C:\Users\Administrator\Desktop\algo-trading\logs"
LOG_FILE = os.path.join(BASE_DIR2, "algo.log")
PID_FILE = os.path.join(BASE_DIR2, "algo.pid")
STOP_FILE = os.path.join(BASE_DIR2, "algo.stop")  # overridden after env load

def load_env_config(filepath):
    """Parses env.txt line-by-line, ignoring comments and stripping quotes."""
    config = {}
    if not os.path.exists(filepath):
        print(f"CRITICAL ERROR: {filepath} not found. Please create it.")
        sys.exit(1)
        
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
                
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                
                # Strip leading and trailing quotes if present
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                    
                config[key] = val
    return config

# Initialize variables securely from disk
env_vars = load_env_config(ENV_FILE)

BOT_TOKEN = env_vars.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_vars.get("TELEGRAM_CHAT_ID")

# v60: /stop file must match MainEngine(config.log_dir)/algo.stop
_log_dir = (env_vars.get("LOG_DIR") or "").strip()
if _log_dir:
    _log_path = _log_dir if os.path.isabs(_log_dir) else os.path.join(BASE_DIR, _log_dir)
    STOP_FILE = os.path.join(_log_path, "algo.stop")
    LOG_FILE = os.path.join(_log_path, "algo.log")
    PID_FILE = os.path.join(_log_path, "algo.pid")
    BASE_DIR2 = _log_path
else:
    STOP_FILE = os.path.join(BASE_DIR2, "algo.stop")

try:
    ALLOWED_USER_ID = int(env_vars.get("ALLOWED_USER_ID", 0))
except ValueError:
    print("CRITICAL ERROR: ALLOWED_USER_ID in env.txt must be a valid number.")
    sys.exit(1)

if not BOT_TOKEN or not ALLOWED_USER_ID:
    print("CRITICAL ERROR: TELEGRAM_BOT_TOKEN or ALLOWED_USER_ID missing in env.txt.")
    sys.exit(1)
# ==========================================================

def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID

def get_active_process():
    """Checks if the script process recorded in algo.pid is still running."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        proc = psutil.Process(pid)
        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
            return proc
    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    help_text = (
        "🤖 *Algo Trading Remote Controller*\n\n"
        "*Configuration Management:*\n"
        "• `/get` - View all variables in `env.txt`\n"
        "• `/get KEY` - View value of a specific key\n"
        "• `/set VALUE` - Update UPSTOX_ACCESS_TOKEN\n\n"
        "*Process Execution:*\n"
        "• `/run` - Launch `main.py` in the background\n"
        "• `/stop` - Graceful stop (flatten live book, then exit)\n"
        "• `/status` - Check process state and recent output\n"
        "• `/logs [N]` - View last N lines of output (default: 20)\n\n"
        "*Screeners:*\n"
        "• `/stock` - Trigger `stock-screener.py`"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# ---------------- File Operations ----------------

async def get_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not os.path.exists(ENV_FILE):
        await update.message.reply_text("⚠️ `env.txt` does not exist.", parse_mode="Markdown")
        return

    args = context.args
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if args:
        target_key = args[0].strip()
        for line in lines:
            if line.strip().startswith("#") or "=" not in line:
                continue
            key, val = line.strip().split("=", 1)
            if key.strip() == target_key:
                await update.message.reply_text(f"`{key.strip()}={val.strip()}`", parse_mode="Markdown")
                return
        await update.message.reply_text(f"Key `{target_key}` not found.", parse_mode="Markdown")
    else:
        content = "".join(lines).strip()
        if not content:
            await update.message.reply_text("`env.txt` is empty.", parse_mode="Markdown")
            return
        if len(content) > 3500:
            content = content[:3500] + "\n... [Truncated]"
        await update.message.reply_text(f"```text\n{content}\n```", parse_mode="Markdown")


async def set_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    # Check if a value was actually provided
    if not context.args:
        await update.message.reply_text("Usage: `/set VALUE`\nExample: `/set eyJhbGciOiJIUzI1Ni...`", parse_mode="Markdown")
        return

    # Hardcode the key and take the entire input as the value
    key_to_set = "UPSTOX_ACCESS_TOKEN"
    value_to_set = " ".join(context.args).strip()
    new_entry = f"{key_to_set}={value_to_set}\n"

    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    key_found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            if k.strip() == key_to_set:
                new_lines.append(new_entry)
                key_found = True
                continue
        new_lines.append(line)

    if not key_found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(new_entry)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    await update.message.reply_text(f"✅ Updated:\n`{key_to_set}={value_to_set}`", parse_mode="Markdown")


# --------------- Process Management ---------------

async def run_algo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    proc = get_active_process()
    if proc:
        await update.message.reply_text(f"⚠️ `main.py` is already running (PID: {proc.pid}).", parse_mode="Markdown")
        return

    if not os.path.exists(TARGET_SCRIPT):
        await update.message.reply_text(f"❌ Script not found: `{TARGET_SCRIPT}`", parse_mode="Markdown")
        return

    try:
        log_handle = open(LOG_FILE, "a", encoding="utf-8")
        
        process = subprocess.Popen(
            [sys.executable, "-u", TARGET_SCRIPT],
            cwd=BASE_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )

        with open(PID_FILE, "w") as f:
            f.write(str(process.pid))

        await update.message.reply_text(f"🚀 Started `main.py`\n• **PID:** `{process.pid}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Execution failed:\n`{str(e)}`", parse_mode="Markdown")


async def run_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers the stock-screener.py script"""
    if not is_authorized(update):
        return

    if not os.path.exists(STOCK_SCREENER_SCRIPT):
        await update.message.reply_text(f"❌ Script not found: `{STOCK_SCREENER_SCRIPT}`", parse_mode="Markdown")
        return

    try:
        process = subprocess.Popen(
            [sys.executable, "-u", STOCK_SCREENER_SCRIPT],
            cwd=BASE_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )

        await update.message.reply_text(f"📈 Triggered `stock-screener.py` successfully!\n• **PID:** `{process.pid}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Execution failed:\n`{str(e)}`", parse_mode="Markdown")


async def stop_algo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v58: request graceful flatten+exit; only force-kill after timeout."""
    if not is_authorized(update):
        return

    proc = get_active_process()
    if not proc:
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass
        await update.message.reply_text("ℹ️ No active `main.py` process found.")
        return

    try:
        os.makedirs(BASE_DIR2, exist_ok=True)
        with open(STOP_FILE, "w", encoding="utf-8") as f:
            f.write(f"flatten\nrequested_at={time.time()}\n")
    except OSError as e:
        await update.message.reply_text(
            f"❌ Could not write stop request:\n`{e}`", parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "🛑 Stop requested — waiting up to 120s for graceful flatten + exit…",
        parse_mode="Markdown",
    )

    parent = psutil.Process(proc.pid)
    deadline = time.time() + 120.0
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        if not parent.is_running() or parent.status() == psutil.STATUS_ZOMBIE:
            if os.path.exists(PID_FILE):
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass
            if os.path.exists(STOP_FILE):
                try:
                    os.remove(STOP_FILE)
                except OSError:
                    pass
            await update.message.reply_text(
                "✅ Process exited after graceful stop.", parse_mode="Markdown"
            )
            return

    # Still alive — last resort. Prefer terminate over kill so Windows can
    # still deliver CTRL_BREAK if the process group handles it.
    try:
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.terminate()
        try:
            parent.wait(timeout=10)
            msg = (
                "⚠️ Graceful stop timed out; process terminated after 120s. "
                "Verify the broker book is flat."
            )
        except psutil.TimeoutExpired:
            parent.kill()
            msg = (
                "⚠️ Force-killed after graceful stop timeout. "
                "MANUAL BROKER FLATTEN REQUIRED if any F&O qty remains."
            )
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to stop process:\n`{str(e)}` — check broker book by hand.",
            parse_mode="Markdown",
        )


async def status_algo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    proc = get_active_process()
    if proc:
        cpu_usage = proc.cpu_percent(interval=0.1)
        mem_info = proc.memory_info().rss / (1024 * 1024) 
        status_msg = (
            f"🟢 **STATUS: RUNNING**\n"
            f"• **PID:** `{proc.pid}`\n"
            f"• **CPU:** `{cpu_usage}%`\n"
            f"• **Memory:** `{mem_info:.1f} MB`\n"
        )
    else:
        status_msg = "🔴 **STATUS: STOPPED**\n"

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            tail = "".join(lines[-5:]) if lines else "No entries."
            status_msg += f"\n*Recent Output:*\n```text\n{tail}\n```"

    await update.message.reply_text(status_msg, parse_mode="Markdown")


async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    n_lines = 20
    if context.args:
        try:
            n_lines = int(context.args[0])
        except ValueError:
            pass

    if not os.path.exists(LOG_FILE):
        await update.message.reply_text("Log file does not exist yet.")
        return

    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
        tail = "".join(lines[-n_lines:]) if lines else "Log file is empty."

    if len(tail) > 3500:
        tail = tail[-3500:]

    await update.message.reply_text(f"```text\n{tail}\n```", parse_mode="Markdown")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("get", get_env))
    app.add_handler(CommandHandler("set", set_env))
    app.add_handler(CommandHandler("run", run_algo))
    app.add_handler(CommandHandler("stock", run_stock))
    app.add_handler(CommandHandler("stop", stop_algo))
    app.add_handler(CommandHandler("status", status_algo))
    app.add_handler(CommandHandler("logs", view_logs))

    print("Bot is listening for commands...")
    app.run_polling()


if __name__ == "__main__":
    main()