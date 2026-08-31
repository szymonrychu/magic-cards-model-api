from mtg_agent.wrapper.ring import RING_CAP, Ring, strip_ansi, strip_ws


def test_strip_ansi_removes_csi_osc_and_stray_escapes():
    assert strip_ansi("\x1b[1;32mhello\x1b[0m") == "hello"
    assert strip_ansi("\x1b]0;title\x07body") == "body"
    assert strip_ansi("\x1b=alt\x1b>") == "alt"


def test_strip_ansi_keeps_newlines_and_tabs_but_drops_other_controls():
    assert strip_ansi("a\nb\tc\x00d\x07e") == "a\nb\tcde"


def test_strip_ws_drops_every_kind_of_space():
    assert strip_ws(" a\tb\nc\r d ") == "abcd"


def test_contains_ignores_ansi_and_whitespace():
    """The TUI lays words out with cursor moves, so on-screen text carries no
    literal spaces once the escapes are stripped."""
    ring = Ring()
    ring.write(b"\x1b[2J\x1b[HBypass\x1b[3C Permissions\r\n   mode\x1b[0m")
    assert ring.contains("Bypass Permissions mode")
    assert not ring.contains("Trust this folder")


def test_written_is_monotonic_across_reset():
    ring = Ring()
    ring.write(b"abc")
    ring.reset()
    ring.write(b"de")
    assert ring.written() == 5
    assert ring.tail(100) == "de"


def test_reset_clears_the_matchable_buffer():
    ring = Ring()
    ring.write(b"Bypass Permissions mode")
    assert ring.contains("Bypass Permissions mode")
    ring.reset()
    assert not ring.contains("Bypass Permissions mode")


def test_buffer_is_capped_but_keeps_the_newest_bytes():
    ring = Ring()
    ring.write(b"x" * (RING_CAP + 100))
    ring.write(b"NEEDLE")
    assert ring.contains("NEEDLE")
    assert len(ring.tail(RING_CAP * 2)) <= RING_CAP


def test_tail_returns_at_most_n_de_ansied_bytes():
    ring = Ring()
    ring.write(b"\x1b[31mabcdefghij")
    assert ring.tail(4) == "ghij"


def test_undecodable_bytes_do_not_raise():
    ring = Ring()
    ring.write(b"\xff\xfe ok")
    assert "ok" in ring.tail(100)
