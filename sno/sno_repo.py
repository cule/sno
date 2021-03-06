import contextlib
import logging
import struct
import subprocess
import sys
from pathlib import Path

import click
import pygit2

from . import is_windows
from .exceptions import (
    translate_subprocess_exit_code,
    InvalidOperation,
    NotFound,
    NO_REPOSITORY,
)
from .repository_version import REPO_VERSIONS, get_repo_version
from .working_copy import WorkingCopy

L = logging.getLogger("sno.sno_repo")


class SnoRepoFiles:
    """Useful files that are found in `sno_repo.gitdir_path`"""

    # Standard git files:
    HEAD = "HEAD"
    INDEX = "index"
    COMMIT_EDITMSG = "COMMIT_EDITMSG"
    ORIG_HEAD = "ORIG_HEAD"
    MERGE_HEAD = "MERGE_HEAD"
    MERGE_MSG = "MERGE_MSG"

    # Sno-specific files:
    MERGE_INDEX = "MERGE_INDEX"
    MERGE_BRANCH = "MERGE_BRANCH"


class SnoConfigKeys:
    """
    Sno specifig config variables found in sno_repo.config
    (which is read from the file at `sno_repo.gitdir_path / "config"`)
    """

    SNO_REPOSITORY_VERSION = "sno.repository.version"
    SNO_WORKINGCOPY_PATH = "sno.workingcopy.path"
    SNO_WORKINGCOPY_BARE = "sno.workingcopy.bare"  # Older sno repos use this custom variable instead of core.bare
    CORE_BARE = "core.bare"  # Newer sno repos use the standard "core.bare" variable.


def _append_checksum(data):
    """Appends the 160-bit git-hash to the end of data"""
    return data + pygit2.hash(data).raw


class LockedGitIndex:
    """
    An empty index file, but extended with a required ".sno" extension in the extensions section of the index binary
    format. (Not the file extension - the filename is simply "index", it has no file extension.)
    Causes all git commands that would involve the index or working copy to fail with "unsupported extension: .sno" -
    in that sense it is "locked" to git. Various techniques can be used to unlock it if certain git functionality is
    needed - eg marking the repository as bare so it is ignored, or removing the unsupported extension.
    """

    GIT_INDEX_VERSION = 2
    BASE_EMPTY_GIT_INDEX = struct.pack(">4sII", b"DIRC", GIT_INDEX_VERSION, 0)

    # Extension name does not start with A-Z, therefore is a required extension.
    LOCKED_SNO_EXTENSION = struct.pack(">4sI", b".sno", 0)

    # See https://git-scm.com/docs/index-format

    LOCKED_EMPTY_GIT_INDEX = _append_checksum(
        BASE_EMPTY_GIT_INDEX + LOCKED_SNO_EXTENSION
    )


class SnoRepo(pygit2.Repository):
    """
    A valid pygit2.Repository, since all sno repos are also git repos - but with some added functionality.
    Ensures the git directory structure is one of the two styles supported by sno - "bare-style" or "tidy-style".
    For tidy-style, prevents worktree-related git commands from working by using a "locked git index".
    Helps set up sno specific config, and adds support for pathlib Paths.

    The two styles of Sno repos:
    Originally, all Sno repos were implemented as bare git repositorys. Some had GPKG working copies, some did not.
    Since they were bare git repositories, all the git internals were immediately visible inside the root directory -
    right alongside the GPKG. For this reason, they were kind of "untidy".

    Eventually, this style of repo was named a "bare-style" Sno repo. "Bare-style" Sno repo's are always implemented
    as bare git repositories, but they may or may not have a working copy, so they may or may not be actually "bare".

    A new style of sno repo was added - a "tidy-style" Sno repo. This type of Sno repo is implemented as a non-bare
    git repository, so the git internals are hidden in a ".sno" subfolder, leaving the root folder mostly empty as
    a place to put a GPKG file or similar. If a "tidy-style" Sno repo were to be reconfigured, it *could* have its
    working copy emoved and so be made bare. But going forward, "bare-style" Sno repos are supposed to be used for
    actual bare Sno repos, and "tidy-style" are supposed to be used for Sno repos with a working copy.

    Note: this is not enforced, especially since all legacy "bare-style" sno repos violate this assumption.
    """

    def __init__(self, root_path, *, validate=True):
        if isinstance(root_path, Path):
            root_path = str(root_path.resolve())

        try:
            super().__init__(root_path)
        except pygit2.GitError:
            raise NotFound("Not an existing sno repository", exit_code=NO_REPOSITORY)

        self.gitdir_path = Path(self.path).resolve()

        if self.is_tidy_style_sno_repo():
            self.workdir_path = self.gitdir_path.parent.resolve()
        else:
            self.workdir_path = self.gitdir_path

        if validate:
            self.validate_sno_repo_style()

    @classmethod
    def init_repository(cls, repo_root_path, repo_version, wc_path=None, bare=False):
        """
        Initialise a new sno repo. A sno repo is basically a git repo, except -
        - git internals are stored in .sno instead of .git
          (.git is a file that contains a reference to .sno, this is allowed by git)
        - datasets are stored in /.sno-dataset/ trees according to a particular dataset format version -
          see DATASETS_v2.md. But, this only matters when there are commits. At this stage they are not yet present.
        - there is a blob called sno.repository.version that contains the dataset format version number - but, this
          written in the first commit. At this stage it is not yet present.
        - there is property in the repo config called sno.repository.version that contains the dataset format version
          number, which is used until the sno.repository.version blob is written.
        - there are extra properties in the repo config about where / how the working copy is written.
        - the .sno/index file has been extended to stop git messing things up - see LOCKED_EMPTY_GIT_INDEX.
        """

        repo_root_path = repo_root_path.resolve()
        cls._ensure_exists_and_empty(repo_root_path)
        if not bare:
            WorkingCopy.check_valid_creation_path(wc_path, wc_path)

        if bare:
            # Create bare-style repo:
            sno_repo = cls._create_with_git_command(
                ["git", "init", "--bare", str(repo_root_path)],
                gitdir_path=repo_root_path,
            )
        else:
            # Create tidy-style repo:
            dot_sno_path = repo_root_path / ".sno"
            dot_init_path = repo_root_path / ".init"

            sno_repo = cls._create_with_git_command(
                [
                    "git",
                    "init",
                    f"--separate-git-dir={dot_sno_path}",
                    str(dot_init_path),
                ],
                gitdir_path=dot_sno_path,
                temp_workdir_path=dot_init_path,
            )
            sno_repo.lock_git_index()

        sno_repo.write_config(repo_version, wc_path, bare)
        sno_repo.write_readme()
        sno_repo.activate()
        return sno_repo

    @classmethod
    def clone_repository(
        cls, clone_url, repo_root_path, clone_args, wc_path=None, bare=False
    ):
        repo_root_path = repo_root_path.resolve()
        cls._ensure_exists_and_empty(repo_root_path)
        if not bare:
            WorkingCopy.check_valid_creation_path(wc_path, wc_path)

        if bare:
            sno_repo = cls._create_with_git_command(
                [
                    "git",
                    "clone",
                    "--bare",
                    *clone_args,
                    clone_url,
                    str(repo_root_path),
                ],
                gitdir_path=repo_root_path,
            )

        else:
            dot_sno_path = repo_root_path if bare else repo_root_path / ".sno"
            dot_clone_path = repo_root_path / ".clone"

            sno_repo = cls._create_with_git_command(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    f"--separate-git-dir={dot_sno_path}",
                    *clone_args,
                    clone_url,
                    str(dot_clone_path),
                ],
                gitdir_path=dot_sno_path,
                temp_workdir_path=dot_clone_path,
            )
            sno_repo.lock_git_index()

        sno_repo.write_config(get_repo_version(sno_repo), wc_path, bare)
        sno_repo.write_readme()
        sno_repo.activate()
        return sno_repo

    @classmethod
    def _create_with_git_command(cls, cmd, gitdir_path, temp_workdir_path=None):
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as e:
            sys.exit(translate_subprocess_exit_code(e.returncode))

        result = SnoRepo(gitdir_path, validate=False)

        # Tidy up temp workdir - this is created as a side effect of the git command.
        if temp_workdir_path is not None and temp_workdir_path.exists():
            if (temp_workdir_path / ".git").exists():
                (temp_workdir_path / ".git").unlink()
            temp_workdir_path.rmdir()

        return result

    def write_config(
        self,
        repo_version,
        wc_path=None,
        bare=False,
    ):
        repo_version = int(repo_version)
        if repo_version not in REPO_VERSIONS:
            raise click.UsageError(f"Unknown sno repo version {repo_version}")
        # Bare-style sno repos are always implemented as bare git repos:
        if self.is_bare_style_sno_repo():
            self.config["core.bare"] = True
        # Force writing to reflogs:
        self.config["core.logAllRefUpdates"] = "always"
        # Write sno repo version to config:
        self.config[SnoConfigKeys.SNO_REPOSITORY_VERSION] = str(repo_version)
        # Write working copy config:
        WorkingCopy.write_config(self, wc_path, bare)

    def write_readme(self):
        try:
            text = "\n".join(
                self.SNO_BARE_STYLE_README
                if self.is_bare_style_sno_repo()
                else self.SNO_TIDY_STYLE_README
            )
            readme_path = self.workdir_path / "SNO_README.txt"
            readme_path.write_text(text)
        except Exception as e:
            L.warn(e)

    def activate(self):
        """
        We create new+tidy repos in .sno/ but we don't write the .git file pointing to .sno/ until everything
        else is ready, and until that file is written, git or sno commands won't find the repo.
        So, if creation fails, the result will be something that doesn't work at all, not something that half
        works but is also half corrupted.
        """
        if self.is_bare_style_sno_repo():
            # Bare-style repos are always activated - since all the files are right there in the root directory,
            # we can't reveal them by writing the .git file. So, no action is required here.
            return

        dot_git_path = self.workdir_path / ".git"
        dot_sno_path = self.gitdir_path
        # .sno is linked from .git at this point, which means git (or sno) can find it
        # and so the repository is activated (ie, sno or git commands will work):
        dot_git_path.write_text("gitdir: .sno\n", encoding="utf-8")

        if is_windows:
            # Hide .git and .sno
            # Best effort: if it doesn't work for some reason, continue anyway.
            subprocess.call(["attrib", "+h", str(dot_git_path)])
            subprocess.call(["attrib", "+h", str(dot_sno_path)])

    def is_bare_style_sno_repo(self):
        """Bare-style sno repos are bare git repos. They are not "tidy": all of the git internals are visible."""
        return self.gitdir_path.stem != ".sno"

    def is_tidy_style_sno_repo(self):
        """Tidy-style sno repos are "tidy": they hide the git internals in a ".sno" directory."""
        return self.gitdir_path.stem == ".sno"

    def validate_sno_repo_style(self):
        if self.is_bare_style_sno_repo() and not super().is_bare:
            raise NotFound(
                "Selected repo isn't a bare-style or tidy-style sno repo. Perhaps a git repo?",
                exit_code=NO_REPOSITORY,
            )

    @property
    def BARE_CONFIG_KEY(self):
        """
        Return the config key we can check to see if the repo is actually bare,
        given that all bare-style Sno repos have core.bare = True regardless of whether they have a working copy.
        """
        return (
            SnoConfigKeys.SNO_WORKINGCOPY_BARE
            if self.is_bare_style_sno_repo()
            else SnoConfigKeys.CORE_BARE
        )

    @property
    def version(self):
        """Returns the sno repository version - eg 2 for 'Datasets V2' See DATASETS_v2.md"""
        return get_repo_version(self)

    @property
    def workingcopy_path(self):
        """Return the path to the sno working copy, if one exists."""
        repo_cfg = self.config
        path_key = SnoConfigKeys.SNO_WORKINGCOPY_PATH
        return Path(repo_cfg[path_key]) if path_key in repo_cfg else None

    @property
    def is_bare(self):
        """
        True if this sno repo is genuinely bare - it has no sno working copy.
        The repo may or may not also be a bare git repository - this is an implementation detail.
        That information can be found super().is_bare
        """
        repo_cfg = self.config
        bare_key = self.BARE_CONFIG_KEY
        return repo_cfg.get_bool(bare_key) if bare_key in repo_cfg else False

    def lock_git_index(self):
        (self.gitdir_path / SnoRepoFiles.INDEX).write_bytes(
            LockedGitIndex.LOCKED_EMPTY_GIT_INDEX
        )

    def ensure_state(self, allowed_states, bad_state_message=None, command_extra=None):
        """Ensures the sno repo is in one of the given states, or raises an error."""
        from .repo_files import RepoState

        RepoState.ensure_state(self, allowed_states, bad_state_message, command_extra)

    def del_config(self, key):
        config = self.config
        if key in config:
            del config[key]

    def gc(self, *args):
        """Runs git-gc on the sno repository."""
        try:
            args = ["git", "-C", self.path, "gc", *args]
            subprocess.check_call(args)
        except subprocess.CalledProcessError as e:
            sys.exit(translate_subprocess_exit_code(e.returncode))

    def _ensure_exists_and_empty(dir_path):
        if dir_path.exists() and any(dir_path.iterdir()):
            raise InvalidOperation(f'"{dir_path}" isn\'t empty')
        elif not dir_path.exists():
            dir_path.mkdir(parents=True)

    @contextlib.contextmanager
    def no_locked_index_file(self):
        try:
            (self.gitdir_path / SnoRepoFiles.INDEX).unlink()
        except FileNotFoundError:
            pass  # Use missing_ok once we have python 3.8
        try:
            yield
        finally:
            self.lock_git_index()

    SNO_COMMON_README = [
        "",
        "sno status",
        "",
        'It may simply output "Empty repository. Use sno import to add some data".',
        "Follow the tutorial at http://sno.earth/ for help getting started with Sno.",
        "",
        "Some more helpful commands for getting a broad view of what a Sno repository",
        "contains are:",
        "",
        "sno log      - show the history of what has been committed to this repository.",
        "sno data ls  - show the names of every dataset in this repository.",
        "",
        "This directory is the default location where Sno puts the repository's working",
        "copy, which is created as soon as there is some data to put in it. However",
        "the working copy can also be configured to be somewhere else, and may not be",
        "a file at all. To see the working copy's location, run this command:",
        "",
        "sno config sno.workingcopy.path",
        "",
        "",
    ]

    SNO_TIDY_STYLE_README = [
        "This directory is a Sno repository.",
        "",
        "It may look empty, but every version of every datasets that this repository",
        'contains is stored in Sno\'s internal format in the ".sno" hidden subdirectory.',
        "To check if a directory is a Sno repository and see what is stored, run:",
    ] + SNO_COMMON_README

    SNO_BARE_STYLE_README = [
        "This directory is a Sno repository.",
        "",
        "In this repository, the internals are visible - in files and in subdirectories",
        'like "HEAD", "objects" and "refs". These are best left untouched. Instead, use',
        "Sno commands to interact with the repository. To check if a directory is a Sno",
        "repository and see what is stored, run:",
    ] + SNO_COMMON_README
