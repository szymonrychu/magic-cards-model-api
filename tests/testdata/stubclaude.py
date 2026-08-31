"""A stand-in for the interactive claude binary, for the integration test.

It reproduces only the three behaviours the wrapper actually depends on:

1. it prints the un-seedable "Bypass Permissions mode" warning at boot and
   refuses to accept input until it has seen Down then Enter;
2. it accepts a bracketed-paste block followed by a separate CR;
3. at end of "turn" it runs the Stop hook, feeding it a payload on stdin the
   same shape the real binary uses.

It is a test double for a subprocess, not for the rules engine: it never
touches a game.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


def main() -> None:
    hook = os.environ["STUB_HOOK_COMMAND"]
    transcript = os.environ.get("STUB_TRANSCRIPT", "")
    print("Bypass Permissions mode", flush=True)
    print("  Yes, I accept / No, exit", flush=True)

    accepted = False
    buffer = ""
    while True:
        char = sys.stdin.read(1)
        if not char:
            return
        buffer += char
        if not accepted:
            if buffer.endswith("\x1b[B\r") or buffer.endswith("\x1b[B\n"):
                accepted = True
                buffer = ""
                print("accepted, ready", flush=True)
            continue
        # NB: not `buffer.rstrip().endswith(("\r", "\n"))` - rstrip removes
        # exactly the characters that test looks for, so it is never true.
        if PASTE_END in buffer and buffer.endswith(("\r", "\n")):
            start = buffer.find(PASTE_START) + len(PASTE_START)
            text = buffer[start : buffer.find(PASTE_END)]
            buffer = ""
            print(f"working on: {text}", flush=True)
            payload = {
                "session_id": "stub-session",
                "transcript_path": transcript,
                "cwd": os.getcwd(),
                "hook_event_name": "Stop",
                "last_assistant_message": f"ACK:{text}",
            }
            subprocess.run(hook, shell=True, input=json.dumps(payload).encode(), check=False)
            print("turn done", flush=True)


if __name__ == "__main__":
    main()
