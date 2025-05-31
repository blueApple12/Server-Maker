import os
import requests
import shutil
import subprocess
import threading
import re
import logging
from threading import Lock

# Load configurable settings from environment variables
SERVER_DIR = os.environ.get("SERVER_DIR", "minecraft_server")
PLUGIN_URL = os.environ.get(
    "PLUGIN_URL",
    "https://github.com/playit-cloud/playit-minecraft-plugin/releases/latest/download/playit-minecraft-plugin.jar"
)

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# Global process state and output buffer
server_process = None
server_output = []
output_lock = Lock()
MAX_LOG_LINES = 300


def server_exists() -> bool:
    """
    Check whether the server directory already exists.
    """
    return os.path.isdir(SERVER_DIR)


def create_server(version: str, ram: int) -> bool:
    """
    Create the server directory, download the latest PaperMC build for the given version,
    set EULA, download Playit plugin, and record RAM allocation.
    Returns True on success, False on failure.
    """
    try:
        os.makedirs(SERVER_DIR, exist_ok=True)
        logger.info(f"Created/verified server directory: {SERVER_DIR}")

        # Fetch build info
        api_url = f"https://api.papermc.io/v2/projects/paper/versions/{version}"
        try:
            resp = requests.get(api_url, timeout=10)
            resp.raise_for_status()
            build_info = resp.json()
        except requests.RequestException as e:
            logger.error(f"Error fetching build info from PaperMC API: {e}")
            return False

        builds = build_info.get("builds")
        if not builds:
            logger.error(f"No builds found for version '{version}'.")
            return False
        latest_build = builds[-1]
        jar_url = (
            f"https://api.papermc.io/v2/projects/paper/versions/{version}/"
            f"builds/{latest_build}/downloads/paper-{version}-{latest_build}.jar"
        )
        jar_path = os.path.join(SERVER_DIR, "server.jar")

        # Download the server JAR
        try:
            with requests.get(jar_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(jar_path, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            logger.info(f"Downloaded PaperMC jar: {jar_path}")
        except requests.RequestException as e:
            logger.error(f"Error downloading PaperMC JAR: {e}")
            return False

        # Accept the EULA
        eula_path = os.path.join(SERVER_DIR, "eula.txt")
        with open(eula_path, "w") as f:
            f.write("eula=true\n")
        logger.info("Wrote eula.txt (accepted)")

        # Download Playit plugin
        plugins_dir = os.path.join(SERVER_DIR, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        plugin_path = os.path.join(plugins_dir, "playit-minecraft-plugin.jar")
        try:
            with requests.get(PLUGIN_URL, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(plugin_path, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            logger.info(f"Downloaded Playit plugin: {plugin_path}")
        except requests.RequestException as e:
            logger.error(f"Error downloading Playit plugin: {e}")
            return False

        # Record RAM allocation
        ram_file = os.path.join(SERVER_DIR, "ram.txt")
        with open(ram_file, "w") as f:
            f.write(str(ram))
        logger.info(f"Recorded RAM allocation ({ram} GB) in {ram_file}")

        return True

    except OSError as e:
        logger.error(f"Filesystem error during server creation: {e}")
        return False


def is_server_running() -> bool:
    """
    Return True if the Minecraft server subprocess is currently running.
    """
    global server_process
    return server_process is not None and server_process.poll() is None


def launch_server() -> None:
    """
    Launch the Minecraft server (via 'java -jar server.jar nogui') with the RAM settings from ram.txt.
    If a server is already running, attempt to stop it first.
    """
    global server_process

    if is_server_running():
        logger.info("Server is already running. Attempting to stop it first...")
        stop_server()
        try:
            server_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("Timeout while waiting for existing server to stop.")

    # Read RAM setting
    ram = 2  # default to 2 GB
    ram_file = os.path.join(SERVER_DIR, "ram.txt")
    if os.path.exists(ram_file):
        try:
            with open(ram_file) as f:
                ram_val = int(f.read().strip())
                if ram_val >= 1:
                    ram = ram_val
                else:
                    logger.warning(f"Invalid RAM value in {ram_file}; defaulting to 2 GB.")
        except ValueError:
            logger.warning(f"Non-integer RAM value in {ram_file}; defaulting to 2 GB.")

    # Clear previous logs
    with output_lock:
        server_output.clear()

    # Start the server process
    try:
        server_process = subprocess.Popen(
            [
                "java",
                f"-Xmx{ram}G",
                f"-Xms{ram}G",
                "-jar",
                "server.jar",
                "nogui",
            ],
            cwd=SERVER_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        logger.info(f"Launched Minecraft server with {ram} GB RAM.")
    except Exception as e:
        logger.error(f"Failed to launch Minecraft server: {e}")
        server_process = None
        return

    # Background thread to capture logs
    def stream_logs() -> None:
        assert server_process.stdout is not None
        for line in server_process.stdout:
            trimmed = line.strip()
            with output_lock:
                server_output.append(trimmed)
                if len(server_output) > MAX_LOG_LINES:
                    server_output.pop(0)

    t = threading.Thread(target=stream_logs, daemon=True)
    t.start()


def stop_server(timeout: int = 30) -> None:
    """
    Stop the running Minecraft server by sending 'stop' to its stdin.
    If it does not terminate within `timeout` seconds, forcibly kill it.
    """
    global server_process
    if not is_server_running():
        logger.info("No server process is running.")
        return

    try:
        server_process.stdin.write("stop\n")
        server_process.stdin.flush()
        server_process.wait(timeout=timeout)
        logger.info("Minecraft server stopped gracefully.")
    except Exception as e:
        logger.warning(f"Graceful stop failed ({e}); killing process...")
        try:
            server_process.kill()
            server_process.wait(timeout=timeout)
            logger.info("Minecraft server killed.")
        except Exception as kill_err:
            logger.error(f"Error killing Minecraft process: {kill_err}")
    finally:
        server_process = None


def get_logs() -> str:
    """
    Return the latest buffered server log output (up to MAX_LOG_LINES) as a single string.
    """
    with output_lock:
        if server_output:
            return "\n".join(server_output)
        else:
            return "Server is starting or not running yet..."


def send_command(cmd: str) -> bool:
    """
    Send a command to the running Minecraft server's stdin. Returns True if successful.
    """
    if not is_server_running():
        logger.warning("Attempted to send command, but server is not running.")
        return False
    try:
        server_process.stdin.write(cmd + "\n")
        server_process.stdin.flush()
        logger.info(f"Sent command to server: {cmd}")
        return True
    except Exception as e:
        logger.error(f"Failed to send command '{cmd}': {e}")
        return False


def delete_server() -> bool:
    """
    Stop the server if running, then delete the entire server directory.
    Returns True on success, False on failure.
    """
    global server_process
    if is_server_running():
        stop_server()

    try:
        if os.path.isdir(SERVER_DIR):
            shutil.rmtree(SERVER_DIR)
            logger.info(f"Deleted server directory: {SERVER_DIR}")
        return True
    except OSError as e:
        logger.error(f"Failed to delete server directory '{SERVER_DIR}': {e}")
        return False


def get_playit_status() -> tuple[bool, str | None, str | None]:
    """
    Scan the buffered logs (most recent lines first) for Playit-related messages.
    Returns a tuple: (tunnel_ready, claim_url, joinmc_domain).
      - If tunnel_ready is True, joinmc_domain is the raw domain (no https://).
      - If tunnel_ready is False but a claim_url exists, that URL can be used to claim a tunnel.
    """
    claim_url = None
    tunnel_ready = False
    joinmc_domain = None

    with output_lock:
        # Look at up to MAX_LOG_LINES most recent lines
        for line in reversed(server_output[-MAX_LOG_LINES:]):
            lower = line.lower()

            # 1) Look for “found minecraft java tunnel:” then grab the domain (bare, no https)
            if "found minecraft java tunnel" in lower:
                # Match either "something.joinmc.link" or "https://something.joinmc.link"
                match = re.search(r"(?:https?://)?([A-Za-z0-9-]+\.joinmc\.link)", line, re.IGNORECASE)
                if match:
                    # We explicitly take group(1), so it's always just the domain (no scheme).
                    joinmc_domain = match.group(1)  # e.g. "catalog-establishment.joinmc.link"
                    tunnel_ready = True
                    break

            # 2) If still no tunnel, look for “failed to exchange, to claim visit: https://playit.gg/mc/…”
            if "claim visit" in lower and not tunnel_ready:
                match = re.search(r"https://playit\.gg/mc/[A-Za-z0-9]+", line, re.IGNORECASE)
                if match:
                    claim_url = match.group(0)

    return tunnel_ready, claim_url, joinmc_domain
