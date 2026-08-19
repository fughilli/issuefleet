"""``bazel run //tools:takeover -- FUG-555`` == ``issuefleet takeover FUG-555``.

A thin entry point so the takeover flow has the first-class command line the
brief asked for; all the logic lives in ``issuefleet.takeover``.
"""

import sys

from issuefleet.cli import main

if __name__ == "__main__":
    sys.exit(main(["takeover", *sys.argv[1:]]))
