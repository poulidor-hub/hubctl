from hubctl_cli import canvas


def test_geometry_exact_fit():
    g = canvas.canvas_geometry(100, 30, 100, 30)
    assert g["origin_col"] == 0
    assert g["origin_row"] == 0
    assert g["is_too_small"] is False


def test_geometry_larger_terminal():
    g = canvas.canvas_geometry(120, 40, 100, 30)
    assert g["origin_col"] == 10
    assert g["origin_row"] == 5
    assert g["is_too_small"] is False


def test_geometry_too_narrow():
    g = canvas.canvas_geometry(80, 40, 100, 30)
    assert g["is_too_small"] is True


def test_geometry_too_short():
    g = canvas.canvas_geometry(120, 20, 100, 30)
    assert g["is_too_small"] is True


def test_geometry_inner_size():
    g = canvas.canvas_geometry(100, 30, 100, 30)
    assert g["inner_cols"] == 98
    assert g["inner_rows"] == 28


def test_geometry_odd_adjustment():
    g = canvas.canvas_geometry(101, 31, 100, 30)
    assert g["origin_col"] == 0
    assert g["origin_row"] == 0
