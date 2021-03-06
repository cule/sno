import os
import re
import subprocess

import pygit2

from .timestamps import tz_offset_to_minutes


def get_head_tree(repo):
    """
    Returns the tree at the current repo HEAD.
    If there is no commit at HEAD - ie, head_is_unborn - returns None.
    """
    return None if repo.head_is_unborn else repo.head.peel(pygit2.Tree)


def get_head_commit(repo):
    """
    Returns the commit at the current repo HEAD.
    If there is no commit at HEAD - ie, head_is_unborn - returns None.
    """
    return None if repo.head_is_unborn else repo.head.peel(pygit2.Commit)


def get_head_branch(repo):
    """
    Returns the branch that HEAD is currently on.
    If HEAD is detached - meaning not on any branch - returns None
    """
    return None if repo.head_is_detached else repo.references["HEAD"].target


def get_head_branch_shorthand(repo):
    """
    Returns the shorthand for the branch that HEAD is currently on.
    If HEAD is detached - meaning not on any branch - returns None
    """
    return (
        None
        if repo.head_is_detached
        else repo.references["HEAD"].target.rsplit("/", 1)[-1]
    )


_GIT_VAR_OUTPUT_RE = re.compile(
    r"^(?P<name>.*) <(?P<email>[^>]*)> (?P<time>\d+) (?P<offset>[+-]?\d+)$"
)


def _signature(repo, person_type, **overrides):
    # 'git var' lets us use the environment variables to
    # control the user info, e.g. GIT_AUTHOR_DATE.
    # libgit2/pygit2 doesn't handle those env vars at all :(
    env = os.environ.copy()

    name = overrides.pop("name", None)
    if name is not None:
        env[f"GIT_{person_type}_NAME"] = name

    email = overrides.pop("email", None)
    if email is not None:
        env[f"GIT_{person_type}_EMAIL"] = email

    output = subprocess.check_output(
        ["git", "var", f"GIT_{person_type}_IDENT"],
        cwd=repo.path,
        encoding="utf8",
        env=env,
    )
    m = _GIT_VAR_OUTPUT_RE.match(output)
    kwargs = m.groupdict()
    kwargs["time"] = int(kwargs["time"])
    kwargs["offset"] = tz_offset_to_minutes(kwargs["offset"])
    kwargs.update(overrides)
    return pygit2.Signature(**kwargs)


def author_signature(repo, **overrides):
    return _signature(repo, "AUTHOR", **overrides)


def committer_signature(repo, **overrides):
    return _signature(repo, "COMMITTER", **overrides)
