from hubctl_cli import animation


def test_discover_finds_default_animation():
    anims = animation.discover_animations()
    names = [a.name for a in anims]
    assert "fly-to-flock" in names


def test_select_falls_back_to_first_when_missing():
    anims = animation.discover_animations()
    chosen = animation.select_animation(anims, name="does-not-exist")
    assert chosen is anims[0]


def test_select_by_name():
    anims = animation.discover_animations()
    chosen = animation.select_animation(anims, name="fly-to-flock")
    assert chosen.name == "fly-to-flock"
