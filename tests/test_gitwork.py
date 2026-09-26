"""`gitwork.run`: found and fixed while developing FD7, kept as its own
regression test - a run with no `input` must never inherit this process's
own stdin. Under some hosting shapes that stdin is a pipe nothing ever
closes, and a subprocess that tries to read from it (a git credential
prompt in production; `cat` here, so this test takes milliseconds rather
than depending on git's own prompting behaviour to reproduce it) blocks
forever instead of failing loudly - which is exactly the hang this pins
shut."""

from __future__ import annotations

from magic_cards_model_api.playscripts import gitwork


def test_run_with_no_input_does_not_inherit_stdin(tmp_path):
    # `cat` with no arguments echoes stdin until EOF; if `run` left stdin
    # inherited from this test process's own stdin, this call could hang
    # instead of returning at once with an empty read.
    done = gitwork.run(["cat"], tmp_path)
    assert done.returncode == 0
    assert done.stdout == ""


def test_run_with_input_still_pipes_it_through(tmp_path):
    done = gitwork.run(["cat"], tmp_path, input="hello\n")
    assert done.returncode == 0
    assert done.stdout == "hello\n"
