"""Drive the Finch REPL in a pseudo-terminal and print what the screen shows (dev tool).

Unit tests can't see prompt_toolkit menus, toolbars or rich rendering; this can. Keystrokes are
sent one argument at a time, and a few step words control timing and output:

    SNAP        print the current screen (then a ==== separator)
    SLEEP<n>    let the app run for n seconds
    WAIT<n>     wait up to n seconds for the prompt to come back after a model turn

Example (Settings › Schedule, move down twice, open the row, then escape twice):

    .venv/bin/python scripts/pty_drive.py --home /tmp/fh --cwd /tmp/work -- \\
        $'/settings\\r' 5 SNAP $'\\x1b[B' $'\\x1b[B' $'\\r' SNAP $'\\x1b' SLEEP1 $'\\x1b'

Keys: enter $'\\r', esc $'\\x1b', up/down $'\\x1b[A' / $'\\x1b[B', ctrl+c $'\\x03', ctrl+u $'\\x15'.
Always pass a scratch --home so the real ~/.finch (config, sessions, scheduled jobs) is untouched.
Needs: pip install pexpect pyte  (the `dev` extra).
"""
import argparse
import os
import shutil
import sys
import time

import pexpect
import pyte

ap = argparse.ArgumentParser()
ap.add_argument("--home", required=True, help="scratch FINCH_HOME")
ap.add_argument("--cwd", default=os.getcwd(), help="working directory for finch -C")
ap.add_argument("-m", "--model", default="gemma4:12b")
ap.add_argument("--size", default="110x45", help="COLSxROWS")
ap.add_argument("--prompt", default="❯", help="prompt glyph WAIT looks for (theme-dependent)")
ap.add_argument("steps", nargs="*")
args = ap.parse_args()
cols, rows = (int(x) for x in args.size.split("x"))
finch = os.path.join(os.path.dirname(sys.executable), "finch")
finch = finch if os.path.exists(finch) else shutil.which("finch")

child = None


class TermScreen(pyte.Screen):
    def write_process_input(self, data):
        # Answer cursor-position requests (ESC[6n) like a real terminal. Without this,
        # prompt_toolkit never learns the height and hides every bottom toolbar.
        child.send(data)


screen = TermScreen(cols, rows)
stream = pyte.ByteStream(screen)
env = dict(os.environ, FINCH_HOME=os.path.abspath(args.home), TERM="xterm-256color",
           COLUMNS=str(cols), LINES=str(rows))
child = pexpect.spawn(finch, ["-m", args.model, "-C", args.cwd], env=env, dimensions=(rows, cols), timeout=600)


def pump(t):
    end = time.time() + t
    while time.time() < end:
        try:
            stream.feed(child.read_nonblocking(65536, timeout=0.3))
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            return


def show():
    print("\n".join(line.rstrip() for line in screen.display).rstrip())


pump(4)
for step in args.steps:
    if step == "SNAP":
        pump(1.5)
        show()
        print("=" * 40)
    elif step.startswith("SLEEP"):
        pump(float(step[5:] or 1))
    elif step.startswith("WAIT"):
        deadline = time.time() + float(step[4:] or 300)
        while time.time() < deadline:
            pump(2)
            if any(line.startswith(args.prompt) for line in screen.display[-8:]):
                break
    else:
        child.send(step)
        pump(1.5)
pump(2)
show()
child.terminate(force=True)
