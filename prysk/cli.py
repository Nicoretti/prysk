"""The command line interface implementation"""
import argparse
import configparser
import os
import shlex
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from prysk.process import execute
from prysk.run import runtests
from prysk.settings import merge_settings, settings_from
from prysk.xunit import runxunit

VERSION = "0.11.0"


class ArgumentParser:
    """argparse.Argumentparser compatible argument parser which allows inspection of options supported by the parser"""

    @classmethod
    def create_parser(cls):
        parser = cls(
            usage="prysk [OPTIONS] TESTS...",
            prog="prysk",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        parser.add_argument(
            "tests",
            metavar="TESTS",
            type=Path,
            nargs="+",
            help="Path(s) to the tests to be executed",
        )
        parser.add_argument("-V", "--version", action="version", version=VERSION)
        parser.add_argument(
            "-q", "--quiet", action="store_true", help="don't print diffs"
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="show filenames and test status",
        )
        parser.add_argument(
            "-i",
            "--interactive",
            action="store_true",
            help="interactively merge changed test output",
        )
        parser.add_argument(
            "-d",
            "--debug",
            action="store_true",
            help="write script output directly to the terminal",
        )
        parser.add_argument(
            "-y", "--yes", action="store_true", help="answer yes to all questions"
        )
        parser.add_argument(
            "-n", "--no", action="store_true", help="answer no to all questions"
        )
        parser.add_argument(
            "-E",
            "--preserve-env",
            action="store_true",
            help="don't reset common environment variables",
        )
        parser.add_argument(
            "--keep-tmpdir",
            action="store_true",
            help="keep temporary directories",
        )
        parser.add_argument(
            "--shell",
            action="store",
            default="/bin/sh",
            metavar="PATH",
            help="shell to use for running tests",
        )
        parser.add_argument(
            "--shell-opts",
            action="store",
            metavar="OPTS",
            help="arguments to invoke shell with",
        )
        parser.add_argument(
            "--indent",
            action="store",
            default=2,
            metavar="NUM",
            type=int,
            help="number of spaces to use for indentation",
        )
        parser.add_argument(
            "--xunit-file",
            action="store",
            metavar="PATH",
            help="path to write xUnit XML output",
        )
        return parser

    def __init__(self, *args, **kwargs):
        self._options = []
        self._parser = argparse.ArgumentParser(*args, **kwargs)

    def add_argument(self, *args, **kwargs):
        """See argparser.Argumentparser:add_argument"""

        def is_boolean_option(a):
            return action.nargs is not None and isinstance(action.const, bool)

        action = self._parser.add_argument(*args, **kwargs)
        if not action.type:
            _type = bool if is_boolean_option(action) else None
        else:
            _type = action.type
        self._options.append((_type, action.dest))
        return action

    def __getattr__(self, item):
        return getattr(self._parser, item)

    @property
    def options(self):
        """
        Normalized options and their type except for -V, --version and -h, --help.

        :return: an iterable containing all boolean options.
        :rtype: Iterable[Tuple(type, str)]
        """
        return self._options


def load(config, supported_options, section="prysk"):
    """
    Load configuration options from a init style format config file.

    :param supported_options: iterable of options and their type which should be collected.
    :param section: which contains the options.
    """
    parser = configparser.ConfigParser()
    parser.read(config)
    dispatcher = defaultdict(
        lambda: (parser.get, "--{}: invalid value: {!r}"),
        {
            bool: (parser.getboolean, "--{}: invalid boolean value: {!r}"),
            int: (parser.getint, "--{}: invalid integer value: {!r}"),
        },
    )
    if not parser.has_section(section):
        return {}

    config = {}
    for _type, option in supported_options:
        if not parser.has_option(section, option):
            continue
        try:
            fetch, error_msg = dispatcher[_type]
            config[option] = fetch(section, option)
        except ValueError as ex:
            fetch, error_msg = dispatcher[_type]
            value = parser.get(section, option)
            raise ValueError(error_msg.format(option, value)) from ex
    return config


def _which(cmd):
    """Return the path to cmd or None if not found"""
    cmd = os.fsencode(cmd)
    for p in os.environ["PATH"].split(os.pathsep):
        path = os.path.join(os.fsencode(p), cmd)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    return None


def _conflicts(settings):
    conflicts = [
        ("--yes", settings.yes, "--no", settings.no),
        ("--quiet", settings.quiet, "--interactive", settings.interactive),
        ("--debug", settings.debug, "--quiet", settings.quiet),
        ("--debug", settings.debug, "--interactive", settings.interactive),
        ("--debug", settings.debug, "--verbose", settings.verbose),
        ("--debug", settings.debug, "--xunit-file", settings.xunit_file),
    ]
    for s1, o1, s2, o2 in conflicts:
        if o1 and o2:
            return s1, s2


def _env_args(var, env=None):
    env = env if env else os.environ
    args = env.get(var, "").strip()
    return shlex.split(args)


def _expandpath(path):
    """Expands ~ and environment variables in path"""
    return os.path.expanduser(os.path.expandvars(path))


def main(argv=None):
    """Main entry point.

    If you're thinking of using Cram in other Python code (e.g., unit tests),
    consider using the test() or testfile() functions instead.

    :param argv: Script arguments (excluding script name)
    :type argv: iterable of strings
    :return: Exit code (non-zero on failure)
    :rtype: int
    """
    argv = sys.argv[1:] if argv is None else argv
    argv.extend(_env_args("PRYSK"))
    parser = ArgumentParser.create_parser()
    args = parser.parse_args(argv)

    try:
        configuration_settings = settings_from(
            load(
                Path(_expandpath(os.environ.get("PRYSKRC", ".pryskrc"))), parser.options
            )
        )
    except ValueError as ex:
        sys.stderr.write(f"{parser.format_usage()}\n")
        sys.stderr.write(f"prysk: error: {ex}\n")
        return 2

    argument_settings = settings_from(args)
    settings = merge_settings(argument_settings, configuration_settings)

    conflict = _conflicts(settings)
    if conflict:
        arg1, arg2 = conflict
        sys.stderr.write(f"options {arg1} and {arg2} are mutually exclusive\n")
        return 2

    shellcmd = _which(settings.shell)
    if not shellcmd:
        sys.stderr.buffer.write(b"shell not found: %s\n" % os.fsencode(settings.shell))
        return 2
    shell = [shellcmd]
    if settings.shell_opts:
        shell += shlex.split(settings.shell_opts)

    patchcmd = None
    if settings.interactive:
        patchcmd = _which("patch")
        if not patchcmd:
            sys.stderr.write("patch(1) required for -i\n")
            return 2

    badpaths = [path for path in settings.tests if not path.exists()]
    if badpaths:
        sys.stderr.buffer.write(b"no such file: %s\n" % badpaths[0])
        return 2

    if settings.yes:
        answer = "y"
    elif settings.no:
        answer = "n"
    else:
        answer = None

    tmpdir = os.environ["PRYSK_TEMP"] = tempfile.mkdtemp("", "prysk-tests-")
    tmpdirb = Path(tmpdir)
    proctmp = Path(tmpdir, "tmp")
    for s in ("TMPDIR", "TEMP", "TMP"):
        os.environ[s] = f"{proctmp}"

    os.mkdir(proctmp)
    try:
        tests = runtests(
            settings.tests,
            tmpdirb,
            shell,
            indent=settings.indent,
            cleanenv=not settings.preserve_env,
            debug=settings.debug,
        )
        if not settings.debug:
            tests = runcli(
                tests,
                quiet=settings.quiet,
                verbose=settings.verbose,
                patchcmd=patchcmd,
                answer=answer,
            )
            if settings.xunit_file is not None:
                tests = runxunit(tests, settings.xunit_file)

        hastests = False
        failed = False
        for path, test in tests:
            hastests = True
            _, _, diff = test()
            if diff:
                failed = True

        if not hastests:
            sys.stderr.write("no tests found\n")
            return 2

        return int(failed)
    finally:
        if settings.keep_tmpdir:
            sys.stdout.buffer.write(b"# Kept temporary directory: %s\n" % tmpdirb)
        else:
            shutil.rmtree(tmpdir)


def _prompt(question, answers, auto=None):
    """Write a prompt to stdout and ask for answer in stdin.

    answers should be a string, with each character a single
    answer. An uppercase letter is considered the default answer.

    If an invalid answer is given, this asks again until it gets a
    valid one.

    If auto is set, the question is answered automatically with the
    specified value.
    """
    default = [c for c in answers if c.isupper()]
    while True:
        sys.stdout.write(f"{question} [{answers}] ")
        sys.stdout.flush()
        if auto is not None:
            sys.stdout.write(auto + "\n")
            sys.stdout.flush()
            return auto

        answer = sys.stdin.readline().strip().lower()
        if not answer and default:
            return default[0]
        elif answer and answer in answers.lower():
            return answer


def _log(msg=None, verbosemsg=None, verbose=False):
    """Write msg to standard out and flush.

    If verbose is True, write verbosemsg instead.
    """
    if verbose:
        msg = verbosemsg
    if msg:
        if isinstance(msg, bytes):
            sys.stdout.buffer.write(msg)
        else:
            sys.stdout.write(msg)
        sys.stdout.flush()


def _patch(cmd, diff):
    """Run echo [lines from diff] | cmd -p0"""
    _, retcode = execute([cmd, "-p0"], stdin=b"".join(diff))
    return retcode == 0


def runcli(tests, quiet=False, verbose=False, patchcmd=None, answer=None):
    """Run tests with command line interface input/output.

    tests should be a sequence of 2-tuples containing the following:

        (test path, test function)

    This function yields a new sequence where each test function is wrapped
    with a function that handles CLI input/output.

    If quiet is True, diffs aren't printed. If verbose is True,
    filenames and status information are printed.

    If patchcmd is set, a prompt is written to stdout asking if
    changed output should be merged back into the original test. The
    answer is read from stdin. If 'y', the test is patched using patch
    based on the changed output.
    """
    total, skipped, failed = [0], [0], [0]

    for path, test in tests:

        def testwrapper():
            """Test function that adds CLI output"""
            total[0] += 1
            _log(None, f"{Path(path.parent.name, path.name)}: ", verbose)

            refout, postout, diff = test()
            if refout is None:
                skipped[0] += 1
                _log("s", "empty\n", verbose)
                return refout, postout, diff

            errpath = Path(f"{path}" + ".err")
            if postout is None:
                skipped[0] += 1
                _log("s", "skipped\n", verbose)
            elif not diff:
                _log(".", "passed\n", verbose)
                if errpath.exists():
                    os.remove(errpath)
            else:
                failed[0] += 1
                _log("!", "failed\n", verbose)
                if not quiet:
                    _log("\n", None, verbose)

                with open(errpath, "wb") as errfile:
                    for line in postout:
                        errfile.write(line)

                if not quiet:
                    origdiff = diff
                    diff = []
                    for line in origdiff:
                        sys.stdout.buffer.write(line)
                        diff.append(line)

                    if patchcmd and _prompt("Accept this change?", "yN", answer) == "y":
                        if _patch(patchcmd, diff):
                            _log(None, f"{path}: merged output\n", verbose)
                            os.remove(errpath)
                        else:
                            _log(f"{path}: merge failed\n")

            return refout, postout, diff

        yield path, testwrapper

    if total[0] > 0:
        _log("\n", None, verbose)
        _log(f"# Ran {total[0]} tests, {skipped[0]} skipped, {failed[0]} failed.\n")
