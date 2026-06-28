from hubctl_cli.__main__ import build_parser


def test_parser_flags():
    p = build_parser()
    args = p.parse_args(["--no-anim", "--dry-run", "--anim", "fly-to-flock"])
    assert args.no_anim is True
    assert args.dry_run is True
    assert args.anim == "fly-to-flock"


def test_parser_defaults():
    p = build_parser()
    args = p.parse_args([])
    assert args.no_anim is False
    assert args.dry_run is False
    assert args.anim is None
