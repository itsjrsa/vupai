from pynput import keyboard

from voxpane.hotkey import Hotkey, MultiHotkey


def _counter():
    box = {"n": 0}

    def inc() -> None:
        box["n"] += 1

    return box, inc


def test_single_press_fires_on_press_once():
    pbox, pinc = _counter()
    rbox, rinc = _counter()
    hk = Hotkey("alt_r", on_press=pinc, on_release=rinc)

    hk._press(keyboard.Key.alt_r)

    assert pbox["n"] == 1
    assert rbox["n"] == 0


def test_held_autorepeat_fires_on_press_once():
    pbox, pinc = _counter()
    rbox, rinc = _counter()
    hk = Hotkey("alt_r", on_press=pinc, on_release=rinc)

    # pynput on_press fires repeatedly while held (auto-repeat)
    hk._press(keyboard.Key.alt_r)
    hk._press(keyboard.Key.alt_r)
    hk._press(keyboard.Key.alt_r)

    assert pbox["n"] == 1


def test_release_fires_on_release_once_and_resets():
    pbox, pinc = _counter()
    rbox, rinc = _counter()
    hk = Hotkey("alt_r", on_press=pinc, on_release=rinc)

    hk._press(keyboard.Key.alt_r)
    hk._release(keyboard.Key.alt_r)

    assert rbox["n"] == 1
    # after release, a new press is accepted again
    hk._press(keyboard.Key.alt_r)
    assert pbox["n"] == 2


def test_unrelated_keys_ignored():
    pbox, pinc = _counter()
    rbox, rinc = _counter()
    hk = Hotkey("alt_r", on_press=pinc, on_release=rinc)

    hk._press(keyboard.Key.alt_l)
    hk._press(keyboard.Key.space)
    hk._release(keyboard.Key.alt_l)

    assert pbox["n"] == 0
    assert rbox["n"] == 0


def test_release_without_held_does_not_fire():
    pbox, pinc = _counter()
    rbox, rinc = _counter()
    hk = Hotkey("alt_r", on_press=pinc, on_release=rinc)

    hk._release(keyboard.Key.alt_r)

    assert rbox["n"] == 0


# ---------------------------------------------------------------------------
# Fix 5: exceptions in callbacks must not propagate out of _press/_release
# ---------------------------------------------------------------------------

def test_press_exception_does_not_propagate():
    def bad_press():
        raise RuntimeError("boom")

    rbox, rinc = _counter()
    hk = Hotkey("alt_r", on_press=bad_press, on_release=rinc)

    # Must not raise even though on_press raises.
    hk._press(keyboard.Key.alt_r)
    # _held must still be True so the release is handled correctly.
    assert hk._held is True

    # A subsequent release must still work (listener thread stays alive).
    hk._release(keyboard.Key.alt_r)
    assert rbox["n"] == 1
    assert hk._held is False


def test_release_exception_does_not_propagate():
    pbox, pinc = _counter()

    def bad_release():
        raise ValueError("oops")

    hk = Hotkey("alt_r", on_press=pinc, on_release=bad_release)
    hk._press(keyboard.Key.alt_r)
    assert pbox["n"] == 1

    # Must not raise.
    hk._release(keyboard.Key.alt_r)
    # _held reset before callback, so it's False even after the exception.
    assert hk._held is False


def test_multi_two_keys_fire_independently():
    a, ainc = _counter()
    b, binc = _counter()
    ra, rainc = _counter()
    rb, rbinc = _counter()
    hk = MultiHotkey([("alt_r", ainc, rainc), ("ctrl_l", binc, rbinc)])

    hk._press(keyboard.Key.alt_r)
    hk._press(keyboard.Key.ctrl_l)
    assert a["n"] == 1 and b["n"] == 1

    hk._release(keyboard.Key.alt_r)
    assert ra["n"] == 1 and rb["n"] == 0


def test_multi_autorepeat_debounced_per_key():
    a, ainc = _counter()
    hk = MultiHotkey([("alt_r", ainc, lambda: None),
                      ("ctrl_l", lambda: None, lambda: None)])
    hk._press(keyboard.Key.alt_r)
    hk._press(keyboard.Key.alt_r)
    assert a["n"] == 1
    # the other key's state is independent
    hk._press(keyboard.Key.ctrl_l)
    hk._release(keyboard.Key.ctrl_l)
    hk._release(keyboard.Key.alt_r)
    assert a["n"] == 1


def test_multi_unbound_key_ignored():
    a, ainc = _counter()
    hk = MultiHotkey([("alt_r", ainc, lambda: None)])
    hk._press(keyboard.Key.space)
    hk._release(keyboard.Key.space)
    assert a["n"] == 0


def test_multi_release_without_press_no_fire():
    r, rinc = _counter()
    hk = MultiHotkey([("alt_r", lambda: None, rinc)])
    hk._release(keyboard.Key.alt_r)
    assert r["n"] == 0


def test_multi_callback_exception_isolated():
    def boom():
        raise RuntimeError("x")

    r, rinc = _counter()
    hk = MultiHotkey([("alt_r", boom, rinc)])
    hk._press(keyboard.Key.alt_r)                      # must not raise
    assert hk._held[keyboard.Key.alt_r] is True
    hk._release(keyboard.Key.alt_r)
    assert r["n"] == 1
