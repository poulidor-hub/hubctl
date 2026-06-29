from hubctl_cli import animation


def test_discover_finds_animations():
    anims = animation.discover_animations()
    names = [a.name for a in anims]
    assert "hub-ascii" in names
    assert "fly-to-flock" in names


def test_select_default_is_hub_ascii():
    anims = animation.discover_animations()
    assert animation.select_animation(anims).name == "hub-ascii"
    assert animation.select_animation(anims, name="does-not-exist").name == "hub-ascii"


def test_select_by_name():
    anims = animation.discover_animations()
    chosen = animation.select_animation(anims, name="fly-to-flock")
    assert chosen.name == "fly-to-flock"
